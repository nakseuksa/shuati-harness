"""Layer 1 单元测试：EmbeddingClient 与 VectorStore（.npz 增量刷新 / 冻结分位 / 模型校验 / 锁）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from shuati.config import Settings
from shuati.db.connection import CompileLock, PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm.embeddings import EmbeddingClient
from shuati.vectors.store import VectorStore


@pytest.mark.asyncio
async def test_mock_embedding_determinism_and_similarity(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    init_db(conn)
    settings = Settings()
    settings.pipeline.use_mock_llm = True

    client = EmbeddingClient(settings, conn=conn, use_mock=True)
    vecs = await client.embed_texts(
        ["鸡兔同笼假设法求头数", "鸡兔同笼假设法求脚数", "圆锥体积与底面积积分公式"],
        stage="EMBED",
    )
    await client.aclose()

    assert vecs.shape == (3, EmbeddingClient.MOCK_DIM)
    assert vecs.dtype == np.float32
    norms = np.linalg.norm(vecs, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)
    sim_close = float(np.dot(vecs[0], vecs[1]))
    sim_far = float(np.dot(vecs[0], vecs[2]))
    assert sim_close > sim_far

    row = conn.execute("SELECT stage, slot, status FROM llm_calls ORDER BY call_id DESC LIMIT 1;").fetchone()
    assert row is not None
    assert row["stage"] == "EMBED"
    assert row["slot"] == "embedding"
    assert row["status"] == "OK"
    conn.close()


@pytest.mark.asyncio
async def test_openai_compatible_embedding_batch_and_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    init_db(conn)
    settings = Settings()
    settings.pipeline.use_mock_llm = False
    settings.llm.embedding.api_key = "test-key"
    settings.llm.embedding.batch_size = 2
    settings.llm.embedding.max_retries = 2

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, json={"error": "rate limit"})
        body = json.loads(request.content.decode("utf-8"))
        inputs = body["input"]
        data = [
            {"index": len(inputs) - 1 - idx, "embedding": [float(len(inputs) - idx), 1.0, 0.0, 0.0]}
            for idx in range(len(inputs))
        ]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 12}})

    transport = httpx.MockTransport(handler)
    client = EmbeddingClient(settings, conn=conn, use_mock=False, transport=transport)
    vecs = await client.embed_texts(["题A", "题B", "题C"], stage="EMBED_TEST")
    await client.aclose()

    assert attempts["count"] >= 3  # 第1批重试成功 + 第2批成功
    assert vecs.shape == (3, 4)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5)
    conn.close()


@pytest.mark.asyncio
async def test_vector_store_incremental_quantiles_and_model_guard(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "vectors")
    settings = Settings()
    settings.llm.embedding.model = "mock-embed-v1"
    client = EmbeddingClient(settings, use_mock=True)

    items = [
        ("q1", "已知长方形长为8厘米，宽为5厘米，求面积。"),
        ("q2", "已知长方形长为10厘米，宽为4厘米，求周长。"),
        ("q3", "甲乙两人同时从两地相向而行，3小时后相遇。"),
        ("q4", "一个数除以5余2，除以7余3，求最小正整数。"),
    ]
    stat1 = await store.sync_vectors("surface", items, client, scope_slug="primary_olympiad")
    assert stat1["total"] == 4
    assert stat1["embedded"] == 4
    assert stat1["reused"] == 0

    # 仅修改 q2，删除 q4，新增 q5 -> q1/q3 应被复用
    items2 = [
        ("q1", "已知长方形长为8厘米，宽为5厘米，求面积。"),
        ("q2", "已知正方形边长为6厘米，求面积。"),
        ("q3", "甲乙两人同时从两地相向而行，3小时后相遇。"),
        ("q5", "从1到100中共有多少个质数？"),
    ]
    stat2 = await store.sync_vectors("surface", items2, client, scope_slug="primary_olympiad")
    assert stat2["total"] == 4
    assert stat2["reused"] == 2
    assert stat2["embedded"] == 2
    assert stat2["removed"] == 1

    # 同步 skeleton 并生成冻结分位表
    await store.sync_vectors(
        "skeleton",
        [(uid, f"骨架:{txt}") for uid, txt in items2],
        client,
        scope_slug="primary_olympiad",
    )
    q_info = store.rebuild_quantiles("primary_olympiad", ("surface", "skeleton"), sample_pairs=200)
    assert q_info["meta_version"] == 1
    meta = store.load_meta()
    assert meta["embedding_model"] == "mock-embed-v1"
    assert len(meta["quantiles"]["primary_olympiad::surface"]) == 101

    pct_low = store.lookup_percentile("primary_olympiad", "surface", -1.0)
    pct_high = store.lookup_percentile("primary_olympiad", "surface", 1.0)
    assert 0.0 <= pct_low <= pct_high <= 1.0

    # 切换模型但未 force_rebuild 必须被阻断
    settings2 = Settings()
    settings2.llm.embedding.model = "new-embed-v2"
    client2 = EmbeddingClient(settings2, use_mock=True)
    with pytest.raises(PreconditionError, match="reindex --vectors --quantiles"):
        await store.sync_vectors("surface", items2, client2, scope_slug="primary_olympiad")

    # 跨进程/重入锁冲突检验
    with CompileLock(store.lock_path):
        assert store.lock_path.exists()
