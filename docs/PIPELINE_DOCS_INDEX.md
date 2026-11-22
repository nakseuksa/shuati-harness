# 全量编译流水线与工程沉淀文件导航

**更新时间**: 2026-11-21 23:56 (Asia/Shanghai)  
**说明**: 本文档汇总当前批编译流水线、操作手册（Skill 草稿）、长期规则与演进文档的落盘位置，方便快速查阅与新会话接力。

---

## 1. 最直观的流程与当前进度总览（推荐先看）

- **当前流水线白话全景**：[WORK_PROGRESS_OVERVIEW.md](WORK_PROGRESS_OVERVIEW.md)  
  解释了我们在把题库编译成什么、之前完成了什么、本轮扩库与 Wave 1–4 拆分进度，以及 `retry` 收尾路径。
- **最初拍板的方案设计书**：[UNIFIED_INIT_PLAN.md](UNIFIED_INIT_PLAN.md)  
  记录核心决策（2A/3A/4A）、脱敏隔离红线、Python 五道确定性校验与规范写库顺序。

---

## 2. 标准操作流程手册（SOP / Skill 草稿，待确认安装）

- **本项目全流程跑库 Skill 草稿（6 步 SOP）**
  - 主说明书：[../drafts/skills/shuati-bank-compile/SKILL.md](../drafts/skills/shuati-bank-compile/SKILL.md)
  - 命令速查与数据库状态查询：[../drafts/skills/shuati-bank-compile/references/cli_commands_and_prompts.md](../drafts/skills/shuati-bank-compile/references/cli_commands_and_prompts.md)
- **通用的“波次并发 + Python 验真 + Swarm 1 轮修复”编排 Skill 草稿**
  - 主说明书：[../drafts/skills/subagent-batch-compiler/SKILL.md](../drafts/skills/subagent-batch-compiler/SKILL.md)
  - 汇总分片修复细则：[../drafts/skills/subagent-batch-compiler/references/swarm_retry_protocol.md](../drafts/skills/subagent-batch-compiler/references/swarm_retry_protocol.md)
  - 可复用脚本模板：
    - Schema `$defs/$ref` 内联与边界收紧：[../drafts/skills/subagent-batch-compiler/scripts/schema_inline.py](../drafts/skills/subagent-batch-compiler/scripts/schema_inline.py)
    - 滑动窗口 `run_workflow` 批编译模板：[../drafts/skills/subagent-batch-compiler/scripts/workflow_template.py](../drafts/skills/subagent-batch-compiler/scripts/workflow_template.py)

---

## 3. 项目级长期守则与后续演进（新会话自动生效）

- **5 条实战工程铁律**：[../.agents/rules/shuati_ops_lessons.md](../.agents/rules/shuati_ops_lessons.md)（已通过 [../AGENTS.md](../AGENTS.md) 第 6 条挂载，新会话启动自动加载遵守）
- **后续架构与“空库全量重编”优化路线**：[ITERATION_ROADMAP.md](ITERATION_ROADMAP.md)（含 SQLite vs PostgreSQL 评估、断点机制与迭代改进路径）
- **产品最终能力全景概览**：[PRODUCT_VISION_OVERVIEW.md](PRODUCT_VISION_OVERVIEW.md)

---

## 4. 核心代码实现与会话临时编排脚本

- **导出、校验与单写者入库代码**：[../src/shuati/pipeline/unified_import.py](../src/shuati/pipeline/unified_import.py)（含 `STAGE2_ONLY` / `UNIFIED_S1_S2`、`--final` 兜底与 `subagent-unified` 防覆盖锁）
- **历史运行交接与里程碑验收**：[HANDOFF.md](HANDOFF.md)、[records/INDEX.md](records/INDEX.md)
- **本次实跑波次编排脚本（会话临时目录）**：`C:\Users\wangh\.gemini\antigravity-cli\brain\47de3593-7e43-4a03-8a26-ea4e549d9120\scratch\run_wave.py`（当前 `Wave 1–4` 正在使用的执行脚本，尚未转正进仓库 `scripts/`）
