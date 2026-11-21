"""测试确定性非线性难度合成引擎与 D4 8 档全量 108 种组合快照。"""

import pytest

from shuati.core.difficulty import compute_difficulty


def test_difficulty_formula_and_key_boundaries():
    # 规则 1：低端宽容，忽略纯运算量 L
    res_l1_min = compute_difficulty(1, 1, 1, 1)
    assert res_l1_min.base_elo == 1120
    assert res_l1_min.tier == "L1_基础直达"
    assert res_l1_min.tier_rank == 1

    res_l1_max = compute_difficulty(1, 1, 1, 3)
    assert res_l1_max.base_elo == 1250
    assert res_l1_max.tier == "L1_基础直达"
    assert res_l1_max.tier_rank == 1

    # 规则 2：C<=2 且 elo < 1400 -> L2
    res_l2 = compute_difficulty(2, 1, 1, 1)
    assert res_l2.base_elo == 1290
    assert res_l2.tier == "L2_常规巩固"
    assert res_l2.tier_rank == 2

    # 规则 3：C<=2 封顶防呆 -> L4-A
    res_l4a = compute_difficulty(2, 3, 3, 3)
    assert res_l4a.base_elo == 1860
    assert res_l4a.tier == "L4-A_进阶拔高(繁)"
    assert res_l4a.tier_rank == 4

    # 规则 4：C<=2 其余 1400<=elo<1600 -> L3
    res_l3 = compute_difficulty(2, 2, 2, 2)
    assert res_l3.base_elo == 1575
    assert res_l3.tier == "L3_综合中档"
    assert res_l3.tier_rank == 3

    # 规则 5：C=3, T=3, L<=2 -> L4-B_进阶拔高(巧)
    res_l4b = compute_difficulty(3, 3, 3, 1)
    assert res_l4b.base_elo == 2070
    assert res_l4b.tier == "L4-B_进阶拔高(巧)"
    assert res_l4b.tier_rank == 5

    # 规则 6：C=3, elo >= 1850 (不满足规则 5) -> L5-1
    res_l51 = compute_difficulty(3, 3, 3, 3)
    assert res_l51.base_elo == 2200
    assert res_l51.tier == "L5-1_压轴挑战"
    assert res_l51.tier_rank == 6

    # 规则 8 (D4 阈值 2400 / 2600):
    # 8a: C=4, elo < 2400 -> L5-1
    res_c4_l51 = compute_difficulty(4, 1, 1, 1)
    assert res_c4_l51.base_elo == 2310
    assert res_c4_l51.tier == "L5-1_压轴挑战"
    assert res_c4_l51.tier_rank == 6

    # 8b: C=4, 2400 <= elo < 2600 -> L5-2_竞赛超难
    res_c4_l52 = compute_difficulty(4, 2, 2, 2)
    assert res_c4_l52.base_elo == 2595
    assert res_c4_l52.tier == "L5-2_竞赛超难"
    assert res_c4_l52.tier_rank == 7

    # 8c: C=4, elo >= 2600 -> L5-3_殿堂极限
    res_c4_l53 = compute_difficulty(4, 3, 3, 3)
    assert res_c4_l53.base_elo == 2800  # capped at 2800
    assert res_c4_l53.tier == "L5-3_殿堂极限"
    assert res_c4_l53.tier_rank == 8


def test_difficulty_all_108_combinations():
    """全量 108 种 (C, S, T, L) 组合遍历测试。"""
    valid_tiers = {
        "L1_基础直达",
        "L2_常规巩固",
        "L3_综合中档",
        "L4-A_进阶拔高(繁)",
        "L4-B_进阶拔高(巧)",
        "L5-1_压轴挑战",
        "L5-2_竞赛超难",
        "L5-3_殿堂极限",
    }
    count = 0
    tier_counts: dict[str, int] = {t: 0 for t in valid_tiers}

    for c in range(1, 5):
        for s in range(1, 4):
            for t in range(1, 4):
                for l in range(1, 4):
                    count += 1
                    res = compute_difficulty(c, s, t, l)
                    assert 800 <= res.base_elo <= 2800
                    assert res.tier in valid_tiers
                    assert 1 <= res.tier_rank <= 8
                    tier_counts[res.tier] += 1

    assert count == 108
    # 确认每一档都有题目命中
    for t, cnt in tier_counts.items():
        assert cnt > 0, f"档位 {t} 没有任何组合命中"


def test_difficulty_validation():
    with pytest.raises(ValueError):
        compute_difficulty(0, 1, 1, 1)
    with pytest.raises(ValueError):
        compute_difficulty(5, 1, 1, 1)
    with pytest.raises(ValueError):
        compute_difficulty(1, 0, 1, 1)
    with pytest.raises(ValueError):
        compute_difficulty(1, 4, 1, 1)
    with pytest.raises(ValueError):
        compute_difficulty(True, 1, 1, 1)  # bool 必须被拒绝
    with pytest.raises(ValueError):
        compute_difficulty(1.0, 1, 1, 1)  # float 必须被拒绝
