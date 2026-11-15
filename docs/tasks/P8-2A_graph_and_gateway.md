# P8-2A 任务契约：知识图谱查询/Compaction 优化与网关客户端复用

## 上下文
Review 发现以下并发阻塞与性能瓶颈：
1. `GET /api/v1/knowledge-graph` 对每个活跃节点循环调用 `node_state(conn, id)`，每次触发 `question_steps JOIN questions` 全表扫描（O(N*M)）。
2. `_new_nodes_since_last_compaction` 仅以 `compaction_audit_ledger.created_at` 为基准；若某次 compaction 扫描结果无可合并对（0 merges，未写审计账本），此后同 L2 下每新建一个 L3 节点都会重复触发 O(N^2) 全量扫描。
3. `match_or_create_l3_node`（async）同步调用 `run_scoped_compaction`，后者通过 `_run_sync`（`ThreadPoolExecutor` + `asyncio.run`）阻塞事件循环线程。
4. `backend/main.py` 的异步路由（`confirm_gate`、`step_submit`、`get_session`、`retry_session_prewarm`）直接在事件循环调用同步 `_require_session`。
5. `llm_gateway.chat_json` 每次调用都新建并销毁 `httpx.AsyncClient`，未复用连接池。

## 修改方案

### 1. `backend/services/knowledge_graph.py`
- 新增并导出 `all_node_states(conn: sqlite3.Connection) -> dict[str, str]`：
  - 仅执行一次 `SELECT s.knowledge_node_id, q.mastery_state FROM question_steps s JOIN questions q ON q.question_uid = s.question_uid WHERE q.origin_type = 'USER_CANONICAL' AND s.is_blocked_step = 1`，复用单个 `CanonicalNodeResolver(conn)`，计算并返回所有活跃主节点的 `{node_id: state}`（默认 `"MASTERED"`）。
- 提取公共候选对构建逻辑，新增 `async def _run_scoped_compaction_async(conn: sqlite3.Connection, l1: str, l2: str) -> str | None`：
  - 与 `run_scoped_compaction` 行为完全一致，但直接 `await _judge_nodes(payload)`，不再经过 `_run_sync` / `ThreadPoolExecutor` 阻塞事件循环。
  - 在 `match_or_create_l3_node` 中：若 `run_scoped_compaction is _ORIG_RUN_SCOPED_COMPACTION`（未被测试 monkeypatch），`await _run_scoped_compaction_async(conn, l1, l2)`；若已被 monkeypatch（测试 spy/桩），仍调用 `run_scoped_compaction(conn, l1, l2)` 保持测试兼容。
- 维护内存扫描水位线 `_NO_MERGE_SCAN_WATERMARKS: dict[tuple[str, str, str], tuple[str, int]]`：
  - key 为 `(db_key, l1, l2)`（其中 `db_key` 取 `PRAGMA database_list` 的 file 路径，若为空串则取 `f"mem:{id(conn)}"`），value 为 `(anchor_node_id, count_since_ledger)`。
  - 当 compaction 扫描返回 `batch_id is None` 时记录当前 `(anchor_node_id, count_since_ledger)`；当 `batch_id is not None`（账本推进）时清除该 key。
  - `_new_nodes_since_last_compaction` 计算基于 `compaction_audit_ledger` 之后的计数 `raw_count` 后，若存在水位线且确认 `anchor_node_id` 仍存在于当前 `conn` 的 `knowledge_nodes` 中（防止重建/内存连接地址复用误命中），返回 `max(0, raw_count - count_at_scan)`；否则清除失效水位线并返回 `raw_count`。

### 2. `backend/main.py`
- `_build_graph_response(conn)` 改为一次调用 `states = knowledge_graph.all_node_states(conn)`，按 `states.get(str(row["node_id"]), "MASTERED")` 填充节点状态。
- `confirm_gate`、`step_submit`、`get_session`、`retry_session_prewarm` 中使用 `await asyncio.to_thread(_require_session, session_id)`，避免在事件循环线程执行同步 DB 查询。
- `_lifespan` 退出阶段调用 `await aclose_shared_clients()` 清理共享客户端。

### 3. `backend/services/llm_gateway.py`
- 使用 `weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[Any, float, httpx.AsyncClient]]` 按运行中的事件循环复用共享 `httpx.AsyncClient`；当 `httpx.AsyncClient` 类/工厂（支持测试 monkeypatch `install_handler`）或 `settings.LLM_TIMEOUT_SECONDS` 变化或旧客户端已关闭时再重建（并关闭旧客户端）。
- 新增并导出 `aclose_shared_clients() -> None`，在应用 lifespan 退出时安全关闭当前事件循环关联的共享客户端。

### 4. 测试（`tests/test_knowledge_graph_dsu.py`, `tests/test_llm_gateway.py`）
- `tests/test_knowledge_graph_dsu.py`：验证 `all_node_states` 与逐节点 `node_state` 结果一致；验证 0-merge compaction 扫描后第 16 个节点不重复触发 `run_scoped_compaction`（直到新增满下一批阈值 30 时才再次触发）。
- `tests/test_llm_gateway.py`：验证多次真实模式 `chat_json` 调用在同事件循环、同配置下复用同一个 `AsyncClient` 实例，且 `aclose_shared_clients()` 正确关闭。

## 文件白名单
- `backend/services/knowledge_graph.py`
- `backend/services/llm_gateway.py`
- `backend/main.py`
- `tests/test_knowledge_graph_dsu.py`
- `tests/test_llm_gateway.py`
