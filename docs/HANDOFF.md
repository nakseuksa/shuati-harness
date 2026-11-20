# 项目交接备忘录（HANDOFF — v2 重头启动）

**更新**: 2026-11-20
**用途**: 新会话记忆入口。开始工作前先读本文件与 [docs/REQUIREMENTS.md](REQUIREMENTS.md)、[docs/DESIGN.md](DESIGN.md)。

---

## 一、 当前阶段与现状

- **项目转型**: 仓库已清理并决定在 `shuati-harness` 内**重头开始构建 v2**：纯理科离线题库知识编译器 + 严密知识网 + 面向 `pi` 终端客户端的检索/组卷/刷题工具。
- **目录归档完成**: 原 v1（在线自适应刷题 Web 系统）的全部源码、测试与文档已通过 `git mv` 无损归档至 `backup/v1/`（**尚未 `git commit`**，留待确认后统一提交）。
- **需求已定稿**: 完整需求规格书已落盘于 [docs/REQUIREMENTS.md](REQUIREMENTS.md)（v2.0 定稿，FR-1 ~ FR-11 + NFR-1 ~ NFR-5）。
- **理论底座已保留**: [docs/理论基础.md](理论基础.md)（原根目录复制保留）。
- **架构设计 v2.4 已完成实证修订**: [docs/DESIGN.md](DESIGN.md)（合入用户决策 D1～D6，以及第四轮结合本地 122 道实题与配图的修正、补充与瘦身裁剪；按用户裁决保留 `S3C/S3D` 分阶段与 `meta.json` 冻结 2 万对分位表），**暂不启动代码实施，待终审确认**。

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

## 四、 下一步工作起点（供新会话直接接力）

1. **终审锁定 [docs/DESIGN.md](DESIGN.md)（v2.4）**（确认后可统一处理归档与设计文档的首次 `git commit`）；
2. **确认模型网关配置**：确定 Stage 1 主/辅跨家族 VLM、Stage 2~3 纯文本模型与 Embedding 模型的端点配置；
3. **按 `DESIGN.md` 第七章里程碑启动实施**：先完成 **M0**（工程基座、20 表 + FTS5 DDL、122 题 Stage 0 入库体检）与 **M1**（Stage 1 验真、复核页、**25 题几何压力测试通过 5 项准出标准**），验证达标后再推进 M2~M4。
