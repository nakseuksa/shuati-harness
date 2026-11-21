# 项目交接备忘录（HANDOFF — v2 重头启动）

**更新**: 2026-11-21 20:40 (Asia/Shanghai)
**用途**: 新会话记忆入口。开始工作前先读本文件与 [docs/REQUIREMENTS.md](REQUIREMENTS.md)、[docs/DESIGN.md](DESIGN.md)、[docs/records/INDEX.md](records/INDEX.md)。

---

## 一、 当前阶段与现状

- **已完成里程碑**（详细验收报告见 [docs/records/INDEX.md](records/INDEX.md)）：
  - **M0 & M0.1 (`PASSED`)**：工程基座、SQLite 20 表 + FTS5 DDL、122 题 Stage 0 入库与配图 CAS 体检全部落盘至 `data/shuati.db`。
  - **M1 工程交付与终审 (`PASSED`)**：Stage 1 双盲双 VLM 验真闭环、跨平台 SymPy 沙盒（含 `sympy.external.gmpy` 内部放行与 `IfExp` 兜底热修）、本地键盘复核页（`src/shuati/review/`）及全量自动化测试（123 passed、`ruff`/`mypy` 0 错）。
- **刚完成：M1 25 题真机几何压测 & 人工复核裁决落盘**：
  - 报告详见 [20261121_154820_M1_stress_test_25_report.md](records/20261121_154820_M1_stress_test_25_report.md)；
  - 当前 `.env` 实测阵容：`vlm_primary=anthropic/claude-sonnet-5.5`、`vlm_secondary=openai/gpt-6.1-terra`、`text_llm/judge_llm=openai/gpt-6.2-sol`、`embedding=google/gemini-embedding-2`；
  - 本轮实测结果：**25/25 零失败（4.8 分钟）**，严格自动共识 **17/25（68.0%；有效题 22 题中占 77.3%）**、实际答案一致 **21/25（84.0%）**、一题多解 **6 题**、SymPy `SYMBOLIC` 通过率 **26/30（86.7%，达标）**、25 题总花费约 **$1.38（¥10.0，单题 ¥0.40）**；
  - **人工复核已完成**：8 题 `NEEDS_REVIEW` 中 **5 题转 `HUMAN_VERIFIED`**（`ch01_010`、`ch01_012`、`ch01_013`、`ch28_009` 采纳主路，`ch28_010` 采纳辅路）、**3 题坏题剔除**（`ch28_025`、`ch28_031`、`ch28_091`，`is_excluded=1`, `eff_is_servable=0`）；17 题 `VERIFIED_CONSENSUS` 保持无驳回；`human_overrides` 共 23 条活跃记录并已同步导出至 `human/overrides.jsonl`；
  - 全部数据已持久化在 `data/shuati.db` 与 `data/llm_raw/`，**`backup/v1/` 与 `题库/` 严格只读零修改**，已按用户要求阶段 `git commit` 存档暂停。

---

## 二、 原始题库输入特征（已核实本地真实题库）

- **当前本地就绪**: `题库/` 下已就绪 2 个章节共 **122 题**（`ch01_计算基础` 15 题、`ch28_平面直线图形高年级卷` 107 题，配图完整性 100%）；后续将陆续放入其余章节至 1000+ 道小学奥数，并平滑扩展初中数理、高中数理化等纯理科。**无官方标准答案与解析**，重度依赖几何配图。
- **真实 JSONL 样例结构**:
  ```json
  {
    "id": "ch28_001",
    "chapter": 28,
    "question": "两张相同的长方形纸片，每张纸片的面积为48平方厘米...",
    "images": ["images/ch28_001_1.jpg"],
    "tags": {
      "domain": "几何",
      "topic": ["勾股定理与弦图问题"],
      "method": ["弦图与勾股构造", "容斥原理与韦恩图", "割补平移与等积变形"],
      "difficulty": 2,
      "visual": "几何图形",
      "format": "解答题",
      "extra_tags": []
    }
  }
  ```
- **实测发现的关键边界样本（已在 `DESIGN.md` v2.4 针对性建模）**:
  - **同卷重扫重题**: `ch28_015` 与 `ch28_057` 为同一道题（仅 `$ABCD$` 包裹与二次裁剪压缩字节不同），由 Stage 0 `plain_fingerprint`（辅以 `dHash`）预检 + 组卷 `CLONE` 14 天冷却避重拦截；
  - **多图与外置子图题注**: `ch01_012`（末尾 `A\n\nB`）、`ch28_083`（末尾 `图1\n\n图2`）原图内部均无子图编号，须保序绑定 `image_captions_json` 并在送 VLM 与渲染 HTML 时显式标注 `配图 1 / 配图 2`，严禁对 `cas_images` 排序哈希或盲删子图题注；
  - **OCR 汉字断词**: `ch28_010`（`面\n\n积`）、`ch28_030`、`ch28_096`，`normalize_text` 仅合并汉字间断词，保留句末标点（`。？！；：`）后的正常分段（如 `ch01_016`）；
  - **多元答案形态**: 显式多小问（`ch01_006`/`ch01_007`/`ch28_081`）、同问混填数字与符号（`ch01_010`，`ORDERED_TUPLE`）、比值（`ch28_020`，`RATIO`）、无标号多区域面积（`ch28_025`，`UNORDERED_MULTISET`）、无标号“计算+说理”复合问（`ch01_015`，固定双槽 `MIXED`）、错标填空的选择题（`ch28_003`/`ch28_016`，提取 `options_json`）、作图操作题（`ch28_031`，`QUALITATIVE`）及小学奥数圆周率 $\pi \approx 3.14$ 兼容。
- **数据治理与架构硬约束**:
  1. **严禁章节与旧标签污染**：Stage 1/2 严禁传入 `chapter`、`legacy_tags`、`question_uid`、`source_collection` 及原始图片路径（由 `prompt_builder.py` 白名单硬隔离）。
  2. **机审与人工覆盖彻底隔离**：`questions` 拆分为 `m_*`（机器层）与 `eff_*`（物化生效层，唯一写入者 `core/effective.py`）；题目级考点覆写挂在 `question_uid` 上（物化至 `eff_node_ids_json`，与 Stage 2 步骤重跑解耦）；人工资产每次写入同步导出按主键排序的全表原子快照至 `human/*.jsonl`；复核页提供 `[1]/[2]` 自包含采纳与 `[R]` 单路辅助重算草稿。

---

## 三、 `backup/v1/` 中可借鉴复用的模块与移植注意事项

新代码按 v2.4 架构全新编写，以下 v1 模块仅作只读算法参考（勿修改 `backup/v1/`）：
- **非线性四维难度合成**: `backup/v1/backend/services/difficulty_rubric.py`
  - *移植注意*：按决策 D4 将 `C=4` 分档阈值由 `2250/2550` 改为 `2400/2600`，档名 `L5-2_压轴巅峰` 改为 `L5-2_竞赛超难`；不移植子步难度 `clamp_substep_difficulty`。
- **SymPy 沙盒与答案等价判定**: `backup/v1/backend/sandbox/sympy_verifier.py`
  - *移植注意*：补齐 Windows Job Object 内存上限与 `taskkill /F /T` 进程树强杀；拦截 `sympify/parse_expr` 字符串求值；**放宽 v1 仅限 `solve/integrate` 的 AST 校验**（支持奥数算术/数论/几何计算，改为拦截纯字面量常量返回的空壳脚本）；新增 `RATIO` 比值、`UNORDERED_MULTISET` 与 $\pi \approx 3.14$ 兼容判等。
- **可逆并查集与分布熵收敛度**: `backup/v1/backend/services/knowledge_graph.py`
  - *移植注意*：`knowledge_nodes` 增加 `node_level ('L2'|'L3')`，向量编码、并查集归并与分布熵严格只统计活跃 `L3` 节点，排除 `L2` 汇总节点。
- **OpenAI 兼容 JSON 自愈网关**: `backup/v1/backend/services/llm_gateway.py`（结构化校验失败附错重试 1 次 + 统一写入 `llm_calls` 成本账本）。

---

## 四、 下一步工作起点（供重启/新会话直接接力）

1. **确认 M1 最终准出并决定 M2 推进节奏**：
   - 复核页裁决已全部落盘（`HUMAN_VERIFIED`: 5、`is_excluded`: 3、`VERIFIED_CONSENSUS`: 17、`human/overrides.jsonl`: 23 条）；
   - 向用户确认 17 题共识集抽检结论（假共识率与 `visual_facts` 可还原度）以正式签出 M1 `PASSED`；
   - 选择推进路线：① 先补跑其余 **97 题** Stage 1（凑齐 122 题有效真相后再建 M2），或 ② 先基于当前 22 题有效样本开发并跑通 **M2**（Stage 1.5 锚点 ~ Stage 4 五边拓扑与知识网），再全库补齐 122 题。
2. **Windows 环境操作备忘**：
   - 避免沙盒内直接跑 `uv run`（`OS error 5`），统一用 `.venv\Scripts\python.exe`、`.venv\Scripts\ruff.exe check --no-cache`、`.venv\Scripts\mypy.exe --cache-dir "$env:TEMP\shuati_mypy_cache"`；
   - 含 `asyncio` / `pytest-asyncio` / 外网 LLM / 本地浏览器可访问 HTTP 服务必须用 `BypassSandbox: true`（Windows 禁网沙盒会阻塞 `ProactorEventLoop` 的 `socketpair()`）。
