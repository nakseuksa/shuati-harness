# P8-1B 任务契约：前后端契约断链修复与 L>=2 变式核心列式骨架存储

## 上下文
Review 发现四处前后端/引擎契约断链：
1. `STAGE_COOLDOWN_DEFERRED`：后端将对比卡片放在 `view_payload["skeleton_comparison_card"]`（含 `standard_skeleton`、`user_answer`、`variant_stem_md`、`retry_policy`），而前端直接从顶层 `view_payload` 找 `standard_answer_latex`/`user_answer`，导致界面渲染 `（未提供）`。
2. Fallback「换一题（重新热备）」与「跳过此题」：`retry_prewarm` 对 `READY_FALLBACK` 直接幂等返回不再生新题；前端「跳过此题」提交 `self_assessment='SKIP'` 后端未识别。
3. `SESSION_COMPLETED`：`view_payload` 未下发 `escrow_combat_delta`，导致前端无法展示托管分差额徽章。
4. `L >= 2` Stage 4：`scaffold_engine._persist_synthetic` 对 `AI_SYNTHETIC_VARIANT` 将 `step_formula_latex` 硬写为最终答案 `gen.answer_latex`，导致 `_stage4_standard` 对真实多方程列式题无法按核心方程组骨架验证。

## 修改方案

### 1. `backend/services/scaffold_engine.py`
1. **增加 `equation_skeleton` 到内核生成与变式持久化**：
   - `_KernelPayload` 增加可选字段 `equation_skeleton: str = ""`。
   - `_KERNEL_SYSTEM_PROMPT` 补充说明：若为综合计算/推导变式，可附带可选字段 `equation_skeleton`（核心列式骨架 LaTeX 方程/方程组，占位符协议同 `parametric_stem_tpl`）。
   - `_GenResult` 增加可选字段 `equation_skeleton: str | None = None`。
   - 新增辅助 `_render_optional_skeleton(tpl: str, params: dict[str, str | int | float] | None) -> str | None`：若 `tpl.strip()` 非空，先按 `_render_stem` 回填；若因 LaTeX 花括号失败，则仅替换 `params` 中出现的 `{key}` 占位符（不破坏 `\frac{...}{...}` 等 LaTeX 花括号），返回非空字符串；并写入 `artifact["equation_skeleton"]`（如非空）。
   - `_persist_synthetic` 中，当 `origin_type == "AI_SYNTHETIC_VARIANT"` 时，`question_steps.step_formula_latex` 写入 `gen.equation_skeleton or gen.answer_latex`；`AI_SYNTHETIC_SCAFFOLD` 保持写入 `gen.answer_latex` 不变。
   - （保留现有 Mock 兼容：若 `kernel.equation_skeleton` 为空字符串，`gen.equation_skeleton` 为 `None`，现有测试 `standard_skeleton == "x = 5"` 行为不变。）

2. **`retry_prewarm` 支持 `force: bool = False`（换一题真正重生成）**：
   - `retry_prewarm(session_id: str, db_path: Path | None = None, *, force: bool = False) -> SessionStepResponse`。
   - `force=False` 保持现有完全一致的幂等行为（`READY_SYMPY`/`READY_FALLBACK` 且双产物齐全直接返回）。
   - `force=True`（非 `SESSION_COMPLETED`）：
     - 若有在途 task 未完成，先 `existing.cancel()` 并清理登记；
     - 收集当前 `prewarm_scaffold_uid` / `prewarm_variant_uid` 作为 `exclude_uids`；
     - 根据当前 `micro_stage` 决定清空范围：若为 `STAGE_2_MICRO_SCAFFOLD` 清空 `prewarm_scaffold_uid=None`（仅重刷 scaffold）；若为 `STAGE_4_DECRUTCH_VARIANT` 清空 `prewarm_variant_uid=None`（仅重刷 variant）；否则两者均清空。同时置 `prewarm_status='RUNNING'`；
     - 启动新预热任务并把 `exclude_uids` 及目标范围传入 `prewarm_session_questions` -> `_prewarm_impl` -> `_prepare_scaffold` / `_prepare_variant` -> `_search_local_scaffold` / `_search_local_variant`（跳过 `question_uid in exclude_uids` 的候选题，确保换出不同题目，找不到新本地复用题时走 `_generate_kernel_question` 生成新题）；
     - `_prewarm_impl` 若某侧未重刷则沿用已有 UID 与其 `verification_status == 'SYMPY_VERIFIED'` 状态来综合计算 `prewarm_status`。

3. **Fallback 降级题 `SKIP` 处理**：
   - 在 `_submit_stage2` 中：当 `scaffold["verification_status"] == "DUAL_LLM_FALLBACK"` 且 `(self_assessment or "").strip().upper() == "SKIP"` 时，直接从 `STAGE_2_MICRO_SCAFFOLD` 流转至 `STAGE_3_REDO_ORIGINAL`（`trigger="fallback_skipped"`），返回响应并附 `notice="已跳过未经沙盒强校验的微母题，直接回到原题。"`（不走降阶、不记错答）。
   - 在 `_submit_stage4` 中：当 `variant["verification_status"] == "DUAL_LLM_FALLBACK"` 且 `(self_assessment or "").strip().upper() == "SKIP"` 时，不扣分不进入冷却，保持当前阶段并返回 `notice="四阶当场验证不可跳过，请点击「换一题（重新热备）」重新生成或提交作答。"`。

4. **`SESSION_COMPLETED` 下发 `escrow_combat_delta`**：
   - 在 `_build_response` 的 `stage == "SESSION_COMPLETED"` 分支，查询 `question_escrow_pool` 中该 `question_uid` 的 `escrow_combat_delta`（仅当 `row is not None and str(question["mastery_state"]) == "PENDING_RETEST"` 时赋值 `float(row["escrow_combat_delta"])`，否则 `None`），写入 `view["escrow_combat_delta"]`。

### 2. `backend/main.py`
- 修改 `@app.post("/api/v1/sessions/{session_id}/retry-prewarm")`：增加可选查询参数 `force: Annotated[bool, Query()] = False`，透传给 `retry_prewarm(session_id, force=force)`。

### 3. `frontend/app.js`
1. **修复 `STAGE_COOLDOWN_DEFERRED` 对比卡片解包与展示**：
   - 从 `v.skeleton_comparison_card`（支持 `parseMaybeJson`）解包对比卡片 `card`，回退到 `v`：
     - `mine` 读取 `user_answer` 等兼容字段；
     - `std` 优先读取 `standard_skeleton`，兼容 `standard_answer_latex` 等字段；
     - 如 `card.variant_stem_md` 存在，补充展示变式题干；如 `card.retry_policy` 存在，替换/补充底部提示。
   - `extraPayloadHtml(v, [..., 'skeleton_comparison_card'])` 避免底部重复渲染原始 JSON。
2. **修复 `fallbackBannerHtml` 与 `swapFallbackQuestion`**：
   - `swapFallbackQuestion` 请求改为 `.../retry-prewarm?force=true`。
   - `fallbackBannerHtml(resp)` 中「跳过此题」按钮仅在 `resp.micro_stage === 'STAGE_2_MICRO_SCAFFOLD'` 时渲染（四阶不展示虚假跳过按钮，仅展示「换一题（重新热备）」）。

### 4. 测试覆盖（`tests/test_scaffold_engine.py`, `tests/test_api_routes.py`）
- `tests/test_scaffold_engine.py`：
  - 验证 `STAGE_4` 通过后 `SESSION_COMPLETED` 的 `view_payload["escrow_combat_delta"]` 等于冻结托管金额（`> 0`），而 `ALREADY_MASTERED` 时为 `None`；
  - 验证 `L >= 2` 合成变式若内核返回 `equation_skeleton`（含 LaTeX 花括号与 `{params}` 占位符），`variant_steps[0]["step_formula_latex"]` 与四阶 `standard_skeleton` 使用该方程骨架而非仅末位数字答案；
  - 验证 `DUAL_LLM_FALLBACK` 在二阶提交 `self_assessment="SKIP"` 跳转 `STAGE_3_REDO_ORIGINAL`，在四阶提交 `self_assessment="SKIP"` 不进入冷却且给出提示；
  - 验证 `retry_prewarm(sid, force=True)` 在 `READY_FALLBACK` 状态下能真正重生成新题（UID 变化）。
- `tests/test_api_routes.py`：
  - 验证 `POST /api/v1/sessions/{sid}/retry-prewarm?force=true` 触发真实重预热而非幂等无操作，且无 `force` 参数仍保持幂等；
  - 验证 `SESSION_COMPLETED` 响应含 `escrow_combat_delta`。

## 文件白名单
- `backend/services/scaffold_engine.py`
- `backend/main.py`
- `frontend/app.js`
- `tests/test_scaffold_engine.py`
- `tests/test_api_routes.py`

**禁止**修改白名单之外的任何文件。**禁止**调用 `run_command`（由 PM 统一复跑门禁验收），**禁止** git commit/push。
