# 刷题 Harness v2

面向命令行与 AI 编程助手（**AGY / Antigravity**、**`pi`** 及各类支持 Skill 或 MCP 的客户端）的 **Local-First 纯理科离线题库知识编译器与自适应刷题系统**。

本系统将本地磁盘上无标准答案、含几何/物理配图、标签杂乱的原始理科题库（`problems.jsonl + images/`），编译为经形式化验真、可算精细难度、具备五维教学关联拓扑的开放本地知识资产，并提供从动态能力画像、梯度组卷、防剧透辅导、双模式练习单打印到外部新错题诊断入库的完整闭环。

---

## 目录

- [一、理论基础、系统用途与能力边界](#一理论基础系统用途与能力边界)
- [二、环境安装与前置配置](#二环境安装与前置配置)
- [三、题库的编译方法](#三题库的编译方法)
  - [3.1 首次全量冷启动编译（`--mode api` 直连主线）](#31-首次全量冷启动编译--mode-api-直连主线)
  - [3.2 免推理 API Key 的 Subagent 双通道（`export / import`）](#32-免推理-api-key-的-subagent-双通道export--import)
  - [3.3 日常四类增量演进速查](#33-日常四类增量演进速查)
- [四、题库编译好后的使用方法](#四题库编译好后的使用方法)
- [五、全量 CLI 命令手册（51 个子命令）](#五全量-cli-命令手册51-个子命令)
  - [5.0 全局通用约定](#50-全局通用约定)
  - [5.1 环境、态势巡检、索引重建、Skill 与 MCP（6 个）](#51-环境态势巡检索引重建skill-与-mcp6-个)
  - [5.2 学段管理与原始题源入库（3 个）](#52-学段管理与原始题源入库3-个)
  - [5.3 提示词接缝、黄金难度锚点与零 DB 校验工具（7 个）](#53-提示词接缝黄金难度锚点与零-db-校验工具7-个)
  - [5.4 离线编译流水线、人工复核台与全库体检（5 个）](#54-离线编译流水线人工复核台与全库体检5-个)
  - [5.5 词表演进与知识图谱拓扑治理（9 个）](#55-词表演进与知识图谱拓扑治理9-个)
  - [5.6 学习者画像、多维检索、梯度组卷与防剧透刷题（13 个）](#56-学习者画像多维检索梯度组卷与防剧透刷题13-个)
  - [5.7 外部新错题接入、人工覆写、资产恢复与开放导出（8 个）](#57-外部新错题接入人工覆写资产恢复与开放导出8-个)
- [六、系统架构与权威文档索引](#六系统架构与权威文档索引)

---

## 一、理论基础、系统用途与能力边界

### 1.1 系统能做到什么

- **无答案带图题库离线验真编译**：原始题库目录全程保持严格只读、零拷贝冗余；自动执行配图视觉事实转译（`visual_facts`）、跨模型双盲交叉求解与 **SymPy 安全沙盒形式化验算**，分歧题自动进入极简本地键盘复核队列（`shuati review`）。
- **自底向上严密知识图谱构建**：不依赖原始教辅粗糙目录，从全库真实解题步骤自底向上聚类涌现并锁定学段标准目录树（`L1` 大模块 / `L2` 核心模型），归并同域原子转化算子（`L3`）且支持无损拆回，构建强无环先修依赖图（DAG），同时跨学段打通思维方法（`Method`）、经典母题（`Motif`）与数理认知陷阱（`Trap`）。
- **多角色隔离的动态能力画像与自适应组卷**：同一题库支持多个学习者独立维护做题足迹；实时重放各模块启发式能力水位、最近发展区（ZPD）、8 档通过率阶梯与薄弱雷达，一键按“稳固 : 拔高 : 排雷”配比组出专题小卷（自动执行孪生题冷却与避重）。
- **严格防剧透的苏格拉底式刷题与双模练习单**：默认 `quiz` 档位物理裁剪标准答案、解法步骤与剧透标签；按步释放 `hint` 引导思路，通过不泄密的 `shuati check` 逐小问判题；支持一键生成自包含单文件离线 HTML 练习单（屏幕浏览支持三级折叠防剧透，浏览器 `Ctrl+P` 打印自动留出作图演算空白区）。
- **外部新错题实时拍照诊断与资产持久化**：遇到题库外的全新错题（单题文本+照片或批量 Markdown 题单），可实时提取骨架定位知识卡点、匹配库内降阶铺垫题与同构变式题，并支持一键正式收入题库；所有人工覆写、学习者档案、做题记录与外部错题自动双写至 `human/*.jsonl` 文本快照，换机或重编永不丢失。

> **刻意不做的设计边界（Non-Goals）**：
> - **纯本地离线优先（Local-First）**：不构建常驻多页面重前端 Web 刷题后台、账号鉴权或云端多租户 SaaS；
> - **聚焦纯理科**：面向小学奥数及初高中数理化等纯理科体系，不涉及语文/英语等文科语篇修辞或词汇闪卡；
> - **零锚定污染**：离线解题、拆解与打标全过程物理拦截原始 JSONL 的 `chapter` 与旧 `tags`，杜绝大模型顺着章节标题猜思路。

### 1.2 核心理论基础概要

系统的难度定标与变式推荐建立在两套可工程化验证的教育测量与认知科学模型之上（完整数学公式、判定阈值与理论溯源详见 [`docs/理论基础.md`](docs/理论基础.md)）。

#### 1. 非对称四维行为难度模型（底层正交锚点 + 左粗右细 8 档）

为消除大模型主观打分漂移、防止“计算繁琐的简单题”混入竞赛压轴档，系统严禁模型直接给总分：模型必须先输出可观测的解题行为证据，再对 **4 个正交维度**打整数分，最后由确定性代码赋予思维深度 `C` 指数权重合成连续先验分（`base_elo`），映射为**左粗右细的非对称 8 档**：

| 四维锚点 / 8 档分层 | 取值 / 档位编码 | 核心区分逻辑与教学意义 |
| :--- | :--- | :--- |
| **`C` 概念与思维深度** | `1 ～ 4`（指数主权重） | 1=单模型直代；2=常规变形/双考点串联；3=跨模块综合/隐式建模；4=无中生有非标构造或高阶洞察 |
| **`S` 推理链路长度** | `1 ～ 3`（不含纯算术） | 1=1～2 逻辑步；2=3～4 步含中间子目标；3=不少于 5 步或独立多分支分类讨论 |
| **`T` 信息隐蔽度与陷阱** | `1 ～ 3` | 1=条件显式透明；2=含冗余干扰或图文隐含条件转译；3=强反直觉、极端临界边界或连环陷阱 |
| **`L` 运算与审题负荷** | `1 ～ 3` | 1=口算/短文本；2=常规纸笔多步推演；3=繁复代数化简、大数死算或高密度多图审题 |
| **基础～中档（粗颗粒）** | `L1_基础直达` / `L2_常规巩固` / `L3_综合中档` | 吸收低中分段微小主观波动，快速完成常规基础达标定位 |
| **进阶拔高（横向分流）** | `L4-A_进阶拔高(繁)` vs `L4-B_进阶拔高(巧)` | `C<=2` 最高封顶 `L4-A`，与重技巧转化的 `L4-B` 横向并列，精准诊断学习者究竟“怕繁”还是“怕巧” |
| **顶尖压轴（纵向高解析）** | `L5-1_压轴挑战` / `L5-2_竞赛超难` / `L5-3_殿堂极限` | 在高难度长尾区收窄带宽，精细分离课内压轴、竞赛选拔与极限构造题 |

#### 2. “剥皮抽骨”四通道与五边教学拓扑

每道题被解耦为**表皮情境**（题干与视觉读图描述）、**去情境抽象骨架**（抹除故事包装后的数理转化结构）、**原子算子与经典母题集合**（`L3 + Motif`）以及**数理陷阱集合**（`Trap`），离线织出 5 类教学关联边：

| 关联边类型 | 方向性与作用域 | 特征判据概要 | 刷题与教学联动用途 |
| :--- | :--- | :--- | :--- |
| **`CLONE`（孪生换数）** | 无向（同学段） | 高表皮相似 + 高骨架同构 + 小难度差 | 同题型即时巩固；组卷自动触发 14 天冷却避重 |
| **`ISOMORPHIC`（跨域同构）** | 无向（支持跨学段） | 低表皮相似 + 高骨架同构 | **水平举一反三核心边**：检验剥离故事包装识别底层模型的能力 |
| **`SCAFFOLD`（阶梯子母）** | **有向：`子题 → 母题`**（同学段） | 子题核心算子被母题包含且存在显著难度阶差 | **纵向升降阶**：母题卡壳时向 `pre` 找铺垫子题追根，子题通关后向 `post` 拔高 |
| **`CONTRASTIVE`（形似神异）** | 无向（同学段） | 题面高度相似但底层内核迥异（经模型精裁） | 并排对比辨析，破除“看字眼套套路”的审题思维定势 |
| **`SHARED_TRAP`（同坑共病）** | 无向（支持跨学段） | 共享高危数理陷阱（剔除纯四则笔误噪音） | 跨模块靶向排雷（如漏验增根、忽略定义域边界、参考系漂移） |

---

## 二、环境安装与前置配置

### 2.1 安装 Python 环境与 CLI

要求 **Python `>= 3.11`**，推荐使用 [`uv`](https://docs.astral.sh/uv/) 管理虚拟环境：

```bash
git clone <your-repo-url> shuati-harness
cd shuati-harness

# 方式 A（推荐）：使用 uv 创建 .venv 并安装全部开发与 MCP 依赖
uv sync --extra dev --extra mcp

# 方式 B：使用标准 venv + pip
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\python.exe -m pip install -e ".[dev,mcp]"
# Linux / macOS:
.venv/bin/python -m pip install -e ".[dev,mcp]"

# 验证 CLI 已就绪
shuati version --json
```

> 若未全局激活虚拟环境，可将下文示例中的 `shuati` 替换为 `.venv\Scripts\shuati.exe`（Windows）或 `uv run shuati`。

### 2.2 挂载 AI 客户端内置 Skill 与 MCP（可选）

仓库在 [`skills/`](skills/) 下提供三套分工明确的内置 Skill：`shuati-init`（冷启动与新学段定标）、`shuati-compiler`（日常四路增量编译与锁表）、`shuati`（日常防剧透自适应刷题）：

| 客户端环境 | 接入方式 | 命令 / 配置说明 |
| :--- | :--- | :--- |
| **AGY (Antigravity)** | **零配置自动发现** | 仓内 [`.agents/skills.json`](.agents/skills.json) 已声明 `skills/` 路径，克隆后用 AGY 打开仓库根目录即自动加载全部三套 Skill。 |
| **`pi` 客户端** | **一键同步挂载** | 运行 `shuati skill install --all --json`，自动软链（Windows 无权限时自动降级复制）至 `.pi/skills/`。 |
| **其他 AI 客户端** | **指向 `skills/` 或挂载 MCP** | 将 `skills/` 加入客户端规则路径，或按角色挂载本地 `stdio` MCP 服务：`shuati mcp serve --role practice`（刷题）/ `--role compiler` / `--role worker` / `--role all`。 |

### 2.3 配置模型与 Embedding 服务

复制 [`.env.example`](.env.example) 为 `.env`（已被 `.gitignore` 忽略）。根据你的编译与使用方式配置（完整配置模板见 [`skills/shuati-init/references/setup_templates.md`](skills/shuati-init/references/setup_templates.md)）：

1. **Embedding 向量服务（必配，三选一）**：
   - **云端 OpenAI 兼容 API**（如 OpenAI / 阿里云百炼 DashScope）：在 `.env` 中填写 `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`；
   - **本地 Ollama 免 Key**：启动 `ollama serve` 并拉取向量模型（如 `bge-m3`）后，在 `.env` 设置 `EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1`、`EMBEDDING_API_KEY=`（留空）、`EMBEDDING_MODEL=bge-m3`；
   - **纯离线 `local_st`**：在 [`config/settings.yaml`](config/settings.yaml) 中将 `llm.embedding.provider` 改为 `"local_st"` 并指定本地 `sentence-transformers` 模型名或路径。
2. **大语言与多模态推理模型（二选一）**：
   - **使用 `--mode api` 直连编译**：在 `.env` 中填写 `VLM_PRIMARY_*`（主路视觉推理）、`VLM_SECONDARY_*`（跨模型家族辅路双盲）与 `TEXT_LLM_*`（文本拆解与归纳；`JUDGE_LLM_*` 留空则自动回退复用 `TEXT_LLM`）；
   - **使用 AGY Swarm / Subagent 免推理 Key 双通道**：上述 `VLM_*` 与 `TEXT_LLM_*` 密钥均可留空，仅配妥 Embedding 即可。

配置完成后，执行一键环境体检（实测 SymPy 沙盒隔离、提示词接缝语法及真实 Embedding 维度返回）：

```bash
# 体检基础环境、SymPy 沙盒与 Embedding 连通性
shuati doctor --json

# 若配置了 .env 推理模型并准备使用 --mode api，追加探测全部 LLM 槽位
shuati doctor --check-llm-api --json
```

---

## 三、题库的编译方法

> **AI Skill 快捷代劳提示**：在 **AGY** 或 **`pi`** 中，无需手动逐条敲下列编译命令——首次接入直接对 Agent 说 *“帮我初始化项目并编译 `D:/我的题库/xxx`”*（自动触发 [`shuati-init`](skills/shuati-init/SKILL.md)）；后续日常加题、勘误或锁表直接说 *“帮我执行增量编译”*（自动触发 [`shuati-compiler`](skills/shuati-compiler/SKILL.md)）。以下为可在终端独立跑通的完整 CLI 流程。

### 3.1 首次全量冷启动编译（`--mode api` 直连主线）

#### Step 1：净仓初始化与准备外部只读题库

若首次克隆本仓库准备构建你自己的题库，先将仓内自带的示例 `human/*.jsonl` 无损归档并初始化干净的个人角色：

```bash
# 预览归档范围后，重置为你的专属默认学习者与目标学段
shuati human reset --learner-id alice --learner-name "Alice" --subject "小学奥数" --dry-run --json
shuati human reset --learner-id alice --learner-name "Alice" --subject "小学奥数" --json
```

外部题库目录支持任意磁盘位置挂载（系统绝不修改原目录），目录下按章节子目录存放 `problems.jsonl` 与相对路径配图，每行是一个标准 JSON 对象：

```json
{"id": "ch01_001", "question": "如图，正方形 ABCD 的边长为 10，求阴影部分面积。", "images": ["images/ch01_001.png"]}
```

- 如需编译新学科（例如 `初中物理`），先注册学段作用域：
  ```bash
  shuati grade-subject list --json
  shuati grade-subject add --name "初中物理" --slug "junior_physics" --family PHYSICS --json
  ```
- 编辑 [`config/settings.yaml`](config/settings.yaml) 中的 `ingest.roots` 填入你的外部题库根目录（若本地没有 `./题库` 目录，请替换掉默认的 `"题库"` 占位项），并在 [`config/collections.yaml`](config/collections.yaml) 中配置子目录通配符到 `grade_subject` 的映射规则。

#### Step 2：Stage 0 入库清洗与去重

```bash
# 1. 只读预检：确认 unmapped_collections 为空且无缺失图片报错
shuati ingest --dry-run --dedup-near --json

# 2. 正式入库（自动完成 CAS 保序图片归档、文本断行清洗、选择题选项提取与精确/近重复去重）
shuati ingest --dedup-near --json
```

#### Step 3：提示词接缝检查与 Stage 1.5 黄金难度锚点定标

每个学段开始深度拆解前，需标定至少 4 道（推荐 4～5 道）跨难度梯度的已验真题目作为 Few-Shot 黄金标尺：

```bash
# 1. 校验提示词接缝语法（新增学段可参照 config/prompts/subjects/primary_olympiad.yaml 新建学科接缝）
shuati prompt lint --json
shuati prompt render --stage unified --grade-subject "小学奥数" --json

# 2. 分层随机抽样 6 道候选锚点题，并仅对候选 UID 先跑 Stage 1 验真求解
shuati anchors sample --grade-subject "小学奥数" --count 6 --json
shuati compile --stage 1 --grade-subject "小学奥数" --only-uids "uid1,uid2,uid3,uid4,uid5,uid6" --mode api --json

# 3. 挑选 4~5 道 VERIFIED_CONSENSUS 代表题写入 config/anchors/anchors_<slug>.yaml 后，注册解锁 Stage 1.5
shuati unified calibrate-anchors --grade-subject "小学奥数" --json
```

#### Step 4：小样本烟测闸门与 Stage 1 ～ Stage 4 全量编译

对于百题以上规模题库，建议先用 `--only-uids` 抽取 20～25 题试跑 Stage 1 与 Stage 2，确认读图、SymPy 验真率与提示词颗粒度满意后再全量放开（所有编译命令均内置断点续跑，中断后重跑自动跳过已完成题目）：

```bash
# [可选推荐] 先抽 20~25 题做烟测验证
shuati compile --stage 1 --grade-subject "小学奥数" --only-uids "uid_a,uid_b,..." --mode api --json
shuati compile --stage 2 --grade-subject "小学奥数" --only-uids "uid_a,uid_b,..." --mode api --json

# 1. Stage 1：全量视觉事实转译 + 跨模型双盲求解 + SymPy 沙盒验真
shuati compile --stage 1 --grade-subject "小学奥数" --mode api --json

# 2. Stage 2：全量多模态深度步骤拆解、苏格拉底分级提示生成与四维正交打标
shuati compile --stage 2 --grade-subject "小学奥数" --mode api --json

# 3. Stage 3a：对有效题目步骤标签聚类归纳，涌现生成首版 DRAFT 标准词表 YAML
shuati taxonomy propose --grade-subject "小学奥数" --mode api --json

# 4. 同步全库本地向量矩阵与冻结余弦分位表（供算子归并与五边拓扑使用）
shuati reindex --vectors --quantiles --grade-subject "小学奥数" --json

# 5. 一键顺序执行 Stage 3c（词表映射）→ 3d（节点挂载与同域算子归并）→ 3e（先修 DAG）→ Stage 4（五边织网）
shuati compile --stage all --grade-subject "小学奥数" --mode api --json

# （也可按子阶段精细分步执行：--stage 3c / --stage 3d + graph compact / --stage 3e + graph check-dag / --stage 4）
```

#### Step 5：人工分歧复核与 Stage Q 全库质量体检

```bash
# 查看各阶段进度矩阵与待复核题量
shuati status --grade-subject "小学奥数" --json

# 临时启动极简本地键盘复核页（http://127.0.0.1:8765/），快速拍板处理少量 NEEDS_REVIEW 分歧题，审完 Ctrl+C 关闭
shuati review --grade-subject "小学奥数"

# 生成 7 大板块全库质量体检报告（输出至 data/reports/）
shuati audit --grade-subject "小学奥数" --json
```

---

### 3.2 免推理 API Key 的 Subagent 双通道（`export / import`）

当你在 AGY Swarm 等多智能体环境中工作、或希望离线批量调度外部模型而不配置 `.env` 推理密钥时，所有耗时推理阶段均支持 **`export`（导出脱敏任务包）→ Worker 批推理与零 DB 自检 → `import`（单写者确定性校验回灌）** 双通道：

```bash
# 1. 导出 Stage 1+2 合一脱敏批任务包（自动剥离章节/旧标签，生成 data/unified/batch_*.jsonl 与隔离的 task_map.json）
shuati unified export --grade-subject "小学奥数" --batch-size 10 --json

# 2. 子代理/外部脚本逐题推理，并在落盘前调用零 DB 无状态工具完成契约与 SymPy 沙盒自检
shuati tool verify-sympy --input @draft_sympy.json --json
shuati tool validate-draft --mode UNIFIED_S1_S2 --input @draft_candidate.json --json

# 3. 单写者确定性回灌入库（若有校验未过条目会生成 retry_tasks.jsonl；终轮仍无法符号化的定性题可加 --final）
shuati unified import --results data/unified/results.jsonl --task-map data/unified/task_map.json --json

# 4. Stage 3a / 3c / compact / 3e / Stage 4 同样对称支持 --mode export 与 --mode import
shuati taxonomy propose --grade-subject "小学奥数" --mode export --json
shuati taxonomy propose --grade-subject "小学奥数" --mode import --results data/unified/stage3_4/propose/results.jsonl --json

shuati compile --stage 3c --grade-subject "小学奥数" --mode export --json
shuati compile --stage 3c --grade-subject "小学奥数" --mode import --tasks-dir data/unified/stage3_4/3c --results data/unified/stage3_4/3c/results.jsonl --json

shuati compile --stage 3d --grade-subject "小学奥数" --json
shuati graph compact --grade-subject "小学奥数" --mode export --json
shuati graph compact --grade-subject "小学奥数" --mode import --results data/unified/stage3_4/compact/results.jsonl --json

shuati compile --stage 3e --grade-subject "小学奥数" --mode export --json
shuati compile --stage 3e --grade-subject "小学奥数" --mode import --tasks-dir data/unified/stage3_4/3e --results data/unified/stage3_4/3e/results.jsonl --json
shuati graph check-dag --grade-subject "小学奥数" --json

shuati reindex --vectors --quantiles --grade-subject "小学奥数" --json
shuati compile --stage 4 --grade-subject "小学奥数" --mode export --json
shuati compile --stage 4 --grade-subject "小学奥数" --mode import --tasks-dir data/unified/stage3_4/stage4 --results data/unified/stage3_4/stage4/results.jsonl --json
```

> **防错提示**：在当前导出的批次完成 `import` 回灌前，切勿附加 `--overwrite-pending` 重复运行 `export`，以免临时 `task_id` 映射错位。

---

### 3.3 日常四类增量演进速查

每次执行增量维护前，养成先跑 **`shuati status --json`** 与 **`shuati ingest --dry-run --dedup-near --json`** 开场双检的习惯：

```bash
# 场景 A：同已有学段追加新章节 / 新题（既有已编译题目自动跳过，严禁误加 --apply-retire）
shuati ingest --dedup-near --json
shuati compile --stage 1 --grade-subject "小学奥数" --mode api --json
shuati compile --stage 2 --grade-subject "小学奥数" --mode api --json
shuati compile --stage 3c --grade-subject "小学奥数" --mode api --json
shuati compile --stage 3d --grade-subject "小学奥数" --json
# 若 shuati status 提示 vectors.needs_reindex == true（如题量增长 >=20% 或模块分布漂移），先重算分位表
shuati reindex --vectors --quantiles --grade-subject "小学奥数" --json
shuati compile --stage 4 --grade-subject "小学奥数" --mode api --json

# 场景 B：源题文字或配图勘误局部重编（自动保护锚点与词表覆写，仅解锁内容变更题）
shuati ingest --dedup-near --json
shuati compile --stage 1 --grade-subject "小学奥数" --only-uids "变更题UID" --mode api --json
shuati compile --stage 2 --grade-subject "小学奥数" --only-uids "变更题UID" --mode api --json
shuati compile --stage all --grade-subject "小学奥数" --mode api --json

# 场景 C：接入全新学段学科（零触碰既有学段数据，独立走完学段注册 → 锚点定标 → 首轮编译）
shuati grade-subject add --name "初中数学" --slug "junior_math" --family MATH --json
# 补充 config/collections.yaml 与 config/prompts/subjects/junior_math.yaml 后按 3.1 节限定 --grade-subject "初中数学" 执行

# 场景 D：审定词表从 DRAFT 晋升锁定为 LOCKED 1.0（或后续带 changelog 增量迁移）
shuati taxonomy candidates --grade-subject "小学奥数" --status PENDING --json
shuati taxonomy lock --grade-subject "小学奥数" --to-version 1.0 --status LOCKED --dry-run --json
shuati taxonomy lock --grade-subject "小学奥数" --to-version 1.0 --status LOCKED --json
shuati compile --stage all --grade-subject "小学奥数" --mode api --json
```

---

## 四、题库编译好后的使用方法

> **AI Skill 快捷代劳提示**：在 **AGY** 或 **`pi`** 中直接对 Agent 说 *“看看我现在的平面几何水平，组一套 5 题小卷并生成可打印练习单”* 或 *“帮我诊断一下这道刚拍的错题”*，Agent 会自动按 [`shuati`](skills/shuati/SKILL.md) 严守防剧透纪律调用下列命令完成交互辅导。

### 4.1 多学习者角色管理与动态能力画像

同一套题库可供家庭或小组内多名学习者共用，做题记录与能力水位完全隔离：

```bash
# 查看全部学习者角色（is_current=1 为当前默认角色）
shuati learner list --json

# 创建新角色并切换为当前激活角色
shuati learner create xiaoming --name "小明" --subject "小学奥数" --current --json
shuati learner use xiaoming --json

# 查看当前角色在目标学段的分模块能力水位 R、推荐最近发展区（ZPD）、8 档阶梯与薄弱考点雷达
shuati profile --grade-subject "小学奥数" --json
```

### 4.2 浏览知识树、多维语义搜题与五边拓扑举一反三

所有查题与邻接推题命令默认均处于 `--reveal quiz` 防剧透保护态（仅返回题干与配图路径）：

```bash
# 浏览知识目录树、节点题量、先修依赖边，并叠加学习者掌握热力
shuati taxonomy --grade-subject "小学奥数" --learner xiaoming --l1 "平面几何" --json

# 组合结构化标签、难度区间、FTS5 关键词或解题骨架语义向量检索未做对的题目
shuati search --grade-subject "小学奥数" --l1 "平面几何" --motif "蝴蝶模型" --exclude-solved --limit 10 --json
shuati search --grade-subject "小学奥数" --semantic "通过等积变形将不规则阴影转化为扇形减三角形" --elo-max 1900 --json

# 沿五边拓扑为某道题查找前置降阶铺垫子题（pre）、升维母题（post）或同构/排雷变式
shuati related ch05_012 --types SCAFFOLD --direction pre --exclude-solved --json
shuati related ch05_012 --types ISOMORPHIC,CONTRASTIVE,SHARED_TRAP --json
```

### 4.3 ZPD 自适应梯度组卷与双模式离线 HTML 练习单

```bash
# 按学习者当前模块水位自动配比生成 5 题专题小卷（2 道稳固 + 2 道拔高 + 1 道薄弱陷阱/辨析排雷）
shuati compose-quiz --grade-subject "小学奥数" --l1 "平面几何" --count 5 --mix "2:2:1" --json

# 将刚生成的组卷批次（或指定 --uids 题单）渲染为单文件自包含 HTML 练习单
# 浏览器直接打开：支持 KaTeX 公式与“逐步提示 / 标准答案 / 深度解析”三级防剧透折叠
# 浏览器按 Ctrl+P 打印：自动隐藏交互按钮、每题下方动态留出作图演算空白，并将答案独立排在末页
shuati render-sheet --quiz-batch-id qz_20261124_xxxx --title "平面几何拔高小卷" --json
```

### 4.4 防剧透苏格拉底分级提示、零泄密判题与足迹管理

```bash
# 1. 出题看题面（默认 quiz 档，不泄露答案与标签）
shuati detail ch05_012 --reveal quiz --json

# 2. 解题卡壳时，按级申请苏格拉底式启发提示（第 1 步不够再升至 --hint-level 2）
shuati detail ch05_012 --reveal hint --hint-level 1 --json

# 3. 提交答案判对错（支持多小问如 "(1)9:4;(2)156"；仅返回逐问正误判定，绝不剧透标准答案，--log 同步记入足迹）
shuati check ch05_012 --answer "24" --hint-level 1 --log --json

# 4. 答对复盘或明确放弃后，查看完整多解推导与知识点拆解
shuati detail ch05_012 --reveal full --json

# 5. 若使用打印纸笔练习单线下作答，事后可单题登记、整卷批量回填，或冲销误记流水
shuati log ch05_012 --outcome SOLVED_SOLO --note "利用等底等高秒解" --json
shuati log-batch --quiz-batch-id qz_20261124_xxxx --entries @batch_outcomes.json --json
shuati log-void 108 --reason "录错题号，冲销重记" --json
```

### 4.5 外部新错题诊断入库、单题人工纠错与换机恢复

```bash
# 1. 遇到题库外的全新错题（支持附带本地手机拍照图片）：只读实时诊断知识卡点、推荐库内铺垫题并生成 draft_id
shuati diagnose --text "甲乙两人分别从 A、B 两地同时相向而行..." --image ./photo1.jpg --grade-subject "小学奥数" --json

# （若是整理好的多题 Markdown 错题文档，先运行只读预检生成规范化副本，再批量诊断）
shuati diagnose-preflight --file ./mistakes.md --json
shuati diagnose --file data/drafts/preflight/mistakes.normalized.md --grade-subject "小学奥数" --json

# 2. 将价值高的外部新错题一键正式收入本地题库（自动归档图片、织入五边拓扑并同步写入 WRONG 记录）
shuati ingest-mistake --draft-id drf_xxxx --log-wrong --learner xiaoming --json

# 3. 日常发现某题答案、读图描述或标签有瑕疵，一键写入人工权威覆写（后续重跑流水线永不覆盖）
shuati override ch05_012 --field visual_facts --value "阴影部分由扇形 ABD 与三角形 BCD 重叠区域构成" --reason "补充阴影边界说明" --json

# 4. 换机或空库重编后的完整资产恢复，以及导出脱离 SQLite 的 4 份开放标准 JSONL
shuati ingest --dedup-near --json
shuati human restore --json
shuati human verify --json
shuati reindex --fts --effective --vectors --quantiles --json
shuati export --export-dir data/exports --json
```

---

## 五、全量 CLI 命令手册（51 个子命令）

### 5.0 全局通用约定

1. **统一 JSON 响应信封（`--json`）**：除本地临时 HTTP 复核服务 `shuati review` 与长连接 `stdio` 服务 `shuati mcp serve` 外，其余 **49 个**命令全部支持 `--json` 选项，向 `stdout` 输出单行 UTF-8 JSON（运行日志仅输出至 `stderr`）：
   ```json
   {
     "ok": true,
     "command": "profile",
     "data": { "...": "..." },
     "warnings": [],
     "error": null,
     "meta": { "schema_version": 1, "duration_ms": 18 }
   }
   ```
2. **标准退出码（Exit Codes）**：
   - `0`：执行成功（`ok: true`）
   - `1`：未预期内部异常（`INTERNAL_ERROR`）
   - `2`：前置门禁未满足、参数不合法或短题号存在多集合歧义（`PRECONDITION_FAILED` / `AMBIGUOUS_UID`）
   - `3`：目标题目、草稿或批次不存在（`NOT_FOUND`）
   - `4`：外部 VLM / LLM / Embedding 网关请求失败（`LLM_ERROR`）
3. **全局通用路径与调试选项**（按命令读写范围自动挂载，默认即指向标准仓库目录，下文各条命令不再重复罗列）：
   - `--db PATH`（默认 `data/shuati.db`）｜`--config-dir PATH`（默认 `config`）｜`--vectors-dir PATH`（默认 `data/vectors`）
   - `--assets-dir PATH`（默认 `data/assets/images`）｜`--human-dir PATH`（默认 `human`）｜`--drafts-dir PATH`（默认 `data/drafts`）
   - `--reports-dir PATH`（默认 `data/reports`）｜`--mock / --no-mock`（启用离线确定性 Mock 探针供无网测试）｜`--json`

---

### 5.1 环境、态势巡检、索引重建、Skill 与 MCP（6 个）

#### 1. `shuati version`
查看当前 `shuati-harness` 版本号。
```bash
shuati version --json
```

#### 2. `shuati doctor`
冷启动与运行环境健康体检（无需已存在数据库；实测 SymPy 安全沙盒、提示词接缝、题源目录及真实 Embedding 向量维度）。
- **业务选项**：`--check-llm-api`（额外实测 `.env` 中 VLM/LLM 推理端点连通性；Swarm 免推理 Key 模式可省略）。
```bash
shuati doctor --check-llm-api --json
```

#### 3. `shuati status`
只读巡检流水线全景态势（Stage 0～4 进度矩阵、挂起未回灌批次 `pending_batches`、词表状态、向量分位漂移 `needs_reindex` 与下一步建议）。
- **业务选项**：`--grade-subject TEXT`（限定学段学科）、`--unified-dir PATH`（默认 `data/unified`）。
```bash
shuati status --grade-subject "小学奥数" --json
```

#### 4. `shuati reindex`
重建 FTS5 全文索引、重算生效层 `eff_*` 列、同步 5 类 `.npz` 本地向量矩阵或重算冻结经验分位表 `meta.json`（不传子开关时默认执行 `--fts --effective`）。
- **业务选项**：`--fts`（重建 FTS5 `trigram` 索引）、`--effective`（全量重算并核验 `eff_*` 生效列）、`--vectors`（差量/全量同步向量矩阵）、`--quantiles`（重算余弦分位表 `meta.json`）、`--grade-subject TEXT`、`--force`（强制清空旧向量模型绑定并全量重算）。
```bash
shuati reindex --fts --effective --vectors --quantiles --grade-subject "小学奥数" --json
```

#### 5. `shuati skill install`
将内置 Skill（`shuati` / `shuati-init` / `shuati-compiler`）软链或复制安装到客户端发现目录（Windows 无软链权限时自动降级复制）。
- **业务选项**：`--all`（一键安装全部三套内置 Skill）、`--skill TEXT`（单装指定：`all` / `shuati` / `shuati-init` / `shuati-compiler`）、`--target-dir PATH`（多 Skill 目标根目录，默认 `.pi/skills`）、`--target PATH`（单 Skill 目标路径）、`--source-dir PATH`（默认 `skills`）、`--source PATH`、`--symlink / --copy`（默认 `--symlink`）。
```bash
shuati skill install --all --json
```

#### 6. `shuati mcp serve`
启动换行分隔的 UTF-8 JSON-RPC 2.0 `stdio` MCP 服务，按角色物理隔离数据与工具权限。
- **业务选项**：`--role TEXT`（默认 `worker`：可选 `worker` 物理零 DB 仅暴露 `verify_sympy` 与 `validate_draft` / `compiler` 编译辅助工具集 / `practice` 防剧透刷题工具集 / `all` 暴露 `compiler + practice` 全集）、`--unified-dir PATH`。
```bash
shuati mcp serve --role practice
```

---

### 5.2 学段管理与原始题源入库（3 个）

#### 7. `shuati grade-subject list`
列出全部已注册的学段学科及其 `slug`、学科家族、黄金锚点解锁状态与词表版本。
```bash
shuati grade-subject list --json
```

#### 8. `shuati grade-subject add`
注册新的独立学段学科作用域。
- **业务选项**：`--name TEXT`（必填，如 `"初中物理"`）、`--slug TEXT`（必填，如 `"junior_physics"`）、`--family TEXT`（学科家族：`MATH` / `PHYSICS` / `CHEMISTRY` / `STEM_OTHER`，默认 `MATH`）、`--display-name TEXT`（可选前端显示名）。
```bash
shuati grade-subject add --name "初中物理" --slug "junior_physics" --family PHYSICS --json
```

#### 9. `shuati ingest`
Stage 0 原始题库递归扫描、配图可读性校验、保序 CAS 归档、文本断行与选择题选项提取清洗、精确与近重复去重入库。
- **业务选项**：`--root PATH`（可多次传入指定题库根目录；省略时读取 `settings.yaml` 的 `ingest.roots`）、`--grade-subject TEXT`（仅单集合导入时显式覆盖学段）、`--dedup-near / --no-dedup-near`（开启/关闭 `plain_fingerprint + dHash` 近重复归并）、`--apply-retire`（将源端已消失题目软下架置 `is_retired=1`）、`--dry-run`（只读预检，不写库不拷图）。
```bash
shuati ingest --dry-run --dedup-near --json
shuati ingest --dedup-near --json
```

---

### 5.3 提示词接缝、黄金难度锚点与零 DB 校验工具（7 个）

#### 10. `shuati prompt list`
列出 `config/prompts/` 下三层提示词接缝清单（`default`、`subjects`、`profiles`、`snippets`）。
```bash
shuati prompt list --json
```

#### 11. `shuati prompt render`
渲染指定阶段与学段合并后的完整 Prompt 预览、内容哈希与扁平化 JSON Schema。
- **业务选项**：`--stage TEXT`（默认 `unified`；可选 `unified` / `1` / `stage1_secondary` / `stage1_judge` / `judge_multi` / `2` / `3a` / `3c` / `3d` / `merge_judge` / `3e` / `4` / `diagnose`）、`--grade-subject TEXT`（默认 `小学奥数`）、`--prompt-profile TEXT`（加载 `config/prompts/profiles/<name>.yaml`）、`--sample TEXT`（样例载荷 JSON 或 `@file.json`）。
```bash
shuati prompt render --stage unified --grade-subject "小学奥数" --json
```

#### 12. `shuati prompt lint`
静态校验所有提示词接缝 YAML 的必填槽位、继承合并合法性与外链片段路径安全性。
```bash
shuati prompt lint --json
```

#### 13. `shuati anchors sample`
只读跨长度分位与有图/无图分层随机抽样候选黄金难度锚点题（自动隐藏原始章节防污染）。
- **业务选项**：`--grade-subject TEXT`（必填）、`--count INTEGER`（抽样数量，默认 `6`）。
```bash
shuati anchors sample --grade-subject "小学奥数" --count 6 --json
```

#### 14. `shuati unified calibrate-anchors`
校验并加载 Stage 1.5 黄金难度锚点 YAML（至少 4 道已验真跨梯度题），写入 `ANCHOR_CALIBRATION` 覆写并置 `anchors_ready=1`。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--anchors-file PATH`（显式指定锚点文件，默认读取 `config/anchors/anchors_<slug>.yaml`）。
```bash
shuati unified calibrate-anchors --grade-subject "小学奥数" --json
```

#### 15. `shuati tool verify-sympy`
零 DB 无状态执行 `AnswerPart[]` 归一化与 SymPy 安全沙盒验算（供子代理或外部批脚本交卷前自检）。
- **业务选项**：`--input TEXT`（JSON 字符串或 `@file.json`，与 `--stdin` 互斥）、`--stdin`（从标准输入读入 JSON）、`--grade-subject TEXT`。
```bash
shuati tool verify-sympy --input @sympy_payload.json --json
```

#### 16. `shuati tool validate-draft`
零 DB 无状态执行拆解草稿全量契约校验（Schema + 防剧透规则 + `raw_l3` 去数字与黑名单 + 四维难度边界 + 可选 SymPy 验算）。
- **业务选项**：`--input TEXT`（JSON 字符串或 `@file.json`）、`--stdin`、`--mode TEXT`（`UNIFIED_S1_S2` / `STAGE2_ONLY` / `DIAGNOSE`）、`--grade-subject TEXT`、`--run-sympy / --no-run-sympy`（默认开启）。
```bash
shuati tool validate-draft --mode UNIFIED_S1_S2 --input @draft_candidate.json --json
```

---

### 5.4 离线编译流水线、人工复核台与全库体检（5 个）

#### 17. `shuati unified export`
导出免推理 Key 的 Stage 1+2 合一（或仅 Stage 2）脱敏批任务包 JSONL、锁模 `unified_manifest.json` 与物理隔离的 `task_map.json`。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--out-dir PATH`（默认 `data/unified`）、`--only-uids TEXT`（限定 UID 或短 `source_id`，支持逗号分隔或重复传参）、`--limit INTEGER`、`--batch-size INTEGER`（默认 `10`）、`--force`（忽略锁定重导）、`--subagent-model TEXT`、`--prompt-profile TEXT`、`--overwrite-pending`（允许覆盖未回灌挂起批）。
```bash
shuati unified export --grade-subject "小学奥数" --batch-size 10 --json
```

#### 18. `shuati unified import`
单写者确定性校验并事务回灌 `unified export` 的批处理结果。
- **业务选项**：`--results PATH`（必填，结果 JSONL）、`--task-map PATH`（默认 `data/unified/task_map.json`）、`--retry-out PATH`、`--final`（终轮回灌：SymPy 仍败但 Stage 2 合规者降级 `NEEDS_REVIEW` 保留拆解）、`--force`、`--worker-tag TEXT`（默认 `main-session`）、`--subagent-model TEXT`。
```bash
shuati unified import --results data/unified/results.jsonl --task-map data/unified/task_map.json --json
```

#### 19. `shuati compile`
按阶段执行断点续跑的离线知识编译流水线，统一支持 `api` 直连与 `export / import` 脱敏批任务包双通道。
- **业务选项**：`--stage TEXT`（必填：`1` 视觉转译与求解验真 / `2` 强制多模态拆解与四维打标 / `3c` 词表回填映射 / `3d` 同域节点挂载 / `3e` 先修依赖 DAG / `4` 五边拓扑织网 / `all` 顺序执行 `3c→3d→compact→3e→4`，`all` 仅限 `--mode api`）、`--mode TEXT`（`api` / `export` / `import`，默认 `api`）、`--grade-subject TEXT`、`--only-uids TEXT`、`--stress-test-25`（Stage 1 仅跑 25 题压力测试集）、`--force`、`--include-human-verified`（Stage 1 包含已人工核验题重跑）、`--include-unservable`（Stage 4 包含 `NEEDS_REVIEW` 题）、`--tasks-dir PATH`、`--results PATH`、`--batch-size INTEGER`、`--subagent-model TEXT`、`--prompt-profile TEXT`、`--overwrite-pending`。
```bash
shuati compile --stage 1 --grade-subject "小学奥数" --mode api --json
shuati compile --stage all --grade-subject "小学奥数" --mode api --json
```

#### 20. `shuati review`
启动仅绑定 `127.0.0.1` 的极简本地键盘复核页（用完即关），同屏展示高清原图、题干与双路解法，支持全键盘拍板处理 `NEEDS_REVIEW` 队列。
- **业务选项**：`--grade-subject TEXT`、`--port INTEGER`（默认 `8765`）、`--audit`（切换为对已验真题目的定向假共识抽检模式）。
```bash
shuati review --grade-subject "小学奥数" --port 8765
```

#### 21. `shuati audit`
执行 Stage Q 全库质量体检，输出覆盖验真率、难度分布、L3 收敛度、DAG 无环、五边度数、向量漂移与人工镜像一致性的 7 板块报告。
- **业务选项**：`--grade-subject TEXT`、`--prune-runs`（清理未被引用的历史失败 `stage1_vlm_runs` 记录）。
```bash
shuati audit --grade-subject "小学奥数" --json
```

---

### 5.5 词表演进与知识图谱拓扑治理（9 个）

#### 22. `shuati taxonomy`（无子命令浏览模式）
浏览目标学段的 `L1/L2/L3` 知识目录树、各节点题量、先修依赖 DAG，并可叠加指定学习者的掌握热力。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--learner TEXT`（叠加该角色掌握状态）、`--l1 TEXT`（仅展开指定一级大模块）。
```bash
shuati taxonomy --grade-subject "小学奥数" --learner xiaoming --json
```

#### 23. `shuati taxonomy propose`
Stage 3a 对全学段有效题目的原始步骤标签（`raw_l1 / raw_l2`）聚类归纳，自动涌现生成 `DRAFT` 候选目录树 YAML。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--mode TEXT`（`api` / `export` / `import`）、`--n-clusters INTEGER`、`--tasks-dir PATH`、`--results PATH`、`--subagent-model TEXT`、`--prompt-profile TEXT`、`--overwrite-pending`。
```bash
shuati taxonomy propose --grade-subject "小学奥数" --mode api --json
```

#### 24. `shuati taxonomy candidates`
查看 Stage 3c 增量映射期间自动收集的新增待审 `L1/L2` 词表候选列表。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--status TEXT`（`PENDING` / `APPROVED` / `REJECTED` / `MAPPED` / `ALL`，默认 `PENDING`）。
```bash
shuati taxonomy candidates --grade-subject "小学奥数" --status PENDING --json
```

#### 25. `shuati taxonomy lock`
Stage 3b 执行四步原子事务，将词表从 `DRAFT` 锁定晋升为 `LOCKED` 正式版（自动备份数据库、迁移人工覆写与做题卡点节点、标记后续阶段待重编）。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--file PATH`（默认取当前学段词表 YAML）、`--to-version TEXT`（默认 `1.0`）、`--status TEXT`（`LOCKED` / `DRAFT`，默认 `LOCKED`）、`--allow-stale-overrides`（将无法自动映射的覆写置为 `TAXONOMY_UNMAPPED` 挂起）、`--dry-run`（仅预检迁移影响）。
```bash
shuati taxonomy lock --grade-subject "小学奥数" --to-version 1.0 --status LOCKED --dry-run --json
```

#### 26. `shuati taxonomy migrate`
按新版 `LOCKED` YAML 中的 `changelog`（`add` / `rename` / `merge`）执行正式词表精准增量版本升级。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--file PATH`（必填，含 `changelog` 的新词表文件）、`--dry-run`。
```bash
shuati taxonomy migrate --grade-subject "小学奥数" --file config/taxonomies/taxonomy_primary_olympiad.v1_1.yaml --dry-run --json
```

#### 27. `shuati graph compact`
Stage 3d 在同一 `(grade_subject, L1, L2)` 域内执行 `Cluster-then-Assign` 标准原子算子（`L3`）与全局概念（`METHOD` / `MOTIF`）聚类归并，写入可逆并查集。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--target TEXT`（`all` / `l3` / `concepts`，默认 `all`）、`--mode TEXT`（`api` / `export` / `import`）、`--batch-size INTEGER`（默认 `40`）、`--tasks-dir PATH`、`--results PATH`、`--subagent-model TEXT`、`--prompt-profile TEXT`、`--overwrite-pending`。
```bash
shuati graph compact --grade-subject "小学奥数" --target all --mode api --json
```

#### 28. `shuati graph revert-merge`
按审计账本严格逆序无损拆回（Un-merge）指定批次的算子或概念归并。
- **业务选项**：`--batch-id TEXT`（必填，待回滚的归并批次 ID）。
```bash
shuati graph revert-merge --batch-id cmp_20261124_xxxx --json
```

#### 29. `shuati graph check-dag`
校验当前学段 `PREREQUISITE` 先修依赖边是否满足强有向无环图（DAG）特性。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）。
```bash
shuati graph check-dag --grade-subject "小学奥数" --json
```

#### 30. `shuati graph calibrate-eval`
评估五边校准样本集命中率/误报率，或从五边及阈值边界带抽样候选题目对供阈值审定。
- **业务选项**：`--stress-yaml PATH`（默认 `config/stress_test_25.yaml`）、`--propose-candidates`（开启抽样模式）、`--per-type-samples INTEGER`（每类边抽样上限，默认 `30`）。
```bash
shuati graph calibrate-eval --propose-candidates --per-type-samples 30 --json
```

---

### 5.6 学习者画像、多维检索、梯度组卷与防剧透刷题（13 个）

#### 31. `shuati learner list`
列出全部已创建的学习者角色及当前激活状态。
```bash
shuati learner list --json
```

#### 32. `shuati learner create <learner_id>`
创建新的学习者隔离角色并同步更新 `human/learners.jsonl`。
- **位置参数与业务选项**：`<learner_id>`（必填角色 ID）、`--name TEXT`（显示名称）、`--subject TEXT`（默认学段学科，默认 `小学奥数`）、`--current`（创建并设为当前激活角色）、`--notes TEXT`。
```bash
shuati learner create xiaoming --name "小明" --subject "小学奥数" --current --json
```

#### 33. `shuati learner use <learner_id>`
切换当前默认激活的学习者角色。
- **位置参数**：`<learner_id>`（目标角色 ID）。
```bash
shuati learner use xiaoming --json
```

#### 34. `shuati profile`
实时重放做题记录，输出分 `L1` 模块能力水位、推荐 ZPD、8 档通过率阶梯（并列诊断 `L4-A` 与 `L4-B`）及薄弱考点/陷阱雷达。
- **业务选项**：`--learner TEXT`（默认当前激活角色）、`--grade-subject TEXT`（默认 `小学奥数`）。
```bash
shuati profile --learner xiaoming --grade-subject "小学奥数" --json
```

#### 35. `shuati search`
多维结构化标签 + FTS5 全文关键词 + 抽象解题骨架语义向量综合检索（默认 `reveal=quiz` 防剧透）。
- **业务选项**：`--grade-subject TEXT`、`--l1 TEXT`、`--l2 TEXT`、`--l3 TEXT`、`--motif TEXT`、`--method TEXT`、`--trap TEXT`、`--tier TEXT`、`--elo-min INTEGER`、`--elo-max INTEGER`、`--query TEXT`（FTS5 关键词）、`--semantic TEXT`（按抽象解题骨架语义检索）、`--exclude-solved`（排除已独立做对题）、`--learner TEXT`、`--reveal TEXT`（`quiz` / `hint` / `full`，默认 `quiz`）、`--hint-level INTEGER`（默认 `1`）、`--include-visual-facts`、`--limit INTEGER`（默认 `20`）。
```bash
shuati search --grade-subject "小学奥数" --l1 "数论" --tier "L4-B_进阶拔高(巧)" --exclude-solved --limit 10 --json
```

#### 36. `shuati related <uid>`
沿五边拓扑查询前置铺垫子题、升维挑战母题、同构变式、形似神异辨析与同坑排雷题。
- **位置参数与业务选项**：`<uid>`（完整 `question_uid` 或唯一短 `source_id`）、`--types TEXT`（逗号分隔：`CLONE,ISOMORPHIC,SCAFFOLD,CONTRASTIVE,SHARED_TRAP`）、`--direction TEXT`（针对 `SCAFFOLD`：`pre` 铺垫子题 / `post` 升维母题 / `both`，默认 `both`）、`--exclude-solved`、`--learner TEXT`、`--reveal TEXT`（默认 `quiz`）、`--hint-level INTEGER`、`--include-visual-facts`、`--limit INTEGER`（默认 `20`）。
```bash
shuati related ch05_012 --types SCAFFOLD --direction pre --json
```

#### 37. `shuati compose-quiz`
按角色分模块能力水位一键梯度组专题小卷，并持久化 `quiz_batch_id`。
- **业务选项**：`--grade-subject TEXT`（默认 `小学奥数`）、`--learner TEXT`、`--l1 TEXT`、`--l2 TEXT`、`--l3 TEXT`、`--motif TEXT`、`--method TEXT`、`--trap TEXT`、`--count INTEGER`（默认 `5`）、`--mix TEXT`（`稳固:拔高:排雷` 比例，默认 `"2:2:1"`）、`--target-elo INTEGER`、`--exclude-solved-days INTEGER`、`--exclude-clone-days INTEGER`、`--exclude-recent-quiz-batches INTEGER`、`--reveal TEXT`（默认 `quiz`）、`--include-visual-facts`。
```bash
shuati compose-quiz --grade-subject "小学奥数" --l1 "行程问题" --count 5 --mix "2:2:1" --json
```

#### 38. `shuati detail <uid>`
按防剧透档位查看单题题面、本地配图路径、分级提示或完整多解解析。
- **位置参数与业务选项**：`<uid>`、`--reveal TEXT`（`quiz` 仅题面配图 / `hint` 展示前 `k` 步提示 / `full` 展示答案与全套解析标签，默认 `quiz`）、`--hint-level INTEGER`（默认 `1`）、`--include-visual-facts`。
```bash
shuati detail ch05_012 --reveal hint --hint-level 1 --json
```

#### 39. `shuati check <uid>`
不泄露标准答案的逐小问等价判题（仅返回各问正误与得分率，可选同步记录做题日志）。
- **位置参数与业务选项**：`<uid>`、`--answer TEXT`（必填，单值或多小问如 `"(1)9:4;(2)156"`）、`--learner TEXT`、`--hint-level INTEGER`（本次使用过的最高提示层级，默认 `0`）、`--log`（同步写入 `practice_log`）、`--request-id TEXT`（幂等请求 ID）、`--quiz-batch-id TEXT`、`--judge-verdict TEXT`（定性小问外部裁判 JSON 或 `@file.json`）。
```bash
shuati check ch05_012 --answer "(1)9:4;(2)156" --log --json
```

#### 40. `shuati log <uid>`
手工登记单题做题结果、部分得分与卡点步骤（自动快照卡点节点并同步 `human/practice_log.jsonl`）。
- **位置参数与业务选项**：`<uid>`、`--outcome TEXT`（必填：`SOLVED_SOLO` / `SOLVED_WITH_HINT` / `WRONG` / `SKIPPED`）、`--learner TEXT`、`--score-ratio FLOAT`（`[0.0, 1.0]`）、`--stuck-step INTEGER`、`--hint-level INTEGER`、`--answer TEXT`、`--note TEXT`、`--quiz-batch-id TEXT`、`--request-id TEXT`、`--practiced-at TEXT`（UTC ISO 时间字符串）。
```bash
shuati log ch05_012 --outcome WRONG --stuck-step 2 --note "漏掉辅助线等高比例转化" --json
```

#### 41. `shuati log-batch`
按 `quiz_batch_id` 整卷批量回填线下做题记录。
- **业务选项**：`--quiz-batch-id TEXT`（必填）、`--entries TEXT`（必填，JSON 数组或 `@file.json`，元素含 `{uid, outcome, ...}`）、`--learner TEXT`、`--request-id TEXT`。
```bash
shuati log-batch --quiz-batch-id qz_20261124_xxxx --entries @outcomes.json --json
```

#### 42. `shuati log-void <log_id>`
以只增不删审计方式冲销误记的做题日志记录。
- **位置参数与业务选项**：`<log_id>`（整数日志主键）、`--reason TEXT`（冲销说明）。
```bash
shuati log-void 42 --reason "题号录错" --json
```

#### 43. `shuati render-sheet`
将小卷批次或任意题单渲染为离线单文件双模式静态 HTML（屏幕折叠防剧透 + 打印动态留白纸笔卷）。
- **业务选项**：`--quiz-batch-id TEXT`（与 `--uids` 二选一）、`--uids TEXT`（逗号分隔 UID 列表）、`--title TEXT`（默认 `"智学练习单"`）、`--embed-images / --no-embed-images`（默认 Base64 内联图片）、`--print-answers-at-end / --no-print-answers-at-end`（默认打印版末页集中附答案）、`--out PATH`、`--sheets-dir PATH`（默认 `data/sheets`）。
```bash
shuati render-sheet --uids "ch05_012,ch05_019,ch08_004" --title "几何专项加练" --json
```

---

### 5.7 外部新错题接入、人工覆写、资产恢复与开放导出（8 个）

#### 44. `shuati diagnose-preflight`
只读预检批量错题 Markdown 文档，自动修复标题漏空格、错位 `图片:` 行与同行正文，生成安全规范化副本。
- **业务选项**：`--file PATH`（必填，待预检 Markdown 路径）、`--out-normalized PATH`（默认输出至 `data/drafts/preflight/`）。
```bash
shuati diagnose-preflight --file ./mistakes.md --json
```

#### 45. `shuati diagnose`
只读实时诊断外部新错题（支持 `--text` 单题带图或 `--file` 批量 Markdown 列表），输出知识定位、库内铺垫/变式推荐并缓存 `draft_id`。
- **业务选项**：`--text TEXT`（与 `--file` 互斥）、`--file PATH`（批量题单路径）、`--image PATH`（单题模式可多次传入图片路径）、`--grade-subject TEXT`（默认 `小学奥数`）、`--answer TEXT`（单题可选已知答案）、`--from-json TEXT`（单题结构化诊断 JSON 或 `@file.json`）、`--concurrency INTEGER`（批量模式并发数，默认 `1`）、`--force`（批量模式忽略缓存重跑）。
```bash
shuati diagnose --text "如图，求阴影面积..." --image ./q1.jpg --grade-subject "小学奥数" --json
```

#### 46. `shuati ingest-mistake`
将外部新错题一键正式写入本地题库、归档图片、织入五边拓扑并镜像持久化至 `human/external_questions.jsonl`。
- **业务选项**：`--draft-id TEXT`（复用 `diagnose` 草稿 ID）、`--text TEXT`（无 `--draft-id` 时必填）、`--image PATH`（可多次传入）、`--grade-subject TEXT`、`--answer TEXT`、`--from-json TEXT`、`--secondary-json TEXT`、`--log-wrong`（入库同时记一条 `WRONG` 日志）、`--learner TEXT`（默认 `default`）、`--note TEXT`、`--dry-run`。
```bash
shuati ingest-mistake --draft-id drf_xxxx --log-wrong --learner xiaoming --json
```

#### 47. `shuati override <uid>`
对单道题目写入人工权威覆写、撤回覆写或重确认源题微调后被挂起的覆写（物理隔离机器层，重编永不丢失）。
- **位置参数与业务选项**：`<uid>`、`--field TEXT`（必填，支持验真类 `question_text` / `visual_facts` / `final_answers_json` / `solutions_override_json` / `verification_status` / `format` / `is_excluded`，标注类 `difficulty_override_json` / `abstract_skeleton` / `thinking_methods_json` / `classic_motifs_json` / `trap_tags_json`，词表类 `primary_l1_module` / `secondary_l1_modules_json` / `taxonomy_override_json`）、`--value TEXT`、`--reason TEXT`、`--revoke`（撤回当前字段覆写）、`--reconfirm`（重新确认挂起覆写）、`--source TEXT`（默认 `PI_CLI`）、`--dry-run`。
```bash
shuati override ch05_012 --field is_excluded --value "1" --reason "原题印刷残缺" --json
```

#### 48. `shuati human reset`
无损归档既有 `human/*.jsonl` 至 `data/backups/`，并初始化为干净的单学习者起跑态。
- **业务选项**：`--learner-id TEXT`（默认 `default`）、`--learner-name TEXT`（默认 `"默认学习者"`）、`--subject TEXT`（默认 `小学奥数`）、`--archive-dir PATH`（默认 `data/backups`）、`--dry-run`。
```bash
shuati human reset --learner-id alice --learner-name "Alice" --subject "小学奥数" --json
```

#### 49. `shuati human restore`
从 `human/*.jsonl`（含外部错题基础行、学习者、人工覆写与做题日志）执行事务级幂等 `UPSERT` 安全恢复并重算生效层。
- **业务选项**：`--from PATH`（默认读取 `settings.yaml` 的 `human_mirror.dir`）、`--force-wipe-logs`（快照日志为空时强制清空库内已有日志）、`--backups-dir PATH`、`--dry-run`。
```bash
shuati human restore --json
```

#### 50. `shuati human verify`
校验 SQLite 数据库内人工资产表与磁盘 `human/*.jsonl` 5 份快照的 SHA-256 哈希一致性。
- **业务选项**：`--from PATH`。
```bash
shuati human verify --json
```

#### 51. `shuati export`
一键导出 4 份完全脱离 SQLite 的开放标准 JSONL 资产（`questions_enriched.jsonl`、`knowledge_nodes.jsonl`、`knowledge_edges.jsonl`、`question_relations.jsonl`）。
- **业务选项**：`--grade-subject TEXT`、`--export-dir PATH`（默认 `data/exports`）、`--include-retired`（包含已下架/屏蔽题目）。
```bash
shuati export --grade-subject "小学奥数" --export-dir data/exports --json
```

---

## 六、系统架构与权威文档索引

系统内部四层架构、九条硬不变量、20 张 SQLite 表完整 DDL、难度合成与在线水位完整数学公式及多智能体编排规范，请直接查阅仓内权威文档：

- **顶层架构总纲（最高裁决权威）**：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **详细实施规范（SQLite DDL / 公式参数 / CLI 信封 / HTML 渲染细则）**：[`docs/DESIGN.md`](docs/DESIGN.md)
- **原始业务需求规格书**：[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md)
- **非对称难度体系与五边关联拓扑理论溯源**：[`docs/理论基础.md`](docs/理论基础.md)
- **会话协作入口与工程经验沉淀**：[`AGENTS.md`](AGENTS.md)、[`.agents/rules/shuati_ops_lessons.md`](.agents/rules/shuati_ops_lessons.md)
- **内置三套 AI Skill 详细操作规程**：
  - 首次克隆冷启动与新学段定标：[`skills/shuati-init/SKILL.md`](skills/shuati-init/SKILL.md)（配置模板见 [`setup_templates.md`](skills/shuati-init/references/setup_templates.md)）
  - 日常四路增量编译与词表锁表：[`skills/shuati-compiler/SKILL.md`](skills/shuati-compiler/SKILL.md)
  - 日常防剧透自适应刷题与诊断：[`skills/shuati/SKILL.md`](skills/shuati/SKILL.md)
