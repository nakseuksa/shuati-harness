---
name: shuati-init
description: Guides first-time git clone cold-start setup, virtual environment bootstrapping, fresh-repo vs machine-migration recovery routing, mounting external read-only problems.jsonl question banks, configuring LLM/Swarm and Embedding providers via shuati doctor, calibrating Stage 1.5 golden difficulty anchors for new grade subjects, running a 20-25 question smoke gate, and executing initial DRAFT knowledge graph compilation.
---

# Shuati 克隆冷启动与新学段初始化向导 (`shuati-init`)

当用户首次 `git clone` 本仓库后请求初始化环境、接入第一批外部 `**/problems.jsonl + images/` 题库、配置模型与向量服务，或由 `shuati-compiler` 在接入全新学段学科时桥接调用，启用本 Skill。

> 配置与文件模板按需查阅 [references/setup_templates.md](references/setup_templates.md)，避免一次性展开占用主对话上下文。

## 全局安全铁律

1. **外部题源严格只读、零拷贝**：用户磁盘上的原始题库目录仅通过 `config/settings.yaml -> ingest.roots` 挂载扫描，严禁拷贝几 GB 原始图片或修改原始 `problems.jsonl` / `md/`。
2. **CLI 单写者与零 DB Worker 隔离**：所有 `data/shuati.db` 写入与 SymPy 终审均由 `shuati` CLI 完成；走 AGY Swarm 模式时，子代理严格锁模（默认 `gemini-4-neon-medium`，禁止 `run_workflow` 退化），且仅可调用 `--role worker` 零 DB 工具（`verify_sympy` / `validate_draft`）自检。
3. **Windows 沙盒守则**：涉及写入 `data/shuati.db`、持 `.compile.lock` 或 SymPy 沙盒（Job Object）的命令，按工程规范申请沙盒外执行；所有 `shuati` 命令均加 `--json` 并检查返回的 `{ok, data, warnings, error}` 信封。

---

## 六阶段交互状态机（Step 0 ～ Step 5）

按顺序推进；**Step 0、Step 2 体检闸门、Step 3 一屏拍板、Step 4 烟测闸门**均为必须向用户确认或实测通过的关键检查点。

### Step 0：`.venv` 环境自举与克隆身份二选一分流

1. **环境检查与依赖安装**：
   - 检查 `.venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（Linux/macOS）是否存在；
   - 缺失时优先运行 `uv sync --extra dev`（若需 MCP 能力补 `--extra mcp`）；无 `uv` 则运行 `python -m venv .venv` 并用 `.venv` 内 `pip install -e ".[dev]"`。
2. **克隆态身份检测与二选一分流**：
   - 检查 `data/shuati.db` 是否不存在且仓内 `human/*.jsonl`（如 `learners.jsonl` / `overrides.jsonl`）非空；
   - 若满足上述条件，**必须主动向用户二选一确认**（严禁直接沿用仓库自带个人学习档案）：
     - **分支 A — 全新用户自建题库**：
       1. 询问用户的默认学习者 ID（如 `alice` 或 `default`）、昵称与首个目标学段学科（如 `小学奥数`、`初中物理`）；
       2. 执行净仓初始化（可先加 `--dry-run` 预览）：
          ```bash
          shuati human reset --learner-id <id> --learner-name "<昵称>" --subject "<学段学科>" --json
          ```
       3. 该命令会自动将仓内原有 `human/*.jsonl` 无损归档至 `data/backups/seed_human_<timestamp>/` 并生成干净的 5 份镜像与用户专属角色，随后进入 **Step 1**。
     - **分支 B — 原库换机恢复（已有自己的 `human/*.jsonl` 备份）**：
       1. 询问原磁盘题库根目录路径，写入 `config/settings.yaml` 的 `ingest.roots`；
       2. 恢复题源与 CAS 图片：`shuati ingest --dedup-near --json`；
       3. 恢复学习档案与覆写：`shuati human restore --json`；
       4. 按 **Step 2** 配妥 `.env` / Embedding 并跑通 `shuati doctor --json`；
       5. 一步重建全文索引、生效层与向量分位表：
          ```bash
          shuati reindex --fts --effective --vectors --quantiles --json
          ```
       6. 恢复完成，直接交接给 [`../shuati/SKILL.md`](../shuati/SKILL.md) 开始日常刷题。

### Step 1：外部题源核验、学段注册与 Stage 0 入库

1. **格式核验（只读）**：请用户提供外部题库磁盘目录（如 `D:/题库/初中物理`），抽检 `**/problems.jsonl` 首行确认存在 `id`、`question`、`images` 字段，且相对路径配图在磁盘真实可读。
2. **注册新学段（如尚未注册）**：
   - 先运行 `shuati grade-subject list --json` 核查；若为新学段，调用：
     ```bash
     shuati grade-subject add --name "<学段学科>" --slug "<slug>" --family <MATH|PHYSICS|CHEMISTRY|STEM_OTHER> --json
     ```
3. **挂载路径与集合规则**：
   - 将题库根目录写入 `config/settings.yaml` 的 `ingest.roots` 列表（**若本地不存在 `./题库` 目录，务必替换掉默认的 `"题库"` 占位项，切勿并列保留**，参考 [references/setup_templates.md](references/setup_templates.md) §2）；
   - 按目录相对路径规律更新 `config/collections.yaml`（参考 [references/setup_templates.md](references/setup_templates.md) §3）。
4. **只读预检 → 正式入库**：
   ```bash
   shuati ingest --dry-run --dedup-near --json
   ```
   确认 `unmapped_collections` 为空且无异常缺失/坏图告警后，执行正式入库：
   ```bash
   shuati ingest --dedup-near --json
   ```

### Step 2：推理通道 + Embedding 配置与 `shuati doctor` 闸门

1. **引导选择推理执行模式**：
   - **① AGY Swarm 子代理免 Key 模式（默认推荐）**：无需配置 `VLM_PRIMARY_API_KEY` / `VLM_SECONDARY_API_KEY` / `TEXT_LLM_API_KEY`，由主会话按导出 Manifest 精确锁模 `gemini-4-neon-medium` 派发 Swarm 子代理完成编译（可在 `config/settings.yaml -> subagent.default_model` 调整）。
   - **② `.env` OpenAI 兼容 API 直连模式**：从 `.env.example` 复制创建 `.env`，填写 `VLM_PRIMARY_*`、`VLM_SECONDARY_*`、`TEXT_LLM_*`（`JUDGE_LLM_*` 留空则回退复用 `TEXT_LLM`）。
2. **引导选择 Embedding 向量方案（三选一，模板见 [references/setup_templates.md](references/setup_templates.md) §1）**：
   - **云端 OpenAI 兼容 API**（如阿里云百炼 DashScope / OpenAI）：填 `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`；
   - **本地 Ollama 免 Key**：`EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1`（或 `localhost`），`EMBEDDING_API_KEY=` 留空即可；
   - **本地 `local_st`**：在 `config/settings.yaml` 将 `llm.embedding.provider` 设为 `"local_st"`。
3. **强制运行 `shuati doctor` 闸门**：
   ```bash
   # AGY Swarm 模式
   shuati doctor --json
   # 若选择 .env API 直连模式，追加 --check-llm-api
   shuati doctor --check-llm-api --json
   ```
   **必须确认** `checks.sympy_sandbox`、`checks.prompt_seams`、`checks.ingest_roots` 与 `checks.embedding`（真实发送 1 条文本请求并返回 `dimension`）全部 `ok == true` 后，方可进入 Step 3。

### Step 3：学段提示词接缝 + 5 题黄金锚点一屏拍板（解锁 Stage 1.5）

1. **生成/微调提示词接缝**：
   - 检查 `config/prompts/subjects/<slug>.yaml`；若为新学段，按 [references/setup_templates.md](references/setup_templates.md) §4 新建文件（明确学科公式约定、禁止超纲工具与 `raw_l3` 示例）；
   - 运行 `shuati prompt lint --json`（必须无报错）及 `shuati prompt render --stage unified --grade-subject "<name>" --json` 抽样核验。
2. **分层抽样并完成 Stage 1 验真（解开锚点冷启动死锁）**：
   - 调用分层抽样器抽取 5～6 道覆盖不同长度、配图与隐藏章节的候选题：
     ```bash
     shuati anchors sample --grade-subject "<name>" --count 6 --json
     ```
   - 仅针对这批候选 UID 先跑 **Stage 1 验真**（保证 `eff_verification_status == VERIFIED_CONSENSUS`）：
     - **AGY Swarm 通道**：
       1. `shuati compile --stage 1 --grade-subject "<name>" --only-uids "<uid1>,<uid2>,..." --mode export --json`
       2. 读取 `data/unified/stage3_4/stage1/stage1_manifest.json` 的 `subagent_model`（默认 `gemini-4-neon-medium`），派发 Swarm Worker 读批任务与 CAS `image_paths` 求解，并强制 Worker 调用 `--role worker` `verify_sympy`（或 `shuati tool verify-sympy --input @payload.json --json`）自检通过后写出结果 JSONL；
       3. `shuati compile --stage 1 --grade-subject "<name>" --mode import --results data/unified/stage3_4/stage1/results.jsonl --json`
       4. 清理临时 Worker：`swarm(action="kill", ...)`。
     - **API 直连通道**：
       ```bash
       shuati compile --stage 1 --grade-subject "<name>" --only-uids "<uid1>,<uid2>,..." --mode api --json
       ```
3. **一屏展示锚点候选表请用户拍板**：
   - 从已验真候选（或用户自选题目）挑选 **4～5 道跨梯度代表题**（覆盖 `L1_基础直达`、`L3_综合中档`、`L4-A_进阶拔高(繁)` / `L4-B_进阶拔高(巧)`、`L5-1_压轴挑战` / `L5-2_竞赛超难`），按 D4 公式计算 `base_elo` 与 `target_tier`（公式与 8 档表见 [references/setup_templates.md](references/setup_templates.md) §5）；
   - **在聊天界面一屏集中展示表格**（`序号 | question_uid | 题干摘要 | target_tier | C | S | T | L | base_elo | rationale`），请用户确认或微调后落盘至 `config/anchors/anchors_<slug>.yaml`。
4. **注册解锁**：
   ```bash
   shuati unified calibrate-anchors --grade-subject "<name>" --json
   ```
   确认 `anchor_count >= 4` 且该学段 `anchors_ready = 1`。

### Step 4：20～25 题烟测闸门（Smoke Gate）

- **触发条件**：当前学段有效题量 **`> 30`** 时必须执行本步（`<= 30` 题可征求用户同意后直接进入 Step 5）。
- **执行流程**：
  1. 抽取 20～25 道跨章节、包含配图题的代表样本（可用 `shuati search --grade-subject "<name>" --limit 25 --json` 选取 UID），用 `--only-uids` 试跑 Stage 1+2：
     - **AGY Swarm 通道**：`shuati unified export --grade-subject "<name>" --only-uids "<uids>" --batch-size 10 --json` → 按 `unified_manifest.json` 锁模 `gemini-4-neon-medium` 派发 Swarm + Worker `validate_draft(run_sympy=True)` 自检 → `shuati unified import --results ... --task-map data/unified/task_map.json --json`（若有极个别纯 SymPy 疑难题可按需加 `--final`）；
     - **API 直连通道**：`shuati compile --stage 1 --grade-subject "<name>" --only-uids "<uids>" --mode api --json` + `shuati compile --stage 2 --grade-subject "<name>" --only-uids "<uids>" --mode api --json`。
  2. **一屏向用户汇报**：SymPy 验真率、`NEEDS_REVIEW` 数量、8 档难度分布，以及 **2～3 道完整拆解样例**（含 `visual_facts`、`sub_goal`、`hint`、`raw_l3`、`abstract_skeleton`、`C/S/T/L`）；
  3. **提示词接缝快速迭代**：如用户对 `hint` 风格、解法约定或 `raw_l3` 颗粒度不满意，立即修订 `config/prompts/subjects/<slug>.yaml` → `shuati prompt lint --json` → 对烟测样本 `--force` 重跑验证，直至用户确认放行。

### Step 5：首轮放量编译至 `DRAFT` 全图谱就绪态

按用户确认的放量范围（全学段或首批指定章节）推进完剩余 Stage 1～4（AGY Swarm 通道全程严格遵循 Manifest 锁模与 `--role worker` 自检，并在最后清理 Worker；`--mode api` 通道直接顺序调用）：

1. **Stage 1+2 放量**：
   - Swarm：`shuati unified export --grade-subject "<name>" --batch-size 10 --json` → Swarm 锁模批跑 → `shuati unified import --results ... --json`（处理 `retry_tasks.jsonl` 直至清零）；
   - API：`shuati compile --stage 1 --grade-subject "<name>" --mode api --json` → `shuati compile --stage 2 --grade-subject "<name>" --mode api --json`。
2. **Stage 3a（生成 `DRAFT` 词表）**：
   - `shuati taxonomy propose --grade-subject "<name>" --mode export|api --json`（`export` 通道由 Swarm 完成后 `--mode import`）。
3. **Stage 3c（词表映射）**：
   - `shuati compile --stage 3c --grade-subject "<name>" --mode export|api --json`。
4. **Stage 3d（同域挂载 + `Cluster-then-Assign` 归并）**：
   - `shuati compile --stage 3d --grade-subject "<name>" --json`
   - `shuati graph compact --grade-subject "<name>" --mode export|api --json`。
5. **Stage 3e（先修 DAG 构建与强无环校验）**：
   - `shuati compile --stage 3e --grade-subject "<name>" --mode export|api --json`
   - `shuati graph check-dag --grade-subject "<name>" --json`。
6. **Stage 4（向量同步 + 五边拓扑织网）**：
   - `shuati reindex --vectors --quantiles --grade-subject "<name>" --json`
   - `shuati compile --stage 4 --grade-subject "<name>" --mode export|api --json`。
7. **Stage Q 全库体检与交接**：
   - 若使用了临时 Swarm Worker，执行 `swarm(action="kill", ...)` 全部释放；
   - 运行 `shuati audit --grade-subject "<name>" --json` 输出质量体检摘要；
   - 告知用户已完成首轮 `DRAFT` 全图谱编译：
     - 日常组卷刷题与错题诊断：启用 [`../shuati/SKILL.md`](../shuati/SKILL.md)；
     - 复核少量 `NEEDS_REVIEW` 分歧题：运行 `shuati review --grade-subject "<name>"`；
     - 后续加新题、改错题、加新学段或锁表 `LOCKED 1.0`：启用 [`../shuati-compiler/SKILL.md`](../shuati-compiler/SKILL.md)。
