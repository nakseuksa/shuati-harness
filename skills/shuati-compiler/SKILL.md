---
name: shuati-compiler
description: Standard operating procedure (SOP) for compiling and incrementally updating Pure STEM question banks using shuati CLI seams, opening dual-checks, four-way incremental auto-routing (same-subject additions with vector drift reindex, errata recompiles, new grade-subject bridging to shuati-init, and DRAFT-to-LOCKED taxonomy transitions), model-pinned Swarm subagent dispatch or --mode api execution, zero-DB worker self-validation, and deterministic import.
---

# Shuati 知识编译与四路增量编排 Skill (`shuati-compiler`)

当用户要求巡检题库编译进度、增量接入新题/新章节、修复源题勘误、扩展全新学段学科、将词表晋升为 `LOCKED 1.0`、调整/预览提示词接缝、批量执行编译（Stage 1+2 / 3a / 3c / 3d / 3e / 4）或预检批量诊断题单时启用本 Skill。

> 若当前尚未完成首次冷启动初始化（`data/shuati.db` 不存在、尚无已校准学段或需要全新配置 `.env`/外部路径），请先调用 [`../shuati-init/SKILL.md`](../shuati-init/SKILL.md)。

---

## 铁律与红线（不可妥协）

1. **严格隔离与单写者入库**：
   - 严禁让任何编排子代理（Swarm Worker）读写 `data/shuati.db`、`task_map.json`、`*_task_map.json`、`src/`、`docs/`、`human/` 或原始 `题库/*/md/` 与外部原始题库目录。
   - 全部 SQLite 写入、`CompileLock`、SymPy 终审与 `subagent:*` 防覆盖锁，均由主会话调用确定性 Python CLI（`shuati unified import` / `shuati compile --mode import`）单写者完成。
2. **精确锁模派发铁律（Swarm 优先，禁止 `run_workflow` 退化；兼容 `--mode api`）**：
   - **AGY Swarm 双通道（默认免推理 Key）**：批导出后会生成 `unified_manifest.json` 或 `<step_kind>_manifest.json`，其中固化了经 `SubagentConfig` 解析的 `subagent_model`（默认 `gemini-4-neon-medium`）与 `dispatch_driver: "swarm"`。**必须**使用 `swarm(action="add", model=manifest["subagent_model"], tasks=...)` 精确锁模分片派发 Worker！**严禁**使用不支持指定模型 ID 的 `run_workflow` 代替批编排派发。
   - **API 直连通道（`--mode api`）**：仅当用户已配置 `.env` 模型密钥且明确选择 API 直连时使用。
3. **Worker 零 DB 物理隔离自检**：
   - 批编译 Worker 仅可挂载 `shuati mcp serve --role worker`（或 `python -m shuati.mcp.server --role worker`），仅暴露 `verify_sympy` 与 `validate_draft` 两个零 DB 工具；或使用零 DB CLI `shuati tool verify-sympy` / `shuati tool validate-draft`（通过 `--input @file.json` 或 `--stdin` 传参，严禁在 PowerShell 内联多层引号）。
4. **防踩踏锁与 Windows 沙盒守则**：
   - 未完成 `import` 回灌前，严禁加 `--overwrite-pending` 重跑 `export` 覆盖未消费批次，否则 `task_id` 将错位。
   - 在 Windows 沙盒环境涉及写 `data/shuati.db`、持 `.compile.lock` 或 SymPy 子进程沙盒（Job Object）的命令，按工程守则申请沙盒外执行。

---

## 开场双检（每次执行增量编译前必跑）

1. **态势巡检**：
   ```bash
   shuati status --json
   # 或限定学段
   shuati status --grade-subject <学段学科> --json
   ```
   - 若 `pending_batches` 存在 `status` 为 `"READY_TO_IMPORT"`、`"PARTIAL_RESULTS"` 或 `"HAS_RETRY"`，**优先完成回灌或重试清理**，切勿直接重导出；
   - 记录各学段 `anchors_ready`、`taxonomy.status`（`DRAFT` / `LOCKED`）与 `vectors.needs_reindex`。
2. **外部只读题源预检**（自动扫描 `config/settings.yaml -> ingest.roots`，零写库、不锁库、不复制图片）：
   ```bash
   shuati ingest --dry-run --dedup-near --json
   ```
   - 核查 `newly_ingested`、`updated_source_changed`、`unmapped_collections` 与 `image_warnings`。

---

## 四类增量场景自动路由决策树

根据「开场双检」结果与用户意图，自动匹配以下 4 类路由之一执行最小化差量编译：

### 路由 A：同已有学段追加新题 / 新目录

1. **补齐集合映射**：若 `ingest --dry-run` 返回 `unmapped_collections` 非空，先在 `config/collections.yaml` 补全对应规则并再次 `--dry-run` 验证归零。
2. **增量入库**：执行正式入库（保留既有章节时**严禁**加 `--apply-retire`）：
   ```bash
   shuati ingest --dedup-near --json
   ```
3. **差量 Stage 1+2**：
   - 仅对新入库题目编译（既有已入库且源题未变的 `subagent:*` 锁定题自动跳过）：
     - Swarm 通道：`shuati unified export --grade-subject "<name>" --batch-size 10 --json` → 锁模派发 + `--role worker` 自检 → `shuati unified import`；
     - API 通道：`shuati compile --stage 1 --grade-subject "<name>" --mode api --json` → `shuati compile --stage 2 --grade-subject "<name>" --mode api --json`。
4. **增量 Stage 3c & 3d**：
   - 执行 `shuati compile --stage 3c --grade-subject "<name>" --mode export|api --json`（若已有 `LOCKED` 词表但出现新知识点，会收集进 `taxonomy_candidates`）；
   - 执行 `shuati compile --stage 3d --grade-subject "<name>" --json`；当新增未归并 `raw_l3` 较多时，运行 `shuati graph compact --grade-subject "<name>" --mode export|api --json`。
5. **向量漂移检测与重算（Stage 4 前置门禁）**：
   - 重新核查 `shuati status --grade-subject "<name>" --json`：若 `vectors.needs_reindex == true`（例如新增题量达存量 $\ge 20\%$ 或特征分位分布漂移），在 Stage 4 前先执行：
     ```bash
     shuati reindex --vectors --quantiles --grade-subject "<name>" --json
     ```
6. **Stage 4 织网 & 体检**：
   - 执行 `shuati compile --stage 4 --grade-subject "<name>" --mode export|api --json` → `shuati audit --grade-subject "<name>" --json`。

### 路由 B：存量源题文字 / 配图勘误局部重编

1. **指纹对比入库**：执行 `shuati ingest --dedup-near --json`。系统利用 `content_hash` 与分组指纹自动识别内容变化题（记入 `updated_source_changed`），**自动保留**黄金锚点与词表类人工覆写，仅挂起受内容变更影响的验真覆写并解锁对应题目 `compile_state`。
2. **局部重编变更题**：针对 `updated_source_changed` 列表，传入 `--only-uids "<uid1>,<uid2>,..."` 局部重跑 Stage 1+2 → Stage 3c/3d → 按需 `reindex --vectors --quantiles` → Stage 4。
3. **人工覆写复核提醒**：若输出提示存在被挂起的覆写记录，提醒用户运行 `shuati review --grade-subject "<name>"` 或 `shuati override <uid> --field <f> --reconfirm --json` 重新确认。

### 路由 C：新增全新学段学科（无缝复用 `shuati-init`）

1. 当出现未在 `grade_subjects` 注册的新学段，或目标学段 `anchors_ready == 0` 时，**直接桥接复用 [`../shuati-init/SKILL.md`](../shuati-init/SKILL.md)** 的对应阶段：
   - **Step 1**：`shuati grade-subject add` + 写入 `settings.yaml -> ingest.roots` 与 `collections.yaml` + `ingest --dry-run` → 正式 `ingest`；
   - **Step 3**：创建 `config/prompts/subjects/<slug>.yaml` + `shuati prompt lint --json` + `shuati anchors sample` + Stage 1 验真 + **一屏展示 4～5 道锚点表请用户拍板** + 落盘 `anchors_<slug>.yaml` + `shuati unified calibrate-anchors`；
   - **Step 4（>30 题时）**：20～25 题烟测把关。
2. 仅针对该新学段执行首轮 `DRAFT` 编译（`taxonomy propose` → `3c` → `3d` → `compact` → `3e` → `4` → `audit`），**零触碰既有学段数据、词表与锚点**。

### 路由 D：词表 `DRAFT → LOCKED 1.0` 锁表与候选演进

1. **审阅新词表候选**：
   ```bash
   shuati taxonomy candidates --grade-subject "<name>" --status PENDING --json
   ```
   将确认接纳的新 `L1/L2` 整理写入目标词表 YAML（或基于当前 `DRAFT` YAML 修订）。
2. **四步事务预检（必走 `--dry-run`）**：
   ```bash
   shuati taxonomy lock --grade-subject "<name>" --to-version 1.0 --status LOCKED --dry-run --json
   ```
   向用户确认受影响的人工覆写及 `practice_log.stuck_node_id` 迁移清单（若需允许无法自动映射的人工覆写置为 `TAXONOMY_UNMAPPED` 挂起，经用户确认后补 `--allow-stale-overrides`）。
3. **正式锁表或版本迁移**：
   - `DRAFT → LOCKED 1.0`：`shuati taxonomy lock --grade-subject "<name>" --to-version 1.0 --status LOCKED --json`
   - 已是 `LOCKED` 态按 `changelog` 增删并升级：`shuati taxonomy migrate --grade-subject "<name>" --file <new_yaml> --json`
4. **对齐重编**：顺序执行该学段 `3c → 3d → graph compact → 3e → 4 → audit`。

---

## 双通道批编排与通用工具操作细则

### 1. 批量诊断 Markdown 预检 (`diagnose-preflight`)
```bash
shuati diagnose-preflight --file path/to/questions.md --json
```
严格只读源文件，提前捕获 `##12.` 漏空格、错位 `图片:` 行与同行正文。

### 2. 提示词接缝审查 (`prompt list / render / lint`)
```bash
shuati prompt list --json
shuati prompt lint --json
shuati prompt render --stage unified --grade-subject "<name>" [--prompt-profile <name>] --json
```

### 3. AGY Swarm 精确锁模分片执行标准流程（Stage 1 / 1+2 / 3a / 3c / compact / 3e / 4）

1. **导出脱敏批任务包**：
   - Stage 1 仅验真（锚点或单独重跑）：`shuati compile --stage 1 --mode export --grade-subject "<name>" [--only-uids ...] --json`（产物在 `data/unified/stage3_4/stage1/`）
   - Stage 1+2 合一：`shuati unified export --grade-subject "<name>" --batch-size 10 [--only-uids ...] --json`（产物在 `data/unified/`）
   - Stage 3a：`shuati taxonomy propose --mode export --grade-subject "<name>" --json`
   - Stage 3c：`shuati compile --stage 3c --mode export --grade-subject "<name>" --json`
   - Stage 3d 归并：`shuati graph compact --mode export --grade-subject "<name>" --json`
   - Stage 3e：`shuati compile --stage 3e --mode export --grade-subject "<name>" --json`
   - Stage 4：`shuati compile --stage 4 --mode export --grade-subject "<name>" --json`
2. **按 Manifest 精确锁模派发 Swarm Worker**：
   - 读取对应目录的 `unified_manifest.json`（或 `<step_kind>_manifest.json`）中的 `subagent_model`（默认 `"gemini-4-neon-medium"`）、`max_parallel_workers`（默认 `4`）与 `batch_files`；
   - 按不超过 `max_parallel_workers` 分片调用：
     `swarm(action="add", name="compile-shard", model=manifest["subagent_model"], tasks=[...], brief="...")`
   - Brief 中必须声明：仅读取分片 `batch_*.jsonl`、显式 `image_paths` 与预展平 Schema；严禁触碰 `data/shuati.db`、`*task_map.json`、`src/`、`human/` 与原始题库；按 `task_id` / `batch_id` 逐行写入指定 `results_shard_<k>.jsonl` 并实时 `flush()`。
3. **Worker 交卷前强制自检 (`--role worker`)**：
   - Stage 1 / Stage 1+2 Worker 写出结果前，必须逐题通过零 DB MCP 或 CLI 自检至 `ok == true`：
     - MCP `--role worker`：`verify_sympy(...)`、`validate_draft(...)`；
     - 零 DB CLI：`shuati tool verify-sympy --input @draft_sympy.json --json`、`shuati tool validate-draft --mode UNIFIED_S1_S2 --input @draft_payload.json --json`。
4. **确定性事务回灌 (`import`)**：
   - 汇总分片结果 JSONL 后执行回灌：
     - Stage 1+2：`shuati unified import --results data/unified/results.jsonl --task-map data/unified/task_map.json --json`（若产出 `retry_tasks.jsonl` 且 `retry_count > 0`，仅修补失败条目重跑回灌；终轮仅 SymPy 失败但 Stage 2 合格者可加 `--final` 降级 `NEEDS_REVIEW` 放行 Stage 2）；
     - 其余 Stage：`shuati compile --stage <1|3c|3e|4> --mode import --tasks-dir ... --results ... --json` 或 `shuati taxonomy propose --mode import ...` / `shuati graph compact --mode import ...`。
5. **释放临时 Swarm Worker**：确认全部批次 Manifest 均变为 `"IMPORTED"` 后，立即调用 `swarm(action="kill", name=...)` 释放所有编译 Worker，并运行 `shuati status --json` 复核终态。

---

## MCP 角色隔离参考

- `shuati mcp serve --role worker`（或 `python -m shuati.mcp.server --role worker`）：物理零 DB（不加载 `sqlite3`），仅暴露 `verify_sympy`、`validate_draft`。
- `shuati mcp serve --role compiler`：暴露 `verify_sympy`、`validate_draft`、`prompt_list`、`prompt_render`、`prompt_lint`、`pipeline_status`、`diagnose_preflight`、`ingest_dry_run`、`doctor`、`anchors_sample`。
- `shuati mcp serve --role practice`：同构暴露 M3 刷题工具（`learner_list`、`profile`、`taxonomy`、`search`、`related`、`detail`、`check`、`compose_quiz`，默认 `reveal="quiz"` 防剧透）。
- `shuati mcp serve --role all`：`compiler` + `practice` 全集。
