"""四维难度 Rubric 与子步单调性引擎（纯确定性，无 LLM 参与）。

唯一权威规格：docs/designs/adaptive_practice_harness.md
§3.1 第 2 条「校准后的四维难度 Rubric 与确定性分档阶梯」与 Target File #6。

- ``calculate_base_elo``：四维 [C, S, T, L] → 连续 Elo（800～2800）+ 8 档 tier。
- ``clamp_substep_difficulty``：子步四维单调性硬裁剪 + D_sub 下界保护，
  保证结果永不违反 SQLite CHECK（sub_elo BETWEEN 800 AND 2650）。
"""

from backend.config import settings

__all__ = ["calculate_base_elo", "clamp_substep_difficulty"]

# 四维合法范围：C∈[1,4]，S/T/L∈[1,3]（下界统一为 1）
_DIM_NAMES: tuple[str, str, str, str] = ("C", "S", "T", "L")
_DIM_UPPER: tuple[int, int, int, int] = (4, 3, 3, 3)

# 合成公式系数（设计文档 §3.1 第 2 条逐字规格；settings 未收录，在此具名常量化）
_BASE_OFFSET = 750
_WEIGHT_C = 85
_WEIGHT_S = 105
_WEIGHT_T = 115
_WEIGHT_L = 65

# 8 档 tier 阶梯阈值（设计文档 §3.1 第 2 条，严格按序匹配）
_L2_ELO_UPPER = 1400  # 规则 2：C≤2 且 elo < 1400 → L2
_L4A_ELO_LOWER = 1600  # 规则 3：C≤2 且 elo ≥ 1600 → L4-A（C≤2 封顶）
_C3_L51_ELO_LOWER = 1850  # 规则 6：C=3 且 elo ≥ 1850 → L5-1
_C4_L51_ELO_UPPER = 2250  # 规则 8：C=4 且 elo < 2250 → L5-1
_C4_L52_ELO_UPPER = 2550  # 规则 8：C=4 且 elo < 2550 → L5-2，否则 L5-3


def _validate_cstl(cstl: tuple[int, int, int, int]) -> None:
    """校验四维打分：必须为 int（拒绝 bool 与非整数），越界抛 ValueError。"""
    for name, value, upper in zip(_DIM_NAMES, cstl, _DIM_UPPER, strict=True):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"维度 {name} 必须为 int（拒绝 bool 与非整数），收到 {value!r}")
        if not 1 <= value <= upper:
            raise ValueError(f"维度 {name} 必须在 [1, {upper}] 范围内，收到 {value}")


def _synthesize_base_elo(c: int, s: int, t: int, load: int) -> int:
    """确定性连续 Elo 合成：min(2800, max(800, 750 + 85*(2^C-1) + 105S + 115T + 65L))。"""
    raw = (
        _BASE_OFFSET + _WEIGHT_C * (2**c - 1) + _WEIGHT_S * s + _WEIGHT_T * t + _WEIGHT_L * load
    )
    return min(settings.BASE_ELO_MAX, max(settings.BASE_ELO_MIN, raw))


def _resolve_tier(c: int, s: int, t: int, load: int, elo: int) -> str:
    """8 档 tier 确定性判定阶梯，严格按设计文档先后顺序匹配。"""
    if c == 1 and s == 1 and t == 1:  # 规则 1（L 任意，优先于分数）
        return "L1_基础直达"
    if c <= 2:
        if elo < _L2_ELO_UPPER:  # 规则 2
            return "L2_常规巩固"
        if elo >= _L4A_ELO_LOWER:  # 规则 3（C≤2 封顶 L4-A，不入 L5）
            return "L4-A_进阶拔高(繁)"
        return "L3_综合中档"  # 规则 4（其余 1400≤elo<1600）
    if c == 3:
        if t == 3 and load <= 2:  # 规则 5（优先于规则 6）
            return "L4-B_进阶拔高(巧)"
        if elo >= _C3_L51_ELO_LOWER:  # 规则 6
            return "L5-1_压轴挑战"
        return "L3_综合中档"  # 规则 7 兜底
    # 规则 8：C == 4
    if elo < _C4_L51_ELO_UPPER:
        return "L5-1_压轴挑战"
    if elo < _C4_L52_ELO_UPPER:
        return "L5-2_压轴巅峰"
    return "L5-3_殿堂极限"


# 参数名 c/s/t/l 为 API 契约固定签名，E741 对 l 的告警在此豁免
def calculate_base_elo(c: int, s: int, t: int, l: int) -> dict[str, int | str]:  # noqa: E741
    """将四维整数打分转化为确定性 base_elo 与 tier。

    输入校验：c∈[1,4]，s/t/l∈[1,3]，必须为 int（拒绝 bool 与非整数），
    越界或类型非法抛 ValueError。

    返回 ``{"base_elo": int, "tier": str}``。
    """
    _validate_cstl((c, s, t, l))
    elo = _synthesize_base_elo(c, s, t, l)
    return {"base_elo": elo, "tier": _resolve_tier(c, s, t, l, elo)}


def clamp_substep_difficulty(
    parent_cstl: tuple[int, int, int, int],
    parent_elo: int,
    raw_sub_cstl: tuple[int, int, int, int],
) -> tuple[tuple[int, int, int, int], int]:
    """子步单调性硬裁剪，返回 (裁剪后的子步四维, D_sub)。

    - parent_cstl 按 rubric 规则严格校验（越界/非 int 抛 ValueError）；
      parent_elo 不做范围校验（低分由 800 下界兜底）。
    - raw_sub_cstl 各维必须为 int（拒绝 bool 与非整数，抛 ValueError），
      但范围违规不抛错：先夹进合法范围 [1, 上界]，再执行单调性裁剪
      C_sub≤max(1,C-1)、S_sub≤max(1,S-1)、T_sub≤T、L_sub≤L。
    - D_sub = max(800, min(2650, parent_elo - 150, base_elo(C_sub,S_sub,T_sub,L_sub)))，
      保证永不违反 SQLite CHECK（sub_elo BETWEEN 800 AND 2650）。
    """
    _validate_cstl(parent_cstl)
    for name, value in zip(_DIM_NAMES, raw_sub_cstl, strict=True):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"raw_sub 维度 {name} 必须为 int（拒绝 bool 与非整数），收到 {value!r}"
            )

    parent_c, parent_s, parent_t, parent_l = parent_cstl
    # 第一步：raw_sub 各维夹进合法范围（下界 1，上界 C=4 / S,T,L=3）
    raw_c = min(max(raw_sub_cstl[0], 1), _DIM_UPPER[0])
    raw_s = min(max(raw_sub_cstl[1], 1), _DIM_UPPER[1])
    raw_t = min(max(raw_sub_cstl[2], 1), _DIM_UPPER[2])
    raw_l = min(max(raw_sub_cstl[3], 1), _DIM_UPPER[3])
    # 第二步：单调性硬裁剪
    sub_cstl = (
        min(raw_c, max(1, parent_c - 1)),
        min(raw_s, max(1, parent_s - 1)),
        min(raw_t, parent_t),
        min(raw_l, parent_l),
    )
    sub_base = _synthesize_base_elo(*sub_cstl)
    d_sub = max(
        settings.BASE_ELO_MIN,
        min(settings.SUB_ELO_MAX, parent_elo - settings.SUBGOAL_ELO_MARGIN, sub_base),
    )
    return sub_cstl, d_sub
