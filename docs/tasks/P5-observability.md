# P5 任务契约：全系统行为日志（Runtime 可观测 / 可审计）

## 上下文
项目：数理自适应刷题 Harness（Local-First）。用户需求：为代码添加行为 log，让系统 runtime 透明化，可通过 log 观测与审计。
现状：DB 层已有审计账本（elo_mutation_ledger / compaction_audit_ledger），但运行时行为（LLM 调用、沙盒执行、FSM 流转、预热流水线、降阶/升层、盲测调度）缺乏统一结构化日志。本任务补齐运行时观测层，与 DB 账本互补。
必读：docs/designs/adaptive_practice_harness.md 的 §3（五大模块）与 §4（API 表），理解各行为点语义。

## 你拥有的文件（白名单）
- `backend/observability.py`（新建）
- `backend/config.py`（追加日志配置）
- `backend/main.py`（lifespan 接入 + 请求日志中间件）
- `backend/services/llm_gateway.py`、`scaffold_engine.py`、`ingest_service.py`、`elo_engine.py`、`blind_drill_scheduler.py`、`knowledge_graph.py`（行为点埋点）
- `backend/sandbox/sympy_verifier.py`（沙盒执行日志）
- `tests/test_observability.py`（新建）
- `README.md`（追加观测章节）

**禁止**触碰白名单外文件。**禁止** git commit/push。**禁止任何网络调用**（测试全程 USE_MOCK_LLM=True）。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律 `.venv/bin/` 可执行文件
- 基线：614 测试全绿、覆盖率 97.10%、ruff/mypy 零告警——**日志不得改变任何业务行为**，基线只增不减
- 门禁：`.venv/bin/python -m pytest -q --cov=backend --cov-fail-under=85` + ruff + mypy 全绿

## 架构规格
### 1. backend/observability.py
```python
def setup_logging() -> None: ...
# 幂等（重复调用不重复挂 handler）；读取 settings：
#   控制台 handler：人类可读（时间 级别 模块 事件 摘要），LOG_CONSOLE 开关
#   RotatingFileHandler：{LOG_DIR}/harness.jsonl，JSON Lines，LOG_MAX_BYTES/LOG_BACKUP_COUNT
def log_event(logger: logging.Logger, event: str, /, **fields: Any) -> None: ...
# 结构化行为事件：record 附加 event 名 + fields；自动附带 contextvar 中的 request_id（如有）
# JSON 格式化器输出：ts, level, logger, event, request_id, ...fields
# 防御性：任何序列化异常不得击穿业务（降级为 repr 截断）
```
- 请求上下文：`contextvars.ContextVar[str]` 存 request_id；main.py 中间件每请求生成 uuid4 并写入，响应头回传 `X-Request-ID`。
- **安全红线**：绝不记录 LLM_API_KEY / Authorization 头 / 完整请求体；题干等内容字段截断 ≤120 字符（全文在 DB）。

### 2. config.py 追加
`LOG_LEVEL: str = "INFO"`、`LOG_DIR: Path = Path("./data/logs")`、`LOG_MAX_BYTES: int = 5_000_000`、`LOG_BACKUP_COUNT: int = 5`、`LOG_CONSOLE: bool = True`。

### 3. 事件目录（必须全覆盖，事件名逐字一致）
| 模块 | 事件 | 关键字段 |
| :--- | :--- | :--- |
| main | `api.request` | method, path, status, duration_ms, request_id |
| ingest | `ingest.received` / `ingest.reimport` / `ingest.diagnosis` / `ingest.persisted` | reimport 带 mode=short_circuit\|stem_updated\|new_draft_session；diagnosis 带 base_elo, tier, error_category, stalled_step, step_count |
| scaffold | `gate.confirmed` | branch, overridden, floor_applied, new_stage |
| scaffold | `fsm.transition` / `fsm.submission` | from_stage, to_stage, trigger；submission 带 stage, correct, no_idea |
| scaffold | `descent.triggered` / `descent.generated` / `descent.floor_reveal` / `ascent.step` | depth, elo_before/elo_after, reason=atomic\|depth_cap\|elo_floor\|gen_failed |
| scaffold | `prewarm.started` / `prewarm.local_hit` / `prewarm.kernel_attempt` / `prewarm.dual_blind` / `prewarm.completed` / `prewarm.failed` | kind, attempt, ast_ok, sandbox_ok, error_kind, duration_ms, status, consensus |
| sandbox | `sandbox.exec` | ok, error_kind, duration_ms |
| gateway | `llm.request` | purpose, model, mock, attempt, latency_ms, ok, error_kind（每次真实/Mock 调用各一条） |
| elo | `elo.event` | trigger, idempotency_key, applied, coverage_before/after, combat_before/after, gamma |
| elo | `escrow.frozen` / `escrow.realized` / `escrow.forfeited` / `escrow.decayed` | amount, decay_factor |
| drill | `drill.queue_locked` / `drill.submission` / `drill.cascade` / `drill.retrain` | active_date, uids, deferred_uids；intermediate_ok, final_ok, passed；retrain_stage |
| graph | `graph.node_reused` / `graph.node_created` / `graph.compaction_merged` / `graph.reverted` / `graph.co_error` | l1/l2/l3, batch_id, from_node, to_node, merges |

所有事件携带相关 session_id / question_uid（如适用）。埋点用各模块现有 `logging.getLogger(__name__)`（无则新建）。

### 4. README 追加「观测与审计」章节
日志位置（data/logs/harness.jsonl）、JSONL 格式示例、事件目录速查、`tail -f` 与按 request_id/session_id 过滤的审计命令示例、与 DB 账本的关系。

## 测试要求（tests/test_observability.py，caplog 为主）
- JSON 格式化器输出可解析且含 event/ts/level；setup_logging 幂等（handler 不重复）
- FSM：stage1→2 流转产生 fsm.transition（from/to 正确）
- Elo：保底发放产生 elo.event（幂等键 + before/after）；二次幂等调用 applied=False 亦留痕
- Gateway：Mock 调用产生 llm.request（mock=True, purpose）；monkeypatch 假密钥后断言日志全文不含密钥
- API：TestClient 请求产生 api.request（status/duration_ms）且响应头含 X-Request-ID
- 降阶：NO_IDEA 提交产生 descent.triggered + descent.generated 或 floor_reveal
- 沙盒：合法脚本执行产生 sandbox.exec（ok=True, duration_ms>=0）
- 日志异常防御：构造不可序列化字段不抛异常
- 既有 614 测试零回归（行为不变证明）

## 完成标准
门禁全绿（≥614+新增）。最终消息汇报：新增/修改文件、事件覆盖清单（对照上表逐行打勾）、新增测试数、覆盖率、密钥防泄漏验证结论。
