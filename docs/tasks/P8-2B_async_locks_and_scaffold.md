# P8-2B 任务契约：SQLite 事务解耦与脚手架引擎延迟/完整性优化

## 上下文
1. `ingest_service.ingest_question` 与 `_reimport_question` 在持有 `sqlite3.Connection` 甚至已执行 `INSERT/UPDATE` 写锁期间执行耗时的 VLM HTTP 调用（`analyze_question_and_draft`）。
2. `scaffold_engine.confirm_diagnostic_gate` 切换到 `ALREADY_MASTERED` 时未扣减 `ingest` 阶段可能已写入的 co-error 共错边。
3. `_dual_blind_solve` 串行发起两次 LLM 请求且未使用 `answers_equivalent` 判定双盲一致性；`_scaffold_parent_uid` 直接 `LIMIT 1` 在共享脚手架被多题复用时存在歧义。

（注：设计 §3.1 与 `test_observability.py:test_prewarm_dual_blind_fallback_events` 显式要求 `TRAP_OR_CALC_ERROR` 仍预热双产物，故保留双分支并发预热。）

## 修改方案

### 1. `backend/services/ingest_service.py`
- 重构 `ingest_question` 生命周期，确保所有 `analyze_question_and_draft` 与 `answers_equivalent` 调用均在**无打开数据库连接、无 SQLite 写锁**的状态下执行：
  1. 先短连接只读查询 `existing`、`latest`（若存在）和 `tree = _build_l1_l2_tree(conn)`，随后立即关闭连接。
  2. 若为重入库且无新草稿：开短事务处理短路或仅 `stem_changed` 更新后返回，绝不跨网络持锁。
  3. 若需调用 VLM（新题或含新草稿重入库）：在连接外 `await analyze_question_and_draft(...)` 及判定 `mastered`（`await asyncio.to_thread(answers_equivalent, ...)`）。
  4. 仅在诊断结果校验通过后，打开短事务完成落库（`_reimport_question` 的 `UPDATE questions` 与 `_insert_session` 同事务提交；`_ingest_new_question` 完成 `match_or_create_l3_node`、写入题目/步骤/会话/Elo/标红边并原子提交）。

### 2. `backend/services/scaffold_engine.py`
- `confirm_diagnostic_gate` 中 `ALREADY_MASTERED` 分支：若题目的 `mastery_state` 原本非 `'MASTERED'`，更新为 `'MASTERED'` 时同事务调用 `knowledge_graph.decrement_co_error_edges_on_mastery(conn, question_uid)`。
- `_dual_blind_solve`：
  - 使用 `await asyncio.gather(...)` 并发调用两次独立 `chat_json`；
  - 一致性比对改用 `await asyncio.to_thread(answers_equivalent, a1, a2)`，支持复合数量与等价 LaTeX。
- `_scaffold_parent_uid(conn, scaffold_uid, original_uid)`：
  - 若 `scaffold_uid` 挂有多条 `SCAFFOLD_PRE` 出边，从当前会话 `original_uid` 沿有向 `SCAFFOLD_PRE` 图定位通向 `scaffold_uid` 的直接前驱父题（BFS 查找路径前驱，找不到再退回默认 `LIMIT 1`），消除多原题复用同一微母题时的升层歧义。

### 3. 测试（`tests/test_ingest_service.py`, `tests/test_scaffold_engine.py`）
- `tests/test_ingest_service.py`：验证在模拟 VLM 阻塞期间，另一连接可正常写入 SQLite（证明 VLM HTTP 期间未持有 SQLite 写锁，含新题与重入库带新草稿路径）。
- `tests/test_scaffold_engine.py`：
  - 验证门禁改判 `ALREADY_MASTERED` 会使原综合错题的 `concept_co_errors.active_error_count` 归零；
  - 验证 `_dual_blind_solve` 对复合数量（如 `"9小时36分"` 与 `"576分"`）判为一致；
  - 验证当两个原题复用同一个 L0 微母题时，会话 B 降阶至 L1 后答对 L1 会正确升层回原属关系链路而不会因 `LIMIT 1` 串题。

## 文件白名单
- `backend/services/ingest_service.py`
- `backend/services/scaffold_engine.py`
- `tests/test_ingest_service.py`
- `tests/test_scaffold_engine.py`
