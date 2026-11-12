"""Phase 1：四维难度 Rubric 与子步单调性裁剪（clamp）测试。

规格：docs/designs/adaptive_practice_harness.md §3.1 第 2 条、Target File #6/#15。
注意：Elo 托管池/水位线相关测试由 Phase 3 追加，本文件当前只含 rubric/clamp 部分。
"""

import itertools

import pytest

from backend.services.difficulty_rubric import calculate_base_elo, clamp_substep_difficulty

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
