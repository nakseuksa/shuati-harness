# P11-A 任务契约：每日盲测 $L \ge 2$ 变式列式核验对齐与下标符号提取修复

## 上下文

Review 发现每日盲测存在两处前后端/等价引擎不一致 Bug：
1. **Bug 1（后端部分）**：`_build_item` 对 `l_score >= 2` 返回 `verification_mode = "equation_skeleton"`，但 `evaluate_blind_drill_submission` 仅将提交与 `drill["standard_answer_latex"]` 比对。对于通过 `equation_skeleton` 持久化在 `question_steps[0].step_formula_latex` 中的 `AI_SYNTHETIC_VARIANT` 变式（如多方程列式骨架与最终化简值不同），用户按 `equation_skeleton` 模式提交核心方程组会被误判错。
2. **Bug 7**：`_extract_symbols` 直接在原始 LaTeX 上用单字母正则 `(?<![A-Za-z])([A-Za-z])(?![A-Za-z])` 提取符号，导致 `v_{max}`、`v_0`、`\theta_0` 等带下标符号退化为单字母 `v` / `theta`（丢失下标），且 `\text{m/s}` 或 Markdown 图片 `cas://...` URL 中的字母可能混入符号表；而 `sympy_verifier.latex_to_plain` 会将下标扁平化为 `vmax` / `v0` / `theta0`。

## 修改方案

### 1. `backend/services/blind_drill_scheduler.py`

1. **`evaluate_blind_drill_submission`**：
   - 保持现有入参/出参与 `BlindDrillItemDTO` 字段集不变（`test_dto_strips_spoilers_and_has_no_leaky_fields` 锁定了 `BlindDrillItemDTO` 字段集合，**禁止**增删 DTO 字段）。
   - 保持原题回退（`item.requires_intermediate == True`）的双重核验语义不变（`intermediate_sub_answer` 核验卡壳步 `step_formula_latex`，`final_answer` 核验 `drill["standard_answer_latex"]`）。
   - 当呈现题为变式（`not item.requires_intermediate`）且 `item.verification_mode == "equation_skeleton"` 且 `str(drill["origin_type"]) == "AI_SYNTHETIC_VARIANT"` 时：
     - 先用 `answers_equivalent(final_answer, str(drill["standard_answer_latex"]))` 判定（兼容现有测试与已化简提交）；
     - 若为 `False`，再查询该变式 `canonical` 分支第 1 步（`ORDER BY step_index ASC LIMIT 1`）的 `step_formula_latex`；若非空且与 `standard_answer_latex` 不同，再以 `answers_equivalent(final_answer, skeleton)` 判定（支持用户按核心列式骨架作答）。
2. **`_extract_symbols(texts: Iterable[str]) -> list[str]`**：
   - 对每段文本先去除 Markdown 图片/链接 URL（保留 alt/label 文本，剔除 `(...)` 中 URL，避免 `cas://` 十六进制字母泄漏）、去除 `\text{...}` / `\mathrm{...}` / `\mbox{...}` / `\operatorname{...}`（单位文本不入符号表）；
   - 优先提取显式下标符号并从临时文本中替换为空格（避免同时抽出裸前缀字母）：
     - 希腊字母下标：`\\([A-Za-z]+)_\{([A-Za-z0-9]+)\}` 与 `\\([A-Za-z]+)_([A-Za-z0-9])`（当命令名属于 `_GREEK_SYMBOL_COMMANDS` 时合并为 `{cmd}{sub}`，如 `\theta_0` → `theta0`）；
     - 拉丁字母下标：`(?<![A-Za-z])([A-Za-z])_\{([A-Za-z0-9]+)\}` 与 `(?<![A-Za-z])([A-Za-z])_([A-Za-z0-9])`（合并为 `{letter}{sub}`，如 `v_{max}` → `vmax`、`v_0` → `v0`、`z_0` → `z0`）；
   - 再提取剩余希腊字母命令名（`_GREEK_SYMBOL_COMMANDS`）；
   - 剥离其余 `\command` 后用 `_SINGLE_LETTER_RE` 提取独立单字母；
   - 返回 `sorted(symbols)`。

### 2. `tests/test_blind_drill_scheduler.py`

新增测试覆盖：
1. `AI_SYNTHETIC_VARIANT`（`l_score >= 2`）在 `question_steps` 存有与 `standard_answer_latex` 不同的 `equation_skeleton` 时：提交列式骨架可通过盲测销账，提交错误列式仍判失败；且原题回退时 `final_answer` 仍严格核验 `standard_answer_latex`（不会被步骤骨架误放行）；
2. `_extract_symbols`（及 `_build_item` 返回的 `allowed_symbols`）正确提取 `v_{max}` → `vmax`、`v_0` → `v0`、`\theta_0` → `theta0`，剥离 `\text{m/s}` 与 `cas://` 图片路径，且保持既有 `"R", "d", "q", "m", "v", "B", "theta"` 与 `"z0" not in allowed_symbols` 行为。

## 文件白名单

- `backend/services/blind_drill_scheduler.py`
- `tests/test_blind_drill_scheduler.py`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**网络调用。
