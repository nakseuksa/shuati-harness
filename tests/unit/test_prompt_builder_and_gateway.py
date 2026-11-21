"""Prompt 白名单防泄漏、EXIF 方向矫正、多图显式标号与 LLMGateway 重试/账本单元测试。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from shuati.config import LLMSlotConfig, Settings
from shuati.db.connection import SingleWriterExecutor, get_connection
from shuati.db.schema import init_db
from shuati.llm import mocks as llm_mocks
from shuati.llm.gateway import LLMGateway, LLMGatewayError
from shuati.llm.prompt_builder import (
    STAGE1_PROMPT_VERSION,
    build_multi_solution_judge_messages,
    build_qualitative_judge_messages,
    build_review_resolve_messages,
    build_stage1_messages,
)
from shuati.models.schemas import (
    MultiSolutionJudgeOutput,
    QualitativeJudgeOutput,
    ReviewResolveOutput,
    Stage1Input,
    Stage1PrimaryOutput,
)


def _make_jpeg_with_exif_orientation_6(path: Path) -> None:
    """生成带 EXIF Orientation=6（需顺时针旋转 90°）的 40x20 图片；矫正后应变为 20x40。"""
    img = Image.new("RGB", (40, 20), color=(255, 0, 0))
    exif = img.getexif()
    exif[0x0112] = 6
    img.save(path, format="JPEG", exif=exif.tobytes())


def _extract_all_text_from_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                    elif item.get("type") == "image_url":
                        url = str(item.get("image_url", {}).get("url", ""))
                        # 不检查 base64 数据本身，只检查前缀非本地路径
                        parts.append(url[:40])
    return "\n".join(parts)


def test_stage1_input_schema_and_prompt_zero_leakage(tmp_path: Path) -> None:
    # 1. Stage1Input 类型签名严禁包含污染字段
    for forbidden_field in ("raw_chapter", "legacy_tags", "source_collection", "raw_images"):
        assert forbidden_field not in Stage1Input.model_fields

    assets_dir = tmp_path / "assets"
    assets_dir.mkdir(parents=True)
    img1 = assets_dir / "a1b2c3d4e5f60001.jpg"
    img2 = assets_dir / "a1b2c3d4e5f60002.jpg"
    _make_jpeg_with_exif_orientation_6(img1)
    Image.new("RGB", (30, 30), color=(0, 255, 0)).save(img2, format="JPEG")

    inp = Stage1Input(
        question_uid="primary_olympiad:ch28_平面直线图形高年级卷:ch28_001",
        grade_subject="小学奥数",
        question_text="如图，求两个阴影部分面积之和。",
        cas_images=["a1b2c3d4e5f60001.jpg", "a1b2c3d4e5f60002.jpg"],
        image_captions=["图1", "图2"],
        options={"A": "10", "B": "12"},
        has_images=True,
    )

    msgs = build_stage1_messages(inp, role="PRIMARY", assets_dir=assets_dir)
    all_text = _extract_all_text_from_messages(msgs)

    # 2. 章节/路径/UID 零泄漏断言
    assert inp.question_uid not in all_text
    assert "平面直线图形高年级卷" not in all_text
    assert "source_collection" not in all_text
    assert "chapter" not in all_text.lower()
    assert re.search(r"ch\d+_", all_text) is None
    assert "a1b2c3d4e5f60001" not in all_text
    assert str(assets_dir) not in all_text

    # 3. 多图显式编号与 EXIF 矫正验证
    user_content = msgs[1]["content"]
    assert isinstance(user_content, list)
    text_blocks = [b["text"] for b in user_content if b["type"] == "text"]
    assert "【配图 1（图1）】" in text_blocks
    assert "【配图 2（图2）】" in text_blocks

    img_blocks = [b for b in user_content if b["type"] == "image_url"]
    assert len(img_blocks) == 2
    first_url = img_blocks[0]["image_url"]["url"]
    assert first_url.startswith("data:image/jpeg;base64,")
    raw_bytes = base64.b64decode(first_url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw_bytes)) as decoded:
        # 原图 40x20 经 Orientation=6 矫正后应为 20x40
        assert decoded.size == (20, 40)


def test_other_prompt_builders() -> None:
    q_msgs = build_qualitative_judge_messages("(2)", "连接对角线", "作对角线", "求作法")
    assert len(q_msgs) == 2 and "连接对角线" in q_msgs[1]["content"]

    m_msgs = build_multi_solution_judge_messages("弦图法", "构造弦图...", "相似法", "利用沙漏相似...")
    assert len(m_msgs) == 2 and "弦图法" in m_msgs[1]["content"]

    r_msgs = build_review_resolve_messages("求面积", "正方形边长为4", "小学奥数")
    assert len(r_msgs) == 2 and "正方形边长为4" in r_msgs[1]["content"]


@pytest.mark.asyncio
async def test_gateway_mock_mode_and_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    init_db(conn)
    writer = SingleWriterExecutor()

    settings = Settings()
    settings.pipeline.use_mock_llm = True
    settings.llm.vlm_primary.model = "mock-vlm-1"
    raw_dir = tmp_path / "llm_raw"

    gw = LLMGateway(settings, conn=conn, writer=writer, llm_raw_dir=raw_dir)
    try:
        res = await gw.call_structured(
            slot="vlm_primary",
            stage="S1",
            messages=[{"role": "user", "content": "hi"}],
            schema=Stage1PrimaryOutput,
            question_uid="primary_olympiad:ch01:ch01_001",
            prompt_version=STAGE1_PROMPT_VERSION,
        )
        assert isinstance(res.parsed, Stage1PrimaryOutput)
        assert res.status == "OK"
        assert res.call_id is not None

        row = conn.execute("SELECT * FROM llm_calls WHERE call_id = ?", (res.call_id,)).fetchone()
        assert row is not None
        assert row["status"] == "OK"
        assert row["stage"] == "S1"
        assert Path(row["raw_response_path"]).exists()

        # 未注册 mock_key 抛异常且记入 FAILED
        with pytest.raises(LLMGatewayError) as exc_info:
            await gw.call_structured(
                slot="judge_llm",
                stage="S1_JUDGE",
                messages=[{"role": "user", "content": "hi"}],
                schema=QualitativeJudgeOutput,
                mock_key="missing_key_123",
            )
        assert exc_info.value.error_kind == "MOCK_KEY_UNREGISTERED"
        failed_row = conn.execute(
            "SELECT status FROM llm_calls ORDER BY call_id DESC LIMIT 1"
        ).fetchone()
        assert failed_row["status"] == "FAILED"

        # 测试自定义 MOCK_RESPONSES
        monkeypatch.setitem(
            llm_mocks.MOCK_RESPONSES,
            "custom_multi",
            {"is_distinct_method": True, "reason": "几何法 vs 代数法"},
        )
        mres = await gw.call_structured(
            slot="judge_llm",
            stage="S1_JUDGE",
            messages=[{"role": "user", "content": "hi"}],
            schema=MultiSolutionJudgeOutput,
            mock_key="custom_multi",
        )
        assert mres.parsed.is_distinct_method is True
    finally:
        await gw.aclose()
        conn.close()


@pytest.mark.asyncio
async def test_gateway_http_retries_and_schema_self_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "gw.db"
    conn = get_connection(db_path)
    init_db(conn)
    writer = SingleWriterExecutor()

    settings = Settings()
    settings.pipeline.use_mock_llm = False
    settings.llm.text_llm = LLMSlotConfig(
        base_url="https://mock.local/v1",
        api_key="sk-test",
        model="deepseek-chat",
        max_concurrency=2,
        rpm_limit=1200,
        max_retries=2,
    )

    call_log: list[dict[str, Any]] = []
    sleeps: list[float] = []

    valid_json = json.dumps(
        {
            "method_summary": "面积公式",
            "solution_markdown": "4*4=16",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "answer": "16",
                    "sympy_expr": "16",
                }
            ],
            "sympy_code": "def verify():\n    return {'主问': str(4*4)}\n",
        },
        ensure_ascii=False,
    )
    # 场景 1：第1次 429 -> 第2次返回非法 JSON -> 自动附错自愈重试1次成功 (SCHEMA_RETRY_OK)
    responses_queue = [
        httpx.Response(429, text="rate limit"),
        httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{bad json"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        ),
        httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": valid_json}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 15},
            },
        ),
    ]

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.is_closed = False

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
            call_log.append(json)
            return responses_queue.pop(0)

        async def aclose(self) -> None:
            self.is_closed = True

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    gw = LLMGateway(settings, conn=conn, writer=writer, llm_raw_dir=tmp_path / "raw")

    async def _fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr(gw, "_sleep", _fake_sleep)

    try:
        res = await gw.call_structured(
            slot="text_llm",
            stage="REVIEW_RESOLVE",
            messages=[{"role": "user", "content": "请重算"}],
            schema=ReviewResolveOutput,
            question_uid="primary_olympiad:ch01:ch01_001",
            prompt_version="r_v1",
        )
        assert res.status == "SCHEMA_RETRY_OK"
        assert res.parsed.final_answers[0].answer == "16"
        assert res.prompt_tokens == 30
        assert res.completion_tokens == 20
        assert sleeps == [1.0]
        assert len(call_log) == 3
        # 自愈消息中包含了上次报错信息
        assert "JSON" in call_log[2]["messages"][-1]["content"]

        row = conn.execute("SELECT status, raw_response_path FROM llm_calls WHERE call_id = ?", (res.call_id,)).fetchone()
        assert row["status"] == "SCHEMA_RETRY_OK"
        assert Path(row["raw_response_path"]).exists()

        # 场景 2：自愈重试仍失败 -> 记入 SCHEMA_RETRY_FAILED
        responses_queue.extend([
            httpx.Response(200, json={"choices": [{"message": {"content": "{still bad"}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": '{"equivalent": "not_bool"}'}}]}),
        ])
        with pytest.raises(LLMGatewayError) as exc_info:
            await gw.call_structured(
                slot="judge_llm",  # 自动回退复用 text_llm
                stage="S1_JUDGE",
                messages=[{"role": "user", "content": "judge"}],
                schema=QualitativeJudgeOutput,
            )
        assert exc_info.value.error_kind == "SCHEMA_RETRY_FAILED"
        last_row = conn.execute("SELECT status FROM llm_calls ORDER BY call_id DESC LIMIT 1").fetchone()
        assert last_row["status"] == "SCHEMA_RETRY_FAILED"

        # 场景 3：并发 Semaphore 与 4xx 不重试
        active = 0
        max_seen = 0

        class _ConcClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.is_closed = False

            async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
                nonlocal active, max_seen
                active += 1
                max_seen = max(max_seen, active)
                await asyncio.sleep(0.02)
                active -= 1
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": valid_json}}]},
                )

            async def aclose(self) -> None:
                self.is_closed = True

        monkeypatch.setattr(httpx, "AsyncClient", _ConcClient)
        await gw.aclose()
        await asyncio.gather(
            *[
                gw.call_structured(
                    slot="text_llm",
                    stage="REVIEW_RESOLVE",
                    messages=[{"role": "user", "content": f"q{i}"}],
                    schema=ReviewResolveOutput,
                )
                for i in range(5)
            ]
        )
        assert max_seen <= 2
    finally:
        await gw.aclose()
        conn.close()
