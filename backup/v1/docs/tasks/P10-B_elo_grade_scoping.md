# P10-B 任务契约：双轨 Elo 引擎学段学科分区

## 上下文

P10 分区功能第二切片（依赖 P10-A 已交付：四表含 `grade_subject` 列，`elo_mutation_ledger.grade_subject` 已由迁移回填）。Elo 改为按 **(grade_subject, l1_module)** 独立核算：每个学段的每个 L1 模块各自 1200/1200 起算，事件折叠/水位线/守恒/幂等全部在分区内成立。

**核心设计（PM 已定）**：
- **写入类函数签名不变**（`record_mastered_init` / `apply_draft_subgoal_floor` / `freeze_decrutch_escrow` / `realize_or_forfeit_blind_test` / `apply_overdue_escrow_decay`）：学段从 `_load_question` 取 `target_grade_and_subject` 自派生；内部 scoped 读写用 (题目学段, l1)。调用方（scaffold/ingest/drill）零改动。
- **读取类函数加 grade 参数**：`get_module_elo(conn, grade_subject, l1_module)`、`replay_module_elo_from_ledger(conn, grade_subject, l1_module)`。
- 衰减 `apply_overdue_escrow_decay(conn, active_date)` 保持**全局维护**（跨学段统一按日衰减，PM 裁决：时间性维护不随用户当前学段选择）。
- 幂等键 `{uid}:{session}:{trigger}` 不变（uid 全局唯一）；Escrow 快照/守恒/不倒挂/水位线公式逐字不变，只是核算域缩到 (学段, 模块)。
- 账本解释条前缀升级为 `【{grade_subject} · {l1_module}】`（原为 `【{l1_module}】`）。

## 修改方案（仅 `backend/services/elo_engine.py`）

1. `_insert_event`：写入 `grade_subject`（来自题目行）；`INSERT` 列清单追加。
2. `get_module_elo` / `replay_module_elo_from_ledger`：签名加 `grade_subject: str`（conn 后第二参）；折叠 SQL `WHERE l1_module = ? AND grade_subject = ?`；replay 断链校验逻辑不变。
3. 四个写入函数内部：凡调用 `get_module_elo` / 折叠读取处传入题目学段；`_load_question` 已取整行，直接用 `question["target_grade_and_subject"]`。
4. `apply_overdue_escrow_decay`：候选题查询不限学段（全局）；每题衰减计算用其所属 (学段, 模块) 的托管池（escrow 按 uid 读取本就唯一，逻辑不变）。
5. 解释条前缀含学段（见上）；`explanation_text` 其余措辞不变。
6. 模块 docstring 更新分区核算域描述。

## 测试（`tests/test_elo_engine.py`、`tests/test_difficulty_and_elo.py`）

**预授权期望演进（机械性，注释引用 P10-B）**：
- `get_module_elo` / `replay` 调用处补 grade 实参（从题目行或 fixture 学段推导）；
- raw SQL 种子账本行补 `grade_subject` 值；
- 解释条断言随前缀升级更新（仅前缀部分，数值/措辞断言不动）。
**核心不变量断言（守恒 ≡、不倒挂、幂等、衰减四条件、replay 断链）一律不动。**

新增测试：
1. **跨学段隔离**：相同学段不同模块 / 不同学段同名 L1（如两学段各有「方程与不等式」）→ 各自 1200 起算；A 学段事件不影响 B 学段折叠值；get_module_elo/replay 按 (学段,模块) 精确返回。
2. **分区内守恒**：同一题全流程（保底→冻结→兑现）在 (学段,模块) 内 Δfloor+escrow≡Δtarget（既有守恒测试补 grade 维度后保留）。
3. **写入派生学段**：record_mastered_init 后账本行 grade_subject == 题目 target_grade_and_subject。
4. **衰减全局性**：两学段各有逾期托管题，一次 apply_overdue_escrow_decay 两者均衰减。
5. **解释条前缀**：含「【{学段} · {模块}】」。

## 文件白名单

- `backend/services/elo_engine.py`
- `tests/test_elo_engine.py`
- `tests/test_difficulty_and_elo.py`

**禁止**修改白名单之外任何文件（main.py/ingest/scaffold/drill 的调用接线属 P10-D/E；get_module_elo 签名变更导致的它们暂时红是预期的，PM 在 D/E 交付后统一复跑全量门禁——你只需保证白名单内测试绿 + ruff/mypy 白名单文件零告警）；**禁止** git commit/push；**禁止**网络调用。
契约与设计冲突时以设计文档为准并向 PM 汇报。
