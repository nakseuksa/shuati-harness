# 项目交接备忘录（HANDOFF — v2）

**更新**: 2026-11-22 22:52 (Asia/Shanghai)  
**用途**: 新会话记忆入口。开始工作前先读本文件与 [docs/REQUIREMENTS.md](REQUIREMENTS.md)、[docs/DESIGN.md](DESIGN.md)、[docs/records/INDEX.md](records/INDEX.md)。

---

## 一、 当前阶段与现状（**M0 / M1 / M2 / M3 全栈交互闭环已正式交付**）

- **已完成里程碑**（详细验收报告见 [docs/records/INDEX.md](records/INDEX.md)）：
  - **M0 & M0.1 (`PASSED`)**：工程基座、SQLite 20 表 + FTS5 DDL、CAS 配图去重与本地复核页。
  - **M1 (`PASSED`)**：Stage 1 验真闭环、跨平台 SymPy 沙盒与 25 题真机几何压测 & 人工裁决落盘。
  - **全量扩库与合一编译初始化 (`COMPLETED`)**：1,728 题入库与 Stage 0/1/1.5/2 初始化。
  - **M2 Stage 3/4/Q（`DRAFT` -> `LOCKED 1.0` 校准复跑 `PASSED`）**：[20261122_215000_M2_locked_v1_0_report.md](records/20261122_215000_M2_locked_v1_0_report.md)。
  - **M3 (`PASSED`)**：Pre-M3 图拓扑/镜像对齐 + `reveal`/`profiler`/`composer`/`mistake`/`render-sheet`/CLI 交互底座/`pi` Skill，见 [20261122_225200_M3_cli_and_interactive_report.md](records/20261122_225200_M3_cli_and_interactive_report.md)。
- **当前 `data/shuati.db` 实时基线（2026-11-22 22:52 热修复与审计导出实测）**：
  - **编译状态覆盖率（1,728 题）**：
    - `S1 = 1728 OK`；`S2 = 1725 OK / 3 BLOCKED`；`S3C = 1725 OK / 3 BLOCKED`；`S3D = 1725 OK / 3 BLOCKED`；
    - `S4 = 1649 OK / 79 BLOCKED`（严格隔离 76 题 `NEEDS_REVIEW` + 3 题 `S2=BLOCKED`）。
    - 可直接出题（`eff_is_servable = 1`）：**1,649 / 1,725（95.59%）**；`HUMAN_VERIFIED = 5`、`VERIFIED_CONSENSUS = 1,644`。
  - **知识词表与图谱（`小学奥数`, `LOCKED`, `1.0`）**：
    - 权威词表：[config/taxonomies/taxonomy_primary_olympiad.yaml](../config/taxonomies/taxonomy_primary_olympiad.yaml)（`8 L1`, `47 L2`）。
    - `knowledge_nodes`：活跃 `L2 = 47`、活跃 `L3 = 5,053`（折叠 `307`，L3 归一化熵 `0.999763`）；`knowledge_edges = 531`（`PREREQUISITE = 57` 0 环 + `CO_OCCURRENCE = 474`）。
    - `concepts`：活跃 `METHOD = 2,597`（折叠 `692`）、活跃 `MOTIF = 3,686`（折叠 `249`）、`TRAP = 6`。
  - **M3 热修复后五边拓扑与导出（`data/exports/`）**：
    - `question_relations = 2,592`：`CLONE = 93`、`ISOMORPHIC = 1`（`LLM_VERIFIED`）、`SCAFFOLD = 4`（`bad_scaffold(target<=source) = 0`）、`CONTRASTIVE = 460`（`LLM_VERIFIED`）、`SHARED_TRAP = 2,034`（已剔除 `TRAP_ARITHMETIC_SLIP` 并启用并集 IDF Jaccard）。
    - 4 份导出：`questions_enriched.jsonl (1725)`、`knowledge_nodes.jsonl (5100)`、`knowledge_edges.jsonl (531)`、`question_relations.jsonl (2592)`。
    - `human/` 快照：`human_mirror.is_consistent = true`（已就绪支持第 5 份 `external_questions.jsonl`）。
  - **已安装交互 Skill**：
    - 权威目录：[skills/shuati/SKILL.md](../skills/shuati/SKILL.md) 与 [commands.md](../skills/shuati/reference/commands.md)，已通过 `shuati skill install` 符号链接到 `.pi/skills/shuati`。

---

## 二、 关键工程约束与测试基线

1. **只读与安全边界**：
   - `backup/v1/` 与 `题库/` 严格只读（禁止读取 `题库/*/md/`）；冷备保留 `data/backups/20261122_111143_m2_pre_draft.db`、`data/backups/20261122_131032_taxonomy_lock.db` 与 `data/backups/20261122_144512_m3_hotfix.db`；未经用户明确指令不执行 `git commit` / `git push`。
2. **代码与测试基线（`2026-11-22 22:51` 最新一次提交级验证）**：
   - `ruff check --no-cache src tests`：0 错误；
   - `mypy --cache-dir "$env:TEMP\shuati_mypy_cache" src`：52 个源文件 0 错误；
   - `pytest -v`（141 项全库测试）：`138 passed, 3 failed`（全部 M3 + M0.1 + M1 + M2 测试零失败；仅 3 项 Stage 0 扩库前旧断言仍写死 `122` 题）。
3. **Windows 经验规则**：详见 [.agents/rules/shuati_ops_lessons.md](../.agents/rules/shuati_ops_lessons.md)。

---

## 三、 下一步可用动作与可选演进方向

1. **直接投入日常 `pi` 刷题使用**：
   - 使用 `shuati taxonomy --json` / `shuati search` / `shuati compose-quiz` / `shuati render-sheet` / `shuati detail` / `shuati check --log` / `shuati profile` / `shuati diagnose` + `shuati ingest-mistake --log-wrong`。
2. **可选图谱/题库维护（非阻断）**：
   - 若希望丰富高阶升维与同构变式邻边密度，可微调 `config/settings.yaml` 中 `scaffold.containment_min` / `isomorphic.surface_pct_max` 后重跑 `Stage 4`；
   - 空闲时可执行 `shuati review` 处理 `76` 题 `NEEDS_REVIEW`、`shuati audit --prune-runs` 清理 `23` 条未引用 `FAILED` 运行，或在单独维护批次更新 `tests/` 中 3 项 `122` 题旧断言。
