# 后续架构演进与全量重编迭代参考方案

**记录时间**: 2026-11-21 23:17 (Asia/Shanghai)
**性质**: 架构备忘与后续迭代优化参考（供正式拍板后分步落地；不自动改动现行基线）

---

## 一、 数据库选型评估：SQLite vs PostgreSQL

### 1.1 迁移至 PostgreSQL 的真实收益
1. **并发读写无锁争抢**：SQLite 为单写者模型（当前依赖 `data/.compile.lock` 与 `SingleWriterExecutor` 防锁冲突）；PostgreSQL 原生 MVCC 行级锁支持后台批量写入的同时前台刷题/复核零阻塞。
2. **`pgvector` 原生混合向量检索**：Stage 3 概念归并（`concepts` -> `knowledge_nodes`）与相似题召回使用 `gemini-embedding-2`；PG + `pgvector`（HNSW）可在单条 SQL 内联 `L1 模块 + Elo 区间 + 冷却排除 + 向量 Top-K`。
3. **`JSONB` + GIN 索引与强类型**：当前大量核心字段为 `*_json` 文本（`eff_final_answers_json`、`eff_node_ids_json`、`part_verdicts_json`、`raw_trap_tags_json`），PG `JSONB` 可直接对嵌套 JSON 建索引，全文检索可接 `pg_jieba` / `zhparser`。
4. **跨端共享与服务端化**：支持手机/平板/Web 多端实时共享同一进度与流复制/PITR 恢复，摆脱单文件拷贝同步限制。

### 1.2 当前阶段切 PostgreSQL 的代价
1. **提高终端分发门槛**：产品后续需沿 `.env` 向终端用户分发，SQLite 零安装、零守护进程、单文件即可携备份；强依赖 PG 则要求本地部署数据库服务与 `pgvector` 插件。
2. **2,000–20,000 题体量下属于性能过剩**：千题级全库仅数十 MB，SQLite WAL 下读查询为微秒/毫秒级（1,734 题 Stage 0 全量 `ingest` 耗时 9.8 秒）；Stage 3/4 向量在万题内直接存 `BLOB` + NumPy 内存点积即可在 <1ms 完成。
3. **方言改造成本**：现有 20 张表 DDL、FTS5 触发器、`effective.py` 写守卫、事务锁与百余条集成测试需整套重写迁移。

### 1.3 务实结论
- **现阶段（个人主用 / 本地 CLI / `pi` 刷题底座 / 2k–20k 题）**：**保持 SQLite**，充分吃透零运维与单文件冷备优势。
- **迁移拐点**（满足任一再动手）：① 转型多用户 Web/SaaS 平台；② 强需求多终端云端实时共用同一刷题主库；③ 题库扩至 10 万+ 题且存在高频并发写与复杂在线向量过滤。
- **未来双轨共存路径**：本地编译端继续产出便携 `.db` 编译包，若上线服务端则增量导入云端 PG，SQLite 侧投入不浪费。

---

## 二、 大批量并发编译的断点容错与恢复边界

### 2.1 当前编排已具备的容错能力
1. **单题抖动无感免疫**：`run_wave.py` 中单题调用包有 `try/except`，单题网络超时或格式异常仅记入 `*.errors.json`，不连坐同分片并发题，也不中止 Wave。
2. **Chunk 级实时落盘（最大损失 ≤ 10 题）**：每完成 10 题并发片立即 `append + flush()` 至 `results_wave_*.jsonl`；启动时自动读取既有 `task_id` 去重跳过，未入结果文件的失败题与未跑题会自动续跑。
3. **数据库侧防覆盖锁**：一旦对部分或全量 `results_wave_*.jsonl` 执行 `shuati unified import`，已过审题目在 `compile_state` 写入 `subagent-unified` + `input_hash` 锁，后续重导或续跑自动跳过。

### 2.2 尚未做到“全自动无感恢复”的边界与操作禁忌
1. **全断网 / Token 配额彻底耗尽**：正在运行的 `run_workflow` 会终止返回，已落盘 chunk 保留在磁盘；恢复网络或切换额度后需重新发起一次续跑指令，无法脱离人工触发自行无限等待重连。
2. **`task_id` 对齐禁忌**：`T0001..T1715` 临时序号与 `data/unified/task_map.json` 强绑定。**严禁在未将已落盘 `results_wave_*.jsonl` 执行 `unified import` 锁库前，提前重跑 `unified export` 覆盖编号**；若要切换导出，必须严格按 `先 import 已产出结果 -> 再 export 剩余任务` 执行。

---

## 三、 “每次改动 Prompt / 算法均由 LLM 从全新数据库全量重编”的顺滑迭代方案

当确定每次重大迭代都要清空 `shuati.db` 并用更新后的提示词/算法让 LLM 全量重编 1,700+ 题时，重点是把 **“空库冷启动 → 烟测闸门 → 全量吞吐 → 双版对比 → 人工收尾”** 工程化压缩：

1. **消除空库冷启动两大重复手工**
   - **黄金锚点动态 `--bootstrap` 自动引导**：不把锚点数写死为固定几题，而是直接读取当前 `config/anchors/anchors_<slug>.yaml` 中配置的任意锚点集（代码校验下限为 `>= 4` 题；可按需求重选或增补锚点题）。空库时自动优先导出这批锚点题作 `Batch 0` → 验真入库 → 自动执行 `calibrate-anchors` → 解锁全量 `unified export`。
   - **题库固有坏题静态黑名单外置**：将与 Prompt 版本无关的原始残缺废题（如 `ch28_025`、`ch28_031`、`ch28_091`）沉淀到 `config/excluded_uids.yaml`；每次新建空库 `ingest` 后自动一键打标跳过，避免每轮重新踩坑与重复剔除。

2. **并发模型升级：整批同步等 → `asyncio.Semaphore` 滑动窗口**
   - 实测 `chunk_size=10` 整批等齐模式约 400 题/小时（1,715 题约 4+ 小时），瓶颈在于同批 9 道简单题干等 1 道长难几何题（木桶短板效应）。
   - 改为 **`asyncio.Semaphore(10~15)` 滑动窗口** + 单题完成即行级 `flush`：同等并发上限下吞吐可提升 **30%–50%**（压缩至 ~2.5 小时），且断点粒度从 10 题细化至 **1 题**。

3. **可配置“回归烟测集（Smoke Gate）”前置闸门**
   - 烟测题量与选题策略不写死，支持按迭代诉求灵活切换（例如：① 固定核心边界回归集 `--only-uids`；② 按章节/题型/`has_images` 分层随机抽样 `N` 题；③ 针对上轮 `NEEDS_REVIEW`/改动波及模块定向抽选 `N` 题）。
   - 每次修改 Prompt/算法后先跑这批烟测片，确认 SymPy 通过率、Hint 零剧透与 Elo 锚点一致性无退化，再放行全量跑库。

4. **跨版本量化对比报告（A/B Diff）**
   - 每次删库重来前，从旧库导出轻量基线摘要（`data/baselines/vN.json`：各题状态、终答、`C/S/T/L`、`eff_base_elo`）。
   - 新一轮全量落库后自动输出 4 项 Diff：
     1. 一次 SymPy 通过率与 `NEEDS_REVIEW` 占比对比；
     2. **终答漂移清单（Answer Drift）**：旧版 `VERIFIED_CONSENSUS` 与新版终答不一致的少数题目（精准人工抽审，不必盲看 1,700 题）；
     3. **退化清单**：上版过审而新版挂掉的题；
     4. 各章节/全库 `eff_base_elo` 分布偏移。

5. **中远期路径选择**
   - **主会话 Subagent 路线**：将 `scratch/run_wave.py` 转正至 `scripts/`，Prompt 模板外置为版本化文件（如 `prompts/unified_v*.md`），配合上述 1–4 项即可极顺滑迭代；
   - **独立 `.env` API 路线**：当 Prompt/模型定型或追求极致速度时，补齐纯 Python 异步 `shuati compile --stage ALL --concurrency 20`，脱离对话窗口与沙盒审批限制，可在终端 20 分钟左右直接并发跑完 1,700 题。

---

## 四、 后续开发与调优的双层多智能体协作架构

把 **`swarm` 工程协同** 与 **`run_workflow` 批量数据生产** 彻底解耦：

| 层级 | 工具与编排 | 适用任务 | 核心约束 |
| :--- | :--- | :--- | :--- |
| **工程 / 算法研发层** | `swarm` 三模型矩阵 | 开发 Stage 3/4、`pi` 交互底座、优化 Prompt 与校验逻辑 | 严格遵守“无 Repro 脚本不挑刺”，`worker` (`gemini-4-hydrogen-high`) 与 `reviewer`/`challenger` (`claude-sonnet/opus-5-5-high`) 基于实证测试收敛 |
| **千题数据生产与质检层** | `run_workflow` 滑动窗口 | 全量 1,700+ 题并发编译、A/B 盲评、错题归因分析 | 每题完全独立上下文、严禁跨题污染；严禁多个 Agent 直接写 `shuati.db` |

### 4.1 三个可立即落地的闭环工作流
1. **“错题归因 → Prompt 自进化”闭环**：将每轮产生的 `retry_*.jsonl` 与 `NEEDS_REVIEW` 清单交给归因 Subagent 聚拢 Top-K 高频失败模式 → 定向精简修改 Prompt（带字数上限防膨胀）→ 在“上轮错题集 + 本轮自选烟测集”重跑回归验证 → 仅当错题翻盘且老题零退化才升级全量。
2. **“软质量”抽样 Critic 质检层**：Python（SymPy/正则）只拦硬错；每次全量或烟测后抽检 10%–15% 复杂/高分歧题交给 Critic Agent，专门打分排查“变相 Hint 剧透”“几何描述歪斜但碰巧算对”“S/T 打分与锚点偏离”，以及双版本盲评 A/B Diff。
3. **严格维持“只写 JSONL、唯一 Python 单写入库”铁律**：任何规模的多 Agent 并发均只产出隔离 JSON/JSONL，最终收口到确定性的 Python 校验、`CompileLock` 与 `effective.recompute` 单事务管线。

---

## 五、 新一轮全量重编迭代实操手册（SOP、命令与开局提示词）

当您在测试一段时间后，调整好了 Prompt、难度算法或新章节题库，准备按现行流程发起**新一轮从全新数据库起步的 LLM 全量编译**时，按以下 4 个阶段推进最顺畅：

### 阶段 0：修改与留底（开新库之前）
1. **（可选）对齐新需求/改代码**：
   - 若要调整 Prompt 模板、难点规则或开发 Stage 3/4，可先发 `/plan`（或 `/grill-me`）敲定改动，再发 `/teamwork` 走三模型矩阵改代码并跑全量 `pytest` / `ruff` / `mypy`。
2. **整库备份与校准配置文件**：
   - 备份当前 `data/shuati.db` 至 `data/backups/`，并按本次需求更新坏题排除列表（如 `ch28_025`、`ch28_031`、`ch28_091`）及 `config/anchors/anchors_<slug>.yaml`（锚点数量 `>=4` 与具体选题均可自由重定）。

### 阶段 1：清空建库 → 体检导入 → 锚点冷启动（人工在场确认权限）
> 以下 CLI 均使用 `.venv\Scripts\python.exe -m shuati.cli.main ...`（在 Windows 沙盒外执行以获得 `data/.compile.lock` 写锁权限）。

```powershell
# 1. （按需）确认已备份后移除旧库 data/shuati.db（或迁至 data/backups/）
# 2. 若有新目录/新学科，先检查 config/collections.yaml 与 config/anchors/
# 3. Stage 0 全量体检、CAS 图片归档与近重复归并（切勿读取 题库/*/md/，有旧目录时勿加 --apply-retire）
.venv\Scripts\python.exe -m shuati.cli.main ingest --dedup-near --json

# 4. 锚点冷启动：若为绝对空库，须先对当前 anchors_*.yaml 指定的全部锚点题完成 Stage 1 验真，再执行标定
.venv\Scripts\python.exe -m shuati.cli.main unified calibrate-anchors --json

# 5. 导出全量脱敏批任务（建议 batch-size=50，方便按 Wave 控制并发与上下文）
.venv\Scripts\python.exe -m shuati.cli.main unified export --batch-size 50 --json
```

### 阶段 2：先跑自选烟测，再开 `/goal` 无人值守全量推完
1. **按需抽选烟测片验证**：根据本次改动重点确定烟测数量与选题规则（如 `--only-uids` 或按题型/章节分层抽样 `N` 题）跑小批 + `unified import`，核查 SymPy 通过率、Hint 零剧透率与 Elo 锚点一致度，确认无退化。
2. **清理上下文并挂机全量跑**：
   - 先单独发一条 `/compact`（清干净前期讨论历史）；
   - 再发送 **`/goal`** + 下方“标准开局提示词”启动全量 Wave 1–N 并发编译、分波 `unified import` 与 `gemini-4-neon-high` Swarm 重试收口。

### 阶段 3：集中人工收口（全部跑完后）
```powershell
# 1. 对所有 NEEDS_REVIEW 尾巴题做键盘集中拍板（采纳、重算或标记排除）
.venv\Scripts\python.exe -m shuati.cli.main review

# 2. 若有外部检索需求，统一刷新全文索引与生效层
.venv\Scripts\python.exe -m shuati.cli.main reindex
```

---

### 可直接复制的【新一轮迭代标准开局提示词】

#### 模板 A：从零起盘并先做前期准备（建议在发 `/compact` 之前使用）
```text
我要开启新一轮【全量重编迭代】，本次我调整了以下内容：<写明改动，如：优化了几何图提示词 / 调整了 S/T 难度标尺 / 重选了黄金锚点 / 新增了章节>。

请严格按以下约定推进：
1. 读取 题库/ 时只允许读各章节 problems.jsonl 与对应 images/，严禁读取任何 md/ 目录下的文件，严禁修改 backup/v1/ 与 题库/。
2. 保留我上次确认跳过的固有坏题清单（ch28_025、ch28_031、ch28_091 等），不要白跑。
3. 先备份当前 data/shuati.db 至 data/backups/，然后清空重设新库 -> 执行 Stage 0 (ingest --dedup-near) -> 按当前 config/anchors/ 指定的锚点清单完成冷启动与 calibrate-anchors -> 用 unified export (--batch-size 50) 导出全量脱敏批任务。
4. 在正式全量跑之前，按我的要求抽取烟测集（数量：<例如 20/50 题或待定>；选题策略：<例如：按章节与题型分层抽样 / 指定 UID 列表 / 含上轮错题>）先跑一轮编译 + unified import，向我汇报效果对比，我确认后再开全量挂机。
```

#### 模板 B：烟测确认完毕，准备 `/compact` 后挂机全量跑库（先单独发 `/compact`，再发本条）
```text
/goal 启动本轮全量编译入库（Wave 1–N 连续推进直到全部收口完成）：
1. 基于当前 data/unified/batches/ 全部待编译切片，使用滑动并发窗口 / 分波 run_workflow（每路 10 并发、每批/每题实时 flush 落盘），先看图后求解与 Stage 2 拆解，严禁编排子代理读取 题库/（含 md/）、docs/、src/、human/、shuati.db 或 task_map.json。
2. 每波完成后立即执行 shuati unified import 写库并锁定 subagent-unified；汇总各波打回的 retry 任务后，拉起 pinned gemini-4-neon-high 的 Swarm Worker 按分片补救 1 轮并执行 --final 终审入库（未过者照常写入 Stage 2 并标为 NEEDS_REVIEW，暂不出题）。
3. 全部完工后输出全库终态统计、全部 NEEDS_REVIEW 待人工复核清单，并更新 docs/HANDOFF.md 与 docs/records/ 三件套（不要自动 git commit/push）。完成后等待我用 shuati review 集中收口。
```

