"""Phase 2 切片 A：双轨 Elo 事件账本引擎专属测试。

规格：docs/designs/adaptive_practice_harness.md §3.5 法则 1/2/3、Target File #11/#15、
Alternative 3A（事件溯源折叠，无可变标量表）。
盲测兑现 / forfeit / 超时衰减 / Replay 的全流程守恒测试属 Phase 3 切片 B，
见 tests/test_difficulty_and_elo.py（本文件不触碰）。

边界说明：设计公式中段为 R-500 <= D < R-200，故 D == R-500 时 γ = exp(-2) ≈ 0.1353
（契约测试条目中「D=R-500 → 0.0」为三段分界的简写，按任务规则以设计公式为准；
零分段从 D < R-500 开始，本文件对两侧边界均有断言）。
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.services import elo_engine
from backend.services.elo_engine import (
    DualEloState,
    LedgerEventResult,
    apply_draft_subgoal_floor,
    compute_watermark_gamma,
    expected_pass,
    freeze_decrutch_escrow,
    get_module_elo,
    record_mastered_init,
)

MODULE_A = "电磁学"
MODULE_B = "解析几何"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_db: Path) -> Iterator[sqlite3.Connection]:
    """调用方管理的连接（get_db_connection 统一 commit/rollback）。"""
    with get_db_connection(tmp_db) as c:
        yield c


def _make_question(
    conn: sqlite3.Connection, uid: str, *, base_elo: int, module: str = MODULE_A
) -> None:
    conn.execute(
        "INSERT INTO questions (question_uid, origin_type, target_grade_and_subject,"
        " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
        " base_elo, tier, primary_l1_module)"
        " VALUES (?, 'USER_CANONICAL', '高中物理-高二', '题干', ?, '42', 2, 2, 2, 2,"
        " ?, 'L3_综合中档', ?)",
        (uid, f"hash-{uid}", base_elo, module),
    )


def _ledger_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM elo_mutation_ledger").fetchone()
    return int(row[0])


def _pool(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM question_escrow_pool WHERE question_uid = ?", (uid,))
    cur.row_factory = sqlite3.Row
    return cur.fetchone()


def _event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    cur = conn.execute("SELECT * FROM elo_mutation_ledger WHERE event_id = ?", (event_id,))
    cur.row_factory = sqlite3.Row
    row = cur.fetchone()
    assert row is not None
    return row


def _assert_ledger_chain(conn: sqlite3.Connection, module: str) -> None:
    """账本链完整性：冷启动起点、before/after 逐事件衔接、任何事件后不倒挂。"""
    cur = conn.execute(
        "SELECT * FROM elo_mutation_ledger WHERE l1_module = ? ORDER BY event_id", (module,)
    )
    cur.row_factory = sqlite3.Row
    prev_cov = prev_combat = settings.MODULE_ELO_INITIAL
    for row in cur.fetchall():
        assert row["coverage_elo_before"] == pytest.approx(prev_cov)
        assert row["combat_elo_before"] == pytest.approx(prev_combat)
        assert row["combat_elo_after"] <= row["coverage_elo_after"]
        assert row["explanation_text"]
        prev_cov = row["coverage_elo_after"]
        prev_combat = row["combat_elo_after"]
    state = get_module_elo(conn, module)
    assert state.coverage_elo == pytest.approx(prev_cov)
    assert state.combat_elo == pytest.approx(prev_combat)
    assert state.combat_elo <= state.coverage_elo


# ---------------------------------------------------------------------------
# expected_pass：标准期望通过率 E(R, D)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rating", [800.0, 1200.0, 2000.0, 2800.0])
def test_expected_pass_equal_is_half(rating: float) -> None:
    assert expected_pass(rating, rating) == pytest.approx(0.5)


@pytest.mark.parametrize("difficulty", [800.0, 1200.0, 2400.0])
def test_expected_pass_400_gap(difficulty: float) -> None:
    assert expected_pass(difficulty + 400.0, difficulty) == pytest.approx(10.0 / 11.0)
    assert expected_pass(difficulty - 400.0, difficulty) == pytest.approx(1.0 / 11.0)


def test_expected_pass_symmetry() -> None:
    for rating, difficulty in [(1200.0, 1600.0), (900.0, 2100.0), (2650.0, 800.0)]:
        assert expected_pass(rating, difficulty) + expected_pass(difficulty, rating) == (
            pytest.approx(1.0)
        )


def test_expected_pass_monotonic() -> None:
    assert expected_pass(1300.0, 1500.0) > expected_pass(1200.0, 1500.0)
    assert expected_pass(1200.0, 1600.0) < expected_pass(1200.0, 1500.0)


# ---------------------------------------------------------------------------
# compute_watermark_gamma：三段分段与边界（设计 §3.5 法则 1）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question_elo", "combat_elo"),
    [
        pytest.param(1000, 1200.0, id="boundary-R-minus-200"),
        pytest.param(1001, 1200.0, id="just-above-full-line"),
        pytest.param(1200, 1200.0, id="equal"),
        pytest.param(1700, 1200.0, id="far-above"),
        pytest.param(2800, 1200.0, id="max-difficulty"),
    ],
)
def test_gamma_full_segment(question_elo: int, combat_elo: float) -> None:
    assert compute_watermark_gamma(question_elo, combat_elo) == 1.0


@pytest.mark.parametrize(
    ("question_elo", "combat_elo"),
    [
        pytest.param(699, 1200.0, id="just-below-zero-line"),
        pytest.param(200, 1200.0, id="far-below"),
        pytest.param(800, 1301.0, id="zero-line-801"),
    ],
)
def test_gamma_zero_segment(question_elo: int, combat_elo: float) -> None:
    assert compute_watermark_gamma(question_elo, combat_elo) == 0.0


@pytest.mark.parametrize(
    ("question_elo", "expected"),
    [
        pytest.param(850, math.exp(-0.5), id="gap-150-one-sigma"),
        pytest.param(900, math.exp(-2.0 / 9.0), id="gap-100"),
        pytest.param(700, math.exp(-2.0), id="boundary-R-minus-500-in-gauss-segment"),
        pytest.param(701, math.exp(-(299.0**2) / 45000.0), id="just-above-zero-line"),
    ],
)
def test_gamma_gauss_segment(question_elo: int, expected: float) -> None:
    # 设计公式：R-500 <= D < R-200 为高斯段，D == R-500 时 γ = exp(-2) ≈ 0.1353。
    assert compute_watermark_gamma(question_elo, 1200.0) == pytest.approx(expected)


def test_gamma_monotonic_and_continuous() -> None:
    values = [compute_watermark_gamma(d, 1200.0) for d in range(650, 1051, 10)]
    assert all(a <= b for a, b in zip(values, values[1:], strict=False))
    # 满门控边界处连续：gap=1 时 γ ≈ 1
    assert compute_watermark_gamma(999, 1200.0) == pytest.approx(1.0, abs=1e-4)


def test_gamma_uses_settings_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WATERMARK_GAUSS_SIGMA", 300.0)
    assert compute_watermark_gamma(700, 1200.0) == pytest.approx(math.exp(-0.5))
    monkeypatch.setattr(settings, "WATERMARK_FULL_MARGIN", 100)
    assert compute_watermark_gamma(1100, 1200.0) == 1.0
    assert compute_watermark_gamma(1050, 1200.0) == pytest.approx(
        math.exp(-(50.0**2) / (2.0 * 300.0**2))
    )


# ---------------------------------------------------------------------------
# get_module_elo：冷启动 / 折叠最新事件 / 模块隔离（Alternative 3A）
# ---------------------------------------------------------------------------


def test_module_elo_cold_start(conn: sqlite3.Connection) -> None:
    state = get_module_elo(conn, MODULE_A)
    assert state == DualEloState(coverage_elo=1200.0, combat_elo=1200.0)
    assert get_module_elo(conn, "从未出现的模块") == state


def test_module_elo_folds_latest_event(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q1", base_elo=1600)
    _make_question(conn, "q2", base_elo=2000)
    r1 = record_mastered_init(conn, "q1", "s1")
    assert get_module_elo(conn, MODULE_A) == DualEloState(r1.coverage_after, r1.combat_after)
    r2 = record_mastered_init(conn, "q2", "s2")
    assert r2.combat_after > r1.combat_after
    assert get_module_elo(conn, MODULE_A) == DualEloState(r2.coverage_after, r2.combat_after)
    _assert_ledger_chain(conn, MODULE_A)


def test_module_elo_isolation(conn: sqlite3.Connection) -> None:
    _make_question(conn, "qa", base_elo=2000, module=MODULE_A)
    _make_question(conn, "qb", base_elo=1600, module=MODULE_B)
    ra = record_mastered_init(conn, "qa", "sa")
    assert ra.applied
    # A 模块的事件绝不串扰 B 模块
    assert get_module_elo(conn, MODULE_B) == DualEloState(1200.0, 1200.0)
    rb = record_mastered_init(conn, "qb", "sb")
    assert rb.combat_before == 1200.0
    assert get_module_elo(conn, MODULE_A) == DualEloState(ra.coverage_after, ra.combat_after)
    _assert_ledger_chain(conn, MODULE_A)
    _assert_ledger_chain(conn, MODULE_B)


# ---------------------------------------------------------------------------
# record_mastered_init：法则 1 水位线结算
# ---------------------------------------------------------------------------


def test_mastered_init_hard_question(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-hard", base_elo=1600)
    result = record_mastered_init(conn, "q-hard", "s1")
    assert result.applied
    assert result.event_id is not None
    delta = settings.K_INIT * (1.0 - expected_pass(1200.0, 1600.0))
    assert delta == pytest.approx(240.0 / 11.0)  # ≈ 21.8182
    assert result.combat_before == 1200.0
    assert result.combat_after == pytest.approx(1200.0 + delta)
    assert result.coverage_after == pytest.approx(result.combat_after)  # cov 同步上推
    # 事件行完整：幂等键 / 触发器 / 难度基准 / gamma / 双轨四值 / 解释条
    row = _event(conn, result.event_id)
    assert row["idempotency_key"] == "q-hard:s1:MASTERED_INIT"
    assert row["mutation_trigger"] == "MASTERED_INIT"
    assert row["l1_module"] == MODULE_A
    assert row["eval_difficulty_elo"] == 1600
    assert row["watermark_gamma"] == 1.0
    assert row["coverage_elo_before"] == 1200.0
    assert row["combat_elo_before"] == 1200.0
    assert row["coverage_elo_after"] == pytest.approx(result.coverage_after)
    assert row["combat_elo_after"] == pytest.approx(result.combat_after)
    assert MODULE_A in result.explanation_text
    assert "1600" in result.explanation_text
    assert f"{delta:.1f}" in result.explanation_text


def test_mastered_init_low_difficulty_gated(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-easy", base_elo=900)
    result = record_mastered_init(conn, "q-easy", "s1")
    gamma = math.exp(-2.0 / 9.0)  # gap = 1200-200-900 = 100
    ungated = settings.K_INIT * (1.0 - expected_pass(1200.0, 900.0))
    delta = gamma * ungated
    assert result.applied
    assert result.combat_after == pytest.approx(1200.0 + delta)
    assert 0.0 < delta < ungated  # 高斯衰减生效
    assert result.event_id is not None
    assert _event(conn, result.event_id)["watermark_gamma"] == pytest.approx(gamma)


def test_mastered_init_far_below_watermark_no_event(conn: sqlite3.Connection) -> None:
    # 先用 5 道 2800 分难题把实战分推过 1300（水位线下限 R-500 > 800）
    for i in range(5):
        uid = f"q-push-{i}"
        _make_question(conn, uid, base_elo=2800)
        record_mastered_init(conn, uid, f"s{i}")
    state = get_module_elo(conn, MODULE_A)
    assert state.combat_elo > 1300.0
    _make_question(conn, "q-floor-elo", base_elo=800)
    count_before = _ledger_count(conn)
    result = record_mastered_init(conn, "q-floor-elo", "s-low")
    assert not result.applied
    assert result.event_id is None
    assert result.combat_before == result.combat_after == state.combat_elo
    assert result.coverage_before == result.coverage_after == state.coverage_elo
    assert _ledger_count(conn) == count_before  # γ=0 不写事件
    assert "不加分" in result.explanation_text
    assert get_module_elo(conn, MODULE_A) == state


def test_mastered_init_keeps_cov_when_combat_stays_below(conn: sqlite3.Connection) -> None:
    # 拆拐杖覆盖分结算制造 cov > combat 的「水分区」，小额掌握结算不动覆盖分
    _make_question(conn, "q-variant", base_elo=1000)
    freeze = freeze_decrutch_escrow(conn, "q-variant", "sv")
    assert freeze.applied
    gap = freeze.coverage_after - freeze.combat_after
    assert gap > 0.0
    _make_question(conn, "q-small", base_elo=1000)
    result = record_mastered_init(conn, "q-small", "s1")
    delta = settings.K_INIT * (1.0 - expected_pass(1200.0, 1000.0))
    assert delta < gap
    assert result.applied
    assert result.coverage_after == result.coverage_before  # max 同步保持覆盖分
    assert result.combat_after == pytest.approx(1200.0 + delta)
    assert result.combat_after <= result.coverage_after
    _assert_ledger_chain(conn, MODULE_A)


def test_mastered_init_idempotent(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-idem", base_elo=1600)
    first = record_mastered_init(conn, "q-idem", "s1")
    count = _ledger_count(conn)
    second = record_mastered_init(conn, "q-idem", "s1")
    assert not second.applied
    assert second.event_id == first.event_id  # 返回原事件快照
    assert second.coverage_before == first.coverage_before
    assert second.coverage_after == first.coverage_after
    assert second.combat_before == first.combat_before
    assert second.combat_after == first.combat_after
    assert second.explanation_text == first.explanation_text
    assert _ledger_count(conn) == count
    assert get_module_elo(conn, MODULE_A) == DualEloState(
        first.coverage_after, first.combat_after
    )


def test_mastered_init_new_session_is_new_event(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-sess", base_elo=1600)
    first = record_mastered_init(conn, "q-sess", "s1")
    second = record_mastered_init(conn, "q-sess", "s2")
    assert second.applied
    assert second.event_id != first.event_id
    assert second.combat_before == pytest.approx(first.combat_after)
    _assert_ledger_chain(conn, MODULE_A)


def test_mastered_init_missing_question_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="题目不存在"):
        record_mastered_init(conn, "ghost", "s1")


# ---------------------------------------------------------------------------
# apply_draft_subgoal_floor：法则 2 保底推演与托管池快照
# ---------------------------------------------------------------------------


def test_floor_grant_and_pool_snapshot(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-draft", base_elo=1600)
    result = apply_draft_subgoal_floor(conn, "q-draft", "s1", d_sub=1400)
    delta = settings.K_FLOOR * (1.0 - expected_pass(1200.0, 1400.0))
    assert delta == pytest.approx(12.1559509, abs=1e-6)
    assert result.applied
    assert result.event_id is not None
    assert result.combat_after == pytest.approx(1200.0 + delta)
    assert result.coverage_after == pytest.approx(result.combat_after)  # 强制 max 同步
    pool = _pool(conn, "q-draft")
    assert pool is not None
    assert pool["l1_module"] == MODULE_A
    assert pool["initial_combat_elo_snapshot"] == 1200.0  # 发放前 combat，而非发放后
    assert pool["floor_elo_applied"] == pytest.approx(delta)
    assert pool["escrow_combat_delta"] == 0.0
    assert pool["is_cov_settled"] == 0
    assert pool["decay_factor"] == 1.0
    assert pool["escrow_status"] == "FLOOR_ONLY"
    row = _event(conn, result.event_id)
    assert row["idempotency_key"] == "q-draft:s1:PARTIAL_DRAFT_SUBGOAL_FLOOR"
    assert row["mutation_trigger"] == "PARTIAL_DRAFT_SUBGOAL_FLOOR"
    assert row["eval_difficulty_elo"] == 1400
    assert row["watermark_gamma"] == 1.0
    assert MODULE_A in result.explanation_text
    assert "1400" in result.explanation_text
    assert f"{delta:.1f}" in result.explanation_text


@pytest.mark.parametrize("d_sub", [1200, 1100])
def test_floor_not_granted_when_combat_ge_dsub(conn: sqlite3.Connection, d_sub: int) -> None:
    _make_question(conn, "q-no-floor", base_elo=1600)
    result = apply_draft_subgoal_floor(conn, "q-no-floor", "s1", d_sub=d_sub)
    assert not result.applied
    assert result.event_id is None
    assert result.combat_before == result.combat_after == 1200.0
    assert result.coverage_before == result.coverage_after == 1200.0
    assert _ledger_count(conn) == 0
    assert _pool(conn, "q-no-floor") is None  # 未发放不建池
    assert "无需保底" in result.explanation_text


def test_floor_idempotent(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-f-idem", base_elo=1600)
    first = apply_draft_subgoal_floor(conn, "q-f-idem", "s1", d_sub=1400)
    pool_before = _pool(conn, "q-f-idem")
    assert pool_before is not None
    second = apply_draft_subgoal_floor(conn, "q-f-idem", "s1", d_sub=1400)
    assert not second.applied
    assert second.event_id == first.event_id
    assert second.combat_after == first.combat_after
    assert _ledger_count(conn) == 1
    pool_after = _pool(conn, "q-f-idem")
    assert pool_after is not None
    assert pool_after["floor_elo_applied"] == pool_before["floor_elo_applied"]  # 不重复发放
    assert pool_after["initial_combat_elo_snapshot"] == 1200.0


def test_floor_snapshot_is_pre_grant_combat_of_that_grant(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-f1", base_elo=1600)
    _make_question(conn, "q-f2", base_elo=1700)
    r1 = apply_draft_subgoal_floor(conn, "q-f1", "s1", d_sub=1400)
    r2 = apply_draft_subgoal_floor(conn, "q-f2", "s2", d_sub=1500)
    assert r2.applied
    assert r2.combat_before == pytest.approx(r1.combat_after)
    pool2 = _pool(conn, "q-f2")
    assert pool2 is not None
    assert pool2["initial_combat_elo_snapshot"] == pytest.approx(r1.combat_after)
    delta2 = settings.K_FLOOR * (1.0 - expected_pass(r1.combat_after, 1500.0))
    assert pool2["floor_elo_applied"] == pytest.approx(delta2)
    _assert_ledger_chain(conn, MODULE_A)


def test_floor_accumulates_on_existing_pool_new_session(conn: sqlite3.Connection) -> None:
    # 新会话再次保底：入库快照不可覆盖，floor_elo_applied 累加供守恒扣除
    _make_question(conn, "q-acc", base_elo=1600)
    r1 = apply_draft_subgoal_floor(conn, "q-acc", "s1", d_sub=1400)
    r2 = apply_draft_subgoal_floor(conn, "q-acc", "s2", d_sub=1500)
    assert r2.applied
    pool = _pool(conn, "q-acc")
    assert pool is not None
    assert pool["initial_combat_elo_snapshot"] == 1200.0
    delta2 = settings.K_FLOOR * (1.0 - expected_pass(r1.combat_after, 1500.0))
    assert pool["floor_elo_applied"] == pytest.approx(
        (r1.combat_after - 1200.0) + delta2
    )
    assert pool["escrow_status"] == "FLOOR_ONLY"
    _assert_ledger_chain(conn, MODULE_A)


def test_floor_pushes_coverage_when_combat_crosses_gap(conn: sqlite3.Connection) -> None:
    # 构造 cov > combat 后发放大额保底，把 combat 推过旧 cov，验证 max 同步不倒挂
    _make_question(conn, "q-easy-orig", base_elo=800)
    freeze = freeze_decrutch_escrow(conn, "q-easy-orig", "sv")
    assert freeze.applied
    assert freeze.coverage_after > freeze.combat_after
    _make_question(conn, "q-big-floor", base_elo=1600)
    result = apply_draft_subgoal_floor(conn, "q-big-floor", "s1", d_sub=1400)
    assert result.applied
    assert result.combat_after > freeze.coverage_after  # 确实越过了旧覆盖分
    assert result.coverage_after == pytest.approx(result.combat_after)  # 强制同步
    assert result.combat_after <= result.coverage_after
    _assert_ledger_chain(conn, MODULE_A)


def test_floor_missing_question_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="题目不存在"):
        apply_draft_subgoal_floor(conn, "ghost", "s1", d_sub=1400)


# ---------------------------------------------------------------------------
# freeze_decrutch_escrow：法则 3 覆盖分一次性结算 + 差额 100% 冻结
# ---------------------------------------------------------------------------


def test_freeze_with_floor_conservation(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-full", base_elo=1600)
    floor = apply_draft_subgoal_floor(conn, "q-full", "s1", d_sub=1400)
    result = freeze_decrutch_escrow(conn, "q-full", "s1")
    assert result.applied
    assert result.event_id is not None
    # 覆盖分：基于当前 R_cov 结算一次；实战分当场分文不动
    cov_delta = settings.K_COV * (1.0 - expected_pass(floor.coverage_after, 1600.0))
    assert result.coverage_after == pytest.approx(floor.coverage_after + cov_delta)
    assert result.combat_before == result.combat_after == floor.combat_after
    assert result.combat_after <= result.coverage_after
    # 守恒：target 基于入库快照 1200，floor + escrow ≡ target
    target = settings.K_COMBAT * (1.0 - expected_pass(1200.0, 1600.0))
    floor_delta = floor.combat_after - 1200.0
    pool = _pool(conn, "q-full")
    assert pool is not None
    assert pool["is_cov_settled"] == 1
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND"
    assert pool["escrow_combat_delta"] == pytest.approx(max(0.0, target - floor_delta))
    assert pool["floor_elo_applied"] + pool["escrow_combat_delta"] == pytest.approx(
        target, abs=1e-9
    )
    assert pool["decay_factor"] == 1.0
    row = _event(conn, result.event_id)
    assert row["idempotency_key"] == "q-full:s1:SCAFFOLD_COVER_GAIN"
    assert row["mutation_trigger"] == "SCAFFOLD_COVER_GAIN"
    assert row["eval_difficulty_elo"] == 1600
    assert row["watermark_gamma"] == 1.0
    assert row["combat_elo_before"] == row["combat_elo_after"]
    _assert_ledger_chain(conn, MODULE_A)


def test_freeze_without_floor_creates_pool(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-no-floor", base_elo=1600)
    result = freeze_decrutch_escrow(conn, "q-no-floor", "s1")
    assert result.applied
    pool = _pool(conn, "q-no-floor")
    assert pool is not None
    assert pool["initial_combat_elo_snapshot"] == 1200.0  # 无保底题 snapshot=当前 combat
    assert pool["floor_elo_applied"] == 0.0
    target = settings.K_COMBAT * (1.0 - expected_pass(1200.0, 1600.0))
    assert pool["escrow_combat_delta"] == pytest.approx(target)
    assert pool["is_cov_settled"] == 1
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND"
    cov_delta = settings.K_COV * (1.0 - expected_pass(1200.0, 1600.0))
    assert result.coverage_after == pytest.approx(1200.0 + cov_delta)
    assert result.combat_after == 1200.0  # 零当场预支
    _assert_ledger_chain(conn, MODULE_A)


def test_freeze_idempotent_same_key(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-fz-idem", base_elo=1600)
    apply_draft_subgoal_floor(conn, "q-fz-idem", "s1", d_sub=1400)
    first = freeze_decrutch_escrow(conn, "q-fz-idem", "s1")
    count = _ledger_count(conn)
    pool_before = _pool(conn, "q-fz-idem")
    assert pool_before is not None
    second = freeze_decrutch_escrow(conn, "q-fz-idem", "s1")
    assert not second.applied
    assert second.event_id == first.event_id
    assert second.coverage_after == first.coverage_after
    assert second.combat_after == first.combat_after
    assert _ledger_count(conn) == count  # 不重复结算覆盖分
    pool_after = _pool(conn, "q-fz-idem")
    assert pool_after is not None
    assert pool_after["escrow_combat_delta"] == pool_before["escrow_combat_delta"]  # 不重复冻结
    assert pool_after["is_cov_settled"] == 1


def test_freeze_cov_settled_breaker_cross_session(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-breaker", base_elo=1600)
    first = freeze_decrutch_escrow(conn, "q-breaker", "s1")
    count = _ledger_count(conn)
    # 不同会话（幂等键不同）也绝不二次结算：is_cov_settled 熔断
    second = freeze_decrutch_escrow(conn, "q-breaker", "s2")
    assert not second.applied
    assert second.event_id == first.event_id  # 回读原结算事件快照
    assert second.coverage_after == first.coverage_after
    assert _ledger_count(conn) == count
    assert get_module_elo(conn, MODULE_A) == DualEloState(
        first.coverage_after, first.combat_after
    )


def test_freeze_escrow_clamped_at_zero(conn: sqlite3.Connection) -> None:
    # 防御性场景：保底超过整题目标增量时差额截断为 0（真实流程中 rubric 保证
    # D_sub <= D_orig - 150，不会触发；此处直接验证 max(0, ...) 公式）
    _make_question(conn, "q-clamp", base_elo=1250)
    floor = apply_draft_subgoal_floor(conn, "q-clamp", "s1", d_sub=1800)
    target = settings.K_COMBAT * (1.0 - expected_pass(1200.0, 1250.0))
    floor_delta = floor.combat_after - 1200.0
    assert floor_delta > target
    result = freeze_decrutch_escrow(conn, "q-clamp", "s1")
    assert result.applied
    pool = _pool(conn, "q-clamp")
    assert pool is not None
    assert pool["escrow_combat_delta"] == 0.0
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND"
    assert result.combat_after == pytest.approx(floor.combat_after)
    assert result.combat_after <= result.coverage_after


def test_freeze_target_uses_snapshot_not_current_combat(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-snap", base_elo=1600)
    _make_question(conn, "q-other", base_elo=2800)
    floor = apply_draft_subgoal_floor(conn, "q-snap", "s1", d_sub=1400)
    # 其他题目把模块实战分推高后，target 仍必须基于入库快照 1200 核算
    record_mastered_init(conn, "q-other", "s2")
    current = get_module_elo(conn, MODULE_A)
    assert current.combat_elo > floor.combat_after
    result = freeze_decrutch_escrow(conn, "q-snap", "s1")
    assert result.applied
    target = settings.K_COMBAT * (1.0 - expected_pass(1200.0, 1600.0))
    floor_delta = floor.combat_after - 1200.0
    pool = _pool(conn, "q-snap")
    assert pool is not None
    assert pool["escrow_combat_delta"] == pytest.approx(target - floor_delta, abs=1e-9)
    wrong = settings.K_COMBAT * (1.0 - expected_pass(current.combat_elo, 1600.0)) - floor_delta
    assert pool["escrow_combat_delta"] != pytest.approx(wrong, abs=1e-3)
    # 覆盖分则基于当前 R_cov 结算
    cov_delta = settings.K_COV * (1.0 - expected_pass(current.coverage_elo, 1600.0))
    assert result.coverage_after == pytest.approx(current.coverage_elo + cov_delta)
    _assert_ledger_chain(conn, MODULE_A)


def test_freeze_explanation_contains_key_numbers(conn: sqlite3.Connection) -> None:
    _make_question(conn, "q-expl", base_elo=1600)
    apply_draft_subgoal_floor(conn, "q-expl", "s1", d_sub=1400)
    result = freeze_decrutch_escrow(conn, "q-expl", "s1")
    cov_delta = result.coverage_after - result.coverage_before
    pool = _pool(conn, "q-expl")
    assert pool is not None
    text = result.explanation_text
    assert text
    assert MODULE_A in text
    assert "1600" in text
    assert f"{cov_delta:.1f}" in text
    assert f"{pool['escrow_combat_delta']:.1f}" in text
    assert "冻结" in text


def test_freeze_missing_question_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="题目不存在"):
        freeze_decrutch_escrow(conn, "ghost", "s1")


# ---------------------------------------------------------------------------
# 混合事件序列：全程不倒挂 + 账本链衔接
# ---------------------------------------------------------------------------


def test_no_inversion_across_mixed_sequence(conn: sqlite3.Connection) -> None:
    _make_question(conn, "m1", base_elo=1600)
    _make_question(conn, "m2", base_elo=2800)
    _make_question(conn, "m3", base_elo=900)
    _make_question(conn, "m4", base_elo=1500)
    _make_question(conn, "m5", base_elo=800)
    _make_question(conn, "mb1", base_elo=1200, module=MODULE_B)
    steps = [
        lambda: apply_draft_subgoal_floor(conn, "m1", "s1", d_sub=1400),
        lambda: freeze_decrutch_escrow(conn, "m1", "s1"),
        lambda: record_mastered_init(conn, "m2", "s2"),
        lambda: record_mastered_init(conn, "m3", "s3"),
        lambda: apply_draft_subgoal_floor(conn, "m4", "s4", d_sub=1350),
        lambda: freeze_decrutch_escrow(conn, "m4", "s4"),
        lambda: record_mastered_init(conn, "m5", "s5"),
        lambda: record_mastered_init(conn, "mb1", "sb1"),
        lambda: freeze_decrutch_escrow(conn, "m1", "s1"),  # 幂等重放
        lambda: apply_draft_subgoal_floor(conn, "m1", "s1", d_sub=1400),  # 幂等重放
    ]
    for step in steps:
        result = step()
        assert result.combat_after <= result.coverage_after
        assert result.combat_before <= result.coverage_before
        for module in (MODULE_A, MODULE_B):
            state = get_module_elo(conn, module)
            assert state.combat_elo <= state.coverage_elo
    _assert_ledger_chain(conn, MODULE_A)
    _assert_ledger_chain(conn, MODULE_B)


# ---------------------------------------------------------------------------
# UNIQUE 兜底竞态 / 零增量守卫 / 熔断防御分支
# ---------------------------------------------------------------------------


def _settle_race(conn: sqlite3.Connection, trigger: str) -> LedgerEventResult:
    if trigger == "MASTERED_INIT":
        return record_mastered_init(conn, "q-race", "s1")
    if trigger == "PARTIAL_DRAFT_SUBGOAL_FLOOR":
        return apply_draft_subgoal_floor(conn, "q-race", "s1", d_sub=1400)
    return freeze_decrutch_escrow(conn, "q-race", "s1")


@pytest.mark.parametrize(
    "trigger",
    ["MASTERED_INIT", "PARTIAL_DRAFT_SUBGOAL_FLOOR", "SCAFFOLD_COVER_GAIN"],
)
def test_unique_backstop_race(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    # 模拟并发竞态：前置幂等查询「失明」漏检，INSERT 撞 UNIQUE 约束后必须
    # 回读既有事件快照（applied=False），且托管池不被二次写入。
    _make_question(conn, "q-race", base_elo=1600)
    first = _settle_race(conn, trigger)
    assert first.applied
    if trigger == "SCAFFOLD_COVER_GAIN":
        # 模拟并发写入方已提交账本事件、但池内结算标志尚未更新的中间态，
        # 使竞态调用穿过熔断闸门直达 INSERT 的 UNIQUE 兜底。
        conn.execute(
            "UPDATE question_escrow_pool SET is_cov_settled = 0 WHERE question_uid = 'q-race'"
        )
    pool_before = _pool(conn, "q-race")
    real_find = elo_engine._find_event_by_key
    blind = {"used": False}

    def blind_find(c: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        if not blind["used"]:
            blind["used"] = True
            return None
        return real_find(c, key)

    monkeypatch.setattr(elo_engine, "_find_event_by_key", blind_find)
    second = _settle_race(conn, trigger)
    assert not second.applied
    assert second.event_id == first.event_id
    assert second.combat_after == first.combat_after
    assert _ledger_count(conn) == 1
    pool_after = _pool(conn, "q-race")
    if pool_before is None:
        assert pool_after is None
    else:
        assert pool_after is not None
        assert pool_after["floor_elo_applied"] == pool_before["floor_elo_applied"]
        assert pool_after["escrow_combat_delta"] == pool_before["escrow_combat_delta"]
        assert pool_after["is_cov_settled"] == pool_before["is_cov_settled"]


@pytest.mark.parametrize(
    "trigger",
    ["MASTERED_INIT", "PARTIAL_DRAFT_SUBGOAL_FLOOR", "SCAFFOLD_COVER_GAIN"],
)
def test_non_idempotent_integrity_error_propagates(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    # 非幂等键冲突的 IntegrityError（如外键）不得被吞掉，必须原样抛出
    _make_question(conn, "q-race", base_elo=1600)

    def boom(*args: object, **kwargs: object) -> int:
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    monkeypatch.setattr(elo_engine, "_insert_event", boom)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _settle_race(conn, trigger)


@pytest.mark.parametrize("k_setting", ["K_INIT", "K_FLOOR", "K_COV"])
def test_zero_delta_writes_no_event(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, k_setting: str
) -> None:
    # 契约规则 4：delta=0 不写事件，before==after，不建托管池
    _make_question(conn, "q-zero", base_elo=1600)
    monkeypatch.setattr(settings, k_setting, 0.0)
    if k_setting == "K_INIT":
        result = record_mastered_init(conn, "q-zero", "s1")
    elif k_setting == "K_FLOOR":
        result = apply_draft_subgoal_floor(conn, "q-zero", "s1", d_sub=1400)
    else:
        result = freeze_decrutch_escrow(conn, "q-zero", "s1")
    assert not result.applied
    assert result.event_id is None
    assert result.combat_before == result.combat_after
    assert result.coverage_before == result.coverage_after
    assert _ledger_count(conn) == 0
    assert _pool(conn, "q-zero") is None


def test_freeze_breaker_without_prior_event(conn: sqlite3.Connection) -> None:
    # 防御分支：池被外部置为已结算但账本无对应事件 → 熔断 no-op，不写事件
    _make_question(conn, "q-manual", base_elo=1600)
    conn.execute(
        "INSERT INTO question_escrow_pool (question_uid, l1_module,"
        " initial_combat_elo_snapshot, floor_elo_applied, escrow_combat_delta,"
        " is_cov_settled, decay_factor, escrow_status)"
        " VALUES ('q-manual', ?, 1200.0, 0.0, 0.0, 1, 1.0, 'FROZEN_PENDING_BLIND')",
        (MODULE_A,),
    )
    result = freeze_decrutch_escrow(conn, "q-manual", "s1")
    assert not result.applied
    assert result.event_id is None
    assert result.coverage_before == result.coverage_after == 1200.0
    assert _ledger_count(conn) == 0


# ---------------------------------------------------------------------------
# 事务与数据类契约
# ---------------------------------------------------------------------------


def test_functions_never_commit(tmp_db: Path) -> None:
    # 使用默认 tuple row_factory 的裸连接：引擎不依赖调用方 row_factory，
    # 且写入停留在调用方事务内（不自行 commit）。
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _make_question(conn, "q-tx", base_elo=1600)
        result = record_mastered_init(conn, "q-tx", "s1")
        assert result.applied
        assert conn.in_transaction
        other = sqlite3.connect(tmp_db)
        try:
            row = other.execute("SELECT COUNT(*) FROM elo_mutation_ledger").fetchone()
            assert row is not None and row[0] == 0  # 未 commit，外部不可见
        finally:
            other.close()
        conn.rollback()  # 调用方回滚 → 事件与题干一并消失（多表原子）
        row = conn.execute("SELECT COUNT(*) FROM elo_mutation_ledger").fetchone()
        assert row is not None and row[0] == 0
        _make_question(conn, "q-tx", base_elo=1600)
        record_mastered_init(conn, "q-tx", "s1")
        conn.commit()  # 调用方 commit → 持久化
        other = sqlite3.connect(tmp_db)
        try:
            row = other.execute("SELECT COUNT(*) FROM elo_mutation_ledger").fetchone()
            assert row is not None and row[0] == 1
        finally:
            other.close()
    finally:
        conn.close()


def test_result_dataclasses_are_frozen() -> None:
    state = DualEloState(coverage_elo=1200.0, combat_elo=1200.0)
    with pytest.raises(FrozenInstanceError):
        state.coverage_elo = 1300.0  # type: ignore[misc]
    result = LedgerEventResult(
        event_id=None,
        applied=False,
        coverage_before=1200.0,
        coverage_after=1200.0,
        combat_before=1200.0,
        combat_after=1200.0,
        explanation_text="",
    )
    with pytest.raises(FrozenInstanceError):
        result.applied = True  # type: ignore[misc]
