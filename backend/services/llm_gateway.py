"""LLM/VLM 统一异步网关（Mock 优先，全项目离线开发基座）。

唯一权威规格：docs/designs/adaptive_practice_harness.md
Risks 表「外部 LLM/VLM 网关超时或故障」行 + NFR Token 经济学段。
API 签名与 Mock 路由机制为 PM 锁定接口（docs/tasks/P2-B_gateway_vlm.md），
vlm_diagnosis / scaffold_engine / 双盲校验均复用本网关，禁止私自变更。

- Mock 模式（``settings.USE_MOCK_LLM``）：按 purpose 路由 ``llm_mocks.MOCK_RESPONSES``，
  经 schema 校验后返回；未注册 purpose 显式抛 ``LLMGatewayError``（绝不静默）。
- 真实模式：``httpx.AsyncClient`` POST ``{LLM_BASE_URL}/chat/completions``（Bearer 鉴权，
  ``response_format={"type": "json_object"}``，Timeout 取 ``settings.LLM_TIMEOUT_SECONDS``，
  默认 300s 适配推理模型思考窗口）；网络错误/5xx/429 指数退避
  （1s/2s）重试 ``settings.LLM_MAX_RETRIES`` 次；JSON 解析或 Pydantic 校验失败时附校验
  错误信息修复重试 1 次；所有异常最终收敛为 ``LLMGatewayError``。
"""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from backend.config import settings
from backend.services import llm_mocks

__all__ = ["LLMGatewayError", "chat_json", "image_path_to_data_url"]

# 图片后缀 → MIME（CAS 入库统一 WebP；兼容常见原始格式）
_MIME_BY_SUFFIX: dict[str, str] = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


class LLMGatewayError(RuntimeError):
    """网关统一异常：网络/HTTP/解析/校验/Mock 路由失败全部收敛于此。"""


def image_path_to_data_url(path: Path) -> str:
    """读取本地图片 → base64 data URL（供 VLM 消息构造，仅诊断首轮携带）。"""
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LLMGatewayError(f"无法读取图片 {path}: {exc}") from exc
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


async def chat_json(
    messages: list[dict[str, Any]],
    schema: type[BaseModel],
    *,
    model: str,
    purpose: str,
    temperature: float | None = None,
) -> BaseModel:
    """结构化 JSON 对话调用（PM 锁定签名）。

    Args:
        messages: OpenAI 格式消息；图片用
            ``{"type": "image_url", "image_url": {"url": "data:image/webp;base64,..."}}``。
        schema: 期望的结构化输出 Pydantic 模型。
        model: 调用方显式传 ``settings.VLM_MODEL_NAME`` / ``SOLVER_LLM_MODEL_NAME``。
        purpose: Mock 路由键（'diagnosis' | 'scaffold_kernel' | 'variant_kernel' |
            'solver_blind' | ...）。
        temperature: 缺省取 ``settings.LLM_TEMPERATURE``。

    Raises:
        LLMGatewayError: Mock purpose 未注册 / fixture 校验失败 / 网络与 HTTP 失败 /
            修复重试后仍无法解析或校验。
    """
    if settings.USE_MOCK_LLM:
        return _resolve_mock(schema, purpose)

    resolved_temperature = settings.LLM_TEMPERATURE if temperature is None else temperature
    request_body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": resolved_temperature,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.LLM_TIMEOUT_SECONDS)) as client:
            content = await _post_with_retries(client, request_body, purpose)
            try:
                return schema.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError) as first_exc:
                # 附校验错误信息修复重试 1 次（契约实现规格）
                repair_body = {
                    **request_body,
                    "messages": _build_repair_messages(messages, content, first_exc),
                }
                repaired = await _post_with_retries(client, repair_body, purpose)
                try:
                    return schema.model_validate(json.loads(repaired))
                except (json.JSONDecodeError, ValidationError) as second_exc:
                    raise LLMGatewayError(
                        f"purpose={purpose!r} 修复重试后仍未通过校验: {second_exc}"
                    ) from second_exc
    except LLMGatewayError:
        raise
    except Exception as exc:  # 所有异常收敛为 LLMGatewayError
        raise LLMGatewayError(f"purpose={purpose!r} 网关调用失败: {exc}") from exc


def _resolve_mock(schema: type[BaseModel], purpose: str) -> BaseModel:
    """Mock 路由：调用期读取 llm_mocks.MOCK_RESPONSES，支持测试 monkeypatch。"""
    fixtures = llm_mocks.MOCK_RESPONSES
    if purpose not in fixtures:
        raise LLMGatewayError(
            f"Mock purpose 未注册: {purpose!r}（已注册: {sorted(fixtures)}）"
        )
    try:
        return schema.model_validate(fixtures[purpose])
    except ValidationError as exc:
        raise LLMGatewayError(
            f"Mock fixture purpose={purpose!r} 未通过 {schema.__name__} 校验: {exc}"
        ) from exc


async def _sleep_backoff(delay_seconds: float) -> None:
    """退避睡眠独立封装，便于测试注入零等待记录器（不发真实等待）。"""
    await asyncio.sleep(delay_seconds)


async def _post_with_retries(
    client: httpx.AsyncClient, body: dict[str, Any], purpose: str
) -> str:
    """POST + 网络错误/5xx/429 指数退避重试（1s/2s），返回 message.content 字符串。"""
    url = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}"}
    max_retries = settings.LLM_MAX_RETRIES
    last_error = "unknown"
    for attempt in range(max_retries + 1):
        try:
            response = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"网络错误: {exc!r}"
        else:
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
            elif response.status_code >= 400:
                raise LLMGatewayError(
                    f"purpose={purpose!r} 网关返回不可重试错误 "
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )
            else:
                return _extract_content(response, purpose)
        if attempt < max_retries:
            await _sleep_backoff(float(2**attempt))  # 1s, 2s, ...
    raise LLMGatewayError(
        f"purpose={purpose!r} 重试 {max_retries} 次后仍失败: {last_error}"
    )


def _extract_content(response: httpx.Response, purpose: str) -> str:
    """从 OpenAI 兼容响应信封提取 content 字符串；信封非法直接终态失败。"""
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMGatewayError(f"purpose={purpose!r} 响应结构非法: {exc}") from exc
    if not isinstance(content, str):
        raise LLMGatewayError(f"purpose={purpose!r} 响应 content 非字符串: {content!r}")
    return content


def _build_repair_messages(
    messages: list[dict[str, Any]], raw_content: str, exc: Exception
) -> list[dict[str, Any]]:
    """构造修复重试消息：原对话 + 上次原始输出 + 校验错误信息。"""
    return [
        *messages,
        {"role": "assistant", "content": raw_content},
        {
            "role": "user",
            "content": (
                "你上一次的输出未通过结构化校验。错误信息：\n"
                f"{exc}\n"
                "请严格按原 JSON Schema 重新输出修复后的完整 JSON 对象，"
                "不要输出任何解释或 Markdown 代码块。"
            ),
        },
    ]
