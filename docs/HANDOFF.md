# 项目交接备忘录（HANDOFF — v2）

**更新**: 2026-11-22 14:12 (Asia/Shanghai)  
**用途**: 新会话记忆入口。开始工作前先读本文件与 [docs/REQUIREMENTS.md](REQUIREMENTS.md)、[docs/DESIGN.md](DESIGN.md)、[docs/records/INDEX.md](records/INDEX.md)。

---

## 一、 当前阶段与现状（全量 1,728 题 Stage 0/1/1.5/2 编译已合拢）

- **已完成里程碑**（详细验收报告见 [docs/records/INDEX.md](records/INDEX.md)）：
  - **M0 & M0.1 (`PASSED`)**：工程基座、SQLite 20 表 + FTS5 DDL、CAS 配图去重与本地复核页。
  - **M1 (`PASSED`)**：Stage 1 验真闭环、跨平台 SymPy 沙盒与 25 题真机几何压测 & 人工裁决落盘。
  - **全量扩库与 Subagent 合一编译初始化 (`COMPLETED`)**：见 [20261122_141200_unified_full_compile_report.md](records/20261122_141200_unified_full_compile_report.md) 与 [PIPELINE_DOCS_INDEX.md](PIPELINE_DOCS_INDEX.md)。
- **当前 `data/shuati.db` 实时基线（2026-11-22 14:11 只读核验）**：
  - **全库总题量**：**1,728 题**（冷备存档于 `data/backups/shuati_pre_full_ingest_20261121.db`；`题库/` 与 `backup/v1/` 严格只读零修改）。
  - **可直接出题（`eff_is_servable = 1`）**：**1,649 / 1,725 有效题（95.6%）**
    - `VERIFIED_CONSENSUS`：**1,644 题**
    - `HUMAN_VERIFIED`：**5 题**（`ch01_010`、`ch01_012`、`ch01_013`、`ch28_009`、`ch28_010`）
  - **待人工复核尾项（`NEEDS_REVIEW`, `eff_is_excluded = 0`, `eff_is_servable = 0`, `S2 = 'OK'`）**：**76 题**
    - `SINGLE_RUN_ONLY`（含证明/作图/定性小问）：**69 题**
    - `VISUAL_AMBIGUITY`（含联合 `SINGLE_RUN_ONLY`）：**4 题**（`ch30_065`、`ch30_066`、`ch30_067`、`ch39_071`）
    - `SYMPY_FAILED`（第 2 轮仍未过 SymPy）：**3 题**（`ch27_083`、`ch34_029`、`ch36_017`）
  - **已永久剔除坏题（`eff_is_excluded = 1`, `S2 = 'BLOCKED'`）**：**3 题**（`ch28_025`、`ch28_031`、`ch28_091`）
  - **编译状态覆盖率**：
    - `S1='OK'`：**1,728 / 1,728**（`subagent-unified` 1,703 + `anthropic/claude-sonnet-5.5` 25）；
    - `S2='OK'`：**1,725 / 1,725 有效题（100%）**；`PENDING = 0`；
    - 共沉淀 **5,385 条解题步骤（`question_steps`）**、**7,230 条候选概念（`concepts`）**、**11,974 条题-概念关联（`question_concepts`）**；`questions_fts = 1,728` 同步；
    - `eff_base_elo` 区间 **`1120..2530`（均值 `1489.1`）**，覆盖 `L1`–`L5-2` 7 个活跃档位。
  - **Stage 1.5 与人工镜像**：`小学奥数` 已激活 5 题黄金锚点（[anchors_primary_olympiad.yaml](../config/anchors/anchors_primary_olympiad.yaml) `v1.0`）；`human_overrides` 共 28 条活跃记录（23 `REVIEW_UI` + 5 `ANCHOR_CALIBRATION`），已同步 `human/overrides.jsonl`。

---

## 二、 关键工程约束与已沉淀规则

1. **只读与隔离红线**：
   - `backup/v1/` 与 `题库/` 严格只读（尤其严禁读取任何 `题库/*/md/` 目录）；
   - `subagent-unified` 已锁定所有已过审题目，后续切换 `.env` 或重配锚点默认不重算，仅 `--force` 触发；
   - 批处理与测试环境经验规则已固化在 [.agents/rules/shuati_ops_lessons.md](../.agents/rules/shuati_ops_lessons.md)（随 `AGENTS.md` 第 6 条自动生效）。
2. **代码与测试基线提醒**：
   - `ruff check --no-cache src tests` 与 `mypy.exe --cache-dir "$env:TEMP\shuati_mypy_cache" src` 均 **0 错误**；
   - 全量 `pytest -q` 当前为 `121 passed, 3 failed`：其中 3 条 M0 遗留断言（`tests/integration/test_stage0_ingest_122.py` 2 条、`tests/unit/test_text_norm.py` 1 条）仍硬编码 `assert == 122`，因 `题库/` 已扩充至 `1,734` 题而触发，**非功能性回归**；
   - 尚未执行 `git commit`（按 `AGENTS.md` 第 4 条等用户指示）。
3. **Windows 环境操作备忘**：
   - 避免沙盒内直接跑 `uv run`（`OS error 5`），统一用 `.venv\Scripts\python.exe`、`.venv\Scripts\ruff.exe`、`.venv\Scripts\mypy.exe`；
   - 含 `data/.compile.lock` / 写入 `data/shuati.db` / `asyncio` / `pytest-asyncio` / 启动 `shuati review` HTTP 服务需在沙盒外（`BypassSandbox: true`）执行；沙盒内只读核验用 `sqlite3('file:data/shuati.db?immutable=1', uri=True)`。

---

## 三、 下一步工作起点（供新会话直接接力）

1. **优先交互收尾（76 题 `NEEDS_REVIEW` 集中拍板）**：
   - 沙盒外运行 `.venv\Scripts\python.exe -m shuati.cli.main review`，优先快速核对 **3 题 `SYMPY_FAILED`（`ch27_083`、`ch34_029`、`ch36_017`）** 与 **4 题 `VISUAL_AMBIGUITY`（`ch30_065`、`ch30_066`、`ch30_067`、`ch39_071`）**，其余 **69 题 `SINGLE_RUN_ONLY`** 均为开放定性/作图说理题，抽审确认即可转 `HUMAN_VERIFIED`。
2. **推进 M2 / M3（当前 `S3C / S3D / S4` 均已就绪 `STALE=1728`）**：
   - **Stage 3（知识树归纳 + 原子算子 `L3` 可逆并查集归并）**：汇总当前 7,230 条候选概念，确定 `小学奥数` 标准 `L1/L2` 树与 `L3` 向量/相似边；
   - **Stage 4（全库五边拓扑 `CLONE / ISOMORPHIC / SCAFFOLD / CONTRASTIVE / SHARED_TRAP`）** 与 **`pi` 刷题/配卷/打印闭环（M4）**；
   - **遗留测试与草稿安装**：由用户确认后决定是否放宽 `122` 题旧单测断言，以及是否将 [drafts/skills/](../drafts/skills/) 下两份 Skill 草稿安装至工作区或全局。
