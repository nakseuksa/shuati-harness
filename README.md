# 刷题 Harness v2

纯理科题库知识编译器与 `pi` 智能刷题底座 —— 将外部只读 `problems.jsonl + images/` 题库编译为经 SymPy 形式化验真、四维正交难度定标、原子步骤归并、先修 DAG 与五边拓扑织网驱动的可进化自适应知识图谱，并向 **AGY（Antigravity）**、**`pi`** 及各类 AI 编程客户端原生提供结构化 CLI / Skill / MCP 能力。

---

## 一、快速冷启动（`git clone` 后 3 步开跑）

### 1. 准备环境与外部题库

- **运行环境**：Python `>= 3.11`，推荐安装 [`uv`](https://docs.astral.sh/uv/)。
- **外部题库格式**：在本地磁盘准备标准目录（支持多目录挂载，系统**严格保持原目录只读、零拷贝图片**），目录内包含 `**/problems.jsonl`（含 `id`、`question`、`images` 等字段）与对应配图文件。

```bash
git clone <your-repo-url> shuati-harness
cd shuati-harness

# 推荐：使用 uv 创建 .venv 并安装依赖（如需 MCP 服务端加 --extra mcp）
uv sync --extra dev --extra mcp

# 或使用标准 venv + pip
python -m venv .venv
# Windows: .venv\Scripts\python.exe -m pip install -e ".[dev,mcp]"
# Linux/macOS: .venv/bin/python -m pip install -e ".[dev,mcp]"
```

### 2. 挂载内置三套 Skill

仓库在 [`skills/`](skills/) 下内置三套互补 Skill，并按客户端提供零配置或一键挂载方式：

| 客户端 | 挂载方式 | 说明 |
| :--- | :--- | :--- |
| **AGY (Antigravity)** | **开箱自动发现（零配置）** | 仓内 [`.agents/skills.json`](.agents/skills.json) 已声明 `{"entries": [{"path": "skills"}]}`，克隆后用 AGY 打开仓库即可自动加载 `shuati-init`、`shuati-compiler`、`shuati` 三套 Skill。 |
| **`pi` 客户端** | **一条命令同步全量挂载** | 在仓库根目录运行：<br>`shuati skill install --all --json`<br>默认同步安装至 `.pi/skills/{shuati,shuati-init,shuati-compiler}`（优先创建目录软链，Windows 无软链权限时自动降级复制；支持 `--target-dir` 自定义目录或 `--skill <name>` 单装）。 |
| **其他 AI 编程助手 / 客户端** | **手动指向 `skills/` 或挂载 MCP** | 将仓内 `skills/` 目录（或其下三个子目录）添加到对应客户端的 Skills / Rules 配置路径中（详见 [`AGENTS.md`](AGENTS.md) 路由表）；若客户端支持 stdio MCP，也可按需挂载：<br>`shuati mcp serve --role practice`（刷题）/ `--role compiler`（编译）/ `--role worker`（零 DB 自检）/ `--role all`（全集）。 |

### 3. 开启三步工作流（冷启动 → 增量编译 → 日常刷题）

1. **首次克隆冷启动（触发 `shuati-init`）**：
   在 AGY 或 `pi` 对话中说明“初始化本项目题库”或“接入我的原始题库”，Agent 会自动按 [`skills/shuati-init/SKILL.md`](skills/shuati-init/SKILL.md) 引导完成：
   - **Step 0**：检查 `.venv` 并在检测到仓内 `human/*.jsonl` 时主动分流——**A. 全新用户自建题库**（`shuati human reset` 无损归档原档案并建干净角色） vs **B. 原库换机恢复**（`ingest` + `human restore` + `doctor` + `reindex` 一步还原）；
   - **Step 1**：核验外部 `problems.jsonl + images/`，写入 `config/settings.yaml` 的 `ingest.roots` 与 `config/collections.yaml`，`ingest --dry-run` 确认零未映射后正式入库；
   - **Step 2**：选择推理方式（**AGY Swarm 子代理免 Key 模式**默认 `gemini-4-neon-medium`，或 **`.env` OpenAI 兼容 API 直连模式**）与 Embedding（云端兼容 API / 本地回环 Ollama 免 Key / 本地 `local_st`），跑通 `shuati doctor --json` 实测发包体检；
   - **Step 3**：生成/微调 `config/prompts/subjects/<slug>.yaml`，用 `shuati anchors sample` 抽样并完成 Stage 1 验真，在聊天界面**一屏展示 4～5 道黄金锚点 `[C, S, T, L]` 请你拍板**后落盘 `calibrate-anchors` 解锁；
   - **Step 4**：题量 `> 30` 时先跑 20～25 题烟测闸门核对验真率与拆解样例；
   - **Step 5**：放量完成首轮 `DRAFT` 全图谱编译（Stage 1～4 + `audit`）。

2. **日常增量编译与词表演进（触发 `shuati-compiler`）**：
   后续每次追加新章节、修改源题勘误、新增全新学段（如初中物理），或将词表从 `DRAFT` 晋升为 `LOCKED 1.0`，由 [`skills/shuati-compiler/SKILL.md`](skills/shuati-compiler/SKILL.md) 执行开场双检（`shuati status --json` + `shuati ingest --dry-run --dedup-near --json`）并自动路由到 4 类最小化增量流程，全过程兼顾 AGY Swarm 精确锁模与 `--mode api` 双通道。

3. **日常自适应刷题与错题诊断（触发 `shuati`）**：
   直接向 Agent 提出按知识点刷题、薄弱摸底、梯度组卷、生成可打印练习单（`shuati render-sheet`）、防剧透提示与判题（`shuati check`）或录入新错题（`shuati diagnose` + `ingest-mistake`），由 [`skills/shuati/SKILL.md`](skills/shuati/SKILL.md) 执行。

---

## 二、常用 CLI 速查

> 所有 CLI 均支持 `--json`（stdout 输出标准 `{ok, command, data, warnings, error, meta}` JSON 信封）。

```bash
# 环境与模型连通体检（含 SymPy 沙盒、Prompt 接缝、外部题源与 Embedding 实测）
shuati doctor --json
shuati doctor --check-llm-api --json

# 态势巡检与增量入库预检
shuati status --json
shuati ingest --dry-run --dedup-near --json
shuati ingest --dedup-near --json

# 黄金锚点候选分层抽样与 Stage 1.5 定标
shuati anchors sample --grade-subject 小学奥数 --count 6 --json
shuati unified calibrate-anchors --grade-subject 小学奥数 --json

# 克隆净仓初始化 / 换机恢复 / 索引重建
shuati human reset --learner-id alice --learner-name "Alice" --subject "初中物理" --json
shuati human restore --json
shuati reindex --fts --effective --vectors --quantiles --json

# 人工复核台与质量体检
shuati review --grade-subject 小学奥数
shuati audit --grade-subject 小学奥数 --json

# 同步安装三套内置 Skill 至 .pi/skills/
shuati skill install --all --json
```

---

## 三、项目架构与权威文档

- **会话入口与协作规范**：[`AGENTS.md`](AGENTS.md)
- **顶层架构总纲（v2.6）**：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **详细实施规范（DDL / 公式 / CLI）**：[`docs/DESIGN.md`](docs/DESIGN.md)
- **需求与理论基线**：[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md)、[`docs/理论基础.md`](docs/理论基础.md)
- **Skill 详细规范**：[`skills/shuati-init/SKILL.md`](skills/shuati-init/SKILL.md)、[`skills/shuati-compiler/SKILL.md`](skills/shuati-compiler/SKILL.md)、[`skills/shuati/SKILL.md`](skills/shuati/SKILL.md)
