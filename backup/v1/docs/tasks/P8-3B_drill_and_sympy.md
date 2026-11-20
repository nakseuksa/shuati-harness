# 任务契约 P8-3B：盲测重训符号与预热 + SymPy 解析边界修复

- **所属阶段**：Phase 8（Review 修复 · 批次 3B）
- **权威设计**：`docs/designs/adaptive_practice_harness.md` CUJ 3、Target File #6/#13
- **允许修改的文件白名单**：
  - `backend/services/blind_drill_scheduler.py`
  - `backend/main.py`
  - `backend/sandbox/sympy_verifier.py`
  - `frontend/app.js`
  - `tests/test_blind_drill_scheduler.py`
  - `tests/test_api_routes.py`
  - `tests/test_answer_matching.py`

## 修复目标

1. **`backend/services/blind_drill_scheduler.py` / `main.py` / `frontend/app.js`（盲测符号来源 + 重训会话可达与预热）**：
   - `BlindDrillOutcomeDTO` 新增 `retrain_session_id: str | None = None`，失败新建重训会话时填充该字段；
   - `_build_item` 当 `mode == "equation_skeleton"` 时优先从实际呈现题 `drill_uid` 提取步骤公式与题干符号，避免呈现变式时混入原题独有符号；
   - `main.py` 的 `POST /api/v1/drills/daily` 失败新建重训会话后在事务外异步触发 `prewarm_session_questions(outcome.retrain_session_id)`；
   - `frontend/app.js` 盲测失败卡片在存在 `retrain_session_id` 时提供一键跳转重训入口（加载该会话并切至训练页签）。
2. **`backend/sandbox/sympy_verifier.py`（复合数量负分段拒绝 + LaTeX 扩展）**：
   - `_parse_quantity` 允许首段带负号（如 `-5米`），拒绝后续段出现负号（如 `9小时-36分` 返回 `None`）；
   - `latex_to_plain` 增补 `\dfrac` / `\tfrac` 归一、nth-root `\sqrt[n]{x}`、单字符简写 `\frac12`（含 `\frac1{b}` / `\frac{a}2`）与 `\div` → `/`。

## 验收测试

- `tests/test_blind_drill_scheduler.py`：验证变式盲测 `allowed_symbols` 来自变式题干/步骤而非原题独有符号；验证失败返回 `retrain_session_id` 且成功返回 `None`；
- `tests/test_api_routes.py`：验证 `/api/v1/drills/daily` 失败返回 `retrain_session_id` 并触发预热；
- `tests/test_answer_matching.py`：验证 `9小时-36分` 被 `_parse_quantity` 拒绝；验证 `\dfrac` / `\tfrac` / `\sqrt[3]{8}` / `\frac12` / `a \div b` 在 `latex_to_plain` 与 `answers_equivalent` 正确通过。
