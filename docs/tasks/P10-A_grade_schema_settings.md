# P10-A 任务契约：学段学科分区基座 — Schema 迁移 + grade_settings 服务

## 上下文

用户需求（2026-11-16，六项决策已锁定）：按「学段学科」（自由单字符串，如「小学奥数」「高中数学-高一」）管理全部题目/知识网/Elo。本契约是 P10 分区功能的地基：新表 + 四表加列回填迁移 + 条目 CRUD 服务。后续 P10-B（Elo 分区）/P10-C（知识网分区）/P10-D（录入/盲测接线）/P10-E（API）/P10-F（前端）依赖本契约。

真实库现状（PM 已勘察）：题目分属「小学奥数」（10 题）与「高中数学-高一」（3 题）；19 个活跃知识节点**无跨学段共享**；elo_mutation_ledger 2 条事件（均属小学奥数）；concept_co_errors 34 条边。迁移回填无歧义。

权威设计：`docs/designs/adaptive_practice_harness.md`（零预设考纲、事件溯源、可逆回滚红线不变）。

## 修改方案

### 1. `backend/db/schema.py`

1. `TABLE_DDLS` 追加第 11 张表：
   ```sql
   CREATE TABLE IF NOT EXISTS grade_subjects (
       grade_subject TEXT PRIMARY KEY,
       created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
   );
   ```
2. 索引追加（IF NOT EXISTS）：`questions(target_grade_and_subject)`、`knowledge_nodes(grade_subject, l1_module, l2_entity)`、`elo_mutation_ledger(grade_subject, l1_module)`、`concept_co_errors(grade_subject)`、`compaction_audit_ledger(grade_subject)`。
3. 新增 `_migrate_grade_scoping(conn)`，在 `init_db` 建表后调用（同一连接，幂等）：
   - 对 `knowledge_nodes` / `elo_mutation_ledger` / `compaction_audit_ledger` / `concept_co_errors` 四表：`PRAGMA table_info` 检测无 `grade_subject` 列则 `ALTER TABLE ... ADD COLUMN grade_subject TEXT NOT NULL DEFAULT ''`；
   - 回填（仅 `grade_subject = ''` 的行，保证幂等）：
     - `knowledge_nodes`：经 `question_steps JOIN questions` 取挂载题的 DISTINCT `target_grade_and_subject`；**同一节点出现多个不同学段 → 抛 ValueError（数据违约，绝不静默择一）**；无自身步骤的 redirect 子节点继承其 redirect 目标的学段；无步骤且无目标学段的节点保持 ''（计数汇报，真实数据不应存在）；
     - `elo_mutation_ledger`：JOIN questions 按 question_uid 回填；
     - `compaction_audit_ledger`：按 from_node_id 回填（节点回填之后执行）；
     - `concept_co_errors`：按 node_u_id 回填；
   - `INSERT OR IGNORE INTO grade_subjects SELECT DISTINCT target_grade_and_subject, CURRENT_TIMESTAMP FROM questions`（现有学段自动注册为条目）。
4. 迁移纯增量非破坏：不删改任何既有数据值（除 '' → 实际学段回填）；重复 init_db 零变化。

### 2. `backend/services/grade_settings.py`（新文件）

pydantic DTO + 服务函数（风格参照 ingest_service/elo_engine 既有约定，全类型注解过 mypy 严格）：
- `GradeSubjectItem(BaseModel)`: `grade_subject: str`, `question_count: int`, `created_at: str`。
- `GradeSubjectInUseError(Exception)`：删除被拒专用（携带 grade_subject 与 question_count；API 层后续映射 409）。
- `list_grade_subjects(conn) -> list[GradeSubjectItem]`：LEFT JOIN questions 计数，按 created_at 排序。
- `create_grade_subject(conn, name: str) -> tuple[GradeSubjectItem, bool]`：strip 后非空校验（空/纯空白 → ValueError）；已存在 → 幂等返回 (既有项, False)；新建 → (新项, True)。
- `get_or_create_grade_subject(conn, name: str) -> str`：供 P10-D 录入接线；strip 非空校验；返回规范化名称。
- `rename_grade_subject(conn, old: str, new: str) -> dict[str, int]`：old 不存在/new 已存在/new 为空 → ValueError；**单事务**级联改标 `grade_subjects`（UPDATE 主键）+ `questions.target_grade_and_subject` + 四张分区表的 grade_subject WHERE old；返回各表受影响行数。
- `delete_grade_subject(conn, name: str) -> None`：该学段下题目数 > 0 → 抛 `GradeSubjectInUseError`；否则删除条目（单事务）。

### 3. 测试

`tests/test_schema.py`：
- 表数断言 10 → 11（**PM 预授权期望演进**，注释引用 P10-A）；grade_subjects 列结构断言；
- 迁移幂等：init_db 两次，四表 grade_subject 列存在且回填结果不变；
- 回填正确性：init_db 后以 raw SQL 种子 legacy 行（grade_subject=''）：两学段各自节点/步骤/题目/账本/审计/边 → 再跑 `_migrate_grade_scoping` → 逐表断言回填值；跨学段共享节点 → ValueError；redirect 子节点继承目标学段；grade_subjects 自动注册 DISTINCT 学段。

`tests/test_grade_settings.py`（新文件）：
- create 新建/幂等/空名拒绝；list 计数正确（含 0 题条目）；
- rename 级联：五处（条目主键/questions/四分区表）全部改标、行数返回正确、单事务（失败回滚无残留——构造 new 已存在冲突验证）；
- delete：有题目 → GradeSubjectInUseError（携带计数）；空条目 → 删除成功；
- get_or_create：新建/既有/空名拒绝。

## 文件白名单

- `backend/db/schema.py`
- `backend/services/grade_settings.py`（新建）
- `tests/test_schema.py`
- `tests/test_grade_settings.py`（新建）

**禁止**修改白名单之外任何文件（含其他测试文件/conftest/服务引擎——分区接线属 P10-B/C/D）；**禁止** git commit/push；**禁止**任何网络调用。
允许本地 targeted pytest / ruff / mypy 自检（.venv/）。注意：本契约只加列与服务，**不改任何既有引擎行为**，全量既有测试必须保持绿（表数断言除外）。
契约与设计冲突时以设计文档为准并向 PM 汇报。
