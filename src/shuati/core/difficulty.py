"""确定性非线性难度合成引擎（决策 D4，DESIGN.md §4.4-3）。

严格实现 D4 阶梯：
- 连续 Elo：min(2800, max(800, 750 + 85*(2^C-1) + 105*S + 115*T + 65*L))
- 非对称 8 档判定，采用 C=4 阈值 2400 / 2600，档名 L5-2_竞赛超难
"""

from __future__ import annotations

from dataclasses import dataclass

_DIM_NAMES: tuple[str, str, str, str] = ("C", "S", "T", "L")
_DIM_UPPER: tuple[int, int, int, int] = (4, 3, 3, 3)

_BASE_OFFSET = 750
_WEIGHT_C = 85
_WEIGHT_S = 105
_WEIGHT_T = 115
_WEIGHT_L = 65

_BASE_ELO_MIN = 800
_BASE_ELO_MAX = 2800

_L2_ELO_UPPER = 1400  # 规则 2：C<=2 且 elo < 1400 -> L2
_L4A_ELO_LOWER = 1600  # 规则 3：C<=2 且 elo >= 1600 -> L4-A (C<=2 封顶)
_C3_L51_ELO_LOWER = 1850  # 规则 6：C=3 且 elo >= 1850 -> L5-1
_C4_L51_ELO_UPPER = 2400  # 规则 8a (D4)：C=4 且 elo < 2400 -> L5-1
_C4_L52_ELO_UPPER = 2600  # 规则 8b (D4)：C=4 且 elo < 2600 -> L5-2_竞赛超难，否则 L5-3


@dataclass(frozen=True)
class DifficultyResult:
    base_elo: int
    tier: str
    tier_rank: int


def _validate_cstl(c: int, s: int, t: int, l: int) -> None:  # noqa: E741
    cstl = (c, s, t, l)
    for name, value, upper in zip(_DIM_NAMES, cstl, _DIM_UPPER, strict=True):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"维度 {name} 必须为 int（拒绝 bool 与非整数），收到 {value!r}")
        if not 1 <= value <= upper:
            raise ValueError(f"维度 {name} 必须在 [1, {upper}] 范围内，收到 {value}")


def _synthesize_base_elo(c: int, s: int, t: int, l: int) -> int:  # noqa: E741
    raw = _BASE_OFFSET + _WEIGHT_C * (2**c - 1) + _WEIGHT_S * s + _WEIGHT_T * t + _WEIGHT_L * l
    return min(_BASE_ELO_MAX, max(_BASE_ELO_MIN, raw))


def _resolve_tier_and_rank(c: int, s: int, t: int, l: int, elo: int) -> tuple[str, int]:  # noqa: E741
    # 规则 1：低端宽容，忽略纯运算量 L
    if c == 1 and s == 1 and t == 1:
        return "L1_基础直达", 1
    # 规则 2-4：C <= 2
    if c <= 2:
        if elo < _L2_ELO_UPPER:
            return "L2_常规巩固", 2
        if elo >= _L4A_ELO_LOWER:
            return "L4-A_进阶拔高(繁)", 4
        return "L3_综合中档", 3
    # 规则 5-7：C == 3
    if c == 3:
        if t == 3 and l <= 2:
            return "L4-B_进阶拔高(巧)", 5
        if elo >= _C3_L51_ELO_LOWER:
            return "L5-1_压轴挑战", 6
        return "L3_综合中档", 3
    # 规则 8：C == 4 (决策 D4 阈值: 2400 / 2600，档名: L5-2_竞赛超难)
    if elo < _C4_L51_ELO_UPPER:
        return "L5-1_压轴挑战", 6
    if elo < _C4_L52_ELO_UPPER:
        return "L5-2_竞赛超难", 7
    return "L5-3_殿堂极限", 8


def compute_difficulty(c: int, s: int, t: int, l: int) -> DifficultyResult:  # noqa: E741
    """将四维打分转化为确定性 base_elo、tier 与 tier_rank。"""
    _validate_cstl(c, s, t, l)
    elo = _synthesize_base_elo(c, s, t, l)
    tier, rank = _resolve_tier_and_rank(c, s, t, l, elo)
    return DifficultyResult(base_elo=elo, tier=tier, tier_rank=rank)
