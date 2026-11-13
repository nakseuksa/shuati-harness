"""分模块双轨制动态 Elo 事件账本引擎（Phase 2 切片 A）。

唯一权威规格：docs/designs/adaptive_practice_harness.md §3.5（模块 5）与 Target File #11。

- 法则 1：水位线高斯门控 ``compute_watermark_gamma`` + ``record_mastered_init``，
  防止直接录入大量简单题导致实战分膨胀。
- 法则 2：草稿子步保底 ``apply_draft_subgoal_floor``，仅当 R_combat < D_sub 发放，
  发放后强制 R_cov ← max(R_cov, R_combat)，杜绝双轨倒挂。
- 法则 3（切片 A 部分）：拆拐杖通过 ``freeze_decrutch_escrow``，覆盖分凭
  ``is_cov_settled`` 熔断只结算一次；实战分目标增量基于入库快照 R_combat^(0)
  核算，扣除已发保底后差额 100% 冻结（零当场预支），待盲测兑现。

架构约束（Alternative 3A 被否决的原因）：
- 不建任何可变标量表；模块双轨分完全由 ``elo_mutation_ledger`` 事件流折叠得出，
  天然支持事件溯源解释与确定性重放。
- 所有函数接收调用方管理的 sqlite3.Connection：不自行开连接、不 commit；
  账本事件 INSERT 先于同函数内其他表写入，配合 ``idempotency_key UNIQUE``
  兜底，保证幂等与调用方事务内的多表原子性。
- 不变量：任何事件后 combat <= coverage（每次写入前强制 max 同步）。
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from backend.config import settings
from backend.observability import log_event

__all__ = [
    "DualEloState",
    "LedgerEventResult",
    "expected_pass",
    "compute_watermark_gamma",
    "get_module_elo",
    "record_mastered_init",
    "apply_draft_subgoal_floor",
    "freeze_decrutch_escrow",
    "realize_or_forfeit_blind_test",
    "apply_overdue_escrow_decay",
    "replay_module_elo_from_ledger",
]

logger = logging.getLogger(__name__)

# 账本触发器全集：切片 A 三类 + 切片 B 两类（盲测兑现 / 超时衰减）。
# 仅为类型别名扩充，切片 A 各函数签名与行为不变。
_Trigger = Literal[
    "MASTERED_INIT",
    "PARTIAL_DRAFT_SUBGOAL_FLOOR",
    "SCAFFOLD_COVER_GAIN",
    "BLIND_TEST_REALIZE",
    "ESCROW_TIMEOUT_DECAY",
]


@dataclass(frozen=True)
class DualEloState:
    """单个 L1 模块的双轨 Elo 快照（覆盖分 >= 实战分）。"""

    coverage_elo: float
    combat_elo: float


@dataclass(frozen=True)
class LedgerEventResult:
    """一次账本结算调用的结果快照。

    - 幂等命中既有事件：返回原 event_id 与原事件 before/after 快照，applied=False；
    - 门控为 0 / 增量为 0 / 熔断拦截：不写事件，event_id=None，before==after；
    - 正常结算：applied=True，event_id 为新事件流水号。
    """

    event_id: int | None
    applied: bool
    coverage_before: float
    coverage_after: float
    combat_before: float
    combat_after: float
    explanation_text: str


# ---------------------------------------------------------------------------
# 纯函数：期望通过率与水位线门控
# ---------------------------------------------------------------------------


def expected_pass(rating: float, difficulty: float) -> float:
    """标准 Elo 期望通过率 E(R, D) = 1 / (1 + 10^((D - R)/400))（设计 §3.5）。"""
    return 1.0 / (1.0 + 10.0 ** ((difficulty - rating) / 400.0))


def compute_watermark_gamma(question_elo: int, combat_elo: float) -> float:
    """水位线高斯门控系数 γ（设计 §3.5 法则 1，三段分段）。

    - D >= R - 200：γ = 1.0（足额结算）；
    - R - 500 <= D < R - 200：γ = exp(-(R - 200 - D)^2 / (2·150^2))（高斯衰减）；
    - D < R - 500：γ = 0.0（远低于水位线，不结算）。

    边界常量取自 settings.WATERMARK_FULL_MARGIN / WATERMARK_ZERO_MARGIN /
    WATERMARK_GAUSS_SIGMA。注意 D == R - 500 按设计公式落在中段（γ = exp(-2)）。
    """
    full_line = combat_elo - settings.WATERMARK_FULL_MARGIN
    zero_line = combat_elo - settings.WATERMARK_ZERO_MARGIN
    if question_elo >= full_line:
        return 1.0
    if question_elo < zero_line:
        return 0.0
    gap = full_line - question_elo
    sigma = settings.WATERMARK_GAUSS_SIGMA
    return math.exp(-(gap * gap) / (2.0 * sigma * sigma))


# ---------------------------------------------------------------------------
# 内部工具：账本读取 / 事件写入（全部复用调用方连接，不 commit）
# ---------------------------------------------------------------------------


def _query_one(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> sqlite3.Row | None:
    """执行查询并返回首行（游标级 Row 工厂，不改动调用方连接的 row_factory）。"""
    cur = conn.execute(sql, params)
    cur.row_factory = sqlite3.Row
    return cur.fetchone()


def get_module_elo(conn: sqlite3.Connection, l1_module: str) -> DualEloState:
    """折叠账本得出模块当前双轨分：取该模块最新事件的 after 值。

    无事件 → 冷启动 (MODULE_ELO_INITIAL, MODULE_ELO_INITIAL)。
    不建可变标量表（Alternative 3A），模块间天然隔离。
    """
    row = _query_one(
        conn,
        "SELECT coverage_elo_after, combat_elo_after FROM elo_mutation_ledger "
        "WHERE l1_module = ? ORDER BY event_id DESC LIMIT 1",
        (l1_module,),
    )
    if row is None:
        initial = settings.MODULE_ELO_INITIAL
        return DualEloState(coverage_elo=initial, combat_elo=initial)
    return DualEloState(
        coverage_elo=float(row["coverage_elo_after"]),
        combat_elo=float(row["combat_elo_after"]),
    )


def _find_event_by_key(conn: sqlite3.Connection, idempotency_key: str) -> sqlite3.Row | None:
    return _query_one(
        conn,
        "SELECT * FROM elo_mutation_ledger WHERE idempotency_key = ?",
        (idempotency_key,),
    )


def _find_latest_question_event(
    conn: sqlite3.Connection, question_uid: str, trigger: _Trigger
) -> sqlite3.Row | None:
    return _query_one(
        conn,
        "SELECT * FROM elo_mutation_ledger WHERE question_uid = ? AND mutation_trigger = ? "
        "ORDER BY event_id DESC LIMIT 1",
        (question_uid, trigger),
    )


def _result_from_event(row: sqlite3.Row) -> LedgerEventResult:
    """幂等命中：原样返回既有事件的 before/after 快照，applied=False。"""
    return LedgerEventResult(
        event_id=int(row["event_id"]),
        applied=False,
        coverage_before=float(row["coverage_elo_before"]),
        coverage_after=float(row["coverage_elo_after"]),
        combat_before=float(row["combat_elo_before"]),
        combat_after=float(row["combat_elo_after"]),
        explanation_text=str(row["explanation_text"]),
    )


def _noop_result(state: DualEloState, explanation: str) -> LedgerEventResult:
    """门控为 0 / 增量为 0 / 熔断拦截：不写事件，before==after。"""
    return LedgerEventResult(
        event_id=None,
        applied=False,
        coverage_before=state.coverage_elo,
        coverage_after=state.coverage_elo,
        combat_before=state.combat_elo,
        combat_after=state.combat_elo,
        explanation_text=explanation,
    )


def _load_question(conn: sqlite3.Connection, question_uid: str) -> sqlite3.Row:
    row = _query_one(
        conn,
        "SELECT question_uid, base_elo, primary_l1_module FROM questions WHERE question_uid = ?",
        (question_uid,),
    )
    if row is None:
        raise ValueError(f"题目不存在：question_uid={question_uid!r}，无法进行 Elo 结算")
    return row


def _insert_event(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    l1_module: str,
    question_uid: str,
    trigger: _Trigger,
    eval_difficulty_elo: int,
    gamma: float,
    before: DualEloState,
    after: DualEloState,
    explanation: str,
) -> int:
    """写入不可变账本事件并返回 event_id。

    幂等键 UNIQUE 约束兜底：并发竞态下重复 INSERT 抛 sqlite3.IntegrityError，
    由调用函数捕获后回读既有事件快照。本函数不 commit。
    """
    cur = conn.execute(
        "INSERT INTO elo_mutation_ledger ("
        " idempotency_key, l1_module, question_uid, mutation_trigger,"
        " eval_difficulty_elo, watermark_gamma,"
        " coverage_elo_before, coverage_elo_after, combat_elo_before, combat_elo_after,"
        " explanation_text"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            idempotency_key,
            l1_module,
            question_uid,
            trigger,
            eval_difficulty_elo,
            gamma,
            before.coverage_elo,
            after.coverage_elo,
            before.combat_elo,
            after.combat_elo,
            explanation,
        ),
    )
    event_id = cur.lastrowid
    if event_id is None:  # AUTOINCREMENT 主键下不可能发生，仅为类型收窄
        raise RuntimeError("elo_mutation_ledger INSERT 未返回 event_id")
    return event_id


def _idempotent_hit(
    conn: sqlite3.Connection, idempotency_key: str
) -> LedgerEventResult | None:
    """UNIQUE 兜底命中后回读既有事件；非幂等冲突（如外键）则返回 None 继续抛出。"""
    row = _find_event_by_key(conn, idempotency_key)
    return _result_from_event(row) if row is not None else None


def _log_elo_event(
    trigger: str,
    key: str,
    question_uid: str,
    session_id: str | None,
    module: str,
    gamma: float,
    result: LedgerEventResult,
) -> None:
    """elo.event 行为事件（P5 观测层）：幂等键 + 双轨 before/after 全量留痕。

    幂等命中 / 门控拦截的 applied=False 调用亦留痕（审计可追溯）。
    """
    fields: dict[str, Any] = {
        "trigger": trigger,
        "idempotency_key": key,
        "question_uid": question_uid,
        "l1_module": module,
        "applied": result.applied,
        "event_id": result.event_id,
        "coverage_before": result.coverage_before,
        "coverage_after": result.coverage_after,
        "combat_before": result.combat_before,
        "combat_after": result.combat_after,
        "gamma": gamma,
    }
    if session_id is not None:
        fields["session_id"] = session_id
    log_event(logger, "elo.event", **fields)


# ---------------------------------------------------------------------------
# 法则 1：直接录入「已掌握」题的水位线结算
# ---------------------------------------------------------------------------


def record_mastered_init(
    conn: sqlite3.Connection, question_uid: str, session_id: str
) -> LedgerEventResult:
    """直接录入已掌握题：ΔR_combat = γ · K_INIT · (1 - E(R_combat, D_q))。

    同步上推 R_cov ← max(R_cov, R_combat)；γ=0 或 Δ=0 不写事件。
    幂等键 '{question_uid}:{session_id}:MASTERED_INIT'。
    """
    key = f"{question_uid}:{session_id}:MASTERED_INIT"
    prior = _find_event_by_key(conn, key)
    if prior is not None:
        result = _result_from_event(prior)
        _log_elo_event(
            "MASTERED_INIT", key, question_uid, session_id,
            str(prior["l1_module"]), float(prior["watermark_gamma"]), result,
        )
        return result

    question = _load_question(conn, question_uid)
    module = str(question["primary_l1_module"])
    difficulty = int(question["base_elo"])
    before = get_module_elo(conn, module)

    gamma = compute_watermark_gamma(difficulty, before.combat_elo)
    if gamma == 0.0:
        result = _noop_result(
            before,
            f"【{module}】直接录入已掌握题：难度基准 {difficulty} 分远低于当前实战分"
            f" {before.combat_elo:.1f} 的水位线下限（-{settings.WATERMARK_ZERO_MARGIN}），"
            "门控 γ=0，本次不加分。",
        )
        _log_elo_event("MASTERED_INIT", key, question_uid, session_id, module, gamma, result)
        return result
    delta = gamma * settings.K_INIT * (1.0 - expected_pass(before.combat_elo, difficulty))
    if delta == 0.0:
        result = _noop_result(
            before,
            f"【{module}】直接录入已掌握题：难度基准 {difficulty} 分，本次增量为 0，不写事件。",
        )
        _log_elo_event("MASTERED_INIT", key, question_uid, session_id, module, gamma, result)
        return result

    combat_after = before.combat_elo + delta
    after = DualEloState(
        coverage_elo=max(before.coverage_elo, combat_after),
        combat_elo=combat_after,
    )
    explanation = (
        f"【{module}】直接录入已掌握题：难度基准 {difficulty} 分，水位线门控 γ={gamma:.3f}，"
        f"实战分 +{delta:.1f}（{before.combat_elo:.1f} → {after.combat_elo:.1f}），"
        f"覆盖分同步至 {after.coverage_elo:.1f}。"
    )
    try:
        event_id = _insert_event(
            conn,
            idempotency_key=key,
            l1_module=module,
            question_uid=question_uid,
            trigger="MASTERED_INIT",
            eval_difficulty_elo=difficulty,
            gamma=gamma,
            before=before,
            after=after,
            explanation=explanation,
        )
    except sqlite3.IntegrityError:
        hit = _idempotent_hit(conn, key)
        if hit is None:
            raise
        _log_elo_event("MASTERED_INIT", key, question_uid, session_id, module, gamma, hit)
        return hit
    result = LedgerEventResult(
        event_id=event_id,
        applied=True,
        coverage_before=before.coverage_elo,
        coverage_after=after.coverage_elo,
        combat_before=before.combat_elo,
        combat_after=after.combat_elo,
        explanation_text=explanation,
    )
    _log_elo_event("MASTERED_INIT", key, question_uid, session_id, module, gamma, result)
    return result


# ---------------------------------------------------------------------------
# 法则 2：草稿子步保底实战分（单调性硬锁裁剪后的 D_sub 由调用方传入）
# ---------------------------------------------------------------------------


def apply_draft_subgoal_floor(
    conn: sqlite3.Connection, question_uid: str, session_id: str, d_sub: int
) -> LedgerEventResult:
    """草稿前缀子目标保底：仅当 R_combat < D_sub 发放 ΔR_floor = K_FLOOR·(1-E(R_combat, D_sub))。

    首次发放创建 question_escrow_pool 行：initial_combat_elo_snapshot=发放前实战分、
    floor_elo_applied=Δ、escrow_status='FLOOR_ONLY'；发放后强制 R_cov ← max(R_cov, R_combat)。
    同幂等键重复调用不重复发放；若池已存在（新会话再次保底），保留入库快照、
    累加 floor_elo_applied，保证法则 3 的守恒核算扣除全部已发保底。
    """
    key = f"{question_uid}:{session_id}:PARTIAL_DRAFT_SUBGOAL_FLOOR"
    prior = _find_event_by_key(conn, key)
    if prior is not None:
        result = _result_from_event(prior)
        _log_elo_event(
            "PARTIAL_DRAFT_SUBGOAL_FLOOR", key, question_uid, session_id,
            str(prior["l1_module"]), float(prior["watermark_gamma"]), result,
        )
        return result

    question = _load_question(conn, question_uid)
    module = str(question["primary_l1_module"])
    before = get_module_elo(conn, module)

    if before.combat_elo >= d_sub:
        result = _noop_result(
            before,
            f"【{module}】草稿子步保底未发放：当前实战分 {before.combat_elo:.1f} 已不低于"
            f"子步难度基准 {d_sub} 分，无需保底推演。",
        )
        _log_elo_event(
            "PARTIAL_DRAFT_SUBGOAL_FLOOR", key, question_uid, session_id, module, 1.0, result
        )
        return result
    delta = settings.K_FLOOR * (1.0 - expected_pass(before.combat_elo, d_sub))
    if delta == 0.0:
        result = _noop_result(
            before,
            f"【{module}】草稿子步保底：子步难度基准 {d_sub} 分，本次增量为 0，不写事件。",
        )
        _log_elo_event(
            "PARTIAL_DRAFT_SUBGOAL_FLOOR", key, question_uid, session_id, module, 1.0, result
        )
        return result

    combat_after = before.combat_elo + delta
    after = DualEloState(
        coverage_elo=max(before.coverage_elo, combat_after),
        combat_elo=combat_after,
    )
    explanation = (
        f"【{module}】草稿子步保底：手写草稿独立完成前缀子目标（难度基准 D_sub={d_sub} 分），"
        f"当前实战分 {before.combat_elo:.1f} 低于该基准，发放保底 +{delta:.1f}"
        f"（→ {after.combat_elo:.1f}），覆盖分同步上推至 {after.coverage_elo:.1f}；"
        "增量已记入单题托管池，待拆拐杖/盲测阶段守恒核算。"
    )
    # 账本 INSERT 先行（幂等闸门），成功后再写托管池；同处调用方事务，保证原子。
    try:
        event_id = _insert_event(
            conn,
            idempotency_key=key,
            l1_module=module,
            question_uid=question_uid,
            trigger="PARTIAL_DRAFT_SUBGOAL_FLOOR",
            eval_difficulty_elo=d_sub,
            gamma=1.0,
            before=before,
            after=after,
            explanation=explanation,
        )
    except sqlite3.IntegrityError:
        hit = _idempotent_hit(conn, key)
        if hit is None:
            raise
        _log_elo_event(
            "PARTIAL_DRAFT_SUBGOAL_FLOOR", key, question_uid, session_id, module, 1.0, hit
        )
        return hit

    pool = _query_one(
        conn,
        "SELECT question_uid FROM question_escrow_pool WHERE question_uid = ?",
        (question_uid,),
    )
    if pool is None:
        conn.execute(
            "INSERT INTO question_escrow_pool ("
            " question_uid, l1_module, initial_combat_elo_snapshot, floor_elo_applied,"
            " escrow_combat_delta, is_cov_settled, decay_factor, escrow_status"
            ") VALUES (?, ?, ?, ?, 0.0, 0, 1.0, 'FLOOR_ONLY')",
            (question_uid, module, before.combat_elo, delta),
        )
    else:
        # 池已存在：入库快照 R_combat^(0) 不可覆盖，累加已发保底供守恒扣除。
        conn.execute(
            "UPDATE question_escrow_pool"
            " SET floor_elo_applied = floor_elo_applied + ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE question_uid = ?",
            (delta, question_uid),
        )
    result = LedgerEventResult(
        event_id=event_id,
        applied=True,
        coverage_before=before.coverage_elo,
        coverage_after=after.coverage_elo,
        combat_before=before.combat_elo,
        combat_after=after.combat_elo,
        explanation_text=explanation,
    )
    _log_elo_event(
        "PARTIAL_DRAFT_SUBGOAL_FLOOR", key, question_uid, session_id, module, 1.0, result
    )
    return result


# ---------------------------------------------------------------------------
# 法则 3（切片 A）：拆拐杖通过 → 覆盖分一次性结算 + 实战分差额 100% 冻结
# ---------------------------------------------------------------------------


def freeze_decrutch_escrow(
    conn: sqlite3.Connection, question_uid: str, session_id: str
) -> LedgerEventResult:
    """当场拆拐杖通过升入 PENDING_RETEST 时调用（设计 §3.5 法则 3，零当场预支）。

    1. 仅当 escrow 池 is_cov_settled==0 时结算一次覆盖分：
       ΔR_cov = K_COV·(1-E(R_cov, D_orig))，写 SCAFFOLD_COVER_GAIN 事件（combat 不变）；
    2. 基于池内入库快照计算 target = K_COMBAT·(1-E(snapshot, D_orig))，
       escrow_combat_delta = max(0, target - floor_elo_applied) 全额冻结，
       状态置 FROZEN_PENDING_BLIND；
    3. 同幂等键二次调用 applied=False；is_cov_settled 熔断拦截跨会话重复结算；
    4. 池不存在（无保底发放的题）：先建池 snapshot=当前 combat、floor=0、FLOOR_ONLY。
    """
    key = f"{question_uid}:{session_id}:SCAFFOLD_COVER_GAIN"
    prior = _find_event_by_key(conn, key)
    if prior is not None:
        result = _result_from_event(prior)
        _log_elo_event(
            "SCAFFOLD_COVER_GAIN", key, question_uid, session_id,
            str(prior["l1_module"]), float(prior["watermark_gamma"]), result,
        )
        return result

    question = _load_question(conn, question_uid)
    module = str(question["primary_l1_module"])
    d_orig = int(question["base_elo"])
    before = get_module_elo(conn, module)

    pool = _query_one(
        conn,
        "SELECT initial_combat_elo_snapshot, floor_elo_applied, is_cov_settled"
        " FROM question_escrow_pool WHERE question_uid = ?",
        (question_uid,),
    )
    if pool is not None and int(pool["is_cov_settled"]) == 1:
        # 覆盖分熔断：无论会话是否相同，只结算一次；回读原结算事件快照。
        settled = _find_latest_question_event(conn, question_uid, "SCAFFOLD_COVER_GAIN")
        if settled is not None:
            result = _result_from_event(settled)
            _log_elo_event(
                "SCAFFOLD_COVER_GAIN", key, question_uid, session_id,
                str(settled["l1_module"]), float(settled["watermark_gamma"]), result,
            )
            return result
        result = _noop_result(
            before,
            f"【{module}】拆拐杖覆盖分已结算过（is_cov_settled=1），本次不重复结算。",
        )
        _log_elo_event("SCAFFOLD_COVER_GAIN", key, question_uid, session_id, module, 1.0, result)
        return result

    snapshot = float(pool["initial_combat_elo_snapshot"]) if pool is not None else before.combat_elo
    floor_applied = float(pool["floor_elo_applied"]) if pool is not None else 0.0

    cov_delta = settings.K_COV * (1.0 - expected_pass(before.coverage_elo, d_orig))
    if cov_delta == 0.0:
        result = _noop_result(
            before,
            f"【{module}】拆拐杖覆盖分结算：原题难度基准 {d_orig} 分，本次增量为 0，不写事件。",
        )
        _log_elo_event("SCAFFOLD_COVER_GAIN", key, question_uid, session_id, module, 1.0, result)
        return result
    after = DualEloState(
        coverage_elo=max(before.coverage_elo + cov_delta, before.combat_elo),
        combat_elo=before.combat_elo,
    )
    target = settings.K_COMBAT * (1.0 - expected_pass(snapshot, d_orig))
    escrow_delta = max(0.0, target - floor_applied)
    explanation = (
        f"【{module}】拆拐杖当场变式通过：按原题难度基准 {d_orig} 分一次性结算覆盖分"
        f" +{cov_delta:.1f}（{before.coverage_elo:.1f} → {after.coverage_elo:.1f}）；"
        f"实战分目标增量 {target:.1f}（基于入库快照 {snapshot:.1f}），扣除已发保底"
        f" {floor_applied:.1f} 后差额 {escrow_delta:.1f} 已 100% 冻结托管，"
        "待延迟盲测通过后兑现，当场实战分不变。"
    )
    try:
        event_id = _insert_event(
            conn,
            idempotency_key=key,
            l1_module=module,
            question_uid=question_uid,
            trigger="SCAFFOLD_COVER_GAIN",
            eval_difficulty_elo=d_orig,
            gamma=1.0,
            before=before,
            after=after,
            explanation=explanation,
        )
    except sqlite3.IntegrityError:
        hit = _idempotent_hit(conn, key)
        if hit is None:
            raise
        _log_elo_event("SCAFFOLD_COVER_GAIN", key, question_uid, session_id, module, 1.0, hit)
        return hit

    if pool is None:
        # 契约第 4 步：无保底发放的题先建 FLOOR_ONLY 池，再走结算/冻结流程。
        conn.execute(
            "INSERT INTO question_escrow_pool ("
            " question_uid, l1_module, initial_combat_elo_snapshot, floor_elo_applied,"
            " escrow_combat_delta, is_cov_settled, decay_factor, escrow_status"
            ") VALUES (?, ?, ?, 0.0, 0.0, 0, 1.0, 'FLOOR_ONLY')",
            (question_uid, module, snapshot),
        )
    conn.execute(
        "UPDATE question_escrow_pool"
        " SET is_cov_settled = 1, escrow_combat_delta = ?,"
        " escrow_status = 'FROZEN_PENDING_BLIND', updated_at = CURRENT_TIMESTAMP"
        " WHERE question_uid = ?",
        (escrow_delta, question_uid),
    )
    result = LedgerEventResult(
        event_id=event_id,
        applied=True,
        coverage_before=before.coverage_elo,
        coverage_after=after.coverage_elo,
        combat_before=before.combat_elo,
        combat_after=after.combat_elo,
        explanation_text=explanation,
    )
    _log_elo_event("SCAFFOLD_COVER_GAIN", key, question_uid, session_id, module, 1.0, result)
    log_event(
        logger,
        "escrow.frozen",
        question_uid=question_uid,
        session_id=session_id,
        l1_module=module,
        amount=escrow_delta,
        decay_factor=1.0,
    )
    return result


# ---------------------------------------------------------------------------
# Phase 3 切片 B：盲测兑现 / forfeit / 温和超时衰减 / 确定性重放
# ---------------------------------------------------------------------------


def realize_or_forfeit_blind_test(
    conn: sqlite3.Connection, question_uid: str, session_id: str, passed: bool
) -> LedgerEventResult:
    """延迟盲测销账（设计 §3.5 法则 3，CUJ 3）。

    - passed=True：解冻 ``escrow_combat_delta * decay_factor`` 全额合入 R_combat，
      置 REALIZED_MASTERED，R_cov ← max(R_cov, R_combat)，写 BLIND_TEST_REALIZE 事件
      （幂等键 ``{uid}:{session_id}:BLIND_TEST_REALIZE``）。decay_factor==1 时
      floor + 已兑现 ≡ target，与在入库快照下直接独立做对严格守恒。
    - passed=False：托管差额归零、置 FORFEITED_RESET；combat/cov 分文不动，
      不写账本事件（枚举无 forfeit trigger），返回 event_id=None、before==after。
    - 幂等：池已 REALIZED/FORFEITED → 二次调用无操作（applied=False）。

    宏观 mastery_state 的流转由调用方（盲测调度器 / FSM）负责，本引擎只动
    Elo 账本与托管池（与切片 A 的职责边界一致）。
    """
    question = _load_question(conn, question_uid)
    module = str(question["primary_l1_module"])
    d_orig = int(question["base_elo"])
    before = get_module_elo(conn, module)

    pool = _query_one(
        conn,
        "SELECT escrow_combat_delta, decay_factor, escrow_status"
        " FROM question_escrow_pool WHERE question_uid = ?",
        (question_uid,),
    )

    # 终态吸收：已兑现 / 已 forfeit 的池，任何二次调用都幂等无操作。
    if pool is not None and pool["escrow_status"] in ("REALIZED_MASTERED", "FORFEITED_RESET"):
        if pool["escrow_status"] == "REALIZED_MASTERED":
            realized = _find_latest_question_event(conn, question_uid, "BLIND_TEST_REALIZE")
            if realized is not None:
                result = _result_from_event(realized)
                _log_elo_event(
                    "BLIND_TEST_REALIZE", f"{question_uid}:{session_id}:BLIND_TEST_REALIZE",
                    question_uid, session_id,
                    str(realized["l1_module"]), float(realized["watermark_gamma"]), result,
                )
                return result
        result = _noop_result(
            before,
            f"【{module}】单题托管池已处于终态 {pool['escrow_status']}，盲测结算幂等无操作。",
        )
        _log_elo_event(
            "BLIND_TEST_REALIZE", f"{question_uid}:{session_id}:BLIND_TEST_REALIZE",
            question_uid, session_id, module, 1.0, result,
        )
        return result

    # 尚未冻结（无池 / FLOOR_ONLY）：无可兑现差额，防御性无操作。
    if pool is None or pool["escrow_status"] != "FROZEN_PENDING_BLIND":
        result = _noop_result(
            before,
            f"【{module}】单题托管池尚未冻结待兑现差额，盲测结算无操作。",
        )
        _log_elo_event(
            "BLIND_TEST_REALIZE", f"{question_uid}:{session_id}:BLIND_TEST_REALIZE",
            question_uid, session_id, module, 1.0, result,
        )
        return result

    if not passed:
        # forfeit：差额归零、状态 FORFEITED_RESET；combat/cov 分文不动，不写账本事件。
        conn.execute(
            "UPDATE question_escrow_pool"
            " SET escrow_combat_delta = 0.0, escrow_status = 'FORFEITED_RESET',"
            " updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
            (question_uid,),
        )
        result = _noop_result(
            before,
            f"【{module}】延迟盲测未通过：托管差额清零（FORFEITED_RESET），"
            "实战分与覆盖分基线分文不动，题目退回重训流。",
        )
        _log_elo_event(
            "BLIND_TEST_FORFEIT", f"{question_uid}:{session_id}:BLIND_TEST_REALIZE",
            question_uid, session_id, module, float(pool["decay_factor"]), result,
        )
        log_event(
            logger,
            "escrow.forfeited",
            question_uid=question_uid,
            session_id=session_id,
            l1_module=module,
            amount=float(pool["escrow_combat_delta"]),
            decay_factor=float(pool["decay_factor"]),
        )
        return result

    key = f"{question_uid}:{session_id}:BLIND_TEST_REALIZE"
    prior = _find_event_by_key(conn, key)
    if prior is not None:
        result = _result_from_event(prior)
        _log_elo_event(
            "BLIND_TEST_REALIZE", key, question_uid, session_id,
            str(prior["l1_module"]), float(prior["watermark_gamma"]), result,
        )
        return result

    escrow = float(pool["escrow_combat_delta"])
    factor = float(pool["decay_factor"])
    delta = escrow * factor
    combat_after = before.combat_elo + delta
    after = DualEloState(
        coverage_elo=max(before.coverage_elo, combat_after),
        combat_elo=combat_after,
    )
    explanation = (
        f"【{module}】延迟盲测一次通过：解冻托管差额 {escrow:.1f} × 衰减系数 {factor:.2f}"
        f" = +{delta:.1f} 实战分（{before.combat_elo:.1f} → {after.combat_elo:.1f}），"
        f"覆盖分同步至 {after.coverage_elo:.1f}，托管池销账为 REALIZED_MASTERED。"
    )
    # 账本 INSERT 先行（幂等闸门），成功后再销账托管池；同处调用方事务，保证原子。
    try:
        event_id = _insert_event(
            conn,
            idempotency_key=key,
            l1_module=module,
            question_uid=question_uid,
            trigger="BLIND_TEST_REALIZE",
            eval_difficulty_elo=d_orig,
            gamma=factor,
            before=before,
            after=after,
            explanation=explanation,
        )
    except sqlite3.IntegrityError:
        hit = _idempotent_hit(conn, key)
        if hit is None:
            raise
        _log_elo_event("BLIND_TEST_REALIZE", key, question_uid, session_id, module, factor, hit)
        return hit

    conn.execute(
        "UPDATE question_escrow_pool"
        " SET escrow_combat_delta = 0.0, escrow_status = 'REALIZED_MASTERED',"
        " updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
        (question_uid,),
    )
    result = LedgerEventResult(
        event_id=event_id,
        applied=True,
        coverage_before=before.coverage_elo,
        coverage_after=after.coverage_elo,
        combat_before=before.combat_elo,
        combat_after=after.combat_elo,
        explanation_text=explanation,
    )
    _log_elo_event("BLIND_TEST_REALIZE", key, question_uid, session_id, module, factor, result)
    log_event(
        logger,
        "escrow.realized",
        question_uid=question_uid,
        session_id=session_id,
        l1_module=module,
        amount=delta,
        decay_factor=factor,
    )
    return result


def apply_overdue_escrow_decay(conn: sqlite3.Connection, active_date: date) -> int:
    """逾期未测的温和超时衰减（设计 §3.5 法则 3，Rusty State）。

    仅对同时满足全部条件的题衰减 decay_factor（每日 -ESCROW_DAILY_DECAY，下限 0）：
      mastery_state='PENDING_RETEST' 且 escrow_status='FROZEN_PENDING_BLIND'
      且 slated_drill_date==active_date（当日已排入题单）
      且 slated_exposure_count > DECAY_EXPOSURE_THRESHOLD。
    系统顺延题当日未排入 → 天然冻结，不得衰减。

    写 ESCROW_TIMEOUT_DECAY 事件（combat/cov before==after，watermark_gamma=新系数，
    中文解释条），幂等键 ``{uid}:{active_date}:ESCROW_TIMEOUT_DECAY``（同日重复不重复衰减）。
    返回本次衰减题数；绝不扣减 combat/cov 基线。
    """
    cur = conn.execute(
        "SELECT q.question_uid, q.primary_l1_module, q.base_elo,"
        " q.slated_exposure_count, p.escrow_combat_delta, p.decay_factor"
        " FROM questions q JOIN question_escrow_pool p ON q.question_uid = p.question_uid"
        " WHERE q.mastery_state = 'PENDING_RETEST'"
        " AND p.escrow_status = 'FROZEN_PENDING_BLIND'"
        " AND q.slated_drill_date = ?"
        " AND q.slated_exposure_count > ?"
        " ORDER BY q.question_uid",
        (active_date.isoformat(), settings.DECAY_EXPOSURE_THRESHOLD),
    )
    cur.row_factory = sqlite3.Row
    candidates = cur.fetchall()

    decayed = 0
    for row in candidates:
        uid = str(row["question_uid"])
        module = str(row["primary_l1_module"])
        key = f"{uid}:{active_date.isoformat()}:ESCROW_TIMEOUT_DECAY"
        if _find_event_by_key(conn, key) is not None:
            state = get_module_elo(conn, module)
            _log_elo_event(
                "ESCROW_TIMEOUT_DECAY", key, uid, None, module, float(row["decay_factor"]),
                LedgerEventResult(
                    event_id=None,
                    applied=False,
                    coverage_before=state.coverage_elo,
                    coverage_after=state.coverage_elo,
                    combat_before=state.combat_elo,
                    combat_after=state.combat_elo,
                    explanation_text="同日已衰减，幂等跳过。",
                ),
            )
            continue  # 同日已衰减，幂等跳过
        before = get_module_elo(conn, module)
        old_factor = float(row["decay_factor"])
        new_factor = max(0.0, old_factor - settings.ESCROW_DAILY_DECAY)
        exposure = int(row["slated_exposure_count"])
        explanation = (
            f"【{module}】逾期未测温和衰减：题目已连续 {exposure} 个活跃日排入盲测题单仍未测，"
            f"托管兑现系数 {old_factor:.2f} → {new_factor:.2f}"
            f"（每日 -{settings.ESCROW_DAILY_DECAY:.2f}）；"
            "仅折减待兑现差额，实战分与覆盖分基线分文不动。"
        )
        try:
            event_id = _insert_event(
                conn,
                idempotency_key=key,
                l1_module=module,
                question_uid=uid,
                trigger="ESCROW_TIMEOUT_DECAY",
                eval_difficulty_elo=int(row["base_elo"]),
                gamma=new_factor,
                before=before,
                after=before,  # combat/cov 基线逐事件不变
                explanation=explanation,
            )
        except sqlite3.IntegrityError:
            if _idempotent_hit(conn, key) is None:
                raise
            continue  # 竞态下已被他方衰减，幂等跳过
        conn.execute(
            "UPDATE question_escrow_pool"
            " SET decay_factor = ?, updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
            (new_factor, uid),
        )
        _log_elo_event(
            "ESCROW_TIMEOUT_DECAY", key, uid, None, module, new_factor,
            LedgerEventResult(
                event_id=event_id,
                applied=True,
                coverage_before=before.coverage_elo,
                coverage_after=before.coverage_elo,
                combat_before=before.combat_elo,
                combat_after=before.combat_elo,
                explanation_text=explanation,
            ),
        )
        log_event(
            logger,
            "escrow.decayed",
            question_uid=uid,
            l1_module=module,
            amount=float(row["escrow_combat_delta"]),
            decay_factor=new_factor,
            old_factor=old_factor,
            active_date=active_date.isoformat(),
        )
        decayed += 1
    return decayed


def replay_module_elo_from_ledger(conn: sqlite3.Connection, l1_module: str) -> DualEloState:
    """从事件流确定性重建模块双轨分（设计 Target #11，事件溯源）。

    从 MODULE_ELO_INITIAL 起按 event_id 顺序重放全部事件增量，逐事件校验 before
    与累计 after 衔接（发现断链抛 ValueError）；终态与 get_module_elo 一致。
    空模块 → (MODULE_ELO_INITIAL, MODULE_ELO_INITIAL)。
    """
    initial = settings.MODULE_ELO_INITIAL
    cov = combat = initial
    cur = conn.execute(
        "SELECT event_id, coverage_elo_before, coverage_elo_after,"
        " combat_elo_before, combat_elo_after"
        " FROM elo_mutation_ledger WHERE l1_module = ? ORDER BY event_id",
        (l1_module,),
    )
    cur.row_factory = sqlite3.Row
    for row in cur.fetchall():
        if (
            abs(float(row["coverage_elo_before"]) - cov) > 1e-9
            or abs(float(row["combat_elo_before"]) - combat) > 1e-9
        ):
            raise ValueError(
                f"模块 {l1_module!r} 账本断链：event_id={row['event_id']} 的 before 值"
                "与上一事件 after 不衔接，无法确定性重放。"
            )
        cov = float(row["coverage_elo_after"])
        combat = float(row["combat_elo_after"])

    rebuilt = DualEloState(coverage_elo=cov, combat_elo=combat)
    current = get_module_elo(conn, l1_module)
    if (
        abs(rebuilt.coverage_elo - current.coverage_elo) > 1e-9
        or abs(rebuilt.combat_elo - current.combat_elo) > 1e-9
    ):
        raise ValueError(
            f"模块 {l1_module!r} 重放终态与 get_module_elo 不一致，确定性重建失败。"
        )
    return rebuilt
