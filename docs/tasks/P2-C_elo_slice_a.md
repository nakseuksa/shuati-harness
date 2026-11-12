# P2-C 任务契约：Elo 引擎切片 A（账本基座 + 水位线 + 保底分）

## 上下文
项目：数理自适应刷题 Harness。你负责 Phase 2 前置的双轨 Elo 切片 A（dev_plan §6.2 批准的跨阶段调整）：事件账本基座、模块分折叠读取、水位线门控、直接掌握结算、草稿子步保底。Escrow 冻结/兑现/衰减/Replay 属 Phase 3 切片 B，**不要实现**。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.5（模块 5 全文，重点法则 1/2 与不变量）**、Target File **#11**、**#15** 的 Elo 测试条目、Alternative 3A（为什么不做原地标量）。设计文档是唯一权威规格。

## 你拥有的文件
- `backend/services/elo_engine.py`
- `tests/test_elo_engine.py`（切片 A 专属测试；tests/test_difficulty_and_elo.py 的全流程守恒测试 Phase 3 再补，勿动该文件）

**禁止**触碰其他文件。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：`.venv/bin/python -m pytest tests/test_elo_engine.py -v` + ruff/mypy 零告警
- 依赖已就位：`backend/db/schema.py`（9 表）、`backend/config.py`（K_INIT/K_FLOOR/MODULE_ELO_INITIAL/WATERMARK_* 等）、conftest 的 tmp_db 夹具
- 若 schema.py 尚未就位（并行开发），用 importorskip 并在汇报中注明，待 PM 复跑

## API 契约（PM 锁定；所有函数接收调用方管理的 sqlite3.Connection，不自行开连接）
```python
@dataclass(frozen=True)
class DualEloState:
    coverage_elo: float
    combat_elo: float

@dataclass(frozen=True)
class LedgerEventResult:
    event_id: int | None        # 幂等命中既有事件时返回原 event_id
    applied: bool               # False = 幂等命中或门控为 0，未产生新事件
    coverage_before: float; coverage_after: float
    combat_before: float; combat_after: float
    explanation_text: str

def expected_pass(rating: float, difficulty: float) -> float: ...
# E(R,D) = 1 / (1 + 10**((D - R)/400))

def get_module_elo(conn: sqlite3.Connection, l1_module: str) -> DualEloState: ...
# 折叠 elo_mutation_ledger 该模块最新事件的 after 值；无事件 → (1200.0, 1200.0)
# 不建可变标量表（Alternative 3A）

def compute_watermark_gamma(question_elo: int, combat_elo: float) -> float: ...
# D >= R-200 → 1.0；R-500 <= D < R-200 → exp(-(R-200-D)^2 / (2*150^2))；D < R-500 → 0.0
# 边界常量用 settings.WATERMARK_FULL_MARGIN / WATERMARK_ZERO_MARGIN / WATERMARK_GAUSS_SIGMA

def record_mastered_init(conn, question_uid: str, session_id: str) -> LedgerEventResult: ...
# 直接录入"已掌握"题：读 questions.base_elo 与 primary_l1_module；
# gamma=compute_watermark_gamma(...)；delta = gamma * K_INIT * (1 - E(R_combat, D))；
# combat += delta；cov = max(cov, combat)；写 MASTERED_INIT 事件（含 before/after、gamma、中文解释条）

def apply_draft_subgoal_floor(conn, question_uid: str, session_id: str, d_sub: int) -> LedgerEventResult: ...
# 仅当 R_combat < d_sub 发放：delta = K_FLOOR * (1 - E(R_combat, d_sub))；
# combat += delta；强制 cov = max(cov, combat)；
# 首次发放时创建 question_escrow_pool 行：initial_combat_elo_snapshot = 发放前 combat、
#   floor_elo_applied = delta、escrow_status='FLOOR_ONLY'；重复调用（同幂等键）不重复发放；
# 写 PARTIAL_DRAFT_SUBGOAL_FLOOR 事件
```

## 实现规格
1. **幂等键**：`f"{question_uid}:{session_id}:{mutation_trigger}"`，UNIQUE 约束兜底；命中既有事件 → 返回原事件快照 `applied=False`，绝不重复加分。
2. **不变量**：任何事件后 `combat <= coverage`（每次写入前强制 max 同步）；事件行完整记录 before/after 双轨四值 + watermark_gamma + eval_difficulty_elo + explanation_text。
3. **解释条**：中文自然语言，含模块名、触发原因、难度基准、增量数值（保留 1 位小数），供前端直接展示。
4. gamma=0 或 delta=0：不写事件，返回 applied=False（before==after）。
5. 事务：函数内不 commit（调用方 get_db_connection 统一管理）；同函数内多表写入保持原子。
6. 切片 B 预留：文件内以 `# ---- Phase 3 切片 B：Escrow/衰减/Replay ----` 注释占位，不写实现。

## 测试要求
- expected_pass 数值抽查（R=D → 0.5；R=D+400 → ~0.909）
- gamma 三段分段与边界（D=R-200 → 1.0；D=R-500 → 0.0；中段高斯值抽查）
- get_module_elo 冷启动 1200/1200；多模块互不串扰
- record_mastered_init：高难题正增量、低难度题 gamma 衰减、远低于水位 gamma=0 不写事件；cov 同步上推
- apply_draft_subgoal_floor：R_combat < d_sub 才发放；escrow 池快照正确（发放前 combat）；floor_elo_applied 记录
- **幂等**：同 (question_uid, session_id, trigger) 二次调用 applied=False 且分数不变
- **不倒挂**：任意事件序列后 combat <= coverage（构造保底分把 combat 推过 cov 的场景验证 max 同步）
- 解释条非空且含关键数值

## 完成标准
门禁全绿。最终消息汇报：文件、测试数、门禁摘要、与设计公式的逐条对照结论。
