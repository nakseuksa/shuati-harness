# 项目交接备忘录（HANDOFF — v2 重头启动）

**更新**: 2026-11-20
**用途**: 新会话记忆入口。开始工作前先读本文件与 [docs/REQUIREMENTS.md](REQUIREMENTS.md)。

---

## 一、 当前阶段与现状

- **项目转型**: 仓库已清理并决定在 `shuati-harness` 内**重头开始构建 v2**：纯理科离线题库知识编译器 + 严密知识网 + 面向 `pi` 终端客户端的检索/组卷/刷题工具。
- **目录归档完成**: 原 v1（在线自适应刷题 Web 系统）的全部源码、测试与文档已通过 `git mv` 无损归档至 `backup/v1/`（**尚未 `git commit`**，留待确认后统一提交）。
- **需求已定稿**: 完整需求规格书已落盘于 [docs/REQUIREMENTS.md](REQUIREMENTS.md)（v2.0 定稿，FR-1 ~ FR-11 + NFR-1 ~ NFR-5）。
- **理论底座已保留**: [docs/理论基础.md](理论基础.md)（原根目录复制保留）。

---

## 二、 原始题库输入特征（已核实真实 JSONL 样例）

- **当前存量**: 1000+ 道小学奥数题（后续将追加初中数学/奥数/物理、高中数学、初高中化学等纯理科），**无官方标准答案与解析**，重度依赖几何配图（如阴影面积、图形连接关系全在图片中）。
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
- **数据治理硬约束**:
  1. 现有 `chapter` 与 `tags` 存在章节锚定污染（如将军饮马被硬标“弦图与勾股构造”）、万金油泛词（“转化与化归”）及题型脏数据（选择题错标填空题）；**Stage 1 解题与 Stage 2 拆解严禁把 `chapter` 和旧 `tags` 喂给模型**，仅作 `legacy_tags` 存档与对比质检。
  2. 几何题必须先做一次性 **`visual_facts`（视觉几何事实转译）** + **双盲双 VLM + SymPy 验算**，置信分级为 `VERIFIED_CONSENSUS` / `NEEDS_REVIEW` / `HUMAN_VERIFIED`，并提供用完即关的极简本地键盘复核脚本处理分歧题。

---

## 三、 `backup/v1/` 中可直接借鉴复用的核心模块

新代码按 v2 架构全新编写，以下 v1 模块算法经过验证可直接参考抽取（勿直接修改 `backup/v1/`）：
- **非线性四维难度合成**: `backup/v1/backend/services/difficulty_rubric.py`（`base_elo` + 8 档非对称阶梯）
- **SymPy 沙盒与答案等价判定**: `backup/v1/backend/sandbox/sympy_verifier.py`（受限子进程 + 方程/数值/单位等价 engine）
- **可逆并查集与分布熵收敛度**: `backup/v1/backend/services/knowledge_graph.py`（路径压缩 + `compaction_audit_ledger` 回滚 + 分布熵指标）
- **OpenAI 兼容 JSON 自愈网关**: `backup/v1/backend/services/llm_gateway.py`（结构化校验失败自动附错重试 1 次）

---

## 四、 下一步工作起点（供新会话直接接力）

1. **确认本地 JSONL 题库与图片目录路径** + 选定要试跑的 VLM / Embedding 网关配置；
2. **编写 v2 详细架构与数据 Schema 设计文档**（`docs/DESIGN.md`：SQLite 表结构 + 人工覆盖层 + 多角色表 + 五阶段流水线 + `pi` CLI/MCP 与静态 HTML 打印练习单设计）；
3. **先跑数据体检 + 25 题几何压力测试**（验证 VLM 阴影读图准确率、双盲共识率与成本），达标后再全量编译 1000+ 题库。
