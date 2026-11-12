# P4-C 任务契约：CUJ 全链路 E2E + README + 总门禁

## 上下文
项目：数理自适应刷题 Harness。你负责 Phase 4 收口验收：CUJ 1~4 端到端集成测试、README 重写、全量质量门禁。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **CUJ 1~4 全文**、Target File **#18/#2**、**Verification Plan** 全节。设计文档是唯一权威规格。
前置：全部后端服务与 API 路由（P4-A）、前端（P4-B 并行，E2E 只测 API 层不依赖前端）。

## 你拥有的文件
- `tests/test_e2e_cuj_flows.py`
- `README.md`（重写占位版）

**禁止**触碰其他文件（发现服务层 bug 汇报 PM，不得自行修改）。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- E2E 配置：**Mock VLM/LLM（USE_MOCK_LLM=True）+ 真实 SQLite + 真实 SymPy 沙盒**；httpx ASGITransport 打真实 FastAPI app；monkeypatch settings 路径到 tmp；时间旅行用 active_date 注入
- 门禁：`.venv/bin/python -m pytest tests/test_e2e_cuj_flows.py -v` 全绿

## E2E 场景（设计 #18 逐条）
### CUJ 1+2：录入→确认→三阶脚手架
- multipart 录入（题干+配图+草稿图，Pillow 合成）→ gate 卡断言（步骤状态/保底水位）→ confirm-gate（CONCEPT_BOTTLENECK）→ 账本出现 PARTIAL_DRAFT_SUBGOAL_FLOOR + 解释条 → 一阶盲探视图（遮罩条件，无原题目标）
- 一阶答错 → 二阶微母题（预热 READY_SYMPY，合成题 origin_type/SCAFFOLD_PRE 边断言）→ 答对 → 三阶原题 → 答对 → 四阶
### CUJ 3：拆拐杖→盲测销账
- L>=2 变式 equation_skeleton 模式：核心方程等价提交 → PENDING_RETEST + 覆盖分结算 + escrow 冻结 + 窗口日期（+3/+7）
- active_date+4 天 GET drills/daily → 题单剥标签断言 → 提交（变式只核最终 / 原题回退双核验两分支）→ MASTERED + escrow 兑现 + 守恒断言（净增 ≡ target）
- 失败分支：盲测答错 → ACTIVE + escrow 清零 + 重训会话起点分流
- 配额：构造 4 道到期题 → 3 入单 1 顺延；同日刷新幂等
### CUJ 4：知识网生长→Compaction→回滚
- 批量录入（monkeypatch 诊断 fixture 变体，两个 L1 模块）→ 节点复用（node_judge mock 命中）→ 满 15 触发 compaction → 审计批次 → 指标（攻克率/收敛度）→ revert → 逐字段恢复 + Elo 零变化
### 幂等与重入库
- 同内容重导入零新行；改题干保进度；新草稿新会话
- Elo 事件重复提交不重复加分（跨端点幂等键）

## README 重写（设计 #2）
- 系统简介 + 架构概览（双层五模块一段话 + 指向设计文档链接）
- 本地启动：`python3.11+ -m venv .venv && .venv/bin/pip install -e ".[dev]"` → `.venv/bin/uvicorn backend.main:app --reload` → http://localhost:8000
- Mock 模式说明（USE_MOCK_LLM=True 离线全闭环）；真实网关 .env 配置（LLM_BASE_URL/LLM_API_KEY/模型名）
- 测试指南（各阶段门禁命令）；目录结构导航；设计文档/理论基础/dev_plan 索引

## 总门禁（Verification Plan）
- `.venv/bin/python -m pytest -v --cov=backend --cov-fail-under=85`
- `.venv/bin/ruff check backend/ tests/ && .venv/bin/mypy backend/`
- 若覆盖率 <85%：优先补 E2E 分支而非改业务代码；仍不足则汇报 PM 缺口明细

## 完成标准
E2E 全绿 + 总门禁通过 + README 完成。最终消息汇报：E2E 场景数、总测试数、覆盖率数值、门禁输出摘要。
