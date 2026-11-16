# P9-A 任务契约：重入库已掌握题的掌握态回显（零 Elo 漂移）

## 上下文

HANDOFF 待办：「重入库卡 `auto_mastered` 恒 False（零漂移决策）；如需回显掌握态属后续增量」。
根因：`backend/services/ingest_service.py` 的两条幂等重入库路径（`short_circuit` 与 `stem_updated`）调用 `_gate_card_from_db(conn, existing, latest, is_reimport=True)` 时不传 `auto_mastered`，导致已 `MASTERED` 的题重入库仍渲染门禁练习流。前端 `auto_mastered === true` 成功卡分支（`renderMasteredCard`）已就绪且已含 ♻️ 重入库徽章。

**用户已批准的行为**：仅回显掌握态，绝不重复结算 Elo（零漂移）；带新草稿的重入库路径行为完全不变（新草稿 = 重新诊断，属既有设计）。

## 修改方案

### 1. `backend/services/ingest_service.py`

在 `ingest_question` 的两条幂等重入库分支（`not stem_changed and not has_new_drafts` 短路分支、`stem_changed and not has_new_drafts` 题干更新分支）返回前：

- 若 `str(existing["mastery_state"]) == "MASTERED"`：
  - 查询该 uid 最新账本解释：`SELECT explanation_text FROM elo_mutation_ledger WHERE question_uid = ? ORDER BY event_id DESC LIMIT 1`；
  - `mastery_explanation` = 有账本行时 `"幂等重入库，未重复结算。" + row["explanation_text"]`；无账本行时 `"该题已掌握；幂等重入库保留历史进度与 Elo 账本，未重复结算。"`；
  - 调 `_gate_card_from_db(conn, existing, latest, is_reimport=True, auto_mastered=True, mastery_explanation=...)`；
  - 在既有 `ingest.reimport` 日志事件 payload 中追加字段 `mastery_echoed=True`（未回显分支追加 `mastery_echoed=False`，保持事件可审计）。
- 非 MASTERED（ACTIVE / PENDING_RETEST）：行为与现状完全一致（`auto_mastered=False`）。
- **零漂移红线**：两条路径不得新增任何 `elo_mutation_ledger` / `question_escrow_pool` 写入，不得新建会话，不得改动 `mastery_state`。
- `_persist_reimport_draft_session`（带新草稿重入库）**不改**：即使题目 MASTERED 也按既有逻辑新建诊断会话、`auto_mastered=False`。
- 同步更新 `_gate_card_from_db` docstring 中「重入库路径不传该标记」一句及模块 docstring 相关不变量描述，使其与新行为一致。

### 2. `frontend/app.js`

- `auto_mastered === true` 分支的 toast 改为区分重入库：
  `dto.is_reimport ? '♻️ 幂等重入库：该题已掌握，历史进度与 Elo 账本已保留（未重复结算）' : '✅ 已掌握入库：知识点已提取，Elo 水位线已结算'`。
- `renderMasteredCard(dto)`：当 `g.is_reimport` 为真时，标题改为 `已掌握（重入库回显）`，副标题改为 `幂等重入库 · 历史进度与 Elo 账本已保留 · 未重复结算`；非重入库文案保持现状。♻️ 徽章逻辑已存在，不动。

### 3. 测试

`tests/test_ingest_service.py`（沿用既有 mock LLM / tmp_db fixture 模式）：
1. 新题以 `mastered_claim=True` 录入（auto_mastered）→ 同内容无草稿重入库：断言 `auto_mastered is True`、`is_reimport is True`、`mastery_explanation` 含「未重复结算」、steps 全 `completed`、`floor_preview_elo is None`；并断言重入库前后 `elo_mutation_ledger` 行数与 `diagnostic_sessions` 行数均不变（零漂移）。
2. MASTERED 题仅题干错别字修正重入库（stem_changed、无新草稿）：同样回显，账本/会话行数不变，`stem_hash` 已更新。
3. ACTIVE 题重入库：`auto_mastered is False`（回归）。
4. PENDING_RETEST 题（测试内 SQL 置状态）重入库：`auto_mastered is False`。
5. MASTERED 题带新草稿重入库：新建会话、`auto_mastered is False`（钉住范围决策）。

`tests/test_api_routes.py`：
6. API 层连续两次 POST `/api/v1/questions/ingest`（同题干、mock 模式、首次 mastered_claim=true）：第二次响应 `auto_mastered=true` 且 `is_reimport=true`。

## 文件白名单

- `backend/services/ingest_service.py`
- `frontend/app.js`
- `tests/test_ingest_service.py`
- `tests/test_api_routes.py`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**任何网络调用（测试一律 mock/本地）。
允许本地运行 targeted pytest / ruff / mypy 自检；PM 将独立复跑全量门禁验收。
契约与设计冲突时以 `docs/designs/adaptive_practice_harness.md` 为准并向 PM 汇报。
