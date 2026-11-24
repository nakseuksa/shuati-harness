---
name: shuati-compiler
description: Standard operating procedure (SOP) for compiling Pure STEM question banks using shuati CLI seams, status inspection, preflight checks, model-pinned Swarm subagent dispatch, zero-DB worker self-validation, and deterministic import.
---

# Shuati 知识编译标准作业 Skill (`shuati-compiler`)

当用户要求巡检题库编译进度、接入新题源、调整/预览提示词接缝、批量导出/派发 Subagent 编排编译（Stage 1+2 / 3a / 3c / 3d / 3e / 4）或预检批量诊断题单时启用本 Skill。

## 铁律与红线

1. **严格隔离与单写者入库**：
   - 严禁让任何编排子代理（Swarm Worker）读写 `data/shuati.db`、`task_map.json`、`*_task_map.json`、`src/`、`docs/`、`human/` 或原始 `题库/*/md/`。
   - 全部 SQLite 写入、`CompileLock`、SymPy 终审与 `subagent:*` 防覆盖锁，均由主会话调用确定性 Python CLI（`shuati unified import` / `shuati compile --mode import`）单写者完成。
2. **精确锁模派发铁律（Swarm 优先，禁止 `run_workflow` 退化）**：
   - 批导出后会生成 `unified_manifest.json` 或 `<step_kind>_manifest.json`，其中固化了经 `SubagentConfig` 解析的 `subagent_model`（默认 `gemini-4-neon-medium`）与 `dispatch_driver: "swarm"`。
   - **必须**使用 `swarm(action="add", model=manifest["subagent_model"], tasks=...)` 精确锁模分片派发 Worker！
   - **严禁**使用不支持指定模型 ID 的 `run_workflow` 代替批编排派发，以免退化继承主会话昂贵模型导致算力浪费与口径漂移。
3. **Worker 零 DB 物理隔离自检**：
   - 批编译 Worker 仅可挂载 `shuati mcp serve --role worker`（或直接运行 `python -m shuati.mcp.server --role worker`），仅暴露 `verify_sympy` 与 `validate_draft` 两个零 DB 工具；或使用零 DB CLI `shuati tool verify-sympy` / `shuati tool validate-draft`（通过 `--input @file.json` 或 `--stdin` 传参，严禁在 PowerShell 内联多层引号）。
4. **防踩踏锁与 Windows 沙盒守则**：
   - 未完成 `import` 回灌前，严禁加 `--overwrite-pending` 重跑 `export` 覆盖未消费批次，否则 `task_id` 将错位。
   - 在 Windows 沙盒环境涉及写 `data/shuati.db`、持 `.compile.lock` 或 SymPy 子进程沙盒（Job Object）的命令，按工程守则申请沙盒外执行。

---

## 标准编排 SOP（8 步闭环）

### Step 1：全景巡检 (`shuati status`)
首先运行只读态势巡检，确认各学段 Stage 0→4 状态、挂起批、向量漂移与下一步命令建议：
```bash
shuati status --json
# 或限定学段
shuati status --grade-subject 小学奥数 --json
```
- 若 `pending_batches` 存在 `status == "READY_TO_IMPORT"` 或 `"HAS_RETRY"`，**优先完成回灌或重试清理**，切勿直接重新导出。
- 若 `vectors.needs_reindex == true`，在 Stage 4 前先执行 `shuati reindex --vectors --quantiles --json`。

### Step 2：只读预检 (`diagnose-preflight` / `ingest --dry-run`)
- **批量错题 Markdown 题单预检**（严格只读源文件，修复 `##12.` 漏空格、错位图片行与同行正文）：
  ```bash
  shuati diagnose-preflight --file path/to/questions.md --json
  ```
- **Stage 0 题库增量入库预检**（零写库、不锁库、不复制图片，核查 `unmapped_collections` 与缺失/坏图）：
  ```bash
  shuati ingest --dry-run --dedup-near --json
  ```
  确认无未映射集合后，再执行正式 `shuati ingest --dedup-near --json`。

### Step 3：提示词接缝审查 (`prompt list / render / lint`)
修改或启用学科/Profile 提示词接缝前，先静态校验与渲染预览：
```bash
shuati prompt list --json
shuati prompt lint --json
shuati prompt render --stage unified --grade-subject 小学奥数 --json
```
如使用特定调优 Profile，传入 `--prompt-profile <name>` 预览其与 `default.yaml` / `subjects/<slug>.yaml` 的合并效果与 `prompt_digest`。

### Step 4：自包含批导出（默认锁模 `gemini-4-neon-medium`）
确保 Stage 1.5 黄金锚点就绪（`anchors_ready == 1`，否则先 `shuati unified calibrate-anchors --grade-subject 小学奥数 --json`）后执行批导出：
- **Stage 1+2 合一编译导出**：
  ```bash
  shuati unified export --grade-subject 小学奥数 --batch-size 10 --json
  # 可选覆写模型或 Profile：--subagent-model gemini-4-neon-medium --prompt-profile <name>
  ```
  产物：`data/unified/batches/batch_*.jsonl`（内置脱敏 `messages`）、`flat_schema_unified.json`、`flat_schema_stage2.json`、`task_map.json`、`unified_manifest.json`（`status: "PENDING_IMPORT"`）。
- **Stage 3/4 双通道导出**：
  - `shuati taxonomy propose --mode export --grade-subject 小学奥数 --json`
  - `shuati compile --stage 3c --mode export --grade-subject 小学奥数 --json`
  - `shuati graph compact --mode export --grade-subject 小学奥数 --json`
  - `shuati compile --stage 3e --mode export --grade-subject 小学奥数 --json`
  - `shuati compile --stage 4 --mode export --grade-subject 小学奥数 --json`

### Step 5：按 Manifest 精确锁模派发 Swarm Worker
读取 `unified_manifest.json`（或 `<step_kind>_manifest.json`）：
1. 读取 `manifest["subagent_model"]`（默认 `"gemini-4-neon-medium"`）、`manifest["max_parallel_workers"]`（默认 `4`）与 `manifest["batch_files"]`；
2. 将 `batch_files` 按不超过 `max_parallel_workers` 分片，调用 `swarm` 工具精确锁模派发：
   - `swarm(action="add", name="compile-shard", model=manifest["subagent_model"], tasks=[...各分片任务说明...], brief="...")`
3. Brief 中必须明确：
   - 仅读取分片对应的 `batch_*.jsonl` 与其中显式 `image_paths`、预展平 Schema 文件；
   - 严禁触碰 `data/shuati.db`、`task_map.json`、`题库/`、`src/`；
   - 结果按 `task_id`（或 `batch_id`）逐行写入指定 `results_shard_<k>.jsonl` 并实时 `flush()`。

### Step 6：Worker 交卷前强制自检 (`--role worker`)
Worker 写出结果前，必须逐题（至少对含 `SYMBOLIC` 验算或 Stage 2 结构化草稿的条目）通过以下任一零 DB 通道自检，直至 `ok == true`：
- **MCP `--role worker` 工具**：
  - `verify_sympy(sympy_code=..., final_answers=..., grade_subject=..., options=...)`
  - `validate_draft(mode=..., candidate=..., question_text=..., grade_subject=..., run_sympy=True)`
- **等价零 DB CLI**：
  ```bash
  shuati tool verify-sympy --input @draft_sympy.json --json
  shuati tool validate-draft --mode UNIFIED_S1_S2 --input @draft_payload.json --json
  ```

### Step 7：确定性回灌入库 (`import`)
汇总各分片结果 JSONL 后，执行确定性事务回灌：
```bash
shuati unified import --results data/unified/results.jsonl --task-map data/unified/task_map.json --json
```
- `import_unified_results` 自动读取同目录 `unified_manifest.json` 的 `subagent_model`，向 `compile_state.model_name` 写入 `subagent:<resolved_model>` 锁定（与存量 `"subagent-unified"` 共同受 `is_compile_locked` 保护）。
- 若产出 `retry_tasks.jsonl`（`retry_count > 0`），仅针对失败条目修补后重跑回灌；终轮对仅 SymPy 失败但 Stage 2 合格的条目可加 `--final` 降级 `NEEDS_REVIEW` 放行 Stage 2。
- 全部成功回灌（`retry_count == 0 and failed_final_count == 0`）后，`unified_manifest.json` 自动置为 `"IMPORTED"`。
- Stage 3/4 对应执行 `--mode import --tasks-dir ... --results ... --json`，校验无误后 `<step_kind>_manifest.json` 自动置为 `"IMPORTED"`。

### Step 8：清理释放临时 Swarm Worker
确认全部批次已 `IMPORTED` 后，立即调用 `swarm(action="kill", name=...)` 释放所有临时编译 Worker，并运行 `shuati status --json` 复核流水线终态。

---

## MCP 角色隔离参考

- `shuati mcp serve --role worker`（或 `python -m shuati.mcp.server --role worker`）：物理零 DB（不加载 `sqlite3`），仅暴露 `verify_sympy`、`validate_draft`。
- `shuati mcp serve --role compiler`：暴露 `verify_sympy`、`validate_draft`、`prompt_list`、`prompt_render`、`prompt_lint`、`pipeline_status`、`diagnose_preflight`、`ingest_dry_run`。
- `shuati mcp serve --role practice`：同构暴露 M3 刷题工具（`learner_list`、`profile`、`taxonomy`、`search`、`related`、`detail`、`check`、`compose_quiz`，默认 `reveal="quiz"` 防剧透）。
- `shuati mcp serve --role all`：`compiler` + `practice` 全集。
