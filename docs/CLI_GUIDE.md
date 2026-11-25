# 刷题 Harness v2 — 详细配置、编译进阶与全量 CLI 参数手册

> 本文档收纳从根目录 [`README.md`](../README.md) 剥离的全部工程配置细节、免推理 Key 子代理（`export / import`）双通道编译流程、四类增量演进命令链，以及全系统 **51 个** CLI 子命令的完整参数字典。
> - **系统核心价值与快速上手**：返回 [`README.md`](../README.md)
> - **难度模型与五边关联拓扑理论**：参见 [`理论基础.md`](理论基础.md)
> - **系统架构总纲与详细设计**：参见 [`ARCHITECTURE.md`](ARCHITECTURE.md)、[`DESIGN.md`](DESIGN.md)

---

## 目录

- [一、环境、模型与外部题源配置细则](#一环境模型与外部题源配置细则)
- [二、题库编译进阶指南（完整冷启动、双通道与增量演进）](#二题库编译进阶指南完整冷启动双通道与增量演进)
  - [2.1 首次冷启动完整配置与阶段细节（`--mode api`）](#21-首次冷启动完整配置与阶段细节--mode-api)
  - [2.2 免推理 API Key 的 Subagent 双通道（`export / import`）](#22-免推理-api-key-的-subagent-双通道export--import)
  - [2.3 日常四类增量编译与词表演进](#23-日常四类增量编译与词表演进)
- [三、全量 CLI 命令参数字典（51 个子命令）](#三全量-cli-命令参数字典51-个子命令)
  - [3.0 全局通用约定（JSON 信封、退出码与通用路径参数）](#30-全局通用约定json-信封退出码与通用路径参数)
  - [3.1 环境、态势巡检、索引重建、Skill 与 MCP（6 个）](#31-环境态势巡检索引重建skill-与-mcp6-个)
  - [3.2 学段管理与原始题源入库（3 个）](#32-学段管理与原始题源入库3-个)
  - [3.3 提示词接缝、黄金难度锚点与零 DB 校验工具（7 个）](#33-提示词接缝黄金难度锚点与零-db-校验工具7-个)
  - [3.4 离线编译流水线、人工复核台与全库体检（5 个）](#34-离线编译流水线人工复核台与全库体检5-个)
  - [3.5 词表演进与知识图谱拓扑治理（9 个）](#35-词表演进与知识图谱拓扑治理9-个)
  - [3.6 学习者画像、多维检索、梯度组卷与防剧透刷题（13 个）](#36-学习者画像多维检索梯度组卷与防剧透刷题13-个)
  - [3.7 外部新错题接入、人工覆写、资产恢复与开放导出（8 个）](#37-外部新错题接入人工覆写资产恢复与开放导出8-个)

---

## 一、环境、模型与外部题源配置细则

### 1.1 Python 虚拟环境安装

要求 **Python `>= 3.11`**，推荐使用 [`uv`](https://docs.astral.sh/uv/)：

```bash
# 推荐：使用 uv 创建 .venv 并安装全部开发与 MCP 依赖
uv sync --extra dev --extra mcp

# 或使用标准 venv + pip
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\python.exe -m pip install -e ".[dev,mcp]"
# Linux / macOS:
.venv/bin/python -m pip install -e ".[dev,mcp]"
```

### 1.2 AI 客户端 Skill 与 MCP 挂载

仓库在 [`skills/`](../skills/) 下内置三套 Skill：[`shuati-init`](../skills/shuati-init/SKILL.md)（冷启动与新学段定标）、[`shuati-compiler`](../skills/shuati-compiler/SKILL.md)（四路增量编译与锁表）、[`shuati`](../skills/shuati/SKILL.md)（防剧透刷题与诊断）：

| 客户端环境 | 接入方式 | 命令 / 配置说明 |
| :--- | :--- | :--- |
| **AGY (Antigravity)** | **零配置自动发现** | 仓内 [`.agents/skills.json`](../.agents/skills.json) 已声明 `skills/` 路径，用 AGY 打开仓库即自动加载三套 Skill。 |
| **`pi` 客户端** | **一键同步挂载** | 运行 `shuati skill install --all --json`，自动软链（Windows 无权限时降级复制）至 `.pi/skills/`。 |
| **其他 AI 客户端** | **指向 `skills/` 或挂载 MCP** | 将 `skills/` 加入客户端规则路径，或挂载 `stdio` MCP：`shuati mcp serve --role practice`（刷题）/ `--role compiler` / `--role worker` / `--role all`。 |

### 1.3 `.env` 模型与 Embedding 向量服务配置

复制 [`.env.example`](../.env.example) 为 `.env`（完整模板见 [`setup_templates.md`](../skills/shuati-init/references/setup_templates.md)）：

1. **Embedding 向量服务（必配，三选一）**：
   - **云端 OpenAI 兼容 API**（如 OpenAI / 阿里云百炼 DashScope）：在 `.env` 填写 `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`；
   - **本地 Ollama 免 Key**：运行 `ollama serve` 并拉取模型（如 `bge-m3`）后，在 `.env` 设置 `EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1`、`EMBEDDING_API_KEY=`（留空）、`EMBEDDING_MODEL=bge-m3`；
   - **纯离线 `local_st`**：在 [`config/settings.yaml`](../config/settings.yaml) 将 `llm.embedding.provider` 改为 `"local_st"` 并指定本地 `sentence-transformers` 模型路径。
2. **大语言与多模态推理模型（二选一）**：
   - **`--mode api` 直连编译**：在 `.env` 填写 `VLM_PRIMARY_*`、`VLM_SECONDARY_*`、`TEXT_LLM_*`（`JUDGE_LLM_*` 留空则自动复用 `TEXT_LLM`）；
   - **AGY Swarm / Subagent 免推理 Key 双通道**：上述 `VLM_*` 与 `TEXT_LLM_*` 留空即可。

```bash
# 体检基础环境、SymPy 沙盒与 Embedding 连通性
shuati doctor --json

# 若配置了 .env 推理模型并准备使用 --mode api，追加探测全部 LLM 槽位
shuati doctor --check-llm-api --json
```

---

## 二、题库编译进阶指南（完整冷启动、双通道与增量演进）

### 2.1 首次冷启动完整配置与阶段细节（`--mode api`）

1. **净仓初始化与外部题源目录配置**：
   - 若首次克隆本仓库自建题库，先将仓内示例 `human/*.jsonl` 无损归档并初始化干净角色：
     ```bash
     shuati human reset --learner-id alice --learner-name "Alice" --subject "小学奥数" --dry-run --json
     shuati human reset --learner-id alice --learner-name "Alice" --subject "小学奥数" --json
     ```
   - 外部题库目录保持严格只读，子目录下包含 `problems.jsonl` 与配图文件，每行格式如下：
     ```json
     {"id": "ch01_001", "question": "如图，正方形 ABCD 的边长为 10，求阴影部分面积。", "images": ["images/ch01_001.png"]}
     ```
   - 如需接入新学科（如 `初中物理`），先执行 `shuati grade-subject add --name "初中物理" --slug "junior_physics" --family PHYSICS --json`；
   - 在 [`config/settings.yaml`](../config/settings.yaml) 的 `ingest.roots` 中填入外部题库根目录（若本地无 `./题库` 目录，请替换默认的 `"题库"` 占位项），并在 [`config/collections.yaml`](../config/collections.yaml) 中配置目录通配符到 `grade_subject` 的映射。
2. **Stage 0 入库清洗 → Stage 1.5 黄金难度锚点定标 → Stage 1～4 编译**：
   ```bash
   # Stage 0：只读预检无误后正式入库
   shuati ingest --dry-run --dedup-near --json
   shuati ingest --dedup-near --json

   # 校验学科提示词接缝
   shuati prompt lint --json
   shuati prompt render --stage unified --grade-subject "小学奥数" --json

   # Stage 1.5：抽样 6 道候选锚点题 -> 仅对候选跑 Stage 1 验真 -> 编辑 config/anchors/anchors_<slug>.yaml 后解锁
   shuati anchors sample --grade-subject "小学奥数" --count 6 --json
   shuati compile --stage 1 --grade-subject "小学奥数" --only-uids "uid1,uid2,uid3,uid4,uid5,uid6" --mode api --json
   shuati unified calibrate-anchors --grade-subject "小学奥数" --json

   # [可选] 先抽 20~25 题用 --only-uids 做小样本烟测，确认满意后全量放开 Stage 1~4
   shuati compile --stage 1 --grade-subject "小学奥数" --mode api --json
   shuati compile --stage 2 --grade-subject "小学奥数" --mode api --json
   shuati taxonomy propose --grade-subject "小学奥数" --mode api --json
   shuati reindex --vectors --quantiles --grade-subject "小学奥数" --json
   shuati compile --stage all --grade-subject "小学奥数" --mode api --json

   # 人工复核 NEEDS_REVIEW 分歧题并输出全库质量体检报告
   shuati review --grade-subject "小学奥数"
   shuati audit --grade-subject "小学奥数" --json
   ```

### 2.2 免推理 API Key 的 Subagent 双通道（`export / import`）

在 AGY Swarm 等多智能体环境或离线批调度场景下，所有耗时推理阶段均支持 **`export`（导出脱敏任务包）→ Worker 批推理与零 DB 自检 → `import`（单写者确定性校验回灌）**：

```bash
# 1. Stage 1+2 合一导出、零 DB 自检与回灌
shuati unified export --grade-subject "小学奥数" --batch-size 10 --json
shuati tool verify-sympy --input @draft_sympy.json --json
shuati tool validate-draft --mode UNIFIED_S1_S2 --input @draft_candidate.json --json
shuati unified import --results data/unified/results.jsonl --task-map data/unified/task_map.json --json

# 2. Stage 3a（候选词表涌现）
shuati taxonomy propose --grade-subject "小学奥数" --mode export --json
shuati taxonomy propose --grade-subject "小学奥数" --mode import --results data/unified/stage3_4/propose/results.jsonl --json

# 3. Stage 3c（词表映射）
shuati compile --stage 3c --grade-subject "小学奥数" --mode export --json
shuati compile --stage 3c --grade-subject "小学奥数" --mode import --tasks-dir data/unified/stage3_4/3c --results data/unified/stage3_4/3c/results.jsonl --json

# 4. Stage 3d（节点挂载与同域算子归并）
shuati compile --stage 3d --grade-subject "小学奥数" --json
shuati graph compact --grade-subject "小学奥数" --mode export --json
shuati graph compact --grade-subject "小学奥数" --mode import --results data/unified/stage3_4/compact/results.jsonl --json

# 5. Stage 3e（先修 DAG 构建与无环校验）
shuati compile --stage 3e --grade-subject "小学奥数" --mode export --json
shuati compile --stage 3e --grade-subject "小学奥数" --mode import --tasks-dir data/unified/stage3_4/3e --results data/unified/stage3_4/3e/results.jsonl --json
shuati graph check-dag --grade-subject "小学奥数" --json

# 6. Stage 4（向量同步与五边拓扑织网）
shuati reindex --vectors --quantiles --grade-subject "小学奥数" --json
shuati compile --stage 4 --grade-subject "小学奥数" --mode export --json
shuati compile --stage 4 --grade-subject "小学奥数" --mode import --tasks-dir data/unified/stage3_4/stage4 --results data/unified/stage3_4/stage4/results.jsonl --json
```

### 2.3 日常四类增量编译与词表演进

每次增量操作前先运行 `shuati status --json` 与 `shuati ingest --dry-run --dedup-near --json`：

```bash
# 场景 A：同已有学段追加新题（既有已锁定题目自动跳过，切勿传 --apply-retire）
shuati ingest --dedup-near --json
shuati compile --stage 1 --grade-subject "小学奥数" --mode api --json
shuati compile --stage 2 --grade-subject "小学奥数" --mode api --json
shuati compile --stage 3c --grade-subject "小学奥数" --mode api --json
shuati compile --stage 3d --grade-subject "小学奥数" --json
shuati reindex --vectors --quantiles --grade-subject "小学奥数" --json
shuati compile --stage 4 --grade-subject "小学奥数" --mode api --json

# 场景 B：源题文字/配图勘误局部重编（自动保护锚点与词表覆写，仅重跑变更 UID）
shuati ingest --dedup-near --json
shuati compile --stage 1 --grade-subject "小学奥数" --only-uids "变更题UID" --mode api --json
shuati compile --stage 2 --grade-subject "小学奥数" --only-uids "变更题UID" --mode api --json
shuati compile --stage all --grade-subject "小学奥数" --mode api --json

# 场景 C：接入全新学段学科（独立注册并走完锚点定标与首轮编译，零触碰既有学段）
shuati grade-subject add --name "初中数学" --slug "junior_math" --family MATH --json

# 场景 D：词表从 DRAFT 晋升锁定为 LOCKED 1.0（或后续带 changelog 增量迁移）
shuati taxonomy candidates --grade-subject "小学奥数" --status PENDING --json
shuati taxonomy lock --grade-subject "小学奥数" --to-version 1.0 --status LOCKED --dry-run --json
shuati taxonomy lock --grade-subject "小学奥数" --to-version 1.0 --status LOCKED --json
shuati compile --stage all --grade-subject "小学奥数" --mode api --json
```

---

## 三、全量 CLI 命令参数字典（51 个子命令）

### 3.0 全局通用约定（JSON 信封、退出码与通用路径参数）

1. **统一 JSON 响应信封（`--json`）**：除本地临时 HTTP 复核服务 `shuati review` 与长连接 `stdio` 服务 `shuati mcp serve` 外，其余 **49 个**命令均支持 `--json`，向 `stdout` 输出单行 UTF-8 JSON（运行日志仅走 `stderr`）：
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
   - `2`：前置门禁未满足、参数校验失败或短题号歧义（`PRECONDITION_FAILED` / `AMBIGUOUS_UID`）
   - `3`：目标题目、草稿或批次不存在（`NOT_FOUND`）
   - `4`：外部 VLM / LLM / Embedding 网关请求失败（`LLM_ERROR`）
3. **全局通用路径与调试选项**（按命令职责自动挂载，默认即指向仓库标准目录，下文各条命令不再重复罗列）：
   - `--db PATH`（默认 `data/shuati.db`）｜`--config-dir PATH`（默认 `config`）｜`--vectors-dir PATH`（默认 `data/vectors`）
   - `--assets-dir PATH`（默认 `data/assets/images`）｜`--human-dir PATH`（默认 `human`）｜`--drafts-dir PATH`（默认 `data/drafts`）
   - `--reports-dir PATH`（默认 `data/reports`）｜`--mock / --no-mock`（离线确定性 Mock 探针）｜`--json`

---

### 3.1 环境、态势巡检、索引重建、Skill 与 MCP（6 个）

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

### 3.2 学段管理与原始题源入库（3 个）

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

### 3.3 提示词接缝、黄金难度锚点与零 DB 校验工具（7 个）

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

### 3.4 离线编译流水线、人工复核台与全库体检（5 个）

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

### 3.5 词表演进与知识图谱拓扑治理（9 个）

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

### 3.6 学习者画像、多维检索、梯度组卷与防剧透刷题（13 个）

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

### 3.7 外部新错题接入、人工覆写、资产恢复与开放导出（8 个）

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
