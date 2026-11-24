# 刷题 Harness v2 — 项目级工程沉淀与批处理守则

沉淀自 v2 全量批编译与 Stage 0/1/1.5/2 实战迭代，供后续每次新会话与回归重编直接遵守。

## 1. AI 纯隔离输出 + Python 单写者入库（铁律）
- 严禁让 `run_workflow` / `swarm` 等并发编排子代理直接读写 `data/shuati.db`、`src/`、`docs/`、`human/`、`data/unified/task_map.json` 或原始 `题库/`（**尤其严禁读取任何 `题库/*/md/` 目录下的文档**）。
- 所有子代理仅消费脱敏后的单题 JSON 与显式 CAS 配图路径，返回结构化 JSON；SymPy 沙盒验真、AST 反硬编码作弊检查、Hint 防剧透、`raw_l3` 去数字、黑名单过滤、Elo 公式合成与 `subagent-unified` 防覆盖锁，一律由确定性 Python 单写者管线（`unified_import.py`）事务落地。

## 2. 大批量并发控流与断点保护
- **控流与落盘**：100 题以上批跑须分波次或使用滑动并发窗口，单题调用必须包 `try/except` 防止单点失败拖垮整批 `parallel(...)`，并按分片或单题实时 `append + flush()` 落盘 JSONL。
- **`task_id` 顺序对齐禁忌**：未对已落盘 `results_*.jsonl` 执行 `unified import` 写入数据库锁之前，**严禁提前重跑 `unified export`** 覆盖 `batch_*.jsonl` 与 `task_map.json`，以免临时 `Txxxx` 序号错位。

## 3. 前置 JSON Schema 就地收紧与边界校验
- 生成面向编排子代理的 JSON Schema 时，须先内联展开 Pydantic `$defs`/`$ref`，并将 Stage 2 业务校验边界直接写进 Schema（如 `C_concept_count` 上限 `4`、`S_chain_length`/`T_trickiness`/`L_computation_load` 上限 `3`），避免 Pydantic 宽松默认值放过越界打分触发打回。

## 4. 异构数据源接入与冷启动守则
- **Stage 0 接入必检**：向 `题库/` 新增不同命名规则目录（如 `xsc_ch*`）或新学段前，先核查 [config/collections.yaml](../../config/collections.yaml) 是否匹配，避免静默落入 `unmapped_collections`；增量导入开启 `--dedup-near`，保留既有目录时勿误加 `--apply-retire`。
- **锚点与烟测参数化**：黄金锚点集合与数量以 `config/anchors/anchors_<slug>.yaml`（校验下限 `>= 4` 题）动态驱动，空库重编需先完成锚点题 Stage 1 验真再运行 `calibrate-anchors`；全量放行前按迭代诉求自选烟测题量与抽样策略做回归闸门。
- **`NEEDS_REVIEW` 非阻断**：视觉歧义或重试后仍未过 SymPy 的题目照常写入其 Stage 2（`S2='OK'`、`eff_is_servable=0`），统一留待 `shuati review` 集中拍板。

## 5. Windows 沙盒与脚本执行适配
- **最小化申请 `BypassSandbox: true`**：`data/.compile.lock`（`msvcrt.locking`）、写入 `data/shuati.db` / `data/unified/` 以及 `pytest-asyncio`（ProactorEventLoop）在 Windows 沙盒受限，按规则先试沙盒再切沙盒外；`ruff check --no-cache`、`mypy --cache-dir "$env:TEMP\shuati_mypy_cache"` 与 `sqlite3('file:data/shuati.db?immutable=1', uri=True)` 只读核查在沙盒内执行。
- **Shell 与编码**：避免在 PowerShell 5.1 内联 `-c` 传入含多层嵌套引号的脚本；复杂检查落盘至一次性脚本并显式设置 `sys.stdout.reconfigure(encoding="utf-8")`，避免 Windows `gbk` 编码报错。

## 6. 批量诊断（diagnose --file）输入格式与 WSL 环境守则

沉淀自 2026-11-23 `diagnose_xingcheng_batch` 批次（行程问题.md 13 题清理重跑）。

- **分题标题必须带空格**：解析器 `_HEADING_RE` 仅认 `## ` / `### `（井号后有空格）。`##2`、`##12.` 这类无空格写法不会触发分题，整份文件会退化为仅按 `---` 切块，造成**多题合并诊断**（13 题被切成 4 块的真实事故）。批跑前先用 `parse_question_list_markdown` 预检题数与配图归属。
- **`图片:` 行必须放在所属题的 `---` 分隔线之前**：写在分隔线之后会被归入下一题。题干与标题同行（如 `##19. 一列火车……`）时题干只会成为标题、主题块为空而被丢弃，须移入正文。
- **规范化副本策略**：用户源文件只读不改；生成临时副本（标题补空格、图片行归位、相对图片路径转绝对或以副本同目录解析），对副本跑批。
- **WSL 调用入口**：仓库 `.venv` 为 Windows 布局（`Scripts/`）在 WSL 不可用，`shuati` 不在 PATH 时用 `PYTHONPATH=src python3 -c "from shuati.cli.main import app; app()" <子命令与参数>`；缺 `PIL` 时 `pip3 install pillow`。
- **清理重跑边界**：只删本批次时间戳对应的 `drf_*.json` 与 `data/drafts/images/` 配图缓存；更早会话的单题草稿不属于本命令产物，保留（断点复用按 `content_hash` 索引，合并块草稿的 hash 与规范化单题不匹配，不会误复用）。
