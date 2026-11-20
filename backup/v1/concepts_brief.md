# 数理自适应刷题与验证框架（刷题 Harness）概念设计简报

* **Author:** nakseuksa
* **Date:** 2026-11-13
* **Reference Baseline:** [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md)

## Reviewer / Brainstorm Lens Status

| Brainstorm Lens | Role Title | Status |
| --- | --- | --- |
| `egm-design` | Hostile Design Critic Brainstormer | `completed` |
| `egm-codebase-details` | Codebase Alignment Brainstormer | `completed` |
| `egm-zoom-out` | Skeptical Architect Brainstormer | `completed` |
| `egm-comprehension` | UX & Ergonomics Brainstormer | `completed` |
| `egm-implementation-readiness` | Implementation Readiness Brainstormer | `completed` |
| `egm-structural` | Structural Validator Brainstormer | `completed` |
| `egm-inventive-catalyst` | Inventive Catalyst Brainstormer | `completed` |

---

## Overview

本项目构建一个面向**数学与物理学科**的本地 Web 自适应刷题与验证框架（刷题 Harness）。系统接收含 LaTeX 公式、题干本地配图及手写演算草稿截图的 Markdown 错题输入，在**零预设考纲（完全冷启动）**条件下运行，通过微观单题闭环与宏观全局导航双层架构消除“看懂解析但独立做不出”的假懂现象：

1. **原题与草稿多模态诊断入口**：解析题目 Markdown 与手写演算草稿截图，结合 [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md#L132-L270) 中的四维行为级 Rubric $[C, S, T, L]$ 自动评估客观难度 Elo（`800~2800` 分），并根据草稿卡点分流：已掌握题直接点亮节点并参与水位线结算；审题陷阱/计算粗心题生成 1 道同陷阱平行变式当场复测；思路断层/概念盲区题定位具体停滞步骤并切入降阶训练流。
2. **倒置防剧透降阶脚手架引擎 + SymPy 沙盒自校验**：拒绝在原题上直接喂给中间结论，依次执行：（1）一阶盲探子目标（隐去原题最终问题，仅追问卡点处的中间子问题）；（2）二阶脱壳微母题（若一阶失败，优先从个人题库检索前置子题 `SCAFFOLD_PRE`；冷启动缺失时回退至 LLM 实时生成换掉数字与外壳的 1 步同构微母题，并通过本地 Python/SymPy 沙盒验算后渲染）；（3）三阶无提示回原题贯通。
3. **拆拐杖验证与隔日盲测销账**：顺着脚手架解出原题后，当场追考 1 道改变数字或背景的同构变式题，一次做对则从 `🔴 攻坚中` 升级为 `🟡 待复测`；随后进入 3~7 天延迟盲测队列，无提示抽测一次做对后正式销账为 `🟢 已掌握`，做错则退回 `🔴 攻坚中`。
4. **三层路径锚定与并查集自收敛知识网**：无预设具体考纲，强制按固定深度三层语法路径 `[L1 学科模块] / [L2 核心实体与定理] / [L3 原子转化算子或考察属性]` 提取考点；新题入库时匹配 L1/L2 并拉取同父 L2 邻域下 L3 叶子节点由 AI 裁判比对复用、扩充 `aliases`；后台定期 Compaction 采用不可变主 ID + 并查集 `redirect_to_id` 重定向合并近义节点；维护跨知识点组合错题标红边，以及“版图攻克率”与“按 L1 模块独立统计 + 分布熵校准的知识网收敛度”双指标。
5. **分模块双轨制动态 Elo 评分系统**：按每个 L1 模块独立维护“覆盖分（借助脚手架做出的上限）”与“实战分（无提示盲测兑现的真实水位）”，包含三条防失真法则：（1）水位线过滤防刷简单题膨胀；（2）基于子问题 $[C_{\text{sub}}, S_{\text{sub}}, T_{\text{sub}}, L_{\text{sub}}]$ 的确定性向下兼容推演保底实战分；（3）两步销账兑现与超时衰减。

---

## Analysis

7 个独立视角的 Goldfish 评审对上述概念进行了零上下文交叉审计，去重后识别出 **6 类核心架构矛盾与工程缺陷**，并给出对应的TRIZ消解方案与系统不变量：

### 1. 动态命题的数理严密性 vs. SymPy 沙盒阻塞、伪校验与学科边界（`egm-design` / `egm-codebase-details` / `egm-zoom-out` / `egm-inventive-catalyst`）

* **暴露的核心缺陷**：
  1. **自证预言伪校验（Vacuous / Tautological Check）**：同一个 LLM 同时生成微母题与 SymPy 脚本时，常写出硬编码 `assert ans == ans` 或未调用符号求解器的假脚本，沙盒返回 `exit_code = 0`，但题干本身条件矛盾或无解。
  2. **符号死循环与阻塞延迟**：含参根式、超越方程或非初等函数极易导致 `sympy.solve()` CPU 挂起；若同步等待 3~5 轮重试，用户将在卡壳瞬间面对 30~90 秒空白等待。
  3. **非代数题型盲区**：立体几何辅助线构造、物理受力分析配图或电磁场定性判断无法完全转化为纯 SymPy 代数方程求解，若把 SymPy 通过作为唯一渲染硬门禁，此类题目将永久卡死。
* **突破性消解方案（TRIZ 预先作用 + 反向作用 + 变害为利）**：
  1. **思考窗口投机预生成（Speculative Pre-warming）**：在模块 1 完成草稿诊断、用户正在阅读和思考“一阶盲探子目标”的天然时间窗口内，后台异步并发启动该卡点的“二阶微母题”与“模块 3 拆拐杖变式题”的生成与沙盒校验，将前端感知的生成等待压缩至接近 0 秒。
  2. **参数化内核先行 + AST 静态探针 + 硬超时降级**：LLM 仅输出参数化方程与求解函数，沙盒执行前先经 Python AST 检查确认包含真实符号调用（`solve`/`simplify`/`Eq`）且无 I/O 网络调用；子进程设 `timeout <= 5s`、`max_retries = 2`。对于非纯代数题型或重试耗尽场景，优雅降级为“双盲 LLM 交叉求解校验（Generator-Solver Self-Consistency）”并附带 `⚠️ 未经符号沙盒强校验（支持一键报错跳过）` 徽章，严禁卡死刷题主流程。
  3. **合格合成题回流暖库**：通过校验的 AI 衍生微母题与变式题标记 `origin_type = 'AI_SYNTHETIC'` 持久化入库（排除在版图攻克率分母外，但可供后续同考点检索复用），并禁止以 AI 衍生题为母题再次二阶衍生。

### 2. 多模态手写草稿诊断的视觉幻觉、“一题多解”误判与输入摩擦（`egm-comprehension` / `egm-design` / `egm-codebase-details` / `egm-inventive-catalyst`）

* **暴露的核心缺陷**：
  1. **题干配图与手写草稿混淆及多轮 Token 爆炸**：Markdown 中题干图与草稿图若无显式槽位隔离，VLM 易将草稿上的错误辅助线当成已知条件；若后续多轮脚手架对话持续携带高分辨率原图，Token 消耗将膨胀 5~10 倍。
  2. **一题多解（Disjunctive OR Path）与笔误误判**：学生可能采用建系代数法而非标准答案的纯几何法并在中途卡壳（见 [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md#L530-L536) Q-Matrix 析取门），或仅因抄错正负号导致后续算不出。若 VLM 黑盒一步直接切入三阶脚手架并结算保底分，会导致错误分流与算分失真。
  3. **空草稿与判题交互未定义**：用户标记“未掌握”但一笔未动（无草稿）时如何分流？在三阶脚手架与盲测中，用户如何提交答案（手敲复杂 LaTeX 极易因格式差异误判）？
* **突破性消解方案（TRIZ 抽取与中介物）**：
  1. **分区剪贴板录入与内容寻址资产库（CAS）**：Web 端拆分为 `[题干区（支持粘贴截图自动 OCR 转 Markdown+LaTeX）]` 与 `[手写草稿粘贴区]`，自动注入幂等 `question_uid` Frontmatter，并将图片压缩转存至按 SHA-256 命名的本地受管资产库（防止外部移动断链）。
  2. **一次性视觉转录（Vision-to-LaTeX Once）+ 步骤对齐确认条（1-Click Diagnostic Gate）**：首轮 VLM 将手写草稿转录为有序公式数组 `draft_steps_latex` 并与标准步骤（或识别出的合法替代解法分支）对齐，后续会话仅传递文本；在切入训练流前展示单行“步骤对齐确认卡”（如：`Step 1 ✅ 已完成 -> Step 2 ❓ 思路中断 [一键切换为计算粗心]`），用户确认后再触发脚手架与保底分结算；若无草稿则默认从 Step 1 启动一阶盲探。
  3. **混合判题契约与半程立式拆拐杖**：作答支持“最终数值/代数式 SymPy 等价判题 + 关键步骤展开自评 / 拍照复核”；当原题运算负荷 $L \ge 2$ 时，模块 3 当场拆拐杖变式仅考核“核心方程组列式”，将完整体力运算留给 3~7 天盲测。

### 3. 零考纲三层知识网的 L1/L2 早期漂移、并查集状态撕裂与伪收敛（`egm-zoom-out` / `egm-structural` / `egm-implementation-readiness` / `egm-codebase-details`）

* **暴露的核心缺陷**：
  1. **L1/L2 顶层发散导致邻域比对失效**：完全冷启动下，若不同批次生成 `L1: 高中物理` vs `L1: 经典力学`，或 `L2: 动能定理` vs `L2: 功能关系`，同义 L3 叶子节点将被隔离在不同父节点下，导致“同父 L2 邻域拉取”无法触发，分模块 Elo 也会碎片化。
  2. **热门 L2 邻域膨胀与并查集跨模块合并撕裂**：当热门 L2 下积压上百个 L3 节点时全量注入 Prompt 会导致注意力退化；后台 Compaction 若将处于不同掌握状态（如 `🟢已掌握` 与 `🔴攻坚中`）或跨 L1 的节点合并，会导致盲测队列悬空、标红边出现自环（Self-loop）、历史 Elo 账本断裂且无法回滚。
  3. **冷启动分布熵除零与无分母攻克率悖论**：当某 L1 模块下活跃 L3 节点数 $N_m \le 1$ 时，归一化香农熵 $-\sum p_i \ln p_i / \ln N_m$ 触发除零 `NaN`。
* **工程收敛与治理方案**：
  1. **L1/L2 全量目录注入 + L1 顶层半受控锚定**：L1（5~10 个学科大类）在建库时提供默认轻量模板并允许用户确认；每次新题提取路径时，Prompt 全量注入当前已有的 `L1/L2` 骨架目录树（仅数百 Token），强制优先挂载既有 `L1/L2`；当同一 L2 下活跃 L3 节点 $> 25$ 个时，先经本地 BM25/Embedding 预筛 Top-15 再送入 AI 裁判。
  2. **同域并查集硬边界 + 悲观状态半格 + 可逆审计表（`compaction_audit_ledger`）**：强制限定 Compaction 仅允许在同一 `L1/L2` 父路径下合并 L3 叶子节点（严禁跨 L1 重定向）；状态合并遵循悲观优先律（`🔴攻坚中 > 🟡待复测 > 🟢已掌握`）；自动清除合并产生的标红边自环并归并平行边计数；记录批次快照以支持一键解绑回滚（Un-merge）。
  3. **分布熵安全边界**：当 $N_m < 3$ 时收敛度校准因子固定返回 `0.0`（标记 `COLD_START_INSUFFICIENT_NODES`），结合近 20 题滑动窗口节点复用率 $\rho_m$ 合成模块收敛度。

### 4. 双轨 Elo 分数倒挂、多阶段重复记账与《理论基础.md》基线断层（`egm-codebase-details` / `egm-design` / `egm-structural`）

* **暴露的核心缺陷**：
  1. **与 [理论基础.md](file:///Users/wanghaichen/0_work/刷题harness/理论基础.md#L189-L218) 的值域及 Schema 断层**：原文档公式 `800 + 95*(2^C) + 110*S + 120*T + 70*L` 的真实值域为 `[1290, 3220]`（第 90 行表格误算为 `2720`），无法覆盖 `800~1289`；且原文档要求必须绑定 `target_grade_and_subject`（学段基准），关系枚举存在 `SCAFFOLD_PRE`、`SCAFFOLDING_SUBSTEP`、`SCAFFOLD_SUBSTEP` 三套混用命名。
  2. **子步保底分倒挂奇点**：若 LLM 对草稿已完成子步评估的 `[C_sub, S_sub, T_sub, L_sub]` 未做单调性限制，可能出现“做错题的局部保底分 $\ge$ 原题总难度分”。
  3. **单题四次状态流转的 Elo 重复累加（Double Counting）与超时惩罚劝退**：一道错题先后经历“草稿保底 $\to$ 脚手架通关 $\to$ 拆拐杖变式 $\to$ 隔日盲测”，若每次都按独立对局加分，会导致“先做错再走脚手架”涨分高于“一次独立做对”；且自然日静默扣减实战分会制造强烈挫败感。
* **确定性数学与账本规范**：
  1. **校准全量程公式与单调性硬锁**：重校准基础公式使其严格覆盖 `800~2800`，保留 $C \le 2$ 封顶 `L4-A` 的硬性熔断；代码层对子步骤强制执行单调性裁剪：$C_{\text{sub}} \le \max(1, C-1),\ S_{\text{sub}} < S,\ T_{\text{sub}} \le T,\ L_{\text{sub}} \le L$，且 $D_{\text{sub}} \le D_{\text{orig}} - 150$。
  2. **单题幂等托管账本（Idempotent Escrow Ledger）与事件溯源**：建立不可变事件流 `elo_mutation_ledger`。录入错题仅结算 $D_{\text{sub}}$ 保底分；通过当场拆拐杖升入 `🟡待复测` 时，按 $D_{\text{orig}}$ 更新覆盖分，并将完整通关应得实战分与已发保底分的差额 $\Delta \text{Elo}_{\text{escrow}}$ 存入托管池；3~7 天盲测通过升入 `🟢已掌握` 时解冻 $\Delta \text{Elo}_{\text{escrow}}$ 合入实战分；超时衰减仅回吐 `🟡待复测` 托管池中的未兑现差额（显示为“待擦亮 Rusty 状态”），绝不倒扣已掌握基线。

### 5. 隔日盲测的“被动混入饥饿”与“表皮记忆泄题”（`egm-comprehension` / `egm-design` / `egm-zoom-out`）

* **暴露的核心缺陷**：若用户停止录入新题，被动混入的 `🟡待复测` 队列将永远无法触发；若盲测直接端出原题且只核对最终数字，用户极易凭 3 天前的短期图像记忆直接背出答案（如 $2\sqrt{3}$），造成假销账。
* **设计修正**：提供Web端独立的 `🎯 每日综合盲测（Due Drill）` 入口并设每日配额上限（防复习债堆积，系统顺延期间冻结衰减计时器）；盲测优先抽取同构换数变式（`CLONE / ISOMORPHIC`），若回退至原题盲测，强制要求同时填写模块 2 曾卡壳的“一阶子目标中间量”与最终答案，且物理剥离题源与考点标签以防剧透。

### 6. Ideal Final Result（理想最终解）

用户在本地粘贴原题与草稿截图后，1 秒内确认步骤卡点，在思考一阶盲探问题的同时，后台已完成二阶微母题与拆拐杖变式的 SymPy 沙盒验算与暖库沉淀；所有知识图谱并查集演进、跨考点标红边与分模块双轨 Elo 水位，均作为不可变做题事件流在最新拓扑上的确定性投影自动保持自洽。

---

## Goals vs. Non-Goals

* **In-Scope Goals:**
  1. **多模态错题导入与两级结构化诊断**：支持题干/配图与手写草稿分区录入、幂等 `question_uid` Frontmatter 与 SHA-256 内容寻址资产归档；绑定学段基准输出题级与步骤级 `[C, S, T, L]`，通过轻量步骤确认卡分流三路训练。
  2. **倒置防剧透三阶脚手架 + 受限 SymPy 沙盒预热流水线**：实现“一阶条件遮罩盲探 $\to$ 二阶脱壳微母题（题库 `SCAFFOLD_PRE` 优先 + 思考窗口后台异步 LLM/SymPy 生成兜底 + AST 防伪校验 + 超时优雅降级）$\to$ 三阶回原题贯通”。
  3. **负荷分级拆拐杖与防背题隔日盲测闭环**：实现宏观掌握状态机（`🔴ACTIVE` / `🟡PENDING_RETEST` / `🟢MASTERED`）与微观训练会话状态机正交解耦；支持 $L \ge 2$ 半程立式验证、独立每日盲测入口、配额熔断及原题中间步防背答案核验。
  4. **L1/L2 目录锚定 + 同域并查集自收敛知识网**：强制 `[L1]/[L2]/[L3]` 三层路径，注入全局 `L1/L2` 树防顶层漂移，同父 L2 下 Top-K 预筛交 AI 裁判复用；实现同父 L2 作用域内的可逆并查集 Compaction（含 `compaction_audit_ledger`、自环清理、悲观状态合并）与双指标面板。
  5. **事件溯源的分模块双轨动态 Elo 账本**：按 L1 模块独立维护覆盖分与实战分，通过 `elo_mutation_ledger` 与单题托管池（Escrow）实现水位线过滤、单调向下兼容保底推演与未兑现差额衰减。
* **Out-of-Scope Non-Goals:**
  1. **不内置全网静态爬虫题库或绑定特定教材章节目录**。
  2. **不构建多租户云端 SaaS 或复杂权限系统**（纯本地 Web + SQLite + 本地文件资产库）。
  3. **不在 Web 端自研实时手写笔迹画板**（采用线下纸笔/平板演算后截图粘贴工作流）。
  4. **不引入《理论基础.md》第五、六部分的文科语篇修辞（RST）拓扑**（仅保留数理六大易错陷阱算子与可选 AnkiConnect 导出接口）。

---

## Alternatives Weighed

1. **微母题与变式题校验方案：同步阻塞式后置 SymPy 校验 / 纯双盲 LLM 无沙盒校验 vs. 思考窗口异步预热 + 参数化内核先行 + 双盲交叉降级（选定）**
   * *Rejected by author (同步阻塞式后置 SymPy 校验):* 当用户在卡点等待时同步执行多轮生成与符号求解，遇到复杂物理/几何题会出现 30~90 秒卡顿或死循环，且同一 Prompt 易写出恒真伪断言（`egm-design`, `egm-codebase-details`）。
   * *Rejected by author (完全砍掉本地 SymPy 沙盒，仅用双盲 LLM 交叉核对):* 虽能降低沙盒维护复杂度（`egm-zoom-out`），但两路 LLM 对同一代数运算或隐含边界仍可能产生共谋性计算失误；采用“思考窗口异步预热 + AST 防伪 + SymPy 沙盒 5s 超时降级至双盲 LLM”可同时兼顾数理确定性与零阻塞体验。
2. **知识网组织方案：L1/L2 完全自由生成 + 跨模块并查集 / 静态 YAML 预设 L1/L2 + 原地 UPDATE vs. L1/L2 全局骨架注入 + 同父 L2 作用域可逆并查集（选定）**
   * *Rejected by author (L1/L2 完全无约束自由生成与跨模块并查集合并):* 早期题量少时 L1/L2 极易发生同义分裂（如“力学”与“牛顿力学”），导致同父 L3 邻域比对失效；跨 L1 合并则会直接撕裂分模块双轨 Elo 的历史归属账本（`egm-structural`, `egm-implementation-readiness`）。
   * *Rejected by author (硬编码静态 YAML 预设全部 L1/L2 并用 SQLite 原地 UPDATE 删除被合并节点):* 违背零预设考纲冷启动的核心设计初衷，且原地物理覆写外键无法在 AI 发生误合并时一键回滚（Un-merge）；采用“入库注入现有 L1/L2 骨架树 + 同父 L2 作用域内 `redirect_to_id` 可逆并查集 + `compaction_audit_ledger`”既保留冷启动自生长能力，又保障账本与回滚安全。
3. **Elo 计分存储方案：模块表原地累加浮点数 / 砍掉动态双轨 Elo 改用静态分档计数 vs. 单题幂等托管池（Escrow）+ 不可变事件溯源账本（`elo_mutation_ledger`）（选定）**
   * *Rejected by author (状态流转时直接在模块表上做原地 Elo 增减):* 单题经历草稿保底、脚手架通关、拆拐杖、隔日盲测 4 个阶段，原地累加会导致重复记账通胀，且调整算分公式或回滚节点合并时无法重放还原（`egm-design`, `egm-structural`）。
   * *Rejected by author (砍掉学生动态双轨 Elo 与子步保底推演，仅统计静态分档题数):* 静态计数无法量化反映“高难错题独立完成前几步”体现出的真实能力底线，也无法直观呈现覆盖分与实战分之间的“假懂水分区”；通过单题托管池（Escrow）与单调性硬锁可低成本消除重复记账与倒挂风险。
4. **草稿诊断路由方案：VLM 端到端黑盒直接跳转训练流 vs. 一次性 LaTeX 转录 + 1秒步骤对齐微确认条（Step-Skeleton Diff Intermediary）（选定）**
   * *Rejected by author (VLM 黑盒直跳):* 手写草稿涂改与“一题多解”极易被 VLM 误判，一旦将笔误判为概念断层（或反之），不仅浪费用户 20 分钟做无效降阶题，还会将错误的 `[C_sub, S_sub, T_sub, L_sub]` 写入实战分保底账本（`egm-comprehension`, `egm-inventive-catalyst`）。

---

## Key Decisions & Staging for Next Steps

### 核心架构契约（进入 `/egm-draft` 的锁定约束）

1. **统一与《理论基础.md》对齐的底层枚举与校准公式**：
   * 固定五种题目拓扑枚举：`CLONE`、`ISOMORPHIC`、`SCAFFOLD_PRE`、`CONTRASTIVE`、`SHARED_TRAP`；题目表区分 `origin_type`（`USER_CANONICAL` vs `AI_SYNTHETIC`）。
   * 绑定 `target_grade_and_subject` 学段基线，采用覆盖 `800~2800` 的校准非线性公式并强制执行子步骤单调性锁：$C_{\text{sub}} \le \max(1, C-1),\ S_{\text{sub}} < S,\ T_{\text{sub}} \le T,\ L_{\text{sub}} \le L$ 且 $D_{\text{sub}} \le D_{\text{orig}} - 150$。
2. **双层正交状态机**：
   * 宏观题目生命周期状态机（`🔴ACTIVE` $\leftrightarrow$ `🟡PENDING_RETEST` $\to$ `🟢MASTERED`）与微观脚手架会话状态机（支持断点续传与次日推迟拆拐杖）物理分离；审题陷阱/计算粗心题当场复测通过后统一进入 `🟡PENDING_RETEST`（短窗口 2~4 天防坑盲测）。
3. **沙盒安全与可观测性红线**：
   * SymPy 子进程强制 `timeout <= 5s`、内存限额、AST 符号求解器调用检查（拦截假脚本）与 `max_retries = 2` 优雅降级；全链路落盘多模态转录对齐日志、沙盒执行 Span、并查集合并审计表与 Elo 算分托管流水。

### 推荐分阶段实施顺序（Staging）

1. **Phase 1 — 数据底座、CAS 资产库与多模态草稿诊断确认流**：SQLite 核心表与事件账本、分区剪贴板导入、VLM 一次性草稿转录、步骤级 `[C,S,T,L]` 拆解与 1 秒步骤对齐确认卡。
2. **Phase 2 — 倒置三阶脚手架与异步 SymPy 预热校验沙盒**：一阶条件遮罩盲探、思考窗口后台预生成二阶微母题与拆拐杖变式、AST+SymPy 子进程校验与优雅降级、合成题暖库回写。
3. **Phase 3 — 双层状态机、独立每日盲测入口与托管式双轨 Elo**：负荷分级拆拐杖验证、3~7 天盲测队列与配额熔断、防背题中间量校验、基于 `elo_mutation_ledger` 的分模块双轨算分与解释条。
4. **Phase 4 — 三层自收敛知识网、同域可逆并查集与双盘表看板**：全局 `L1/L2` 注入锚定、同父 L2 下 Top-K 召回复用、带 Undo 审计的并查集 Compaction、标红组合边与分布熵校准收敛度面板。
