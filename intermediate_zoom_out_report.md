# Stage 8: Intermediate Zoom Out Review Report (`intermediate_zoom_out_report.md`)

* **Target File**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L1-L742) (Lines 1..742)
* **Document Standard**: [zoom-out-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/zoom-out-standard.md)
* **Context Reviewed**: [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md), [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md), [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md), [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md), [concepts_brief.md](file:///Users/wanghaichen/0_work/刷题harness/concepts_brief.md)

## Reviewer Status

| Reviewer | Role Title | Status |
| :--- | :--- | :--- |
| `egm-zoom-out` | Stage 8 Intermediate Zoom Out Reviewer | `completed` |

---

### IZO-1: 双轨 Elo 职责边界混同（当场预支 20% 实战分 + 覆盖分按日回吐）引发多列状态膨胀，且与测试套件的“净增量严格守恒”契约直接矛盾

* **Severity**: **Blocker**
* **Affected Scope**:
  * `Technical Plan -> 2.4 question_escrow_pool & elo_mutation_ledger`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L311-L349)）
  * `Technical Plan -> 3.5 模块 5：分模块双轨制动态 Elo 与单题幂等托管池`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L445-L464)）
  * `Non-Functional Requirements -> Rollout, Idempotency & Rollback Safety`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L540)）
  * `Detailed Implementation Plan -> 3. config.py, 11. elo_engine.py & 15. test_difficulty_and_elo.py`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L586), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L655-L668), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L700)）
* **Simplicity Rationale**:
  1. **跨节数学契约自相矛盾（FeatureRange vs. DetailedRange）**：第 700 行单元测试明确要求“同一道错题依次走完‘草稿保底 $\to$ 脚手架通关 $\to$ 拆拐杖变式 $\to$ 3～7 天盲测销账’全流程后，累计实战分净增量与直接独立做对该题严格守恒”。但在第 450、455、459 行及第 586 行（`config.py`）中，“直接独立做对”采用 $K_{\text{init}} = 24.0$，而错题通关目标采用 $K_{\text{combat}} = 28.0$，且发放草稿保底分 $\Delta R_{\text{floor}}$（$K_{\text{floor}} = 16.0$）后实时修改了模块 $R_{\text{combat}}$，导致后续在 `freeze_decrutch_escrow`（第 664 行）中按变化后的 $R_{\text{combat}}$ 计算 $\Delta R_{\text{target}}$ 时，无论如何都不可能与初次直接做对该题的净增量相等。
  2. **破坏双轨正交边界与补丁式表结构膨胀（Abstraction Boundary Violation & YAGNI Bloat）**：文档第 15 行将双轨定义为“覆盖分（借助脚手架上限）”与“实战分（盲测兑现水位）”。然而第 459 行与第 664 行却规定在升入 `🟡PENDING_RETEST`（尚未盲测）时“当场先兑现 20% 实战分”，第 463 行与第 666 行又规定逾期衰减时“同步回吐未兑现覆盖分”。这不仅造成“20% 预支 + 100% `escrow_combat_delta` 解冻 = 120% 超发”及法则 2 未同步上推覆盖分导致 $R_{\text{combat}} > R_{\text{cov}}$ 倒挂（见 `critic_report.md` DC-1），而且如果按 DC-1 / IC-2 建议往 `question_escrow_pool` 拆出 4～5 个预支/回吐追踪列（`floor_combat_applied`, `decrutch_combat_prepaid`, `escrow_combat_remaining`, `escrow_coverage_applied`）并引入回吐重算状态机，将使单题托管账本急剧膨胀。
  3. **伪事件溯源与死分支触发器（Dead Code）**：第 332、338 行定义了 `'WATERMARK_CLAMPED'` 与 `'COMPACTION_REPLAY_ADJUST'`，但第 661–666 行 `elo_engine.py` 的四个写入函数从未发出这两个触发器；且并查集 Compaction 已严禁跨 `L1` 合并（第 429 行），绝不会影响按 `L1` 独立核算的 Elo，第 97 行与第 476 行声称回滚并查集需重放事件流属无效耦合。
* **极简收敛方案**:
  * **严守双轨正交边界（零预支、零覆盖分倒扣）**：
    1. 草稿保底阶段（法则 2，第 455 行）发放 $\Delta R_{\text{floor}}$ 后，强制执行 $R_{\text{cov}} \leftarrow \max(R_{\text{cov}}, R_{\text{combat}})$ 维护双轨不变量；
    2. 当场变式通过升入 `🟡PENDING_RETEST` 时，仅更新覆盖分 $\Delta R_{\text{cov}}$，并将待发实战分差额 $\text{escrow\_combat\_delta} = \max(0, \Delta R_{\text{target}} - \text{floor\_elo\_applied})$ **100% 冻结在托管池（砍掉“当场预支 20% 实战分”与“覆盖分按日回吐”）**；
    3. 盲测通过升入 `🟢MASTERED` 时解冻 `escrow_combat_delta * decay_factor` 合入实战分；盲测失败退回 `🔴ACTIVE` 时仅将 `escrow_combat_delta` 置零（重训再次升入 `🟡PENDING_RETEST` 时因差额基于历史已发保底分计算，且覆盖分仅在首次升黄时记账，天然杜绝无限刷分，无需拆分 5 个字段）。
  * **对齐守恒参数**：统一令 $K_{\text{init}} = K_{\text{combat}} = 24.0$，并以入库刻的初始实战分 $R_{\text{combat}}^{(0)}$ 锚定整题目标总增量 $\Delta R_{\text{target}}$，使第 450、459、586 行与第 700 行测试断言严格自洽。
  * **清理死枚举**：从 `elo_mutation_ledger`（第 330–339 行）删除 `'WATERMARK_CLAMPED'` 与 `'COMPACTION_REPLAY_ADJUST'`，并移除第 97、476 行关于并查集回滚重放 Elo 的冗余描述。

---

### IZO-2: 四维难度校准公式、分档映射阈值表与子步单调性裁剪在正文、Schema 与实现计划间存在契约断层

* **Severity**: **Blocker**
* **Affected Scope**:
  * `CUJ 1`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L59-L60)）
  * `Technical Plan -> 2.1 questions & question_steps`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L168-L173), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L200-L204)）
  * `Technical Plan -> 3.1 模块 1 & 3.5 法则 2`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L362-L366), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L453-L454)）
  * `Detailed Implementation Plan -> 6. difficulty_rubric.py & 15. test_difficulty_and_elo.py`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L608-L610), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L698-L699)）
* **Simplicity Rationale**:
  1. **`s_sub` 单调性裁剪击穿 SQLite `CHECK` 约束**：第 453 行与第 610 行规定 $S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, S_{\text{orig}} - 1)$。当原题 $S_{\text{orig}} = 1$（代表 1～2 个逻辑步）且用户在第 2 步卡壳时，$S_{\text{sub}}$ 被裁剪为 `0`，直接触发第 201 行 `CHECK (s_sub BETWEEN 1 AND 3)` 数据库异常；同时第 200 行 `c_sub` 允许 `0`，与第 453 行的下界 `1` 不一致。
  2. **重校准公式缺少配套分档阈值表（Missing Interface Specification）**：第 365 行与第 609 行将基础公式重校准为 `750 + 85*(2^c - 1) + 105*s + 115*t + 65*l`（当 $C \in [1,4], S,T,L \in [1,3]$ 时，实际题级值域为 `[1120, 2800]`，子步裁剪后保底值域下探至 `800`）。由于该公式比 [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md#L192) 原公式整体下移了 170～420 分，而第 366 行与第 609 行仅声明了两条熔断规则（$C \le 2$ 封顶 `L4-A`、$C = 4$ 保底 `L5-1`），未给出 `L1_基础直达` 至 `L5-3_殿堂极限` 完整的数值分档阶梯，导致 `calculate_base_elo`（第 609 行）无法确定性实现。
  3. **CUJ 算例算术错误**：第 60 行将 $(C_{\text{sub}}=2, S_{\text{sub}}=1, T_{\text{sub}}=1, L_{\text{sub}}=1)$ 代入第 365 行公式的结果写为 `1345`，而实际计算值为 $750 + 85 \times 3 + 105 + 115 + 65 = 1290$。
* **极简收敛方案**:
  * 将第 200 行 `c_sub` 约束改为 `CHECK (c_sub BETWEEN 1 AND 4)`，并将第 453 行与第 610 行的 $S_{\text{sub}}$ 裁剪统一加上下界保护：$S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, \max(1, S_{\text{orig}} - 1))$（由外层 $D_{\text{sub}} \le \max(800, D_{\text{orig}} - 150)$ 保证保底分严格低于原题分）。
  * 在第 366 行与第 609 行内联补齐适配新公式的确定性分档判定逻辑，并将第 60 行算例修正为 `1290`。

---

### IZO-3: 本地题库“四通道连续阈值检索”在 Schema 与入库流中无数据来源，若照搬实现将引入 $O(N)$ 向量比对过度设计

* **Severity**: **Major**
* **Affected Scope**:
  * `Technical Plan -> 2.1 questions & 2.2 question_relations`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L158-L228)）
  * `Technical Plan -> 3.2 模块 2 & 3.3 模块 3`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L387), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L420)）
  * `Detailed Implementation Plan -> 9. scaffold_engine.py & 12. blind_drill_scheduler.py`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L641), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L675)）
* **Simplicity Rationale**:
  * 第 387 行与第 641 行要求 `prewarm_session_questions`“先在本地 `questions` 表中按四通道阈值检索是否存在匹配的 `SCAFFOLD_PRE` 与 `ISOMORPHIC` 题目”，第 420 行与第 675 行又要求盲测通关后按 `SCAFFOLD_PRE`（$I \ge 0.85$）触发前置子题连带销账。然而 `questions` 表并未存储四通道特征向量，且模块 1（`vlm_diagnosis.py`，第 614–621 行）在新题入库时根本不会对既有 `USER_CANONICAL` 题目做两两四通道打分写入 `question_relations`。
  * **防止过度设计（YAGNI & Reuse Existing）**：若为了修补该断层而在每次入库时调用 Embedding 或 LLM 对全库题目做 $O(N)$ 四通道相似度计算，将严重违背单机冷启动极简原则与 Token 预算（第 537 行）。系统其实已经构建了经 `CanonicalNodeResolver` 归一化的 `question_steps.knowledge_node_id`、六大易错陷阱枚举与 `base_elo` 索引。
* **极简收敛方案**:
  * 在 `questions` 表（第 167 行）增加轻量列 `trap_tags_json TEXT NOT NULL DEFAULT '[]'`（由首轮 VLM 诊断一并落库）；
  * 将第 387、420、641、675 行的本地题库检索与连带销账直接定义为基于现有 SQLite 结构化索引的确定性查询（并优先读取 `question_relations` 已存边）：
    * `SCAFFOLD_PRE`（前置微母题/连带销账）：命中卡点步骤经 `CanonicalNodeResolver` 解析后的同一个主 `knowledge_node_id`，且目标子题总步数 $\le 2$、难度满足 $D_{\text{parent}} - D_{\text{sub}} \ge 200$（此时非对称步骤包含度天然满足 $I(A \subset B) = 1.0 \ge 0.85$）；
    * `ISOMORPHIC`（同构变式）：共享相同的活跃主 `knowledge_node_id` 集合且 $|D_A - D_B| \le 150$；
    * `SHARED_TRAP`（同坑变式）：命中相同的 `trap_taxonomy_tag`。

---

### IZO-4: 异步预热触发边界与粗心分支（`TRAP_OR_CALC_ERROR`）断路，且读接口 `GET /api/v1/drills/daily` 存在非幂等写库副作用

* **Severity**: **Major**
* **Affected Scope**:
  * `Technical Plan -> 2.1 questions`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L178-L180)）
  * `Technical Plan -> 3.2 模块 2, 3.3 模块 3 & 4. 核心内部 API 契约`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L381-L386), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L400-L401), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L418-L419), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L471-L474)）
  * `Detailed Implementation Plan -> 9. scaffold_engine.py & 12. blind_drill_scheduler.py`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L637-L642), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L666), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L674)）
* **Simplicity Rationale**:
  1. **`TRAP_OR_CALC_ERROR` 分支在 `scaffold_engine.py` 中遗漏预热触发**：第 639–640 行规定 `confirm_diagnostic_gate` 仅在 `CONCEPT_BOTTLENECK` 分支下调用 `asyncio.create_task(prewarm_session_questions)`，而在 `TRAP_OR_CALC_ERROR` 分支下直接跳入 `STAGE_4_DECRUTCH_VARIANT` 却未说明如何获取或异步等待 `SHARED_TRAP` 变式题。
  2. **`GET /api/v1/drills/daily` 无幂等锚点原地修改顺延天数**：第 474 行与第 674 行让 `get_daily_blind_drill_queue` 在每次查询时对超出 3 道的题目原地递增 `system_deferred_days` 并顺延 `blind_test_due_at`，但 `questions` 表没有记录“某题被选入当日 3 题题单的日期（`slated_drill_date`）”，刷新页面即导致无限顺延。
* **极简收敛方案**:
  * **保持门禁后再触发预热，补齐 In-Flight 等待状态**：在 `confirm_diagnostic_gate`（第 637–640 行）中，无论确认类别为 `CONCEPT_BOTTLENECK` 还是 `TRAP_OR_CALC_ERROR` 均先查本地库，未命中则启动 `prewarm_session_questions` 后台任务；当界面切入 `STAGE_2` 或直跳 `STAGE_4` 时，若 `prewarm_status == 'RUNNING'`，API 返回 `prewarm_status: 'RUNNING'` 由前端展示非阻塞加载态（或 `await` 该单飞任务），并将第 36、536 行的 `< 2.0s` 指标明确界定为“经过 `STAGE_1` 思考窗口预热完成后的切换延迟”。
  * **每日盲测题单幂等锁**：在 `questions` 表（第 180 行）增加 `slated_drill_date DATE` 列。当日首次调用 `get_daily_blind_drill_queue` 时将入选的 `<= 3` 道题标记 `slated_drill_date = CURRENT_DATE`（仅对当日尚未顺延过的溢出题执行一次顺延），使同日多次 `GET` 完全幂等，同时为 `apply_overdue_escrow_decay` 提供精确的“已入选题单但逾期未测”过滤锚点。

---

### IZO-5: 三层知识网的节点状态聚合、复用计数（$k_{\text{reuse}}$）及并查集回滚表（`compaction_audit_ledger`）存在 Schema 缺失与外部依赖冗余

* **Severity**: **Major**
* **Affected Scope**:
  * `Technical Plan -> 2.1 question_steps & 2.3 knowledge_nodes / concept_co_errors / compaction_audit_ledger`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L190-L206), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L234-L271)）
  * `Technical Plan -> 3.4 模块 4`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L427-L442)）
  * `Detailed Implementation Plan -> 1. pyproject.toml, 10. knowledge_graph.py & 17. test_knowledge_graph_dsu.py`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L566), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L649-L654), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L718-L721)）
* **Simplicity Rationale**:
  * 在 `question_steps`（第 197 行）增加 `reused_existing_node INTEGER NOT NULL DEFAULT 0`；明确规定仅受阻及后续未达步骤（`step_index >= stalled_step_index`）参与节点 `ACTIVE / PENDING_RETEST` 悲观状态聚合与 `concept_co_errors` 标红边生成（题级销账为 `MASTERED` 时递减对应标红边的 `active_error_count`），而草稿已独立完成的前缀步骤（`step_index < stalled_step_index`）仅提供 `MASTERED` 正向证据。
  * 在 `compaction_audit_ledger`（第 260 行）中改用 `merge_id INTEGER PRIMARY KEY AUTOINCREMENT`（保留 `batch_id TEXT NOT NULL` 索引），并增加 `affected_child_node_ids_json TEXT NOT NULL DEFAULT '[]'` 以支持路径压缩的无损逆操作。
  * 从 `pyproject.toml`（第 566 行）与 `knowledge_graph.py`（第 650 行）移除 `rank-bm25`，统一使用标准库字符 2-gram 集合重合度完成 `Top-15` 预筛。

---

### IZO-6: SymPy 沙盒隔离机制（`setrlimit` 禁网误用与 macOS `RLIMIT_AS` 崩溃）及半程立式方程等价判据跨节不一致

* **Severity**: **Major**
* **Affected Scope**:
  * `Technical Plan -> 3.2 模块 2 & 3.3 模块 3`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L388-L391), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L415-L416)）
  * `Non-Functional Requirements -> Security & Sandbox Isolation`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L531-L533)）
  * `Detailed Implementation Plan -> 8. sympy_verifier.py & 16. test_sympy_sandbox.py`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L628-L630), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L708-L711)）
* **Simplicity Rationale**:
  * 在第 390、533、629 行中，明确网络与 I/O 隔离由 AST **模块导入白名单**（仅允许 `sympy`, `math`, `fractions`）保障；子进程资源限制在 Linux 下启用 `RLIMIT_AS`，在 macOS (`sys.platform == 'darwin'`) 下捕获 `(ValueError, OSError)` 并依靠 `RLIMIT_CPU` 与 `subprocess.run(timeout=5.0)` 进程组强杀保障安全。
  * 在第 416 行与第 630 行将 `verify_equation_equivalence` 修正为：将含 `=` 的等式移项归一化为隐式表达式 $F=0$，并检验 `simplify(F1 - F2) == 0 or simplify(F1 + F2) == 0 or simplify(F1 / F2).is_constant()`（多方程组则对目标未知量消元比对解集），确保与第 711 行测试完全一致。

---

### IZO-7: 正文与实现计划之间的枚举命名、盲测窗口参数及盲测变式来源细节不一致

* **Severity**: **Nit**
* **Affected Scope**:
  * `CUJ 2 & CUJ 3`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L75), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L84)）
  * `Technical Plan -> 2.1 questions & 3.3 模块 3`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L160-L162), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L178-L179), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L409-L421)）
  * `Detailed Implementation Plan -> 3. config.py, 10. knowledge_graph.py & 12. blind_drill_scheduler.py`（[adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L586), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L653), [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L671-L675)）
