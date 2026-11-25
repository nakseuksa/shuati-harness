# 刷题 Harness v2（纯理科题库知识编译器 & `pi` 智能刷题底座）— 会话入口

**开始工作前先读 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) 与 [docs/records/INDEX.md](docs/records/INDEX.md)**。

## 核心约定

1. **权威文档（主从双层架构）**:
   - **顶层架构总纲（最高裁决权威）**：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（v2.7 收官定稿）
   - **详细实施规范（从层 DDL/公式/CLI 细则）**：[docs/DESIGN.md](docs/DESIGN.md)（v2.6 主从对齐版）
   - **需求与理论基线**：[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)、[docs/理论基础.md](docs/理论基础.md)
2. **历史归档保护**: `backup/v1/` 为旧版 Web 系统完整备份，**只读参考（勿修改）**；新系统在根目录从零搭建。
3. **确认优先**: 实现与设计中有歧义先向用户确认，不自作主张；代码修改遵循清晰模块化拆分与完整自动化测试验证。
4. **提交纪律**: 未经用户要求不自动 `git commit` / `git push`。
5. **汇报、裁决与执行记录三件套落盘规范**:
   - **存放目录与总索引**: 所有面向用户的正式裁决、报告与执行记录统一落盘至 `docs/records/`，并同步追加登记到 [docs/records/INDEX.md](docs/records/INDEX.md)（只落盘文件，仍遵守第 4 条不自动提交）。
   - **触发门槛**: 仅当发生实质代码/配置/数据变更、多步骤任务交付、技术分歧/Repro Triage 裁决或里程碑验收汇报时建档；纯问答、概念澄清或未发生状态变更的对话不建档。
   - **防覆盖命名与追加机制**: 按任务批次/阶段使用 `Asia/Shanghai` 时间戳命名：`YYYYMMDD_HHMMSS_<task_slug>_<kind>.md`（`kind` 固定为 `decisions` / `report` / `execution`，严禁 `:` 等 Windows 非法字符，同秒撞名时追加 `_01`）。同一任务批次内多轮迭代按 `[YYYY-MM-DD HH:MM]` 节标题**只增不删地追加**；每次新启动独立任务或正式开启新阶段交付时新建一组带新时间戳的文件，严禁覆盖既有记录。
   - **三份文档职责解耦（互链不重复）**:
     1. `*_decisions.md`（裁决文档）：记录背景、备选方案、最终裁决结论、依据条款与状态；不堆砌终端日志。
     2. `*_execution.md`（执行记录）：记录客观时间线、实际变更文件清单、实测命令及真实退出码/输出切片、过程产物链接；不掺杂主观总结。
     3. `*_report.md`（汇报/验收报告）：面向用户 Review 的高密度结论视图（交付概览、验收矩阵 PASS/FAIL、非阻断建议与待拍板项），细节通过链接指向同批 `decisions` 与 `execution`。
   - **与内部协作目录衔接**: `.agents/teamwork/<role>/` 继续承载多智能体内部协作契约、Handoff 与 Repro 审查过程稿（多轮审查只增不删历史轮次），最终通过三件套汇总向用户交付。
6. **批处理编排与 Windows 工程经验沉淀**: 详见 [.agents/rules/shuati_ops_lessons.md](.agents/rules/shuati_ops_lessons.md)（每次批编译、数据源扩充与测试环境排障均须遵守）。

## 内置三套 Skill 职责路由表

项目通过 `.agents/skills.json`（AGY 开箱自动发现）与 `shuati skill install --all`（`pi` 同步挂载）提供三套分工明确的内置 Skill：

| Skill | 路径 | 职责边界与触发时机 |
| :--- | :--- | :--- |
| **`shuati-init`** | [skills/shuati-init/SKILL.md](skills/shuati-init/SKILL.md) | 首次 `git clone` 冷启动初始化（`.venv`、净仓/换机恢复分流、外部只读 `problems.jsonl` 挂载、`shuati doctor` 体检、新学段提示词与黄金锚点一屏定标、20～25 题烟测及首轮 `DRAFT` 全图谱编译） |
| **`shuati-compiler`** | [skills/shuati-compiler/SKILL.md](skills/shuati-compiler/SKILL.md) | 日常四路增量编译与词表演进（`status` + `ingest --dry-run` 开场双检、同学段加题与向量漂移重算、源题勘误局部重编、新学段桥接 `shuati-init` 定标、`DRAFT → LOCKED 1.0` 锁表、Swarm 精确锁模编排与批诊断预检） |
| **`shuati`** | [skills/shuati/SKILL.md](skills/shuati/SKILL.md) | 日常自适应刷题与诊断（学习者画像、知识树浏览、ZPD 梯度组卷、离线 HTML 双模式练习单、防剧透苏格拉底式讲题、`check` 判题与新错题诊断入库） |
