"""P5：运行时结构化行为日志测试（docs/tasks/P5-observability.md 测试要求）。

全程 USE_MOCK_LLM=True + 临时 SQLite/LOG_DIR，零真实网络。caplog 为主：
- JSON 格式化器可解析（event/ts/level）；setup_logging 幂等；
- FSM / Elo 幂等留痕 / Gateway Mock + 密钥防泄漏 / API api.request + X-Request-ID /
  降阶 descent / 沙盒 sandbox.exec / 不可序列化字段防御；
- 事件目录全模块覆盖（ingest / prewarm / escrow / drill / graph）。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import BaseModel

from backend import main as main_module
from backend.config import settings
from backend.db.schema import get_db_connection
from backend.main import create_app
from backend.observability import (
    CONTENT_MAX_CHARS,
    ConsoleFormatter,
    JsonFormatter,
    log_event,
    request_id_var,
    setup_logging,
)
from backend.sandbox.sympy_verifier import (
    run_sympy_in_sandbox,
    verify_equation_equivalence,
)
from backend.services import (
    elo_engine,
    ingest_service,
    knowledge_graph,
    llm_gateway,
    llm_mocks,
    scaffold_engine,
)
from backend.services.blind_drill_scheduler import (
    evaluate_blind_drill_submission,
    get_daily_blind_drill_queue,
)
from backend.services.ingest_service import ingest_question
from backend.services.knowledge_graph import NodeJudgeResult
from backend.services.llm_gateway import LLMGatewayError, chat_json
from backend.services.scaffold_engine import (
    confirm_diagnostic_gate,
    in_flight_prewarm_tasks,
    submit_stage_answer,
)
from backend.services.vlm_diagnosis import DiagnosisPayload

logger = logging.getLogger("tests.observability")

GRADE = "高中物理-高二"
STEM_MD = (
    "汽车以速度 $v$ 通过半径 $R = 40\\,\\text{m}$ 的拱形桥最高点，"
    "取 $g = 10\\,\\text{m/s}^2$。求汽车不脱离桥面的最大速度 $v_{max}$。"
)
STAGE1_WRONG = r"v_{max} = \sqrt{2gR}"
DAY = date(2026, 11, 20)
PAST_UNLOCK = "2026-11-10 00:00:00"

_VALID_KERNEL = (
    "import sympy\n\n"
    "def solve_kernel():\n"
    "    x = sympy.symbols('x')\n"
    "    sol = sympy.solve(sympy.Eq(2 * x, 10), x)\n"
    "    return {'params': {'a': 2}, 'answer_latex': 'x = ' + str(sol[0])}\n"
)


# ---------------------------------------------------------------------------
# 夹具：环境隔离 / Mock 网关 / 预热 Task 清理 / 日志 handler 清理
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "USE_MOCK_LLM", True)
    monkeypatch.setattr(settings, "SQLITE_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(settings, "CAS_ASSETS_DIR", tmp_path / "assets")
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(settings, "LOG_CONSOLE", False)


@pytest.fixture(autouse=True)
def _caplog_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)


@pytest.fixture(autouse=True)
async def _isolate_prewarm_tasks() -> Any:
    in_flight_prewarm_tasks.clear()
    yield
    for task in list(in_flight_prewarm_tasks.values()):
        task.cancel()
    in_flight_prewarm_tasks.clear()


@pytest.fixture(autouse=True)
def _cleanup_logging() -> Any:
    """测试后摘除本观测层挂载的 handler（关闭临时日志文件句柄）。"""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_harness_handler", False):
            root.removeHandler(handler)
            handler.close()


@pytest.fixture()
def install_diagnosis(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(payload: DiagnosisPayload) -> None:
        async def fake(
            stem_md: str,
            stem_images: list[Path],
            draft_images: list[Path],
            target_grade: str,
            existing_l1_l2_tree: dict[str, list[str]],
        ) -> DiagnosisPayload:
            return payload

        monkeypatch.setattr(ingest_service, "analyze_question_and_draft", fake)

    return _install


def _payload(category: str = "CONCEPT_BOTTLENECK") -> DiagnosisPayload:
    payload = DiagnosisPayload.model_validate(copy.deepcopy(llm_mocks.MOCK_RESPONSES["diagnosis"]))
    payload.alignment.error_category = category  # type: ignore[assignment]
    payload.alignment.last_valid_step_index = 1
    payload.alignment.stalled_step_index = 2
    payload.alignment.trap_taxonomy_tag = (
        "TRAP_DOMAIN_BOUNDARY" if category == "TRAP_OR_CALC_ERROR" else None
    )
    return payload


async def _ingest(
    db_path: Path,
    install_diagnosis: Any,
    stem_md: str = STEM_MD,
    initial_state: str = "错题",
) -> Any:
    install_diagnosis(_payload())
    return await ingest_question(
        stem_md, [], [], GRADE, initial_state=initial_state, db_path=db_path  # type: ignore[arg-type]
    )


async def _drain(session_id: str) -> None:
    task = in_flight_prewarm_tasks.get(session_id)
    if task is not None:
        await task
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 断言工具
# ---------------------------------------------------------------------------


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def _f(record: logging.LogRecord) -> dict[str, Any]:
    return getattr(record, "fields", {})


def _one(caplog: pytest.LogCaptureFixture, name: str) -> logging.LogRecord:
    records = _events(caplog, name)
    assert records, f"缺少事件: {name}"
    return records[0]


def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    fmt = JsonFormatter()
    return "\n".join(fmt.format(r) for r in caplog.records)


def _mk_question(
    db_path: Path,
    uid: str,
    *,
    module: str = "力学",
    base_elo: int = 1500,
    answer: str = "x = 5",
    mastery: str = "ACTIVE",
    node_id: str | None = None,
    unlock_at: str | None = None,
) -> str:
    """直插单步题 + 节点 + 会话（观测测试夹具，绕过 VLM，行为与落库契约一致）。"""
    with get_db_connection(db_path) as conn:
        if node_id is None:
            node_id = f"node-{uid}"
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_nodes"
            " (node_id, l1_module, l2_entity, l3_operator, aliases_json)"
            " VALUES (?, ?, '实体', '算子', '[]')",
            (node_id, module),
        )
        conn.execute(
            "INSERT INTO questions (question_uid, origin_type, target_grade_and_subject,"
            " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
            " base_elo, tier, trap_tags_json, primary_l1_module, mastery_state,"
            " blind_test_unlock_at, blind_test_due_at, verification_status)"
            " VALUES (?, 'USER_CANONICAL', '高中物理', ?, ?, ?, 2, 2, 2, 1, ?, 'L3_综合中档',"
            " '[]', ?, ?, ?, ?, 'HUMAN_OR_CANONICAL')",
            (
                uid,
                f"题干 {uid}",
                hashlib.sha256(uid.encode()).hexdigest(),
                answer,
                base_elo,
                module,
                mastery,
                unlock_at,
                unlock_at,
            ),
        )
        conn.execute(
            "INSERT INTO question_steps (step_id, question_uid, solution_branch_id, step_index,"
            " step_goal_desc, step_formula_latex, knowledge_node_id, masked_conditions_json,"
            " probe_question_md, is_blocked_step, reused_existing_node,"
            " c_sub, s_sub, t_sub, l_sub, sub_elo)"
            " VALUES (?, ?, 'canonical', 1, '子目标', ?, ?, '[]', 'probe?', 1, 0, 2, 2, 2, 1, ?)",
            (f"step-{uid}", uid, answer, node_id, min(base_elo, 2650)),
        )
        conn.execute(
            "INSERT INTO diagnostic_sessions (session_id, question_uid,"
            " draft_image_cas_uris_json, draft_steps_latex_json, matched_branch_id,"
            " last_valid_step_index, stalled_step_index, error_category,"
            " user_confirmed_gate, micro_stage)"
            " VALUES (?, ?, '[]', '[]', 'canonical', 0, 1, 'CONCEPT_BOTTLENECK', 1,"
            " 'STAGE_1_BLIND_PROBE')",
            (f"sess-{uid}", uid),
        )
    return uid


def _mk_relation(
    db_path: Path,
    source: str,
    target: str,
    relation_type: str,
    *,
    is_directed: int = 0,
    containment: float = 0.9,
) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO question_relations (source_question_uid, target_question_uid,"
            " relation_type, is_directed, target_step_index, surface_sim, skeleton_sim,"
            " containment_score, trap_overlap, confidence_source)"
            " VALUES (?, ?, ?, ?, 1, 0.5, 0.85, ?, 0.0, 'HUMAN_VERIFIED')",
            (source, target, relation_type, is_directed, containment),
        )


# ---------------------------------------------------------------------------
# 1. 格式化器 / setup_logging 幂等 / JSONL 落盘
# ---------------------------------------------------------------------------


def test_json_formatter_parseable_with_event_ts_level() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    record.event = "test.evt"  # type: ignore[attr-defined]
    record.fields = {"a": 1, "session_id": "s1"}  # type: ignore[attr-defined]
    record.request_id = "r1"  # type: ignore[attr-defined]
    data = json.loads(JsonFormatter().format(record))
    assert data["event"] == "test.evt"
    assert data["level"] == "INFO"
    assert data["logger"] == "t"
    assert data["request_id"] == "r1"
    assert data["a"] == 1 and data["session_id"] == "s1"
    assert "ts" in data


def test_json_formatter_non_event_record_message() -> None:
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    data = json.loads(JsonFormatter().format(record))
    assert data["message"] == "hello world"
    assert "event" not in data


def test_json_formatter_unserializable_fields_fallback() -> None:
    """绕过 log_event 净化直接构造脏 record：格式化器仍产出可解析 JSON。"""
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    record.event = "test.dirty"  # type: ignore[attr-defined]
    record.fields = {"obj": object(), "bad": {("tuple", "key"): 1}}  # type: ignore[attr-defined]
    data = json.loads(JsonFormatter().format(record))
    assert data["event"] == "test.dirty"


def test_json_formatter_unprintable_message_fallback() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "%d", ("not-int",), None)
    data = json.loads(JsonFormatter().format(record))
    assert data["message"] == "<unprintable message>"


def test_console_formatter_human_readable(caplog: pytest.LogCaptureFixture) -> None:
    log_event(logger, "test.console", session_id="s1", ok=True)
    line = ConsoleFormatter().format(_one(caplog, "test.console"))
    assert "test.console" in line
    assert "session_id=s1" in line
    assert "INFO" in line


def test_setup_logging_idempotent_and_jsonl_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(settings, "LOG_CONSOLE", True)
    setup_logging()
    setup_logging()  # 重复调用不重复挂 handler
    marked = [
        h for h in logging.getLogger().handlers if getattr(h, "_harness_handler", False)
    ]
    assert len(marked) == 2  # console + file
    log_event(logger, "test.file", k="v")
    for handler in marked:
        handler.flush()
    lines = (tmp_path / "logs" / "harness.jsonl").read_text(encoding="utf-8").strip().splitlines()
    data = json.loads(lines[-1])
    assert data["event"] == "test.file" and data["k"] == "v"
    assert "ts" in data and "level" in data and "logger" in data


def test_setup_logging_console_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(settings, "LOG_CONSOLE", False)
    setup_logging()
    marked = [
        h for h in logging.getLogger().handlers if getattr(h, "_harness_handler", False)
    ]
    assert len(marked) == 1  # 仅 file


def test_setup_logging_bad_dir_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "LOG_DIR", blocker / "sub")  # mkdir 必失败
    monkeypatch.setattr(settings, "LOG_CONSOLE", True)
    setup_logging()  # 不得抛异常
    log_event(logger, "test.nofile", ok=True)


# ---------------------------------------------------------------------------
# 2. log_event 防御 / 截断 / 脱敏 / request_id
# ---------------------------------------------------------------------------


def test_log_event_unserializable_field_no_raise(caplog: pytest.LogCaptureFixture) -> None:
    class Nasty:
        def __repr__(self) -> str:
            raise RuntimeError("repr 亦失败")

    log_event(logger, "test.nasty", obj=Nasty(), ok=True)  # 不得抛异常
    fields = _f(_one(caplog, "test.nasty"))
    assert fields["obj"] == "<unserializable>"
    assert fields["ok"] is True


def test_log_event_truncates_content(caplog: pytest.LogCaptureFixture) -> None:
    log_event(logger, "test.trunc", stem="字" * 300, nested={"deep": "x" * 300})
    fields = _f(_one(caplog, "test.trunc"))
    assert len(fields["stem"]) <= CONTENT_MAX_CHARS == 120
    assert len(fields["nested"]["deep"]) <= 120


def test_log_event_redacts_secrets(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-secret-abc123456"
    monkeypatch.setattr(settings, "LLM_API_KEY", secret)
    log_event(
        logger,
        "test.secret",
        api_key=secret,
        authorization=f"Bearer {secret}",
        note=f"调用失败 key={secret}",
    )
    fields = _f(_one(caplog, "test.secret"))
    assert fields["api_key"] == "***REDACTED***"
    assert fields["authorization"] == "***REDACTED***"
    assert secret not in fields["note"]
    assert secret not in _all_log_text(caplog)


def test_request_id_contextvar_attached(caplog: pytest.LogCaptureFixture) -> None:
    token = request_id_var.set("rid-42")
    try:
        log_event(logger, "test.rid", x=1)
    finally:
        request_id_var.reset(token)
    record = _one(caplog, "test.rid")
    assert record.request_id == "rid-42"  # type: ignore[attr-defined]
    assert json.loads(JsonFormatter().format(record))["request_id"] == "rid-42"
    # 无请求上下文 → 不附带 request_id
    log_event(logger, "test.norid", x=1)
    assert "request_id" not in json.loads(JsonFormatter().format(_one(caplog, "test.norid")))


def test_log_event_degrades_when_handler_fails() -> None:
    """带 extra 的写入失败 → 降级为无字段事件名日志，绝不击穿业务。"""

    class FlakyLogger:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
            self.calls.append(kwargs)
            if "extra" in kwargs:
                raise RuntimeError("handler 故障")

    flaky = FlakyLogger()
    log_event(flaky, "test.degraded", x=1)  # type: ignore[arg-type]
    assert len(flaky.calls) == 2
    assert "extra" not in flaky.calls[1]  # 降级无字段

    class DeadLogger:
        def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("彻底故障")

    log_event(DeadLogger(), "test.dead", x=1)  # type: ignore[arg-type]  # 不得抛异常


def test_json_formatter_exc_info() -> None:
    import sys

    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "msg", None, None)
    try:
        raise ValueError("boom")
    except ValueError:
        record.exc_info = sys.exc_info()
    data = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in data["exc"]


def test_json_formatter_total_serialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两次 dumps 均失败 → 硬编码最小 JSON，绝不抛异常。"""
    import backend.observability as obs

    def boom(*args: Any, **kwargs: Any) -> str:
        raise ValueError("dumps 彻底失败")

    monkeypatch.setattr(obs.json, "dumps", boom)
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    record.event = "test.total"  # type: ignore[attr-defined]
    line = JsonFormatter().format(record)
    assert line == '{"level": "ERROR", "message": "<log serialization failed>"}'


def test_json_formatter_repr_failure_default() -> None:
    class Nasty:
        def __repr__(self) -> str:
            raise RuntimeError("repr 失败")

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    record.event = "test.nastydefault"  # type: ignore[attr-defined]
    record.fields = {"obj": Nasty()}  # type: ignore[attr-defined]
    data = json.loads(JsonFormatter().format(record))
    assert data["obj"] == "<unserializable>"


def test_console_formatter_plain_and_exception_records() -> None:
    plain = logging.LogRecord("t", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    line = ConsoleFormatter().format(plain)
    assert "hello world" in line and "INFO" in line

    bad = logging.LogRecord("t", logging.INFO, __file__, 1, "%d", ("not-int",), None)
    assert "<unprintable message>" in ConsoleFormatter().format(bad)

    import sys

    exc_record = logging.LogRecord("t", logging.ERROR, __file__, 1, "msg", None, None)
    try:
        raise KeyError("k")
    except KeyError:
        exc_record.exc_info = sys.exc_info()
    assert "KeyError" in ConsoleFormatter().format(exc_record)


def test_setup_logging_survives_handler_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")

    class BadHandler(logging.Handler):
        def close(self) -> None:
            raise RuntimeError("close 失败")

    bad = BadHandler()
    bad._harness_handler = True
    root = logging.getLogger()
    root.addHandler(bad)
    try:
        setup_logging()  # 旧 handler.close 异常不得击穿启动
        assert bad not in root.handlers
    finally:
        root.removeHandler(bad)


# ---------------------------------------------------------------------------
# 3. Gateway：llm.request（Mock / 真实重试 / 密钥防泄漏）
# ---------------------------------------------------------------------------


class _BlindPayload(BaseModel):
    answer_latex: str
    reasoning_brief: str = ""


async def test_llm_request_mock_event(caplog: pytest.LogCaptureFixture) -> None:
    await chat_json(
        [{"role": "user", "content": "求解"}],
        _BlindPayload,
        model="test-model",
        purpose="solver_blind",
    )
    fields = _f(_one(caplog, "llm.request"))
    assert fields["mock"] is True
    assert fields["purpose"] == "solver_blind"
    assert fields["model"] == "test-model"
    assert fields["ok"] is True
    assert fields["attempt"] == 1
    assert fields["latency_ms"] >= 0
    assert fields["error_kind"] is None


async def test_llm_request_mock_unregistered_purpose(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(LLMGatewayError):
        await chat_json([], _BlindPayload, model="m", purpose="not_registered")
    fields = _f(_one(caplog, "llm.request"))
    assert fields["mock"] is True and fields["ok"] is False
    assert fields["error_kind"] == "MOCK_PURPOSE_UNREGISTERED"


async def test_llm_request_no_secret_leak(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """monkeypatch 假密钥后，日志全文（含所有 record 序列化）不得含密钥。"""
    secret = "sk-fake-secret-key-987654321"
    monkeypatch.setattr(settings, "LLM_API_KEY", secret)
    await chat_json(
        [{"role": "user", "content": f"题干含密钥引用 {secret}"}],
        _BlindPayload,
        model="m",
        purpose="solver_blind",
    )
    assert _events(caplog, "llm.request")
    assert secret not in _all_log_text(caplog)


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_gateway.httpx, "AsyncClient", factory)
    return requests


async def test_llm_request_real_mode_retry_attempts(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-real-mode-fake-key"
    monkeypatch.setattr(settings, "USE_MOCK_LLM", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", secret)
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://fake-gateway.local/v1")

    async def fake_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr(llm_gateway, "_sleep_backoff", fake_sleep)
    content = json.dumps({"answer_latex": "x = 5", "reasoning_brief": ""})
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(500, text="err")
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )

    _install_transport(monkeypatch, handler)
    result = await chat_json(
        [{"role": "user", "content": "求解"}], _BlindPayload, model="m", purpose="solver_blind"
    )
    assert result.answer_latex == "x = 5"
    fields = _f(_one(caplog, "llm.request"))
    assert fields["mock"] is False
    assert fields["attempt"] == 3
    assert fields["ok"] is True
    assert fields["latency_ms"] >= 0
    assert secret not in _all_log_text(caplog)


async def test_llm_request_real_mode_exhausted(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "USE_MOCK_LLM", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-x")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://fake-gateway.local/v1")

    async def fake_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr(llm_gateway, "_sleep_backoff", fake_sleep)
    _install_transport(monkeypatch, lambda request: httpx.Response(503, text="down"))
    with pytest.raises(LLMGatewayError):
        await chat_json([], _BlindPayload, model="m", purpose="solver_blind")
    fields = _f(_one(caplog, "llm.request"))
    assert fields["ok"] is False
    assert fields["error_kind"] == "HTTP_5XX"
    assert fields["attempt"] == 3


# ---------------------------------------------------------------------------
# 4. 沙盒：sandbox.exec
# ---------------------------------------------------------------------------


def test_sandbox_exec_ok_event(caplog: pytest.LogCaptureFixture) -> None:
    result = run_sympy_in_sandbox(_VALID_KERNEL)
    assert result.ok is True
    fields = _f(_one(caplog, "sandbox.exec"))
    assert fields["ok"] is True
    assert fields["duration_ms"] >= 0
    assert fields["error_kind"] is None
    assert fields["mode"] == "kernel"


def test_sandbox_exec_ast_rejected_event(caplog: pytest.LogCaptureFixture) -> None:
    result = run_sympy_in_sandbox("import os\n")
    assert result.ok is False
    fields = _f(_one(caplog, "sandbox.exec"))
    assert fields["ok"] is False
    assert fields["error_kind"] == "AST_REJECTED"


def test_sandbox_exec_equivalence_event(caplog: pytest.LogCaptureFixture) -> None:
    assert verify_equation_equivalence("x + 1", "1 + x") is True
    records = _events(caplog, "sandbox.exec")
    fields = _f(records[-1])
    assert fields["mode"] == "equivalence"
    assert fields["ok"] is True
    assert fields["equivalent"] is True
    assert fields["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# 5. FSM / 门禁 / 降阶 / 升层
# ---------------------------------------------------------------------------


async def test_gate_confirmed_and_fsm_transition_events(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, install_diagnosis: Any
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)
    caplog.clear()
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)

    gate = _f(_one(caplog, "gate.confirmed"))
    assert gate["session_id"] == card.session_id
    assert gate["question_uid"] == card.question_uid
    assert gate["branch"] == "CONCEPT_BOTTLENECK"
    assert gate["overridden"] is False
    assert gate["floor_applied"] is True
    assert gate["new_stage"] == "STAGE_1_BLIND_PROBE"

    trans = _f(_one(caplog, "fsm.transition"))
    assert trans["from_stage"] == "STAGE_0_CONFIRM_GATE"
    assert trans["to_stage"] == "STAGE_1_BLIND_PROBE"
    assert trans["trigger"] == "gate_confirmed"
    assert trans["session_id"] == card.session_id


async def test_fsm_transition_stage1_to_stage2(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, install_diagnosis: Any
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)
    caplog.clear()
    resp = await submit_stage_answer(card.session_id, STAGE1_WRONG, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_2_MICRO_SCAFFOLD"

    trans = [
        _f(r)
        for r in _events(caplog, "fsm.transition")
        if _f(r)["from_stage"] == "STAGE_1_BLIND_PROBE"
    ]
    assert trans and trans[0]["to_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    assert trans[0]["trigger"] == "stage1_stuck"
    assert trans[0]["question_uid"] == card.question_uid

    sub = _f(_one(caplog, "fsm.submission"))
    assert sub["stage"] == "STAGE_1_BLIND_PROBE"
    assert sub["correct"] is False
    assert sub["no_idea"] is False


async def test_descent_triggered_and_generated_events(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, install_diagnosis: Any
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)
    await submit_stage_answer(card.session_id, STAGE1_WRONG, db_path=tmp_db)
    caplog.clear()
    await submit_stage_answer(card.session_id, "不会", db_path=tmp_db)

    triggered = _f(_one(caplog, "descent.triggered"))
    assert triggered["session_id"] == card.session_id
    assert triggered["question_uid"] == card.question_uid
    assert triggered["depth"] == 0
    assert triggered["elo_before"] > 0
    # Mock 降阶内核必成功：generated 或 floor_reveal 二者居其一（契约）
    assert _events(caplog, "descent.generated") or _events(caplog, "descent.floor_reveal")
    if _events(caplog, "descent.generated"):
        gen = _f(_one(caplog, "descent.generated"))
        assert gen["depth"] == 1
        assert gen["elo_after"] < gen["elo_before"]
    sub = _f(_one(caplog, "fsm.submission"))
    assert sub["stage"] == "STAGE_2_MICRO_SCAFFOLD" and sub["no_idea"] is True


async def test_ascent_step_event(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, install_diagnosis: Any
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)
    await submit_stage_answer(card.session_id, STAGE1_WRONG, db_path=tmp_db)
    await submit_stage_answer(card.session_id, "不会", db_path=tmp_db)  # 降阶一层
    caplog.clear()
    resp = await submit_stage_answer(card.session_id, "x = 5", db_path=tmp_db)  # 答对回上层
    assert "回到上一层" in resp.view_payload.get("notice", "")
    ascent = _f(_one(caplog, "ascent.step"))
    assert ascent["session_id"] == card.session_id
    assert ascent["question_uid"] == card.question_uid
    assert ascent["elo_after"] > ascent["elo_before"]
    assert ascent["depth"] == 0


async def test_gate_mastered_elo_event(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, install_diagnosis: Any
) -> None:
    # P7-A：initial_state='已掌握' 已改为录入即结算；门禁分支仍可达的前置构造：
    # VLM 诊断判 ALREADY_MASTERED 但 initial_state=错题（obs _ingest 固定 CONCEPT，故直构）。
    install_diagnosis(_payload("ALREADY_MASTERED"))
    card = await ingest_question(STEM_MD, [], [], GRADE, initial_state="错题", db_path=tmp_db)
    assert card.auto_mastered is False
    caplog.clear()
    await confirm_diagnostic_gate(card.session_id, 1, "ALREADY_MASTERED", db_path=tmp_db)
    gate = _f(_one(caplog, "gate.confirmed"))
    assert gate["branch"] == "ALREADY_MASTERED"
    assert gate["floor_applied"] is False
    assert gate["new_stage"] == "SESSION_COMPLETED"
    elo = _f(_one(caplog, "elo.event"))
    assert elo["trigger"] == "MASTERED_INIT"
    assert elo["applied"] is True
    assert elo["question_uid"] == card.question_uid


# ---------------------------------------------------------------------------
# 6. Elo / Escrow 事件
# ---------------------------------------------------------------------------


def test_elo_floor_event_and_idempotent_trace(
    tmp_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _mk_question(tmp_db, "q-elo", base_elo=1500)
    key = "q-elo:s1:PARTIAL_DRAFT_SUBGOAL_FLOOR"
    with get_db_connection(tmp_db) as conn:
        r1 = elo_engine.apply_draft_subgoal_floor(conn, "q-elo", "s1", 1500)
    assert r1.applied is True
    with get_db_connection(tmp_db) as conn:
        r2 = elo_engine.apply_draft_subgoal_floor(conn, "q-elo", "s1", 1500)
    assert r2.applied is False  # 二次幂等调用

    records = _events(caplog, "elo.event")
    assert len(records) == 2
    f1, f2 = _f(records[0]), _f(records[1])
    assert f1["idempotency_key"] == key and f2["idempotency_key"] == key
    assert f1["applied"] is True and f2["applied"] is False
    assert f1["combat_after"] > f1["combat_before"]
    assert f1["coverage_after"] >= f1["combat_after"]
    assert f1["question_uid"] == "q-elo" and f1["session_id"] == "s1"
    assert f2["combat_before"] == f1["combat_before"]  # 原事件快照


def test_escrow_frozen_realized_forfeited_events(
    tmp_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _mk_question(tmp_db, "q-esc", base_elo=1500)
    with get_db_connection(tmp_db) as conn:
        elo_engine.freeze_decrutch_escrow(conn, "q-esc", "sess-q-esc")
    frozen = _f(_one(caplog, "escrow.frozen"))
    assert frozen["question_uid"] == "q-esc"
    assert frozen["amount"] > 0 and frozen["decay_factor"] == 1.0

    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        elo_engine.realize_or_forfeit_blind_test(conn, "q-esc", "sess-q-esc", passed=True)
    realized = _f(_one(caplog, "escrow.realized"))
    assert realized["amount"] > 0 and realized["decay_factor"] == 1.0
    assert _f(_one(caplog, "elo.event"))["trigger"] == "BLIND_TEST_REALIZE"

    # forfeit 路径（另一题）
    _mk_question(tmp_db, "q-esc2", base_elo=1500)
    with get_db_connection(tmp_db) as conn:
        elo_engine.freeze_decrutch_escrow(conn, "q-esc2", "sess-q-esc2")
    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        elo_engine.realize_or_forfeit_blind_test(conn, "q-esc2", "sess-q-esc2", passed=False)
    forfeited = _f(_one(caplog, "escrow.forfeited"))
    assert forfeited["question_uid"] == "q-esc2" and forfeited["amount"] > 0


def test_escrow_decayed_event_and_idempotent(
    tmp_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _mk_question(tmp_db, "q-decay", mastery="PENDING_RETEST", unlock_at=PAST_UNLOCK)
    with get_db_connection(tmp_db) as conn:
        elo_engine.freeze_decrutch_escrow(conn, "q-decay", "sess-q-decay")
        conn.execute(
            "UPDATE questions SET slated_drill_date = ?, slated_exposure_count = 4"
            " WHERE question_uid = ?",
            (DAY.isoformat(), "q-decay"),
        )
    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        assert elo_engine.apply_overdue_escrow_decay(conn, DAY) == 1
    decayed = _f(_one(caplog, "escrow.decayed"))
    assert decayed["question_uid"] == "q-decay"
    assert decayed["decay_factor"] == pytest.approx(0.85)
    assert decayed["amount"] > 0
    elo = _f(_one(caplog, "elo.event"))
    assert elo["trigger"] == "ESCROW_TIMEOUT_DECAY" and elo["applied"] is True

    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        assert elo_engine.apply_overdue_escrow_decay(conn, DAY) == 0
    skip = _f(_one(caplog, "elo.event"))
    assert skip["applied"] is False  # 同日幂等跳过亦留痕


# ---------------------------------------------------------------------------
# 7. Ingest 事件
# ---------------------------------------------------------------------------


async def test_ingest_new_question_events(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, install_diagnosis: Any
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)
    received = _f(_one(caplog, "ingest.received"))
    assert received["question_uid"] == card.question_uid
    assert received["initial_state"] == "错题"
    assert len(received["stem_excerpt"]) <= 120
    diagnosis = _f(_one(caplog, "ingest.diagnosis"))
    assert diagnosis["base_elo"] == 2085
    assert diagnosis["error_category"] == "CONCEPT_BOTTLENECK"
    assert diagnosis["stalled_step"] == 2
    assert diagnosis["step_count"] == 3
    assert "tier" in diagnosis
    persisted = _f(_one(caplog, "ingest.persisted"))
    assert persisted["session_id"] == card.session_id
    assert persisted["is_reimport"] is False


async def test_ingest_reimport_modes(
    tmp_db: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    install_diagnosis: Any,
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)
    with get_db_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT stem_md FROM questions WHERE question_uid = ?", (card.question_uid,)
        ).fetchone()
    stem_with_uid = str(row["stem_md"])

    caplog.clear()
    await ingest_question(stem_with_uid, [], [], GRADE, db_path=tmp_db)
    assert _f(_one(caplog, "ingest.reimport"))["mode"] == "short_circuit"

    caplog.clear()
    await ingest_question(stem_with_uid + "\n（修订：补充条件）", [], [], GRADE, db_path=tmp_db)
    assert _f(_one(caplog, "ingest.reimport"))["mode"] == "stem_updated"
    assert _f(_one(caplog, "ingest.persisted"))["is_reimport"] is True

    caplog.clear()
    draft = tmp_path / "draft.png"
    Image.new("RGB", (8, 8), "white").save(draft)
    await ingest_question(stem_with_uid, [], [draft], GRADE, db_path=tmp_db)
    reimport = _f(_one(caplog, "ingest.reimport"))
    assert reimport["mode"] == "new_draft_session"
    assert reimport["session_id"] != card.session_id
    assert _events(caplog, "ingest.diagnosis")


# ---------------------------------------------------------------------------
# 8. 预热流水线事件
# ---------------------------------------------------------------------------


async def test_prewarm_pipeline_events(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, install_diagnosis: Any
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)
    caplog.clear()
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)

    started = _f(_one(caplog, "prewarm.started"))
    assert started["session_id"] == card.session_id
    assert started["question_uid"] == card.question_uid

    attempts = [_f(r) for r in _events(caplog, "prewarm.kernel_attempt")]
    kinds = {a["kind"] for a in attempts}
    assert kinds == {"scaffold", "variant"}
    for a in attempts:
        assert a["ast_ok"] is True and a["sandbox_ok"] is True
        assert a["error_kind"] is None and a["attempt"] >= 1
        assert a["duration_ms"] >= 0

    completed = _f(_one(caplog, "prewarm.completed"))
    assert completed["status"] == "READY_SYMPY"
    assert completed["scaffold_uid"] and completed["variant_uid"]
    assert completed["duration_ms"] >= 0


async def test_prewarm_local_hit_event(
    tmp_db: Path,
    caplog: pytest.LogCaptureFixture,
    install_diagnosis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card1 = await _ingest(tmp_db, install_diagnosis)
    card2 = await _ingest(tmp_db, install_diagnosis, stem_md=STEM_MD + "（另一道）")
    with get_db_connection(tmp_db) as conn:
        q1 = conn.execute(
            "SELECT * FROM questions WHERE question_uid = ?", (card1.question_uid,)
        ).fetchone()
        q2 = conn.execute(
            "SELECT * FROM questions WHERE question_uid = ?", (card2.question_uid,)
        ).fetchone()
        steps2 = scaffold_engine._branch_steps(conn, card2.question_uid, "canonical")
        stalled2 = scaffold_engine._find_step(steps2, 2)
    monkeypatch.setattr(scaffold_engine, "_search_local_scaffold", lambda conn, q, s: q1)
    caplog.clear()
    outcome = await scaffold_engine._prepare_scaffold(
        q2, stalled2, tmp_db, session_id=card2.session_id
    )
    assert outcome is not None and outcome.uid == card1.question_uid
    hit = _f(_one(caplog, "prewarm.local_hit"))
    assert hit["kind"] == "scaffold"
    assert hit["question_uid"] == card2.question_uid
    assert hit["reused_uid"] == card1.question_uid
    assert hit["verified"] is True


async def test_prewarm_dual_blind_fallback_events(
    tmp_db: Path,
    caplog: pytest.LogCaptureFixture,
    install_diagnosis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)
    # 空转内核（无真实求解调用）必被真实 AST 探针拒绝；具体题干模板（无占位符）
    # 保证沙盒重试耗尽后双盲降级可实例化。
    vacuous_kernel = {
        "parametric_stem_tpl": "解关于 $x$ 的一元一次方程：$2x = 10$。",
        "sympy_solver_code": (
            "def solve_kernel():\n    return {'params': {}, 'answer_latex': 'x = 5'}\n"
        ),
        "constraints_check": "x = 5",
    }
    monkeypatch.setitem(llm_mocks.MOCK_RESPONSES, "scaffold_kernel", vacuous_kernel)
    monkeypatch.setitem(llm_mocks.MOCK_RESPONSES, "variant_kernel", vacuous_kernel)
    caplog.clear()
    resp = await confirm_diagnostic_gate(card.session_id, 2, "TRAP_OR_CALC_ERROR", db_path=tmp_db)
    assert resp.prewarm_status == "READY_FALLBACK"

    attempts = [_f(r) for r in _events(caplog, "prewarm.kernel_attempt")]
    assert attempts
    for a in attempts:
        assert a["ast_ok"] is False
        assert a["error_kind"] == "VACUOUS_SCRIPT_REJECTED"
    duals = [_f(r) for r in _events(caplog, "prewarm.dual_blind")]
    assert {d["kind"] for d in duals} == {"scaffold", "variant"}
    assert all(d["consensus"] is True for d in duals)
    assert _f(_one(caplog, "prewarm.completed"))["status"] == "READY_FALLBACK"


async def test_prewarm_failed_event(
    tmp_db: Path,
    caplog: pytest.LogCaptureFixture,
    install_diagnosis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = await _ingest(tmp_db, install_diagnosis)

    async def boom(session_id: str, db_path: Path | None) -> None:
        raise RuntimeError("模拟预热崩溃")

    monkeypatch.setattr(scaffold_engine, "_prewarm_impl", boom)
    caplog.clear()
    await scaffold_engine.prewarm_session_questions(card.session_id, tmp_db)
    failed = _f(_one(caplog, "prewarm.failed"))
    assert failed["session_id"] == card.session_id
    assert failed["error_kind"] == "RuntimeError"
    with get_db_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT prewarm_status FROM diagnostic_sessions WHERE session_id = ?",
            (card.session_id,),
        ).fetchone()
    assert row["prewarm_status"] == "FAILED"


# ---------------------------------------------------------------------------
# 9. 盲测调度事件
# ---------------------------------------------------------------------------


def test_drill_queue_locked_and_idempotent(
    tmp_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _mk_question(tmp_db, "q-drill", mastery="PENDING_RETEST", unlock_at=PAST_UNLOCK)
    with get_db_connection(tmp_db) as conn:
        items = get_daily_blind_drill_queue(conn, DAY)
    assert [i.question_uid for i in items] == ["q-drill"]
    locked = _f(_one(caplog, "drill.queue_locked"))
    assert locked["active_date"] == DAY.isoformat()
    assert locked["uids"] == ["q-drill"]
    assert locked["deferred_uids"] == []
    assert locked["idempotent"] is False

    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        get_daily_blind_drill_queue(conn, DAY)
    assert _f(_one(caplog, "drill.queue_locked"))["idempotent"] is True


def test_drill_submission_and_retrain_events(
    tmp_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _mk_question(tmp_db, "q-wrong", mastery="PENDING_RETEST", unlock_at=PAST_UNLOCK)
    with get_db_connection(tmp_db) as conn:
        elo_engine.freeze_decrutch_escrow(conn, "q-wrong", "sess-q-wrong")
        get_daily_blind_drill_queue(conn, DAY)
    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        outcome = evaluate_blind_drill_submission(conn, "q-wrong", "x = 9", active_date=DAY)
    assert outcome.passed is False
    sub = _f(_one(caplog, "drill.submission"))
    assert sub["question_uid"] == "q-wrong"
    assert sub["final_ok"] is False and sub["passed"] is False
    assert sub["active_date"] == DAY.isoformat()
    retrain = _f(_one(caplog, "drill.retrain"))
    assert retrain["retrain_stage"] == "STAGE_1_BLIND_PROBE"
    assert retrain["new_session_id"].startswith("retrain-")
    assert _events(caplog, "escrow.forfeited")


def test_drill_cascade_event(tmp_db: Path, caplog: pytest.LogCaptureFixture) -> None:
    _mk_question(tmp_db, "q-parent", mastery="PENDING_RETEST", unlock_at=PAST_UNLOCK)
    _mk_question(tmp_db, "q-variant")
    _mk_question(tmp_db, "q-child", base_elo=1200, mastery="PENDING_RETEST", unlock_at=PAST_UNLOCK)
    _mk_relation(tmp_db, "q-variant", "q-parent", "CLONE")
    _mk_relation(tmp_db, "q-child", "q-parent", "SCAFFOLD_PRE", is_directed=1, containment=0.9)
    with get_db_connection(tmp_db) as conn:
        elo_engine.freeze_decrutch_escrow(conn, "q-parent", "sess-q-parent")
        elo_engine.freeze_decrutch_escrow(conn, "q-child", "sess-q-child")
        get_daily_blind_drill_queue(conn, DAY)
    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        outcome = evaluate_blind_drill_submission(conn, "q-parent", "x = 5", active_date=DAY)
    assert outcome.passed is True
    assert outcome.cascaded_question_uids == ["q-child"]
    cascade = _f(_one(caplog, "drill.cascade"))
    assert cascade["question_uid"] == "q-parent"
    assert cascade["uids"] == ["q-child"]
    sub = _f(_one(caplog, "drill.submission"))
    assert sub["passed"] is True and sub["drill_question_uid"] == "q-variant"
    realized = [_f(r) for r in _events(caplog, "escrow.realized")]
    assert {r["question_uid"] for r in realized} == {"q-parent", "q-child"}


# ---------------------------------------------------------------------------
# 10. 知识网事件
# ---------------------------------------------------------------------------


async def test_graph_node_created_and_reused_events(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_db_connection(tmp_db) as conn:
        nid, reused = await knowledge_graph.match_or_create_l3_node(
            conn, "力学", "实体", "算子A", []
        )
    assert reused is False
    created = _f(_one(caplog, "graph.node_created"))
    assert created["l1"] == "力学" and created["l2"] == "实体"
    assert created["l3"] == "算子A" and created["node_id"] == nid

    async def fake_judge(payload: dict[str, Any]) -> NodeJudgeResult:
        return NodeJudgeResult(match_node_id=nid, evidence="同一算子")

    monkeypatch.setattr(knowledge_graph, "_judge_nodes", fake_judge)
    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        nid2, reused2 = await knowledge_graph.match_or_create_l3_node(
            conn, "力学", "实体", "算子A别名", []
        )
    assert reused2 is True and nid2 == nid
    reused_ev = _f(_one(caplog, "graph.node_reused"))
    assert reused_ev["node_id"] == nid and reused_ev["l3"] == "算子A别名"


async def test_graph_compaction_merged_and_reverted_events(
    tmp_db: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_db_connection(tmp_db) as conn:
        n1, _ = await knowledge_graph.match_or_create_l3_node(
            conn, "力学", "实体", "动量定理求电荷量", []
        )
        n2, _ = await knowledge_graph.match_or_create_l3_node(
            conn, "力学", "实体", "动量定理求电荷量大小", []
        )

    async def confirm_judge(payload: dict[str, Any]) -> NodeJudgeResult:
        return NodeJudgeResult(
            match_node_id=payload["existing_nodes"][0]["node_id"], evidence="近义合并"
        )

    monkeypatch.setattr(knowledge_graph, "_judge_nodes", confirm_judge)
    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        batch_id = knowledge_graph.run_scoped_compaction(conn, "力学", "实体")
    assert batch_id is not None
    merged = _f(_one(caplog, "graph.compaction_merged"))
    assert merged["batch_id"] == batch_id
    assert {merged["from_node"], merged["to_node"]} == {n1, n2}
    assert merged["l1"] == "力学" and merged["l2"] == "实体"

    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        knowledge_graph.revert_compaction_batch(conn, batch_id)
    reverted = _f(_one(caplog, "graph.reverted"))
    assert reverted["batch_id"] == batch_id and reverted["merges"] == 1

    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        knowledge_graph.revert_compaction_batch(conn, batch_id)  # 幂等
    assert _f(_one(caplog, "graph.reverted"))["merges"] == 0


def test_graph_co_error_events(tmp_db: Path, caplog: pytest.LogCaptureFixture) -> None:
    _mk_question(tmp_db, "q-co", node_id="node-a")
    with get_db_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO knowledge_nodes (node_id, l1_module, l2_entity, l3_operator,"
            " aliases_json) VALUES ('node-b', '力学', '实体', '算子B', '[]')"
        )
        conn.execute(
            "INSERT INTO question_steps (step_id, question_uid, solution_branch_id, step_index,"
            " step_goal_desc, step_formula_latex, knowledge_node_id, masked_conditions_json,"
            " probe_question_md, is_blocked_step, reused_existing_node,"
            " c_sub, s_sub, t_sub, l_sub, sub_elo)"
            " VALUES ('step2-q-co', 'q-co', 'canonical', 2, '子目标2', 'y = 3', 'node-b',"
            " '[]', 'probe?', 1, 0, 2, 2, 2, 1, 1400)"
        )
        knowledge_graph.record_co_error_edges(conn, "q-co")
    recorded = _f(_one(caplog, "graph.co_error"))
    assert recorded["action"] == "record"
    assert recorded["question_uid"] == "q-co"
    assert recorded["nodes"] == ["node-a", "node-b"]
    assert recorded["edges"] == 1

    caplog.clear()
    with get_db_connection(tmp_db) as conn:
        knowledge_graph.decrement_co_error_edges_on_mastery(conn, "q-co")
    decremented = _f(_one(caplog, "graph.co_error"))
    assert decremented["action"] == "decrement"
    assert decremented["question_uid"] == "q-co"


# ---------------------------------------------------------------------------
# 11. API：api.request + X-Request-ID + request_id 贯穿 + 错误路径
# ---------------------------------------------------------------------------


def test_api_request_event_and_request_id_header(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(frontend_dir=tmp_path / "frontend")
    with TestClient(app) as client:
        resp = client.get("/api/v1/drills/daily")
    assert resp.status_code == 200
    rid = resp.headers["X-Request-ID"]
    assert rid

    api_records = _events(caplog, "api.request")
    assert api_records
    record = api_records[-1]
    fields = _f(record)
    assert fields["method"] == "GET"
    assert fields["path"] == "/api/v1/drills/daily"
    assert fields["status"] == 200
    assert fields["duration_ms"] >= 0
    assert record.request_id == rid  # type: ignore[attr-defined]
    # request_id 经 contextvar 贯穿到服务层事件
    queue_records = _events(caplog, "drill.queue_locked")
    assert queue_records
    assert queue_records[-1].request_id == rid  # type: ignore[attr-defined]


def test_api_request_404_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(frontend_dir=tmp_path / "frontend")
    with TestClient(app) as client:
        resp = client.get("/api/v1/sessions/nonexistent")
    assert resp.status_code == 404
    fields = _f(_events(caplog, "api.request")[-1])
    assert fields["status"] == 404
    assert fields["session_id"] == "nonexistent"


def test_api_request_500_unhandled_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(session_id: str, db_path: Path | None = None) -> Any:
        raise RuntimeError("模拟内部错误")

    monkeypatch.setattr(main_module, "get_session_view", boom)
    monkeypatch.setattr(main_module, "_require_session", lambda session_id: None)
    app = create_app(frontend_dir=tmp_path / "frontend")
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/sessions/s-x")
    assert resp.status_code == 500
    fields = _f(_events(caplog, "api.request")[-1])
    assert fields["status"] == 500
    assert fields["session_id"] == "s-x"
    assert resp.headers["X-Request-ID"]
