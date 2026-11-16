# P10-C 任务契约：知识网引擎学段学科分区

## 上下文

P10 分区功能第二切片（依赖 P10-A 已交付：`knowledge_nodes.grade_subject` / `concept_co_errors.grade_subject` / `compaction_audit_ledger.grade_subject` 列与回填）。知识网全部操作缩到学段作用域：召回/复用/Compaction/标红边/指标/树注入按学段隔离。**Compaction 作用域由 (L1, L2) 升级为 (grade_subject, L1, L2) 三元组**——消除现有跨学段误合并隐患（真实库中「方程与不等式」在小学奥数与高中数学-高一各自长节点）。

**核心设计（PM 已定）**：
- 按 uid 操作的函数签名不变：`node_state(conn, node_id)`、`record_co_error_edges(conn, question_uid)`、`decrement_co_error_edges_on_mastery(conn, question_uid)`、`revert_compaction_batch(conn, batch_id)`（学段从题目/节点/批次行自派生；标红边与审计写入时落 grade_subject）。
- 作用域类函数加 grade 参数（conn 后）：`CanonicalNodeResolver(conn, grade_subject)`、`match_or_create_l3_node(conn, grade_subject, l1, l2, ...)`、`run_scoped_compaction(conn, grade_subject, l1, l2)`、`_run_scoped_compaction_async` 同、`all_node_states(conn, grade_subject)`、`compute_graph_metrics(conn, grade_subject, l1_module)`。
- 水位线 key 升级 `(db_key, grade, l1, l2)`；`_new_nodes_since_last_compaction` 计数限同学段。
- `_assert_same_scope` 扩展学段相等断言（跨学段合并硬拒，与跨 L1/L2 同级红线）。
- redirect 解析链同学段内（迁移保证 + 新建/合并均同学段，解析器 SQL 加 grade 过滤防御）。
- 回滚逐字段恢复逻辑不变（快照含 grade_subject 列自然恢复）。

## 修改方案（仅 `backend/services/knowledge_graph.py`）

1. `CanonicalNodeResolver.__init__(conn, grade_subject)`：resolve/活跃节点查询加 `grade_subject = ?`。
2. `match_or_create_l3_node`：加 grade 参数；`_active_nodes(conn, grade, l1, l2)` 召回限同学段；新建节点 INSERT 落 grade_subject；复用判定/aliases/Jaccard 预筛逻辑不变（域内）。
3. Compaction 全链（`run_scoped_compaction` / async 变体 / `_raw_nodes_since_last_compaction` / `_new_nodes_since_last_compaction` / `_record_compaction_watermark` / `_merge_pair` / `_build_compaction_pairs`）：加 grade 维度；审计 INSERT 落 grade_subject；`_assert_same_scope` 含学段。
4. `all_node_states(conn, grade_subject)`：JOIN 过滤限学段；`node_state` 不变。
5. `record_co_error_edges`：从题目行取学段；边 INSERT/UPDATE 落 grade_subject（同学段两端点由构造保证）；`decrement_co_error_edges_on_mastery` 按 uid 操作不变。
6. `compute_graph_metrics(conn, grade_subject, l1_module)`：节点/步骤/最近 W=20 题统计全部限 (学段, 模块)；冷启动除零保护不变。
7. `revert_compaction_batch` / `_revert_single_merge`：按 batch 行操作不变（恢复快照含新列）。
8. 模块 docstring 更新作用域描述。

## 测试（`tests/test_knowledge_graph_dsu.py`）

**预授权期望演进（机械性，注释引用 P10-C）**：作用域类函数调用补 grade 实参；raw SQL 种子节点/边/审计行补 grade_subject；resolver 构造补 grade。**既有核心断言（跨 L1/L2 硬拒、两跳回滚逐字段精确恢复、悲观半格、自环消除、冷启动无 NaN、Elo 正交）一律不动。**

新增测试：
1. **跨学段隔离**：两学段同名 (L1, L2, L3) → 各自独立节点；召回不跨学段复用；compaction 扫描不跨学段配对；`_assert_same_scope` 跨学段硬拒。
2. **学段内 compaction 全链**：同学段近义合并 → 审计行 grade_subject 正确；回滚精确恢复（含 grade 列）。
3. **标红边学段归属**：record_co_error_edges 落题目学段；同学段边正常生长/销账。
4. **指标分区**：两学段同名 L1 各自独立 mastery_rate/convergence；冷启动学段返回 0 无 NaN。
5. **水位线分区**：(db, gradeA, l1, l2) 0-merge 水位线不影响 gradeB 同 (l1, l2) 触发计数。

## 文件白名单

- `backend/services/knowledge_graph.py`
- `tests/test_knowledge_graph_dsu.py`

**禁止**修改白名单之外任何文件（ingest/main 的调用接线属 P10-D/E；签名变更导致的它们暂时红是预期的，PM 统一复跑——你只需保证白名单内测试绿 + ruff/mypy 白名单文件零告警）；**禁止** git commit/push；**禁止**网络调用。
契约与设计冲突时以设计文档为准并向 PM 汇报。
