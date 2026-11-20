# 刷题 Harness v2（纯理科题库知识编译器 & `pi` 智能刷题底座）— 会话入口

**开始工作前先读 [docs/HANDOFF.md](docs/HANDOFF.md) 与 [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)**。

## 核心约定

1. **权威文档**:
   - 需求权威：[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)
   - 理论权威：[docs/理论基础.md](docs/理论基础.md)
   - 架构设计：`docs/DESIGN.md`（待下一步起草并经用户确认）
2. **历史归档保护**: `backup/v1/` 为旧版 Web 系统完整备份，**只读参考（勿修改）**；新系统在根目录从零搭建。
3. **确认优先**: 实现与设计中有歧义先向用户确认，不自作主张；代码修改遵循清晰模块化拆分与完整自动化测试验证。
4. **提交纪律**: 未经用户要求不自动 `git commit` / `git push`。
