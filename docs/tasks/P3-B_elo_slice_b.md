# P3-B 任务契约：Elo 引擎切片 B（兑现/ forfeit / 衰减 / Replay）+ 全流程守恒测试

## 上下文
项目：数理自适应刷题 Harness。你负责双轨 Elo 的 Phase 3 切片 B，在切片 A（已验收，62 测试）基础上补齐盲测兑现、失败清零、温和超时衰减与确定性重放，并按设计 #15 补全 tests/test_difficulty_and_elo.py 的 Elo 部分。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.5 法则 3 全节**、Target File **#11/#15**、CUJ 3。设计文档是唯一权威规格。
切片 A 现状：`backend/services/elo_engine.py` 已实现 expected_pass/gamma/get_module_elo/record_mastered_init/apply_draft_subgoal_floor/freeze_decrutch_escrow，文件尾有 `# ---- Phase 3 切片 B ----` 占位。**不得改动切片 A 已有函数与测试**（tests/test_elo_engine.py 归切片 A，勿动）。

## 你拥有的文件
- `backend/services/elo_engine.py`（仅在切片 B 占位区追加）
- `tests/test_difficulty_and_elo.py`（仅追加 Elo/escrow 测试；**不得改动既有 rubric/clamp 测试**，追加后 `-k "rubric or clamp"` 仍须全绿）

**禁止**触碰其他文件。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：`.venv/bin/python -m pytest tests/test_difficulty_and_elo.py tests/test_elo_engine.py -v` + ruff/mypy 零告警 + 全量无回归
- 测试数据直接 SQL 构造 questions/escrow 行 + 调用切片 A 函数，不依赖 ingest/scaffold（并行开发中）

## API 契约（conn 调用方管理，不 commit，同切片 A 模式）
```python
def realize_or_forfeit_blind_test(
    conn, question_uid: str, session_id: str, passed: bool,
) -> LedgerEventResult: ...
# passed=True：解冻 escrow_combat_delta * decay_factor 合入 combat；
#   escrow_status='REALIZED_MASTERED'；cov=max(cov,combat)；写 BLIND_TEST_REALIZE 事件
#   （幂等键 {uid}:{session_id}:BLIND_TEST_REALIZE；decay_factor==1 时 floor+已兑现 ≡ target 严格守恒）
# passed=False：escrow_combat_delta=0、escrow_status='FORFEITED_RESET'；combat/cov 分文不动；
#   不写账本事件（枚举无 forfeit trigger），返回 event_id=None、before==after
# 幂等：池已 REALIZED/FORFEITED → 二次调用无操作（applied=False）

def apply_overdue_escrow_decay(conn, active_date: date) -> int: ...
# 仅对满足全部条件的题衰减：mastery_state='PENDING_RETEST' 且 escrow_status='FROZEN_PENDING_BLIND'
#   且 slated_drill_date == active_date（当日已排入题单）且 slated_exposure_count > settings.DECAY_EXPOSURE_THRESHOLD
#   （系统顺延题当日未排入 → 天然冻结，不得衰减）
# decay_factor = max(0.0, decay_factor - settings.ESCROW_DAILY_DECAY)；
# 写 ESCROW_TIMEOUT_DECAY 事件（combat/cov before==after，watermark_gamma=新 decay_factor，中文解释条），
# 幂等键 {uid}:{active_date.isoformat()}:ESCROW_TIMEOUT_DECAY（同日重复调用不重复衰减）
# 返回本次衰减题数；绝不扣减 combat/cov 基线

def replay_module_elo_from_ledger(conn, l1_module: str) -> DualEloState: ...
# 从 MODULE_ELO_INITIAL 起按 event_id 顺序重放全部事件增量，校验 before/after 链逐事件衔接
# （发现断链抛 ValueError）；终态必须与 get_module_elo 一致（确定性重建）
```

## 测试要求（设计 #15 逐条，追加到 test_difficulty_and_elo.py，命名含 elo/escrow）
- **四阶段全流程守恒**：同一错题依次「草稿保底 → 拆拐杖冻结 → 盲测兑现」后，combat 净增量 ≡ K_COMBAT*(1-E(snapshot, D_orig))（abs<1e-9）；与「直接独立做对」对照
- decay_factor=0.7 时兑现 = escrow*0.7，守恒式相应折减；floor 不受影响
- **全程不倒挂**：混合事件序列每步 combat<=coverage
- forfeit：escrow 清零、combat/cov 不变、状态 FORFEITED_RESET、再调用幂等
- 衰减：exposure=4 且当日 slated → -0.15；exposure=3 → 不衰减；非当日 slated（顺延期）→ 不衰减；同日二次调用幂等；decay 下限 0；**combat/cov 基线逐事件不变**
- replay：与 get_module_elo 一致；篡改账本中间 after 值 → 断链 ValueError；空模块 → 1200/1200
- 追加后回归：`-k "rubric or clamp"` 71 条仍全绿

## 完成标准
门禁全绿 + 全量无回归。最终消息汇报：文件、新增测试数、门禁摘要、守恒律验证结论。
