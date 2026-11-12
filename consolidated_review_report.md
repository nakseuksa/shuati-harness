# Consolidated Review Report: `adaptive_practice_harness.md`

* **Target Document**: [adaptive_practice_harness.md](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md) (Lines 1..742)
* **Feature Range**: Lines 1..555 (`FeatureRange`)
* **Detailed Range**: Lines 556..742 (`DetailedRange`)
* **Stage Reports**:
  1. **Stage 5 (Initial Zoom Out)**: [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) (`egm-zoom-out`, `egm-inventive-catalyst`)
  2. **Stage 6 (Comprehension Test)**: [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md) (`egm-comprehension`)
  3. **Stage 7 (Critic Review)**: [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) (`egm-design`, `egm-structural`, `egm-style`)
  4. **Stage 8 (Intermediate Zoom Out)**: [intermediate_zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/intermediate_zoom_out_report.md) (`egm-zoom-out`)
  5. **Stage 9 (Implementation Readiness Test)**: [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) (`egm-implementation-readiness`, `egm-codebase-details`, `egm-style`)

---

## Executive Summary of Review Findings

5 个审查阶段的 10 个独立 Goldfish 视角共提出 **41 条原始发现**，经跨阶段去重与架构对齐后收敛为 **14 项独立可执行修改项（Action Items）**：
* **🔴 Blocker（阻塞级，共 4 项）**：涉及双轨 Elo 托管池数学守恒与不变量破缺、四维难度校准公式分档阈值缺失与 SQLite `CHECK` 边界崩溃、SymPy 沙盒在 macOS 下的 `RLIMIT_AS` 崩溃与方程移项等价判据矛盾、`origin_type` 枚举冲突与 `pyproject.toml` 核心依赖缺失。
* **🟡 Major（重要级，共 6 项）**：涉及知识网节点步骤级状态聚合与并查集两跳回滚快照、本地题库结构化索引检索与每日盲测题单幂等锁、异步预热在粗心分支的补齐与 API 契约、实施计划分阶段门禁（Phasing & Checkpoints）、文档结构标准化（独立 `Risks & Mitigations` 章节与 H2 层级）、以及绝对化声明与术语校准。
* **🟢 Nit（细节打磨级，共 4 项）**：涉及状态图与枚举对称性、移除 `Rejected by author:` 归因前缀与分割线/公式格式统一、前端 `cas://` 重写与自动 Compaction 触发衔接、以及编译校验命令跨 Shell 兼容。

---

## 🔴 Blockers (Must Resolve Before Implementation)

### CR-1 [Blocker]: 双轨 Elo 托管池（Escrow）数学守恒矛盾、双轨不变量倒挂与死分支触发器
* **Source Findings**: `COMP-1`, `DC-1`, `IC-2`, `ZO-3`, `IZO-1`, `IR-3`, `CD-1`, `DC-7`, `COMP-6`
* **Affected Lines**: [L63](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L63), [L83-L87](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L83-L87), [L97](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L97), [L311-L349](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L311-L349), [L445-L464](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L445-L464), [L476](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L476), [L540](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L540), [L586](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L586), [L655-L668](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L655-L668), [L700](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L700)
* **Problem Description**:
  1. **跨阶段净增量不守恒（导致 L700 单测必败）**：L700 要求“错题走完草稿保底 $\to$ 脚手架 $\to$ 拆拐杖 $\to$ 盲测销账全流程后，累计实战分净增量与直接独立做对该题严格守恒”。但 L450 直接做对使用 $K_{\text{init}} = 24.0$，而 L459 错题目标增量使用 $K_{\text{combat}} = 28.0$；且第一阶段发放 $\Delta R_{\text{floor}}$ 后模块 $R_{\text{combat}}$ 已上涨，若 L459 用上涨后的 $R_{\text{combat}}$ 计算 $E(R_{\text{combat}}, D_{\text{orig}})$，总增量无法守恒。
  2. **20% 预支与全额解冻导致 120% 超发及盲测失败刷分漏洞**：L459 规定 `escrow_combat_delta = max(0, Delta_R_target - floor_elo_applied)` 并注明“当场先兑现 20%，剩余 80% 冻结”，而 L461 又将 `escrow_combat_delta * decay_factor` 全额解冻，语义歧义导致发放 120%；且盲测失败重置托管池时若不记录已发放分值，重做变式会重复刷取 20% 实战分与覆盖分。
  3. **法则 2 未同步上推覆盖分导致 $R_{\text{combat}} > R_{\text{cov}}$ 倒挂**：L455 发放草稿保底实战分 $\Delta R_{\text{floor}}$ 时未执行 $R_{\text{cov}} \leftarrow \max(R_{\text{cov}}, R_{\text{combat}})$，在初始模块（`1200/1200`）下直接违反 L445 的全局双轨不变量。
  4. **死分支触发器枚举**：并查集 Compaction 已严禁跨 `L1` 合并（L429），不可能影响按 `L1` 核算的 Elo，L338 的 `'COMPACTION_REPLAY_ADJUST'` 与 L333 的 `'WATERMARK_CLAMPED'` 在 `elo_engine.py` 四个写入函数中从未使用。
* **Actionable Resolution (Adopted from Stage 8 Minimalist Convergence `IZO-1`)**:
  1. **严守双轨正交边界（零预支、零覆盖分倒扣）**：
     - 在 L455（法则 2）结算草稿保底分 $\Delta R_{\text{floor}}$ 后，强制追加 $R_{\text{cov}} \leftarrow \max(R_{\text{cov}}, R_{\text{combat}})$；
     - 在 `question_escrow_pool`（L312）中记录 `initial_combat_elo_snapshot REAL NOT NULL`（入库刻模块初始实战分 $R_{\text{combat}}^{(0)}$）、`floor_elo_applied REAL NOT NULL DEFAULT 0.0` 与 `escrow_combat_delta REAL NOT NULL DEFAULT 0.0`，并增加 `is_cov_settled INTEGER NOT NULL DEFAULT 0`；
     - 统一令 $K_{\text{init}} = K_{\text{combat}} = 24.0$（更新 L450, L459, L586）；当场拆拐杖通过升入 `🟡PENDING_RETEST` 时，仅在 `is_cov_settled == 0` 时结算一次覆盖分 $\Delta R_{\text{cov}}$，并基于入库初始快照分 $R_{\text{combat}}^{(0)}$ 计算整题目标总增量 $\Delta R_{\text{target}} = K_{\text{combat}} \cdot (1 - E(R_{\text{combat}}^{(0)}, D_{\text{orig}}))$，将差额 $\text{escrow\_combat\_delta} = \max(0, \Delta R_{\text{target}} - \text{floor\_elo\_applied})$ **100% 冻结在托管池（移除当场预支 20% 实战分与覆盖分按日回吐）**；
     - 3～7 天盲测通过升入 `🟢MASTERED` 时解冻 `escrow_combat_delta * decay_factor` 合入实战分（当 `decay_factor == 1.0` 时，累计实战分净增量 $\Delta R_{\text{floor}} + \text{escrow\_combat\_delta} \equiv \Delta R_{\text{target}}$，与在 $R_{\text{combat}}^{(0)}$ 下直接独立做对严格守恒，完美通过 L700 测试）；逾期未测时按日线性扣减 `decay_factor = max(0.0, decay_factor - 0.15)`。
  2. **清理死枚举**：从 `elo_mutation_ledger.mutation_trigger`（L330-L339）移除 `'WATERMARK_CLAMPED'`（由 `'MASTERED_INIT'` 的 `watermark_gamma` 列体现）与 `'COMPACTION_REPLAY_ADJUST'`，并移除 L97、L476 中回滚并查集需重算 Elo 的冗余表述。

---

### CR-2 [Blocker]: 四维难度 $[C, S, T, L]$ 分档阈值表缺失、CUJ 算例算错及子步单调性裁剪触发 SQLite `CHECK` 约束崩溃
* **Source Findings**: `COMP-3`, `DC-2`, `ZO-4`, `IZO-2`, `IR-1`, `IR-2`, `CD-2`, `SY2-5`
* **Affected Lines**: [L35](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L35), [L59-L60](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L59-L60), [L63](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L63), [L168-L173](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L168-L173), [L200-L204](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L200-L204), [L362-L366](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L362-L366), [L453-L454](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L453-L454), [L608-L610](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L608-L610), [L698-L699](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L698-L699)
* **Problem Description**:
  1. **子步单调性裁剪在 $S_{\text{orig}} = 1$ 或 $D_{\text{orig}} < 950$ 时触发 SQLite `CHECK` 崩溃**：L453 与 L610 写为 $S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, S_{\text{orig}} - 1)$。当 $S_{\text{orig}} = 1$ 时 $S_{\text{sub}} = 0$，违反 L201 `CHECK (s_sub BETWEEN 1 AND 3)`；当 $D_{\text{orig}} < 950$ 时 $D_{\text{orig}} - 150 < 800$，违反 L204 `CHECK (sub_elo BETWEEN 800 AND 2650)`；且 L200 `c_sub` 允许 `0`，与 `c >= 1` 冲突。
  2. **重校准公式缺少 8 档 `tier` 确定性判定阈值表**：L365 与 L609 将公式重校准为 `750 + 85*(2**c - 1) + 105*s + 115*t + 65*l`（题级值域 `[1120, 2800]`，子步保底值域 `[800, 2650]`）。在该公式下，L698 测试用例 `(2, 3, 1, 3)` 得分为 `1630`；若沿用《理论基础.md》旧阈值（`>= 1750` 才入 `L4-A`），单测 `(2,3,1,3) -> L4-A` 将直接失败。且正文未内联说明 $C, S, T, L$ 四个维度的含义。
  3. **CUJ 1 算例计算错误**：L60 中 $(C_{\text{sub}}=2, S_{\text{sub}}=1, T_{\text{sub}}=1, L_{\text{sub}}=1)$ 代入公式实际为 $750 + 255 + 105 + 115 + 65 = 1290$，而非 `1345`。
* **Actionable Resolution**:
  1. **修正 SQL 约束与单调性裁剪下界**：
     - 将 L200 `c_sub` 的约束修正为 `CHECK (c_sub BETWEEN 1 AND 4)`；
     - 将 L453-L454 与 L610 的裁剪公式修正为：
       $$C_{\text{sub}} \leftarrow \min(C_{\text{sub}}, \max(1, C_{\text{orig}} - 1)), \quad S_{\text{sub}} \leftarrow \min(S_{\text{sub}}, \max(1, S_{\text{orig}} - 1)), \quad T_{\text{sub}} \leftarrow \min(T_{\text{sub}}, T_{\text{orig}}), \quad L_{\text{sub}} \leftarrow \min(L_{\text{sub}}, L_{\text{orig}})$$
       $$D_{\text{sub}} = \max\left(800,\; \min\left(2650,\; D_{\text{orig}} - 150,\; \text{base\_elo}(C_{\text{sub}}, S_{\text{sub}}, T_{\text{sub}}, L_{\text{sub}})\right)\right)$$
  2. **内联补齐 $[C, S, T, L]$ 维度定义与重校准后的 8 档 `tier` 映射规则**（L362-L366, L609）：
     - 内联说明四维含义：$C \in [1,4]$（概念耦合与建模深度）、$S \in [1,3]$（推理链路长度）、$T \in [1,3]$（转化跨度与技巧隐蔽度）、$L \in [1,3]$（运算与分类讨论负荷）；
     - 明确确定性分档 `if/elif` 阶梯：
       1. `c == 1 and s == 1 and t == 1` $\to$ `'L1_基础直达'`；
       2. `c <= 2 and base_elo < 1400` $\to$ `'L2_常规巩固'`；
       3. `c <= 2 and base_elo >= 1600` $\to$ `'L4-A_进阶拔高(繁)'`（满足 `(2,3,1,3) -> 1630 -> L4-A`，且 $C \le 2$ 封顶 `L4-A`）；
       4. `c <= 2`（其余 `1400 <= base_elo < 1600`）$\to$ `'L3_综合中档'`；
       5. `c == 3 and t == 3 and l <= 2` $\to$ `'L4-B_进阶拔高(巧)'`；
       6. `c == 3 and base_elo >= 1850` $\to$ `'L5-1_压轴挑战'`；
       7. `c == 3`（其余）$\to$ `'L3_综合中档'`；
       8. `c == 4`：若 `base_elo < 2250` $\to$ `'L5-1_压轴挑战'`；若 `base_elo < 2550` $\to$ `'L5-2_压轴巅峰'`；否则（`>= 2550`）$\to$ `'L5-3_殿堂极限'`。
  3. **修正 CUJ 1 算例**：将 L60、L63 中的 `1345` 修正为 `1290`。

---

### CR-3 [Blocker]: SymPy 沙盒在 macOS 下调用 `RLIMIT_AS` 必现崩溃、AST 白名单未对齐及移项反号方程等价判据矛盾
* **Source Findings**: `COMP-5`, `DC-5`, `IC-4`, `IZO-6`, `IR-4`, `CD-3`, `SY2-2`
* **Affected Lines**: [L388-L390](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L388-L390), [L416](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L416), [L531-L533](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L531-L533), [L628-L630](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L628-L630), [L708-L711](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L708-L711)
* **Problem Description**:
  1. **macOS 下 `resource.RLIMIT_AS = 256MB` 启动即崩溃 & `setrlimit` 无法禁网**：在 macOS (`darwin`) 上，Python 子进程虚拟地址空间因系统 `dyld` 共享缓存映射已超数十 GB，设置 `RLIMIT_AS = 256MB` 会立即抛出 `ValueError` 或 `MemoryError`；且 POSIX `setrlimit` 不具备禁用网络功能。
  2. **方程移项反号等价判定与 `simplify(lhs - rhs) == 0` 矛盾**：L416 写 `simplify(lhs - rhs) == 0`，但 L711 单测要求验证 `m*v**2/2 - q*U` 与 `q*U - 1/2*m*v**2` 等价（二者互为相反数 $E_1 = -E_2$，`simplify(E1 - E2) != 0`）。
  3. **`solve_kernel()` 返回结构与 AST 白名单未闭合**：L388-L390 未规定 `solve_kernel()` 的返回值结构，且 L628 仅写了黑名单导入而非 L531 要求的导入白名单与双下划线内省属性拦截。
* **Actionable Resolution**:
  1. **平台兼容的子进程资源隔离与 AST 白名单**（L389-L390, L531-L533, L628-L629）：
     - AST 校验强制采用**导入模块白名单**（仅允许 `sympy`, `math`, `fractions`），拦截禁用内置函数（`open`, `eval`, `exec`, `__import__`, `getattr`）及一切双下划线属性访问（`attr.startswith("__")`，如 `__subclasses__`, `__globals__`, `__builtins__`），从语言层阻断网络与文件 I/O；
     - 子进程资源限制：在 Linux (`sys.platform.startswith("linux")`) 下设置 `RLIMIT_AS = 256MB` 与 `RLIMIT_CPU = 5`；在 macOS (`sys.platform == "darwin"`) 下设置 `RLIMIT_CPU = 5` 并对 `RLIMIT_AS` 捕获 `(ValueError, OSError)` 降级忽略，依靠宿主进程 `subprocess.run(..., timeout=5.0, start_new_session=True)` 超时强杀进程组。
  2. **明确 `solve_kernel()` 返回契约**（L388, L629）：规定 `solve_kernel()` 必须返回字典 `{"params": dict[str, str | int | float], "answer_latex": str}`，由沙盒校验 `answer_latex` 非空后再将 `params` 格式化回填至 `parametric_stem_tpl`。
  3. **修正 `verify_equation_equivalence` 判据与受控符号提示**（L416, L630, L711）：
     - 前端在 $L \ge 2$ 半程立式题干下方展示受控符号表芯片（`allowed_symbols`）；
     - `verify_equation_equivalence(user_expr_str, standard_expr_str)` 使用 `parse_expr(..., transformations=standard_transformations + (implicit_multiplication_application, convert_xor))` 与 `nsimplify(..., rational=True)` 解析；若含 `=` 则移项为 `lhs - rhs`；若满足 `simplify(e1 - e2) == 0` 或 `simplify(e1 + e2) == 0` 或 `simplify(e1 / e2).is_constant()`（多方程组则按 `;` / 换行切分并消去中间变量后比对目标未知量解集），均判定为代数等价。

---

### CR-4 [Blocker]: `origin_type` 枚举值跨节不一致，且 `pyproject.toml` 缺失 `pydantic-settings` / `python-multipart` 与包发现配置
* **Source Findings**: `COMP-7`, `SY1-4`, `SY2-2`, `IR-6`, `IR-7`, `CD-4`, `IZO-7`
* **Affected Lines**: [L75](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L75), [L160-L162](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L160-L162), [L392](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L392), [L560-L567](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L560-L567), [L582](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L582), [L653](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L653)
* **Problem Description**:
  1. `questions.origin_type` 在 L161 DDL 中定义为 `'USER_CANONICAL' | 'AI_SYNTHETIC_SCAFFOLD' | 'AI_SYNTHETIC_VARIANT'`，但 L75 与 L653 写为 `'AI_SYNTHETIC'`，按字面入库会触发 SQLite `CHECK` 失败，按字面过滤会漏掉合成题。
  2. L582 使用 `BaseSettings`，但 L566 `pyproject.toml` 未声明 `pydantic-settings`（Pydantic v2 中已拆分）和 `python-multipart`（FastAPI 上传文件必需），且在含 `backend/`、`frontend/`、`tests/`、`docs/` 多顶层目录的仓库下未声明 `[tool.setuptools.packages.find]`，执行 `pip install -e .` 必败；同时未将 `__init__.py` 列入文件清单。
* **Actionable Resolution**:
  1. 将 L75 修正为 `origin_type = 'AI_SYNTHETIC_SCAFFOLD'`；将 L653 修正为“仅统计 `origin_type = 'USER_CANONICAL'` 题目（排除 `'AI_SYNTHETIC_SCAFFOLD'` 与 `'AI_SYNTHETIC_VARIANT'`）”。
  2. 在 `pyproject.toml`（L560-L567）中补齐 `pydantic-settings>=2.0`、`python-multipart`，移除不适配无空格中文短语的 `rank-bm25`（见 CR-5），显式声明 `[build-system]`、`[tool.setuptools.packages.find] include = ["backend*"]` 与 `[tool.pytest.ini_options] pythonpath = ["."]`，并将 `backend/__init__.py`、`backend/db/__init__.py`、`backend/services/__init__.py`、`backend/sandbox/__init__.py` 纳入实施计划的目标文件清单。

---

## 🟡 Majors (Important Architectural, Structural & Readiness Improvements)

### CR-5 [Major]: 三层知识网步骤级状态归因、复用快照字段（$k_{\text{reuse}}$）、标红边生命周期与并查集两跳回滚修复
* **Source Findings**: `COMP-2`, `IC-1`, `DC-6`, `ZO-5`, `ZO-6`, `IZO-5`, `IR-7`, `CD-6`
* **Affected Lines**: [L190-L206](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L190-L206), [L244-L271](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L244-L271), [L427-L442](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L427-L442), [L566](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L566), [L649-L654](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L649-L654), [L718-L721](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L718-L721)
* **Problem Description**:
  1. **前缀已掌握步骤被整题状态“连坐标红”**：知识点挂载在 `question_steps.knowledge_node_id`。若将整题 `ACTIVE` 状态无差别传染给所有步骤节点，用户在草稿上已独立证通的前置步骤（`step_index < stalled_step_index`）对应的已掌握节点会被错误打回 `🔴ACTIVE`。
  2. **收敛度 $k_{\text{reuse}}$ 缺乏持久化列且 `p_i` 分母未明确**：表结构中未记录题目入库时是否复用了已有节点，无法在节点合并后还原最近 $W=20$ 题的复用率 $\rho_m = k_{\text{reuse}} / w$。
  3. **标红边 `concept_co_errors` 缺失写入与销账扣减入口**。
  4. **`compaction_audit_ledger` 主键冲突与两跳路径压缩回滚丢失**：以 `batch_id` 为主键导致单批次无法存多对合并，且未保存原指向 `from_node_id` 的二级子节点列表 `affected_child_node_ids_json`。
  5. **`rank-bm25` 不适用于无分词中文短词条**。
* **Actionable Resolution**:
  1. 在 `question_steps`（L197）增加 `is_blocked_step INTEGER NOT NULL DEFAULT 0`（确认门禁后对 `step_index >= stalled_step_index` 置 `1`，前缀已完成步骤置 `0`）与 `reused_existing_node INTEGER NOT NULL DEFAULT 0`（`match_or_create_l3_node` 命中既有节点时置 `1`）。
  2. 明确节点状态聚合与收敛度计算规则（L432, L439-L441, L653）：
     - 节点展示状态：仅聚合挂载在该节点上的 `USER_CANONICAL` 题目步骤中 `is_blocked_step = 1` 的题目状态（若某节点下仅有 `is_blocked_step = 0` 的已走通前缀步骤或 `MASTERED` 题目，则状态为 `🟢MASTERED`）；
     - 分布熵校准收敛度：对所属 `l1_module` 下最近 $W=20$ 道 `USER_CANONICAL` 题目的关联步骤统计 `reused_existing_node == 1` 的比例作为 $\rho_m$；$p_i$ 为节点 $i$ 挂载的 `USER_CANONICAL` 步骤数占该模块总步骤数的比例（$p_i = 0$ 时约定 $0 \ln 0 = 0$）。
  3. 在 `knowledge_graph.py`（L651-L652）中补充 `record_co_error_edges`（当错题存在 $\ge 2$ 个受阻/关联步骤节点对时按 `u < v` 写入或递增 `active_error_count`）与 `decrement_co_error_edges_on_mastery`（题目盲测销账为 `MASTERED` 时递减 `active_error_count`）。
  4. 将 `compaction_audit_ledger`（L260）主键改为 `merge_id INTEGER PRIMARY KEY AUTOINCREMENT`（保留 `batch_id TEXT NOT NULL`），并增加 `affected_child_node_ids_json TEXT NOT NULL DEFAULT '[]'`，使 `revert_compaction_batch(batch_id)` 可完整还原多对合并与二级子节点指针。
  5. 移除 `rank-bm25`，在 `match_or_create_l3_node`（L427, L650）中直接使用 Python 标准库字符 2-gram / 3-gram Jaccard 重合度预筛 `Top-15`。

---

### CR-6 [Major]: 本地题库关联检索改为结构化索引查询，并为每日盲测队列补充 `slated_drill_date` 幂等锁
* **Source Findings**: `ZO-1`, `ZO-2`, `COMP-4`, `DC-3`, `IZO-3`, `IZO-4`, `CD-7`
* **Affected Lines**: [L167-L180](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L167-L180), [L212-L228](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L212-L228), [L387](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L387), [L418-L421](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L418-L421), [L474](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L474), [L641](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L641), [L666](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L666), [L674-L675](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L674-L675)
* **Problem Description**:
  1. **本地题库四通道检索缺少特征来源**：`questions` 表未存储四通道向量，新题入库时 `question_relations` 为空；若对全表实时做四通道向量比对将导致 $O(N)$ 开销。
  2. **`GET /api/v1/drills/daily` 无幂等锁导致刷新即无限顺延**：`get_daily_blind_drill_queue` 每次调用都递增溢出题的 `system_deferred_days` 并顺延 `blind_test_due_at`，且 `apply_overdue_escrow_decay` 缺乏判断“某题已进入题单但超过 3 个活跃日未测”的字段依据。
* **Actionable Resolution**:
  1. **基于 SQLite 结构化索引的本地检索与四通道填充**（L167, L387, L420, L641, L675）：
     - 在 `questions` 表补充 `trap_tags_json TEXT NOT NULL DEFAULT '[]'`；
     - 本地检索优先查询 `question_relations` 既有边；若无预存边，直接通过 SQLite 索引执行确定性匹配并写入 `question_relations`：
       - `SCAFFOLD_PRE`：命中卡点步骤经 `CanonicalNodeResolver` 解析后的同一主 `knowledge_node_id`，且目标子题总步数 $\le 2$、难度差 $D_{\text{orig}} - D_{\text{cand}} \ge 200$（此时步骤包含度 $I(A \subset B) = 1.0 \ge 0.85$）；
       - `ISOMORPHIC`：共享相同活跃主 `knowledge_node_id` 集合且 $|D_{\text{orig}} - D_{\text{cand}}| \le 150$；
       - `SHARED_TRAP`：命中相同的 `trap_taxonomy_tag`。
  2. **每日盲测题单幂等锁**（L180, L419, L666, L674）：
     - 在 `questions` 表增加 `slated_drill_date DATE` 与 `slated_ exposure_count INTEGER NOT NULL DEFAULT 0`；
     - 当日首次调用 `get_daily_blind_drill_queue(active_date)` 时，若当日已锁定满 `limit` 道（`slated_drill_date == active_date`）则直接幂等返回；否则按 `blind_test_unlock_at ASC` 锁定前 `limit` 道题更新 `slated_drill_date = active_date`、递增 `slated_exposure_count`，且仅对 `last_deferred_date != active_date` 的溢出到期题执行一次单日顺延；
     - `apply_overdue_escrow_decay` 对满足 `slated_exposure_count > 3`（已进入 3 个活跃日题单仍未完成盲测）的题目执行 `decay_factor` 每日 `-0.15` 衰减。

---

### CR-7 [Major]: 补齐 `scaffold_engine.py` 分支处理、盲测窗口双轨规则、REST DTO 与 LLM/VLM 网关配置
* **Source Findings**: `IC-3`, `DC-4`, `COMP-6`, `IZO-4`, `IR-8`, `CD-5`, `CD-7`, `CD-8`
* **Affected Lines**: [L381-L386](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L381-L386), [L400-L421](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L400-L421), [L467-L478](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L467-L478), [L566](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L566), [L582-L587](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L582-L587), [L637-L642](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L637-L642), [L671-L676](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L671-L676)
* **Problem Description**:
  1. `confirm_diagnostic_gate`（L637-L640）遗漏了 `ALREADY_MASTERED` 分支与空草稿（`last_valid_step_index == 0`）跳过保底分结算的判断，且在 `TRAP_OR_CALC_ERROR` 分支下未触发或等待 `SHARED_TRAP` 变式题预热。
  2. FeatureRange 正文（L409-L421）未写明粗心题 2～4 天 vs. 概念题 3～7 天的双轨盲测窗口（仅在 L178 注释与 L586 出现），且未说明盲测回退原题时 `BlindDrillItemDTO` 需携带 `probe_question_md` 及如何构造非会话态的 `idempotency_key`。
  3. `pyproject.toml`（L566）将客户端写成 `httpx / openai / google-genai` 三选一未决，且 `backend/config.py`（L582-L586）缺少模型网关配置项。
* **Actionable Resolution**:
  1. 在 `confirm_diagnostic_gate`（L637-L642）中补齐：
     - 若 `confirmed_category == 'ALREADY_MASTERED'`：调用 `elo_engine.record_mastered_init()`，置 `questions.mastery_state = 'MASTERED'`、`micro_stage = 'SESSION_COMPLETED'`；
     - 仅当 `confirmed_category == 'CONCEPT_BOTTLENECK'` 且 `last_valid_step_index >= 1` 时调用 `apply_draft_subgoal_floor()`；
     - 对 `CONCEPT_BOTTLENECK` 与 `TRAP_OR_CALC_ERROR` 均启动 `prewarm_session_questions`（维护进程内 `in_flight_prewarm_tasks: dict[str, asyncio.Task]` 单飞字典，若用户在预热完成前请求二阶微母题或四阶变式题，则 `await` 在途 Task）；
     - 拆拐杖通过升入 `PENDING_RETEST` 时，按 `error_category` 分别应用 `BLIND_WINDOW_CONCEPT_DAYS = (3, 7)` 或 `BLIND_WINDOW_TRAP_DAYS = (2, 4)` 设置 `blind_test_unlock_at / blind_test_due_at`（同步在 L410/L418 正文中写明）。
  2. 在 L421 与 L674-L675 明确：盲测优先抽取本地未做过的 `CLONE / ISOMORPHIC` 关联题，否则回退抽取原题并在 `BlindDrillItemDTO` 中携带最近一次会话卡点步骤的 `probe_question_md`（校验输入与 `step_formula_latex` 等价）；盲测与按日衰减的账本幂等键 `session_id` 段分别采用 `drill_{active_date}` 与 `decay_{active_date}`。
  3. 在 L566 与 L582-L586 明确锁定使用 `httpx` 调用兼容 OpenAI Chat Completions JSON Schema 的大模型接口，并在 `Settings` 中补充 `LLM_API_KEY: str = ""`, `LLM_BASE_URL: str = "https://api.openai.com/v1"`, `VLM_MODEL_NAME: str = "gpt-4o"`, `SOLVER_LLM_MODEL_NAME: str = "gpt-4o-mini"`, `LLM_TEMPERATURE: float = 0.1`, `USE_MOCK_LLM: bool = False`。

---

### CR-8 [Major]: 在 `Detailed Implementation Plan` 中补充分阶段实施划分与阶段验证门禁（Phasing & Checkpoints）
* **Source Findings**: `IR-5`
* **Affected Lines**: [L556-L737](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L556-L737)
* **Problem Description**:
  对照 [implementation-readiness-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/implementation-readiness-standard.md#L20-L23) 的 **Phasing & Checkpoints** 要求，`Detailed Implementation Plan` 目前仅平铺了 18 个文件，缺少逻辑递进的分阶段划分（Phases）与每个阶段可独立构建/测试的 Checkpoint 命令。
* **Actionable Resolution**:
  在 `## Detailed Implementation Plan` 开头增设 `### Implementation Phases & Incremental Checkpoints` 表格，将全部目标文件划分为 4 个增量阶段：
  - **Phase 1（存储、CAS 与四维难度基座）**：文件 `#1`～`#6` + `#15`（前半），Checkpoint：`pytest tests/test_difficulty_and_elo.py -k "rubric or clamp"`；
  - **Phase 2（受限 SymPy 沙盒、VLM 诊断与三阶预热脚手架）**：文件 `#7`～`#9` + `#16`，Checkpoint：`pytest tests/test_sympy_sandbox.py -v`；
  - **Phase 3（三层并查集知识网、双轨托管 Elo 与每日盲测调度）**：文件 `#10`～`#12` + `#15`（全量）+ `#17`，Checkpoint：`pytest tests/test_difficulty_and_elo.py tests/test_knowledge_graph_dsu.py -v`；
  - **Phase 4（FastAPI 路由、本地 Web 工作台与全链路 E2E 验收）**：文件 `#13`～`#14` + `#18`，Checkpoint：`pytest -v --cov=backend --cov-fail-under=85 && ruff check backend/ tests/ && mypy backend/`。

---

### CR-9 [Major]: 增设独立的 `## Risks & Mitigations` 章节并将 `Additional Resources` / `Future Improvements` 提升为二级标题（H2）
* **Source Findings**: `ST-1`, `ST-2`
* **Affected Lines**: [L529-L555](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L529-L555)
* **Problem Description**:
  1. 对照 [feature-spec-standard.md](file:///Users/wanghaichen/.gemini/config/plugins/egm/resources/document_standards/feature-spec-standard.md#L23-L39)，文档缺少显式的 `## Risks & Mitigations` 一级章节来集中声明系统边界、潜在故障模式与缓解策略。
  2. L544 `### Additional Resources` 与 L549 `### Future Improvements` 位于顶层分割线之后却误用了 H3，在文档 AST 中被错误归入 `## Non-Functional Requirements` 下。
* **Actionable Resolution**:
  1. 在 `## Non-Functional Requirements` 之后新增独立的二级标题章节 `## Risks & Mitigations`，以结构化表格列出：非纯代数/几何作图题无法被 SymPy 覆盖的边界、macOS/Linux 子进程资源限制平台差异、SQLite WAL 单写锁在异步预热与 Compaction 并发写入下的繁忙超时（`busy_timeout = 5000ms`）、以及外部 VLM/LLM 接口超时或离线降级策略。
  2. 将 `### Additional Resources` 与 `### Future Improvements` 升级为二级标题 `## Additional Resources` 与 `## Future Improvements`。

---

### CR-10 [Major]: 校准绝对化能力声明、统一“步骤对齐微确认条”命名并消除“物理级”术语误用
* **Source Findings**: `SY1-1`, `SY1-2`, `SY1-3`, `SY1-5`, `SY2-1`, `SY2-3`, `SY2-4`
* **Affected Lines**: [L13-L14](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L13-L14), [L21-L31](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L21-L31), [L54](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L54), [L83](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L83), [L97](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L97), [L109](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L109), [L255](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L255), [L289](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L289), [L356-L374](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L356-L374), [L391](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L391), [L421-L429](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L421-L429), [L485-L539](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L485-L539), [L641-L728](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L641-L728)
* **Problem Description**:
  1. 多处使用未校准的绝对化表述（如“消除幻觉”、“全程零数据丢失”、“永不死锁”、“100% 算得通”、“零等待感知”、“100%拦截”、“100% 恢复”）。
  2. 组件名称“1 秒步骤对齐确认条”与 G1 中 `< 3 秒` 的延迟目标字面冲突。
  3. 滥用“物理 / 物理级”修饰前端 UI 槽位、SQL `CHECK` 约束和接口字段剥离。
* **Actionable Resolution**:
  1. 将绝对化声明统一校准为客观可验证表述（“抑制实时命题幻觉”、“避免主流程因沙盒超时而同步阻塞”、“思考窗口异步预热与低延迟切换”、“精确恢复至合并前快照状态”）。
  2. 将组件名称统一为“**步骤对齐微确认条（Step-Skeleton Diff Gate）**”，在交互指标处保留 `< 3s`。
  3. 将 L255、L359、L421、L429、L674 中的“物理级拦截 / 物理隔离 / 物理剥离 / 物理禁止”分别替换为“数据库约束级拦截 / 独立槽位隔离 / 接口与视图层剥离 / 强制禁止”，并清理口语化修饰词（“咬合”、“捅破窗户纸”、“绝不”）。

---

## 🟢 Nits (Editorial, Formatting & Minor Polish)

### CR-11 [Nit]: 补全 Mermaid 状态图中的 `STAGE_0` 初始入口、移除 `SESSION_COMPLETED` 漏项及定义盲测失败重训起点
* **Source Findings**: `ST-3`, `DC-7`
* **Affected Lines**: [L396-L412](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L396-L412)
* **Actionable Resolution**:
  在 `stateDiagram-v2` 的 `state ACTIVE { ... }` 内部补充 `[*] --> STAGE_0_CONFIRM_GATE` 与终态 `SESSION_COMPLETED`，并在 3.3 节注明：当盲测失败退回 `🔴ACTIVE` 时，新创建的重训会话若在盲测中连“一阶中间子目标”也答错则从 `STAGE_1_BLIND_PROBE` 启动，若中间量正确仅最终结果算错则直接从 `STAGE_4_DECRUTCH_VARIANT` 启动。

---

### CR-12 [Nit]: 移除 `Alternatives Considered` 中的 `Rejected by author:` 前缀，统一分割线、章节锚点与 KaTeX 上标格式
* **Source Findings**: `ST-4`, `ST-5`
* **Affected Lines**: [L46](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L46), [L149](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L149), [L209](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L209), [L352](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L352), [L437](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L437), [L465](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L465), [L487-L525](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L487-L525), [L551](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L551)
* **Actionable Resolution**:
  1. 移除 L487-L525 共 7 处 `Rejected by author:` 前缀，直接陈述技术取舍；
  2. 将 L437 KaTeX 公式中的 Emoji 上标 `\text{🟢}`、`\text{🟡}` 改为 `\text{mastered}`、`\text{pending}`；
  3. 将 L209 的《理论基础.md》改为带锚点的 Markdown 链接，并统一全文分割线风格。

---

### CR-13 [Nit]: 明确 Compaction 自动触发条件与前端 `cas://` 图片 URI 渲染重写规则
* **Source Findings**: `IR-9`, `IR-10`
* **Affected Lines**: [L650-L651](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L650-L651), [L682](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L682), [L691](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L691)
* **Actionable Resolution**:
  在 L650-L651 说明当 `match_or_create_l3_node` 新建节点使同一 `(l1, l2)` 下未归并活跃节点增量达到 `COMPACTION_BATCH_THRESHOLD = 15` 时自动触发 `run_scoped_compaction(l1, l2)`；在 L691 说明前端渲染 Markdown 时将 `cas://([a-f0-9]{64})` 重写为 `/assets/$1`。

---

### CR-14 [Nit]: 统一目标文件清单为仓库根相对路径，并将编译验证命令改为 `python3 -m compileall`
* **Source Findings**: `IR-6`, `CD-9`, `CD-10`
* **Affected Lines**: [L560-L723](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L560-L723), [L732](file:///Users/wanghaichen/0_work/刷题harness/docs/designs/adaptive_practice_harness.md#L732)
* **Actionable Resolution**:
  将 `Target Files to Create/Modify` 各条目标题中的路径显示文本统一为仓库根相对路径（如 `[pyproject.toml](../../pyproject.toml)`、`[backend/config.py](../../backend/config.py)`），并将 L732 的 `python3 -m py_compile backend/**/*.py tests/*.py` 替换为跨 Shell 兼容的 `python3 -m compileall -q backend tests`。

---

## Traceability Matrix (Raw Findings $\to$ Consolidated Action Items)

| Stage | Report File | Reviewer | Raw Finding ID | Severity | Mapped Consolidated ID |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-zoom-out` | `ZO-1` | Major | `CR-6` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-zoom-out` | `ZO-2` | Major | `CR-6` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-zoom-out` | `ZO-3` | Major | `CR-1` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-zoom-out` | `ZO-4` | Major | `CR-2` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-zoom-out` | `ZO-5` | Major | `CR-5` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-zoom-out` | `ZO-6` | Nit | `CR-4`, `CR-5` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-inventive-catalyst` | `IC-1` | Major | `CR-5` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-inventive-catalyst` | `IC-2` | Major | `CR-1` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-inventive-catalyst` | `IC-3` | Major | `CR-7` |
| Stage 5 | [zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/zoom_out_report.md) | `egm-inventive-catalyst` | `IC-4` | Nit | `CR-3` |
| Stage 6 | [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md) | `egm-comprehension` | `COMP-1` | Blocker | `CR-1` |
| Stage 6 | [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md) | `egm-comprehension` | `COMP-2` | Blocker | `CR-5` |
| Stage 6 | [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md) | `egm-comprehension` | `COMP-3` | Major | `CR-2` |
| Stage 6 | [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md) | `egm-comprehension` | `COMP-4` | Major | `CR-6` |
| Stage 6 | [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md) | `egm-comprehension` | `COMP-5` | Major | `CR-3` |
| Stage 6 | [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md) | `egm-comprehension` | `COMP-6` | Major | `CR-1`, `CR-7` |
| Stage 6 | [comprehension_report.md](file:///Users/wanghaichen/0_work/刷题harness/comprehension_report.md) | `egm-comprehension` | `COMP-7` | Nit | `CR-4` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-design` | `DC-1` | Blocker | `CR-1` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-design` | `DC-2` | Blocker | `CR-2` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-design` | `DC-3` | Blocker | `CR-6` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-design` | `DC-4` | Major | `CR-7` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-design` | `DC-5` | Major | `CR-3` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-design` | `DC-6` | Major | `CR-5` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-design` | `DC-7` | Nit | `CR-1`, `CR-11` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-structural` | `ST-1` | Major | `CR-9` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-structural` | `ST-2` | Major | `CR-9` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-structural` | `ST-3` | Nit | `CR-11` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-structural` | `ST-4` | Nit | `CR-12` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-structural` | `ST-5` | Nit | `CR-12` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-style` | `SY1-1` | Major | `CR-10` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-style` | `SY1-2` | Major | `CR-10` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-style` | `SY1-3` | Major | `CR-10` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-style` | `SY1-4` | Major | `CR-4` |
| Stage 7 | [critic_report.md](file:///Users/wanghaichen/0_work/刷题harness/critic_report.md) | `egm-style` | `SY1-5` | Nit | `CR-10` |
| Stage 8 | [intermediate_zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/intermediate_zoom_out_report.md) | `egm-zoom-out` | `IZO-1` | Blocker | `CR-1` |
| Stage 8 | [intermediate_zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/intermediate_zoom_out_report.md) | `egm-zoom-out` | `IZO-2` | Blocker | `CR-2` |
| Stage 8 | [intermediate_zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/intermediate_zoom_out_report.md) | `egm-zoom-out` | `IZO-3` | Major | `CR-6` |
| Stage 8 | [intermediate_zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/intermediate_zoom_out_report.md) | `egm-zoom-out` | `IZO-4` | Major | `CR-6`, `CR-7` |
| Stage 8 | [intermediate_zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/intermediate_zoom_out_report.md) | `egm-zoom-out` | `IZO-5` | Major | `CR-5` |
| Stage 8 | [intermediate_zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/intermediate_zoom_out_report.md) | `egm-zoom-out` | `IZO-6` | Major | `CR-3` |
| Stage 8 | [intermediate_zoom_out_report.md](file:///Users/wanghaichen/0_work/刷题harness/intermediate_zoom_out_report.md) | `egm-zoom-out` | `IZO-7` | Nit | `CR-4`, `CR-7` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-1` | Blocker | `CR-2` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-2` | Blocker | `CR-2` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-3` | Blocker | `CR-1` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-4` | Blocker | `CR-3` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-5` | Major | `CR-8` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-6` | Major | `CR-4`, `CR-14` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-7` | Major | `CR-4`, `CR-5` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-8` | Major | `CR-7` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-9` | Nit | `CR-13` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-implementation-readiness` | `IR-10` | Nit | `CR-13` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-1` | Blocker | `CR-1` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-2` | Blocker | `CR-2` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-3` | Blocker | `CR-3` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-4` | Blocker | `CR-4` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-5` | Major | `CR-7` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-6` | Major | `CR-5` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-7` | Major | `CR-6`, `CR-7` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-8` | Major | `CR-7` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-9` | Nit | `CR-14` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-codebase-details` | `CD-10` | Nit | `CR-14` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-style` | `SY2-1` | Major | `CR-10` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-style` | `SY2-2` | Major | `CR-3`, `CR-4` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-style` | `SY2-3` | Nit | `CR-10` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-style` | `SY2-4` | Nit | `CR-10` |
| Stage 9 | [implementation_readiness_report.md](file:///Users/wanghaichen/0_work/刷题harness/implementation_readiness_report.md) | `egm-style` | `SY2-5` | Nit | `CR-2` |
