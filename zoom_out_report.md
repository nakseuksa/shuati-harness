# Stage 5: Initial Zoom Out Review Report (`zoom_out_report.md`)

* **Target File**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md) (Lines 1..742)
* **Document Standard**: [zoom-out-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/zoom-out-standard.md)

## Reviewer Status

| Reviewer | Role Title | Status |
| :--- | :--- | :--- |
| `egm-zoom-out` | Stage 5 Initial Zoom Out Reviewer | `completed` |
| `egm-inventive-catalyst` | Stage 5 Inventive Catalyst Reviewer | `completed` |

---

## Part 1: Skeptical Architect (`egm-zoom-out`) Findings

### ZO-1: 本地预热检索声称依赖“四通道连续阈值”，但主表未存四通道特征，且与已有的 `L3 知识节点 + 陷阱标签 + Elo` 索引重复造轮子

*   **Severity**: Major
*   **Affected Scope**:
    *   `Technical Plan -> 2.1 题目主表` & `2.2 题目关联五边拓扑表` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L158-L228))
    *   `Technical Plan -> 3.2 模块 2：倒置防剧透三阶脚手架` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L387-L388))
    *   `Detailed Implementation Plan -> 9. backend/services/scaffold_engine.py` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L641-L642))
*   **Simplicity Rationale**:
    *   **架构断层与过度设计（YAGNI & Minimalist Data Flow）**：第 387 行与第 641 行规定新错题进入一阶思考窗口时，`prewarm_session_questions` 先在本地 `questions` 表中“按四通道阈值检索（如 `SCAFFOLD_PRE` 要求 $I(A \subset B) \ge 0.85$ 且 $D_B - D_A \ge 200$）”。但 `questions` 表（第 158–187 行）根本没有存储四通道特征向量（$V_{\text{surface}}, V_{\text{skeleton}}, K_{\text{set}}, E_{\text{trap}}$），而 `question_relations` 表仅存储**已知题对**的静态边——对于刚刚录入的新错题，它在 `question_relations` 中没有任何预存边，若临时与全库题目做四通道比对将产生 $O(N)$ 次 LLM/向量计算。
    *   **收敛建议**：直接复用系统已建立的结构化索引（正如 CUJ 2 第 72 行所述“检索本地题库中是否存在同考点的 `SCAFFOLD_PRE` 前置子题”）：本地检索仅需通过 `CanonicalNodeResolver` 解析后的卡点步骤 `knowledge_node_id`、`trap_taxonomy_tag` 及 `base_elo` 差值（如 $D_B - D_A \ge 200$）在 SQLite 中做确定性关系查询，命中后再写入或读取 `question_relations`，无需在单机冷启动阶段额外引入一套跨全表的四通道实时向量比对流水线。

---

### ZO-2: 每日综合盲测通过 `GET` 端点直接修改顺延状态，且缺失“当日已入选题单”持久化锚点，刷新页面即导致状态污染

*   **Severity**: Major
*   **Affected Scope**:
    *   `Technical Plan -> 2.1 题目主表` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L178-L180))
    *   `Technical Plan -> 3.3 模块 3：独立每日盲测入口、配额熔断` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L418-L419))
    *   `Technical Plan -> 4. 核心内部 API 契约` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L474))
    *   `Detailed Implementation Plan -> 12. backend/services/blind_drill_scheduler.py` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L674))
*   **Simplicity Rationale**:
    *   **违反平台标准与最小化状态流（Anti-Pattern & State Corruption）**：第 674 行规定 `get_daily_blind_drill_queue(limit: int = 3)`（对应 `GET /api/v1/drills/daily`）在查询到期题超出 3 道时，“其余到期题自动递增 `system_deferred_days` 并顺延 `blind_test_due_at`”。由于 `questions` 表没有任何字段记录“某题是否已被锁入今日（`active_date`）的 3 题题单”，用户每次刷新首页触发 `GET /api/v1/drills/daily` 都会重复递增 `system_deferred_days` 并把截止时间无限往后推；同时第 419 行要求“仅对已进入当日 3 题题单但用户超过 3 个活跃日拒做的题目执行托管差额衰减”，在无“入选题单日期”记录的前提下也无法判定哪 3 道题属于“已进入题单但拒做”。
    *   **收敛建议**：要么在 `questions` 表增加一个轻量字段 `last_slated_drill_date DATE`（按自然日幂等锁定当日 Top-3 题单），要么彻底砍掉 `system_deferred_days` 原地写库副作用——每天仅对解锁时间最早的前 3 道 `PENDING_RETEST` 题计算逾期衰减，其余排队题目因不在 Top-3 窗口内天然免于衰减，用无状态排序取代复杂的顺延写库逻辑。

---

### ZO-3: 双轨 Elo 账本中存在死分支触发器（`COMPACTION_REPLAY_ADJUST`），且事件表缺失增量/托管字段导致无法如宣称那样独立重放

*   **Severity**: Major
*   **Affected Scope**:
    *   `CUJ 4` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L97))
    *   `Technical Plan -> 2.4 elo_mutation_ledger` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L324-L349))
    *   `Non-Functional Requirements -> Rollout, Idempotency & Rollback Safety` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L540))
    *   `Detailed Implementation Plan -> 11. backend/services/elo_engine.py` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L661-L667))
*   **Simplicity Rationale**:
    *   **投机性死代码（YAGNI）与重放契约矛盾**：
        1. 系统已严格锁定并查集 Compaction **只允许在同一 `(l1_module, l2_entity)` 内部合并 `L3` 节点，严禁跨 `L1` 重定向**（第 429 行），而双轨 Elo 是按 `L1` 模块独立核算的（第 445 行）。因此同域 `L3` 节点的合并或解绑回滚（Un-merge）**绝对不会改变任何题目的 `L1` 模块归属，也绝不可能影响任何 `L1` 模块的 Elo 分值**。但在 `elo_mutation_ledger`（第 338 行）中却定义了从不会被调用的 `'COMPACTION_REPLAY_ADJUST'`，且在第 97、476 行声称回滚并查集需重放事件流。
        2. 第 540 行与第 667 行宣称 `replay_module_elo_from_ledger` 可从 `elo_mutation_ledger` 从头确定性重放重建双轨 Elo，但 `elo_mutation_ledger` 仅记录了静态结果快照（`coverage_elo_before/after`, `combat_elo_before/after`），既未记录单题托管池的冻结差额与衰减系数，也未在 `elo_engine.py` 的 4 个写入函数中使用 `'WATERMARK_CLAMPED'` 枚举。
    *   **收敛建议**：从 `elo_mutation_ledger.mutation_trigger` 中移除无用的 `'COMPACTION_REPLAY_ADJUST'`（并将 `'WATERMARK_CLAMPED'` 合并入 `'MASTERED_INIT'` 的 `watermark_gamma` 字段体现）；明确 `Un-merge` 仅需刷新知识网节点状态与收敛度指标，无需重算 Elo；将 `elo_mutation_ledger` 定位为不可变审计流水表（通过最新一条流水或独立模块状态视图读取当前分值），消除伪事件溯源重放的过度设计。

---

### ZO-4: 子步骤难度单调性裁剪公式与 SQLite `CHECK (s_sub BETWEEN 1 AND 3)` 约束存在边界冲突

*   **Severity**: Major
*   **Affected Scope**:
    *   `Technical Plan -> 2.1 question_steps` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L200-L205))
    *   `Technical Plan -> 3.5 法则 2：受单调性硬锁保护的向下兼容保底推演` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L453))
    *   `Detailed Implementation Plan -> 6. backend/services/difficulty_rubric.py` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L610))
*   **Simplicity Rationale**:
    *   **数据契约自相矛盾**：第 453 行与第 610 行对子问题推理步数 $S_{\text{sub}}$ 强制执行 $S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, S_{\text{orig}} - 1)$，但未像 $C_{\text{sub}}$ 那样加 $\max(1, \dots)$ 下界保护。当原题 $S_{\text{orig}} = 1$ 时，$S_{\text{sub}}$ 会被裁剪为 `0`，直接触发 `question_steps` 表第 201 行的 `s_sub INTEGER NOT NULL CHECK (s_sub BETWEEN 1 AND 3)` 约束报错（同理第 200 行 `c_sub` 允许 `0` 但 `calculate_base_elo` 要求 `c >= 1`）。
    *   **收敛建议**：统一将 `question_steps` 中 `c_sub` 与 `s_sub` 的取值下界对齐为 `1`，并将裁剪公式统一修正为 $S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, \max(1, S_{\text{orig}} - 1))$。

---

### ZO-5: `compaction_audit_ledger` 主键设计与两跳路径压缩回滚存在数据丢失死胡同

*   **Severity**: Major
*   **Affected Scope**:
    *   `Technical Plan -> 2.3 compaction_audit_ledger` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L259-L271))
    *   `Technical Plan -> 3.4 同域并查集 Compaction 与不变量维护` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L430-L434))
    *   `Detailed Implementation Plan -> 10. backend/services/knowledge_graph.py` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L651-L652))
*   **Simplicity Rationale**:
    *   **Architectural Dead-End**：
        1. `compaction_audit_ledger` 以 `batch_id TEXT PRIMARY KEY` 作为主键，却在单行内只存一对 `(from_node_id, to_node_id)`。若单次 Compaction 批次在同一 `(l1, l2)` 下合并了多对近义节点，共用 `batch_id` 会触发主键冲突。
        2. 第 431 行规定合并 `from_node_id -> to_node_id` 时，“更新 `from_node_id` 及所有原指向 `from_node_id` 的子节点，使其 `redirect_to_id = to_node_id`”。但 `compaction_audit_ledger` 并未记录这些连带被扁平化改写指针的孙节点 ID 列表；当执行第 652 行的 `revert_compaction_batch` 时，仅把 `from_node_id` 的 `redirect_to_id` 还原为 `NULL`，会导致原本指向 `from_node_id` 的子节点残留指向 `to_node_id`，破坏“100% 无损回滚”承诺。
    *   **收敛建议**：在 `compaction_audit_ledger` 中增设 `merge_id` 主键（或将 `affected_child_node_ids_json` 存入快照），回滚时将 `affected_child_node_ids_json` 中的节点指针恢复指向 `from_node_id`。

---

### ZO-6: 引入第三方 `rank-bm25` 处理无空格中文短词条属不匹配的外部依赖

*   **Severity**: Nit
*   **Affected Scope**:
    *   `Technical Plan -> 3.4 模块 4` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L427))
    *   `Detailed Implementation Plan -> 1. pyproject.toml` & `10. knowledge_graph.py` ([adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L566), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L650))
*   **Simplicity Rationale**:
    *   **CodeMin / YAGNI**：`rank-bm25` 默认依赖分词后的词表数组，而中文数理算子短语（如“放缩圆与临界相切几何构造”）不含空格；且在单一 `(l1, l2)` 作用域下每新增 15 个节点就会触发 Compaction 归并，候选集极小。直接使用第 427 行提到的 Python 标准库字符 N-gram（2-gram / 3-gram）集合重合度或 SQLite 内置匹配即可在 10 行代码内完成 `Top-15` 预筛，无需额外引入 `rank-bm25` 依赖。

---

## Part 2: Inventive Catalyst (`egm-inventive-catalyst`) Findings

### IC-1: 宏观知识网“全题连坐”悲观半格 vs. 微观草稿“前缀已掌握步骤”的认知诊断矛盾

* **Severity**: **Major**
* **Affected Scope**:
  * `CUJ 1`（[Lines 60–63](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L60-L63)）
  * `2.1 questions & question_steps 表结构`（[Lines 175–206](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L175-L206)）
  * `2.3 concept_co_errors 表结构`（[Lines 245–256](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L245-L256)）
  * `3.4 模块 4：同域并查集 Compaction 与双指标定义`（[Lines 431–441](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L431-L441)）
  * `Detailed Implementation Plan #10 knowledge_graph.py`（[Lines 650–653](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L650-L653)）
* **False Compromise（妥协与系统反噬矛盾）**:
  1. **前缀已掌握步骤被整题悲观半格“连坐降级”**：文档的核心论点（[Lines 21–28](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L21-L28)、[Lines 60–63](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L60-L63)）是学习者在做高难综合压轴题时，已在草稿上独立完成前置步骤（如 `Step 1 [电场加速动能定理]: ✅ 已独立完成`，对应 `step_index <= last_valid_step_index`），仅卡在后续步骤（`Step 2 [有界磁场放缩圆]: ❌ 思路中断`）。然而在数据模型中，`mastery_state` 仅存于整题级 `questions.mastery_state`（[Line 175](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L175)），而模块 4（[Line 431](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L431)）规定 `L3` 节点状态“由其下挂载的所有 `USER_CANONICAL` 题目动态聚合，优先级为 `🔴ACTIVE > 🟡PENDING_RETEST > 🟢MASTERED`”。这导致**每当优等生录入一道综合错题并在草稿上证明自己完全掌握 `Step 1`（其对应的基础节点 $N_1$ 原本已处于 `🟢MASTERED`）时，整题的 `🔴ACTIVE` 状态会通过 `question_steps` 无差别连坐污染 $N_1$，将已掌握的基础节点强行打回 `🔴ACTIVE`，导致版图攻克率 $\text{MasteryRate}_m$（[Line 437](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L437)）不升反降**。
  2. **跨考点标红边（`concept_co_errors`）全题笛卡尔积噪声**：同理，若对一道 4 步综合错题的所有步骤节点两两建立 `concept_co_errors`（[Lines 245–256](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L245-L256)），会将用户早已熟练走通的 `Step 1 <-> Step 2` 也误记为组合错题标红边。
  3. **收敛度复用率 $k_{\text{reuse}}$ 缺乏入库不可变快照**：模块 4 收敛度公式（[Line 439](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L439)）依赖“最近 $W=20$ 道题中直接复用老节点的次数 $k_{\text{reuse}}$”，但 `questions` 与 `question_steps` 表（[Lines 158–206](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L158-L206)）均未记录入库刻是否复用了老节点；若动态比对 `question.created_at > resolved_node.created_at`，则后台并查集 Compaction 将新节点合并入更早的主节点后，会篡改历史题目的“入库复用”计数，制造伪收敛跳跃。
* **Proposed Resolution（TRIZ #1 Segmentation 分割 + #2 Extraction 抽取）**:
  1. **步骤级状态归因分割（Step-Scoped Semilattice Attribution）**：在聚合 `knowledge_nodes` 的悲观半格状态时，结合最新确认会话的 `stalled_step_index`（或在 `question_steps` 上维护 `is_blocked_step` 标记），**仅将未独立走通的受阻步骤（`step_index >= stalled_step_index`）纳入整题 `🔴ACTIVE / 🟡PENDING_RETEST` 的悲观传染**；草稿已独立完成的前缀步骤（`step_index <= last_valid_step_index`）仅提供正向掌握证据，绝不将既有 `🟢MASTERED` 节点拉回 `🔴ACTIVE`。
  2. **标红边聚焦断点边界（Boundary-Only Co-Error Edge）**：`concept_co_errors` 仅提取卡点耦合边界对 `(resolve(node(stalled_step_index - 1)), resolve(node(stalled_step_index)))`（以及多个断层步骤之间），剔除前缀已掌握步骤间的伪错边。
  3. **入库复用布尔快照**：在 `question_steps`（或 `questions`）中增加不可变列 `reused_existing_node INTEGER NOT NULL DEFAULT 0`，在入库 `match_or_create_l3_node`（[Line 650](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L650)）命中既有节点时写入 `1`，使 $\rho_m = k_{\text{reuse}} / w$ 与后续 Compaction 合并解耦。
* **Secondary Liabilities（次生工程代价）**:
  `compute_graph_metrics` 与知识网拓扑查询需联表过滤 `step_index >= stalled_step_index`，并在 `question_steps` 中增加 1～2 个布尔状态列，略微增加 SQL 聚合视图复杂度。

---

### IC-2: 单题托管池（Escrow）“当场预支兑现”与“盲测失败/逾期回退重训”之间的非守恒通胀矛盾

* **Severity**: **Major**
* **Affected Scope**:
  * `CUJ 3`（[Lines 83–87](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L83-L87)）
  * `2.4 question_escrow_pool & elo_mutation_ledger 表结构`（[Lines 311–349](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L311-L349)）
  * `3.3 状态机 PENDING_RETEST --> ACTIVE`（[Line 411](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L411)）
  * `3.5 模块 5：法则 3 单题托管池两步销账与超时衰减`（[Lines 445, 458–464](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L445-L464)）
  * `Detailed Implementation Plan #11 elo_engine.py & #15 test_difficulty_and_elo.py`（[Lines 661–666, 700](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L661-L700)）
* **False Compromise（妥协与账本不自洽矛盾）**:
  模块 5 为平衡“当场通关变式的即时获得感”与“隔日盲测防假懂”，规定升入 `🟡PENDING_RETEST` 时立即全额结算覆盖分 $\Delta R_{\text{cov}}$，并**当场先兑现待发实战分差额的 $20\%$**，剩余 $80\%$ 存入 `question_escrow_pool.escrow_combat_delta`（[Lines 458–459, 664](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L458-L459)）；逾期未测时按日递减 `decay_factor` 并“同步回吐对应的未兑现覆盖分虚高部分”（[Line 463](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L463)）；盲测失败时“退回 `🔴ACTIVE`、清空托管池并进入重训流”（[Lines 87, 411, 665](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L411)）。
  这一设计在“失败/逾期回退 $\to$ 二次重训通关”闭环上存在三个数学与 Schema 漏洞：
  1. **盲测失败重训循环的重复预支通胀（或幂等键锁死）**：当用户在盲测中做错退回 `🔴ACTIVE` 并重走脚手架再次通过 `STAGE_4_DECRUTCH_VARIANT` 升入 `🟡PENDING_RETEST` 时，首轮已落袋的 $100\%$ 覆盖分增量与 $20\%$ 当场实战分增量并未回吐。若重训采用新 `session_id`，用户每次“盲测做错 $\to$ 重过当场变式”都会重复白嫖一次 $20\%$ 实战分与 $100\%$ 覆盖分，直接击穿 [Line 700](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L700) 要求的“全流程实战分净增量守恒”；反之若复用旧键，则会被 `elo_mutation_ledger` 的唯一键 `'{question_uid}:{session_id}:{mutation_trigger}'`（[Line 327](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L327)）拦截，导致二轮通关永远无法再次冻结或兑现托管分。
  2. **覆盖分回吐缺失字段锚点与双轨倒挂风险**：`question_escrow_pool` 表（[Lines 311–322](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L311-L322)）仅定义了 `floor_elo_applied` 与 `escrow_combat_delta`，既未存储该题已预支的 $20\%$ 实战分，也未存储该题在 `🟡PENDING_RETEST` 阶段贡献的覆盖分增量 `escrow_coverage_delta`。因此 [Line 463](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L463) 与 [Line 666](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L666) 要求的“同步回吐未兑现覆盖分虚高部分”在表中根本无据可查；且若回吐覆盖分时未做下界钳制，可能破坏 $\text{Combat\_Elo}_m \le \text{Coverage\_Elo}_m$（[Line 445](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L445)）不变量。
* **Proposed Resolution（TRIZ #4 Beforehand Cushioning 预先防范 — 单题累计已兑现水位线 High-Water Mark 账本）**:
  1. **补齐托管池全量追踪列**：在 `question_escrow_pool`（[Lines 311–322](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L311-L322)）中显式维护 `total_realized_combat_elo REAL NOT NULL DEFAULT 0.0`（累计已落袋实战分，含草稿保底分与首轮 20% 预支分）和 `escrow_coverage_delta REAL NOT NULL DEFAULT 0.0`（本次待复测周期内浮动挂载的覆盖分增量）。
  2. **守恒差额重冻结与覆盖分安全回吐**：
     * 当盲测失败退回 `🔴ACTIVE`（`FORFEITED_RESET`）或逾期衰减时，按 `(1 - decay_factor) * escrow_coverage_delta` 回吐浮动覆盖分（强制钳制 `coverage_elo_after = max(combat_elo, coverage_elo_before - rollback)` 以维护双轨不变量），而 `total_realized_combat_elo` 保持不动（不倒扣已落袋实战分）。
     * 当该题重训再次升入 `🟡PENDING_RETEST` 时，应得目标增量扣除 `total_realized_combat_elo` 后的余额 **100% 计入 `escrow_combat_delta` 冻结池（二次重训不再重复预支 20% 即时实战分）**，且覆盖分仅补足上次已回吐的部分；`idempotency_key` 格式扩展为 `'{question_uid}:{session_id}:{mutation_trigger}:{retry_seq}'`。
* **Secondary Liabilities（次生工程代价）**:
  `question_escrow_pool` 增加 2 个浮点列，`elo_engine.py` 在处理 `freeze_decrutch_escrow` 与 `realize_or_forfeit_blind_test` 时需读取历史已兑现水位线做差额幂等扣减。

---

### IC-3: 思考窗口异步预热的“门禁后置触发”与“粗心分支零思考窗口”时序矛盾

* **Severity**: **Major**
* **Affected Scope**:
  * `CUJ 1 & CUJ 2`（[Lines 64, 71](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L64-L71)）
  * `1. 系统总体架构图`（[Lines 136–138](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L136-L138)）
  * `3.2 模块 2：思考窗口投机预热`（[Lines 381–386](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L381-L386)）
  * `3.3 模块 3 状态机 STAGE_0 --> STAGE_4`（[Lines 400–401](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L400-L401)）
  * `4. 核心内部 API 契约 /confirm-gate`（[Line 472](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L472)）
  * `Detailed Implementation Plan #9 scaffold_engine.py`（[Lines 638–641](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L638-L641)）
* **False Compromise（妥协与阻塞死角矛盾）**:
  设计文档为了避免在用户确认前浪费 Token，将 `Prewarm_Worker` 的启动时机绑定在用户点击 `/api/v1/sessions/{id}/confirm-gate` **之后**（[Lines 64, 381–383, 472, 639](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L381-L386)），假设所有用户都会先进入 `STAGE_1_BLIND_PROBE` 思考一阶问题，从而掩盖后台 SymPy 沙盒的生成耗时。
  然而这与文档自身的另外两条分支产生了直接时序冲突：
  1. **`TRAP_OR_CALC_ERROR`（审题/计算粗心分支）零思考窗口直跳阻塞**：根据状态机（[Line 401](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L401)）与 `confirm_diagnostic_gate`（[Line 640](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L640)），若错因为 `TRAP_OR_CALC_ERROR`，系统**不经过 `STAGE_1`～`STAGE_3`，直接从 `STAGE_0_CONFIRM_GATE` 跳入 `STAGE_4_DECRUTCH_VARIANT` 并立即返回平行变式题（[Line 472](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L472)）**。由于此前未启动任何预热，该分支完全没有 `STAGE_1` 思考窗口可用，用户在点击确认条瞬间将直接撞上 10～25 秒的 LLM + SymPy 沙盒同步阻塞，违背 [Line 36](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L36) 与 [Line 536](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L536) 的 `< 2.0s` 延迟承诺。
  2. **一阶秒点“仍无思路”时的 In-Flight 竞态**：若用户在 `STAGE_1_BLIND_PROBE` 1 秒内直接点击“仍无思路，请求二阶微母题铺垫”（[Line 71](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L71)），此时 `prewarm_status` 仍处于 `'RUNNING'`（[Line 305](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L305)），文档未定义此时如何复用在途 `asyncio.Task`，容易引发重复派发沙盒任务或空指针读取。
* **Proposed Resolution（TRIZ #3 Preliminary Action 预先作用 + #4 Beforehand Cushioning 预先缓冲）**:
  1. **预热时机前移至 `/ingest` 完成刻（Stage-0 Window Pre-warming）**：在 `/api/v1/questions/ingest`（[Line 471](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L471)）VLM 返回初始诊断并渲染 `STAGE_0_CONFIRM_GATE` 的同时，后台立即按 VLM 首选诊断（若为 `CONCEPT_BOTTLENECK` 预热二阶微母题+变式；若为 `TRAP_OR_CALC_ERROR` 预热 `SHARED_TRAP` 变式）启动 `Prewarm_Worker`，利用用户核对步骤确认条的数秒窗口提前抢跑；仅当用户在 `/confirm-gate` 手动覆写了 `stalled_step_index` 或切换了错因（`user_overridden = 1`）时，才取消旧协程并重派预热。
  2. **In-Flight 协程单飞等待（Singleflight Task Registry）**：在 `scaffold_engine.py` 中维护会话级内存 `in_flight_prewarm_tasks: dict[str, asyncio.Task]`；当用户直跳 `STAGE_4` 或快速进入 `STAGE_2` 且 `prewarm_status == 'RUNNING'` 时，请求直接 `await` 既有在途 Task（前端展示沙盒验算进度态），杜绝重复触发或竞态失败。
* **Secondary Liabilities（次生工程代价）**:
  当用户在 `STAGE_0_CONFIRM_GATE` 手动推翻 VLM 初始诊断（低频分支）时，被取消的预热协程会产生一次废弃的 LLM 调用开销；后端需维护 `session_id -> asyncio.Task` 的生命周期取消清理。

---

### IC-4: 半程立式拆拐杖（$L \ge 2$）中“自然代数列式”与 `simplify(lhs - rhs) == 0` 脆弱判定之间的矛盾

* **Severity**: **Nit**
* **Affected Scope**:
  * `CUJ 3`（[Line 81](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L81)）
  * `3.3 模块 3：负荷解耦的当场拆拐杖验证`（[Line 416](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L416)）
  * `Detailed Implementation Plan #8 sympy_verifier.py`（[Line 630](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L630)）
  * `Detailed Implementation Plan #16 test_sympy_sandbox.py`（[Line 711](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L711)）
* **False Compromise（妥协与自相矛盾）**:
  [Line 416](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L416) 规定在 $L \ge 2$ 的半程立式验证模式中，通过 `simplify(lhs - rhs) == 0` 验证用户方程组与标准方程组在消元意义下代数等价；而 [Line 711](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L711) 的单元测试用例写明：`测试 verify_equation_equivalence 对移项变形方程（如 m*v**2/2 - q*U 与 q*U - 1/2*m*v**2）的等价判定准确率`。
  这里存在直接的数理实现矛盾：
  1. **反号与常数缩放导致 `simplify(expr1 - expr2) == 0` 必败**：以 [Line 711](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L711) 自身举出的 `E1 = m*v**2/2 - q*U`（对应 $\frac{1}{2}mv^2 - qU = 0$）与 `E2 = q*U - 1/2*m*v**2`（对应 $qU - \frac{1}{2}mv^2 = 0$）为例，`simplify(E1 - E2)` 的结果是 `m*v**2 - 2*q*U != 0`（因为 `E1 == -E2`）！同理，若用户写出 `m*v**2 - 2*q*U = 0`（两边同乘 2），差值同样不为 0。
  2. **多方程消元与中间变量符号不对齐**：在复合场或几何综合题中，若前端未限定可用符号名，用户将半径写为 `r` 而标准解为 `R`，或者用户将两步方程合并消去了一个中间变量，逐条表达式相减将 100% 误判为错误。
* **Proposed Resolution（TRIZ #3 Preliminary Action 预先作用 + #8 Intermediary 中介物）**:
  1. **前端展示受控符号表芯片（Locked Symbol Palette）**：在 $L \ge 2$ 半程立式题干下方显式渲染允许使用的已知量与必选中间变量符号芯片（从 `verification_artifact_json` 提取，如 `已知符号: m, q, U, B, d | 中间未知量: v, R`），从源头消除 `r` vs `R` 命名漂移。
  2. **非零常数比值与目标变量消元等价校验**：在 `verify_equation_equivalence`（[Line 630](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L630)）中，将两个隐式方程 $F_1(\mathbf{x})=0$ 与 $F_2(\mathbf{x})=0$ 的等价判定修正为检验 `simplify(F1 - F2) == 0 or simplify(F1 + F2) == 0 or simplify(F1 / F2).is_constant()`（或通过 `sympy.groebner` / 消去中间变量后对目标变量的 `solveset` 解集比对），从而真正通过 [Line 711](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L711) 的移项/缩放变形测试并支持多方程组消元等价。
* **Secondary Liabilities（次生工程代价）**:
  `verification_artifact_json` Schema 需增加 `allowed_symbols` 字段，且 `verify_equation_equivalence` 需多执行一次比值常量性或 Gröbner 基规约判定（耗时增加 `< 50ms`）。
