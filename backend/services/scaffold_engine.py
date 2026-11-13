"""脚手架引擎：微观训练会话 FSM + 倒置三阶防剧透流转 + 思考窗口投机预热。

唯一权威规格：docs/designs/adaptive_practice_harness.md
§3.2（模块 2）、§3.3 状态机图与第 1 条（模块 3）、CUJ 2/3、Target File #9；
API 签名按 PM 契约 docs/tasks/P2-E_scaffold.md。

核心不变量：
- 微观状态机逐边实现设计 stateDiagram（STAGE_0~4 + COOLDOWN + SESSION_COMPLETED），
  所有状态迁移单事务；非法阶段提交抛 ValueError；
- 思考窗口投机预热：门禁通过后 asyncio.create_task 并发启动「二阶微母题 + 四阶变式」
  两条流水线，登记于进程内 ``in_flight_prewarm_tasks``；前台请求二阶/四阶时若预热
  未完成则 await 在途 Task；预热任何异常收敛为 prewarm_status='FAILED'，绝不击穿前台；
- 本地题库结构化检索优先（SCAFFOLD_PRE / ISOMORPHIC / SHARED_TRAP），未命中走
  内核先行（LLM 内核 JSON → AST 探针 → SymPy 子进程沙盒 → 附 stderr 重试）；
  仍失败降级双盲 LLM 交叉求解（两次独立零上下文，答案一致性核对）；
- 合成题（AI_SYNTHETIC_*）可被检索直接复用，但不得再作为母题衍生（硬拒绝）；
- Elo 结算全部委托 elo_engine（调用方事务内），幂等键防重复发放：
  门禁保底 / 已掌握归档 / 四阶通过冻结托管 + 盲测窗口（TRAP 2~4 天，CONCEPT 3~7 天）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.observability import log_event
from backend.sandbox.sympy_verifier import (
    answers_equivalent,
    normalize_answer,
    run_sympy_in_sandbox,
    validate_script_ast,
    verify_equation_equivalence,
)
from backend.services import elo_engine
from backend.services.difficulty_rubric import calculate_base_elo, clamp_substep_difficulty
from backend.services.llm_gateway import chat_json

__all__ = [
    "SessionStepResponse",
    "confirm_diagnostic_gate",
    "get_session_view",
    "in_flight_prewarm_tasks",
    "prewarm_session_questions",
    "retry_prewarm",
    "submit_stage_answer",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（设计 §3.2 / §3.3 数值；settings 未收录者在此具名常量化）
# ---------------------------------------------------------------------------

# 进程内在途预热 Task 字典（Target File #9：支持同步 await 等待在途生成）
in_flight_prewarm_tasks: dict[str, asyncio.Task[None]] = {}

_CATEGORIES = frozenset({"ALREADY_MASTERED", "TRAP_OR_CALC_ERROR", "CONCEPT_BOTTLENECK"})
# 一阶自评「仍无思路」触发词（结构化自评，直接请求二阶铺垫）
_NO_IDEA_ASSESSMENTS = frozenset({"no_idea", "stuck", "无思路", "仍无思路"})

# 作答框求助语义识别（二阶递归降阶触发，2026-11-13 用户需求）
_NO_IDEA_TEXT_RE = re.compile(
    r"不会|不懂|不知道|没思路|无思路|没有思路|没头绪|请求铺垫|要铺垫|给铺垫|铺垫|"
    r"降阶|再简单|更简单|简单点|求助|help|no\s*idea|stuck",
    re.IGNORECASE,
)


def _is_no_idea_text(answer: str) -> bool:
    """短文本 + 短语匹配识别求助语义，避免误伤正常数学作答（如“不等式”）。"""
    s = (answer or "").strip()
    if not s or len(s) > 24:
        return False
    return bool(_NO_IDEA_TEXT_RE.search(s))

# 本地检索阈值（设计 §3.2 第 2 条「本地题库优先结构化检索」）
_SCAFFOLD_MAX_STEPS = 2  # SCAFFOLD_PRE：子题步数 <= 2
_SCAFFOLD_MIN_ELO_GAP = 200  # SCAFFOLD_PRE：D_orig - D_cand >= 200
_ISOMORPHIC_MAX_ELO_GAP = 150  # ISOMORPHIC：|D_orig - D_cand| <= 150

# 关联边启发式相似度先验（本地复用 / 内核生成；后续 LOG_CALIBRATED 由作答日志校准）
_REUSE_SIMS = (0.5, 0.8, 0.85)  # surface / skeleton / containment
_GEN_SCAFFOLD_SIMS = (0.35, 0.85, 0.9)  # 脱壳微母题：表面相似度低、骨架高度同构
_GEN_VARIANT_SIMS = (0.7, 0.9, 0.9)  # 同构变式：换数换背景、骨架一致

_FALLBACK_BADGE = "⚠️ 未经符号沙盒强校验（支持一键跳过/换题）"
_WRONG_FEEDBACK = "答案未通过代数等价校验，请检查核心关系式后重试。"
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"  # 与 SQLite CURRENT_TIMESTAMP（UTC）一致

_EQUATION_SPLIT_RE = re.compile(r"[;\n]+")
_SYMBOL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_FUNC_TOKENS = frozenset({"sqrt", "sin", "cos", "tan", "log", "ln", "exp", "abs", "pi"})


# ---------------------------------------------------------------------------
# DTO 与内核协议 Schema
# ---------------------------------------------------------------------------


class SessionStepResponse(BaseModel):
    """一次门禁确认 / 阶段提交后的会话快照与当前阶段前端视图。"""

    session_id: str
    question_uid: str
    micro_stage: str
    mastery_state: str
    prewarm_status: str
    view_payload: dict[str, Any]
    explanation_text: str | None = None


class _KernelPayload(BaseModel):
    """内核先行协议（设计 §3.2）：str.format 占位符题干模板 + SymPy 求解内核。"""

    parametric_stem_tpl: str
    sympy_solver_code: str
    constraints_check: str


class _BlindSolverPayload(BaseModel):
    """双盲独立求解器输出。"""

    answer_latex: str
    reasoning_brief: str = ""


@dataclass(frozen=True)
class _PrewarmOutcome:
    """单条预热流水线产物：题目 uid + 是否经 SymPy 强校验。"""

    uid: str
    sympy_verified: bool


@dataclass(frozen=True)
class _GenResult:
    """内核先行 / 双盲降级生成并通过校验的合成题内容。"""

    stem_md: str
    answer_latex: str
    verification_status: Literal["SYMPY_VERIFIED", "DUAL_LLM_FALLBACK"]
    artifact: dict[str, Any]


# ---------------------------------------------------------------------------
# 答案匹配辅助（LaTeX 规范化 / 等价判定共享栈已迁至 sympy_verifier，P4-F）
# ---------------------------------------------------------------------------


def _probe_answer_matches(user_answer: str, step_formula_latex: str) -> bool:
    """一阶盲探判定：作答（可为多式）与卡点步公式的结论段代数等价。"""
    target = normalize_answer(step_formula_latex)
    if not target:
        return False
    user = normalize_answer(user_answer)
    parts = [p.strip() for p in _EQUATION_SPLIT_RE.split(user) if p.strip()]
    return any(answers_equivalent(part, target) for part in parts)


def _extract_allowed_symbols(standard: str) -> list[str]:
    """核心列式验证模式允许使用的符号集（标准式中的自由标识符，去函数名）。"""
    tokens = set(_SYMBOL_TOKEN_RE.findall(normalize_answer(standard)))
    return sorted(tokens - _FUNC_TOKENS)


# ---------------------------------------------------------------------------
# DB 基础工具（所有写入均经 get_db_connection 单事务）
# ---------------------------------------------------------------------------


def _load_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"会话不存在: {session_id}")
    return row


def _load_question(conn: sqlite3.Connection, question_uid: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM questions WHERE question_uid = ?", (question_uid,)
    ).fetchone()
    if row is None:
        raise ValueError(f"题目不存在: {question_uid}")
    return row


def _branch_steps(
    conn: sqlite3.Connection, question_uid: str, branch_id: str
) -> list[sqlite3.Row]:
    """取会话匹配分支的步骤（按 step_index 升序；分支缺失回退 canonical）。"""
    rows = conn.execute(
        "SELECT * FROM question_steps WHERE question_uid = ? AND solution_branch_id = ? "
        "ORDER BY step_index",
        (question_uid, branch_id),
    ).fetchall()
    if not rows and branch_id != "canonical":
        rows = conn.execute(
            "SELECT * FROM question_steps WHERE question_uid = ? "
            "AND solution_branch_id = 'canonical' ORDER BY step_index",
            (question_uid,),
        ).fetchall()
    return rows


def _find_step(steps: list[sqlite3.Row], step_index: int) -> sqlite3.Row:
    for step in steps:
        if int(step["step_index"]) == step_index:
            return step
    raise ValueError(f"步骤序号越界: {step_index}")


def _write_session_fields(db_path: Path | None, session_id: str, **fields: Any) -> None:
    """独立事务更新会话字段（键为内部常量，无注入面），同步刷新 updated_at。"""
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with get_db_connection(db_path) as conn:
        conn.execute(
            f"UPDATE diagnostic_sessions SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE session_id = ?",
            (*fields.values(), session_id),
        )


def _transition(
    db_path: Path | None, session_id: str, *, expect: str, new: str, trigger: str
) -> None:
    """单事务状态迁移：事务内复核当前阶段，非法迁移抛 ValueError 并回滚。

    成功迁移后记录 fsm.transition 行为事件（P5 观测层）。
    """
    with get_db_connection(db_path) as conn:
        current = _load_session(conn, session_id)
        if current["micro_stage"] != expect:
            raise ValueError(
                f"非法状态迁移: 当前 {current['micro_stage']}，期望 {expect} → {new}"
            )
        question_uid = str(current["question_uid"])
        conn.execute(
            "UPDATE diagnostic_sessions SET micro_stage = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE session_id = ?",
            (new, session_id),
        )
    log_event(
        logger,
        "fsm.transition",
        session_id=session_id,
        question_uid=question_uid,
        from_stage=expect,
        to_stage=new,
        trigger=trigger,
    )


def _parse_db_timestamp(value: str) -> datetime:
    """解析 SQLite CURRENT_TIMESTAMP（UTC）；兼容 ISO 格式。"""
    try:
        parsed = datetime.strptime(value, _TS_FORMAT)
    except ValueError:
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _node_id_set(conn: sqlite3.Connection, question_uid: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT knowledge_node_id FROM question_steps "
        "WHERE question_uid = ? AND solution_branch_id = 'canonical'",
        (question_uid,),
    ).fetchall()
    return {str(row["knowledge_node_id"]) for row in rows}


def _trap_tags(question: sqlite3.Row) -> set[str]:
    return set(json.loads(question["trap_tags_json"]))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# 门禁确认（设计 Target File #9 三分支）
# ---------------------------------------------------------------------------


async def confirm_diagnostic_gate(
    session_id: str,
    confirmed_stalled_step: int,
    confirmed_category: str,
    db_path: Path | None = None,
) -> SessionStepResponse:
    """步骤对齐微确认门禁：写确认标志 → 三分支结算/流转 → 启动思考窗口预热。

    - ALREADY_MASTERED：record_mastered_init → MASTERED + SESSION_COMPLETED；
    - CONCEPT_BOTTLENECK：last_valid>=1 时发放草稿子步保底（账本解释条），
      进入 STAGE_1_BLIND_PROBE（仅展示遮罩条件 + probe，不剧透原题结论）；
    - TRAP_OR_CALC_ERROR：直入 STAGE_4_DECRUTCH_VARIANT（推 SHARED_TRAP 变式，
      需立即渲染变式题干，故 await 刚启动的预热 Task）。

    CONCEPT/TRAP 两分支均以 asyncio.create_task 启动 prewarm 并登记
    in_flight_prewarm_tasks；预热异常不击穿前台（收敛为 FAILED）。
    """
    if confirmed_category not in _CATEGORIES:
        raise ValueError(f"非法错误类别: {confirmed_category}")

    explanation: str | None = None
    floor_applied = False
    with get_db_connection(db_path) as conn:
        session = _load_session(conn, session_id)
        if session["micro_stage"] != "STAGE_0_CONFIRM_GATE":
            raise ValueError("门禁已确认或会话已流转，不可重复确认")
        question_uid = str(session["question_uid"])
        steps = _branch_steps(conn, question_uid, str(session["matched_branch_id"]))
        if not 1 <= confirmed_stalled_step <= len(steps):
            raise ValueError(f"卡点步骤序号越界: {confirmed_stalled_step}")
        _find_step(steps, confirmed_stalled_step)

        overridden = int(
            confirmed_stalled_step != int(session["stalled_step_index"])
            or confirmed_category != session["error_category"]
        )

        if confirmed_category == "ALREADY_MASTERED":
            result = elo_engine.record_mastered_init(conn, question_uid, session_id)
            explanation = result.explanation_text
            conn.execute(
                "UPDATE questions SET mastery_state = 'MASTERED', "
                "updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
                (question_uid,),
            )
            new_stage = "SESSION_COMPLETED"
            new_prewarm_status = str(session["prewarm_status"])
        else:
            if (
                confirmed_category == "CONCEPT_BOTTLENECK"
                and int(session["last_valid_step_index"]) >= 1
            ):
                prefix_step = _find_step(steps, int(session["last_valid_step_index"]))
                result = elo_engine.apply_draft_subgoal_floor(
                    conn, question_uid, session_id, int(prefix_step["sub_elo"])
                )
                explanation = result.explanation_text
                floor_applied = result.applied
            new_stage = (
                "STAGE_1_BLIND_PROBE"
                if confirmed_category == "CONCEPT_BOTTLENECK"
                else "STAGE_4_DECRUTCH_VARIANT"
            )
            new_prewarm_status = "RUNNING"

        conn.execute(
            "UPDATE diagnostic_sessions SET user_confirmed_gate = 1, user_overridden = ?, "
            "stalled_step_index = ?, error_category = ?, micro_stage = ?, prewarm_status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
            (
                overridden,
                confirmed_stalled_step,
                confirmed_category,
                new_stage,
                new_prewarm_status,
                session_id,
            ),
        )
        # 同步刷新步骤阻断标志（step_index >= stalled_step_index，全分支）
        conn.execute(
            "UPDATE question_steps SET is_blocked_step = (step_index >= ?) "
            "WHERE question_uid = ?",
            (confirmed_stalled_step, question_uid),
        )

    log_event(
        logger,
        "gate.confirmed",
        session_id=session_id,
        question_uid=question_uid,
        branch=confirmed_category,
        overridden=bool(overridden),
        floor_applied=floor_applied,
        new_stage=new_stage,
    )
    log_event(
        logger,
        "fsm.transition",
        session_id=session_id,
        question_uid=question_uid,
        from_stage="STAGE_0_CONFIRM_GATE",
        to_stage=new_stage,
        trigger="gate_confirmed",
    )

    if confirmed_category != "ALREADY_MASTERED":
        task = _ensure_prewarm_task(session_id, db_path)
        if confirmed_category == "TRAP_OR_CALC_ERROR":
            # 四阶视图需要变式题干：无思考窗口可借，直接 await 在途预热
            await task

    return _build_response(session_id, db_path, explanation_text=explanation)


# ---------------------------------------------------------------------------
# 思考窗口投机预热（设计 §3.2）
# ---------------------------------------------------------------------------


def _ensure_prewarm_task(session_id: str, db_path: Path | None) -> asyncio.Task[None]:
    """登记并返回在途预热 Task；已存在且未完成则复用，否则（重）启动。"""
    existing = in_flight_prewarm_tasks.get(session_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(prewarm_session_questions(session_id, db_path))
    in_flight_prewarm_tasks[session_id] = task

    def _cleanup(finished: asyncio.Task[None]) -> None:
        if in_flight_prewarm_tasks.get(session_id) is finished:
            del in_flight_prewarm_tasks[session_id]

    task.add_done_callback(_cleanup)
    return task


async def prewarm_session_questions(session_id: str, db_path: Path | None = None) -> None:
    """并发预热二阶微母题（SCAFFOLD_PRE）与四阶变式（ISOMORPHIC/SHARED_TRAP）。

    本地检索优先 → 内核先行 + AST/SymPy 沙盒 → 双盲 LLM 降级；落库用独立事务。
    任何异常收敛为 prewarm_status='FAILED'，绝不向调用方（前台）抛出。
    """
    try:
        await _prewarm_impl(session_id, db_path)
    except Exception as exc:
        logger.exception("预热异常 session=%s", session_id)
        log_event(
            logger,
            "prewarm.failed",
            session_id=session_id,
            error_kind=type(exc).__name__,
        )
        try:
            _write_session_fields(db_path, session_id, prewarm_status="FAILED")
        except Exception:
            logger.exception("预热失败状态回写亦失败 session=%s", session_id)


async def get_session_view(session_id: str, db_path: Path | None = None) -> SessionStepResponse:
    """只读刷新当前会话视图（前端轮询 / 断点续传刷新）。

    不触发预热、不流转状态、不结算 Elo；预热在途时原样返回当前 prewarm_status，
    由前端据此决定是否继续轮询。未知会话抛 ValueError（路由层前置 404）。
    """
    with get_db_connection(db_path) as conn:
        _load_session(conn, session_id)
    return _build_response(session_id, db_path)


async def retry_prewarm(session_id: str, db_path: Path | None = None) -> SessionStepResponse:
    """FAILED / 产物缺失时重试预热（前端「重试预热 / 换一题」入口）。

    READY 且双产物齐全 → 幂等无操作返回当前视图；SESSION_COMPLETED → ValueError；
    否则重启预热 Task（已有在途则复用），立即返回不 await，前端轮询 get_session_view。
    """
    with get_db_connection(db_path) as conn:
        session = _load_session(conn, session_id)
    if str(session["micro_stage"]) == "SESSION_COMPLETED":
        raise ValueError("会话已完成，无需重试预热")
    status = str(session["prewarm_status"])
    has_both = (
        session["prewarm_scaffold_uid"] is not None
        and session["prewarm_variant_uid"] is not None
    )
    if status in ("READY_SYMPY", "READY_FALLBACK") and has_both:
        return _build_response(session_id, db_path)
    if status != "RUNNING":
        _write_session_fields(db_path, session_id, prewarm_status="RUNNING")
    _ensure_prewarm_task(session_id, db_path)
    return _build_response(session_id, db_path)


async def _prewarm_impl(session_id: str, db_path: Path | None) -> None:
    started = time.perf_counter()
    with get_db_connection(db_path) as conn:
        session = _load_session(conn, session_id)
        question = _load_question(conn, str(session["question_uid"]))
        steps = _branch_steps(
            conn, str(question["question_uid"]), str(session["matched_branch_id"])
        )
    question_uid = str(question["question_uid"])
    log_event(
        logger, "prewarm.started", session_id=session_id, question_uid=question_uid
    )
    if question["origin_type"] != "USER_CANONICAL":
        # 合成题不得再作为母题衍生（设计 §3.2）：检索命中可直接复用，生成硬拒绝
        logger.warning("拒绝以合成题为母题预热: %s", question["question_uid"])
        log_event(
            logger,
            "prewarm.failed",
            session_id=session_id,
            question_uid=question_uid,
            error_kind="SYNTHETIC_PARENT_REJECTED",
        )
        _write_session_fields(db_path, session_id, prewarm_status="FAILED")
        return
    if not steps:
        raise ValueError(f"题目缺少步骤骨架: {question['question_uid']}")
    stalled_step = _find_step(steps, int(session["stalled_step_index"]))

    _write_session_fields(db_path, session_id, prewarm_status="RUNNING")
    scaffold_out, variant_out = await asyncio.gather(
        _prepare_scaffold(question, stalled_step, db_path, session_id=session_id),
        _prepare_variant(
            question, str(session["error_category"]), db_path, session_id=session_id
        ),
    )

    fields: dict[str, Any] = {}
    if scaffold_out is not None:
        fields["prewarm_scaffold_uid"] = scaffold_out.uid
    if variant_out is not None:
        fields["prewarm_variant_uid"] = variant_out.uid
    if scaffold_out is None or variant_out is None:
        fields["prewarm_status"] = "FAILED"
    elif scaffold_out.sympy_verified and variant_out.sympy_verified:
        fields["prewarm_status"] = "READY_SYMPY"
    else:
        fields["prewarm_status"] = "READY_FALLBACK"
    _write_session_fields(db_path, session_id, **fields)
    log_event(
        logger,
        "prewarm.completed",
        session_id=session_id,
        question_uid=question_uid,
        status=fields["prewarm_status"],
        scaffold_uid=fields.get("prewarm_scaffold_uid"),
        variant_uid=fields.get("prewarm_variant_uid"),
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def _prepare_scaffold(
    question: sqlite3.Row,
    stalled_step: sqlite3.Row,
    db_path: Path | None,
    session_id: str | None = None,
) -> _PrewarmOutcome | None:
    """二阶脱壳微母题：本地 SCAFFOLD_PRE 检索 → 内核先行生成（针对卡点步）。"""
    try:
        parent_uid = str(question["question_uid"])
        with get_db_connection(db_path) as conn:
            local = _search_local_scaffold(conn, question, stalled_step)
            if local is not None:
                _ensure_relation(
                    conn,
                    source_uid=str(local["question_uid"]),
                    target_uid=parent_uid,
                    relation_type="SCAFFOLD_PRE",
                    is_directed=1,
                    target_step_index=int(stalled_step["step_index"]),
                    confidence_source=_reuse_confidence(local),
                    sims=_REUSE_SIMS,
                    trap_overlap=_jaccard(_trap_tags(question), _trap_tags(local)),
                )
                log_event(
                    logger,
                    "prewarm.local_hit",
                    kind="scaffold",
                    session_id=session_id,
                    question_uid=parent_uid,
                    reused_uid=str(local["question_uid"]),
                    verified=_reuse_verified(local),
                )
                return _PrewarmOutcome(str(local["question_uid"]), _reuse_verified(local))
        gen = await _generate_kernel_question(
            question, kind="scaffold", stalled_step=stalled_step, session_id=session_id
        )
        if gen is None:
            return None
        uid = _persist_synthetic(
            db_path,
            question,
            gen,
            origin_type="AI_SYNTHETIC_SCAFFOLD",
            relation_type="SCAFFOLD_PRE",
            stalled_step=stalled_step,
        )
        return _PrewarmOutcome(uid, gen.verification_status == "SYMPY_VERIFIED")
    except Exception as exc:
        logger.exception("二阶微母题预热失败 question=%s", question["question_uid"])
        log_event(
            logger,
            "prewarm.failed",
            kind="scaffold",
            session_id=session_id,
            question_uid=str(question["question_uid"]),
            error_kind=type(exc).__name__,
        )
        return None


async def _prepare_variant(
    question: sqlite3.Row,
    error_category: str,
    db_path: Path | None,
    session_id: str | None = None,
) -> _PrewarmOutcome | None:
    """四阶拆拐杖变式：TRAP → SHARED_TRAP，否则 ISOMORPHIC（整题）。"""
    relation_type = "SHARED_TRAP" if error_category == "TRAP_OR_CALC_ERROR" else "ISOMORPHIC"
    try:
        parent_uid = str(question["question_uid"])
        with get_db_connection(db_path) as conn:
            local = _search_local_variant(conn, question, relation_type)
            if local is not None:
                _ensure_relation(
                    conn,
                    source_uid=str(local["question_uid"]),
                    target_uid=parent_uid,
                    relation_type=relation_type,
                    is_directed=0,
                    target_step_index=None,
                    confidence_source=_reuse_confidence(local),
                    sims=_REUSE_SIMS,
                    trap_overlap=_jaccard(_trap_tags(question), _trap_tags(local)),
                )
                log_event(
                    logger,
                    "prewarm.local_hit",
                    kind="variant",
                    session_id=session_id,
                    question_uid=parent_uid,
                    reused_uid=str(local["question_uid"]),
                    verified=_reuse_verified(local),
                )
                return _PrewarmOutcome(str(local["question_uid"]), _reuse_verified(local))
        gen = await _generate_kernel_question(
            question, kind="variant", stalled_step=None, session_id=session_id
        )
        if gen is None:
            return None
        uid = _persist_synthetic(
            db_path,
            question,
            gen,
            origin_type="AI_SYNTHETIC_VARIANT",
            relation_type=relation_type,
            stalled_step=None,
        )
        return _PrewarmOutcome(uid, gen.verification_status == "SYMPY_VERIFIED")
    except Exception as exc:
        logger.exception("四阶变式预热失败 question=%s", question["question_uid"])
        log_event(
            logger,
            "prewarm.failed",
            kind="variant",
            session_id=session_id,
            question_uid=str(question["question_uid"]),
            error_kind=type(exc).__name__,
        )
        return None


# ---- 本地结构化检索（设计 §3.2 第 2 条；候选题 origin_type 不限） ----


def _search_local_scaffold(
    conn: sqlite3.Connection, question: sqlite3.Row, stalled_step: sqlite3.Row
) -> sqlite3.Row | None:
    """SCAFFOLD_PRE：既有有向边优先；否则同主考点、步数<=2、D_orig-D_cand>=200。"""
    parent_uid = str(question["question_uid"])
    row = conn.execute(
        "SELECT q.* FROM question_relations r "
        "JOIN questions q ON q.question_uid = r.source_question_uid "
        "WHERE r.target_question_uid = ? AND r.relation_type = 'SCAFFOLD_PRE' LIMIT 1",
        (parent_uid,),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT q.* FROM questions q WHERE q.question_uid <> ? AND q.base_elo <= ? "
        "AND (SELECT COUNT(*) FROM question_steps s WHERE s.question_uid = q.question_uid "
        "     AND s.solution_branch_id = 'canonical') BETWEEN 1 AND ? "
        "AND EXISTS (SELECT 1 FROM question_steps s WHERE s.question_uid = q.question_uid "
        "     AND s.knowledge_node_id = ?) "
        "ORDER BY q.base_elo DESC LIMIT 1",
        (
            parent_uid,
            int(question["base_elo"]) - _SCAFFOLD_MIN_ELO_GAP,
            _SCAFFOLD_MAX_STEPS,
            str(stalled_step["knowledge_node_id"]),
        ),
    ).fetchone()


def _search_local_variant(
    conn: sqlite3.Connection, question: sqlite3.Row, relation_type: str
) -> sqlite3.Row | None:
    """ISOMORPHIC：同考点集且 |ΔD|<=150；SHARED_TRAP：trap_tags 交集非空。"""
    parent_uid = str(question["question_uid"])
    for sql in (
        "SELECT q.* FROM question_relations r "
        "JOIN questions q ON q.question_uid = r.source_question_uid "
        "WHERE r.target_question_uid = ? AND r.relation_type = ? LIMIT 1",
        "SELECT q.* FROM question_relations r "
        "JOIN questions q ON q.question_uid = r.target_question_uid "
        "WHERE r.source_question_uid = ? AND r.relation_type = ? LIMIT 1",
    ):
        row = conn.execute(sql, (parent_uid, relation_type)).fetchone()
        if row is not None:
            return row

    parent_elo = int(question["base_elo"])
    best: sqlite3.Row | None = None
    best_gap = float("inf")
    if relation_type == "ISOMORPHIC":
        parent_nodes = _node_id_set(conn, parent_uid)
        candidates = conn.execute(
            "SELECT * FROM questions WHERE question_uid <> ? AND ABS(base_elo - ?) <= ?",
            (parent_uid, parent_elo, _ISOMORPHIC_MAX_ELO_GAP),
        ).fetchall()
        for cand in candidates:
            if _node_id_set(conn, str(cand["question_uid"])) != parent_nodes:
                continue
            gap = abs(int(cand["base_elo"]) - parent_elo)
            if gap < best_gap:
                best, best_gap = cand, gap
        return best
    # SHARED_TRAP
    parent_tags = _trap_tags(question)
    if not parent_tags:
        return None
    candidates = conn.execute(
        "SELECT * FROM questions WHERE question_uid <> ?", (parent_uid,)
    ).fetchall()
    for cand in candidates:
        if not parent_tags & _trap_tags(cand):
            continue
        gap = abs(int(cand["base_elo"]) - parent_elo)
        if gap < best_gap:
            best, best_gap = cand, gap
    return best


def _reuse_verified(candidate: sqlite3.Row) -> bool:
    """本地复用题是否视同强校验：人工主题或 SymPy 已验合成题。"""
    if candidate["origin_type"] == "USER_CANONICAL":
        return True
    return candidate["verification_status"] == "SYMPY_VERIFIED"


def _reuse_confidence(candidate: sqlite3.Row) -> str:
    if candidate["origin_type"] == "USER_CANONICAL":
        return "HUMAN_VERIFIED"
    if candidate["verification_status"] == "SYMPY_VERIFIED":
        return "SYMPY_KERNEL_GEN"
    return "LLM_PRIOR"


def _ensure_relation(
    conn: sqlite3.Connection,
    *,
    source_uid: str,
    target_uid: str,
    relation_type: str,
    is_directed: int,
    target_step_index: int | None,
    confidence_source: str,
    sims: tuple[float, float, float],
    trap_overlap: float,
) -> None:
    """幂等写入五边拓扑关联（SCAFFOLD_PRE：source=子题，target=原题，有向）。"""
    conn.execute(
        "INSERT OR IGNORE INTO question_relations ("
        " source_question_uid, target_question_uid, relation_type, is_directed,"
        " target_step_index, surface_sim, skeleton_sim, containment_score, trap_overlap,"
        " confidence_source"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_uid,
            target_uid,
            relation_type,
            is_directed,
            target_step_index,
            sims[0],
            sims[1],
            sims[2],
            trap_overlap,
            confidence_source,
        ),
    )


# ---- 内核先行生成 + AST/SymPy 沙盒 + 双盲降级 ----

_KERNEL_SYSTEM_PROMPT = (
    "你是数理题参数化内核生成器。仅输出 JSON 对象，含三个字段："
    "parametric_stem_tpl（题干模板，用 str.format 占位符如 {a}，模板内不得出现其他花括号）、"
    "sympy_solver_code（必须定义 solve_kernel()，返回 "
    '{"params": dict[str, str|int|float], "answer_latex": str}）、'
    "constraints_check（参数约束说明）。"
    "脚本仅允许 import sympy / math / fractions，必须包含真实符号求解调用"
    "（solve/solveset/Eq/simplify 等），禁止直接返回常量字面量。"
)

_BLIND_SYSTEM_PROMPT = (
    "你是独立零上下文求解器。仅根据题目本身求解，不参考任何外部结论。"
    '仅输出 JSON 对象：{"answer_latex": 最终答案, "reasoning_brief": 一句话思路}。'
)


def _kernel_messages(
    question: sqlite3.Row,
    kind: Literal["scaffold", "variant", "descent"],
    stalled_step: sqlite3.Row | None,
) -> list[dict[str, Any]]:
    if kind == "scaffold":
        assert stalled_step is not None
        user_content = (
            f"原题题干：\n{question['stem_md']}\n\n"
            f"卡点步骤（第 {stalled_step['step_index']} 步）：{stalled_step['step_goal_desc']}\n"
            f"核心关系式：{stalled_step['step_formula_latex']}\n\n"
            "请生成一道剥离原题背景、1～2 步可解的脱壳微母题内核，"
            "针对该卡点步的纯代数/几何关系，难度显著低于原题。"
        )
    elif kind == "descent":
        user_content = (
            f"当前铺垫题题干：\n{question['stem_md']}\n\n"
            f"标准答案：{question['standard_answer_latex']}\n\n"
            "学生仍然不会。请生成一道严格更简单的降阶铺垫题内核：保留同一原子技能，"
            "压缩到 1 步直达，数值更小更整，去除一切多余情境与干扰条件，"
            "难度必须显著低于当前铺垫题。"
        )
    else:
        user_content = (
            f"原题题干：\n{question['stem_md']}\n\n"
            f"标准答案：{question['standard_answer_latex']}\n\n"
            "请生成一道整题同构变式内核（改变数值与表面条件，保持考点结构与陷阱算子），"
            "用于当场拆拐杖验证。"
        )
    return [
        {"role": "system", "content": _KERNEL_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _render_stem(tpl: str, params: dict[str, str | int | float] | None) -> str | None:
    """str.format 占位符协议回填；无法实例化（缺参数/模板非法）返回 None。"""
    try:
        return tpl.format(**(params or {}))
    except (KeyError, IndexError, ValueError):
        return None


async def _generate_kernel_question(
    question: sqlite3.Row,
    *,
    kind: Literal["scaffold", "variant", "descent"],
    stalled_step: sqlite3.Row | None,
    session_id: str | None = None,
) -> _GenResult | None:
    """内核先行流水线：LLM 内核 → AST 探针 → SymPy 沙盒 → 重试 → 双盲降级。

    kind='descent' 为用户驱动的递归降阶（2026-11-13 需求）：允许以合成铺垫题为母题，
    由深度上限 + 单调性硬锁约束；自动预热路径仍禁止合成题衍生（设计 §3.2）。
    """
    if kind != "descent" and question["origin_type"] != "USER_CANONICAL":
        raise ValueError("合成题不得作为母题再次衍生（设计 §3.2）")
    purpose = {
        "scaffold": "scaffold_kernel",
        "variant": "variant_kernel",
        "descent": "descent_kernel",
    }[kind]
    messages = _kernel_messages(question, kind, stalled_step)
    last_error = "unknown"
    last_params: dict[str, str | int | float] | None = None
    tpl = ""
    code = ""

    for attempt in range(settings.SYMPY_MAX_RETRIES + 1):
        attempt_start = time.perf_counter()
        kernel = cast(
            _KernelPayload,
            await chat_json(
                messages, _KernelPayload, model=settings.SOLVER_LLM_MODEL_NAME, purpose=purpose
            ),
        )
        tpl = kernel.parametric_stem_tpl
        code = kernel.sympy_solver_code
        probe = validate_script_ast(code)
        sandbox_ok: bool | None = None
        error_kind: str | None = None
        if not probe.ok:
            last_error = f"AST 探针拒绝: {probe.reason}"
            error_kind = probe.reason
        else:
            sandbox = await asyncio.to_thread(run_sympy_in_sandbox, code)
            sandbox_ok = sandbox.ok
            if sandbox.params is not None:
                last_params = sandbox.params
            if sandbox.ok and sandbox.answer_latex:
                stem = _render_stem(tpl, sandbox.params)
                if stem is not None:
                    artifact: dict[str, Any] = {
                        "method": "SYMPY_KERNEL",
                        "sympy_solver_code": code,
                        "params": sandbox.params,
                        "answer_latex": sandbox.answer_latex,
                        "constraints_check": kernel.constraints_check,
                        "attempts": attempt + 1,
                        "sympy_version": _pkg_version("sympy"),
                    }
                    log_event(
                        logger,
                        "prewarm.kernel_attempt",
                        kind=kind,
                        session_id=session_id,
                        question_uid=str(question["question_uid"]),
                        attempt=attempt + 1,
                        ast_ok=True,
                        sandbox_ok=True,
                        error_kind=None,
                        duration_ms=round((time.perf_counter() - attempt_start) * 1000, 2),
                    )
                    return _GenResult(stem, sandbox.answer_latex, "SYMPY_VERIFIED", artifact)
                last_error = "题干模板参数回填失败"
                error_kind = "RENDER_FAILED"
            else:
                last_error = f"{sandbox.error_kind}: {sandbox.stderr_tail}"
                error_kind = sandbox.error_kind
        log_event(
            logger,
            "prewarm.kernel_attempt",
            kind=kind,
            session_id=session_id,
            question_uid=str(question["question_uid"]),
            attempt=attempt + 1,
            ast_ok=probe.ok,
            sandbox_ok=sandbox_ok,
            error_kind=error_kind,
            duration_ms=round((time.perf_counter() - attempt_start) * 1000, 2),
        )
        # 附 stderr/拒绝原因重试（<= settings.SYMPY_MAX_RETRIES 次）
        messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    f"上一版内核未通过校验（{last_error}）。"
                    "请重新输出修复后的完整内核 JSON，不要输出任何解释。"
                ),
            },
        ]

    # 优雅降级：双盲 LLM 交叉求解（非纯代数题 / 沙盒重试耗尽）
    stem = _render_stem(tpl, last_params)
    if stem is None:
        logger.warning("双盲降级失败：题干模板无法实例化 question=%s", question["question_uid"])
        return None
    consensus = await _dual_blind_solve(stem)
    log_event(
        logger,
        "prewarm.dual_blind",
        kind=kind,
        session_id=session_id,
        question_uid=str(question["question_uid"]),
        consensus=consensus is not None,
    )
    if consensus is None:
        logger.warning("双盲答案不一致 question=%s", question["question_uid"])
        return None
    answer, blind_answers = consensus
    artifact = {
        "method": "DUAL_LLM",
        "blind_answers": blind_answers,
        "answer_latex": answer,
        "sympy_solver_code": code,
        "last_error": last_error,
        "sympy_version": _pkg_version("sympy"),
    }
    return _GenResult(stem, answer, "DUAL_LLM_FALLBACK", artifact)


async def _dual_blind_solve(stem: str) -> tuple[str, list[str]] | None:
    """两次独立零上下文盲做，核对 answer_latex 一致性（等价引擎或规范化比对）。"""

    def _fresh_messages() -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": _BLIND_SYSTEM_PROMPT},
            {"role": "user", "content": f"求解下题，给出最终答案：\n{stem}"},
        ]

    first = cast(
        _BlindSolverPayload,
        await chat_json(
            _fresh_messages(),
            _BlindSolverPayload,
            model=settings.SOLVER_LLM_MODEL_NAME,
            purpose="solver_blind",
        ),
    )
    second = cast(
        _BlindSolverPayload,
        await chat_json(
            _fresh_messages(),
            _BlindSolverPayload,
            model=settings.SOLVER_LLM_MODEL_NAME,
            purpose="solver_blind",
        ),
    )
    a1, a2 = first.answer_latex, second.answer_latex
    n1, n2 = normalize_answer(a1), normalize_answer(a2)
    if n1 and n1 == n2:
        return a1, [a1, a2]
    if await asyncio.to_thread(verify_equation_equivalence, n1, n2):
        return a1, [a1, a2]
    return None


def _persist_synthetic(
    db_path: Path | None,
    parent: sqlite3.Row,
    gen: _GenResult,
    *,
    origin_type: Literal["AI_SYNTHETIC_SCAFFOLD", "AI_SYNTHETIC_VARIANT"],
    relation_type: str,
    stalled_step: sqlite3.Row | None,
) -> str:
    """合成题 + 单步骨架 + 五边拓扑关联，单事务原子落库（独立连接）。"""
    parent_uid = str(parent["question_uid"])
    if origin_type == "AI_SYNTHETIC_SCAFFOLD":
        assert stalled_step is not None
        dims = (
            int(stalled_step["c_sub"]),
            int(stalled_step["s_sub"]),
            int(stalled_step["t_sub"]),
            int(stalled_step["l_sub"]),
        )
        node_id = str(stalled_step["knowledge_node_id"])
        trap_tags: list[str] = []
        sims = _GEN_SCAFFOLD_SIMS
        goal_desc = "脱壳微母题：直接求解卡点步的核心代数/几何关系"
    else:
        dims = (
            int(parent["c_score"]),
            int(parent["s_score"]),
            int(parent["t_score"]),
            int(parent["l_score"]),
        )
        with get_db_connection(db_path) as conn:
            parent_steps = _branch_steps(conn, parent_uid, "canonical")
        node_id = str(parent_steps[0]["knowledge_node_id"])
        trap_tags = sorted(_trap_tags(parent))
        sims = _GEN_VARIANT_SIMS
        goal_desc = "同构变式：核心列式与求解（当场拆拐杖验证）"

    rubric = calculate_base_elo(*dims)
    base_elo = int(rubric["base_elo"])
    sub_cstl, sub_elo = clamp_substep_difficulty(dims, base_elo, dims)
    uid = uuid.uuid4().hex
    confidence = "SYMPY_KERNEL_GEN" if gen.verification_status == "SYMPY_VERIFIED" else "LLM_PRIOR"
    is_scaffold = origin_type == "AI_SYNTHETIC_SCAFFOLD"

    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO questions ("
            " question_uid, origin_type, parent_question_uid, target_grade_and_subject,"
            " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
            " base_elo, tier, trap_tags_json, primary_l1_module, mastery_state,"
            " verification_status, verification_artifact_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
            (
                uid,
                origin_type,
                parent_uid,
                parent["target_grade_and_subject"],
                gen.stem_md,
                hashlib.sha256(gen.stem_md.encode("utf-8")).hexdigest(),
                gen.answer_latex,
                *dims,
                base_elo,
                str(rubric["tier"]),
                json.dumps(trap_tags, ensure_ascii=False),
                parent["primary_l1_module"],
                gen.verification_status,
                json.dumps(gen.artifact, ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO question_steps ("
            " step_id, question_uid, solution_branch_id, step_index, step_goal_desc,"
            " step_formula_latex, knowledge_node_id, masked_conditions_json, probe_question_md,"
            " is_blocked_step, reused_existing_node, c_sub, s_sub, t_sub, l_sub, sub_elo"
            ") VALUES (?, ?, 'canonical', 1, ?, ?, ?, '[]', ?, 0, 1, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                uid,
                goal_desc,
                gen.answer_latex,
                node_id,
                gen.stem_md,
                *sub_cstl,
                sub_elo,
            ),
        )
        _ensure_relation(
            conn,
            source_uid=uid,
            target_uid=parent_uid,
            relation_type=relation_type,
            is_directed=1 if is_scaffold else 0,
            target_step_index=int(stalled_step["step_index"]) if stalled_step is not None else None,
            confidence_source=confidence,
            sims=sims,
            trap_overlap=_jaccard(set(trap_tags), _trap_tags(parent)),
        )
    return uid


# ---------------------------------------------------------------------------
# 阶段提交（设计 §3.3 stateDiagram 逐边实现）
# ---------------------------------------------------------------------------


async def submit_stage_answer(
    session_id: str,
    user_answer: str,
    self_assessment: str | None = None,
    db_path: Path | None = None,
) -> SessionStepResponse:
    """提交当前微观阶段作答，驱动状态机流转（所有迁移单事务）。

    非法阶段（未过门禁 / 已完成 / 冷却未满）抛 ValueError；
    请求二阶/四阶而预热未完成时 await 在途 Task（断点续传后无在途 Task 则重新预热）。
    """
    with get_db_connection(db_path) as conn:
        session = _load_session(conn, session_id)
    stage = str(session["micro_stage"])
    if stage == "STAGE_0_CONFIRM_GATE":
        raise ValueError("请先通过步骤对齐微确认门禁（confirm_diagnostic_gate）")
    if stage == "SESSION_COMPLETED":
        raise ValueError("会话已完成，不可重复提交")
    if stage == "STAGE_1_BLIND_PROBE":
        return await _submit_stage1(session, user_answer, self_assessment, db_path)
    if stage == "STAGE_2_MICRO_SCAFFOLD":
        return await _submit_stage2(session, user_answer, self_assessment, db_path)
    if stage == "STAGE_3_REDO_ORIGINAL":
        return await _submit_stage3(session, user_answer, db_path)
    if stage == "STAGE_4_DECRUTCH_VARIANT":
        return await _submit_stage4(session, user_answer, db_path)
    if stage == "STAGE_COOLDOWN_DEFERRED":
        return await _submit_cooldown(session, db_path)
    raise ValueError(f"非法微观阶段: {stage}")


async def _ensure_prewarm_question(
    session_id: str, kind: Literal["scaffold", "variant"], db_path: Path | None
) -> sqlite3.Row:
    """取预热产物题目；未完成则 await 在途 Task（含挂起恢复后的重新预热）。"""
    column = "prewarm_scaffold_uid" if kind == "scaffold" else "prewarm_variant_uid"
    with get_db_connection(db_path) as conn:
        session = _load_session(conn, session_id)
    uid = session[column]
    if uid is None:
        if session["prewarm_status"] == "FAILED":
            raise ValueError("预热失败（FAILED），无法调出该阶段题目；可重新发起预热")
        task = _ensure_prewarm_task(session_id, db_path)
        await task
        with get_db_connection(db_path) as conn:
            session = _load_session(conn, session_id)
        uid = session[column]
        if uid is None:
            raise ValueError("预热未产出该阶段题目，无法流转")
    with get_db_connection(db_path) as conn:
        return _load_question(conn, str(uid))


async def _submit_stage1(
    session: sqlite3.Row, user_answer: str, self_assessment: str | None, db_path: Path | None
) -> SessionStepResponse:
    """一阶盲探：答对 → 直跳三阶；错/自评无思路 → 二阶（await 在途预热）。"""
    session_id = str(session["session_id"])
    with get_db_connection(db_path) as conn:
        question = _load_question(conn, str(session["question_uid"]))
        steps = _branch_steps(
            conn, str(question["question_uid"]), str(session["matched_branch_id"])
        )
    stalled_step = _find_step(steps, int(session["stalled_step_index"]))

    no_idea = (self_assessment or "").strip().lower() in _NO_IDEA_ASSESSMENTS
    correct = False
    if not no_idea and user_answer.strip():
        correct = await asyncio.to_thread(
            _probe_answer_matches, user_answer, str(stalled_step["step_formula_latex"])
        )
    log_event(
        logger,
        "fsm.submission",
        session_id=session_id,
        question_uid=str(session["question_uid"]),
        stage="STAGE_1_BLIND_PROBE",
        correct=correct,
        no_idea=no_idea,
    )
    if correct:
        _transition(
            db_path, session_id, expect="STAGE_1_BLIND_PROBE", new="STAGE_3_REDO_ORIGINAL",
            trigger="stage1_correct",
        )
        return _build_response(session_id, db_path)
    await _ensure_prewarm_question(session_id, "scaffold", db_path)
    _transition(
        db_path, session_id, expect="STAGE_1_BLIND_PROBE", new="STAGE_2_MICRO_SCAFFOLD",
        trigger="stage1_stuck",
    )
    return _build_response(session_id, db_path)


def _scaffold_parent_uid(conn: sqlite3.Connection, uid: str) -> str | None:
    """SCAFFOLD_PRE 有向边（source=子题 → target=母题）的直接上层。"""
    row = conn.execute(
        "SELECT target_question_uid FROM question_relations "
        "WHERE source_question_uid = ? AND relation_type = 'SCAFFOLD_PRE' LIMIT 1",
        (uid,),
    ).fetchone()
    return str(row["target_question_uid"]) if row else None


def _scaffold_depth(conn: sqlite3.Connection, scaffold_uid: str, original_uid: str) -> int:
    """沿 SCAFFOLD_PRE 链回溯的降阶层数（直接铺垫=0，防环；回到原题的边不计入）。"""
    depth = 0
    cur = scaffold_uid
    seen = {cur}
    while cur != original_uid and depth <= settings.SCAFFOLD_DESCENT_MAX_DEPTH + 1:
        nxt = _scaffold_parent_uid(conn, cur)
        if nxt is None or nxt in seen or nxt == original_uid:
            break
        seen.add(nxt)
        cur = nxt
        depth += 1
    return depth


def _worked_example_view(current: sqlite3.Row) -> dict[str, Any]:
    """降阶触底时的标准答案演示卡（最基础原子题，不剧透原题结论）。"""
    return {
        "stem_md": str(current["stem_md"]),
        "answer_latex": str(current["standard_answer_latex"]),
        "hint": "这已是最基础的原子题。看懂标准答案后重新作答，答对即可逐层返回原题。",
    }


async def _descend_scaffold(
    session: sqlite3.Row, current: sqlite3.Row, db_path: Path | None
) -> SessionStepResponse:
    """用户驱动的递归降阶：生成严格更简单的铺垫题，直到最基础原子题。

    终止条件：深度上限 / C=S=1 原子题 / 难度触底 800 → 展示标准答案演示卡。
    难度单调递减由 _persist_synthetic 内的 clamp_substep_difficulty 保证
    （每层 C/S 至少降 1，elo 至少降 150，直至 800 下界）。
    """
    session_id = str(session["session_id"])
    original_uid = str(session["question_uid"])
    current_uid = str(current["question_uid"])
    with get_db_connection(db_path) as conn:
        depth = _scaffold_depth(conn, current_uid, original_uid)
        steps = _branch_steps(conn, current_uid, "canonical")
    log_event(
        logger,
        "descent.triggered",
        session_id=session_id,
        question_uid=original_uid,
        scaffold_uid=current_uid,
        depth=depth,
        elo_before=int(current["base_elo"]),
    )
    atomic = int(current["c_score"]) <= 1 and int(current["s_score"]) <= 1
    if (
        depth >= settings.SCAFFOLD_DESCENT_MAX_DEPTH
        or atomic
        or int(current["base_elo"]) <= settings.BASE_ELO_MIN
        or not steps
    ):
        if depth >= settings.SCAFFOLD_DESCENT_MAX_DEPTH:
            reason = "depth_cap"
        elif int(current["base_elo"]) <= settings.BASE_ELO_MIN:
            reason = "elo_floor"
        else:
            reason = "atomic"  # C=S=1 原子题或缺步骤骨架，不再降阶
        log_event(
            logger,
            "descent.floor_reveal",
            session_id=session_id,
            question_uid=original_uid,
            scaffold_uid=current_uid,
            depth=depth,
            elo_before=int(current["base_elo"]),
            reason=reason,
        )
        return _build_response(
            session_id,
            db_path,
            notice="🪜 已降至最基础的原子题，不再继续降阶；看懂标准答案后重新作答即可逐层返回。",
            worked_example=_worked_example_view(current),
        )
    gen = await _generate_kernel_question(
        current, kind="descent", stalled_step=None, session_id=session_id
    )
    if gen is None:
        log_event(
            logger,
            "descent.floor_reveal",
            session_id=session_id,
            question_uid=original_uid,
            scaffold_uid=current_uid,
            depth=depth,
            elo_before=int(current["base_elo"]),
            reason="gen_failed",
        )
        return _build_response(
            session_id,
            db_path,
            notice="降阶题生成未通过校验；已展示当前铺垫题的标准答案供学习。",
            worked_example=_worked_example_view(current),
        )
    new_uid = _persist_synthetic(
        db_path,
        current,
        gen,
        origin_type="AI_SYNTHETIC_SCAFFOLD",
        relation_type="SCAFFOLD_PRE",
        stalled_step=steps[0],
    )
    _write_session_fields(db_path, session_id, prewarm_scaffold_uid=new_uid)
    with get_db_connection(db_path) as conn:
        new_q = _load_question(conn, new_uid)
    log_event(
        logger,
        "descent.generated",
        session_id=session_id,
        question_uid=original_uid,
        scaffold_uid=new_uid,
        depth=depth + 1,
        elo_before=int(current["base_elo"]),
        elo_after=int(new_q["base_elo"]),
    )
    return _build_response(
        session_id,
        db_path,
        notice=(
            f"🪜 已降阶至第 {depth + 1} 层铺垫（难度 {new_q['base_elo']}）；"
            "逐层答对可返回原题。"
        ),
    )


async def _submit_stage2(
    session: sqlite3.Row, user_answer: str, self_assessment: str | None, db_path: Path | None
) -> SessionStepResponse:
    """二阶脱壳微母题：等价 → 升层/三阶；不会/请求铺垫 → 递归降阶；错 → 停留反馈。"""
    session_id = str(session["session_id"])
    scaffold = await _ensure_prewarm_question(session_id, "scaffold", db_path)
    no_idea = (
        (self_assessment or "").strip().lower() in _NO_IDEA_ASSESSMENTS
        or _is_no_idea_text(user_answer)
    )
    if no_idea:
        log_event(
            logger,
            "fsm.submission",
            session_id=session_id,
            question_uid=str(session["question_uid"]),
            stage="STAGE_2_MICRO_SCAFFOLD",
            correct=False,
            no_idea=True,
        )
        return await _descend_scaffold(session, scaffold, db_path)
    correct = await asyncio.to_thread(
        answers_equivalent, user_answer, str(scaffold["standard_answer_latex"])
    )
    log_event(
        logger,
        "fsm.submission",
        session_id=session_id,
        question_uid=str(session["question_uid"]),
        stage="STAGE_2_MICRO_SCAFFOLD",
        correct=correct,
        no_idea=False,
    )
    if correct:
        with get_db_connection(db_path) as conn:
            parent_uid = _scaffold_parent_uid(conn, str(scaffold["question_uid"]))
            parent = _load_question(conn, parent_uid) if parent_uid else None
            ascent_depth = (
                _scaffold_depth(
                    conn, str(parent["question_uid"]), str(session["question_uid"])
                )
                if parent is not None
                else 0
            )
        if parent is not None and parent["origin_type"] != "USER_CANONICAL":
            # 降阶梯 ascent：答对当前层 → 回到上一层铺垫题（仍为合成题）
            _write_session_fields(db_path, session_id, prewarm_scaffold_uid=parent_uid)
            log_event(
                logger,
                "ascent.step",
                session_id=session_id,
                question_uid=str(session["question_uid"]),
                from_uid=str(scaffold["question_uid"]),
                to_uid=str(parent["question_uid"]),
                depth=ascent_depth,
                elo_before=int(scaffold["base_elo"]),
                elo_after=int(parent["base_elo"]),
            )
            return _build_response(
                session_id, db_path, notice="✅ 通过！已回到上一层铺垫题，继续作答。"
            )
        _transition(
            db_path, session_id, expect="STAGE_2_MICRO_SCAFFOLD", new="STAGE_3_REDO_ORIGINAL",
            trigger="stage2_passed",
        )
        return _build_response(session_id, db_path)
    return _build_response(session_id, db_path, feedback=_WRONG_FEEDBACK)


async def _submit_stage3(
    session: sqlite3.Row, user_answer: str, db_path: Path | None
) -> SessionStepResponse:
    """三阶无提示回原题：与原题标准答案等价 → 四阶；错 → 停留并反馈。"""
    session_id = str(session["session_id"])
    with get_db_connection(db_path) as conn:
        question = _load_question(conn, str(session["question_uid"]))
    correct = await asyncio.to_thread(
        answers_equivalent, user_answer, str(question["standard_answer_latex"])
    )
    log_event(
        logger,
        "fsm.submission",
        session_id=session_id,
        question_uid=str(session["question_uid"]),
        stage="STAGE_3_REDO_ORIGINAL",
        correct=correct,
        no_idea=False,
    )
    if correct:
        await _ensure_prewarm_question(session_id, "variant", db_path)
        _transition(
            db_path, session_id, expect="STAGE_3_REDO_ORIGINAL", new="STAGE_4_DECRUTCH_VARIANT",
            trigger="stage3_passed",
        )
        return _build_response(session_id, db_path)
    return _build_response(session_id, db_path, feedback=_WRONG_FEEDBACK)


def _stage4_standard(
    question: sqlite3.Row, variant: sqlite3.Row, variant_steps: list[sqlite3.Row]
) -> tuple[str, str]:
    """四阶校验标准与模式：L>=2 核心列式（equation_skeleton）；L=1 最终答案。"""
    if int(question["l_score"]) >= 2:
        standard = (
            str(variant_steps[0]["step_formula_latex"])
            if variant_steps
            else str(variant["standard_answer_latex"])
        )
        return standard, "equation_skeleton"
    return str(variant["standard_answer_latex"]), "final_answer"


async def _submit_stage4(
    session: sqlite3.Row, user_answer: str, db_path: Path | None
) -> SessionStepResponse:
    """四阶当场拆拐杖：通过 → 冻结托管 + PENDING_RETEST + 盲测窗口；错 → 冷却。"""
    session_id = str(session["session_id"])
    with get_db_connection(db_path) as conn:
        question = _load_question(conn, str(session["question_uid"]))
    variant = await _ensure_prewarm_question(session_id, "variant", db_path)
    with get_db_connection(db_path) as conn:
        variant_steps = _branch_steps(conn, str(variant["question_uid"]), "canonical")
    standard, mode = _stage4_standard(question, variant, variant_steps)
    correct = await asyncio.to_thread(answers_equivalent, user_answer, standard)
    log_event(
        logger,
        "fsm.submission",
        session_id=session_id,
        question_uid=str(session["question_uid"]),
        stage="STAGE_4_DECRUTCH_VARIANT",
        correct=correct,
        no_idea=False,
    )

    if correct:
        with get_db_connection(db_path) as conn:
            current = _load_session(conn, session_id)
            if current["micro_stage"] != "STAGE_4_DECRUTCH_VARIANT":
                raise ValueError(f"非法状态迁移: 当前 {current['micro_stage']}")
            result = elo_engine.freeze_decrutch_escrow(
                conn, str(question["question_uid"]), session_id
            )
            window = (
                settings.BLIND_WINDOW_TRAP_DAYS
                if current["error_category"] == "TRAP_OR_CALC_ERROR"
                else settings.BLIND_WINDOW_CONCEPT_DAYS
            )
            now = datetime.now(UTC)
            unlock_at = (now + timedelta(days=window[0])).strftime(_TS_FORMAT)
            due_at = (now + timedelta(days=window[1])).strftime(_TS_FORMAT)
            conn.execute(
                "UPDATE questions SET mastery_state = 'PENDING_RETEST', blind_test_unlock_at = ?, "
                "blind_test_due_at = ?, updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
                (unlock_at, due_at, question["question_uid"]),
            )
            conn.execute(
                "UPDATE diagnostic_sessions SET micro_stage = 'SESSION_COMPLETED', "
                "updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                (session_id,),
            )
        log_event(
            logger,
            "fsm.transition",
            session_id=session_id,
            question_uid=str(question["question_uid"]),
            from_stage="STAGE_4_DECRUTCH_VARIANT",
            to_stage="SESSION_COMPLETED",
            trigger="stage4_passed",
        )
        return _build_response(session_id, db_path, explanation_text=result.explanation_text)

    _transition(
        db_path, session_id, expect="STAGE_4_DECRUTCH_VARIANT", new="STAGE_COOLDOWN_DEFERRED",
        trigger="stage4_failed",
    )
    response = _build_response(session_id, db_path, feedback=_WRONG_FEEDBACK)
    card = response.view_payload.get("skeleton_comparison_card")
    if isinstance(card, dict):
        card["user_answer"] = user_answer
    return response


async def _submit_cooldown(session: sqlite3.Row, db_path: Path | None) -> SessionStepResponse:
    """冷却期满（updated_at 次日）再提交 → 回四阶重新作答；未满抛 ValueError。"""
    session_id = str(session["session_id"])
    entered_at = _parse_db_timestamp(str(session["updated_at"]))
    cooldown_end_date = (entered_at + timedelta(days=1)).date()
    if datetime.now(UTC).date() < cooldown_end_date:
        raise ValueError("冷却期未满（次日重测），暂不可提交")
    _transition(
        db_path, session_id, expect="STAGE_COOLDOWN_DEFERRED", new="STAGE_4_DECRUTCH_VARIANT",
        trigger="cooldown_elapsed",
    )
    return _build_response(session_id, db_path, notice="冷却期满，请重新提交变式作答。")


# ---------------------------------------------------------------------------
# 视图组装（防剧透：一阶仅遮罩条件 + probe；三阶才展开原题题干）
# ---------------------------------------------------------------------------


def _question_view(
    conn: sqlite3.Connection, uid: str | None, key: str
) -> dict[str, Any]:
    if uid is None:
        return {key: None, "prewarm_failed": True, "verification_badge": None}
    row = _load_question(conn, uid)
    badge = _FALLBACK_BADGE if row["verification_status"] == "DUAL_LLM_FALLBACK" else None
    return {
        key: {
            "question_uid": row["question_uid"],
            "stem_md": row["stem_md"],
            "verification_status": row["verification_status"],
        },
        "verification_badge": badge,
    }


def _stage4_view(
    conn: sqlite3.Connection, session: sqlite3.Row, question: sqlite3.Row
) -> dict[str, Any]:
    view = _question_view(conn, session["prewarm_variant_uid"], "variant")
    mode = "equation_skeleton" if int(question["l_score"]) >= 2 else "final_answer"
    view["verification_mode"] = mode
    view["allowed_symbols"] = []
    variant_uid = session["prewarm_variant_uid"]
    if variant_uid is not None and mode == "equation_skeleton":
        variant = _load_question(conn, str(variant_uid))
        variant_steps = _branch_steps(conn, str(variant_uid), "canonical")
        standard, _ = _stage4_standard(question, variant, variant_steps)
        view["allowed_symbols"] = _extract_allowed_symbols(standard)
    return view


def _comparison_card(
    conn: sqlite3.Connection, session: sqlite3.Row, question: sqlite3.Row
) -> dict[str, Any]:
    """骨架对比卡（四阶失败 → 冷却）：标准骨架 + 模式 + 次日重测提示。"""
    variant_uid = session["prewarm_variant_uid"]
    if variant_uid is None:
        return {"prewarm_failed": True}
    variant = _load_question(conn, str(variant_uid))
    variant_steps = _branch_steps(conn, str(variant_uid), "canonical")
    standard, mode = _stage4_standard(question, variant, variant_steps)
    return {
        "variant_stem_md": variant["stem_md"],
        "standard_skeleton": standard,
        "verification_mode": mode,
        "allowed_symbols": (
            _extract_allowed_symbols(standard) if mode == "equation_skeleton" else []
        ),
        "retry_policy": "次日冷却期满后可重新作答（STAGE_COOLDOWN_DEFERRED → STAGE_4）。",
    }


def _build_response(
    session_id: str,
    db_path: Path | None,
    *,
    explanation_text: str | None = None,
    feedback: str | None = None,
    notice: str | None = None,
    worked_example: dict[str, Any] | None = None,
) -> SessionStepResponse:
    with get_db_connection(db_path) as conn:
        session = _load_session(conn, session_id)
        question = _load_question(conn, str(session["question_uid"]))
        stage = str(session["micro_stage"])
        view: dict[str, Any] = {}
        if stage == "STAGE_1_BLIND_PROBE":
            steps = _branch_steps(
                conn, str(question["question_uid"]), str(session["matched_branch_id"])
            )
            stalled = _find_step(steps, int(session["stalled_step_index"]))
            view = {
                "stalled_step_index": stalled["step_index"],
                "step_goal_desc": stalled["step_goal_desc"],
                "masked_conditions": json.loads(stalled["masked_conditions_json"]),
                "probe_question_md": stalled["probe_question_md"],
            }
        elif stage == "STAGE_2_MICRO_SCAFFOLD":
            view = _question_view(conn, session["prewarm_scaffold_uid"], "micro_scaffold")
            if session["prewarm_scaffold_uid"] is not None:
                view["scaffold_depth"] = _scaffold_depth(
                    conn,
                    str(session["prewarm_scaffold_uid"]),
                    str(question["question_uid"]),
                )
            if worked_example is not None:
                view["worked_example"] = worked_example
        elif stage == "STAGE_3_REDO_ORIGINAL":
            view = {
                "stem_md": question["stem_md"],
                "instruction": "撤去全部提示与微母题，独立完成原题并提交最终答案。",
            }
        elif stage == "STAGE_4_DECRUTCH_VARIANT":
            view = _stage4_view(conn, session, question)
        elif stage == "STAGE_COOLDOWN_DEFERRED":
            view = {"skeleton_comparison_card": _comparison_card(conn, session, question)}
        elif stage == "SESSION_COMPLETED":
            view = {
                "mastery_state": question["mastery_state"],
                "blind_test_unlock_at": question["blind_test_unlock_at"],
                "blind_test_due_at": question["blind_test_due_at"],
            }
        if feedback is not None:
            view["feedback"] = feedback
            view["last_submission_correct"] = False
        if notice is not None:
            view["notice"] = notice
    return SessionStepResponse(
        session_id=session_id,
        question_uid=str(question["question_uid"]),
        micro_stage=stage,
        mastery_state=str(question["mastery_state"]),
        prewarm_status=str(session["prewarm_status"]),
        view_payload=view,
        explanation_text=explanation_text,
    )
