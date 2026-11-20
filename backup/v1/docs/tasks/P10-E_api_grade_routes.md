# P10-E 任务契约：API 路由学段分区 + Settings 端点 + E2E 分区贯通

## 上下文

P10 分区第五切片（依赖 P10-A/B/C/D 已交付）。main.py 路由层接入学段作用域 + 新增 Settings CRUD 端点；E2E 全链路分区贯通。本切片交付后**全量门禁必须复绿**（此前各切片的预期红全部由本切片接线消除）。

**调用点勘察（PM 已核实）**：
- `_build_graph_response(conn)`：`CanonicalNodeResolver(conn)` / 节点全表 SELECT / l1_modules 并集（节点+questions）/ `all_node_states(conn)` / `compute_graph_metrics(conn, l1)` / red_edges resolve / compaction_batches —— 全部需加学段过滤。
- `_build_elo_ledger_response(conn, l1_module)`：l1_modules 并集 / 事件 SELECT / 托管池 SELECT（**池无 grade 列，JOIN questions 过滤**）/ `get_module_elo(conn, module)` —— 全部需加学段。
- drills GET → `get_daily_blind_drill_queue(conn, day)`（P10-D 已加 `grade_subject` keyword 参数）；drills POST 按 uid 操作**不需要** grade。
- ingest 路由 target_grade Form 字段不变（P10-D 服务层已 get_or_create + 规范化）。

## 修改方案

### 1. `backend/main.py`

1. **Settings CRUD 端点**（grade_settings 服务，P10-A）：
   - `GET /api/v1/settings/grade-subjects` → `list[GradeSubjectItem]`；
   - `POST /api/v1/settings/grade-subjects`，body `{grade_subject: str}` → `{item: GradeSubjectItem, created: bool}`；ValueError → 400；
   - `PATCH /api/v1/settings/grade-subjects/{name}`，body `{new_name: str}` → `{renamed: str, affected: dict[str, int]}`；ValueError → 400；
   - `DELETE /api/v1/settings/grade-subjects/{name}` → 204；`GradeSubjectInUseError` → **409** `{detail: 异常消息}`；ValueError → 400；
   - 路径参数含中文（FastAPI 自动 URL 解码）；写操作单连接事务提交（沿用既有路由模式）。
2. **学段参数**（必填 Query，缺失 → FastAPI 422）：
   - `GET /api/v1/knowledge-graph?grade_subject=X` → `_build_graph_response(conn, grade_subject)`；
   - `GET /api/v1/elo/ledger?grade_subject=X&l1_module=Y` → `_build_elo_ledger_response(conn, grade_subject, l1_module)`；
   - `GET /api/v1/drills/daily?grade_subject=X&active_date=...` → 队列传 `grade_subject=...`；
   - POST drills / sessions 系 / ingest / revert / assets：**不加** grade（uid/batch 操作学段自派生）。
3. `_build_graph_response(conn, grade_subject)`：resolver 加 grade；节点 SELECT `WHERE grade_subject = ?`；l1_modules 并集两路均限学段（questions 路 `WHERE target_grade_and_subject = ?`）；all_node_states/compute_graph_metrics 传 grade；red_edges SELECT 加 `WHERE grade_subject = ?`；batches SELECT 加学段过滤。
4. `_build_elo_ledger_response(conn, grade_subject, l1_module)`：l1_modules 并集限学段；事件 SELECT 加 `AND grade_subject = ?`；托管池 `JOIN questions q ON q.question_uid = p.question_uid WHERE q.target_grade_and_subject = ? AND p.l1_module = ? AND p.escrow_status = 'FROZEN_PENDING_BLIND'`；get_module_elo 传 grade。
5. 响应 DTO 结构**不变**（前端 normalize 层零改动前提）；模块 docstring 补分区说明。

### 2. 测试

`tests/test_api_routes.py`（机械演进预授权：既有 graph/elo/drill 调用补 grade_subject 参数，注释引用 P10-E；断言不动）新增：
- Settings CRUD 全链：create（新建/幂等/空名 400）→ list 计数 → rename 级联（questions/节点/账本/边学段全改）→ delete 空条目 204 / 有题 409（detail 含计数）/ 不存在 rename 400；
- grade 必填：graph/elo/drill GET 缺 grade_subject → 422；
- 分区响应：两学段同名 L1 各有题/节点/事件 → graph 响应只含当前学段节点/边/批次/指标；elo 响应只含当前学段模块与事件（另一学段 1200 冷启动不串）；drill 队列只含当前学段题；
- ingest 自动建条目后 settings list 可见。

`tests/test_e2e_cuj_flows.py`（机械演进预授权同上）：
- CUJ1~4 全链路补 grade 上下文保持绿；
- 新增 **跨学段 E2E**：A/B 两学段各录一道错题（mock）→ 各自走确认门禁 → graph/elo/drill 三端点逐学段断言隔离（节点/事件/题单互不串）→ A 学段盲测销账不影响 B 学段 Elo。

`tests/test_observability.py`（机械演进预授权同上）：直接构造 resolver/种子节点处补 grade；事件断言不动（C 已给 node_created 等事件补 grade 字段，如有字段集断言按新增字段演进，注释引用）。

## 文件白名单

- `backend/main.py`
- `tests/test_api_routes.py`
- `tests/test_e2e_cuj_flows.py`
- `tests/test_observability.py`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**网络调用（测试全 mock/本地）。
**本切片交付后全量门禁必须绿**：`.venv/bin/python -m pytest -q --cov=backend --cov-fail-under=85` + ruff + mypy 全量零告警。如有白名单外残留红，溯源汇报 PM，不得擅自改白名单外文件。
契约与设计冲突时以设计文档为准并向 PM 汇报。
