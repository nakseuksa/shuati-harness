"""Phase 1：四维难度 Rubric 与子步单调性裁剪（clamp）测试。

规格：docs/designs/adaptive_practice_harness.md §3.1 第 2 条、Target File #6/#15。
Phase 3 切片 B 已在文件尾追加 Elo/escrow 全流程守恒测试（设计 §3.5 法则 3、
Target File #11/#15、CUJ 3）；既有 rubric/clamp 测试保持不变。
"""

import itertools
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.services import elo_engine
from backend.services.difficulty_rubric import calculate_base_elo, clamp_substep_difficulty
from backend.services.elo_engine import (
    DualEloState,
    apply_draft_subgoal_floor,
    apply_overdue_escrow_decay,
    expected_pass,
    freeze_decrutch_escrow,
    get_module_elo,
    realize_or_forfeit_blind_test,
    record_mastered_init,
    replay_module_elo_from_ledger,
)

# 8 档 tier 全集（字符串一字不差）
ALL_TIERS = {
    "L1_基础直达",
    "L2_常规巩固",
    "L3_综合中档",
    "L4-A_进阶拔高(繁)",
    "L4-B_进阶拔高(巧)",
    "L5-1_压轴挑战",
    "L5-2_压轴巅峰",
    "L5-3_殿堂极限",
}

# 4×3×3×3 = 108 种合法四维全组合
ALL_CSTL = list(itertools.product(range(1, 5), range(1, 4), range(1, 4), range(1, 4)))


# ---------------------------------------------------------------------------
# Rubric：PM 已核验向量（必须逐条断言）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cstl", "expected_elo", "expected_tier"),
    [
        pytest.param((1, 1, 1, 1), 1120, "L1_基础直达", id="c1-min-L1"),
        pytest.param((2, 3, 1, 3), 1630, "L4-A_进阶拔高(繁)", id="c2-cap-L4A-fuse"),
        pytest.param((4, 3, 3, 3), 2800, "L5-3_殿堂极限", id="c4-max-upper-clamp"),
        pytest.param((3, 3, 2, 3), 2085, None, id="cuj1-example-2085"),
        pytest.param((1, 1, 1, 3), 1250, "L1_基础直达", id="rule1-priority-over-score"),
        pytest.param((3, 2, 3, 2), 2030, "L4-B_进阶拔高(巧)", id="rule5-priority-over-rule6"),
        pytest.param((3, 1, 1, 1), 1630, "L3_综合中档", id="rule7-fallback"),
        pytest.param((4, 1, 1, 1), 2310, "L5-2_压轴巅峰", id="c4-min-L52"),
        pytest.param((4, 2, 2, 1), 2530, "L5-2_压轴巅峰", id="below-2550-boundary"),
        pytest.param((4, 2, 2, 2), 2595, "L5-3_殿堂极限", id="above-2550-boundary"),
    ],
)
def test_rubric_pm_verified_vectors(
    cstl: tuple[int, int, int, int], expected_elo: int, expected_tier: str | None
) -> None:
    result = calculate_base_elo(*cstl)
    assert result["base_elo"] == expected_elo
    if expected_tier is not None:
        assert result["tier"] == expected_tier


def test_rubric_rule1_matches_any_l() -> None:
    """规则 1：C=S=T=1 时 L 任意均判 L1，且优先于分数阶梯。"""
    for load in (1, 2, 3):
        result = calculate_base_elo(1, 1, 1, load)
        assert result["tier"] == "L1_基础直达"
    # (1,1,1,3)=1250 < 1400，若不按序匹配会误判 L2
    assert calculate_base_elo(1, 1, 1, 3)["base_elo"] == 1250


def test_rubric_c2_cap_never_reaches_l5() -> None:
    """C≤2 封顶熔断：任何 C≤2 组合都不得进入 L5 档。"""
    allowed = {"L1_基础直达", "L2_常规巩固", "L3_综合中档", "L4-A_进阶拔高(繁)"}
    for c, s, t, load in itertools.product((1, 2), range(1, 4), range(1, 4), range(1, 4)):
        assert calculate_base_elo(c, s, t, load)["tier"] in allowed


@pytest.mark.parametrize(
    ("cstl", "expected_elo", "expected_tier"),
    [
        pytest.param((1, 2, 1, 1), 1225, "L2_常规巩固", id="c1-L2"),
        pytest.param((2, 1, 1, 2), 1355, "L2_常规巩固", id="c2-below-1400"),
        pytest.param((2, 1, 1, 3), 1420, "L3_综合中档", id="c2-at-least-1400"),
        pytest.param((2, 3, 1, 2), 1565, "L3_综合中档", id="c2-below-1600"),
        pytest.param((1, 3, 3, 3), 1690, "L4-A_进阶拔高(繁)", id="c1-can-reach-L4A"),
        pytest.param((3, 2, 1, 1), 1735, "L3_综合中档", id="c3-below-1850-fallback"),
        pytest.param((3, 2, 2, 1), 1850, "L5-1_压轴挑战", id="c3-exactly-1850"),
        pytest.param((3, 1, 3, 1), 1860, "L4-B_进阶拔高(巧)", id="c3-rule5-low-elo"),
        pytest.param((3, 3, 3, 2), 2135, "L4-B_进阶拔高(巧)", id="c3-rule5-high-elo"),
    ],
)
def test_rubric_tier_ladder_boundaries(
    cstl: tuple[int, int, int, int], expected_elo: int, expected_tier: str
) -> None:
    result = calculate_base_elo(*cstl)
    assert result["base_elo"] == expected_elo
    assert result["tier"] == expected_tier


def test_rubric_all_108_combinations() -> None:
    """全组合遍历：公式逐点核对、800≤elo≤2800、tier∈8 档集合、关键不变量。"""
    assert len(ALL_CSTL) == 108
    seen_tiers: set[str] = set()
    for c, s, t, load in ALL_CSTL:
        result = calculate_base_elo(c, s, t, load)
        elo = result["base_elo"]
        tier = result["tier"]
        assert isinstance(elo, int)
        assert isinstance(tier, str)
        expected = min(2800, max(800, 750 + 85 * (2**c - 1) + 105 * s + 115 * t + 65 * load))
        assert elo == expected
        assert 800 <= elo <= 2800
        assert tier in ALL_TIERS
        seen_tiers.add(tier)
        if c <= 2:
            assert tier not in {"L5-1_压轴挑战", "L5-2_压轴巅峰", "L5-3_殿堂极限"}
        if c == 1 and s == 1 and t == 1:
            assert tier == "L1_基础直达"
    # 8 档在合法输入空间内全部可达
    assert seen_tiers == ALL_TIERS


# ---------------------------------------------------------------------------
# Rubric：输入校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cstl",
    [
        (0, 1, 1, 1),
        (5, 1, 1, 1),
        (-1, 1, 1, 1),
        (1, 0, 1, 1),
        (1, 4, 1, 1),
        (1, -2, 1, 1),
        (1, 1, 0, 1),
        (1, 1, 4, 1),
        (1, 1, 1, 0),
        (1, 1, 1, 4),
    ],
)
def test_rubric_rejects_out_of_range(cstl: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError):
        calculate_base_elo(*cstl)


@pytest.mark.parametrize("bad", [True, False, 1.0, 2.5, "1", None])
def test_rubric_rejects_bool_and_non_int(bad: object) -> None:
    for pos in range(4):
        cstl: list[object] = [1, 1, 1, 1]
        cstl[pos] = bad
        with pytest.raises(ValueError):
            calculate_base_elo(*cstl)


# ---------------------------------------------------------------------------
# Clamp：PM 已核验向量与边界
# ---------------------------------------------------------------------------


def test_clamp_pm_verified_vector() -> None:
    """parent=(1,1,1,1)/1120，raw_sub=(4,3,3,3) → (1,1,1,1)，D_sub=970。"""
    sub, d_sub = clamp_substep_difficulty((1, 1, 1, 1), 1120, (4, 3, 3, 3))
    assert sub == (1, 1, 1, 1)
    assert d_sub == 970


@pytest.mark.parametrize("parent", [(1, 1, 1, 1), (2, 1, 3, 2), (4, 1, 2, 3)])
@pytest.mark.parametrize("raw", [(4, 3, 3, 3), (2, 3, 2, 2), (9, 9, 9, 9), (0, -1, 2, 0)])
def test_clamp_parent_s1_caps_sub_s(
    parent: tuple[int, int, int, int], raw: tuple[int, int, int, int]
) -> None:
    """parent S=1 时 S_sub 恒 ≤1（下界保护下恒等于 1）。"""
    parent_elo = calculate_base_elo(*parent)["base_elo"]
    assert isinstance(parent_elo, int)
    sub, d_sub = clamp_substep_difficulty(parent, parent_elo, raw)
    assert sub[1] == 1
    assert 800 <= d_sub <= 2650


@pytest.mark.parametrize(
    ("parent_elo", "expected_d_sub"),
    [(800, 800), (900, 800), (949, 800), (950, 800), (951, 801), (1120, 970)],
)
def test_clamp_low_parent_elo_floor_800(parent_elo: int, expected_d_sub: int) -> None:
    """parent_elo<950 时 parent_elo-150<800，D_sub 触发 800 下界。"""
    sub, d_sub = clamp_substep_difficulty((1, 1, 1, 1), parent_elo, (1, 1, 1, 1))
    assert sub == (1, 1, 1, 1)
    assert d_sub == expected_d_sub


@pytest.mark.parametrize(
    ("raw", "expected_sub"),
    [
        ((0, 0, 0, 0), (1, 1, 1, 1)),
        ((-3, -1, 0, -2), (1, 1, 1, 1)),
        ((1, -5, 2, 0), (1, 1, 2, 1)),
    ],
)
def test_clamp_raw_below_one_floored(
    raw: tuple[int, int, int, int], expected_sub: tuple[int, int, int, int]
) -> None:
    """raw_sub 含 0 或负数 → 下界 1 不崩。"""
    sub, d_sub = clamp_substep_difficulty((4, 3, 3, 3), 2800, raw)
    assert sub == expected_sub
    assert 800 <= d_sub <= 2650


def test_clamp_raw_above_upper_bounds_clamped_first() -> None:
    """raw_sub 超上限 → 先夹进合法范围，再做单调性裁剪。"""
    sub, d_sub = clamp_substep_difficulty((4, 3, 3, 3), 2800, (9, 9, 9, 9))
    assert sub == (3, 2, 3, 3)
    # base_elo(3,2,3,3)=2095；min(2650, 2800-150, 2095)=2095
    assert d_sub == 2095
    sub2, d_sub2 = clamp_substep_difficulty((2, 2, 2, 2), 1575, (9, 9, 9, 9))
    assert sub2 == (1, 1, 2, 2)
    # base_elo(1,1,2,2)=1300；min(2650, 1425, 1300)=1300
    assert d_sub2 == 1300


def test_clamp_monotonicity_and_check_range_exhaustive() -> None:
    """108 种 parent × (108 种合法 raw + 极端 raw) 全遍历：

    - 子步四维恒在合法范围且满足单调性硬约束；
    - D_sub 恒满足 SQLite CHECK（800≤D_sub≤2650）；
    - D_sub 逐点等于 max(800, min(2650, parent_elo-150, base_elo(sub)))。
    """
    extreme_raws = [(0, 0, 0, 0), (-1, -2, -3, -4), (99, 99, 99, 99), (4, 3, 3, 3)]
    raws = ALL_CSTL + extreme_raws
    for parent in ALL_CSTL:
        parent_elo = calculate_base_elo(*parent)["base_elo"]
        assert isinstance(parent_elo, int)
        pc, ps, pt, pl = parent
        for raw in raws:
            sub, d_sub = clamp_substep_difficulty(parent, parent_elo, raw)
            sc, ss, st, sl = sub
            assert 1 <= sc <= 4
            assert 1 <= ss <= 3
            assert 1 <= st <= 3
            assert 1 <= sl <= 3
            assert sc <= max(1, pc - 1)
            assert ss <= max(1, ps - 1)
            assert st <= pt
            assert sl <= pl
            assert 800 <= d_sub <= 2650
            sub_base = calculate_base_elo(*sub)["base_elo"]
            assert isinstance(sub_base, int)
            assert d_sub == max(800, min(2650, parent_elo - 150, sub_base))


# ---------------------------------------------------------------------------
# Clamp：输入校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parent",
    [(0, 1, 1, 1), (5, 1, 1, 1), (1, 4, 1, 1), (1, 1, 1, 0), (True, 1, 1, 1)],
)
def test_clamp_rejects_invalid_parent(parent: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError):
        clamp_substep_difficulty(parent, 1500, (1, 1, 1, 1))


@pytest.mark.parametrize("bad", [1.5, "2", None, True])
def test_clamp_rejects_non_int_raw(bad: object) -> None:
    raw = (bad, 1, 1, 1)
    with pytest.raises(ValueError):
        clamp_substep_difficulty((2, 2, 2, 2), 1575, raw)


# ===========================================================================
# Phase 3 切片 B：Elo / Escrow 全流程守恒、forfeit、温和衰减与确定性重放
# 规格：docs/designs/adaptive_practice_harness.md §3.5 法则 3、Target File #11/#15、CUJ 3。
# 测试数据一律直接 SQL 构造 questions/escrow 行 + 调用切片 A/B 函数，不依赖 ingest/scaffold。
# ===========================================================================

ELO_MODULE = "电磁学"


@pytest.fixture()
def elo_conn(tmp_db: Path) -> Iterator[sqlite3.Connection]:
    """调用方管理的连接（get_db_connection 统一 commit/rollback）。"""
    with get_db_connection(tmp_db) as c:
        yield c


def _elo_make_question(
    conn: sqlite3.Connection,
    uid: str,
    *,
    base_elo: int,
    module: str = ELO_MODULE,
    mastery: str = "ACTIVE",
) -> None:
    conn.execute(
        "INSERT INTO questions (question_uid, origin_type, target_grade_and_subject,"
        " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
        " base_elo, tier, primary_l1_module, mastery_state)"
        " VALUES (?, 'USER_CANONICAL', '高中物理-高二', '题干', ?, '42', 2, 2, 2, 2,"
        " ?, 'L3_综合中档', ?, ?)",
        (uid, f"hash-{uid}", base_elo, module, mastery),
    )


def _elo_pool(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    cur = conn.execute("SELECT * FROM question_escrow_pool WHERE question_uid = ?", (uid,))
    cur.row_factory = sqlite3.Row
    row = cur.fetchone()
    assert row is not None, f"托管池不存在：{uid}"
    return row


def _elo_events(conn: sqlite3.Connection, module: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM elo_mutation_ledger WHERE l1_module = ? ORDER BY event_id", (module,)
    )
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


def _elo_set_slated(
    conn: sqlite3.Connection, uid: str, *, drill_date: str | None, exposure: int
) -> None:
    conn.execute(
        "UPDATE questions SET slated_drill_date = ?, slated_exposure_count = ?"
        " WHERE question_uid = ?",
        (drill_date, exposure, uid),
    )


def _elo_assert_chain(conn: sqlite3.Connection, module: str) -> None:
    """账本链完整性：冷启动起点、before/after 逐事件衔接、任何事件后不倒挂。"""
    prev_cov = prev_combat = settings.MODULE_ELO_INITIAL
    for row in _elo_events(conn, module):
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


def _elo_setup_decay_question(
    conn: sqlite3.Connection,
    uid: str,
    *,
    exposure: int,
    drill_date: str | None,
    base_elo: int = 1600,
) -> None:
    """构造一道已冻结待盲测、PENDING_RETEST、指定曝光/排期的题（直接 SQL + 切片 A）。"""
    _elo_make_question(conn, uid, base_elo=base_elo, mastery="PENDING_RETEST")
    apply_draft_subgoal_floor(conn, uid, "s1", d_sub=1400)
    freeze_decrutch_escrow(conn, uid, "s1")
    _elo_set_slated(conn, uid, drill_date=drill_date, exposure=exposure)


# ---------------------------------------------------------------------------
# 四阶段全流程守恒（草稿保底 → 拆拐杖冻结 → 盲测兑现）与直接独立做对对照
# ---------------------------------------------------------------------------


def test_elo_full_flow_conservation_and_direct_comparison(elo_conn: sqlite3.Connection) -> None:
    """同一错题走完 Elo 全流程后 combat 净增量 ≡ K_COMBAT·(1-E(snapshot,D_orig))。

    脚手架通关属宏观 FSM（不产生 Elo 事件），Elo 相关阶段为保底→冻结→兑现。
    与「直接独立做对」（record_mastered_init，fresh 模块）严格守恒对照。
    """
    conn = elo_conn
    d_orig, d_sub = 1600, 1400
    _elo_make_question(conn, "q-flow", base_elo=d_orig)
    initial = get_module_elo(conn, ELO_MODULE)
    assert initial == DualEloState(1200.0, 1200.0)
    snapshot = initial.combat_elo

    floor = apply_draft_subgoal_floor(conn, "q-flow", "s1", d_sub=d_sub)
    assert floor.applied
    floor_delta = floor.combat_after - floor.combat_before

    freeze = freeze_decrutch_escrow(conn, "q-flow", "s1")
    assert freeze.applied
    pool = _elo_pool(conn, "q-flow")
    escrow = float(pool["escrow_combat_delta"])
    target = settings.K_COMBAT * (1.0 - expected_pass(snapshot, d_orig))
    assert escrow == pytest.approx(max(0.0, target - floor_delta), abs=1e-9)
    assert pool["decay_factor"] == 1.0

    realize = realize_or_forfeit_blind_test(conn, "q-flow", "s-blind", passed=True)
    assert realize.applied
    assert _elo_pool(conn, "q-flow")["escrow_status"] == "REALIZED_MASTERED"

    final = get_module_elo(conn, ELO_MODULE)
    net = final.combat_elo - snapshot
    assert net == pytest.approx(target, abs=1e-9)
    assert floor_delta + escrow == pytest.approx(target, abs=1e-9)

    # 对照：另一 fresh 模块直接独立做对同难度题（γ=1，K_INIT=K_COMBAT）
    _elo_make_question(conn, "q-direct", base_elo=d_orig, module="对照模块")
    direct = record_mastered_init(conn, "q-direct", "s-direct")
    assert direct.applied
    direct_delta = direct.combat_after - direct.combat_before
    assert direct_delta == pytest.approx(target, abs=1e-9)
    assert net == pytest.approx(direct_delta, abs=1e-9)
    _elo_assert_chain(conn, ELO_MODULE)


def test_elo_conservation_without_floor(elo_conn: sqlite3.Connection) -> None:
    """无保底题：冻结差额即整题目标增量，兑现后净增量 ≡ target。"""
    conn = elo_conn
    _elo_make_question(conn, "q-nf", base_elo=1600)
    freeze_decrutch_escrow(conn, "q-nf", "s1")
    target = settings.K_COMBAT * (1.0 - expected_pass(1200.0, 1600.0))
    assert _elo_pool(conn, "q-nf")["escrow_combat_delta"] == pytest.approx(target, abs=1e-9)
    realize_or_forfeit_blind_test(conn, "q-nf", "s-blind", passed=True)
    net = get_module_elo(conn, ELO_MODULE).combat_elo - 1200.0
    assert net == pytest.approx(target, abs=1e-9)
    _elo_assert_chain(conn, ELO_MODULE)


def test_elo_realize_with_decay_factor_conservation(elo_conn: sqlite3.Connection) -> None:
    """decay_factor=0.7：兑现=escrow×0.7，守恒式相应折减，floor 不受影响。"""
    conn = elo_conn
    d_orig, d_sub = 1600, 1400
    _elo_make_question(conn, "q-dec", base_elo=d_orig)
    floor = apply_draft_subgoal_floor(conn, "q-dec", "s1", d_sub=d_sub)
    floor_delta = floor.combat_after - floor.combat_before
    freeze_decrutch_escrow(conn, "q-dec", "s1")
    pool = _elo_pool(conn, "q-dec")
    escrow = float(pool["escrow_combat_delta"])
    snapshot = float(pool["initial_combat_elo_snapshot"])
    target = settings.K_COMBAT * (1.0 - expected_pass(snapshot, d_orig))

    conn.execute("UPDATE question_escrow_pool SET decay_factor=0.7 WHERE question_uid='q-dec'")
    realize = realize_or_forfeit_blind_test(conn, "q-dec", "s-blind", passed=True)
    assert realize.applied
    realized_delta = realize.combat_after - realize.combat_before
    assert realized_delta == pytest.approx(escrow * 0.7, abs=1e-9)

    pool2 = _elo_pool(conn, "q-dec")
    assert pool2["floor_elo_applied"] == pytest.approx(floor_delta, abs=1e-9)  # floor 不受影响
    net = get_module_elo(conn, ELO_MODULE).combat_elo - snapshot
    assert net == pytest.approx(floor_delta + escrow * 0.7, abs=1e-9)
    assert net == pytest.approx(target - escrow * 0.3, abs=1e-9)
    assert net < target  # 折减
    _elo_assert_chain(conn, ELO_MODULE)


# ---------------------------------------------------------------------------
# 全程不倒挂：混合事件序列（含 realize/forfeit/decay）每步 combat<=coverage
# ---------------------------------------------------------------------------


def test_elo_no_inversion_across_full_mixed_sequence(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    today = date(2026, 11, 12)
    for uid, elo in [("n1", 1600), ("n2", 1500), ("n3", 1700), ("n4", 2800), ("n5", 900)]:
        _elo_make_question(conn, uid, base_elo=elo)

    def check() -> None:
        state = get_module_elo(conn, ELO_MODULE)
        assert state.combat_elo <= state.coverage_elo
        _elo_assert_chain(conn, ELO_MODULE)

    steps = [
        lambda: apply_draft_subgoal_floor(conn, "n1", "s1", d_sub=1400),
        lambda: freeze_decrutch_escrow(conn, "n1", "s1"),
        lambda: apply_draft_subgoal_floor(conn, "n2", "s2", d_sub=1350),
        lambda: freeze_decrutch_escrow(conn, "n2", "s2"),
        lambda: realize_or_forfeit_blind_test(conn, "n2", "s2b", passed=False),  # forfeit
        lambda: freeze_decrutch_escrow(conn, "n3", "s3"),  # 无保底
        lambda: record_mastered_init(conn, "n4", "s4"),
        lambda: record_mastered_init(conn, "n5", "s5"),
    ]
    for step in steps:
        step()
        check()

    conn.execute("UPDATE questions SET mastery_state='PENDING_RETEST' WHERE question_uid='n3'")
    _elo_set_slated(conn, "n3", drill_date=today.isoformat(), exposure=4)
    assert apply_overdue_escrow_decay(conn, today) == 1
    check()

    realize_or_forfeit_blind_test(conn, "n1", "s1b", passed=True)
    check()
    realize_or_forfeit_blind_test(conn, "n3", "s3b", passed=True)
    check()
    # 幂等重放不破坏不变量
    freeze_decrutch_escrow(conn, "n1", "s1")
    check()
    realize_or_forfeit_blind_test(conn, "n1", "s1b", passed=True)
    check()
    _elo_assert_chain(conn, ELO_MODULE)


# ---------------------------------------------------------------------------
# forfeit：escrow 清零、combat/cov 不变、状态 FORFEITED_RESET、再调用幂等
# ---------------------------------------------------------------------------


def test_elo_forfeit_clears_escrow_and_idempotent(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    _elo_make_question(conn, "q-for", base_elo=1600)
    apply_draft_subgoal_floor(conn, "q-for", "s1", d_sub=1400)
    freeze_decrutch_escrow(conn, "q-for", "s1")
    before = get_module_elo(conn, ELO_MODULE)
    assert _elo_pool(conn, "q-for")["escrow_combat_delta"] > 0
    count_before = len(_elo_events(conn, ELO_MODULE))

    result = realize_or_forfeit_blind_test(conn, "q-for", "s-blind", passed=False)
    assert result.event_id is None
    assert result.combat_before == result.combat_after
    assert result.coverage_before == result.coverage_after
    assert get_module_elo(conn, ELO_MODULE) == before  # combat/cov 分文不动
    pool = _elo_pool(conn, "q-for")
    assert pool["escrow_combat_delta"] == 0.0
    assert pool["escrow_status"] == "FORFEITED_RESET"
    assert len(_elo_events(conn, ELO_MODULE)) == count_before  # 不写账本事件

    # 再调用幂等（同会话 / 跨会话 / 甚至 passed=True 都对终态无操作）
    again = realize_or_forfeit_blind_test(conn, "q-for", "s-blind", passed=False)
    assert not again.applied and again.event_id is None
    cross = realize_or_forfeit_blind_test(conn, "q-for", "s-other", passed=True)
    assert not cross.applied
    pool2 = _elo_pool(conn, "q-for")
    assert pool2["escrow_status"] == "FORFEITED_RESET" and pool2["escrow_combat_delta"] == 0.0
    assert get_module_elo(conn, ELO_MODULE) == before
    assert len(_elo_events(conn, ELO_MODULE)) == count_before


def test_elo_realize_idempotent(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    _elo_make_question(conn, "q-ri", base_elo=1600)
    apply_draft_subgoal_floor(conn, "q-ri", "s1", d_sub=1400)
    freeze_decrutch_escrow(conn, "q-ri", "s1")
    first = realize_or_forfeit_blind_test(conn, "q-ri", "s-blind", passed=True)
    assert first.applied
    count = len(_elo_events(conn, ELO_MODULE))
    state = get_module_elo(conn, ELO_MODULE)
    second = realize_or_forfeit_blind_test(conn, "q-ri", "s-blind", passed=True)
    assert not second.applied
    assert second.event_id == first.event_id
    assert second.combat_after == first.combat_after
    third = realize_or_forfeit_blind_test(conn, "q-ri", "s-other", passed=True)
    assert not third.applied
    assert len(_elo_events(conn, ELO_MODULE)) == count
    assert get_module_elo(conn, ELO_MODULE) == state


# ---------------------------------------------------------------------------
# 温和超时衰减：曝光阈值 / 当日排期 / 同日幂等 / 下限 0 / 基线逐事件不变
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exposure", "expected_count", "expected_factor"),
    [
        pytest.param(4, 1, 0.85, id="exposure4-decay"),
        pytest.param(3, 0, 1.0, id="exposure3-no-decay"),
        pytest.param(5, 1, 0.85, id="exposure5-decay"),
    ],
)
def test_elo_decay_exposure_threshold(
    elo_conn: sqlite3.Connection, exposure: int, expected_count: int, expected_factor: float
) -> None:
    conn = elo_conn
    today = date(2026, 11, 12)
    _elo_setup_decay_question(conn, "q-d", exposure=exposure, drill_date=today.isoformat())
    before = get_module_elo(conn, ELO_MODULE)
    n = apply_overdue_escrow_decay(conn, today)
    assert n == expected_count
    assert _elo_pool(conn, "q-d")["decay_factor"] == pytest.approx(expected_factor)
    assert get_module_elo(conn, ELO_MODULE) == before  # 基线不变
    decay_events = [
        e for e in _elo_events(conn, ELO_MODULE) if e["mutation_trigger"] == "ESCROW_TIMEOUT_DECAY"
    ]
    assert len(decay_events) == expected_count
    for e in decay_events:
        assert e["combat_elo_before"] == e["combat_elo_after"]
        assert e["coverage_elo_before"] == e["coverage_elo_after"]
        assert e["watermark_gamma"] == pytest.approx(expected_factor)
        assert e["explanation_text"]


@pytest.mark.parametrize("drill_date", [None, "2026-11-11"], ids=["never-slated", "deferred-day"])
def test_elo_decay_skips_not_slated_today(
    elo_conn: sqlite3.Connection, drill_date: str | None
) -> None:
    """系统顺延题当日未排入（NULL 或旧日期）→ 天然冻结，不得衰减。"""
    conn = elo_conn
    today = date(2026, 11, 12)
    _elo_setup_decay_question(conn, "q-def", exposure=4, drill_date=drill_date)
    before = get_module_elo(conn, ELO_MODULE)
    assert apply_overdue_escrow_decay(conn, today) == 0
    assert _elo_pool(conn, "q-def")["decay_factor"] == 1.0
    assert get_module_elo(conn, ELO_MODULE) == before
    assert not [
        e for e in _elo_events(conn, ELO_MODULE) if e["mutation_trigger"] == "ESCROW_TIMEOUT_DECAY"
    ]


def test_elo_decay_same_day_idempotent(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    today = date(2026, 11, 12)
    _elo_setup_decay_question(conn, "q-idem", exposure=4, drill_date=today.isoformat())
    assert apply_overdue_escrow_decay(conn, today) == 1
    factor1 = _elo_pool(conn, "q-idem")["decay_factor"]
    count1 = len(_elo_events(conn, ELO_MODULE))
    before = get_module_elo(conn, ELO_MODULE)
    assert apply_overdue_escrow_decay(conn, today) == 0  # 同日二次不重复衰减
    assert _elo_pool(conn, "q-idem")["decay_factor"] == factor1
    assert len(_elo_events(conn, ELO_MODULE)) == count1
    assert get_module_elo(conn, ELO_MODULE) == before


def test_elo_decay_floor_at_zero(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    today = date(2026, 11, 12)
    _elo_setup_decay_question(conn, "q-zero", exposure=4, drill_date=today.isoformat())
    conn.execute("UPDATE question_escrow_pool SET decay_factor=0.1 WHERE question_uid='q-zero'")
    before = get_module_elo(conn, ELO_MODULE)
    assert apply_overdue_escrow_decay(conn, today) == 1
    assert _elo_pool(conn, "q-zero")["decay_factor"] == 0.0  # max(0, 0.1-0.15)=0
    assert get_module_elo(conn, ELO_MODULE) == before


def test_elo_decay_multi_day_reaches_zero_never_negative(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    start = date(2026, 11, 12)
    _elo_setup_decay_question(conn, "q-multi", exposure=4, drill_date=start.isoformat())
    before = get_module_elo(conn, ELO_MODULE)
    factors: list[float] = []
    for i in range(9):
        day = start + timedelta(days=i)
        _elo_set_slated(conn, "q-multi", drill_date=day.isoformat(), exposure=4 + i)
        assert apply_overdue_escrow_decay(conn, day) == 1
        factor = float(_elo_pool(conn, "q-multi")["decay_factor"])
        factors.append(factor)
        assert factor >= 0.0
        assert get_module_elo(conn, ELO_MODULE) == before  # 基线逐事件不变
    assert factors[6] == pytest.approx(0.0)  # 1.0 - 0.15*7 < 0 → 截断为 0
    assert factors[7] == 0.0 and factors[8] == 0.0
    assert all(a >= b for a, b in zip(factors, factors[1:], strict=False))
    _elo_assert_chain(conn, ELO_MODULE)


def test_elo_decay_requires_pending_retest_and_frozen(elo_conn: sqlite3.Connection) -> None:
    """mastery_state 非 PENDING_RETEST 或池非 FROZEN 均不衰减。"""
    conn = elo_conn
    today = date(2026, 11, 12)
    # ACTIVE 状态（即便已冻结、当日排期、高曝光）不衰减
    _elo_make_question(conn, "q-active", base_elo=1600, mastery="ACTIVE")
    apply_draft_subgoal_floor(conn, "q-active", "s1", d_sub=1400)
    freeze_decrutch_escrow(conn, "q-active", "s1")
    _elo_set_slated(conn, "q-active", drill_date=today.isoformat(), exposure=4)
    assert apply_overdue_escrow_decay(conn, today) == 0
    assert _elo_pool(conn, "q-active")["decay_factor"] == 1.0


# ---------------------------------------------------------------------------
# replay：与 get_module_elo 一致 / 篡改中间 after 断链 / 空模块 1200
# ---------------------------------------------------------------------------


def test_elo_replay_matches_get_module_elo_all_triggers(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    today = date(2026, 11, 12)
    _elo_make_question(conn, "r1", base_elo=1600, mastery="PENDING_RETEST")
    _elo_make_question(conn, "r2", base_elo=2000)
    apply_draft_subgoal_floor(conn, "r1", "s1", d_sub=1400)
    freeze_decrutch_escrow(conn, "r1", "s1")
    _elo_set_slated(conn, "r1", drill_date=today.isoformat(), exposure=4)
    assert apply_overdue_escrow_decay(conn, today) == 1
    record_mastered_init(conn, "r2", "s2")
    realize_or_forfeit_blind_test(conn, "r1", "s1b", passed=True)
    triggers = {e["mutation_trigger"] for e in _elo_events(conn, ELO_MODULE)}
    assert triggers == {
        "MASTERED_INIT",
        "PARTIAL_DRAFT_SUBGOAL_FLOOR",
        "SCAFFOLD_COVER_GAIN",
        "BLIND_TEST_REALIZE",
        "ESCROW_TIMEOUT_DECAY",
    }
    state = get_module_elo(conn, ELO_MODULE)
    rebuilt = replay_module_elo_from_ledger(conn, ELO_MODULE)
    assert rebuilt == state
    assert replay_module_elo_from_ledger(conn, ELO_MODULE) == rebuilt  # 确定性
    assert rebuilt.combat_elo <= rebuilt.coverage_elo
    _elo_assert_chain(conn, ELO_MODULE)


def test_elo_replay_tampered_middle_after_raises(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    _elo_make_question(conn, "t1", base_elo=1600)
    _elo_make_question(conn, "t2", base_elo=2000)
    apply_draft_subgoal_floor(conn, "t1", "s1", d_sub=1400)
    freeze_decrutch_escrow(conn, "t1", "s1")
    record_mastered_init(conn, "t2", "s2")
    realize_or_forfeit_blind_test(conn, "t1", "s1b", passed=True)
    events = _elo_events(conn, ELO_MODULE)
    assert len(events) >= 3
    mid = events[1]
    conn.execute(
        "UPDATE elo_mutation_ledger SET combat_elo_after = combat_elo_after + 50"
        " WHERE event_id = ?",
        (mid["event_id"],),
    )
    with pytest.raises(ValueError, match="断链"):
        replay_module_elo_from_ledger(conn, ELO_MODULE)


def test_elo_replay_empty_module(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    state = replay_module_elo_from_ledger(conn, "不存在的模块")
    assert state == DualEloState(1200.0, 1200.0)
    assert state == get_module_elo(conn, "不存在的模块")


# ---------------------------------------------------------------------------
# 健壮性：缺失题目 / 未冻结 no-op / UNIQUE 兜底竞态 / 不 commit
# ---------------------------------------------------------------------------


def test_elo_realize_missing_question_raises(elo_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="题目不存在"):
        realize_or_forfeit_blind_test(elo_conn, "ghost", "s1", passed=True)


def test_elo_realize_not_frozen_noop(elo_conn: sqlite3.Connection) -> None:
    conn = elo_conn
    _elo_make_question(conn, "q-nofreeze", base_elo=1600)
    apply_draft_subgoal_floor(conn, "q-nofreeze", "s1", d_sub=1400)  # 仅 FLOOR_ONLY
    before = get_module_elo(conn, ELO_MODULE)
    result = realize_or_forfeit_blind_test(conn, "q-nofreeze", "s-blind", passed=True)
    assert not result.applied and result.event_id is None
    assert result.combat_before == result.combat_after
    assert get_module_elo(conn, ELO_MODULE) == before
    assert _elo_pool(conn, "q-nofreeze")["escrow_status"] == "FLOOR_ONLY"


def test_elo_realize_unique_backstop_race(
    elo_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前置幂等查询失明漏检时，INSERT 撞 UNIQUE 后回读既有事件，池不被二次销账。"""
    conn = elo_conn
    _elo_make_question(conn, "q-race", base_elo=1600)
    apply_draft_subgoal_floor(conn, "q-race", "s1", d_sub=1400)
    freeze_decrutch_escrow(conn, "q-race", "s1")
    escrow = float(_elo_pool(conn, "q-race")["escrow_combat_delta"])
    first = realize_or_forfeit_blind_test(conn, "q-race", "s-blind", passed=True)
    assert first.applied
    # 模拟他方已写账本事件、池尚未销账的中间态：重置池回 FROZEN
    conn.execute(
        "UPDATE question_escrow_pool SET escrow_status='FROZEN_PENDING_BLIND',"
        " escrow_combat_delta=?, decay_factor=1.0 WHERE question_uid='q-race'",
        (escrow,),
    )
    count = len(_elo_events(conn, ELO_MODULE))
    real_find = elo_engine._find_event_by_key
    blind = {"used": False}

    def blind_find(c: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        if not blind["used"]:
            blind["used"] = True
            return None
        return real_find(c, key)

    monkeypatch.setattr(elo_engine, "_find_event_by_key", blind_find)
    second = realize_or_forfeit_blind_test(conn, "q-race", "s-blind", passed=True)
    assert not second.applied
    assert second.event_id == first.event_id
    assert second.combat_after == first.combat_after
    assert len(_elo_events(conn, ELO_MODULE)) == count
    pool = _elo_pool(conn, "q-race")
    assert pool["escrow_combat_delta"] == escrow
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND"


def test_elo_slice_b_functions_never_commit(tmp_db: Path) -> None:
    """裸连接（默认 tuple row_factory）：引擎不依赖调用方 row_factory，写入停留事务内。"""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _elo_make_question(conn, "q-tx", base_elo=1600, mastery="PENDING_RETEST")
        apply_draft_subgoal_floor(conn, "q-tx", "s1", d_sub=1400)
        freeze_decrutch_escrow(conn, "q-tx", "s1")
        _elo_set_slated(conn, "q-tx", drill_date="2026-11-12", exposure=4)
        assert apply_overdue_escrow_decay(conn, date(2026, 11, 12)) == 1
        realize_or_forfeit_blind_test(conn, "q-tx", "s-blind", passed=True)
        assert conn.in_transaction
        other = sqlite3.connect(tmp_db)
        try:
            row = other.execute("SELECT COUNT(*) FROM elo_mutation_ledger").fetchone()
            assert row is not None and row[0] == 0  # 未 commit，外部不可见
        finally:
            other.close()
        conn.rollback()
        row = conn.execute("SELECT COUNT(*) FROM elo_mutation_ledger").fetchone()
        assert row is not None and row[0] == 0
    finally:
        conn.close()
