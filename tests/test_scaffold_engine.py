"""P2-E：脚手架引擎测试（USE_MOCK_LLM=True + 真实 SQLite + 真实沙盒，零真实网络）。

规格：docs/tasks/P2-E_scaffold.md 测试要求 +
docs/designs/adaptive_practice_harness.md §3.2 / §3.3 状态机图 / CUJ 2/3。

造题统一走 ingest_service.ingest_question（monkeypatch 诊断注入类别/前缀/负荷），
默认 Mock 诊断：3 步拱形桥题 (3,3,2,3) → base_elo 2085，卡在 Step 2，
前缀保底 sub_elo 1290；内核 Mock 默认参数解 x = 5，双盲答案一致。
"""

import asyncio
import copy
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.services import elo_engine, ingest_service, llm_mocks, scaffold_engine
from backend.services.ingest_service import GateCardDTO, ingest_question
from backend.services.scaffold_engine import (
    SessionStepResponse,
    confirm_diagnostic_gate,
    in_flight_prewarm_tasks,
    prewarm_session_questions,
    submit_stage_answer,
)
from backend.services.vlm_diagnosis import DiagnosisPayload

GRADE = "高中物理-高二"
STEM_MD = (
    "汽车以速度 $v$ 通过半径 $R = 40\\,\\text{m}$ 的拱形桥最高点，"
    "取 $g = 10\\,\\text{m/s}^2$。求汽车不脱离桥面的最大速度 $v_{max}$。"
)
EXPECTED_BASE_ELO = 2085
PREFIX_SUB_ELO = 1290  # steps[last_valid=1 - 1].sub_elo（Mock 诊断 Step 1 前缀）
TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# 一阶卡点步（Step 2）结论段：v_{max} = sqrt(gR)
STAGE1_CORRECT = r"v_{max} = \sqrt{gR}"
STAGE1_WRONG = r"v_{max} = \sqrt{2gR}"
STAGE3_CORRECT = r"v_{max} = 20\ \text{m/s}"
STAGE4_CORE_EQUATION = "2*(x + 3) = 16"  # 与 x = 5 常数比率等价（核心列式模式）
STAGE4_WRONG = "x = 999"

_VACUOUS_CODE = "def solve_kernel():\n    return {'params': {}, 'answer_latex': 'x = 5'}\n"
_CONCRETE_SCAFFOLD_TPL = "解关于 $x$ 的一元一次方程：$3x + 5 = 20$。"
_CONCRETE_VARIANT_TPL = "一个矩形的宽为 $3$，周长为 $16$。设长为 $x$，求 $x$。"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "USE_MOCK_LLM", True)


@pytest.fixture(autouse=True)
async def _isolate_prewarm_tasks() -> Any:
    """每个测试前后清理在途预热 Task 字典，防止跨测试泄漏。"""
    in_flight_prewarm_tasks.clear()
    yield
    for task in list(in_flight_prewarm_tasks.values()):
        task.cancel()
    in_flight_prewarm_tasks.clear()
    await asyncio.sleep(0)


@pytest.fixture()
def install_diagnosis(monkeypatch: pytest.MonkeyPatch) -> Any:
    """monkeypatch 诊断：注入定制 DiagnosisPayload（类别/前缀/负荷/骨架）。"""

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


def _payload(
    category: str = "CONCEPT_BOTTLENECK",
    last_valid: int = 1,
    stalled: int = 2,
    l_score: int | None = None,
) -> DiagnosisPayload:
    payload = DiagnosisPayload.model_validate(copy.deepcopy(llm_mocks.MOCK_RESPONSES["diagnosis"]))
    payload.alignment.error_category = category  # type: ignore[assignment]
    payload.alignment.last_valid_step_index = last_valid
    payload.alignment.stalled_step_index = stalled
    payload.alignment.trap_taxonomy_tag = (
        "TRAP_DOMAIN_BOUNDARY" if category == "TRAP_OR_CALC_ERROR" else None
    )
    if l_score is not None:
        payload.trace.l_score = l_score
    return payload


async def _ingest(
    db_path: Path,
    install_diagnosis: Any,
    payload: DiagnosisPayload | None = None,
    stem_md: str = STEM_MD,
    initial_state: str = "错题",
) -> GateCardDTO:
    install_diagnosis(payload if payload is not None else _payload())
    return await ingest_question(
        stem_md, [], [], GRADE, initial_state=initial_state, db_path=db_path  # type: ignore[arg-type]
    )


async def _concept_session(db_path: Path, install_diagnosis: Any, **kwargs: Any) -> GateCardDTO:
    return await _ingest(db_path, install_diagnosis, _payload(**kwargs))


async def _drain(session_id: str) -> None:
    """等待在途预热 Task 完成（并让 done 回调出队）。"""
    task = in_flight_prewarm_tasks.get(session_id)
    if task is not None:
        await task
    await asyncio.sleep(0)


def _query(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with get_db_connection(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def _one(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row:
    rows = _query(db_path, sql, params)
    assert len(rows) == 1, f"期望恰好一行: {sql} -> {len(rows)}"
    return rows[0]


def _execute(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute(sql, params)


def _ledger_events(db_path: Path, trigger: str) -> list[sqlite3.Row]:
    return _query(
        db_path, "SELECT * FROM elo_mutation_ledger WHERE mutation_trigger = ?", (trigger,)
    )


def _assert_blind_window(db_path: Path, question_uid: str, min_days: int, max_days: int) -> None:
    row = _one(db_path, "SELECT * FROM questions WHERE question_uid = ?", (question_uid,))
    now = datetime.now(UTC)
    unlock = datetime.strptime(str(row["blind_test_unlock_at"]), TS_FORMAT).replace(
        tzinfo=UTC
    )
    due = datetime.strptime(str(row["blind_test_due_at"]), TS_FORMAT).replace(tzinfo=UTC)
    assert abs((unlock - now).total_seconds() - min_days * 86400) < 300
    assert abs((due - now).total_seconds() - max_days * 86400) < 300


class _CyclingBlindResponses(dict[str, Any]):
    """solver_blind 按调用次序循环返回不同答案（模拟双盲不一致）。"""

    def __init__(self, base: dict[str, Any], answers: list[str]) -> None:
        super().__init__(base)
        self._answers = answers
        self._calls = 0

    def __getitem__(self, key: str) -> Any:
        if key == "solver_blind":
            answer = self._answers[self._calls % len(self._answers)]
            self._calls += 1
            return {"answer_latex": answer, "reasoning_brief": "独立盲做"}
        return super().__getitem__(key)


def _bad_kernel(tpl: str) -> dict[str, Any]:
    return {
        "parametric_stem_tpl": tpl,
        "sympy_solver_code": _VACUOUS_CODE,
        "constraints_check": "x 为正整数。",
    }


async def _reach_stage4(db_path: Path, sid: str) -> SessionStepResponse:
    """概念分支快速通道：一阶答对 → 三阶答对 → 四阶。"""
    resp = await submit_stage_answer(sid, STAGE1_CORRECT, db_path=db_path)
    assert resp.micro_stage == "STAGE_3_REDO_ORIGINAL"
    resp = await submit_stage_answer(sid, STAGE3_CORRECT, db_path=db_path)
    assert resp.micro_stage == "STAGE_4_DECRUTCH_VARIANT"
    return resp


# ---------------------------------------------------------------------------
# confirm_diagnostic_gate 三分支
# ---------------------------------------------------------------------------


async def test_confirm_mastered_archives_and_settles_once(
    tmp_db: Path, install_diagnosis: Any
) -> None:
    card = await _ingest(tmp_db, install_diagnosis, initial_state="已掌握")
    resp = await confirm_diagnostic_gate(card.session_id, 2, "ALREADY_MASTERED", db_path=tmp_db)
    assert resp.micro_stage == "SESSION_COMPLETED"
    assert resp.mastery_state == "MASTERED"
    assert resp.prewarm_status == "IDLE"
    assert resp.explanation_text and "已掌握" in resp.explanation_text
    events = _ledger_events(tmp_db, "MASTERED_INIT")
    assert len(events) == 1
    assert card.session_id not in in_flight_prewarm_tasks
    # 重复确认被拒绝，且不重复结算
    with pytest.raises(ValueError):
        await confirm_diagnostic_gate(card.session_id, 2, "ALREADY_MASTERED", db_path=tmp_db)
    assert len(_ledger_events(tmp_db, "MASTERED_INIT")) == 1


async def test_confirm_concept_floor_grant_and_blind_probe_view(
    tmp_db: Path, install_diagnosis: Any
) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    resp = await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    assert resp.micro_stage == "STAGE_1_BLIND_PROBE"
    assert resp.mastery_state == "ACTIVE"
    # 一阶防剧透：仅遮罩条件 + probe，不含原题题干/答案
    view = resp.view_payload
    assert view["stalled_step_index"] == 2
    assert view["masked_conditions"] == ["汽车不脱离桥面", r"桥面支持力 $N \geq 0$"]
    assert "支持力" in view["probe_question_md"]
    assert "stem_md" not in view
    # 保底发放 + 解释条
    assert resp.explanation_text and "保底" in resp.explanation_text
    events = _ledger_events(tmp_db, "PARTIAL_DRAFT_SUBGOAL_FLOOR")
    assert len(events) == 1
    assert int(events[0]["eval_difficulty_elo"]) == PREFIX_SUB_ELO
    pool = _one(tmp_db, "SELECT * FROM question_escrow_pool")
    assert pool["escrow_status"] == "FLOOR_ONLY"
    assert float(pool["floor_elo_applied"]) > 0
    assert float(pool["initial_combat_elo_snapshot"]) == settings.MODULE_ELO_INITIAL
    # 门禁标志与步骤阻断刷新
    session = _one(tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (sid,))
    assert session["user_confirmed_gate"] == 1
    assert session["user_overridden"] == 0
    blocked = _query(
        tmp_db,
        "SELECT step_index, is_blocked_step FROM question_steps "
        "WHERE question_uid = ? ORDER BY step_index",
        (card.question_uid,),
    )
    assert [int(r["is_blocked_step"]) for r in blocked] == [0, 1, 1]
    # 预热已登记（CONCEPT 分支不阻塞前台）
    assert sid in in_flight_prewarm_tasks
    await _drain(sid)


async def test_confirm_concept_no_prefix_no_floor(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis, last_valid=0, stalled=1)
    resp = await confirm_diagnostic_gate(card.session_id, 1, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    assert resp.micro_stage == "STAGE_1_BLIND_PROBE"
    assert resp.explanation_text is None
    assert _ledger_events(tmp_db, "PARTIAL_DRAFT_SUBGOAL_FLOOR") == []
    await _drain(card.session_id)


async def test_confirm_trap_direct_stage4_shared_trap_variant(
    tmp_db: Path, install_diagnosis: Any
) -> None:
    card = await _ingest(tmp_db, install_diagnosis, _payload(category="TRAP_OR_CALC_ERROR"))
    resp = await confirm_diagnostic_gate(
        card.session_id, 2, "TRAP_OR_CALC_ERROR", db_path=tmp_db
    )
    assert resp.micro_stage == "STAGE_4_DECRUTCH_VARIANT"
    assert resp.prewarm_status == "READY_SYMPY"  # TRAP 分支 await 在途预热
    assert _ledger_events(tmp_db, "PARTIAL_DRAFT_SUBGOAL_FLOOR") == []
    view = resp.view_payload
    assert view["variant"] is not None
    assert "矩形" in view["variant"]["stem_md"]
    assert view["verification_mode"] == "equation_skeleton"  # 原题 L=3
    assert view["allowed_symbols"] == ["x"]
    assert view["verification_badge"] is None
    # SHARED_TRAP 变式边（无向）
    session = _one(
        tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card.session_id,)
    )
    edge = _one(
        tmp_db,
        "SELECT * FROM question_relations WHERE relation_type = 'SHARED_TRAP'",
    )
    assert edge["source_question_uid"] == session["prewarm_variant_uid"]
    assert edge["target_question_uid"] == card.question_uid
    assert edge["is_directed"] == 0
    assert edge["confidence_source"] == "SYMPY_KERNEL_GEN"
    await _drain(card.session_id)


async def test_confirm_override_updates_gate_and_blocked_steps(
    tmp_db: Path, install_diagnosis: Any
) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    resp = await confirm_diagnostic_gate(
        card.session_id, 3, "TRAP_OR_CALC_ERROR", db_path=tmp_db
    )
    assert resp.micro_stage == "STAGE_4_DECRUTCH_VARIANT"
    session = _one(
        tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card.session_id,)
    )
    assert session["user_overridden"] == 1
    assert session["stalled_step_index"] == 3
    assert session["error_category"] == "TRAP_OR_CALC_ERROR"
    blocked = _query(
        tmp_db,
        "SELECT is_blocked_step FROM question_steps WHERE question_uid = ? ORDER BY step_index",
        (card.question_uid,),
    )
    assert [int(r["is_blocked_step"]) for r in blocked] == [0, 0, 1]
    assert _ledger_events(tmp_db, "PARTIAL_DRAFT_SUBGOAL_FLOOR") == []
    await _drain(card.session_id)


async def test_confirm_validation_errors(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    with pytest.raises(ValueError):
        await confirm_diagnostic_gate(card.session_id, 2, "NOT_A_CATEGORY", db_path=tmp_db)
    with pytest.raises(ValueError):
        await confirm_diagnostic_gate(card.session_id, 0, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    with pytest.raises(ValueError):
        await confirm_diagnostic_gate(card.session_id, 99, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    with pytest.raises(ValueError):
        await confirm_diagnostic_gate("no-such-session", 1, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)


# ---------------------------------------------------------------------------
# 预热流水线：内核先行 / 双盲降级 / FAILED / 本地检索复用 / 合成题不衍生
# ---------------------------------------------------------------------------


async def test_prewarm_sympy_pipeline_persists_synthetics(
    tmp_db: Path, install_diagnosis: Any
) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(sid)
    session = _one(tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (sid,))
    assert session["prewarm_status"] == "READY_SYMPY"
    scaffold_uid = str(session["prewarm_scaffold_uid"])
    variant_uid = str(session["prewarm_variant_uid"])

    scaffold = _one(tmp_db, "SELECT * FROM questions WHERE question_uid = ?", (scaffold_uid,))
    assert scaffold["origin_type"] == "AI_SYNTHETIC_SCAFFOLD"
    assert scaffold["parent_question_uid"] == card.question_uid
    assert scaffold["verification_status"] == "SYMPY_VERIFIED"
    assert "3x + 5 = 20" in scaffold["stem_md"]  # 参数已回填，无占位符残留
    assert "{" not in scaffold["stem_md"]
    assert scaffold["standard_answer_latex"] == "x = 5"
    assert int(scaffold["base_elo"]) <= EXPECTED_BASE_ELO - 200
    artifact = json.loads(scaffold["verification_artifact_json"])
    assert artifact["method"] == "SYMPY_KERNEL"
    assert "solve_kernel" in artifact["sympy_solver_code"]
    assert artifact["answer_latex"] == "x = 5"
    assert artifact["sympy_version"]
    assert artifact["attempts"] == 1

    variant = _one(tmp_db, "SELECT * FROM questions WHERE question_uid = ?", (variant_uid,))
    assert variant["origin_type"] == "AI_SYNTHETIC_VARIANT"
    assert variant["verification_status"] == "SYMPY_VERIFIED"
    assert "周长为 $16$" in variant["stem_md"]

    # SCAFFOLD_PRE 有向边：source=子题 target=原题，针对卡点步
    edge = _one(
        tmp_db, "SELECT * FROM question_relations WHERE relation_type = 'SCAFFOLD_PRE'"
    )
    assert edge["source_question_uid"] == scaffold_uid
    assert edge["target_question_uid"] == card.question_uid
    assert edge["is_directed"] == 1
    assert edge["target_step_index"] == 2
    assert edge["confidence_source"] == "SYMPY_KERNEL_GEN"
    iso = _one(tmp_db, "SELECT * FROM question_relations WHERE relation_type = 'ISOMORPHIC'")
    assert iso["source_question_uid"] == variant_uid
    assert iso["is_directed"] == 0
    # 微母题单步骨架（供日后本地检索复用）
    steps = _query(
        tmp_db, "SELECT * FROM question_steps WHERE question_uid = ?", (scaffold_uid,)
    )
    assert len(steps) == 1
    assert sid not in in_flight_prewarm_tasks  # done 回调已出队


async def test_prewarm_bad_kernel_retries_then_dual_blind_fallback(
    tmp_db: Path, install_diagnosis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = copy.deepcopy(llm_mocks.MOCK_RESPONSES)
    fixtures["scaffold_kernel"] = _bad_kernel(_CONCRETE_SCAFFOLD_TPL)
    monkeypatch.setattr(llm_mocks, "MOCK_RESPONSES", fixtures)

    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    resp = await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    assert resp.micro_stage == "STAGE_1_BLIND_PROBE"  # 前台不受影响
    await _drain(sid)
    session = _one(tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (sid,))
    assert session["prewarm_status"] == "READY_FALLBACK"
    scaffold = _one(
        tmp_db, "SELECT * FROM questions WHERE question_uid = ?", (session["prewarm_scaffold_uid"],)
    )
    assert scaffold["verification_status"] == "DUAL_LLM_FALLBACK"
    assert scaffold["standard_answer_latex"] == "x = 5"
    artifact = json.loads(scaffold["verification_artifact_json"])
    assert artifact["method"] == "DUAL_LLM"
    assert artifact["blind_answers"] == ["x = 5", "x = 5"]
    assert "VACUOUS_SCRIPT_REJECTED" in artifact["last_error"]  # AST 拒绝重试后降级
    variant = _one(
        tmp_db, "SELECT * FROM questions WHERE question_uid = ?", (session["prewarm_variant_uid"],)
    )
    assert variant["verification_status"] == "SYMPY_VERIFIED"
    edge = _one(
        tmp_db, "SELECT * FROM question_relations WHERE relation_type = 'SCAFFOLD_PRE'"
    )
    assert edge["confidence_source"] == "LLM_PRIOR"

    # 二阶视图挂载 ⚠️ 徽章
    resp = await submit_stage_answer(sid, STAGE1_WRONG, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_2_MICRO_SCAFFOLD"
    assert resp.view_payload["verification_badge"] == scaffold_engine._FALLBACK_BADGE
    assert resp.view_payload["micro_scaffold"]["stem_md"] == _CONCRETE_SCAFFOLD_TPL


async def test_prewarm_dual_blind_disagreement_failed(
    tmp_db: Path, install_diagnosis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = _CyclingBlindResponses(
        copy.deepcopy(llm_mocks.MOCK_RESPONSES), ["x = 5", "x = 7"]
    )
    fixtures["scaffold_kernel"] = _bad_kernel(_CONCRETE_SCAFFOLD_TPL)
    fixtures["variant_kernel"] = _bad_kernel(_CONCRETE_VARIANT_TPL)
    monkeypatch.setattr(llm_mocks, "MOCK_RESPONSES", fixtures)

    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    resp = await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    assert resp.micro_stage == "STAGE_1_BLIND_PROBE"  # 预热异常不击穿前台
    await _drain(sid)
    session = _one(tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (sid,))
    assert session["prewarm_status"] == "FAILED"
    assert session["prewarm_scaffold_uid"] is None
    assert session["prewarm_variant_uid"] is None
    assert _one(tmp_db, "SELECT COUNT(*) AS n FROM questions")["n"] == 1
    # 预热失败时请求二阶：受控 ValueError，而非崩溃
    with pytest.raises(ValueError):
        await submit_stage_answer(sid, STAGE1_WRONG, db_path=tmp_db)


async def test_prewarm_gateway_error_converges_to_failed(
    tmp_db: Path, install_diagnosis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = copy.deepcopy(llm_mocks.MOCK_RESPONSES)
    del fixtures["scaffold_kernel"]
    del fixtures["variant_kernel"]
    monkeypatch.setattr(llm_mocks, "MOCK_RESPONSES", fixtures)
    card = await _concept_session(tmp_db, install_diagnosis)
    resp = await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    assert resp.micro_stage == "STAGE_1_BLIND_PROBE"
    await _drain(card.session_id)
    session = _one(
        tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card.session_id,)
    )
    assert session["prewarm_status"] == "FAILED"


async def test_prewarm_local_reuse_scaffold_candidate(tmp_db: Path, install_diagnosis: Any) -> None:
    # 本地简单题：同主考点（Step 2 节点）、单步、D=1120（2085-1120>=200）
    easy = _payload(last_valid=0, stalled=1)
    step2 = easy.trace.steps[1]
    step2.step_index = 1
    step2.c_sub = step2.s_sub = step2.t_sub = step2.l_sub = 1
    easy.trace.steps = [step2]
    easy.trace.c_score = easy.trace.s_score = easy.trace.t_score = easy.trace.l_score = 1
    easy_card = await _ingest(
        tmp_db, install_diagnosis, easy, stem_md="纯几何微母题：圆与平行线相切求半径比值。"
    )
    card = await _concept_session(tmp_db, install_diagnosis)
    # 对齐主考点：将简单题唯一步骤挂到原题卡点步（Step 2）的知识节点
    # （节点解析属 knowledge_graph 职责，此处直接构造「同主考点」检索前置条件）
    stalled_node = _one(
        tmp_db,
        "SELECT knowledge_node_id FROM question_steps WHERE question_uid = ? AND step_index = 2",
        (card.question_uid,),
    )["knowledge_node_id"]
    _execute(
        tmp_db,
        "UPDATE question_steps SET knowledge_node_id = ? WHERE question_uid = ?",
        (stalled_node, easy_card.question_uid),
    )
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)
    session = _one(
        tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card.session_id,)
    )
    assert session["prewarm_scaffold_uid"] == easy_card.question_uid
    assert session["prewarm_status"] == "READY_SYMPY"
    # 未生成合成微母题；复用边置信度 HUMAN_VERIFIED
    assert _query(
        tmp_db, "SELECT * FROM questions WHERE origin_type = 'AI_SYNTHETIC_SCAFFOLD'"
    ) == []
    edge = _one(
        tmp_db, "SELECT * FROM question_relations WHERE relation_type = 'SCAFFOLD_PRE'"
    )
    assert edge["source_question_uid"] == easy_card.question_uid
    assert edge["confidence_source"] == "HUMAN_VERIFIED"
    assert edge["target_step_index"] == 2


async def test_prewarm_local_reuse_isomorphic_variant(tmp_db: Path, install_diagnosis: Any) -> None:
    # 同考点集（3 节点全同）且 |ΔD|=0<=150 的本地题
    twin_card = await _ingest(
        tmp_db,
        install_diagnosis,
        _payload(category="TRAP_OR_CALC_ERROR"),
        stem_md="同构题：汽车以速度 $v$ 通过半径 $R = 50\\,\\text{m}$ 的拱形桥最高点，求最大速度。",
    )
    card = await _concept_session(tmp_db, install_diagnosis)
    # 对齐考点集： twin 题三步逐一挂到原题对应步骤的知识节点（同考点集检索前置条件）
    orig_nodes = _query(
        tmp_db,
        "SELECT step_index, knowledge_node_id FROM question_steps "
        "WHERE question_uid = ? AND solution_branch_id = 'canonical' ORDER BY step_index",
        (card.question_uid,),
    )
    for row in orig_nodes:
        _execute(
            tmp_db,
            "UPDATE question_steps SET knowledge_node_id = ? "
            "WHERE question_uid = ? AND step_index = ? AND solution_branch_id = 'canonical'",
            (row["knowledge_node_id"], twin_card.question_uid, row["step_index"]),
        )
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)
    session = _one(
        tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card.session_id,)
    )
    assert session["prewarm_variant_uid"] == twin_card.question_uid
    assert _query(
        tmp_db, "SELECT * FROM questions WHERE origin_type = 'AI_SYNTHETIC_VARIANT'"
    ) == []
    edge = _one(tmp_db, "SELECT * FROM question_relations WHERE relation_type = 'ISOMORPHIC'")
    assert edge["source_question_uid"] == twin_card.question_uid
    assert edge["confidence_source"] == "HUMAN_VERIFIED"


async def test_synthetic_parent_derivation_rejected(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)
    session = _one(
        tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card.session_id,)
    )
    scaffold_uid = str(session["prewarm_scaffold_uid"])
    # 以合成题为母题新建会话并请求预热 → 硬拒绝（FAILED，不产生任何衍生题）
    _execute(
        tmp_db,
        "INSERT INTO diagnostic_sessions (session_id, question_uid, draft_image_cas_uris_json,"
        " draft_steps_latex_json, error_category) VALUES ('syn-session', ?, '[]', '[]', ?)",
        (scaffold_uid, "CONCEPT_BOTTLENECK"),
    )
    await prewarm_session_questions("syn-session", db_path=tmp_db)
    syn_session = _one(
        tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = 'syn-session'"
    )
    assert syn_session["prewarm_status"] == "FAILED"
    assert _query(
        tmp_db, "SELECT * FROM questions WHERE parent_question_uid = ?", (scaffold_uid,)
    ) == []


# ---------------------------------------------------------------------------
# 状态机全链路（CUJ 2/3）与逐边覆盖
# ---------------------------------------------------------------------------


async def test_cuj2_cuj3_full_chain_equation_skeleton(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(sid)

    # 一阶错 → 二阶（微母题题干，无原题剧透）
    resp = await submit_stage_answer(sid, STAGE1_WRONG, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_2_MICRO_SCAFFOLD"
    assert "3x + 5 = 20" in resp.view_payload["micro_scaffold"]["stem_md"]
    # 二阶对 → 三阶（展开原题完整题干）
    resp = await submit_stage_answer(sid, "x = 5", db_path=tmp_db)
    assert resp.micro_stage == "STAGE_3_REDO_ORIGINAL"
    assert resp.view_payload["stem_md"] == STEM_MD or "拱形桥" in resp.view_payload["stem_md"]
    # 三阶对 → 四阶（L=3 → 核心列式模式 + allowed_symbols）
    resp = await submit_stage_answer(sid, STAGE3_CORRECT, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_4_DECRUTCH_VARIANT"
    assert resp.view_payload["verification_mode"] == "equation_skeleton"
    assert resp.view_payload["allowed_symbols"] == ["x"]
    # 四阶核心方程组等价（2*(x+3)=16 与 x=5 常数比率等价）→ 通关
    resp = await submit_stage_answer(sid, STAGE4_CORE_EQUATION, db_path=tmp_db)
    assert resp.micro_stage == "SESSION_COMPLETED"
    assert resp.mastery_state == "PENDING_RETEST"
    assert resp.explanation_text and "冻结" in resp.explanation_text

    # 覆盖分一次性结算 + 托管差额冻结 + 盲测窗口 3~7 天
    events = _ledger_events(tmp_db, "SCAFFOLD_COVER_GAIN")
    assert len(events) == 1
    assert int(events[0]["eval_difficulty_elo"]) == EXPECTED_BASE_ELO
    pool = _one(tmp_db, "SELECT * FROM question_escrow_pool")
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND"
    assert pool["is_cov_settled"] == 1
    floor = float(pool["floor_elo_applied"])
    target = settings.K_COMBAT * (
        1.0 - elo_engine.expected_pass(settings.MODULE_ELO_INITIAL, EXPECTED_BASE_ELO)
    )
    assert float(pool["escrow_combat_delta"]) == pytest.approx(max(0.0, target - floor), abs=1e-6)
    assert float(pool["escrow_combat_delta"]) > 0
    _assert_blind_window(tmp_db, card.question_uid, *settings.BLIND_WINDOW_CONCEPT_DAYS)
    # 当场实战分不变（零预支），覆盖分上升
    assert float(events[0]["combat_elo_before"]) == float(events[0]["combat_elo_after"])
    assert float(events[0]["coverage_elo_after"]) > float(events[0]["coverage_elo_before"])


async def test_stage1_correct_jumps_to_stage3(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(card.session_id)
    resp = await submit_stage_answer(card.session_id, STAGE1_CORRECT, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_3_REDO_ORIGINAL"


@pytest.mark.parametrize("assessment", ["no_idea", "stuck", "仍无思路"])
async def test_stage1_no_idea_self_assessment_requests_stage2(
    tmp_db: Path, install_diagnosis: Any, assessment: str
) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    await confirm_diagnostic_gate(card.session_id, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    resp = await submit_stage_answer(
        card.session_id, "", self_assessment=assessment, db_path=tmp_db
    )
    assert resp.micro_stage == "STAGE_2_MICRO_SCAFFOLD"
    assert resp.view_payload["micro_scaffold"] is not None
    await _drain(card.session_id)


async def test_stage2_wrong_stays_with_feedback(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await submit_stage_answer(sid, STAGE1_WRONG, db_path=tmp_db)
    resp = await submit_stage_answer(sid, STAGE4_WRONG, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_2_MICRO_SCAFFOLD"
    assert resp.view_payload["last_submission_correct"] is False
    assert resp.view_payload["feedback"]
    assert _ledger_events(tmp_db, "SCAFFOLD_COVER_GAIN") == []


async def test_stage3_wrong_stays_with_feedback(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(sid)
    await submit_stage_answer(sid, STAGE1_CORRECT, db_path=tmp_db)
    resp = await submit_stage_answer(sid, r"v_{max} = 999", db_path=tmp_db)
    assert resp.micro_stage == "STAGE_3_REDO_ORIGINAL"
    assert resp.view_payload["last_submission_correct"] is False
    assert _ledger_events(tmp_db, "SCAFFOLD_COVER_GAIN") == []


async def test_stage4_wrong_cooldown_then_next_day_resume(
    tmp_db: Path, install_diagnosis: Any
) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(sid)
    await _reach_stage4(tmp_db, sid)

    # 四阶错 → 冷却（骨架对比卡）
    resp = await submit_stage_answer(sid, STAGE4_WRONG, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_COOLDOWN_DEFERRED"
    assert resp.mastery_state == "ACTIVE"
    card_view = resp.view_payload["skeleton_comparison_card"]
    assert card_view["user_answer"] == STAGE4_WRONG
    assert card_view["standard_skeleton"] == "x = 5"
    assert card_view["verification_mode"] == "equation_skeleton"
    assert _ledger_events(tmp_db, "SCAFFOLD_COVER_GAIN") == []

    # 冷却未满：拒绝提交
    with pytest.raises(ValueError):
        await submit_stage_answer(sid, STAGE4_CORE_EQUATION, db_path=tmp_db)
    # 次日冷却期满 → 回四阶（重新作答）
    _execute(
        tmp_db,
        "UPDATE diagnostic_sessions SET updated_at = datetime('now', '-2 days') "
        "WHERE session_id = ?",
        (sid,),
    )
    resp = await submit_stage_answer(sid, STAGE4_CORE_EQUATION, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_4_DECRUTCH_VARIANT"
    assert resp.view_payload["notice"]
    # 重新提交核心方程 → 通关
    resp = await submit_stage_answer(sid, STAGE4_CORE_EQUATION, db_path=tmp_db)
    assert resp.micro_stage == "SESSION_COMPLETED"
    assert resp.mastery_state == "PENDING_RETEST"


async def test_trap_branch_blind_window_2_4_days(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _ingest(tmp_db, install_diagnosis, _payload(category="TRAP_OR_CALC_ERROR"))
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "TRAP_OR_CALC_ERROR", db_path=tmp_db)
    resp = await submit_stage_answer(sid, "x = 5", db_path=tmp_db)
    assert resp.micro_stage == "SESSION_COMPLETED"
    assert resp.mastery_state == "PENDING_RETEST"
    _assert_blind_window(tmp_db, card.question_uid, *settings.BLIND_WINDOW_TRAP_DAYS)
    pool = _one(tmp_db, "SELECT * FROM question_escrow_pool")
    assert float(pool["floor_elo_applied"]) == 0.0  # TRAP 分支无保底
    assert float(pool["initial_combat_elo_snapshot"]) == settings.MODULE_ELO_INITIAL
    assert float(pool["escrow_combat_delta"]) > 0
    assert _ledger_events(tmp_db, "PARTIAL_DRAFT_SUBGOAL_FLOOR") == []


async def test_l1_final_answer_mode(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis, l_score=1)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(sid)
    resp = await _reach_stage4(tmp_db, sid)
    assert resp.view_payload["verification_mode"] == "final_answer"
    assert resp.view_payload["allowed_symbols"] == []
    resp = await submit_stage_answer(sid, "x = 5", db_path=tmp_db)
    assert resp.micro_stage == "SESSION_COMPLETED"
    _assert_blind_window(tmp_db, card.question_uid, *settings.BLIND_WINDOW_CONCEPT_DAYS)


async def test_idempotent_stage4_no_double_settlement(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(sid)
    await _reach_stage4(tmp_db, sid)
    await submit_stage_answer(sid, STAGE4_CORE_EQUATION, db_path=tmp_db)
    assert len(_ledger_events(tmp_db, "SCAFFOLD_COVER_GAIN")) == 1
    # 重复提交同阶段（已完成）：拒绝且不重复结算
    with pytest.raises(ValueError):
        await submit_stage_answer(sid, STAGE4_CORE_EQUATION, db_path=tmp_db)
    assert len(_ledger_events(tmp_db, "SCAFFOLD_COVER_GAIN")) == 1
    assert len(_ledger_events(tmp_db, "PARTIAL_DRAFT_SUBGOAL_FLOOR")) == 1
    pool = _one(tmp_db, "SELECT * FROM question_escrow_pool")
    assert pool["is_cov_settled"] == 1


async def test_submit_before_gate_and_unknown_session(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    with pytest.raises(ValueError):
        await submit_stage_answer(card.session_id, "x = 5", db_path=tmp_db)
    with pytest.raises(ValueError):
        await submit_stage_answer("no-such-session", "x = 5", db_path=tmp_db)
    await _drain(card.session_id)


async def test_submit_awaits_inflight_prewarm(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    # 预热仍在途（CONCEPT 分支不阻塞）：立即提交一阶错答 → await 语义调出微母题
    assert sid in in_flight_prewarm_tasks
    resp = await submit_stage_answer(sid, STAGE1_WRONG, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_2_MICRO_SCAFFOLD"
    assert resp.prewarm_status == "READY_SYMPY"
    assert resp.view_payload["micro_scaffold"] is not None
    await asyncio.sleep(0)
    assert sid not in in_flight_prewarm_tasks


async def test_breakpoint_resume_next_day(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(sid)
    resp = await submit_stage_answer(sid, STAGE1_CORRECT, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_3_REDO_ORIGINAL"
    # 挂起次日继续：会话状态持久化，跨日提交照常流转
    _execute(
        tmp_db,
        "UPDATE diagnostic_sessions SET updated_at = datetime('now', '-1 days') "
        "WHERE session_id = ?",
        (sid,),
    )
    persisted = _one(tmp_db, "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (sid,))
    assert persisted["micro_stage"] == "STAGE_3_REDO_ORIGINAL"
    resp = await submit_stage_answer(sid, STAGE3_CORRECT, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_4_DECRUTCH_VARIANT"


async def test_prewarm_relaunch_after_restart(tmp_db: Path, install_diagnosis: Any) -> None:
    card = await _concept_session(tmp_db, install_diagnosis)
    sid = card.session_id
    await confirm_diagnostic_gate(sid, 2, "CONCEPT_BOTTLENECK", db_path=tmp_db)
    await _drain(sid)
    # 模拟进程重启：在途 Task 丢失且预热产物未回填 → 前台请求时重新预热
    in_flight_prewarm_tasks.clear()
    _execute(
        tmp_db,
        "UPDATE diagnostic_sessions SET prewarm_status = 'RUNNING', prewarm_scaffold_uid = NULL, "
        "prewarm_variant_uid = NULL WHERE session_id = ?",
        (sid,),
    )
    resp = await submit_stage_answer(sid, STAGE1_WRONG, db_path=tmp_db)
    assert resp.micro_stage == "STAGE_2_MICRO_SCAFFOLD"
    assert resp.prewarm_status == "READY_SYMPY"
    assert resp.view_payload["micro_scaffold"] is not None
    await _drain(sid)
