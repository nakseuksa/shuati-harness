"""向量与嵌入基础设施：异步 Embedding 客户端（OpenAI 兼容 / Mock / local_st）。"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from shuati.config import EmbeddingConfig, Settings
from shuati.db.connection import PreconditionError, SingleWriterExecutor


class EmbeddingError(RuntimeError):
    """Embedding 请求或本地编码失败。"""

    def __init__(self, message: str, *, error_kind: str = "EMBEDDING_ERROR") -> None:
        super().__init__(message)
        self.error_kind = error_kind


class _AsyncRPMBucket:
    """简单异步 RPM 令牌桶。"""

    def __init__(self, rpm: int) -> None:
        self.rpm = max(0, int(rpm))
        self.capacity = float(max(1, self.rpm))
        self.tokens = self.capacity
        self.rate = self.rpm / 60.0 if self.rpm > 0 else 0.0
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if self.rpm <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self.updated_at)
                self.updated_at = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_sec = (1.0 - self.tokens) / self.rate
            await sleep_fn(min(wait_sec, 1.0))


def _l2_normalize_rows(arr: np.ndarray) -> np.ndarray:
    """对二维向量矩阵按行做 L2 归一化，零向量兜底第一维为 1.0。"""
    if arr.size == 0:
        return arr.astype(np.float32)
    mat = np.asarray(arr, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat.reshape(1, -1)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    zero_mask = norms.squeeze(axis=1) <= 1e-12
    if np.any(zero_mask):
        mat = mat.copy()
        mat[zero_mask, 0] = 1.0
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return (mat / norms).astype(np.float32)


def _mock_embed_single(text: str, dim: int = 64) -> np.ndarray:
    """基于文本 SHA-256 与字符 1/2/3-gram 生成确定性局部相似伪向量。"""
    vec = np.zeros(dim, dtype=np.float32)
    norm_text = " ".join((text or "").split())
    if not norm_text:
        vec[0] = 1.0
        return vec

    # 字符 n-gram 特征提供稳定的字面局部相似度，便于单测验证聚类与最近邻召回
    features: list[tuple[str, float]] = []
    for ch in norm_text:
        if not ch.isspace():
            features.append((f"1:{ch}", 0.5))
    for i in range(max(0, len(norm_text) - 1)):
        features.append((f"2:{norm_text[i:i+2]}", 1.2))
    for i in range(max(0, len(norm_text) - 2)):
        features.append((f"3:{norm_text[i:i+3]}", 1.5))

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[bucket] += sign * weight

    # 混合全文 SHA-256 扰动消除完全同字符异序碰撞
    full_digest = hashlib.sha256(norm_text.encode("utf-8")).digest()
    for i in range(min(dim, 32)):
        vec[(i * 7) % dim] += ((full_digest[i] / 255.0) - 0.5) * 0.25

    return _l2_normalize_rows(vec)[0]


class EmbeddingClient:
    """统一 Embedding 客户端，支持 OpenAI 兼容接口、本地 SentenceTransformer 与确定性 Mock。"""

    MOCK_DIM = 64

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection | None = None,
        writer: SingleWriterExecutor | None = None,
        *,
        use_mock: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.cfg: EmbeddingConfig = settings.llm.embedding
        self.conn = conn
        self.writer = writer
        self.use_mock = bool(use_mock or settings.pipeline.use_mock_llm)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(max(1, int(self.cfg.max_concurrency)))
        self._bucket = _AsyncRPMBucket(int(self.cfg.rpm_limit))
        self._fallback_db_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._st_model: Any = None

    @property
    def model_name(self) -> str:
        """用于 VectorStore meta.json 校验的模型标识。"""
        return self.cfg.model

    def _record_model_name(self) -> str:
        return f"mock/{self.cfg.model}" if self.use_mock else self.cfg.model

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(float(self.cfg.timeout_seconds)),
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def _record_call(
        self,
        *,
        stage: str,
        status: str,
        prompt_tokens: int | None,
        latency_ms: int,
        error_message: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self.conn is None:
            return

        def _insert(tx_conn: sqlite3.Connection) -> int:
            cur = tx_conn.execute(
                """
                INSERT INTO llm_calls(
                    stage, slot, model_name, question_uid, prompt_version,
                    status, prompt_tokens, completion_tokens, latency_ms, est_cost, raw_response_path
                )
                VALUES (?, 'embedding', ?, NULL, 'embed-v1', ?, ?, 0, ?, NULL, '');
                """,
                (stage, self._record_model_name(), status, prompt_tokens, latency_ms),
            )
            call_id = int(cur.lastrowid)  # type: ignore[arg-type]
            if error_message or extra:
                import json

                dump_dir = Path("data/llm_raw")
                dump_dir.mkdir(parents=True, exist_ok=True)
                p = dump_dir / f"embed_{call_id}.json"
                p.write_text(
                    json.dumps(
                        {
                            "call_id": call_id,
                            "stage": stage,
                            "status": status,
                            "error": error_message,
                            "extra": extra or {},
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                tx_conn.execute(
                    "UPDATE llm_calls SET raw_response_path = ? WHERE call_id = ?",
                    (str(p), call_id),
                )
            return call_id

        if self.writer is not None:
            async with self.writer.async_transaction(self.conn) as tx_conn:
                _insert(tx_conn)
            return

        loop = asyncio.get_running_loop()
        lock = self._fallback_db_locks.setdefault(loop, asyncio.Lock())
        async with lock:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE;")
                try:
                    _insert(self.conn)
                    self.conn.commit()
                except BaseException:
                    self.conn.rollback()
                    raise
            else:
                _insert(self.conn)

    async def _embed_openai_batch(self, batch: list[str], stage: str) -> np.ndarray:
        if not self.cfg.api_key:
            raise PreconditionError(
                "未配置 EMBEDDING_API_KEY（或 llm.embedding.api_key），请先配置后再执行向量化，或传 --mock。"
            )
        url = f"{self.cfg.base_url.rstrip('/')}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": self.cfg.model, "input": batch}
        client = await self._get_client()
        max_retries = max(0, int(self.cfg.max_retries))
        last_error = "unknown"
        last_kind = "EMBEDDING_ERROR"

        t0 = time.perf_counter()
        for attempt in range(max_retries + 1):
            await self._bucket.acquire()
            async with self._sem:
                try:
                    resp = await client.post(url, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"网络错误: {exc!r}"
                    last_kind = "NETWORK_ERROR"
                else:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        last_kind = "HTTP_429" if resp.status_code == 429 else "HTTP_5XX"
                    elif resp.status_code >= 400:
                        latency_ms = int((time.perf_counter() - t0) * 1000)
                        await self._record_call(
                            stage=stage,
                            status="FAILED",
                            prompt_tokens=None,
                            latency_ms=latency_ms,
                            error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
                        )
                        raise EmbeddingError(
                            f"Embedding 返回不可重试错误 HTTP {resp.status_code}: {resp.text[:200]}",
                            error_kind="HTTP_4XX",
                        )
                    else:
                        data = resp.json()
                        items = sorted(data.get("data", []), key=lambda x: int(x.get("index", 0)))
                        if len(items) != len(batch):
                            raise EmbeddingError(
                                f"Embedding 返回条目数不匹配: expected={len(batch)}, got={len(items)}",
                                error_kind="BAD_ENVELOPE",
                            )
                        vecs = np.asarray([it["embedding"] for it in items], dtype=np.float32)
                        usage = data.get("usage") if isinstance(data, dict) else None
                        p_tok = int(usage["prompt_tokens"]) if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int) else None
                        latency_ms = int((time.perf_counter() - t0) * 1000)
                        await self._record_call(
                            stage=stage,
                            status="OK",
                            prompt_tokens=p_tok,
                            latency_ms=latency_ms,
                        )
                        return _l2_normalize_rows(vecs)
            if attempt < max_retries:
                await asyncio.sleep(float(2**attempt))

        latency_ms = int((time.perf_counter() - t0) * 1000)
        await self._record_call(
            stage=stage,
            status="FAILED",
            prompt_tokens=None,
            latency_ms=latency_ms,
            error_message=last_error,
        )
        raise EmbeddingError(
            f"Embedding 请求重试 {max_retries} 次后仍失败: {last_error}",
            error_kind=last_kind,
        )

    def _embed_local_st(self, texts: list[str]) -> np.ndarray:
        if self._st_model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise PreconditionError(
                    "provider='local_st' 需要安装 sentence-transformers，当前环境未安装。"
                ) from exc
            self._st_model = SentenceTransformer(self.cfg.model)
        vecs = self._st_model.encode(texts, normalize_embeddings=True)
        return _l2_normalize_rows(np.asarray(vecs, dtype=np.float32))

    async def embed_texts(self, texts: list[str], *, stage: str = "EMBED") -> np.ndarray:
        """批量将文本编码为 L2 归一化的 float32 矩阵。"""
        if not texts:
            return np.zeros((0, self.MOCK_DIM if self.use_mock else 0), dtype=np.float32)

        cleaned = [t if (t and t.strip()) else " " for t in texts]
        if self.use_mock:
            t0 = time.perf_counter()
            vecs = np.stack([_mock_embed_single(t, dim=self.MOCK_DIM) for t in cleaned], axis=0)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await self._record_call(
                stage=stage,
                status="OK",
                prompt_tokens=None,
                latency_ms=latency_ms,
            )
            return vecs.astype(np.float32)

        if self.cfg.provider == "local_st":
            t0 = time.perf_counter()
            vecs = await asyncio.to_thread(self._embed_local_st, cleaned)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await self._record_call(
                stage=stage,
                status="OK",
                prompt_tokens=None,
                latency_ms=latency_ms,
            )
            return vecs

        if self.cfg.provider != "openai_compatible":
            raise PreconditionError(f"不支持的 embedding provider: {self.cfg.provider!r}")

        batch_size = max(1, int(self.cfg.batch_size))
        batches = [cleaned[i : i + batch_size] for i in range(0, len(cleaned), batch_size)]
        results = await asyncio.gather(*(self._embed_openai_batch(b, stage=stage) for b in batches))
        return np.vstack(results).astype(np.float32)

    def embed_texts_sync(self, texts: list[str], *, stage: str = "EMBED") -> np.ndarray:
        """同步包装接口，仅供无活跃事件循环的同步上下文调用。"""
        return asyncio.run(self.embed_texts(texts, stage=stage))

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
