"""P2-B：LLM 网关测试（Mock 路由 + 假 httpx transport 真实模式，全程零真实网络）。

规格：docs/tasks/P2-B_gateway_vlm.md 测试要求 +
docs/designs/adaptive_practice_harness.md Risks 表网关行。
所有真实模式用例均以 monkeypatch 注入 httpx.MockTransport，绝不发真实请求。
"""

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from backend.config import settings
from backend.services import llm_gateway, llm_mocks
from backend.services.llm_gateway import LLMGatewayError, chat_json, image_path_to_data_url
from backend.services.vlm_diagnosis import DiagnosisPayload


class KernelPayload(BaseModel):
    """内核先行 JSON（设计 §3.2）。"""

    parametric_stem_tpl: str
    sympy_solver_code: str
    constraints_check: str


class BlindAnswer(BaseModel):
    """双盲求解答案。"""

    answer_latex: str
    reasoning_brief: str


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认全程 Mock，杜绝真实网络；真实模式用例显式翻转并注入假 transport。"""
    monkeypatch.setattr(settings, "USE_MOCK_LLM", True)


@pytest.fixture()
def real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "USE_MOCK_LLM", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://fake-gateway.local/v1")


@pytest.fixture()
def sleep_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """记录退避延迟且零等待。"""
    delays: list[float] = []

    async def fake_sleep(delay_seconds: float) -> None:
        delays.append(delay_seconds)

    monkeypatch.setattr(llm_gateway, "_sleep_backoff", fake_sleep)
    return delays


def install_handler(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> tuple[list[httpx.Request], list[dict[str, Any]]]:
    """将假 transport 注入网关的 httpx.AsyncClient，记录请求与客户端构造参数。"""
    requests: list[httpx.Request] = []
    client_kwargs: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response: httpx.Response = handler(request)
        return response

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client_kwargs.append(kwargs)
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_gateway.httpx, "AsyncClient", factory)
    return requests, client_kwargs


def completion(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


def sequence_handler(items: list[httpx.Response | Exception]) -> Any:
    iterator = iter(items)

    def handler(request: httpx.Request) -> httpx.Response:
        item = next(iterator)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


SIMPLE_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "求解 3x+5=20"}]
KERNEL_JSON = json.dumps(
    {
        "parametric_stem_tpl": "解方程：${a}x + {b} = {c}$。",
        "sympy_solver_code": "import sympy\n",
        "constraints_check": "a ≠ 0",
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# Mock 路由
# ---------------------------------------------------------------------------


async def test_mock_routes_diagnosis() -> None:
    result = await chat_json(
        SIMPLE_MESSAGES, DiagnosisPayload, model="any-model", purpose="diagnosis"
    )
    assert isinstance(result, DiagnosisPayload)
    assert len(result.trace.steps) == 3
    assert result.alignment.stalled_step_index == 2
    assert result.trace.trap_tags == ["TRAP_DOMAIN_BOUNDARY"]


@pytest.mark.parametrize("purpose", ["scaffold_kernel", "variant_kernel"])
async def test_mock_routes_kernel_purposes(purpose: str) -> None:
    result = await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose=purpose)
    assert isinstance(result, KernelPayload)
    assert "solve_kernel" in result.sympy_solver_code
    assert result.parametric_stem_tpl
    assert result.constraints_check


async def test_mock_routes_solver_blind() -> None:
    result = await chat_json(SIMPLE_MESSAGES, BlindAnswer, model="m", purpose="solver_blind")
    assert isinstance(result, BlindAnswer)
    assert result.answer_latex == "x = 5"
    assert result.reasoning_brief


async def test_mock_unregistered_purpose_raises() -> None:
    with pytest.raises(LLMGatewayError, match="未注册"):
        await chat_json(SIMPLE_MESSAGES, BlindAnswer, model="m", purpose="no_such_purpose")


async def test_mock_malformed_fixture_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(llm_mocks.MOCK_RESPONSES, "diagnosis", {"trace": "broken"})
    with pytest.raises(LLMGatewayError, match="校验"):
        await chat_json(SIMPLE_MESSAGES, DiagnosisPayload, model="m", purpose="diagnosis")


async def test_mock_deleted_fixture_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(llm_mocks.MOCK_RESPONSES, "solver_blind")
    with pytest.raises(LLMGatewayError, match="未注册"):
        await chat_json(SIMPLE_MESSAGES, BlindAnswer, model="m", purpose="solver_blind")


# ---------------------------------------------------------------------------
# image_path_to_data_url
# ---------------------------------------------------------------------------


def test_image_data_url_webp_roundtrip(tmp_path: Path) -> None:
    raw = b"RIFF\x00\x01fake-webp-bytes"
    path = tmp_path / "img.webp"
    path.write_bytes(raw)
    url = image_path_to_data_url(path)
    assert url.startswith("data:image/webp;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == raw


@pytest.mark.parametrize(
    ("suffix", "mime"),
    [(".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".gif", "image/gif")],
)
def test_image_data_url_mime_variants(tmp_path: Path, suffix: str, mime: str) -> None:
    path = tmp_path / f"img{suffix}"
    path.write_bytes(b"x")
    assert image_path_to_data_url(path).startswith(f"data:{mime};base64,")


def test_image_data_url_missing_file(tmp_path: Path) -> None:
    with pytest.raises(LLMGatewayError, match="无法读取图片"):
        image_path_to_data_url(tmp_path / "missing.webp")


# ---------------------------------------------------------------------------
# 真实模式（假 transport）：请求体 / 超时 / 重试 / 修复重试 / 终态失败
# ---------------------------------------------------------------------------


async def test_real_request_body_headers_and_timeout(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    requests, client_kwargs = install_handler(
        monkeypatch, lambda req: completion(KERNEL_JSON)
    )
    result = await chat_json(SIMPLE_MESSAGES, KernelPayload, model="solver-x", purpose="p")
    assert isinstance(result, KernelPayload)

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://fake-gateway.local/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "solver-x"
    assert body["messages"] == SIMPLE_MESSAGES
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == settings.LLM_TEMPERATURE  # 缺省温度
    assert client_kwargs[0]["timeout"] == httpx.Timeout(settings.LLM_TIMEOUT_SECONDS)
    assert sleep_delays == []


async def test_real_explicit_temperature_override(
    real_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests, _ = install_handler(monkeypatch, lambda req: completion(KERNEL_JSON))
    await chat_json(
        SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p", temperature=0.7
    )
    assert json.loads(requests[0].content)["temperature"] == 0.7


async def test_real_retry_on_500_then_success(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    requests, _ = install_handler(
        monkeypatch,
        sequence_handler([httpx.Response(500, text="boom"), completion(KERNEL_JSON)]),
    )
    result = await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
    assert isinstance(result, KernelPayload)
    assert len(requests) == 2
    assert sleep_delays == [1.0]


async def test_real_retry_on_429_then_success(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    requests, _ = install_handler(
        monkeypatch,
        sequence_handler([httpx.Response(429, text="slow down"), completion(KERNEL_JSON)]),
    )
    assert isinstance(
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p"), KernelPayload
    )
    assert len(requests) == 2
    assert sleep_delays == [1.0]


async def test_real_retry_on_network_error_then_success(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    requests, _ = install_handler(
        monkeypatch,
        sequence_handler([httpx.ConnectError("simulated disconnect"), completion(KERNEL_JSON)]),
    )
    assert isinstance(
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p"), KernelPayload
    )
    assert len(requests) == 2
    assert sleep_delays == [1.0]


async def test_real_timeout_retried(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    requests, _ = install_handler(
        monkeypatch,
        sequence_handler([httpx.ReadTimeout("simulated timeout"), completion(KERNEL_JSON)]),
    )
    assert isinstance(
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p"), KernelPayload
    )
    assert len(requests) == 2
    assert sleep_delays == [1.0]


async def test_real_retries_exhausted(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    requests, _ = install_handler(monkeypatch, lambda req: httpx.Response(503, text="down"))
    with pytest.raises(LLMGatewayError, match="重试"):
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
    assert len(requests) == settings.LLM_MAX_RETRIES + 1 == 3
    assert sleep_delays == [1.0, 2.0]  # 指数退避 1s/2s


async def test_real_max_retries_setting_respected(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 1)
    requests, _ = install_handler(monkeypatch, lambda req: httpx.Response(500, text="down"))
    with pytest.raises(LLMGatewayError):
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
    assert len(requests) == 2
    assert sleep_delays == [1.0]


async def test_real_4xx_no_retry(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    requests, _ = install_handler(monkeypatch, lambda req: httpx.Response(401, text="bad key"))
    with pytest.raises(LLMGatewayError, match="401"):
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
    assert len(requests) == 1
    assert sleep_delays == []


async def test_real_malformed_json_repair_retry(
    real_mode: None, monkeypatch: pytest.MonkeyPatch, sleep_delays: list[float]
) -> None:
    requests, _ = install_handler(
        monkeypatch,
        sequence_handler([completion("这不是 JSON {{{"), completion(KERNEL_JSON)]),
    )
    result = await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
    assert isinstance(result, KernelPayload)
    assert len(requests) == 2  # 修复重试恰 1 次
    repair_body = json.loads(requests[1].content)
    repair_messages = repair_body["messages"]
    assert repair_messages[: len(SIMPLE_MESSAGES)] == SIMPLE_MESSAGES
    assert repair_messages[-2] == {"role": "assistant", "content": "这不是 JSON {{{"}
    assert repair_messages[-1]["role"] == "user"
    assert "校验" in repair_messages[-1]["content"]
    assert sleep_delays == []


async def test_real_schema_mismatch_repair_retry(
    real_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_schema_json = json.dumps({"parametric_stem_tpl": "缺少其余必填字段"})
    requests, _ = install_handler(
        monkeypatch,
        sequence_handler([completion(bad_schema_json), completion(KERNEL_JSON)]),
    )
    result = await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
    assert isinstance(result, KernelPayload)
    assert len(requests) == 2
    repair_prompt = json.loads(requests[1].content)["messages"][-1]["content"]
    assert "sympy_solver_code" in repair_prompt  # Pydantic 校验错误信息已附上


async def test_real_repair_failure_raises(
    real_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests, _ = install_handler(
        monkeypatch,
        sequence_handler([completion("bad-1"), completion("bad-2")]),
    )
    with pytest.raises(LLMGatewayError, match="修复重试"):
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
    assert len(requests) == 2  # 不会无限修复


async def test_real_bad_envelope_raises(
    real_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests, _ = install_handler(
        monkeypatch, lambda req: httpx.Response(200, json={"no_choices": True})
    )
    with pytest.raises(LLMGatewayError, match="响应结构非法"):
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
    assert len(requests) == 1


async def test_real_non_string_content_raises(
    real_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_handler(
        monkeypatch,
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": 42}}]}),
    )
    with pytest.raises(LLMGatewayError, match="非字符串"):
        await chat_json(SIMPLE_MESSAGES, KernelPayload, model="m", purpose="p")
