# P10-D 任务契约：录入与盲测调度学段分区接线

## 上下文

P10 分区第四切片（依赖 P10-A/B/C 已交付）。将学段作用域接入录入链与每日盲测队列。
**调用点勘察（PM 已核实）**：
- `scaffold_engine.py` 对 elo_engine/knowledge_graph 的调用全部是**签名不变**的写入类函数（record_mastered_init/apply_draft_subgoal_floor/freeze_decrutch_escrow/decrement_co_error_edges_on_mastery）→ **零改动，不在白名单**。
- `vlm_diagnosis.py` 的 L1/L2 树由 ingest 传入（传入即已 scoped）→ **零改动，不在白名单**。
- `blind_drill_scheduler.py` 的 elo 调用均为签名不变写入类 + 全局衰减 → 仅队列选择需加学段过滤。
- 衰减 `apply_overdue_escrow_decay` 保持全局（PM 裁决），同日幂等键 `{uid}:{active_date}:ESCROW_TIMEOUT_DECAY` 已保证多学段队列调用不重复衰减（PM 已核实源码）。

## 修改方案

### 1. `backend/services/ingest_service.py`

1. `_build_l1_l2_tree(conn, grade_subject: str)`：节点查询加 `grade_subject = ? AND redirect_to_id IS NULL` 过滤（只注入当前学段骨架，防跨学段 L1 碎片化，设计 Alternative 2A  rationale）。
2. `ingest_question`：
   - 入口处 `grade = target_grade.strip()`；空/纯空白 → ValueError（API 层映射 400）；
   - 读连接内 `tree = _build_l1_l2_tree(conn, grade)`（新题用入参学段；**重入库带新草稿路径用 `existing["target_grade_and_subject"]`**——题目归属零漂移）；
   - 新题落库事务内（`_ingest_new_question`）：`grade = get_or_create_grade_subject(conn, grade)`（grade_settings 服务，P10-A 已交付）→ `questions.target_grade_and_subject` 存规范化名；`match_or_create_l3_node(conn, grade, step.l1_module, step.l2_entity, step.l3_operator, [])` 补 grade 实参（P10-C 签名）；
   - **重入库幂等红线不变**：短路/题干更新/带新草稿三路径均不改题目学段归属；重入库**不**用入参 target_grade 建条目（防误建空条目）；同内容跨学段重录命中既有 uid 时保持原归属（uid 内容确定性设计不变）。
3. `record_co_error_edges` 调用不变（P10-C 内部自派生学段）。

### 2. `backend/services/blind_drill_scheduler.py`

1. `get_daily_blind_drill_queue(conn, active_date, limit=3, *, grade_subject: str)`：候选题 SQL 加 `q.target_grade_and_subject = ?`；**每学段独立 ≤limit 配额**、独立 `slated_drill_date` 当日幂等锁、独立溢出顺延与 `system_deferred_days`（用户裁决④）；衰减调用保持全局不动。
2. `evaluate_blind_drill_submission` 与母题连带销账：签名/逻辑不变（uid 操作，学段由 P10-B/C 内部派生；SCAFFOLD_PRE 子题与母题同学段由构造保证）。
3. 模块 docstring 更新分配额描述。

### 3. 测试

`tests/test_ingest_service.py`（机械演进预授权：fixture/helper 补 grade 上下文，注释引用 P10-D；既有断言不动）新增：
1. 新题录入自动建条目（grade_subjects 出现该学段）+ 节点 grade_subject == 题目学段 + 账本行学段正确（P10-B 联动）；
2. 树注入 scoped：两学段同名 L1 各有节点 → 录 A 学段题时 VLM payload 的树只含 A 学段 L1/L2（mock 捕获断言）；
3. 重入库零漂移：同学段重录短路/改题干/带新草稿三路径题目学段不变、不新建条目；入参学段与既有归属不同时保持原归属；
4. 空学段 ValueError。

`tests/test_blind_drill_scheduler.py`（机械演进预授权同上）新增：
5. **跨学段配额隔离**：两学段各 ≥4 道到期题 → 各自队列 ≤3、各自锁定幂等、各自顺延计数互不影响；
6. 衰减全局性：两学段各一道逾期曝光题，任一学段队列调用后两者均衰减（同日二次调用不重复）；
7. 既有销账/双重核验/重训分流测试补 grade 上下文后保持绿。

## 文件白名单

- `backend/services/ingest_service.py`
- `backend/services/blind_drill_scheduler.py`
- `tests/test_ingest_service.py`
- `tests/test_blind_drill_scheduler.py`

**禁止**修改白名单之外任何文件（main.py/E2E 属 P10-E；scaffold/vlm 零改动）；**禁止** git commit/push；**禁止**网络调用。
main.py/E2E 因签名变更暂时红是预期的，PM 在 P10-E 后统一复跑；你只需保证白名单内测试绿 + ruff/mypy 白名单文件零告警。
契约与设计冲突时以设计文档为准并向 PM 汇报。
