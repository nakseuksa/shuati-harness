# 刷题 Harness — 会话入口

**开始工作前先读 [docs/HANDOFF.md](docs/HANDOFF.md)**（项目状态 / 环境命令 / 协作约定 / 技术决策 / 待办）。

## 硬性约定

1. 设计权威: `docs/designs/adaptive_practice_harness.md`；实现中有问题向用户确认，不自作主张
2. 分工: 所有代码修改委派 subagent worker（契约在 `docs/tasks/`，文件白名单制）；主会话只负责根因分析、方案设计、最终验收
3. URL 纪律: 工具 URL 必须与当前任务目标一致；严禁擅自替换为 routify 等临时代理；不盲从历史地址；每次调用重新核对；必要调用不遗漏、不无故增加
4. subagents 需要 pi 运行在 tmux 内
5. worker 禁止 git commit/push 与网络调用；PM 验收后统一提交

## 速查

- 启动: `.venv/bin/uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000`
- 门禁: `.venv/bin/python -m pytest -q --cov=backend --cov-fail-under=85 && .venv/bin/ruff check backend/ tests/ && .venv/bin/mypy backend/`
- 行为审计: `data/logs/harness.jsonl`；数据库: `data/harness.db`；真实网关配置: `.env`（gitignore）
