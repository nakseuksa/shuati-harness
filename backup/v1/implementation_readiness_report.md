# Stage 9: Implementation Readiness Review Report (`implementation_readiness_report.md`)

* **Target File**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L556-L742) (`DetailedRange`: Lines 556..742)
* **Document Standard**: [implementation-readiness-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/implementation-readiness-standard.md)

## Reviewer Status

| Reviewer | Role Title | Status |
| :--- | :--- | :--- |
| `egm-implementation-readiness` | Stage 9 Implementation Readiness Reviewer | `completed` |
| `egm-codebase-details` | Stage 9 Codebase Details Reviewer | `completed` |
| `egm-style` | Stage 9 Style Reviewer (DetailedRange) | `completed` |

---

## Part 1: Pragmatic Tech Lead (`egm-implementation-readiness`) Findings

### IR-1: [Blocker] 四维难度重校准公式的分档阈值表缺失，且旧阈值直接导致单元测试 `(2,3,1,3) -> L4-A` 失败
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L364-L366)、[L604-L610](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L604-L610)（`backend/services/difficulty_rubric.py`）、[L698](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L698)（`tests/test_difficulty_and_elo.py`）
* **Finding & Guidance**:
  1. 文档将基础 Elo 公式从 [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md#L192) 的 `800 + 95*(2^C) + 110*S + 120*T + 70*L` 重校准为 `min(2800, max(800, 750 + 85*(2**c - 1) + 105*s + 115*t + 65*l))`（`L365`, `L609`），但 **未给出重校准后映射至 8 个 `tier`（`L1_基础直达` 至 `L5-3_殿堂极限`）的完整 `if/elif` 分档阈值表**。
  2. 在新公式下，测试断言 `L698` 要求验证 `(2,3,1,3) -> L4-A`。代入新公式计算：`750 + 85*(2^2 - 1) + 105*3 + 115*1 + 65*3 = 1630`。若实现者直接沿用 [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md#L199) 的旧阈值（`C <= 2 and base_elo >= 1750` 才进入 `L4-A_进阶拔高(繁)`），`1630` 会落入 `L3_综合中档`，导致 `L698` 单测直接失败！
  3. **修复要求**：在 `L366` 及 `L609` 明确写出适配新公式的完整确定性 `if/elif` 分档条件，消除实现者的阈值猜测。

---

### IR-2: [Blocker] 子步骤单调性硬裁剪公式在边界值（`S_orig = 1` 或 `D_orig < 950`）下触发 SQLite `CHECK` 约束崩溃
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L200-L204)（`question_steps` DDL）、[L453-L454](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L453-L454)、[L610](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L610)（`clamp_substep_difficulty`）、[L699](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L699)
* **Finding & Guidance**:
  1. `L453` 与 `L610` 规定子步骤推理链路长度裁剪为 $S_{\text{sub}} \le S_{\text{orig}} - 1$（未加下界 `max(1, ...)`），而 `L201` 的 `question_steps` 表定义了硬约束 `s_sub INTEGER NOT NULL CHECK (s_sub BETWEEN 1 AND 3)`。当原题 $S_{\text{orig}} = 1$ 时，$S_{\text{orig}} - 1 = 0$，写入 `question_steps` 将直接抛出 `sqlite3.IntegrityError: CHECK constraint failed: s_sub BETWEEN 1 AND 3`。
  2. 同理，`L454` 与 `L610` 规定 $D_{\text{sub}} = \min(D_{\text{orig}} - 150, \text{base\_elo}(...))$，而 `L204` 定义了 `sub_elo INTEGER NOT NULL CHECK (sub_elo BETWEEN 800 AND 2650)`。当原题 $D_{\text{orig}} \in [800, 949]$ 时，$D_{\text{orig}} - 150 < 800$，同样会触发 `CHECK` 约束崩溃。
  3. **修复要求**：在 `L453-L454` 与 `L610` 将裁剪公式修正为带下界保护的精确表达式：
     - $S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, \max(1, S_{\text{orig}} - 1))$；
     - $D_{\text{sub}} \leftarrow \max(800, \min(2650, D_{\text{orig}} - 150, \text{base\_elo}(C_{\text{sub}}, S_{\text{sub}}, T_{\text{sub}}, L_{\text{sub}})))$。

---

### IR-3: [Blocker] 双轨 Elo 托管池（Escrow）与实战分守恒算式存在多处未决歧义，无法直接通过 `L700` 守恒单测
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L311-L349)（`question_escrow_pool` & `elo_mutation_ledger` DDL）、[L458-L464](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L458-L464)、[L655-L668](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L655-L668)（`backend/services/elo_engine.py`）、[L700](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L700)
* **Finding & Guidance**:
  1. **基准分 $R_{\text{combat}}$ 引用时刻歧义**：`L700` 要求“同一道错题依次走完草稿保底 $\to$ 脚手架通关 $\to$ 拆拐杖变式 $\to$ 3～7 天盲测销账全流程后，累计实战分净增量与直接独立做对该题严格守恒”。需明确：计算 $\Delta R_{\text{target}}$ 时使用的基准分是进入该题前的初始快照分 $R_0 = R_{\text{combat}} - \text{floor\_elo\_applied}$（并对齐目标系数或明确守恒基准为 $\Delta R_{\text{target}}(R_0, D_{\text{orig}})$）。
  2. **20% 当场兑现与 80% 冻结的字段语义歧义**：设总余量为 $\Delta_{\text{rem}} = \max(0, \Delta R_{\text{target}} - \text{floor\_elo\_applied})$，拆拐杖通过当场向 `combat_elo` 增加 $0.2 \cdot \Delta_{\text{rem}}$，而存入 `question_escrow_pool.escrow_combat_delta` 的是剩余未兑现的 $0.8 \cdot \Delta_{\text{rem}}$。
  3. **超时衰减时“同步回吐未兑现覆盖分”的算式与枚举映射缺失**：需在 `question_escrow_pool` 增加 `escrow_coverage_delta` 并明确回吐算式及 `elo_mutation_ledger.mutation_trigger` 映射关系。

---

### IR-4: [Blocker] SymPy 沙盒存在两处必然导致测试失败的实现陷阱（macOS `RLIMIT_AS` 崩溃 + 移项反号方程等价判定矛盾）
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L416)、[L628-L630](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L628-L630)（`backend/sandbox/sympy_verifier.py`）、[L710-L711](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L710-L711)（`tests/test_sympy_sandbox.py`）
* **Finding & Guidance**:
  1. **macOS 下 `resource.RLIMIT_AS` 崩溃问题**：在 macOS 上，Python 进程因加载系统 `dyld` 共享缓存，初始虚拟地址空间已远超 `256MB`；在子进程中直接调用 `resource.setrlimit(resource.RLIMIT_AS, (256*1024*1024, ...))`（`L629`）会立即抛出 `ValueError` 或 `OSError(EINVAL)`。需在 `L629` 明确平台兼容：在 Linux 上设置 `RLIMIT_AS`，在 macOS (`sys.platform == 'darwin'`) 上捕获 `(ValueError, OSError)` 降级为设置 CPU 时间上限 `RLIMIT_CPU`。
  2. **方程移项变形等价判定的数学矛盾**：`L416` 指示使用 `simplify(lhs - rhs) == 0` 校验等价性，但 `L711` 的测试用例要求验证 `m*v**2/2 - q*U` 与 `q*U - 1/2*m*v**2` 等价（互为相反数 $E_1 = -E_2$）。需在 `L630` 明确规定同时检查 `simplify(e1 - e2) == 0` 或 `simplify(e1 + e2) == 0`（或比值为非零常数）。

---

### IR-5: [Major] 违反 Standard 的 `Phasing & Checkpoints` 要求：缺少分阶段实施划分与阶段性验证门禁
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L556-L737)
* **Finding & Guidance**:
  [implementation-readiness-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/implementation-readiness-standard.md#L20-L23) 强制要求将实施计划划分为逻辑增量阶段（Phases & Checkpoints）。需在 `Detailed Implementation Plan` 中补充分阶段执行计划（Phase 1～Phase 4），明确每个 Phase 包含的目标文件编号与对应的阶段性编译/单测验证指令。

---

### IR-6: [Major] 文件边界（Write Allowlist）遗漏 Python 包初始化文件与构建配置细节，且路径前缀不统一
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L560-L567)（`pyproject.toml`）、[L577-L729](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L577-L729)、[L732-L736](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L732-L736)
* **Finding & Guidance**:
  将 `backend/__init__.py`、`backend/db/__init__.py`、`backend/services/__init__.py`、`backend/sandbox/__init__.py` 显式纳入 `Target Files to Create/Modify`，在 `pyproject.toml` 条目中明确指定 `[build-system]`、`[tool.setuptools.packages.find] include = ["backend*"]` 与 `[tool.pytest.ini_options] pythonpath = ["."]`，并使用仓库根相对路径（repository-relative file paths）。

---

### IR-7: [Major] 知识网“分布熵校准收敛度”的节点复用率 $k_{\text{reuse}}$ 与“版图攻克率”的 `origin_type` 过滤条件同 SQLite Schema 不一致
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L160-L162)、[L190-L206](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L190-L206)、[L439-L441](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L439-L441)、[L650-L654](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L650-L654)（`backend/services/knowledge_graph.py`）
* **Finding & Guidance**:
  在 `question_steps` 表（`L197`）增加 `reused_existing_node INTEGER NOT NULL DEFAULT 0` 字段，并在 `L653` 明确规定使用 `origin_type = 'USER_CANONICAL'` 过滤用户录入题。

---

### IR-8: [Major] REST API 请求/响应 DTO 字段契约、盲测原题中间量核验数据源及 LLM/VLM 网关接口未完整定义
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L467-L478)、[L566](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L566)、[L612-L622](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L612-L622)、[L669-L684](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L669-L684)
* **Finding & Guidance**:
  1. 补充 REST 端点的核心入参/出参字段定义（特别是 `/api/v1/questions/ingest` 的 `initial_status` 入参）。
  2. 明确盲测原题中间量核验的数据源为该题最近一次 `diagnostic_sessions` 的 `(matched_branch_id, stalled_step_index)` 对应的 `question_steps.probe_question_md` 与 `step_formula_latex`。
  3. 明确锁定统一使用 `httpx`（调用兼容 OpenAI Chat Completions 格式的 VLM/LLM 端点）并在 `config.py` 中配置 `LLM_API_KEY`、`LLM_BASE_URL`、`VLM_MODEL_NAME`、`SOLVER_LLM_MODEL_NAME` 与 `USE_MOCK_LLM`。

---

### IR-9: [Nit] Compaction 自动触发时机与手动触发端点的衔接说明
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L94)、[L585](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L585)、[L650-L651](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L650-L651)、[L682](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L682)

---

### IR-10: [Nit] 前端渲染 `cas://` 图片 URI 的重写规则
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L601-L602)、[L682](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L682)、[L685-L692](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L685-L692)

---

## Part 2: Pedantic Technical Quality Auditor (`egm-codebase-details`) Findings

### CD-1: 🔴 Blocker：双轨 Elo 跨阶段守恒测试断言与算分参数（`K_INIT` vs `K_COMBAT`）及托管兑现逻辑存在数学自相矛盾
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L586) (`backend/config.py`)、[L661-L665](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L661-L665) (`backend/services/elo_engine.py`)、[L700](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L700) (`tests/test_difficulty_and_elo.py`)
* **Discrepancy Description & Rationale**:
  `freeze_decrutch_escrow` 计算 $\Delta R_{\text{target}}$ 时必须基于进入该题前的初始快照分 $R_{\text{combat}}^{(0)}$，且需统一目标系数（或明确守恒基准），并明确 `escrow_combat_delta` 仅存储冻结的 **80% 余额**（即 $0.8 \times \max(0, \Delta R_{\text{target}} - \Delta R_{\text{floor}})$）。

---

### CD-2: 🔴 Blocker：四维难度新校准公式未同步重校准分档阈值，且子步单调性裁剪公式在 $S=1$ 时触发 SQLite `CHECK` 约束崩溃
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L608-L611) (`backend/services/difficulty_rubric.py`)、[L698-L699](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L698-L699) (`tests/test_difficulty_and_elo.py`)
* **Discrepancy Description & Rationale**:
  需在第 609 行完整列出与新公式匹配的确定性 `tier` 判定分支表（确保 `(2,3,1,3) -> 1630` 精确映射至 `L4-A_进阶拔高(繁)`），并将第 610 行（及第 453-454 行）的子步裁剪公式修正为 $S_{\text{sub}} \le \max(1, S-1)$ 且 $D_{\text{sub}} = \max(800, \min(D_{\text{orig}} - 150, \text{base\_elo}(C_{\text{sub}}, S_{\text{sub}}, T_{\text{sub}}, L_{\text{sub}})))$。

---

### CD-3: 🔴 Blocker：SymPy 沙盒在 macOS 上强制调用 `resource.RLIMIT_AS` 必现崩溃，且 `verify_equation_equivalence` 无法通过移项反号测试
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L628-L630) (`backend/sandbox/sympy_verifier.py`)、[L708-L711](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L708-L711) (`tests/test_sympy_sandbox.py`)
* **Discrepancy Description & Rationale**:
  明确 `RLIMIT_AS` 在 macOS (`sys.platform == 'darwin'`) 下的兼容处理；在 `verify_equation_equivalence` 中支持 `simplify(e1 - e2) == 0 or simplify(e1 + e2) == 0`（及非零常数比值）；在 AST 校验中仅放行 `sympy`/`math`/`fractions` 导入并拒绝双下划线属性访问。

---

### CD-4: 🔴 Blocker：`origin_type` 枚举值与 SQLite `CHECK` 约束冲突，且 `pyproject.toml` 缺失 `pydantic-settings` 等必需依赖与构建配置
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L560-L567) (`pyproject.toml`)、[L582](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L582) (`backend/config.py`)、[L653](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L653) (`backend/services/knowledge_graph.py`)
* **Discrepancy Description & Rationale**:
  1. 将第 653 行（及第 75 行）统一修正为精确枚举值（`origin_type == 'USER_CANONICAL'` / `'AI_SYNTHETIC_SCAFFOLD'`）。
  2. 在 `pyproject.toml`（第 566-567 行）补充 `pydantic-settings`、`python-multipart` 及 `[tool.setuptools.packages.find]` / `[tool.pytest.ini_options]` 配置。

---

### CD-5: 🟡 Major：`scaffold_engine.py` 遗漏 `ALREADY_MASTERED` 分支与空草稿（$m=0$）保底分防呆，且未区分粗心题的 2～4 天盲测窗口
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L637-L642) (`backend/services/scaffold_engine.py`)、[L663-L665](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L663-L665) (`backend/services/elo_engine.py`)

---

### CD-6: 🟡 Major：跨知识点标红边 `concept_co_errors` 只有合并清理与回滚逻辑，缺失入库写入与销账扣减的模块职责定义
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L644-L654) (`backend/services/knowledge_graph.py`)、[L259-L271](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L259-L271) (`compaction_audit_ledger` DDL)

---

### CD-7: 🟡 Major：每日盲测队列 `BlindDrillItemDTO` 缺失原题中间步反问提示字段与变式优先路由，且盲测/逾期衰减缺少幂等键 `session_id` 规范
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L661-L676) (`backend/services/elo_engine.py`, `backend/services/blind_drill_scheduler.py`)

---

### CD-8: 🟡 Major：`backend/config.py` 与 `pyproject.toml` 遗漏 VLM/LLM 模型网关配置项，违反 Zero-Decision Rule
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L566) (`pyproject.toml`)、[L577-L587](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L577-L587) (`backend/config.py`)

---

### CD-9: 🟢 Nit：目标文件标题路径使用相对文档路径 `../../` 与 Verification Plan 的仓库根相对路径不一致
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L560-L723) vs [L732-L736](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L732-L736)

---

### CD-10: 🟢 Nit：Verification Plan 中的 `python3 -m py_compile backend/**/*.py` 在非 globstar Shell 下会漏检顶层模块
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L732)

---

## Part 3: Technical Copy Editor (`egm-style`, DetailedRange) Findings

### SY2-1: 绝对化测试覆盖声明与主观修饰词失真（Defensive Caveats & Uncalibrated Claims: `CALIBRATE`）
* **Severity**: Major
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L706-L720)（第 706、710、720 行）
* **Style Rationale**:
  校准第 706 行“100%拦截 / 优雅降级”、第 710 行“可靠终止”、第 720 行“100% 恢复”为具体可观测断言。

---

### SY2-2: 枚举值简称与 Schema DDL 契约及安全声明不一致（Claim Calibration: `CALIBRATE`）
* **Severity**: Major
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L628-L653)（第 628、641、653 行）
* **Style Rationale**:
  第 653 行统一为 `origin_type = 'USER_CANONICAL'`；第 628 行对齐第 531 行导入白名单与双下划线属性访问拦截。

---

### SY2-3: 绝对化情绪副词与宣传性修饰语（Hyperbole & Marketing Fluff）
* **Severity**: Nit
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L658-L701)（第 658、666、688、698、701、728 行）

---

### SY2-4: 同义词堆叠与口语化动词（Synonym Redundancy & Wordiness）
* **Severity**: Nit
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L641-L728)（第 641、667、728 行）

---

### SY2-5: 子步骤单调性裁剪公式边界表述精度（Precision Nit）
* **Severity**: Nit
* **Affected Scope**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L610)（第 610 行）
