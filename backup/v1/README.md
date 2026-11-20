# 数理自适应刷题与验证框架（刷题 Harness）

面向**数学与物理学科**的 Local-First 单机自适应刷题与验证框架。系统接收含 LaTeX 公式、题干配图与手写演算草稿截图的错题输入，在**零预设考纲（完全冷启动）**条件下，把题目从"看懂解析"推进至"脱离提示独立解出"，并自发汇聚出三层知识网与分模块双轨 Elo 水平画像。

## 架构概览

系统采用**双层架构**：**微观单题闭环层**通过多模态手写草稿卡点诊断、步骤对齐微确认门禁（Step-Skeleton Diff Gate）、倒置防剧透三阶降阶脚手架（后台异步 Python AST + SymPy 子进程沙盒预热验算）、当场同构变式"拆拐杖"与 3～7 天延迟无提示盲测，驱动单题状态机 `🔴ACTIVE → 🟡PENDING_RETEST → 🟢MASTERED`；**宏观全局导航层**在零预设前提下以固定深度三层语法路径（`L1 学科模块 / L2 核心实体与定理 / L3 原子转化算子`）与同域可逆并查集 Compaction 生长知识图谱，并按四维难度 Rubric `[C, S, T, L]` 分模块独立核算"覆盖分（借助脚手架上限）/ 实战分（盲测兑现水位）"双轨动态 Elo（事件溯源账本 + 单题幂等托管池 Escrow）。落地为五大核心模块：①多模态导入与难度校准门禁、②三阶脚手架与 SymPy 沙盒、③双层正交状态机与延迟盲测调度、④三层知识网与可逆并查集、⑤双轨 Elo 与托管账本。技术栈：FastAPI + SQLite（WAL）+ SHA-256 内容寻址资产库（CAS）+ 原生 Web 前端（Markdown + KaTeX）。

完整规格（唯一权威设计文档）：[docs/designs/adaptive_practice_harness.md](docs/designs/adaptive_practice_harness.md)

## 本地启动

要求 **Python ≥ 3.11**（无需任何外部服务，SQLite 数据库首次启动自动建表）。

```bash
# 1. 创建虚拟环境并安装（含 dev 依赖）
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2. 启动 Web 服务（lifespan 自动执行 init_db()，数据落 ./data/）
.venv/bin/uvicorn backend.main:app --reload

# 3. 打开浏览器
open http://localhost:8000
```

运行时数据（已被 `.gitignore` 排除）：

- `./data/harness.db` — SQLite 数据库（题目/步骤/知识网/事件账本，WAL 模式）
- `./data/assets/{hash[0:2]}/{hash[2:4]}/{hash}.webp` — SHA-256 内容寻址图片资产库

### Mock 模式（离线全闭环，默认推荐开发方式）

设置 `USE_MOCK_LLM=True` 后，VLM 诊断、脚手架/变式内核生成、双盲求解与节点裁判全部走内置确定性 fixture（`backend/services/llm_mocks.py`），**零真实网络**即可跑通"录入 → 门禁 → 三阶脚手架 → 拆拐杖 → 延迟盲测 → 知识网/Elo 看板"全链路；SymPy 沙盒验算与 SQLite 存储始终为真实实现。

```bash
USE_MOCK_LLM=True .venv/bin/uvicorn backend.main:app --reload
```

### 真实模型网关配置（.env）

在项目根创建 `.env`（pydantic-settings 自动加载，环境变量优先）：

```dotenv
USE_MOCK_LLM=False
LLM_BASE_URL=https://api.openai.com/v1   # 任意 OpenAI 兼容网关
LLM_API_KEY=sk-...
VLM_MODEL_NAME=gpt-4o                    # 多模态诊断/转录模型
SOLVER_LLM_MODEL_NAME=gpt-4o-mini        # 双盲交叉求解模型
LLM_TEMPERATURE=0.1
```

其余超参数（沙盒配额、知识网阈值、盲测窗口、Elo K 系数等）见 [backend/config.py](backend/config.py)，均可用同名环境变量覆盖。

## 测试指南

```bash
# 全量总门禁（交付验收：覆盖率 >= 85% + Lint + 类型检查）
.venv/bin/python -m pytest -v --cov=backend --cov-fail-under=85 \
  && .venv/bin/ruff check backend/ tests/ \
  && .venv/bin/mypy backend/

# CUJ 1~4 全链路 E2E（Mock LLM + 真实 SQLite + 真实 SymPy 沙盒，零真实网络）
.venv/bin/python -m pytest tests/test_e2e_cuj_flows.py -v

# 各阶段验收门禁
.venv/bin/python -m pytest tests/test_difficulty_and_elo.py -k "rubric or clamp" -v  # Phase 1
.venv/bin/python -m compileall -q backend tests                                     # Phase 1
.venv/bin/python -m pytest tests/test_sympy_sandbox.py -v                            # Phase 2
.venv/bin/python -m pytest tests/test_difficulty_and_elo.py tests/test_knowledge_graph_dsu.py -v  # Phase 3
```

测试约定：`tests/conftest.py` 提供临时 SQLite/CAS 夹具；API/E2E 测试通过 monkeypatch `settings` 路径 + `httpx.ASGITransport` 直驱真实 ASGI 应用，时间旅行用 `active_date` 注入，不依赖真实时钟与网络。

## 目录结构

```
backend/
  main.py                     # FastAPI 应用工厂 + 7 个 REST 端点 + /assets + 前端静态挂载
  config.py                   # Settings：超参数/网关/沙盒配额/存储路径（.env 可覆盖）
  observability.py            # P5 观测层：结构化行为日志 + request_id 上下文 + 脱敏截断
  db/schema.py                # SQLite DDL 与连接管理（WAL、外键、幂等建表）
  sandbox/sympy_verifier.py   # Python AST 静态探针 + 平台兼容 SymPy 子进程沙盒
  services/
    cas_store.py              # SHA-256 内容寻址资产库（WebP 转码、长边限制）
    difficulty_rubric.py      # 四维难度 [C,S,T,L] → base_elo + 8 档 tier + 保底推演
    llm_gateway.py            # OpenAI 兼容网关（重试/超时/Mock 路由）
    llm_mocks.py              # 离线 Mock fixture（诊断/内核/双盲/节点裁判）
    vlm_diagnosis.py          # 多模态诊断 Payload 校验（步骤骨架 + 草稿对齐）
    ingest_service.py         # 录入：CAS 归档 + 诊断 + 幂等重入库 + 确认卡
    scaffold_engine.py        # 门禁三分支 + 微观状态机 + 思考窗口异步预热
    knowledge_graph.py        # 三层知识网 + CanonicalNodeResolver + 可逆并查集 Compaction
    elo_engine.py             # 双轨 Elo 事件溯源 + 水位门控 + 保底 + Escrow 守恒
    blind_drill_scheduler.py  # 每日盲测题单（配额/顺延/幂等锁）+ 双重核验销账
frontend/                     # 原生 Web 工作台（index.html / app.js / styles.css）
tests/                        # 单元 + 集成 + E2E（test_e2e_cuj_flows.py 为全链路验收）
docs/
  designs/adaptive_practice_harness.md  # 权威设计文档（CUJ/Schema/算法/API 契约）
  dev_plan.md                 # 分阶段开发计划与验收门禁
  tasks/                      # 各阶段任务契约（P1-A ~ P4-C）
理论基础.md                    # 教育学/测量学理论依据（四维 Rubric、双轨 Elo、五边拓扑）
```

## 观测与审计

运行时行为日志（P5 观测层，`backend/observability.py`）让系统透明化：LLM 调用、沙盒执行、
FSM 流转、预热流水线、降阶/升层、盲测调度、知识网操作、API 请求全部以结构化事件留痕，
与 DB 审计账本互补。日志不改任何业务行为；序列化异常降级为 repr 截断，绝不击穿业务。

### 日志位置与格式

- 文件：`data/logs/harness.jsonl`（JSON Lines；滚动 `LOG_MAX_BYTES`=5MB × `LOG_BACKUP_COUNT`=5 备份）
- 控制台：人类可读「时间 级别 模块 事件 摘要」（`LOG_CONSOLE=False` 关闭；`LOG_LEVEL` 控制级别）
- 每请求生成 `request_id`（uuid4），经 contextvar 贯穿该请求触发的全部事件，响应头回传 `X-Request-ID`

JSONL 示例：

```json
{"ts": "2026-11-13T00:24:48.123+00:00", "level": "INFO", "logger": "backend.services.scaffold_engine", "event": "fsm.transition", "request_id": "3f2a9c…", "session_id": "sess-…", "question_uid": "q-…", "from_stage": "STAGE_1_BLIND_PROBE", "to_stage": "STAGE_2_MICRO_SCAFFOLD", "trigger": "stage1_stuck"}
```

### 事件目录速查

| 模块 | 事件 | 关键字段 |
| :--- | :--- | :--- |
| main | `api.request` | method, path, status, duration_ms, request_id |
| ingest | `ingest.received` / `ingest.reimport` / `ingest.diagnosis` / `ingest.persisted` | reimport 带 mode=short_circuit\|stem_updated\|new_draft_session；diagnosis 带 base_elo, tier, error_category, stalled_step, step_count |
| scaffold | `gate.confirmed` | branch, overridden, floor_applied, new_stage |
| scaffold | `fsm.transition` / `fsm.submission` | from_stage, to_stage, trigger；submission 带 stage, correct, no_idea |
| scaffold | `descent.triggered` / `descent.generated` / `descent.floor_reveal` / `ascent.step` | depth, elo_before/elo_after, reason=atomic\|depth_cap\|elo_floor\|gen_failed |
| scaffold | `prewarm.started` / `prewarm.local_hit` / `prewarm.kernel_attempt` / `prewarm.dual_blind` / `prewarm.completed` / `prewarm.failed` | kind, attempt, ast_ok, sandbox_ok, error_kind, duration_ms, status, consensus |
| sandbox | `sandbox.exec` | ok, error_kind, duration_ms（mode=kernel\|equivalence） |
| gateway | `llm.request` | purpose, model, mock, attempt, latency_ms, ok, error_kind |
| elo | `elo.event` | trigger, idempotency_key, applied, coverage_before/after, combat_before/after, gamma |
| elo | `escrow.frozen` / `escrow.realized` / `escrow.forfeited` / `escrow.decayed` | amount, decay_factor |
| drill | `drill.queue_locked` / `drill.submission` / `drill.cascade` / `drill.retrain` | active_date, uids, deferred_uids；intermediate_ok, final_ok, passed；retrain_stage |
| graph | `graph.node_reused` / `graph.node_created` / `graph.compaction_merged` / `graph.reverted` / `graph.co_error` | l1/l2/l3, batch_id, from_node, to_node, merges |

所有事件携带相关 `session_id` / `question_uid`（如适用）；幂等命中（applied=false）亦留痕。

### 审计命令

```bash
# 实时跟踪
tail -f data/logs/harness.jsonl

# 按请求链路过滤（request_id 取自响应头 X-Request-ID）
grep '"request_id": "<rid>"' data/logs/harness.jsonl | jq .

# 按会话审计 FSM 流转
jq 'select(.session_id == "<sid>" and .event == "fsm.transition")' data/logs/harness.jsonl

# 按题目审计 Elo 结算（含幂等 applied=false 留痕）
jq 'select(.question_uid == "<uid>" and .event == "elo.event")' data/logs/harness.jsonl

# 沙盒失败排查
jq 'select(.event == "sandbox.exec" and .ok == false)' data/logs/harness.jsonl

# LLM 调用耗时与失败分类
jq 'select(.event == "llm.request") | {purpose, mock, attempt, latency_ms, ok, error_kind}' data/logs/harness.jsonl
```

### 安全红线

- 绝不记录 `LLM_API_KEY` / `Authorization` 头 / 完整请求体；密钥类字段名一律脱敏，
  字符串值中出现的真实密钥与 Bearer 令牌替换为 `***REDACTED***`；
- 题干等内容字段截断 ≤120 字符（全文在 DB）；
- 日志只写 method/path/status 等元数据，不写请求体与查询参数。

### 与 DB 审计账本的关系

- `elo_mutation_ledger` / `compaction_audit_ledger`：不可变业务账本，是 Elo 确定性重放与
  合并回滚的**事实来源**（事务内原子写入，永不滚动删除）。
- `harness.jsonl`：运行时观测层，记录「发生了什么、耗时多少、是否幂等命中、失败原因」，
  覆盖账本之外的行为（LLM/沙盒/FSM/预热/API）。两者经 `question_uid` / `session_id` /
  `idempotency_key` / `batch_id` 关联互查；日志滚动过期不影响账本审计。

## 文档索引

- [设计文档（权威规格）](docs/designs/adaptive_practice_harness.md) — CUJ 1~4、SQLite Schema、五大模块算法与状态机、API 契约、Verification Plan
- [理论基础.md](理论基础.md) — 四维难度 Rubric、剥皮抽骨题目关联五边拓扑、双轨 Elo 与托管池的理论推导
- [开发计划 docs/dev_plan.md](docs/dev_plan.md) — 分阶段任务分解、依赖关系与每阶段 Definition of Done
- [任务契约 docs/tasks/](docs/tasks/) — P1（存储/CAS/难度基座）→ P2（沙盒/诊断/脚手架）→ P3（知识网/Elo/盲测）→ P4（API/前端/E2E）