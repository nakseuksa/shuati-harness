"""OpenAI 兼容多槽位异步结构化网关：Semaphore + RPM 令牌桶 + 退避重试 + JSON 附错自愈重试 + llm_calls 账本。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ValidationError

from shuati.config import LLMSlotConfig, Settings
from shuati.db.connection import SingleWriterExecutor
from shuati.llm.mocks import resolve_mock

T = TypeVar("T", bound=BaseModel)

SlotName = Literal["vlm_primary", "vlm_secondary", "text_llm", "judge_llm"]

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?([\s\S]*?)\n?\s*```\s*$", re.IGNORECASE)


class LLMGatewayError(RuntimeError):
    """网关统一异常：网络/HTTP/解析/校验/Mock 路由失败全部收敛于此。"""

    def __init__(self, message: str, *, error_kind: str = "GATEWAY_ERROR") -> None:
        super().__init__(message)
        self.error_kind = error_kind


@dataclass
class LLMCallResult(Generic[T]):
    parsed: T
    model_name: str
    raw_response: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    call_id: int | None = None
    status: Literal["OK", "SCHEMA_RETRY_OK"] = "OK"


class _TokenBucket:
    """异步 RPM 令牌桶限流器。"""

    def __init__(self, rpm: int) -> None:
        self._rpm = int(rpm)
        self._capacity = float(max(1, self._rpm))
        self._tokens = self._capacity
        self._rate_per_sec = self._rpm / 60.0 if self._rpm > 0 else 0.0
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, sleep_fn: Any) -> None:
        if self._rpm <= 0:
            return
        while True:
            wait_seconds = 0.0
            async with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._last_refill)
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_sec)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_seconds = (1.0 - self._tokens) / self._rate_per_sec
            await sleep_fn(wait_seconds)


def _strip_json_fences(raw: str) -> str:
    m = _CODE_FENCE_RE.match(raw)
    return m.group(1).strip() if m else raw.strip()


class LLMGateway:
    """多槽位异步结构化 LLM/VLM 网关。"""

    def __init__(
        self,
        settings: Settings,
        *,
        conn: sqlite3.Connection | None = None,
        writer: SingleWriterExecutor | None = None,
        llm_raw_dir: Path = Path("data/llm_raw"),
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.writer = writer
        self.llm_raw_dir = Path(llm_raw_dir)

        self._loop_state: dict[asyncio.AbstractEventLoop, dict[str, Any]] = {}
        self._clients: dict[tuple[asyncio.AbstractEventLoop, float], httpx.AsyncClient] = {}
        self._fallback_db_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    def _get_slot_config(self, slot: SlotName) -> LLMSlotConfig:
        cfg = getattr(self.settings.llm, slot)
        if slot == "judge_llm" and not cfg.model:
            fallback = self.settings.llm.text_llm.model_copy()
            fallback.temperature = 0.0
            return fallback
        return cfg

    def _get_slot_limiters(self, slot: SlotName) -> tuple[asyncio.Semaphore, _TokenBucket]:
        loop = asyncio.get_running_loop()
        state = self._loop_state.setdefault(loop, {})
        if slot not in state:
            cfg = self._get_slot_config(slot)
            sem = asyncio.Semaphore(max(1, cfg.max_concurrency))
            bucket = _TokenBucket(cfg.rpm_limit)
            state[slot] = (sem, bucket)
        return state[slot]

    async def _get_client(self, timeout_seconds: float) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        key = (loop, float(timeout_seconds))
        factory = httpx.AsyncClient
        cached = self._clients.get(key)
        if cached is not None and not getattr(cached, "is_closed", False):
            if type(cached) is factory:
                return cached
            await cached.aclose()
            self._clients.pop(key, None)
        client = factory(timeout=httpx.Timeout(float(timeout_seconds)))
        self._clients[key] = client
        return client

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def _current_yyyymm(self) -> str:
        try:
            tz = ZoneInfo(self.settings.timezone)
            return datetime.now(tz).strftime("%Y%m")
        except Exception:
            return datetime.now(UTC).strftime("%Y%m")

    def _write_raw_dump(self, call_id: int, payload: dict[str, Any]) -> str:
        yyyymm = self._current_yyyymm()
        target_dir = self.llm_raw_dir / yyyymm
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{call_id}.json"
        tmp_path = target_dir / f".{call_id}.json.tmp"
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, target_path)
        return target_path.as_posix()

    async def _record_llm_call(
        self,
        *,
        stage: str,
        slot: str,
        model_name: str,
        question_uid: str | None,
        prompt_version: str,
        status: Literal["OK", "FAILED", "SCHEMA_RETRY_OK", "SCHEMA_RETRY_FAILED"],
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: int,
        raw_response: str,
        error_message: str | None = None,
    ) -> int | None:
        if self.conn is None:
            return None

        def _insert_and_dump(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                """
                INSERT INTO llm_calls(
                    stage, slot, model_name, question_uid, prompt_version,
                    status, prompt_tokens, completion_tokens, latency_ms, est_cost, raw_response_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '');
                """,
                (
                    stage,
                    slot,
                    model_name,
                    question_uid,
                    prompt_version,
                    status,
                    prompt_tokens,
                    completion_tokens,
                    latency_ms,
                ),
            )
            call_id = int(cur.lastrowid)  # type: ignore[arg-type]
            raw_path = self._write_raw_dump(
                call_id,
                {
                    "call_id": call_id,
                    "stage": stage,
                    "slot": slot,
                    "model_name": model_name,
                    "question_uid": question_uid,
                    "prompt_version": prompt_version,
                    "status": status,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "latency_ms": latency_ms,
                    "raw_response": raw_response,
                    "error": error_message,
                },
            )
            conn.execute(
                "UPDATE llm_calls SET raw_response_path = ? WHERE call_id = ?",
                (raw_path, call_id),
            )
            return call_id

        if self.writer is not None:
            async with self.writer.async_transaction(self.conn) as tx_conn:
                return _insert_and_dump(tx_conn)

        loop = asyncio.get_running_loop()
        db_lock = self._fallback_db_locks.setdefault(loop, asyncio.Lock())
        async with db_lock:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE;")
                try:
                    cid = _insert_and_dump(self.conn)
                    self.conn.commit()
                    return cid
                except BaseException:
                    self.conn.rollback()
                    raise
            return _insert_and_dump(self.conn)

    async def _post_with_retries(
        self,
        *,
        slot: SlotName,
        slot_cfg: LLMSlotConfig,
        stage: str,
        body: dict[str, Any],
    ) -> tuple[str, int | None, int | None, str]:
        """在 Semaphore + RPM 令牌桶保护下发送请求，支持 429/5xx/网络异常指数退避重试。"""
        url = f"{slot_cfg.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {slot_cfg.api_key}",
            "Content-Type": "application/json",
        }
        sem, bucket = self._get_slot_limiters(slot)
        client = await self._get_client(slot_cfg.timeout_seconds)

        max_retries = max(0, int(slot_cfg.max_retries))
        last_error = "unknown"
        last_kind = "GATEWAY_ERROR"

        for attempt in range(max_retries + 1):
            await bucket.acquire(self._sleep)
            async with sem:
                try:
                    response = await client.post(url, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"网络错误: {exc!r}"
                    last_kind = "NETWORK_ERROR"
                else:
                    if response.status_code == 429 or response.status_code >= 500:
                        last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                        last_kind = "HTTP_429" if response.status_code == 429 else "HTTP_5XX"
                    elif response.status_code >= 400:
                        raise LLMGatewayError(
                            f"stage={stage!r} slot={slot!r} 返回不可重试错误 HTTP {response.status_code}: {response.text[:200]}",
                            error_kind="HTTP_4XX",
                        )
                    else:
                        return self._extract_response_fields(response, stage=stage, slot=slot)

            if attempt < max_retries:
                await self._sleep(float(2**attempt))

        raise LLMGatewayError(
            f"stage={stage!r} slot={slot!r} 重试 {max_retries} 次后仍失败: {last_error}",
            error_kind=last_kind,
        )

    def _extract_response_fields(
        self, response: httpx.Response, *, stage: str, slot: str
    ) -> tuple[str, int | None, int | None, str]:
        raw_text = response.text
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMGatewayError(
                f"stage={stage!r} slot={slot!r} 响应信封结构非法: {exc}",
                error_kind="BAD_ENVELOPE",
            ) from exc
        if not isinstance(content, str):
            raise LLMGatewayError(
                f"stage={stage!r} slot={slot!r} 响应 content 非字符串: {content!r}",
                error_kind="BAD_ENVELOPE",
            )
        usage = data.get("usage") if isinstance(data, dict) else None
        p_tok: int | None = None
        c_tok: int | None = None
        if isinstance(usage, dict):
            if isinstance(usage.get("prompt_tokens"), int):
                p_tok = usage["prompt_tokens"]
            if isinstance(usage.get("completion_tokens"), int):
                c_tok = usage["completion_tokens"]
        return content, p_tok, c_tok, raw_text

    def _build_repair_messages(
        self,
        messages: list[dict[str, Any]],
        raw_content: str,
        exc: Exception,
        schema: type[BaseModel],
    ) -> list[dict[str, Any]]:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        return [
            *messages,
            {"role": "assistant", "content": raw_content},
            {
                "role": "user",
                "content": (
                    "你上一次的输出未通过 JSON 解析或 Pydantic Schema 校验。具体错误信息如下：\n"
                    f"{exc}\n"
                    "目标完整 JSON Schema 定义如下：\n"
                    f"{schema_json}\n"
                    "请严格按上述 JSON Schema 修复并仅输出合法的完整 JSON 对象，不要包含任何解释或 Markdown 代码块。"
                ),
            },
        ]

    async def call_structured(
        self,
        *,
        slot: SlotName,
        stage: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        question_uid: str | None = None,
        prompt_version: str = "",
        mock_key: str | None = None,
    ) -> LLMCallResult[T]:
        """多槽位异步结构化调用。"""
        slot_cfg = self._get_slot_config(slot)
        model_name = slot_cfg.model or f"mock-{slot}"
        start = time.perf_counter()

        if self.settings.pipeline.use_mock_llm:
            try:
                parsed_model, raw_resp = resolve_mock(
                    schema,
                    slot=slot,
                    stage=stage,
                    question_uid=question_uid,
                    mock_key=mock_key,
                )
            except LLMGatewayError as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                await self._record_llm_call(
                    stage=stage,
                    slot=slot,
                    model_name=model_name,
                    question_uid=question_uid,
                    prompt_version=prompt_version,
                    status="FAILED",
                    prompt_tokens=None,
                    completion_tokens=None,
                    latency_ms=latency_ms,
                    raw_response="",
                    error_message=str(exc),
                )
                raise
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                await self._record_llm_call(
                    stage=stage,
                    slot=slot,
                    model_name=model_name,
                    question_uid=question_uid,
                    prompt_version=prompt_version,
                    status="FAILED",
                    prompt_tokens=None,
                    completion_tokens=None,
                    latency_ms=latency_ms,
                    raw_response="",
                    error_message=str(exc),
                )
                raise LLMGatewayError(f"Mock 执行异常: {exc}", error_kind="MOCK_ERROR") from exc

            latency_ms = int((time.perf_counter() - start) * 1000)
            call_id = await self._record_llm_call(
                stage=stage,
                slot=slot,
                model_name=model_name,
                question_uid=question_uid,
                prompt_version=prompt_version,
                status="OK",
                prompt_tokens=None,
                completion_tokens=None,
                latency_ms=latency_ms,
                raw_response=raw_resp,
            )
            return LLMCallResult(
                parsed=parsed_model,  # type: ignore[arg-type]
                model_name=model_name,
                raw_response=raw_resp,
                prompt_tokens=None,
                completion_tokens=None,
                latency_ms=latency_ms,
                call_id=call_id,
                status="OK",
            )

        body: dict[str, Any] = {
            "model": slot_cfg.model,
            "messages": messages,
            "temperature": slot_cfg.temperature,
            "response_format": {"type": "json_object"},
        }
        raw_content = ""
        total_p_tok: int | None = None
        total_c_tok: int | None = None

        try:
            content, p_tok, c_tok, _ = await self._post_with_retries(
                slot=slot, slot_cfg=slot_cfg, stage=stage, body=body
            )
            raw_content = content
            total_p_tok = p_tok
            total_c_tok = c_tok

            try:
                parsed_obj = json.loads(_strip_json_fences(content))
                parsed_model = schema.model_validate(parsed_obj)
                call_status: Literal["OK", "SCHEMA_RETRY_OK"] = "OK"
            except (ValueError, ValidationError) as first_exc:
                repair_body = {
                    **body,
                    "messages": self._build_repair_messages(messages, content, first_exc, schema),
                }
                repaired_content, rp_tok, rc_tok, _ = await self._post_with_retries(
                    slot=slot, slot_cfg=slot_cfg, stage=stage, body=repair_body
                )
                raw_content = repaired_content
                if total_p_tok is not None or rp_tok is not None:
                    total_p_tok = (total_p_tok or 0) + (rp_tok or 0)
                if total_c_tok is not None or rc_tok is not None:
                    total_c_tok = (total_c_tok or 0) + (rc_tok or 0)

                try:
                    repaired_obj = json.loads(_strip_json_fences(repaired_content))
                    parsed_model = schema.model_validate(repaired_obj)
                    call_status = "SCHEMA_RETRY_OK"
                except (ValueError, ValidationError) as second_exc:
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    await self._record_llm_call(
                        stage=stage,
                        slot=slot,
                        model_name=model_name,
                        question_uid=question_uid,
                        prompt_version=prompt_version,
                        status="SCHEMA_RETRY_FAILED",
                        prompt_tokens=total_p_tok,
                        completion_tokens=total_c_tok,
                        latency_ms=latency_ms,
                        raw_response=raw_content,
                        error_message=str(second_exc),
                    )
                    raise LLMGatewayError(
                        f"stage={stage!r} slot={slot!r} 自愈重试后仍未通过校验: {second_exc}",
                        error_kind="SCHEMA_RETRY_FAILED",
                    ) from second_exc

            latency_ms = int((time.perf_counter() - start) * 1000)
            call_id = await self._record_llm_call(
                stage=stage,
                slot=slot,
                model_name=model_name,
                question_uid=question_uid,
                prompt_version=prompt_version,
                status=call_status,
                prompt_tokens=total_p_tok,
                completion_tokens=total_c_tok,
                latency_ms=latency_ms,
                raw_response=raw_content,
            )
            return LLMCallResult(
                parsed=parsed_model,  # type: ignore[arg-type]
                model_name=model_name,
                raw_response=raw_content,
                prompt_tokens=total_p_tok,
                completion_tokens=total_c_tok,
                latency_ms=latency_ms,
                call_id=call_id,
                status=call_status,
            )

        except LLMGatewayError as exc:
            if exc.error_kind != "SCHEMA_RETRY_FAILED":
                latency_ms = int((time.perf_counter() - start) * 1000)
                await self._record_llm_call(
                    stage=stage,
                    slot=slot,
                    model_name=model_name,
                    question_uid=question_uid,
                    prompt_version=prompt_version,
                    status="FAILED",
                    prompt_tokens=total_p_tok,
                    completion_tokens=total_c_tok,
                    latency_ms=latency_ms,
                    raw_response=raw_content,
                    error_message=str(exc),
                )
            raise
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            await self._record_llm_call(
                stage=stage,
                slot=slot,
                model_name=model_name,
                question_uid=question_uid,
                prompt_version=prompt_version,
                status="FAILED",
                prompt_tokens=total_p_tok,
                completion_tokens=total_c_tok,
                latency_ms=latency_ms,
                raw_response=raw_content,
                error_message=str(exc),
            )
            raise LLMGatewayError(
                f"stage={stage!r} slot={slot!r} 网关调用失败: {exc}",
                error_kind="GATEWAY_ERROR",
            ) from exc

    async def aclose(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            if not getattr(client, "is_closed", False):
                await client.aclose()
