# Goldfish Annotated Review Report

Status: resolved

**Target:** `docs/designs/adaptive_practice_harness.md` (`1..742`)

## Prioritized Findings & AI Suggestions

### CR-1 [egm-synthesizer / Multi-Persona] - Severity: Blocker
*   **Affected Scope:** [Lines 63, 83-87, 97, 311-349, 445-464, 476, 540, 586, 655-668, 700](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L63) (CUJ 1, DDL 2.4, 3.5, 4, 5.2, Tests)
*   **Discrepancy Description:**
    1. **跨阶段净增量不守恒（导致 L700 单测必败）**：L700 要求“错题走完草稿保底 -> 脚手架 -> 拆拐杖 -> 盲测销账全流程后，累计实战分净增量与直接独立做对该题严格守恒”。但 L450 直接做对使用 $K_{\text{init}} = 24.0$，而 L459 错题目标增量使用 $K_{\text{combat}} = 28.0$；且第一阶段发放 $\Delta R_{\text{floor}}$ 后模块 $R_{\text{combat}}$ 已上涨，若 L459 用上涨后的 $R_{\text{combat}}$ 计算 $E(R_{\text{combat}}, D_{\text{orig}})$，总增量无法守恒。
    2. **20% 预支与全额解冻导致 120% 超发及盲测失败刷分漏洞**：L459 规定 `escrow_combat_delta = max(0, Delta_R_target - floor_elo_applied)` 并注明“当场先兑现 20%，剩余 80% 冻结”，而 L461 又将 `escrow_combat_delta * decay_factor` 全额解冻，语义歧义导致发放 120%；且盲测失败重置托管池时若不记录已发放分值，重做变式会重复刷取 20% 实战分与覆盖分。
    3. **法则 2 未同步上推覆盖分导致 $R_{\text{combat}} > R_{\text{cov}}$ 倒挂**：L455 发放草稿保底实战分 $\Delta R_{\text{floor}}$ 时未执行 $R_{\text{cov}} \leftarrow \max(R_{\text{cov}}, R_{\text{combat}})$，在初始模块（`1200/1200`）下直接违反 L445 的全局双轨不变量。
    4. **死分支触发器枚举**：并查集 Compaction 已严禁跨 `L1` 合并（L429），不可能影响按 `L1` 核算的 Elo，L338 的 `'COMPACTION_REPLAY_ADJUST'` 与 L333 的 `'WATERMARK_CLAMPED'` 在 `elo_engine.py` 四个写入函数中从未使用。
*   **Rationale:** 双轨 Elo 评分系统的数学严密性与守恒性是衡量学生真实水平与防刷分的核心支柱。如果不变量倒挂或产生超发漏洞，评级体系将彻底失真并导致核心单元测试失败。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 采纳 Stage 8 极简收敛方案（IZO-1）：
    1. 统一令 $K_{\text{init}} = K_{\text{combat}} = 24.0$；
    2. 在 `question_escrow_pool` 中记录入库初始实战分快照 `initial_combat_elo_snapshot`，计算总目标增量 $\Delta R_{\text{target}} = K_{\text{combat}} \cdot (1 - E(R_{\text{combat}}^{(0)}, D_{\text{orig}}))$；
    3. 严守双轨正交边界：当场拆拐杖仅结算覆盖分并把实战分差额 100% 冻结入托管池（彻底移除“当场预支 20% 实战分”与“覆盖分按日回吐”）；
    4. 法则 2 保底结算后强制 $R_{\text{cov}} \leftarrow \max(R_{\text{cov}}, R_{\text{combat}})$，杜绝倒挂；
    5. 清理 `'WATERMARK_CLAMPED'` 与 `'COMPACTION_REPLAY_ADJUST'` 无用枚举。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L63, L311-L349, L445-L464, L476, L540, L586, L655-L668, L700
    *   Change:
        - 在 `question_escrow_pool` DDL 中增加 `initial_combat_elo_snapshot REAL NOT NULL` 与 `is_cov_settled INTEGER NOT NULL DEFAULT 0`；
        - 从 `elo_mutation_ledger.mutation_trigger` 中移除 `'WATERMARK_CLAMPED'` 与 `'COMPACTION_REPLAY_ADJUST'`；
        - 在法则 2（L455）追加：$\Delta R_{\text{floor}}$ 结算后同步执行 $R_{\text{cov}} \leftarrow \max(R_{\text{cov}}, R_{\text{combat}})$；
        - 在法则 3（L458-L463）重写为：当场拆拐杖仅在 `is_cov_settled == 0` 时发放 $\Delta R_{\text{cov}}$（$K_{\text{cov}}=32$），基于快照分 $R_{\text{combat}}^{(0)}$ 计算 $\Delta R_{\text{target}}$，将待兑现差额 $\text{escrow\_combat\_delta} = \max(0, \Delta R_{\text{target}} - \text{floor\_elo\_applied})$ 100% 冻结入池（零当场预支）；盲测通过解冻 $\text{escrow\_combat\_delta} \times \text{decay\_factor}$；逾期仅对未兑现部分衰减 `decay_factor`，不扣实战分基线；
        - 移除 L97 与 L476 中“回滚并查集需重算 Elo”的陈述，在 L540 明确 Elo 账本与并查集解耦。

---

### CR-2 [egm-comprehension / Multi-Persona] - Severity: Blocker
*   **Affected Scope:** [Lines 35, 59-60, 63, 168-173, 200-204, 362-366, 453-454, 608-610, 698-699](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L35) (CUJ 1, DDL 2.1, 3.1, 3.5, 5.2, Tests)
*   **Discrepancy Description:**
    1. **子步单调性裁剪在 $S_{\text{orig}} = 1$ 或 $D_{\text{orig}} < 950$ 时触发 SQLite `CHECK` 崩溃**：L453 与 L610 写为 $S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, S_{\text{orig}} - 1)$。当 $S_{\text{orig}} = 1$ 时 $S_{\text{sub}} = 0$，违反 L201 `CHECK (s_sub BETWEEN 1 AND 3)`；当 $D_{\text{orig}} < 950$ 时 $D_{\text{orig}} - 150 < 800$，违反 L204 `CHECK (sub_elo BETWEEN 800 AND 2650)`；且 L200 `c_sub` 允许 `0`，与 `c >= 1` 冲突。
    2. **重校准公式缺少 8 档 `tier` 确定性判定阈值表**：L365 与 L609 将公式重校准为 `750 + 85*(2**c - 1) + 105*s + 115*t + 65*l`（题级值域 `[1120, 2800]`，子步保底值域 `[800, 2650]`）。在该公式下，L698 测试用例 `(2, 3, 1, 3)` 得分为 `1630`；若沿用《理论基础.md》旧阈值（`>= 1750` 才入 `L4-A`），单测 `(2,3,1,3) -> L4-A` 将直接失败。且正文未内联说明 $C, S, T, L$ 四个维度的含义。
    3. **CUJ 1 算例计算错误**：L60 中 $(C_{\text{sub}}=2, S_{\text{sub}}=1, T_{\text{sub}}=1, L_{\text{sub}}=1)$ 代入公式实际为 $750 + 255 + 105 + 115 + 65 = 1290$，而非 `1345`。
*   **Rationale:** 字段约束超界将直接在入库时触发 SQLite `IntegrityError` 致命异常；算例计算错误和评级阈值未决将导致测试断言无法通过。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 
    1. 修正 `question_steps.c_sub` 为 `CHECK (c_sub BETWEEN 1 AND 4)`；
    2. 在裁剪公式中添加严格下界：$S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, \max(1, S_{\text{orig}} - 1))$ 与 $D_{\text{sub}} = \max(800, \min(2650, D_{\text{orig}} - 150, \dots))$；
    3. 内联补齐 CSTL 维度定义与 8 档 `tier` 确定性梯形判定逻辑；
    4. 修正 CUJ 1 中的算例数值为 `1290`。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L60, L63, L200, L362-L366, L453-L454, L608-L610, L698
    *   Change:
        - 修正 L200 为 `c_sub INTEGER NOT NULL CHECK (c_sub BETWEEN 1 AND 4)`;
        - 将 L453-L454 与 L610 的单调性裁剪公式更新为含 $\max(1, S_{\text{orig}}-1)$ 与 $\max(800, \dots)$ 下界保护；
        - 在 L362-L366 与 L609 内联阐述 $C, S, T, L$ 定义及 8 档梯形阈值表（确保 `(2,3,1,3) -> 1630 -> L4-A` 通过）；
        - 将 L60、L63 中的 `1345` 统一修正为 `1290`。

---

### CR-3 [egm-comprehension / Multi-Persona] - Severity: Blocker
*   **Affected Scope:** [Lines 388-390, 416, 531-533, 628-630, 708-711](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L388-L390) (3.2, 3.3, NFR, 5.2, Tests)
*   **Discrepancy Description:**
    1. **macOS 下 `resource.RLIMIT_AS = 256MB` 启动即崩溃 & `setrlimit` 无法禁网**：在 macOS (`darwin`) 上，Python 子进程虚拟地址空间因系统 `dyld` 共享缓存映射已超数十 GB，设置 `RLIMIT_AS = 256MB` 会立即抛出 `ValueError` 或 `MemoryError`；且 POSIX `setrlimit` 不具备禁用网络功能。
    2. **方程移项反号等价判定与 `simplify(lhs - rhs) == 0` 矛盾**：L416 写 `simplify(lhs - rhs) == 0`，但 L711 单测要求验证 `m*v**2/2 - q*U` 与 `q*U - 1/2*m*v**2` 等价（二者互为相反数 $E_1 = -E_2$，`simplify(E1 - E2) != 0`）。
    3. **`solve_kernel()` 返回结构与 AST 白名单未闭合**：L388-L390 未规定 `solve_kernel()` 的返回值结构，且 L628 仅写了黑名单导入而非 L531 要求的导入白名单与双下划线内省属性拦截。
*   **Rationale:** 本地运行环境包含 macOS 与 Linux。硬编码不可用的系统调用会导致本地开发与运行全线崩溃；数学判据漏洞会导致正确的物理/代数方程被判定为错误。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 
    1. 对平台做兼容处理：Linux 下设 `RLIMIT_AS`，macOS 下捕获异常降级忽略，依赖 `subprocess.run(timeout=5.0)` 进程组强杀保障安全；
    2. 语言级安全：在 AST 中采用严格导入白名单（`sympy`, `math`, `fractions`）并拦截 `__` 开头的属性访问；
    3. 明确 `solve_kernel()` 返回结构为 `{"params": dict[str, str | int | float], "answer_latex": str}`；
    4. 扩充 `verify_equation_equivalence`：支持 `simplify(e1 - e2) == 0`、`simplify(e1 + e2) == 0` 及非零常数比值。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L388-L390, L416, L531-L533, L628-L630, L711
    *   Change:
        - 在 L388-L390 与 L628-L629 明确平台差异化子进程资源限制处理与 `solve_kernel` 规范字典返回结构；
        - 在 L416 与 L630 扩充 `verify_equation_equivalence` 代数等价判据（支持相反数与常数倍，消去未知量判等）；
        - 在 L531-L533 与 L628 明确 AST 导入白名单与禁止 `__` 属性访问规则。

---

### CR-4 [egm-comprehension / Multi-Persona] - Severity: Blocker
*   **Affected Scope:** [Lines 75, 160-162, 392, 560-567, 582, 653](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L75) (CUJ 2, DDL 2.1, 3.2, 5.1, 5.2)
*   **Discrepancy Description:**
    1. `questions.origin_type` 在 L161 DDL 中定义为 `'USER_CANONICAL' | 'AI_SYNTHETIC_SCAFFOLD' | 'AI_SYNTHETIC_VARIANT'`，但 L75 与 L653 写为 `'AI_SYNTHETIC'`，按字面入库会触发 SQLite `CHECK` 失败，按字面过滤会漏掉合成题。
    2. L582 使用 `BaseSettings`，但 L566 `pyproject.toml` 未声明 `pydantic-settings`（Pydantic v2 中已拆分）和 `python-multipart`（FastAPI 上传文件必需），且在含 `backend/`、`frontend/`、`tests/`、`docs/` 多顶层目录的仓库下未声明 `[tool.setuptools.packages.find]`，执行 `pip install -e .` 必败；同时未将 `__init__.py` 列入文件清单。
*   **Rationale:** 枚举不一致将导致数据库写入异常，依赖项与构建配置缺失将直接导致项目无法安装和初始化。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 
    1. 将缩写统一修正为符合 DDL 约束的字面枚举值 `'AI_SYNTHETIC_SCAFFOLD'` 与 `'AI_SYNTHETIC_VARIANT'`；
    2. 在 `pyproject.toml` 中补齐 `pydantic-settings>=2.0`、`python-multipart`，配置 `[build-system]`、`[tool.setuptools.packages.find]` 与 `[tool.pytest.ini_options]`，并在文件清单中补齐 `__init__.py`。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L75, L560-L567, L582, L653
    *   Change:
        - 修正 L75 为 `origin_type = 'AI_SYNTHETIC_SCAFFOLD'`；
        - 修正 L653 明确仅统计 `origin_type = 'USER_CANONICAL'`，排除 `'AI_SYNTHETIC_SCAFFOLD'` 与 `'AI_SYNTHETIC_VARIANT'`；
        - 在 L560-L567 完善 `pyproject.toml` 配置与依赖，并向 5.2 节文件清单补齐各模块 `__init__.py`。

---

### CR-5 [egm-comprehension / Multi-Persona] - Severity: Major
*   **Affected Scope:** [Lines 190-206, 244-271, 427-442, 566, 649-654, 718-721](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L190-206) (DDL 2.1, 2.3, 3.4, 5.1, 5.2, Tests)
*   **Discrepancy Description:**
    1. **前缀已掌握步骤被整题状态“连坐标红”**：知识点挂载在 `question_steps.knowledge_node_id`。若将整题 `ACTIVE` 状态无差别传染给所有步骤节点，用户在草稿上已独立证通的前置步骤（`step_index < stalled_step_index`）对应的已掌握节点会被错误打回 `🔴ACTIVE`。
    2. **收敛度 $k_{\text{reuse}}$ 缺乏持久化列且 `p_i` 分母未明确**：表结构中未记录题目入库时是否复用了已有节点，无法在节点合并后还原最近 $W=20$ 题的复用率 $\rho_m = k_{\text{reuse}} / w$。
    3. **标红边 `concept_co_errors` 缺失写入与销账扣减入口**。
    4. **`compaction_audit_ledger` 主键冲突与两跳路径压缩回滚丢失**：以 `batch_id` 为主键导致单批次无法存多对合并，且未保存原指向 `from_node_id` 的二级子节点列表 `affected_child_node_ids_json`。
    5. **`rank-bm25` 不适用于无分词中文短词条**。
*   **Rationale:** 知识网状态染色的精确度直接决定用户薄弱点的诊断质量，并查集可逆回滚的健全性决定了知识图谱数据的安全性。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 
    1. 在 `question_steps` 中增加 `is_blocked_step` 与 `reused_existing_node`；
    2. 节点展示状态仅聚合 `is_blocked_step = 1` 的步骤；
    3. 明确收敛度 $\rho_m$ 计算与 $p_i$ 分母定义；
    4. 在 `knowledge_graph.py` 补充标红边记录与销账方法；
    5. 将 `compaction_audit_ledger` 主键改为自增 `merge_id` 并增加 `affected_child_node_ids_json`；
    6. 移除 `rank-bm25`，改用标准库 N-gram Jaccard 重合度。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L197, L260, L427-L441, L566, L650-L653
    *   Change:
        - 在 `question_steps` DDL 增加 `is_blocked_step` 与 `reused_existing_node` 列；
        - 在 `compaction_audit_ledger` DDL 将主键改为 `merge_id INTEGER PRIMARY KEY AUTOINCREMENT`，保留 `batch_id TEXT NOT NULL`，增加 `affected_child_node_ids_json TEXT NOT NULL DEFAULT '[]'`；
        - 明确节点状态仅由 `is_blocked_step = 1` 聚合驱动；
        - 补充 `record_co_error_edges` 与 `decrement_co_error_edges_on_mastery` 接口说明；
        - 将 Top-15 检索实现改为标准库字符 N-gram Jaccard 重合度。

---

### CR-6 [egm-zoom-out / Multi-Persona] - Severity: Major
*   **Affected Scope:** [Lines 167-180, 212-228, 387, 418-421, 474, 641, 666, 674-675](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L167-180) (DDL 2.1, 2.2, 3.2, 3.3, 4, 5.2)
*   **Discrepancy Description:**
    1. **本地题库四通道检索缺少特征来源**：`questions` 表未存储四通道向量，新题入库时 `question_relations` 为空；若对全表实时做四通道向量比对将导致 $O(N)$ 开销。
    2. **`GET /api/v1/drills/daily` 无幂等锁导致刷新即无限顺延**：`get_daily_blind_drill_queue` 每次调用都递增溢出题的 `system_deferred_days` 并顺延 `blind_test_due_at`，且 `apply_overdue_escrow_decay` 缺乏判断“某题已进入题单但超过 3 个活跃日未测”的字段依据。
*   **Rationale:** 本地轻量化系统必须依赖确定性且低消耗的检索手段，每日题单必须具备严格的幂等锁防止数据状态漂移。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 
    1. 在 `questions` 增加 `trap_tags_json`；本地关联题检索优先基于 SQLite 索引按考点、步数、难度与陷阱标签直接匹配；
    2. 在 `questions` 增加 `slated_drill_date` 与 `slated_exposure_count` 字段，锁定当日推送题单，多次刷新页面幂等返回，仅在跨日首次加载时按需顺延溢出题。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L167, L180, L387, L419, L641, L666, L674-L675
    *   Change:
        - 在 `questions` 表增加 `trap_tags_json TEXT NOT NULL DEFAULT '[]'`、`slated_drill_date DATE` 与 `slated_exposure_count INTEGER NOT NULL DEFAULT 0`；
        - 明确本地轻量化检索基于 SQLite 索引的确定性规则；
        - 重写 `get_daily_blind_drill_queue` 逻辑，使用 `slated_drill_date` 实现当日幂等锁定。

---

### CR-7 [egm-inventive-catalyst / Multi-Persona] - Severity: Major
*   **Affected Scope:** [Lines 381-386, 400-421, 467-478, 566, 582-587, 637-642, 671-676](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L381-386) (3.2, 3.3, 4, 5.1, 5.2)
*   **Discrepancy Description:**
    1. `confirm_diagnostic_gate`（L637-L640）遗漏了 `ALREADY_MASTERED` 分支与空草稿（`last_valid_step_index == 0`）跳过保底分结算的判断，且在 `TRAP_OR_CALC_ERROR` 分支下未触发或等待 `SHARED_TRAP` 变式题预热。
    2. FeatureRange 正文（L409-L421）未写明粗心题 2～4 天 vs. 概念题 3～7 天的双轨盲测窗口（仅在 L178 注释与 L586 出现），且未说明盲测回退原题时 `BlindDrillItemDTO` 需携带 `probe_question_md` 及如何构造非会话态的 `idempotency_key`。
    3. `pyproject.toml`（L566）将客户端写成 `httpx / openai / google-genai` 三选一未决，且 `backend/config.py`（L582-L586）缺少模型网关配置项。
*   **Rationale:** 状态机分支缺失与网络层配置模糊会导致核心训练流程死锁、前后端联调失败。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 
    1. 补齐 `confirm_diagnostic_gate` 的 `ALREADY_MASTERED` 与空草稿分支；
    2. 引入 `in_flight_prewarm_tasks` 字典管理预热并发并支持同步 await 等待；
    3. 正文明确双轨盲测窗口规则（粗心题 2-4 天，概念题 3-7 天）；
    4. 明确盲测回退原题时携带 `probe_question_md`；
    5. 锁定 `httpx` 调用 OpenAI 兼容接口，并补齐 `Settings` 配置字段。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L410, L418, L421, L566, L582-L587, L637-L642, L674-L675
    *   Change:
        - 补全 `confirm_diagnostic_gate` 状态机分支逻辑与预热 await 机制；
        - 在 3.3 节正文写明双轨盲测窗口与回退携带 `probe_question_md` 规则；
        - 在 `config.py` 补齐大模型 API Key、Base URL 与 Model Name 等配置项。

---

### CR-8 [egm-implementation-readiness] - Severity: Major
*   **Affected Scope:** [Lines 556-737](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L556-737) (Section 5)
*   **Discrepancy Description:**
    对照实施就绪标准（Implementation Readiness Standard）的 **Phasing & Checkpoints** 要求，`Detailed Implementation Plan` 目前仅平铺了 18 个文件，缺少逻辑递进的分阶段划分（Phases）与每个阶段可独立构建/测试的 Checkpoint 命令。
*   **Rationale:** 缺乏分阶段门禁会给后续代码实现带来风险，开发者难以循序渐进地构建系统并做阶段性验收。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 按照“存储与算法底座 -> 沙盒与脚手架 -> 知识网与 Elo 账本 -> API 路由与全链路”将 18 个文件收敛为 4 个清晰的执行阶段，并为每个阶段定义明确的自动化 Checkpoint 命令。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L556 之后（`## Detailed Implementation Plan` 开头）
    *   Change: 增设 `### Implementation Phases & Incremental Checkpoints` 结构化表格与阶段验证指令。

---

### CR-9 [egm-structural] - Severity: Major
*   **Affected Scope:** [Lines 529-555](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L529-555) (Section 4, NFR, Resources, Future)
*   **Discrepancy Description:**
    1. 对照 Feature Spec Standard，文档缺少显式的 `## Risks & Mitigations` 一级章节来集中声明系统边界、潜在故障模式与缓解策略。
    2. L544 `### Additional Resources` 与 L549 `### Future Improvements` 位于顶层分割线之后却误用了 H3，在文档 AST 中被错误归入 `## Non-Functional Requirements` 下。
*   **Rationale:** 风险与缓解机制是技术方案评估的必备组成部分，标题层级错乱会破坏文档结构规范与可读性。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 
    1. 在 `## Non-Functional Requirements` 后新增独立的 `## Risks & Mitigations` 章节，表格化呈现系统边界、平台差异、并发写锁与外部接口超时的应对方案；
    2. 将 `Additional Resources` 和 `Future Improvements` 升级为独立的二级标题（H2）。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L542-L555
    *   Change:
        - 插入 `## Risks & Mitigations` 章节及四维风险对照表；
        - 将 `### Additional Resources` 替换为 `## Additional Resources`；
        - 将 `### Future Improvements` 替换为 `## Future Improvements`。

---

### CR-10 [egm-style] - Severity: Major
*   **Affected Scope:** [Lines 13-14, 21-31, 54, 83, 97, 109, 255, 289, 356-374, 391, 421-429, 485-539, 641-728](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L13-14) (全文多处)
*   **Discrepancy Description:**
    1. 多处使用未校准的绝对化表述（如“消除幻觉”、“全程零数据丢失”、“永不死锁”、“100% 算得通”、“零等待感知”、“100%拦截”、“100% 恢复”）。
    2. 组件名称“1 秒步骤对齐确认条”与 G1 中 `< 3 秒` 的延迟目标字面冲突。
    3. 滥用“物理 / 物理级”修饰前端 UI 槽位、SQL `CHECK` 约束和接口字段剥离。
*   **Rationale:** 技术规范应保持客观、严谨与精确，过度夸大的绝对化用词和术语误用会削弱技术方案的可信度。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 
    1. 将绝对化用语校准为工程客观表述；
    2. 组件名称统一规范为“**步骤对齐微确认条（Step-Skeleton Diff Gate）**”；
    3. 将“物理级”替换为“数据库约束级 / 独立槽位隔离 / 接口层剥离”，清理口语化修饰词。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` 全文对应行
    *   Change: 批量执行中立化文本替换与术语校准。

---

### CR-11 [egm-structural / egm-design] - Severity: Nit
*   **Affected Scope:** [Lines 396-412](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L396-412) (3.3 状态机图)
*   **Discrepancy Description:**
    `stateDiagram-v2` 中缺少 `[*] --> STAGE_0_CONFIRM_GATE` 初始转移箭头，未展示终态 `SESSION_COMPLETED`，且未说明盲测失败退回 `🔴ACTIVE` 时重训会话的起始阶段。
*   **Rationale:** 保证 Mermaid 状态机图语法严谨，并消除盲测失败退回后的状态分流歧义。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 在 Mermaid 状态图中补充初始状态转移与 `SESSION_COMPLETED`，并在 3.3 节正文说明盲测失败根据是否做出一阶子目标分别从 `STAGE_1` 或 `STAGE_4` 启动重训。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L396-L412
    *   Change: 在 Mermaid 状态机代码块中补齐入口和终态，并在正文增加退回重训分支说明。

---

### CR-12 [egm-structural] - Severity: Nit
*   **Affected Scope:** [Lines 46, 149, 209, 352, 437, 465, 487-525, 551](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L46) (全文格式)
*   **Discrepancy Description:**
    1. L487-L525 共 7 处出现口语化的 `Rejected by author:` 归因前缀；
    2. L437 KaTeX 公式内混用 Emoji 上标 `\text{🟢}`、`\text{🟡}`；
    3. L209 的《理论基础.md》引用缺少带锚点的 Markdown 链接；
    4. 分割线格式混用（`---` vs `--------------------------------------------------------------------------------`）。
*   **Rationale:** 符合 Markdown 排版与 Google Style Guide 规范要求，消除排版瑕疵。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 剥离口语化前缀，KaTeX 上标改为纯文本，规范化分割线与文档超链接。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` 对应行
    *   Change: 执行文本与排版规范化替换。

---

### CR-13 [egm-implementation-readiness] - Severity: Nit
*   **Affected Scope:** [Lines 650-651, 682, 691](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L650-651) (5.2 文件清单)
*   **Discrepancy Description:**
    1. 正文说明每新增 15 个节点触发 Compaction，但代码清单未声明该常量；
    2. 前端 Markdown 渲染未明确说明 `cas://` 协议如何转换为静态资源 URL。
*   **Rationale:** 消除前后端实现层面的隐式约定。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 在 `knowledge_graph.py` 说明中明确 `COMPACTION_BATCH_THRESHOLD = 15`，在前端组件说明中明确正则替换规则 `cas://([a-f0-9]{64})` -> `/assets/$1`。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L650, L691
    *   Change: 补充常量定义与前端正则替换说明。

---

### CR-14 [egm-implementation-readiness / egm-codebase-details] - Severity: Nit
*   **Affected Scope:** [Lines 560-723, 732](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L560-723) (5.2, 5.3)
*   **Discrepancy Description:**
    1. 文件清单标题展示的路径在部分位置为包含 `../../` 的相对路径，部分为纯文件名；
    2. L732 的 `python3 -m py_compile backend/**/*.py tests/*.py` 在缺少通配符递归支持的 Shell 下会报错。
*   **Rationale:** 保证构建脚本与测试命令在各种终端环境下的跨平台兼容性。
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 统一标题显示为仓库根相对路径，并将编译检查命令替换为标准的 `python3 -m compileall -q backend tests`。
*   ***Proposed Edit:***
    *   Location: `docs/designs/adaptive_practice_harness.md` L560-L723, L732
    *   Change: 规范化文件清单标题链接文本，替换编译命令为 `python3 -m compileall -q backend tests`。
