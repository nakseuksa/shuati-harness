# P1-C 任务契约：四维难度 Rubric 与子步单调性引擎

## 上下文
项目：数理自适应刷题 Harness（Local-First）。你负责 Phase 1 的确定性难度引擎——全系统第一个核心算法。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.1 第 2 条（校准后的四维难度 Rubric 与确定性分档阶梯）**、Target File **#6** 与 **#15** 的测试向量。设计文档是唯一权威规格。
注意：仓库外另有一个旧版打分器（exercise_grading），公式不同，**不要参考**，一切以本设计文档为准。

## 你拥有的文件（只允许创建/修改这些）
- `backend/services/difficulty_rubric.py`
- `tests/test_difficulty_and_elo.py`（本阶段只写 rubric/clamp 部分；Phase 3 会由他人追加 Elo 测试，勿写 Elo 相关内容）

**禁止**触碰其他任何文件。**禁止** git commit/push。

## 环境
- 项目根目录：`/Users/wanghaichen/0_work/刷题harness`
- Python 一律用 `.venv/bin/python`
- 阶段门禁：`.venv/bin/python -m pytest tests/test_difficulty_and_elo.py -k "rubric or clamp" -v`（测试函数/类名必须含 rubric 或 clamp 字样以命中过滤器）
- 质量门禁：`.venv/bin/ruff check backend/services/difficulty_rubric.py tests/test_difficulty_and_elo.py` 与 `.venv/bin/mypy backend/services/difficulty_rubric.py` 零告警

## API 契约
```python
def calculate_base_elo(c: int, s: int, t: int, l: int) -> dict[str, int | str]: ...
# 返回 {"base_elo": int, "tier": str}

def clamp_substep_difficulty(
    parent_cstl: tuple[int, int, int, int],
    parent_elo: int,
    raw_sub_cstl: tuple[int, int, int, int],
) -> tuple[tuple[int, int, int, int], int]: ...
# 返回 (裁剪后的子步四维, D_sub)
```

## 算法规格（逐字实现）
1. **输入校验**：c∈[1,4]，s/t/l∈[1,3]，必须为 int（拒绝 bool 与非整数），越界抛 ValueError。
2. **合成公式**：`base_elo = min(2800, max(800, 750 + 85*(2**c - 1) + 105*s + 115*t + 65*l))`。边界常量从 `backend.config.settings` 读取（BASE_ELO_MIN/BASE_ELO_MAX）。
3. **8 档 tier 阶梯，严格按序匹配**（tier 字符串一字不差）：
   1. `C==1 and S==1 and T==1` → `'L1_基础直达'`（L 任意）
   2. `C<=2 and elo<1400` → `'L2_常规巩固'`
   3. `C<=2 and elo>=1600` → `'L4-A_进阶拔高(繁)'`（C≤2 封顶 L4-A）
   4. `C<=2`（其余 1400≤elo<1600）→ `'L3_综合中档'`
   5. `C==3 and T==3 and L<=2` → `'L4-B_进阶拔高(巧)'`
   6. `C==3 and elo>=1850` → `'L5-1_压轴挑战'`
   7. `C==3`（其余）→ `'L3_综合中档'`
   8. `C==4`：elo<2250 → `'L5-1_压轴挑战'`；elo<2550 → `'L5-2_压轴巅峰'`；否则 → `'L5-3_殿堂极限'`
4. **单调性硬裁剪**：先把 raw_sub 各维夹进合法范围（下界 1），再执行
   `C_sub=min(C_sub, max(1, C-1))`、`S_sub=min(S_sub, max(1, S-1))`、`T_sub=min(T_sub, T)`、`L_sub=min(L_sub, L)`；
   `D_sub = max(800, min(2650, parent_elo - 150, base_elo(C_sub,S_sub,T_sub,L_sub)))`。
   常量用 settings.SUB_ELO_MAX / SUBGOAL_ELO_MARGIN / BASE_ELO_MIN。保证结果永不违反 SQLite CHECK（sub_elo BETWEEN 800 AND 2650）。

## 测试要求（PM 已核验的向量，必须全部断言）
- `(1,1,1,1) → 1120, L1_基础直达`
- `(2,3,1,3) → 1630, L4-A_进阶拔高(繁)`（C≤2 封顶熔断）
- `(4,3,3,3) → 2800, L5-3_殿堂极限`（上钳位）
- `(3,3,2,3) → 2085`（CUJ1 示例）
- `(1,1,1,3) → 1250, L1_基础直达`（规则 1 优先于分数）
- `(3,2,3,2) → 2030, L4-B_进阶拔高(巧)`（规则 5 优先于规则 6）
- `(3,1,1,1) → 1630, L3_综合中档`（规则 7 兜底）
- `(4,1,1,1) → 2310, L5-2_压轴巅峰`
- `(4,2,2,1) → 2530, L5-2_压轴巅峰` 与 `(4,2,2,2) → 2595, L5-3_殿堂极限`（2550 边界两侧）
- clamp：parent=(1,1,1,1)/1120，raw_sub=(4,3,3,3) → 裁剪为 (1,1,1,1)，D_sub=970
- clamp：parent S=1 时 S_sub 恒 ≤1；parent_elo=900（<950）时 D_sub=800 下界
- clamp：raw_sub 含 0 或负数 → 下界 1 不崩；raw_sub 超上限 → 先夹进合法范围
- 输入校验：越界/非整数/bool 抛异常
- 建议加一个全组合遍历测试（4×3×3×3=108 种）：base_elo∈[800,2800]、tier 属于 8 档集合、clamp 结果恒满足 CHECK 范围

## 完成标准
阶段门禁命令全绿 + ruff/mypy 零告警。最终消息汇报：创建的文件、测试通过数、门禁输出摘要。
