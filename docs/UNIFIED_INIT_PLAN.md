# 题库初始化：主会话 Subagent 合一编译器（Stage 1+2 Unified）实施方案

**状态**：已由用户全项确认，待下令“开始实施”后启动（尚未改动代码）。  
**基准提交**：`main` `7f33fc3`（2026-11-21 20:41）

---

## 一、 当前数据库基线（2026-11-21 21:20 已实测）

`data/shuati.db` 共 122 题：

| 状态 | 题数 | `eff_is_servable` | 后续处置 |
| :--- | :---: | :---: | :--- |
| `VERIFIED_CONSENSUS` | 17 | 1 | 导出 **`STAGE2_ONLY`**（补纯文本拆解与打标，不改 S1） |
| `HUMAN_VERIFIED` | 5 | 1 | 导出 **`STAGE2_ONLY`**（`ch01_010`, `ch01_012`, `ch01_013`, `ch28_009`, `ch28_010`，严格保护人工覆写） |
| `is_excluded = 1` (`NEEDS_REVIEW`) | 3 | 0 | **跳过**（`ch28_025`, `ch28_031`, `ch28_091`） |
| `PENDING` | 97 | 0 | 导出 **`UNIFIED_S1_S2`**（合一生成 S1 + S2） |

- `human/overrides.jsonl` 已有 23 条活跃记录。
- **Windows 数据库访问备忘**：沙盒内只读查询可使用 `sqlite3.connect('file:data/shuati.db?immutable=1', uri=True)`；涉及写 `data/shuati.db` 或 `asyncio` / `pytest-asyncio` / 启动本地服务须留意沙盒边界（必要时用 `BypassSandbox: true`）。

---

## 二、 用户已锁定的 7 项核心决策

1. **双轨分工**：对外最终产品保留 `.env` API 路线（`LLMGateway` 原样保留）；当前自有题库初始化走主会话 `Subagent` 合一编译器。
2. **Stage 1.5 黄金锚点**：由 Agent 从当前 **22 道已验真有效题**中筛选 4～5 道覆盖不同梯度的代表题定稿（若缺顶端竞赛档则如实记入 `missing_tiers`），生成 `config/anchors/anchors_primary_olympiad.yaml`，写入 `grade_subjects(anchors_ready=1, anchors_version='1.0')` 与 `human_overrides(source='ANCHOR_CALIBRATION')`。
3. **2A（极省纯单路）**：全量只跑 1 次单路推理，不另起跨家族辅路。
4. **3A（`run_workflow` 10 题/批扇出）**：
   - 使用 `run_workflow` 按 **10 题/批**并发扇出，**1 题 1 Agent**（`model="inherit"`，当前 `Gemini 4 Hydrogen (High)`，配额 92%），通过 `schema` 强制由 `finish` 返回结构化结果；
   - **必须**在 `fn` 内包 `try/except` 捕获单题失败，防止 `parallel()` 单题异常取消整批任务；每批完成立即追加落盘 JSONL 并执行 Python 验真回灌。
5. **4A（失败定向重试 1 轮，指定 `Gemini 4 Neon (High)`）**：
   - Python 验真/校验失败的题输出至 `retry_tasks.jsonl`（附精确报错原因与上次输出）；
   - 通过 `swarm` 单独拉起 1 个绑定 **`gemini-4-neon-high`（`Gemini 4 Neon (High)`）** 的 Worker 定向修复 1 轮，修完即 `swarm(action="kill")` 释放并执行第 2 次 Python 回灌。
6. **门禁落态与 Stage 2 不阻断**：
   - **`VERIFIED_CONSENSUS`**：`visual_ambiguity_flag == false`，且不含纯定性 `QUALITATIVE` 小问（`SYMBOLIC` 小问经 `sympy_sandbox` 全部验算通过；`CHOICE` / `SYMBOL_FILL` 等 `EXACT` 小问无图意模糊即放行）；
   - **`NEEDS_REVIEW`**：含任意 `QUALITATIVE`（说理/证明/作图）小问（原因码 `SINGLE_RUN_ONLY`）、或 `visual_ambiguity_flag == true`（`VISUAL_AMBIGUITY`）、或重试后仍 `SYMPY_FAILED`；
   - **不阻断 Stage 2**：落为 `NEEDS_REVIEW` 的题目，**只要其 Stage 2 字段通过全部校验，依然同步落盘 `question_solutions` / `question_steps` / `concepts` / 难度并置 `compile_state(S2='OK')`**（若后续在复核页修正答案或解法，再由 `refresh.py` 自动将 `S2` 置 `STALE`）。
7. **`S1/S2` 防覆盖保护**：
   - 回灌落库时在 `compile_state` 记录 `model_name = 'subagent-unified'` 与内容指纹；
   - 在 `stage1_solve.py`（及后续 M2 `stage2_decompose.py`）跳过判断中增加保护：当 `status == 'OK'` 且 `model_name == 'subagent-unified'` 时，只要源题 `content_hash` 未变，**切换 `.env` 模型名或升级 `anchors_version` 均默认跳过**，仅显式传 `--force` 才允许重算。

---

## 三、 工程实现清单与校验红线

### 1. 脱敏隔离规则（不变量 1）
- 导出任务 JSONL 字段白名单：`task_id`（匿名序号映射 `question_uid`）、`mode`（`UNIFIED_S1_S2` | `STAGE2_ONLY`）、`grade_subject`、`question_text`、`options`、`cas_image_paths`（直接使用无章节名的 `data/assets/images/<16位hash>.jpg`）、`image_captions`、`anchors_fewshot`（匿名化）。
- **严禁传给任何 Subagent**：`question_uid`、`source_collection`、`source_id`、`chapter`、`legacy_tags`；Prompt 中硬性禁止 Subagent 读取 `题库/`、`docs/`、`src/`、`human/`、`data/shuati.db`（`STAGE2_ONLY` 额外只传生效题干、`eff_visual_facts`、生效解法与 `eff_final_answers`）。

### 2. Python 确定性校验（5 道关）
1. **Pydantic Schema + `AnswerPart` 白名单**：严格检验 `answer_type × verify_mode` 映射，禁止自造 `(1)-k` 子标签；
2. **SymPy 沙盒 + 等价判等**（`UNIFIED_S1_S2` 且含 `SYMBOLIC` 小问时）：调用 `src/shuati/verify/sympy_sandbox.py` 与 `equivalence.py`；
3. **防剧透 `hint` 正则校验**（`DESIGN.md §4.4-2`）：禁止出现具体阿拉伯数字（含计算结果/边长）、`=` 号、单/多大写字母顶点（如 `AB`、`△ABC`）、或“连接/作/延长+字母”等辅助线剧透词；
4. **`raw_l3` 脱壳正则校验**：4～14 字标准中文短语，拦截数字、单字母顶点和英文符号；
5. **`method_blacklist.yaml` 过滤 + `TRAP_*` 枚举子集校验 + 确定性难度合成**（调用 `src/shuati/core/difficulty.py` 计算 `base_elo` / `tier`）。

### 3. 规范写库顺序（不变量 2）
1. 写入 `stage1_vlm_runs`（`UNIFIED_S1_S2` 模式，记 `role='PRIMARY'`）；
2. 更新 `questions` 表的 **`m_*` 列**与机器专属列（绝不直接 `UPDATE eff_*`；`m_verification_status` 仅写 `VERIFIED_CONSENSUS` 或 `NEEDS_REVIEW`）；
3. 写入 `question_solutions`（`source_origin='PRIMARY_VLM'`，注意 `STAGE2_ONLY` 保留原有 `question_solutions`，仅补挂 `question_steps`）；
4. 写入 `question_steps`（`sub_goal`、`core_formula`、`hint`、`raw_l1`、`raw_l2`、`raw_l3`）；
5. 写入 `concepts` & `question_concepts(origin='MACHINE')`（跳过已有激活 `HUMAN` 覆写的 `kind`）；
6. 更新 `compile_state`（`S1='OK'`、`S2='OK'`，`model_name='subagent-unified'`，置 `S3C='STALE'`）；
7. 调用 `effective.recompute(conn, uid)` 统一物化 `eff_*` 与 FTS5。

---

## 四、 实施步骤与常用命令

### 执行阶段顺序
1. **阶段 1（锚点 + 代码 + 单测）**：
   - 只读查询 22 题筛选并生成 `config/anchors/anchors_primary_olympiad.yaml`（可先写入并写入 DB 激活，或在试跑前展示确认）；
   - 新增/扩展：`src/shuati/models/schemas.py`（联合输出模型）、`src/shuati/pipeline/unified_import.py`（导出、校验、回灌）、`src/shuati/pipeline/stage1_solve.py`（`subagent-unified` 防覆盖跳过保护）、`src/shuati/cli/main.py`（导出/回灌子命令）及对应单元/集成测试；
   - 验证命令：
     - `.venv\Scripts\python.exe -m pytest -q`
     - `.venv\Scripts\ruff.exe check --no-cache src tests`
     - `.venv\Scripts\mypy.exe --cache-dir "$env:TEMP\shuati_mypy_cache" src`
2. **阶段 2（第 1 批 10 题真机试跑验收）**：
   - 用前请先读取 `C:\Users\wangh\.gemini\antigravity-cli\builtin\skills\run-workflow\SKILL.md`；
   - 导出脱敏任务 → 仅抽第 1 批 10 题跑 `run_workflow` → Python 校验回灌 → 若有失败则开 `swarm(action="add", name="retry-fixer", model="gemini-4-neon-high", ...)` 重试 1 轮并回灌 → 向用户汇报通过率、耗时、典型抽样质量；
3. **阶段 3（全量分批跑完）**：
   - 用户验收满意后，分批跑完其余约 87 题 `UNIFIED_S1_S2` + 22 题 `STAGE2_ONLY`；
   - 最终有 `NEEDS_REVIEW` 题启动 `shuati review` 键盘快速扫尾后，正式进入 **M2（Stage 3 知识网 + Stage 4 五边拓扑）**。

### Swarm 可用模型参考（2026-11-21 21:05 实测）
- **Gemini 家族（剩余配额 92%）**：`gemini-4-hydrogen-high`、`gemini-4-neon-high`、`gemini-4-carbon-high`、`gemini-3.8-flash-high` 等；
- **Claude / GPT-OSS（剩余配额 31%，11-27 重置）**：`claude-sonnet-5-5-high`、`claude-opus-5-5-high`、`gpt-oss-120b-medium`。
