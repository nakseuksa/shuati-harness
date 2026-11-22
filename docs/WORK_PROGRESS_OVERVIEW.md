# 项目阶段工作与全量编译进度说明（截至 2026-11-21 23:49）

**记录时间**: 2026-11-21 23:49 (Asia/Shanghai)  
**关联文档**: [HANDOFF.md](HANDOFF.md)、[UNIFIED_INIT_PLAN.md](UNIFIED_INIT_PLAN.md)、[ITERATION_ROADMAP.md](ITERATION_ROADMAP.md)、[PRODUCT_VISION_OVERVIEW.md](PRODUCT_VISION_OVERVIEW.md)

---

## 一、 一句话理解：我们在造什么？

原始 `题库/` 里只有 **题干 + 几何配图 + 原始章节/粗标签**，**没有标准答案和解析**。  
截至目前所做的全部工作，核心目标就是运行一套 **“知识编译流水线”**，把每一道原始题加工成存入本地 SQLite 数据库（`data/shuati.db`）的结构化学习资产：

1. **Stage 0（入库体检与去重）**：扫描题目与配图，对图片做内容哈希去重存储（CAS），去除重复题；
2. **Stage 1（视觉转译 + 数学验真）**：让 AI 看图把几何结构转译为纯文本描述（`visual_facts`），算出答案并生成可执行的 Python/SymPy 验算脚本，通过沙盒确认数学自洽；
3. **Stage 2（纯文本拆解与打标）**：基于前面已验证的信息，拆出解题步骤、`hint`（严格防剧透具体数字和顶点字母的启发提示）、原子考点标签 `raw_l1/l2/l3`、经典母题、常见陷阱，并由确定性代码按四维公式算出客观难度分 `eff_base_elo`；
4. **人工兜底隔离**：机器产物写机器列（`m_*`），人工确认/修改/剔除的数据存独立人工表与快照（`human/overrides.jsonl`），**后续重跑 AI 不会覆盖人工裁决**。

---

## 二、 此前已完成的两大阶段（扩充 1,600+ 新题之前）

1. **工程基座与第一轮试水（M0 → M1，最初 122 题）**
   - 搭建完成 20 张 SQLite 表与 FTS5 全文索引底座，最早入库 `ch01`（15 题）+ `ch28`（107 题）共 122 题。
   - 使用 `.env` API 实测了 25 道几何难题，并在本地键盘复核页 `shuati review` 完成人工裁决（5 题确认采用转 `HUMAN_VERIFIED`，3 道残缺坏题 `ch28_025` / `ch28_031` / `ch28_091` 永久剔除，快照导出至 `human/overrides.jsonl`）。
2. **主会话 Subagent 合一编译器研发 & 前 10 题试跑**
   - 按 [UNIFIED_INIT_PLAN.md](UNIFIED_INIT_PLAN.md) 拍板的省费隔离方案，完成 `src/shuati/pipeline/unified_import.py`、`src/shuati/pipeline/stage1_solve.py` 等改造与测试：
     - **一次同时产出 Stage 1 + Stage 2**，省去双路跨模型和重复视觉读图 Token；
     - 已入库题目打上 `subagent-unified` 与内容指纹防覆盖锁，后续切换 `.env` 模型不自动重算；
     - 定稿并校准了 Stage 1.5 的 5 道黄金难度锚点（`config/anchors/anchors_primary_olympiad.yaml`）。
   - **第 1 批 10 题真机试跑全部 10/10 一次通过**（7 题合一过审，3 题补全 Stage 2，0 重试）。

---

## 三、 扩充新题库并启动全量批编译后，已经完成的工作

1. **冷备份旧库 & 补齐新目录映射**
   - 将原数据库冷备份至 `data/backups/shuati_pre_full_ingest_20261121.db`，采用不删库增量方案（保留此前剔除的 3 道坏题、5 道黄金锚点、人工覆写和试跑锁定的 10 题）。
   - 针对新放入的 `xsc_ch01` / `xsc_ch02` / `xsc_ch04` 等目录，更新 `config/collections.yaml` 增加 `xsc_ch* -> 小学奥数` 映射规则。
2. **全量 Stage 0 入库体检**
   - 执行增量去重入库后共扫描 **1,734 题**：新增入库 **1,612 题**、跳过未变动 122 题、精确去重 4 题、近似去重合并 2 题、未映射 0、拒收 0（报告见 `data/reports/ingest_20261121_142959_958700_09c896.json`）。
   - 全库总计 **1,728 题**（含 3 道已剔除坏题，活跃待处理 1,725 题；扣除试跑已锁定的 10 题，本轮待编译 **1,715 题**）。
3. **全量脱敏导出与防透题隔离**
   - 将 1,715 题剥离章节名、旧标签与原目录路径，切分为 35 个批次文件（`batch_001.jsonl` .. `batch_035.jsonl`，每批最多 50 题），存放于 `data/unified/batches/`；真实题号映射表单独隔离在 `data/unified/task_map.json`，严禁任何子代理读取 `题库/`（尤其 `题库/*/md/`）、`src/`、`docs/`、`human/` 与 `data/shuati.db`。
   - 编写波次编排脚本（`scratch/run_wave.py`）：内联 Schema 并前置收紧边界、分 4 大波次推进、每 10 题并发小片完成即 `flush` 落盘，单题异常独立隔离且支持断点去重续跑。
4. **Wave 1（前 450 题）子代理推理已全部完成**
   - `batch_001` .. `batch_009` 共 **450 题**已全部生成结构化结果并落盘至 `data/unified/results_wave_01.jsonl`（450 行）。
5. **同步沉淀的长期文档与 Skill 草稿**
   - **架构演进与产品全景**：[ITERATION_ROADMAP.md](ITERATION_ROADMAP.md)、[PRODUCT_VISION_OVERVIEW.md](PRODUCT_VISION_OVERVIEW.md)
   - **项目级工程守则**：[.agents/rules/shuati_ops_lessons.md](../.agents/rules/shuati_ops_lessons.md)（已挂接至 [AGENTS.md](../AGENTS.md) 第 6 条）
   - **两份待用户审阅确认后自行安装的 Skill 草稿**：
     - 通用批编译技能：[drafts/skills/subagent-batch-compiler/SKILL.md](../drafts/skills/subagent-batch-compiler/SKILL.md)
     - 本项目编译 SOP 技能：[drafts/skills/shuati-bank-compile/SKILL.md](../drafts/skills/shuati-bank-compile/SKILL.md)

---

## 四、 本快照时刻正在后台运行的任务（截至 2026-11-21 23:49）

| 后台任务 | 执行内容 | 当前进度快照 |
| :--- | :--- | :--- |
| **`task-537`** (`unified import`) | 对 **Wave 1（450 题）** 逐题跑 SymPy 沙盒验算、防剧透正则、原子打标检查、Elo 合成并写入 `shuati.db` | 持续写入中：库内 `VERIFIED_CONSENSUS` 已推进至 **381 题**（`PENDING` 剩 1,336 题） |
| **`task-538`** (`run_workflow`) | **Wave 2（`batch_010` .. `batch_018`，第 451–900 题）** 子代理并发编译 | 持续生成并实时落盘中：`results_wave_02.jsonl` 已写入 **218,964 字节** |

---

## 五、 后续收尾路线

1. **Wave 2 → Wave 3（450 题）→ Wave 4（365 题）**：依次完成全量 **1,715 题**的 AI 编译与第 1 轮 Python 确定性验真入库；
2. **1 轮 `swarm` 定向修复**：汇总 4 个波次产生的 `retry_wave_01..04.jsonl`，按 5–8 题/分片交给 `gemini-4-neon-high` 做 1 轮修复并执行 `--final` 终态入库；
3. **最终验收与交接**：全库只读核查、更新 [HANDOFF.md](HANDOFF.md) 与 [records/INDEX.md](records/INDEX.md) 对应三件套记录，把最终少数遗留的 `NEEDS_REVIEW` 题交由你用 `shuati review` 集中人工拍板（此类题的 Stage 2 拆解已非阻断提前落库）。
