# P11-C 任务契约：新草稿重入库多解分支持久化与学段改名 Frontmatter 同步

## 上下文

Review 发现两处后端数据一致性 Bug：
1. **Bug 5（带新草稿重入库丢失 `alt_branch_steps`）**：当已有题目补充新草稿重入库时，若 VLM 诊断识别出非默认解法（`matched_branch_id = 'user_alt_1'` 且 `alt_branch_steps` 非空），`_persist_reimport_draft_session` 仅调用 `_insert_session`，未将 `alt_branch_steps` 写入 `question_steps` 表。结果新会话绑定了 `'user_alt_1'` 但表内无该分支步骤；更严重的是，若替代分支步数多于 `canonical` 且卡在靠后步骤，确认门禁 `confirm_diagnostic_gate` 会因步骤越界直接报 400。
2. **Bug 6（后端部分：学段改名未同步 Frontmatter）**：`grade_settings.rename_grade_subject` 级联更新了 `questions.target_grade_and_subject` 与四张分区表的 `grade_subject`，但未同步更新 `questions.stem_md` 顶部 YAML Frontmatter 内的 `target_grade_and_subject: <old>` 行（导致导出/重读 `stem_md` 时与列值不一致）。

## 修改方案

### 1. `backend/services/ingest_service.py`

1. 将 `_persist_reimport_draft_session` 改为 `async def`，在 `ingest_question` 中 `await` 调用。
2. 在 `_persist_reimport_draft_session` 中：
   - 保持「重入库绝不改写 `mastery_state` / 盲测字段 / Elo 账本 / 托管池，也不改动既有 `canonical` 步骤行」的既有不变量不变；
   - 若 `payload.alignment.alt_branch_steps` 非空：
     - 查询该 `uid` 在 `question_steps` 中已有的 `solution_branch_id` 集合 `existing_branches`；
     - 从 `payload.alignment.matched_branch_id`（默认 `'user_alt_1'`）出发，若该分支名已存在于 `existing_branches`，递增生成不冲突的新分支名 `user_alt_2` / `user_alt_3` / ...（保留旧会话所引用的历史分支）；
     - 构造更新后的 `alignment = payload.alignment.model_copy(update={"matched_branch_id": branch_id})`；
     - 使用 `fresh_existing` 的 `(c_score, s_score, t_score, l_score)` 与 `base_elo` 以及 `tree_grade = str(fresh_existing["target_grade_and_subject"])`，逐条调用 `clamp_substep_difficulty` 与 `await match_or_create_l3_node(conn, tree_grade, ...)`，再经 `_insert_step_row` 将该分支步骤写入 `question_steps`；
     - 随后用该 `alignment` 调用 `_insert_session`，并基于 `fresh_existing` 返回 `_gate_card_from_db`。
   - 若 `payload.alignment.alt_branch_steps` 为空（`canonical`）：`question_steps` 零新增/零修改，与既有行为完全一致。

### 2. `backend/services/grade_settings.py`

1. 在 `rename_grade_subject(conn, old, new)` 的 SAVEPOINT 事务内：
   - 先保持 `UPDATE questions SET target_grade_and_subject = ? WHERE target_grade_and_subject = ?`（保持 `counts["questions"]` 返回语义与行数完全一致）；
   - 对改标命中的题目同步检查其 `stem_md` 顶部 YAML Frontmatter（仅限首行 `---` 到闭合 `---`/`...` 之间）：若存在 `target_grade_and_subject:` 行，将其值改写为 `new_name` 并原地更新 `stem_md`（**绝不修改 `stem_hash` 与 `question_uid`**，也绝不误改正文文本）。

### 3. `tests/test_ingest_service.py` 与 `tests/test_grade_settings.py`

- `tests/test_ingest_service.py`：
  - 新增测试：已有题携带新草稿重入库且 VLM 返回 `matched_branch_id='user_alt_1'`、`alt_branch_steps`（例如 4 步，多于 `canonical` 的 3 步，卡在 Step 4）时，新分支步骤正确持久化到 `question_steps`、`GateCardDTO.steps` 展示该替代分支 4 步、`canonical` 3 步完好无损、Elo/掌握态零漂移；且再次携带新草稿又识别出 `user_alt_1` 时自动分配 `user_alt_2` 无 `UNIQUE` 冲突。
- `tests/test_grade_settings.py`：
  - 新增测试：`questions.stem_md` 含 `---\nquestion_uid: ...\ntarget_grade_and_subject: <old>\n---` 时，执行 `rename_grade_subject` 后 `stem_md` frontmatter 同步变为 `<new>`，正文（含正文里出现的相同文字）、`stem_hash`、`counts["questions"]` 均保持精确。

## 文件白名单

- `backend/services/ingest_service.py`
- `backend/services/grade_settings.py`
- `tests/test_ingest_service.py`
- `tests/test_grade_settings.py`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**网络调用。
