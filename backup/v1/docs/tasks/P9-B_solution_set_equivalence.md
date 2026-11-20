# P9-B 任务契约：多方程「消元比对解集」完全体（严格全符号解集等价）

## 上下文

设计 §3.3 代数等价判据要求「多方程组按 `;` 或换行切分并**消去中间变量后比对目标未知量解集**」；现状（P2-A 起）仅实现切分 + 逐方程配对，且 `_verify_equation_equivalence_impl` 硬性要求双方方程数相等（不等 → BAD_INPUT）。完全体此前明确延后（dev_plan 编排日志 2026-11-13），本轮交付。

**用户已裁决（2026-11-16）：严格语义** —— 未知量取双方全部自由符号的**并集**，解集必须完全一致（互代验证）。缺项、增根、多解不一致均判错。纯数字红线（5≠7、5≠-5）绝不放宽。

## 修改方案（仅 `backend/sandbox/sympy_verifier.py`）

### 1. `_verify_equation_equivalence_impl`

- 移除 `len(user_eqs) != len(std_eqs)` 的 BAD_INPUT 硬拒；保留：非字符串 → BAD_INPUT、任一切分列表为空 → BAD_INPUT、`_is_safe_expression` 不安全 → UNSAFE_EXPRESSION。
- 双方列表原样传入 runner（payload 结构不变）。超时/rlimit/进程组隔离、sentinel 解析、error_kind 语义全部不变。

### 2. `_EQUIV_RUNTIME`（子进程 runner 字符串）

在既有逐方程配对逻辑之后新增解集兜底（配对成功则无需兜底；配对仅在方程数相等时尝试，保持现状）：

```
_solutions_equivalent(user_exprs, std_exprs) -> bool
```

算法与守卫（全部在 runner 内实现，任何异常 → False）：
1. **自由符号守卫**：user 侧全体表达式与 std 侧全体表达式各自 `free_symbols` 并集均非空，否则 False（纯数字绝不走解集路径）。
2. **规模上限**：任一方方程数 > 8 或并集符号数 > 8 → False（防 solve 组合爆炸；5s 子进程超时仍是硬兜底）。
3. `U = sorted(union(free_symbols), key=lambda s: s.name)`（确定性排序）。
4. `sols_u = solve(user_exprs, U, dict=True)`；`sols_s = solve(std_exprs, U, dict=True)`（默认 check=True，保留 sympy 自身解验证，防奇异变换伪解）。
5. 任一解集为空列表 → False（无解/求解失败保守拒绝）。
6. **互代验证**：对 `sols_u` 的每个解 dict，代入 `std_exprs` 每个表达式，`simplify(expr.subs(sol)) == 0` 必须全部成立；反向对称验证 `sols_s` × `user_exprs`。任一不成立 → False。
7. 参数化欠定系统（如 `x+y=10` vs `x=10-y`）由 subs+simplify 自然处理；多解（如 `x**2=4` 的 ±2）逐解验证，解集不一致自然拒绝。

最终 `_equivalent = 逐方程配对结果 or _solutions_equivalent(...)`（配对已 True 时短路，不触发 solve）。
`_single_equivalent` 的自由符号守卫、反号/常数比率判据**一字不改**。

### 3. 明确不改

- `answers_equivalent` 的复合数量权威快路径、「标签+数值」快路径、normalize 链、拉丁单位豁免、裸值对单方程右端路径全部不动（多方程答案经 `verify_equation_equivalence` 自动受益）。
- `latex_to_plain`、`_parse_quantity`、沙盒 AST 探针、`run_sympy_in_sandbox` 不动。

### 4. 测试

`tests/test_answer_matching.py` 新增（`answers_equivalent` 与/或 `verify_equation_equivalence` 层）：
- `"x+y=10; x-y=2"` ≡ `"x=6; y=4"`（双向 True）
- `"a+b=c/6; a-b=c/12"` ≡ `"a=c/8; b=c/24"`（参数化欠定，True；注意表达式黑名单禁下划线，符号用单字母）
  **PM 裁决 2026-11-16**：原契约用 `S` 作参数，但 sympy parse_expr 默认命名空间保留单字母 S/E/I/N/Q（SingletonRegistry/自然常数/虚数单位等），`S/6` 无法解析为自由符号；改用非保留字母 `c`，解析链一字不动（强行转符号会破坏 E/I 数学语义且超范围）。测试处需注释说明此限制。
- 方程数不等的冗余方程组：`"x=6; y=4; x+y=10"` ≡ `"x+y=10; x-y=2"`（True）
- `"x+y=10"` ≡ `"x=10-y"`（True）
- LaTeX 形态：`"a=c/8; b=c/24"` ≡ `"a=\frac{c}{8}; b=\frac{c}{24}"`（经 answers_equivalent，True）
- 拒绝：`"x=6; y=5"` vs `"x+y=10; x-y=2"`；`"x**2=4"` vs `"x=2"`（双向）；`"x=1; x=2"` vs `"x=3"`（空解集）；`"x=6"` vs `"x+y=10; x-y=2"`（缺项）
- 回归：`"5"` vs `"7"` False、`"5"` vs `"-5"` False、`"9小时36分"` ≡ `"576分"` True、`"鸡40，兔60"` 对 `\text{鸡 }40\text{ 只，兔 }60\text{ 只}` True

`tests/test_sympy_sandbox.py`：
- 既有参数化用例 `("x=2; y=3", "x=2", False)` 在严格语义下**仍为 False**（std 未约束 y，解集不同）——保留断言，仅把注释「方程数量不一致」更新为「解集不一致（std 未约束 y）」；
- 新增解集等价正例（如 `"x+y=10; x-y=2"` vs `"x=6; y=4"`）与增根反例各 ≥2 条。

**测试期望演进预授权**：仅允许更新直接钉住旧「方程数必须相等」/「无解集比对」行为的期望，改动处必须注释引用 P9-B 并在交付汇报中逐条列出；其余断言一律不动。若全量自检发现其他冲突，停下汇报 PM，不得擅自放宽。

## 文件白名单

- `backend/sandbox/sympy_verifier.py`
- `tests/test_answer_matching.py`
- `tests/test_sympy_sandbox.py`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**任何网络调用。
允许本地运行 targeted pytest / ruff / mypy 自检；PM 将独立复跑全量门禁验收。
契约与设计冲突时以 `docs/designs/adaptive_practice_harness.md` 为准并向 PM 汇报。
