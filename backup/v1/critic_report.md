# Stage 7: Critic Review Report (`critic_report.md`)

* **Target File**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L1-L555) (`FeatureRange`: Lines 1..555)
* **Document Standard**: [feature-spec-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/feature-spec-standard.md)

## Reviewer Status

| Reviewer | Role Title | Status |
| :--- | :--- | :--- |
| `egm-design` | Stage 7 Design Critic Reviewer | `completed` |
| `egm-structural` | Stage 7 Structural Reviewer | `completed` |
| `egm-style` | Stage 7 Style Reviewer (FeatureRange) | `completed` |

---

## Part 1: Hostile Design Critic (`egm-design`) Findings

### DC-1: [Blocker] 双轨 Elo 核心不变量破缺、托管池（Escrow）状态重置刷分漏洞与覆盖分回吐字段缺失

* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L311-L349), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L409-L412), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L445-L464)
* **Finding**:
  1. **不变量 $\text{Combat\_Elo}_m \le \text{Coverage\_Elo}_m$ 在法则 2 下直接破缺**：第 445 行声明严格维持全局不变量 $\text{Combat\_Elo}_m \le \text{Coverage\_Elo}_m$（初始均为 `1200`）。然而在第 63 行与第 455 行（法则 2）中，当用户在初始模块（`1200/1200`）录入错题并通过 1 秒步骤确认门禁（`STAGE_0_CONFIRM_GATE`）时，系统立即向 $R_{\text{combat}}$ 发放保底实战分增量 $\Delta R_{\text{floor}} > 0$，却未像法则 1（第 450 行）那样同步执行 $R_{\text{cov}} = \max(R_{\text{cov}}, R_{\text{combat}})$。这导致用户在完成模块 3 拆拐杖前（或中途挂起会话时）必然出现 $R_{\text{combat}} > R_{\text{cov}}$ 的倒挂。
  2. **法则 3 的 20% 当场兑现与全额解冻导致 120% 实战分超发**：第 459 行定义 `escrow_combat_delta = max(0, Delta_R_target - floor_elo_applied)` 并在括号内规定“当场先兑现其中 20%，剩余 80% 冻结在托管池”，但第 461 行在盲测通过时又规定“将 `question_escrow_pool` 中剩余的 `escrow_combat_delta * decay_factor` 全额解冻合入 $R_{\text{combat}}$”。若 `escrow_combat_delta` 存储的是 100% 差额而非扣除 20% 后的 80% 余额，盲测通过时将累计发放 $20\% + 100\% = 120\%$ 的差额；且第 330-339 行 `elo_mutation_ledger.mutation_trigger` 枚举中缺少对应这 20% 当场实战分兑现的触发器（或未界定 `SCAFFOLD_COVER_GAIN` 是否同时修改实战分）。
  3. **盲测失败重置托管池（`FORFEITED_RESET`）引发无限刷分漏洞与回吐缺失**：第 87 行与第 411 行规定盲测做错或衰减归零时“退回 `🔴ACTIVE`，清空托管池”。但在首次升入 `🟡PENDING_RETEST` 时，系统已全额发放了覆盖分增量 $\Delta R_{\text{cov}}$（第 458 行）及 20% 首付实战分。若仅清空托管池而不在 `question_escrow_pool` 中持久化单题历史已发放的覆盖分与实战分累计上限，用户故意在盲测答错退回 `🔴ACTIVE` 并重做变式，即可无限刷取 $\Delta R_{\text{cov}}$ 与 20% 实战分。同时，第 311-321 行 `question_escrow_pool` 表中根本没有记录该题未兑现的覆盖分增量字段（如 `escrow_coverage_delta`），导致第 463 行所称“同步回吐对应的未兑现覆盖分虚高部分”在数据层无法确定性计算。
* **Recommendation**:
  1. 在第 455 行法则 2 结算保底实战分后，强制追加不变量维护语句 $R_{\text{cov}} \leftarrow \max(R_{\text{cov}}, R_{\text{combat}})$。
  2. 在 `question_escrow_pool` 表（第 311-321 行）中显式拆分字段：`floor_combat_applied`（已发草稿保底分）、`decrutch_combat_prepaid`（当场变式已兑现的 20% 实战分）、`escrow_combat_remaining`（冻结待盲测兑现的 80% 实战分余额）与 `escrow_coverage_applied`（进入待复测时发放的覆盖分增量）。
  3. 明确单题幂等上限规则：当盲测失败或衰减归零触发 `PENDING_RETEST -> ACTIVE` 时，按 `escrow_coverage_applied` 与 `decrutch_combat_prepaid` 回吐未兑现水分（仅保留 `floor_combat_applied` 草稿保底分），并在 `elo_mutation_ledger.mutation_trigger`（第 330-339 行）中补充 `'BLIND_TEST_FAIL_REVERT'` 枚举。

---

### DC-2: [Blocker] 子步骤难度单调性裁剪公式与 `question_steps` SQL CHECK 约束冲突崩溃，且校准 Elo 公式未覆盖声明的 `[800, 1119]` 区间

* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L59-L60), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L168-L173), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L200-L204), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L362-L366), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L453-L454)
* **Finding**:
  1. **`s_sub` 下界越界触发 SQLite CHECK 崩溃**：第 453 行单调性硬裁剪公式规定 $S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, S_{\text{orig}} - 1)$（未像 $C_{\text{sub}}$ 那样包裹 $\max(1, \dots)$）。根据 [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md#L160) 锚点定义，$S_{\text{orig}} = 1$ 代表“1～2 个逻辑步”；当用户在包含 2 个逻辑步的 $S_{\text{orig}} = 1$ 题目中完成第 1 步并在第 2 步卡壳时，$S_{\text{sub}}$ 会被裁剪为 $0$，写入第 201 行 `s_sub INTEGER NOT NULL CHECK (s_sub BETWEEN 1 AND 3)` 时将直接触发 SQLite 约束异常崩溃（反之第 200 行 `c_sub` 却声明为 `BETWEEN 0 AND 4`）。
  2. **校准公式值域断层、算例算错与分档阈值缺失**：第 35、172、362 行声称新公式覆盖 `800～2800` 分（旨在修复《理论基础.md》旧公式最低分 `1290` 的问题）。然而受第 168-171 行 `CHECK` 约束限制，最小输入 $(C=1, S=1, T=1, L=1)$ 代入第 365 行公式得 $\text{base\_elo}(1,1,1,1) = 750 + 85(2^1 - 1) + 105(1) + 115(1) + 65(1) = 1120$，`[800, 1119]` 区间依然是不可达死区（`max(800, ...)` 为死代码）。此外，第 60 行 CUJ 1 中 $(C_{\text{sub}}=2, S_{\text{sub}}=1, T_{\text{sub}}=1, L_{\text{sub}}=1)$ 代入第 365 行公式实际得 $750 + 255 + 105 + 115 + 65 = 1290$，正文误算为 `1345`；且由于新公式相比《理论基础.md》第 192 行公式整体下移了 170～420 分，第 366 行未给出重校准后的 `L1_基础直达` 至 `L5-3_殿堂极限` 数值分档阈值。
* **Recommendation**:
  1. 将第 200 行 `c_sub` 的 CHECK 约束统一为 `BETWEEN 1 AND 4`，并将第 453 行 $S_{\text{sub}}$ 裁剪公式修正为 $S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, \max(1, S_{\text{orig}} - 1))$（若题目总步数 `step_count >= 2` 但 $S_{\text{orig}}=1$，依赖第 454 行 $D_{\text{sub}} \le D_{\text{orig}} - 150$ 硬锁保证分数严格递减）。
  2. 修正第 365 行公式常数项或明确其值域 `[1120, 2800]`（含子步保底裁剪下界 `800`），同步修正第 59-60 行 CUJ 1 算例数值（`1290`），并在第 366 行显式列出 `L1`～`L5-3` 的 Elo 映射阈值表。

---

### DC-3: [Blocker] 题目四通道指纹未落库且入库未构建题目间关联边，导致本地题库四通道检索与“上位母题连带销账”成为死逻辑

* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L158-L187), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L212-L228), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L356-L393), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L420)
* **Finding**:
  1. 第 387 行规定二阶微母题与变式题优先执行“本地题库检索四通道阈值（如 `SCAFFOLD_PRE` 要求 $I(A \subset B) \ge 0.85$ 且 $D_B - D_A \ge 200$）”，第 420 行又规定盲测做对高阶母题 $B$ 时自动连带销账满足 `SCAFFOLD_PRE` 的待复测子题 $A$。
  2. 然而检视 `questions` 表 DDL（第 158-187 行）与模块 1 入库流水线（第 356-378 行）：`questions` 表既未存储 [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md#L399-L405) 要求的单题“剥皮抽骨”四通道指纹（缺 `abstract_skeleton_md` 抽象骨架文本与题级 `trap_tags_json`，目前仅在 `diagnostic_sessions` 存了单次会话的 `trap_taxonomy_tag`），模块 1 与模块 4 在新题入库时也**没有任何步骤去计算新题与同模块/同父 L2 下既有 `USER_CANONICAL` 题目的四通道关系并写入 `question_relations` 表**。这导致 `question_relations` 表中永远只有某道母题与其专属 AI 衍生题之间的边，任意两道用户录入题之间关系恒为空，本地题库跨题检索与第 420 行“上位母题连带销账”在运行时 100% 无法命中。
* **Recommendation**:
  1. 在 `questions` 表（第 158-187 行）中补充 `trap_tags_json TEXT NOT NULL DEFAULT '[]'`（或直接基于结构化索引检索），由模块 1 首轮 VLM 诊断一并提取。
  2. 明确本地检索通过 `CanonicalNodeResolver` 解析后的 `knowledge_node_id`、`trap_tags` 与 `base_elo` 差值执行确定性关联匹配并写入 `question_relations` 表。

---

### DC-4: [Major] 思考窗口投机预热（Speculative Pre-warming）在“审题/计算粗心”分支存在 0 秒窗口击穿，且缺少预热未就绪时的等待契约

* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L36), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L71), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L381-L391), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L400-L401), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L472), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L536)
* **Finding**:
  1. **粗心分支 0 秒思考窗口直接击穿 `< 2s` 延迟承诺**：第 381-385 行与第 536 行依赖用户在 `STAGE_1_BLIND_PROBE` 的审题思考时间来掩盖后台 LLM 生成 + SymPy 沙盒验算耗时（单轮生成+验算约 `8～20s`，含重试可达 `30s+`）。但根据第 401 行状态机与第 472 行 API 契约，当 `error_category == 'TRAP_OR_CALC_ERROR'`（或用户在门禁处一键切换为“审题/计算粗心”）时，状态机**跳过一阶盲探直接进入 `STAGE_4_DECRUTCH_VARIANT`**，此时思考窗口为 **0 秒**，前端将被迫同步死等变式题生成。
  2. **一阶秒点跳过时缺乏流式/轮询等待协议**：在第 71 行（CUJ 2）中，若用户进入 `STAGE_1_BLIND_PROBE` 后在 2～3 秒内直接点击“仍无思路，请求二阶微母题铺垫”，后台 `prewarm_status` 极大概率仍处于 `'RUNNING'`，但第 473 行 API 未定义此时的非阻塞响应协议。
* **Recommendation**:
  1. 将 `Prewarm_Worker` 的触发时机从第 472 行 `/confirm-gate` **前移至第 471 行 `/questions/ingest` VLM 诊断完成瞬间**：利用用户审视“步骤对齐确认卡”的窗口期提前启动后台预热；若用户在 `/confirm-gate` 覆写了诊断结论，则取消在途任务并重派。
  2. 在第 472-473 行 API 契约中明确：维护会话级 `in_flight_prewarm_tasks` 单飞注册表，当 `prewarm_status == 'RUNNING'` 时支持 `await` 在途任务或返回 `prewarm_status: 'RUNNING'` 进度态，并将第 36 行与第 536 行的 `< 2.0s` 延迟指标限定为“一阶思考时长 $\ge 15\text{s}$ 正常流下的 P95 切换延迟”。

---

### DC-5: [Major] 半程立式验证（`simplify(lhs - rhs) == 0`）无法判定多方程联立消元等价，且沙盒 `setrlimit` 内存/网络隔离描述存在系统工程错误

* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L388-L391), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L415-L416), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L530-L534)
* **Finding**:
  1. **多方程消元代数等价无法用 `simplify(lhs - rhs) == 0` 实现**：第 416 行声称用户输入“核心物理定律方程组或关键几何转化等式”后，后端调用 SymPy 通过 `simplify(lhs - rhs) == 0` 验证其在**消元意义下代数等价**。实际上，`simplify(lhs - rhs) == 0` 只能判断变量名完全相同的单个标量表达式恒等；面对方程移项反号（如 `E1 = -E2`）或多步方程组时直接相减化简必然失败。
  2. **AST 防伪探针仍可被死代码绕过**：第 388-389 行仅检查 AST 中包含 `solve`/`simplify` 调用，但未定义 `solve_kernel()` 的结构化返回值契约。
  3. **`resource.setrlimit` 无法禁用网络且 `256MB` 虚拟内存上限在 macOS 下会导致 Python/SymPy 启动崩溃**：第 390 行与第 533 行声称“通过 `resource.setrlimit` 限制内存 `256MB`、禁用网络”。在 POSIX（macOS/Linux）系统中，`setrlimit` 根本不支持禁用网络；且在 macOS 下将虚拟地址空间 `RLIMIT_AS` 设为 `256MB`，会导致 Python 子进程直接因 `ValueError` / `MemoryError` 崩溃。
* **Recommendation**:
  1. **修正半程立式验证算法与输入契约**：在 `STAGE_4_DECRUTCH_VARIANT` 半程立式模式下，界面显式展示受控符号表芯片；后端支持含 `=` 方程移项为 `lhs - rhs`，并检验 `simplify(e1 - e2) == 0 or simplify(e1 + e2) == 0 or simplify(e1 / e2).is_constant()`（以及多方程消去中间量后对目标未知量的解等价）。
  2. **强化内核先行防伪契约**：要求 `solve_kernel()` 返回结构化字典 `{"params": {...}, "answer_latex": "..."}` 并回填至 `parametric_stem_tpl`。
  3. **修正沙盒隔离机制**：通过 AST **白名单**（仅允许导入 `sympy`, `math`, `fractions` 模块，拦截一切其他导入及双下划线属性访问）在语言层阻断网络与文件 I/O；内存限制在 Linux 下应用 `RLIMIT_AS`，在 macOS (`sys.platform == 'darwin'`) 下捕获 `(ValueError, OSError)` 降级为 CPU 时间限制 `RLIMIT_CPU` 与超时强杀。

---

### DC-6: [Major] 可逆并查集 Compaction 一键回滚（Un-merge）存在二级子节点丢失与跨时间线快照覆写损坏，且节点状态按整题聚合会误标红已掌握步骤

* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L190-L206), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L244-L271), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L430-L442)
* **Finding**:
  1. **路径压缩导致 Un-merge 丢失原二级子节点归属**：第 431 行规定合并 `from_node_id -> to_node_id` 时执行一次性路径压缩：“更新 `from_node_id` 及所有原指向 `from_node_id` 的子节点，使其 `redirect_to_id = to_node_id`”。但第 259-271 行 `compaction_audit_ledger` 仅记录了 `from_node_id` 与 `to_node_id`，未记录合并前原本指向 `from_node_id` 的二级子节点列表（`affected_child_node_ids_json`）。执行 `Un-merge` 时无法还原这些二级子节点的 `redirect_to_id`。
  2. **节点掌握状态按整题聚合连坐污染已掌握的前置步骤**：在当前 Schema 中，知识点挂载在 `question_steps.knowledge_node_id`（第 197 行）。当用户在压轴题中已独立完成 `Step 1`、仅在 `Step 2` 卡壳时（如 CUJ 1），按第 432 行“节点展示状态由其下挂载的所有 `USER_CANONICAL` 题目动态聚合（`🔴 > 🟡 > 🟢`）”，由于整题状态为 `🔴ACTIVE`，用户已独立证通的 `Step 1` 对应知识点也会被错误标红为 `🔴ACTIVE`。此外，第 439 行计算模块收敛度中的“直接复用老节点次数 $k_{\text{reuse}}$”在表结构中缺少入库时的持久化标记字段。
* **Recommendation**:
  1. 在 `compaction_audit_ledger`（第 259-271 行）中将主键改为 `merge_id INTEGER PRIMARY KEY AUTOINCREMENT`（保留 `batch_id` 索引），并增加 `affected_child_node_ids_json TEXT NOT NULL DEFAULT '[]'`。
  2. 在 `question_steps` 粒度区分步骤受阻状态与复用快照：增加 `is_blocked_step INTEGER NOT NULL DEFAULT 0` 与 `reused_existing_node INTEGER NOT NULL DEFAULT 0`；聚合 `knowledge_nodes` 的悲观半格状态时，前缀已独立完成的步骤（`is_blocked_step = 0`）仅提供 `🟢MASTERED` 正向证据，不传播整题的 `🔴ACTIVE` 阻塞态；`concept_co_errors` 仅在受阻步骤与关联步骤间建立，并在题目销账为 `🟢MASTERED` 时递减 `active_error_count`。

---

### DC-7: [Nit] 盲测失败退回 `🔴ACTIVE` 时的微观会话恢复起点未定义，且 `COMPACTION_REPLAY_ADJUST` 触发器与同域并查集约束语义冲突

* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L338), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L397-L412)
* **Finding**:
  1. 第 411 行状态图定义了宏观状态转换 `PENDING_RETEST --> ACTIVE: 盲测做错`，但未明确盲测失败退回 `ACTIVE` 时新会话应初始化在哪个 `micro_stage`。
  2. 第 338 行在 `elo_mutation_ledger.mutation_trigger` 中定义了 `'COMPACTION_REPLAY_ADJUST'`，但第 429 行已严格规定 Compaction 禁止跨 `L1` 或跨 `L2` 合并，而双轨 Elo 仅按 `l1_module` 独立核算，因此同域 `L3` 并查集合并与回滚永远不会改变任何 `L1` 模块的 Elo 分值。

---

## Part 2: Structural Validator (`egm-structural`) Findings

### ST-1: 缺少独立的 `Risks & Mitigations`（或 `Risks & Limits`）结构化章节
* **Severity**: Major
* **Affected Scope**: `## Non-Functional Requirements` 及全局一级章节结构（Lines 529–555）
* **Discrepancy Description & Rationale**:
  对照 [feature-spec-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/feature-spec-standard.md#L23-L39) 中的 **Risks & Limits** 与 **Requirements (`Risks & Mitigations`)** 要求，规范明确规定设计文档必须显式列出系统自身的约束边界、潜在故障模式（Failure Modes）、安全影响、扩展性上限（Scalability Limits）及其对应的缓解措施。当前文档虽在 `## Non-Functional Requirements`（Lines 529–540）及各模块正文中零散提及了沙盒隔离、延迟和回滚安全，但缺少显式的 `## Risks & Mitigations` 章节或风险矩阵表。

---

### ST-2: 标题层级越界与归属错位（`Additional Resources` 与 `Future Improvements` 误用 H3）
* **Severity**: Major
* **Affected Scope**: `## Non-Functional Requirements` 尾部及后续章节（Lines 529–554，具体为 Lines 544, 549）
* **Discrepancy Description & Rationale**:
  第 529 行为二级标题 `## Non-Functional Requirements`，第 542 行使用顶层分割线分隔后，第 544 行 `### Additional Resources` 与第 549 行 `### Future Improvements` 却使用了三级标题（H3）。它们应升级为与 `## Non-Functional Requirements` 平级的二级标题（`## Additional Resources`、`## Future Improvements`）。

---

### ST-3: 状态机图（Mermaid `stateDiagram-v2`）与 SQLite Schema 状态枚举不完全对称
* **Severity**: Nit
* **Affected Scope**: `#### 2.4 微观训练会话状态机与双轨 Elo 事件溯源账本表`（Lines 291–300）与 `#### 3.3 模块 3：双层正交状态机、负荷分级拆拐杖与防背题延迟盲测`（Lines 396–412）
* **Discrepancy Description & Rationale**:
  第 291–300 行的 `diagnostic_sessions.micro_stage` `CHECK` 约束定义了 7 个微观状态（包含初始门禁 `STAGE_0_CONFIRM_GATE` 与终态 `SESSION_COMPLETED`）。然而在第 396–412 行的 `stateDiagram-v2` 复合状态 `state ACTIVE { ... }` 中，既缺少指向 `STAGE_0_CONFIRM_GATE` 的内部初始入口转换（`[*] --> STAGE_0_CONFIRM_GATE`），也遗漏了 `SESSION_COMPLETED` 终态节点。

---

### ST-4: `Alternatives Considered` 拒绝理由包含未附链接的个人归因前缀（`Rejected by author:`）
* **Severity**: Nit
* **Affected Scope**: `## Alternatives Considered`（Lines 487, 493, 501, 507, 513, 519, 525）
* **Discrepancy Description & Rationale**:
  对照 [feature-spec-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/feature-spec-standard.md#L19-L21) 的 **The Alternatives Rule**，文档在全部 7 个备选方案的 `Reason for Rejection` 开头均带有模板化的 `Rejected by author:` 个人归因前缀且未附讨论链接。建议移除 `Rejected by author:` 前缀，直接陈述客观技术与范围取舍。

---

### ST-5: 分割线语法混用、跨文档链接格式不一致及 KaTeX 公式块内嵌 Emoji
* **Severity**: Nit
* **Affected Scope**: 全局分割线（Lines 7, 149, 352, 465, 527, 542, 554）、跨文档引用（Lines 46, 209, 551）及数学公式块（Line 437）
* **Discrepancy Description & Rationale**:
  1. **分割线语法与对称性不一致**：文档混用了 80 字符长横线 `--------------------------------------------------------------------------------` 与 3 字符短横线 `---`。
  2. **跨文档引用格式不统一**：Line 209 标题使用纯文本书名号而非 Markdown 链接；Lines 46、551 虽链接至 `[理论基础.md](../../理论基础.md)` 并点明具体章节，但未附带对应的章节锚点。
  3. **KaTeX 公式内嵌 Emoji**：Line 437 的独立数学公式块在 LaTeX `\text{...}` 上标中直接写入 Unicode Emoji（`🟢`、`🟡`），建议改用英文文本上标（如 `\text{mastered}`、`\text{pending}`）。

---

## Part 3: Technical Copy Editor (`egm-style`, FeatureRange) Findings

### SY1-1: 未校准的绝对化能力声明与营销修辞（Uncalibrated Claims & Hyperbole）
* **Severity**: Major
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L31) (Lines 31, 97, 391, 493, 536)
* **Style Rationale** (`CALIBRATE`):
  1. **Line 31**：“**消除**剧透与实时命题幻觉……**杜绝**背答案假销账……实现跨阶段**零通胀**的精准能力定级” $\to$ 应校准为“**抑制**实时命题幻觉”、“**通过中间子目标核验拦截**背答案假销账”、“**防止跨阶段重复计分通胀**”。
  2. **Line 97**：“**全程零数据丢失**” $\to$ 应改为“完整恢复合并前的题目挂载关系与标红边状态”。
  3. **Line 391**：“保证主流程**永不死锁**” $\to$ 应改为“避免主流程因沙盒验算失败而同步阻塞中断”。
  4. **Line 493**：“衍生题 **100% 算得通且答案唯一正确**” $\to$ 应校准为“衍生题需具备可验证的数理可解性与答案确定性”。
  5. **Line 536**：“**思考窗口零等待感知**……P95 响应延迟 `< 2.0s`” $\to$ 应将小标题改为“**思考窗口异步预热与低延迟切换**”。

---

### SY1-2: “1 秒步骤对齐”组件命名与实际延迟指标（`< 3 秒`）前后不一致
* **Severity**: Major
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L14) (Lines 14, 31, 35, 54, 109, 289, 356, 374, 525)
* **Style Rationale** (`CALIBRATE`):
  文档在 `G1`（Line 35）与 `Alternative 4`（Line 525）中明确将该步骤确认门禁的耗时指标定义为 `< 3 秒`，但在多处将组件冠名为“**1 秒**步骤对齐微确认条/门禁”。建议统一组件客观名称为“**步骤对齐微确认条（Step-Skeleton Diff Gate）**”，仅在性能/交互目标处声明 `< 3s` 确认耗时。

---

### SY1-3: 滥用“物理 / 物理级”修饰纯软件、UI 与数据库约束（Imprecise Jargon）
* **Severity**: Major
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L255) (Lines 255, 359, 421, 429)
* **Style Rationale**:
  1. **Line 255**：`-- 物理级拦截自环边` $\to$ 改为“**数据库约束级拦截**”。
  2. **Line 359**：`前端录入界面物理隔离为` $\to$ 改为“**前端录入界面划分为独立的**”。
  3. **Line 421**：`盲测界面物理剥离原题来源` $\to$ 改为“**盲测界面隐去（或接口层剥离）**原题来源”。
  4. **Line 429**：`物理禁止跨 L1 或跨 L2 重定向` $\to$ 改为“**强制禁止**跨 `L1` 或跨 `L2` 重定向”。

---

### SY1-4: `CUJ 2` 中的枚举值写法与 Schema DDL 定义不一致
* **Severity**: Major
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L75) (Line 75 vs. Lines 161, 392)
* **Style Rationale** (`CALIBRATE`):
  Line 75 写为 `origin_type = 'AI_SYNTHETIC'`，而 Line 161 的 `questions` 表 `CHECK` 约束明确规定合法枚举值为 `'AI_SYNTHETIC_SCAFFOLD'` 与 `'AI_SYNTHETIC_VARIANT'`。应将 Line 75 校准为 `origin_type = 'AI_SYNTHETIC_SCAFFOLD'`。

---

### SY1-5: 口语化比喻、拟人修辞与情绪化强调词（Colloquialisms & Subjective Intensifiers）
* **Severity**: Nit
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L13) (Lines 13, 21, 25, 83, 366, 463, 485, 486, 535, 539)
* **Style Rationale**:
  将“咬合而成”、“直接喂给 / 直接捅破窗户纸”、“长出”、“大幅提升”、“严重阻塞心流”、“骗过”、“绝不 / 严禁”等口语比喻和主观强调词替换为客观技术表述。
