# 项目交接备忘录（HANDOFF — v2）

**更新**: 2026-11-22 21:23 (Asia/Shanghai)  
**用途**: 新会话记忆入口。开始工作前先读本文件与顶层架构总纲 [docs/ARCHITECTURE.md](ARCHITECTURE.md)、[docs/DESIGN.md](DESIGN.md)、[docs/REQUIREMENTS.md](REQUIREMENTS.md)、[docs/records/INDEX.md](records/INDEX.md)。

---

## 一、 当前阶段与现状（M2 `DRAFT` 首跑完成 + 第三方架构评审 v2.6 主从规范已定稿）

- **已完成里程碑**（详细验收报告见 [docs/records/INDEX.md](records/INDEX.md)）：
  - **M0 & M0.1 (`PASSED`)**：工程基座、SQLite 20 表 + FTS5 DDL、CAS 配图去重与本地复核页。
  - **M1 (`PASSED`)**：Stage 1 验真闭环、跨平台 SymPy 沙盒与 25 题真机几何压测 & 人工裁决落盘。
  - **全量扩库与合一编译初始化 (`COMPLETED`)**：1,728 题入库与 Stage 0/1/1.5/2 初始化。
  - **M2 Stage 3/4/Q & `DRAFT` 首轮全库试跑 (`DRAFT_PIPELINE_PASSED`)**：见 [20261122_205800_M2_draft_pipeline_report.md](records/20261122_205800_M2_draft_pipeline_report.md)。
  - **第三方独立架构审核与 v2.6 主从双层权威规范定稿 (`ARCHITECTURE_V2_6_LOCKED`)**：见 [ARCHITECTURE.md](ARCHITECTURE.md) 与 [20261122_212300_arch_audit_v2_6_report.md](records/20261122_212300_arch_audit_v2_6_report.md)。
- **当前 `data/shuati.db` 实时基线（2026-11-22 20:56 Stage Q 实测）**：
  - **编译状态覆盖率（1,728 题）**：
    - `S1 = 1728 OK`；`S2 = 1725 OK / 3 BLOCKED`；`S3C = 1725 OK / 3 BLOCKED`；`S3D = 1725 OK / 3 BLOCKED`；
    - `S4 = 1649 OK / 79 BLOCKED`（严格隔离 76 题 `NEEDS_REVIEW` + 3 题 `S2=BLOCKED`）。
    - 可直接出题（`eff_is_servable = 1`）：**1,649 / 1,725（95.59%）**；`HUMAN_VERIFIED = 5`、`VERIFIED_CONSENSUS = 1,644`。
  - **知识词表与图谱（`小学奥数`, `DRAFT`, `draft-1`）**：
    - 词表文件：[config/taxonomies/taxonomy_primary_olympiad.draft.yaml](../config/taxonomies/taxonomy_primary_olympiad.draft.yaml)（`8 L1`, `43 L2`；另有 `37` 条 `PENDING` 候选新 L2 待审）。
    - `knowledge_nodes`：活跃 `L2 = 43`、活跃 `L3 = 5,050`（折叠归并 `310`，L3 归一化熵 `0.999754`）。
    - `concepts`：活跃 `METHOD = 2,597`（折叠 `692`）、活跃 `MOTIF = 3,686`（折叠 `249`）、`TRAP = 6`。
    - `knowledge_edges`：`PREREQUISITE = 49`（`dag_cycles_detected = 0` 强无环），`CO_OCCURRENCE = 423`。
  - **向量仓（`google/gemini-embedding-2`, 3072 维, `meta_version = 1`）**：
    - `surface_primary_olympiad.npz = 1725`, `skeleton_primary_olympiad.npz = 1725`, `l3_nodes_primary_olympiad.npz = 5360`, `concepts_method.npz = 3289`, `concepts_motif.npz = 3935`。
    - 冻结分位表（`20,000` 抽样对）：`surface p95=0.7595, p99=0.8069`；`skeleton p95=0.7591, p99=0.8073`。
  - **Stage 4 拓扑与导出（`data/exports/`）**：
    - `question_relations = 3,882`（`CLONE=93, ISOMORPHIC=0, SCAFFOLD=0, CONTRASTIVE=460, SHARED_TRAP=3329`）。
    - 标定采样文件：[data/reports/calibration_candidates_primary_olympiad.yaml](../data/reports/calibration_candidates_primary_olympiad.yaml)（90 条：`CLONE=30, CONTRASTIVE=30, SHARED_TRAP=30, ISOMORPHIC=0, SCAFFOLD=0`）。
    - Stage Q 体检报告：[data/reports/audit_20261122_205623.md](../data/reports/audit_20261122_205623.md)（`effective diff_count = 0`，`human_mirror is_consistent = true`）。
    - 4 份导出：`questions_enriched.jsonl (1725)`、`knowledge_nodes.jsonl (5093)`、`knowledge_edges.jsonl (472)`、`question_relations.jsonl (3882)`。

---

## 二、 关键工程约束与测试基线

1. **只读与安全边界**：
   - `backup/v1/` 与 `题库/` 严格只读（禁止读取 `题库/*/md/`）；M2 施工前冷备 `data/backups/20261122_111143_m2_pre_draft.db`；未经用户明确指令不执行 `git commit` / `git push`。
   - 当前 `.env` 中 `EMBEDDING_API_KEY` 有效，但 `TEXT_LLM_API_KEY` 鉴权返回 401；大批量 LLM 步骤继续采用 `compile --mode export` -> Subagent -> `--mode import` 脱敏双通道即可零依赖推进。
2. **代码与测试基线（2026-11-22 20:57 实测）**：
   - `.venv\Scripts\ruff.exe check --no-cache src tests`：0 错误；
   - `.venv\Scripts\mypy.exe --cache-dir "$env:TEMP\shuati_mypy_cache" src`：45 个源文件 0 错误；
   - M2+M1 自动化测试套件（10 个文件共 `27` 项）：`27 passed in 37.36s`（M0 旧 `assert == 122` 用例按约定未改）。
3. **Windows 经验规则**：详见 [.agents/rules/shuati_ops_lessons.md](../.agents/rules/shuati_ops_lessons.md)（用 `.venv\Scripts\python.exe`，禁 `uv run` 与 `&&`）。

---

## 三、 下一步工作起点（按 [ARCHITECTURE.md §六](ARCHITECTURE.md#六-迈向-locked-10-与-m3-的代码对齐路线图11-项实施清单) 推进）

1. **M2.1：v2.6 架构裁决代码对齐（11 项待修清单）+ Stage 3d/4 实库重跑**：
   - **P0 核心修复**：① 修正 `topology.py` / `prompt_builder.py` 中反向的 `SCAFFOLD` 边方向为 `source=子题 → target=母题`；② `SHARED_TRAP` 剔除 `TRAP_ARITHMETIC_SLIP` 并改用并集加权 Jaccard + 平滑 IDF `+1`；③ `stage4_weave.py` 补齐 `surface` 向量拼接 `eff_visual_facts`；④ 将 Stage 3d 升级为同 `L2` 域 `Cluster-then-Assign` 标准算子聚类归纳，并放宽 Stage 4 `ISOMORPHIC`/`SCAFFOLD` 为“向量近邻 + `L2` 共现宽进召回 → LLM 精裁”（消除当前 `ISOMORPHIC=0, SCAFFOLD=0`）。
   - **P1 闭环补齐**：补齐独立 `pipeline/stage2_decompose.py`（有图强制传原图 + 人工 `eff_visual_facts` 最高优先）与 `compile --stage 2`；新增 `human/external_questions.jsonl` 镜像与恢复保护；落地分组 `base_fingerprint`、`taxonomy lock` 迁移 `stuck_node_id` 与维护降级模式、`subagent_bridge` 批次完整性校验及 `meta.json` 漂移巡检。
   - **晋升 `LOCKED 1.0`**：审阅吸纳 `37` 条候选新 L2 后执行 `taxonomy lock`，完成正式版知识网与五边重织网。
2. **启动 M3（`pi` 智能刷题与组卷打印闭环）**：
   - 实现 `facade.py`、`reveal.py`、`profiler.py`、`composer.py`（含低水位 ZPD 钳位与 `{L4-B, L5-1}` 合并爬坡）、`check`、`diagnose` + `ingest-mistake`、自包含静态双模式 HTML 练习单渲染器 `sheet_renderer.py` 与 `pi` Skill。
