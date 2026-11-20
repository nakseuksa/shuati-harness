# P3-C 任务契约：每日盲测调度器（配额熔断 + 防剧透 + 双重核验 + 连带销账）

## 上下文
项目：数理自适应刷题 Harness。你负责延迟盲测的调度与销账收口。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.3 第 2 条全节**、**CUJ 3**、Target File **#12**。设计文档是唯一权威规格。
前置：schema、elo_engine 切片 A+B（realize_or_forfeit_blind_test / apply_overdue_escrow_decay）、knowledge_graph（decrement_co_error_edges_on_mastery / CanonicalNodeResolver）、sandbox 等价引擎。

## 你拥有的文件
- `backend/services/blind_drill_scheduler.py`
- `tests/test_blind_drill_scheduler.py`
- `backend/db/schema.py`（**仅允许追加**下述 blind_drill_attempts 表 DDL 与索引，不得改动既有 9 表）

**禁止**触碰其他文件。**禁止** git commit/push。

## PM 批准的 Schema 扩展（追加到 TABLE_DDLS）
```sql
CREATE TABLE IF NOT EXISTS blind_drill_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
    drill_question_uid TEXT NOT NULL REFERENCES questions(question_uid),
    active_date DATE NOT NULL,
    intermediate_correct INTEGER,          -- NULL = 本题不要求中间量
    final_correct INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```
用途：变式曝光追踪（"未曝光过"判据）+ 失败重训分流的作答日志。索引：(question_uid, active_date)。

## API 契约
```python
class BlindDrillItemDTO(BaseModel):
    question_uid: str                  # 原题 uid（销账主键）
    drill_question_uid: str            # 实际呈现题（变式或原题）
    stem_md: str                       # 已剥离来源/L1/L2/L3 标签
    verification_mode: Literal["final_answer", "equation_skeleton"]
    allowed_symbols: list[str]
    probe_question_md: str | None      # 仅回退原题时携带（中间子目标核验）
    requires_intermediate: bool

def get_daily_blind_drill_queue(
    conn, active_date: date, limit: int | None = None,   # 缺省 settings.DAILY_BLIND_DRILL_CAP
) -> list[BlindDrillItemDTO]: ...

class BlindDrillOutcomeDTO(BaseModel):
    passed: bool; mastery_state: str
    retrain_stage: str | None          # 失败时新会话起点
    cascaded_question_uids: list[str]  # 连带销账的子题
    explanation_text: str | None

def evaluate_blind_drill_submission(
    conn, question_uid: str, final_answer: str,
    intermediate_sub_answer: str | None = None, active_date: date | None = None,
) -> BlindDrillOutcomeDTO: ...
```

## 实现规格
### 1. 每日题单（幂等锁 + 配额熔断）
- 当日已锁定（slated_drill_date == active_date）→ 幂等返回锁定题单，不重复递增曝光。
- 否则选 PENDING_RETEST 且 blind_test_unlock_at <= active_date 的题，按 unlock_at ASC 取前 limit：置 slated_drill_date=active_date、slated_exposure_count += 1。
- **溢出顺延**：其余到期题 unlock_at/due_at 各 +1 天、system_deferred_days += 1，当日不排入（衰减天然冻结）。
- **呈现题选择**：优先 blind_drill_attempts 中从未曝光过的 CLONE/ISOMORPHIC 变式（question_relations 查原题的变式）；无可用变式 → 回退原题，requires_intermediate=True 且携带原会话 probe_question_md（曾卡壳步）。
- verification_mode：原题 L>=2 → equation_skeleton（allowed_symbols 从题干/步骤公式提取符号集），L=1 → final_answer。
- DTO 严禁携带 primary_l1_module/知识点路径/parent 信息。
### 2. 提交销账
- 呈现题确定性重放（同 active_date 同选择逻辑）；答案用 verify_equation_equivalence 判定。
- 回退原题：中间量（对卡壳步 step_formula_latex）与最终答案**双正确**才 pass；中间量缺失/错误 → fail（即使最终对）。
- pass → elo.realize_or_forfeit_blind_test(passed=True) + mastery='MASTERED' + knowledge_graph.decrement_co_error_edges_on_mastery + **连带销账**：SCAFFOLD_PRE 边 source=子题A target=本题B 且 containment_score>=0.85 且 A 为 PENDING_RETEST → A 逐题 realize+MASTERED（各自 escrow 独立兑现）。
- fail → realize_or_forfeit(passed=False) + mastery='ACTIVE' + 新建重训 diagnostic_sessions：中间量也错/缺 → STAGE_1_BLIND_PROBE；中间量对仅最终错 → STAGE_4_DECRUTCH_VARIANT（error_category 沿用原会话）。
- 写 blind_drill_attempts 日志；全程单事务。
### 3. 衰减接线
- 题单锁定后调用 elo.apply_overdue_escrow_decay(conn, active_date)（当日已排入且曝光>3 的题按日 -0.15；顺延题冻结）。

## 测试要求
- 幂等锁：同日二次 GET 同题单、曝光不重复递增；跨日重新锁定
- 配额：4 道到期题 → 3 道入单 + 1 道顺延（日期+1、deferred+1、当日不排入）；unlock ASC 排序
- 剥标签：DTO 无模块/路径/parent 字段；变式优先且未曝光判据生效；无变式回退原题带 probe
- 双重核验：原题中间错+最终对 → fail；双对 → pass；变式只核最终
- pass：MASTERED + escrow 兑现（combat 增量=escrow*decay）+ 标红边 active 递减
- 连带销账：I=0.9 子题连带 MASTERED；I=0.7 不连带；子题非 PENDING 不连带
- fail：ACTIVE + escrow 清零 + 重训起点分流两分支 + attempts 日志落库
- 衰减集成：曝光 4 次且当日 slated 的题 decay_factor 下降；顺延题不动
- L>=2 equation_skeleton 模式 + allowed_symbols 非空

## 完成标准
门禁：.venv/bin/python -m pytest tests/test_blind_drill_scheduler.py -v 全绿 + ruff/mypy 零告警 + 全量无回归（含既有 schema 测试）。最终消息汇报：文件、测试数、门禁摘要。
