# 刷题 Harness v2

一套**纯理科智能题库编译器与个性化刷题系统**。它能把零散的原始题库编译为严密的知识网络，并以此为底座实现错题深度诊断、学习者动态水平画像与按需智能组卷。

---

## 一、核心功能与价值

1. **将海量原始题库编译为严密知识网**
   - 直接接入无标准答案、带几何/物理配图、标签杂乱的本地题库（保持原目录只读），自动看图解题、用数学沙盒验算答案，并拆解出每道题的推理步骤与精细难度。
   - 自动从全库题目中归纳出“主干模块 → 核心模型 → 解题步骤算子”三级知识树及先修依赖关系，并在题目之间织出五类关联边：**孪生换数题（`CLONE`）、换皮同构题（`ISOMORPHIC`）、阶梯子母题（`SCAFFOLD`）、形似易混题（`CONTRASTIVE`）与同坑易错题（`SHARED_TRAP`）**。
2. **根据知识网深度分析错题、举一反三**
   - 无论是题库内做错的题，还是从外部随手拍照/整理的全新错题，系统都能定位它卡在哪个知识点、属于哪个经典母题、掉进了哪类数理陷阱。
   - 顺着知识网一键找出配套的**降阶铺垫题**（先补齐前置短板）和**同构/同坑变式题**（换个故事背景检验是否真懂），并支持将外部好题一键收入题库。
3. **为学习者构建动态水平画像**
   - 支持多个学习者共用同一套题库、各自独立记录做题足迹。
   - 根据历史做题表现，实时算出每个人在各个知识模块的能力水位，直观区分他是**“怕计算繁琐”**还是**“怕思维技巧”**，并汇总高频薄弱考点与易错盲区雷达。
4. **按需智能组卷与防剧透伴学**
   - 围绕学习者当前水平或指定薄弱模块，按“稳固基础 + 适度拔高 + 易错排雷”比例一键生成专题小卷（自动避开近期做过的题和孪生题）。
   - 支持在终端或 AI 助手（**AGY** / **`pi`**）中进行**防剧透苏格拉底式讲题**（先给分级提示、不泄密判对错），也可一键导出为**自包含离线 HTML 练习单**（电脑屏幕点击折叠看提示，或浏览器 `Ctrl+P` 打印出带作图演算留白的纸质卷）。

> 非对称难度分级与五边关联网络的理论设计详见 [`docs/理论基础.md`](docs/理论基础.md)。

---

## 二、核心使用场景（Use Cases）

> **快速准备**：需 Python `>= 3.11`。运行 `uv sync --extra dev --extra mcp` 安装环境，配置 `.env` 后运行 `shuati doctor` 检查连通性（在 **AGY** 中打开仓库即可直接用自然语言让 AI 代劳；**`pi`** 用户运行一次 `shuati skill install --all` 即可挂载全部技能。详细环境与模型配置见 [`docs/CLI_GUIDE.md`](docs/CLI_GUIDE.md#一环境模型与外部题源配置细则)）。

### Use Case 1：编译题库（把原始题目变成知识网）

首次接入一套外部题库（`problems.jsonl + images/`）时，按顺序完成清洗入库、难度基准定标、全库解题拆解与知识网编织：

```bash
# 1. 初始化干净的个人学习档案，并将外部题库清洗去重入库
shuati human reset --learner-id alice --learner-name "Alice" --subject "小学奥数"
shuati ingest --dedup-near

# 2. 抽样 6 道候选题验真，挑选 4~5 道代表题写入 anchors 配置后锁定难度标尺
shuati anchors sample --grade-subject "小学奥数" --count 6
shuati compile --stage 1 --grade-subject "小学奥数" --only-uids "uid1,uid2,uid3,uid4,uid5,uid6"
shuati unified calibrate-anchors --grade-subject "小学奥数"

# 3. 批量看图解题验真（Stage 1）并拆解步骤、提示与难度（Stage 2）
shuati compile --stage 1 --grade-subject "小学奥数"
shuati compile --stage 2 --grade-subject "小学奥数"

# 4. 归纳生成知识树词表、同步语义向量，并一键完成知识点挂载、先修图与题目关联织网
shuati taxonomy propose --grade-subject "小学奥数"
shuati reindex --vectors --quantiles --grade-subject "小学奥数"
shuati compile --stage all --grade-subject "小学奥数"

# 5. 启动本地网页快速复核少量存疑题，并生成全库质量体检报告
shuati review --grade-subject "小学奥数"
shuati audit --grade-subject "小学奥数"
```

> 免推理 API Key 的子代理批编译（`export / import`）与日常增量加题/锁表流程详见 [`docs/CLI_GUIDE.md`](docs/CLI_GUIDE.md#二题库编译进阶指南完整冷启动双通道与增量演进)。

### Use Case 2：根据知识网分析错题与举一反三

当某道题做错、或遇到题库外的全新错题时，利用知识网定位原因并拉取针对性训练题：

```bash
# 场景 A：题库内的题做错了 -> 顺着知识网找“降阶铺垫子题（pre）”先打通卡点，再找“同构/同坑变式”巩固
shuati related ch05_012 --types SCAFFOLD --direction pre --exclude-solved
shuati related ch05_012 --types ISOMORPHIC,CONTRASTIVE,SHARED_TRAP

# 场景 B：遇到题库外的全新错题（支持附上手机拍的题目照片）-> 实时诊断考点并推荐库内关联题
shuati diagnose --text "如图，正方形边长为10，求阴影面积..." --image ./mistake.jpg --grade-subject "小学奥数"

# 场景 C：批量分析整理好的错题 Markdown 文档，并将有价值的外部错题一键正式收入题库
shuati diagnose-preflight --file ./mistakes.md
shuati diagnose --file data/drafts/preflight/mistakes.normalized.md --grade-subject "小学奥数"
shuati ingest-mistake --draft-id drf_xxxx --log-wrong --learner alice
```

### Use Case 3：管理学习者并生成水平画像

记录日常做题对错与卡点步骤，随时查看各模块的真实掌握水位与薄弱点：

```bash
# 1. 创建或切换当前学习者角色
shuati learner create xiaoming --name "小明" --subject "小学奥数" --current
shuati learner use xiaoming

# 2. 登记单题或整套试卷的做题结果（SOLVED_SOLO / SOLVED_WITH_HINT / WRONG / SKIPPED）
shuati log ch05_012 --outcome WRONG --stuck-step 2 --note "没看出等高模型面积比"
shuati log-batch --quiz-batch-id qz_20261124_xxxx --entries @outcomes.json

# 3. 查看该学习者的分模块水平画像（能力水位、怕繁/怕巧对比、高频易错点）与知识树掌握热力
shuati profile --grade-subject "小学奥数"
shuati taxonomy --grade-subject "小学奥数" --learner xiaoming --l1 "平面几何"
```

### Use Case 4：按需智能组卷、防剧透刷题与打印练习单

按目标模块或薄弱考点一键组卷，支持在终端边拿提示边做，或导出排版精美的练习单：

```bash
# 1. 按当前水平自动组一套 5 题几何小卷（默认 2 道稳固 + 2 道拔高 + 1 道薄弱排雷）
shuati compose-quiz --grade-subject "小学奥数" --l1 "平面几何" --count 5 --mix "2:2:1"

# 2. 生成离线单文件 HTML 练习单（屏幕打开可点击折叠看提示；浏览器 Ctrl+P 可直接打印带留白纸质卷）
shuati render-sheet --quiz-batch-id qz_20261124_xxxx --title "平面几何周末巩固卷"

# 3. 终端防剧透伴学：先看题面 -> 卡壳时要第 1 级提示 -> 交答案不泄密判对错 -> 事后看完整解析
shuati detail ch05_012 --reveal quiz
shuati detail ch05_012 --reveal hint --hint-level 1
shuati check ch05_012 --answer "24" --hint-level 1 --log
shuati detail ch05_012 --reveal full
```

---

## 三、全部系统命令总览（51 个子命令）

> 所有命令均支持附加 `--json` 输出结构化数据。各命令的**完整参数枚举、选项默认值与进阶示例**请查阅 [`docs/CLI_GUIDE.md`](docs/CLI_GUIDE.md#三全量-cli-命令参数字典51-个子命令)。

### 1. 题库导入与知识网编译（15 个）

| 命令 | 常用调用示例 | 一句话作用 |
| :--- | :--- | :--- |
| `shuati grade-subject list` | `shuati grade-subject list` | 查看全部已注册的学段与学科状态 |
| `shuati grade-subject add` | `shuati grade-subject add --name "初中物理" --slug "junior_physics" --family PHYSICS` | 注册新的学段学科作用域 |
| `shuati ingest` | `shuati ingest --dedup-near` | 扫描外部只读题库、清洗文本配图并去重入库（加 `--dry-run` 只读预检） |
| `shuati prompt list` | `shuati prompt list` | 列出默认与各学科的提示词配置清单 |
| `shuati prompt render` | `shuati prompt render --stage unified --grade-subject "小学奥数"` | 预览合并后的阶段提示词与输出结构 |
| `shuati prompt lint` | `shuati prompt lint` | 校验所有提示词配置文件的语法与槽位完整性 |
| `shuati anchors sample` | `shuati anchors sample --grade-subject "小学奥数" --count 6` | 分层随机抽取难度定标候选基准题 |
| `shuati unified calibrate-anchors` | `shuati unified calibrate-anchors --grade-subject "小学奥数"` | 加载黄金难度基准题，解锁后续深度拆解 |
| `shuati compile` | `shuati compile --stage 1 --grade-subject "小学奥数"` | 按阶段运行编译流水线（`--stage 1\|2\|3c\|3d\|3e\|4\|all`） |
| `shuati unified export` | `shuati unified export --grade-subject "小学奥数" --batch-size 10` | 导出免推理 API Key 的子代理批处理任务包 |
| `shuati unified import` | `shuati unified import --results data/unified/results.jsonl` | 校验并回灌子代理批处理结果入库 |
| `shuati tool verify-sympy` | `shuati tool verify-sympy --input @sympy.json` | 零数据库无状态运行 SymPy 数学沙盒验算 |
| `shuati tool validate-draft` | `shuati tool validate-draft --mode UNIFIED_S1_S2 --input @draft.json` | 零数据库无状态校验题目拆解草稿是否合规 |
| `shuati review` | `shuati review --grade-subject "小学奥数"` | 启动本地极简键盘复核网页，人工拍板处理存疑题 |
| `shuati audit` | `shuati audit --grade-subject "小学奥数"` | 生成涵盖验真率、难度分布与图谱健康度的全库体检报告 |

### 2. 知识词表与图谱拓扑治理（9 个）

| 命令 | 常用调用示例 | 一句话作用 |
| :--- | :--- | :--- |
| `shuati taxonomy` | `shuati taxonomy --grade-subject "小学奥数" --learner alice` | 浏览学段知识目录树、先修依赖与指定学习者的掌握热力 |
| `shuati taxonomy propose` | `shuati taxonomy propose --grade-subject "小学奥数"` | 从全库解题步骤中自动聚类归纳出候选知识树词表 |
| `shuati taxonomy candidates` | `shuati taxonomy candidates --grade-subject "小学奥数"` | 查看增量加题时发现的待审新知识点候选 |
| `shuati taxonomy lock` | `shuati taxonomy lock --grade-subject "小学奥数" --to-version 1.0` | 将审定后的知识词表锁定为正式版本 |
| `shuati taxonomy migrate` | `shuati taxonomy migrate --grade-subject "小学奥数" --file new.yaml` | 按变更记录平滑升级已锁定的正式知识词表 |
| `shuati graph compact` | `shuati graph compact --grade-subject "小学奥数"` | 归并同一模块下同义的细粒度解题步骤算子与概念 |
| `shuati graph revert-merge` | `shuati graph revert-merge --batch-id cmp_xxxx` | 按批次无损拆回（回滚）已归并的知识点算子 |
| `shuati graph check-dag` | `shuati graph check-dag --grade-subject "小学奥数"` | 检查知识点先修依赖图是否存在循环依赖 |
| `shuati graph calibrate-eval` | `shuati graph calibrate-eval --propose-candidates` | 评估或抽样校准题目间五类关联边的匹配阈值 |

### 3. 错题诊断与关联找题（5 个）

| 命令 | 常用调用示例 | 一句话作用 |
| :--- | :--- | :--- |
| `shuati search` | `shuati search --l1 "平面几何" --motif "蝴蝶模型" --exclude-solved` | 按知识点、母题、陷阱、难度、关键词或解题思路语义搜题 |
| `shuati related` | `shuati related ch05_012 --types SCAFFOLD --direction pre` | 顺着知识网查找某题的降阶铺垫题、升维母题、同构题或同坑题 |
| `shuati diagnose-preflight` | `shuati diagnose-preflight --file ./mistakes.md` | 预检并自动规范化批量错题 Markdown 文档格式 |
| `shuati diagnose` | `shuati diagnose --text "题干..." --image ./q.jpg --grade-subject "小学奥数"` | 实时分析题库外的单道或批量新错题，定位卡点并推荐库内关联题 |
| `shuati ingest-mistake` | `shuati ingest-mistake --draft-id drf_xxxx --log-wrong` | 将诊断过的外部新错题一键正式收入题库并织入知识网 |

### 4. 学习者管理、水平画像与智能刷题（11 个）

| 命令 | 常用调用示例 | 一句话作用 |
| :--- | :--- | :--- |
| `shuati learner list` | `shuati learner list` | 查看全部学习者角色及当前激活状态 |
| `shuati learner create` | `shuati learner create xiaoming --name "小明" --current` | 创建新的学习者角色 |
| `shuati learner use` | `shuati learner use xiaoming` | 切换当前默认学习者角色 |
| `shuati profile` | `shuati profile --grade-subject "小学奥数"` | 查看学习者在各模块的能力水位、难度阶梯通过率与薄弱点雷达 |
| `shuati compose-quiz` | `shuati compose-quiz --l1 "平面几何" --count 5 --mix "2:2:1"` | 按学习者当前水平一键智能组出“稳固+拔高+排雷”专题小卷 |
| `shuati render-sheet` | `shuati render-sheet --quiz-batch-id qz_xxxx` | 将小卷或题单导出为可屏幕折叠刷题、可纸笔打印留白的 HTML 练习单 |
| `shuati detail` | `shuati detail ch05_012 --reveal hint --hint-level 1` | 查看单题题面（`quiz`）、分级提示（`hint`）或完整解析（`full`） |
| `shuati check` | `shuati check ch05_012 --answer "24" --log` | 在不泄露标准答案的前提下核对作答正误（支持多小问与自动记入足迹） |
| `shuati log` | `shuati log ch05_012 --outcome WRONG --stuck-step 2` | 手动登记单道题的做题结果与卡点步骤 |
| `shuati log-batch` | `shuati log-batch --quiz-batch-id qz_xxxx --entries @outcomes.json` | 线下做完纸质卷后，按批次整卷批量回填做题记录 |
| `shuati log-void` | `shuati log-void 42 --reason "录错题号"` | 冲销（作废）误记的做题记录 |

### 5. 人工纠错、数据备份恢复与环境维护（11 个）

| 命令 | 常用调用示例 | 一句话作用 |
| :--- | :--- | :--- |
| `shuati override` | `shuati override ch05_012 --field visual_facts --value "..."` | 手工修正单道题的答案、读图描述、标签或屏蔽坏题（重编永不丢失） |
| `shuati human reset` | `shuati human reset --learner-id alice --subject "小学奥数"` | 归档旧个人数据，初始化干净的单学习者起跑环境 |
| `shuati human restore` | `shuati human restore` | 换机或重编后，从 `human/*.jsonl` 备份完整恢复人工修正、错题与做题记录 |
| `shuati human verify` | `shuati human verify` | 校验数据库中的人工数据与磁盘 `human/*.jsonl` 备份是否完全一致 |
| `shuati export` | `shuati export --export-dir data/exports` | 导出脱离数据库的 4 份开放标准 JSONL（题目、知识节点、先修边、题目关联边） |
| `shuati status` | `shuati status` | 查看全库各阶段编译进度、词表状态与下一步操作建议 |
| `shuati reindex` | `shuati reindex --fts --effective --vectors --quantiles` | 重建全文检索索引、生效层数据、语义向量矩阵或分位表 |
| `shuati doctor` | `shuati doctor --check-llm-api` | 一键体检运行环境、数学沙盒、外部题库路径与模型/向量服务连通性 |
| `shuati skill install` | `shuati skill install --all` | 将内置三套 AI 技能同步安装到 `.pi/skills/` 目录 |
| `shuati mcp serve` | `shuati mcp serve --role practice` | 启动本地 `stdio` MCP 服务供通用 AI 客户端调用 |
| `shuati version` | `shuati version` | 查看当前系统版本号 |

---

## 四、深入阅读与技术文档导航

- **配置模板、双通道/增量编译与 51 命令全参数手册**：[`docs/CLI_GUIDE.md`](docs/CLI_GUIDE.md)
- **非对称四维难度模型与“剥皮抽骨”五边拓扑理论**：[`docs/理论基础.md`](docs/理论基础.md)
- **系统架构总纲与核心设计不变量**：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **数据库 Schema、公式参数与详细实现规范**：[`docs/DESIGN.md`](docs/DESIGN.md)
- **业务需求规格书与 AI 协作入口**：[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md)、[`AGENTS.md`](AGENTS.md)
