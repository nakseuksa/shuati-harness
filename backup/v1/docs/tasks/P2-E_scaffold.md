# P2-E 任务契约：脚手架引擎（微观 FSM + 思考窗口异步预热）

## 上下文
项目：数理自适应刷题 Harness。你负责 Phase 2 的收口模块：微观训练会话状态机、倒置三阶脚手架流转、思考窗口投机预热（本地检索 → 内核先行生成 → AST+SymPy 沙盒 → 双盲降级）。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.2 全节**、**§3.3 状态机图与第 1 条**、**CUJ 2/3**、Target File **#9**。设计文档是唯一权威规格。
前置已就位：schema、rubric、cas、sandbox（P2-A）、llm_gateway/vlm_diagnosis（P2-B）、elo_engine 切片 A 含 freeze_decrutch_escrow（P2-C）、ingest_service（P2-D）。

## 你拥有的文件
- `backend/services/scaffold_engine.py`
- `tests/test_scaffold_engine.py`

**禁止**触碰其他文件。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：`.venv/bin/python -m pytest tests/test_scaffold_engine.py -v` + ruff/mypy 零告警
- 测试全程 USE_MOCK_LLM=True + 真实 SQLite（tmp_db）+ 真实沙盒；用 ingest_service 造题（monkeypatch 诊断）

## API 契约
```python
class SessionStepResponse(BaseModel):
    session_id: str; question_uid: str
    micro_stage: str; mastery_state: str
    prewarm_status: str
    view_payload: dict[str, Any]      # 当前阶段前端所需：遮罩条件/probe、微母题题干、原题题干、变式题干+allowed_symbols+验证模式
    explanation_text: str | None      # Elo 加分解释条（来自账本事件）

async def confirm_diagnostic_gate(
    session_id: str, confirmed_stalled_step: int, confirmed_category: str,
    db_path: Path | None = None,
) -> SessionStepResponse: ...

async def prewarm_session_questions(session_id: str, db_path: Path | None = None) -> None: ...

async def submit_stage_answer(
    session_id: str, user_answer: str, self_assessment: str | None = None,
    db_path: Path | None = None,
) -> SessionStepResponse: ...
```

## 实现规格
### 1. confirm_diagnostic_gate（设计 #9 三分支）
- 写 user_confirmed_gate=1、user_overridden（与 VLM 原结论不一致时）、更新 stalled_step_index/error_category；同步刷新 steps.is_blocked_step。
- `ALREADY_MASTERED` → elo.record_mastered_init → mastery_state='MASTERED'、micro_stage='SESSION_COMPLETED'。
- `CONCEPT_BOTTLENECK` 且 last_valid_step_index>=1 → elo.apply_draft_subgoal_floor(d_sub=steps[last_valid-1].sub_elo)，explanation 取账本解释条。
- `CONCEPT_BOTTLENECK` → STAGE_1_BLIND_PROBE，view_payload=卡点步的 masked_conditions+probe_question_md；`TRAP_OR_CALC_ERROR` → STAGE_4_DECRUTCH_VARIANT（推 SHARED_TRAP 变式）。两者均以 asyncio.create_task 启动 prewarm 并登记 `in_flight_prewarm_tasks: dict[str, asyncio.Task]`（模块级）。
### 2. prewarm（§3.2）
- 并发两任务：二阶微母题（SCAFFOLD_PRE，针对 stalled 步）+ 四阶变式（ISOMORPHIC/SHARED_TRAP，整题）。
- **本地检索优先**：SCAFFOLD_PRE 候选=同主考点、步数≤2、D_orig−D_cand≥200；ISOMORPHIC=同考点集且 |ΔD|≤150；SHARED_TRAP=trap_tags 交集。候选题 origin_type 不限，但**合成题不得再作为母题衍生**（检索命中的合成题可直接复用，不得以其为 parent 再生成）。
- 未命中 → 内核先行：gateway.chat_json(purpose='scaffold_kernel'/'variant_kernel') 取 {parametric_stem_tpl, sympy_solver_code, constraints_check} → validate_script_ast → run_sympy_in_sandbox；失败附 stderr_tail 重试 ≤ settings.SYMPY_MAX_RETRIES；仍失败或非代数题 → **双盲 LLM**：两次独立零上下文 chat_json(purpose='solver_blind') 核对 answer_latex 一致性（用 verify_equation_equivalence 或规范化字符串比对）。
- 落库：questions（origin_type='AI_SYNTHETIC_SCAFFOLD'/'AI_SYNTHETIC_VARIANT'，parent_question_uid=原题，verification_status='SYMPY_VERIFIED'/'DUAL_LLM_FALLBACK'，verification_artifact_json 存脚本+答案快照）+ question_relations（SCAFFOLD_PRE is_directed=1 source=子题 target=原题；confidence_source='SYMPY_KERNEL_GEN'/'LLM_PRIOR'）+ 回填 session.prewarm_scaffold_uid/prewarm_variant_uid/prewarm_status（READY_SYMPY/READY_FALLBACK/FAILED）。
- 落库用独立 get_db_connection 事务；异常不得击穿前台（捕获置 FAILED）。
### 3. submit_stage_answer（状态机图逐边实现）
- STAGE_1：答案与卡点步 step_formula_latex 代数等价（verify_equation_equivalence）→ STAGE_3_REDO_ORIGINAL；错/自评无思路 → STAGE_2_MICRO_SCAFFOLD（await 在途预热 Task，view=微母题题干；READY_FALLBACK 时附 ⚠️ 徽章字段）。
- STAGE_2：与微母题 standard_answer_latex 等价 → STAGE_3。
- STAGE_3：与原题 standard_answer_latex 等价 → STAGE_4（view=变式题干；L>=2 → equation_skeleton 模式+allowed_symbols，L=1 → final_answer 模式）；错 → 停留并反馈。
- STAGE_4：L>=2 核心方程组等价 / L=1 最终答案等价 → **elo.freeze_decrutch_escrow** + mastery_state='PENDING_RETEST' + 盲测窗口（TRAP_OR_CALC_ERROR → settings.BLIND_WINDOW_TRAP_DAYS，否则 CONCEPT_DAYS；写 blind_test_unlock_at/due_at）+ micro_stage='SESSION_COMPLETED'；错 → STAGE_COOLDOWN_DEFERRED（次日重测，view=骨架对比卡数据）。
- STAGE_COOLDOWN_DEFERRED：冷却期满（updated_at 次日）再提交 → 回 STAGE_4。
- 支持断点续传：任何阶段可挂起（会话保留 micro_stage，次日继续）。
### 4. 通用
- 所有状态迁移单事务；非法阶段提交抛 ValueError；预热未完成时请求二阶/四阶 → await 在途 Task（正常流 P95<2s 由预热保障，测试验证 await 语义）。

## 测试要求（Mock LLM + 真实沙盒 + 真实 SQLite）
- 三分支 confirm：MASTERED 直接归档（账本有 MASTERED_INIT）；CONCEPT_BOTTLENECK 带前缀 → 保底发放+解释条；TRAP → 直入 STAGE_4
- 预热：Mock 内核题过沙盒 → READY_SYMPY + 合成题落库 + SCAFFOLD_PRE 有向边；Mock 坏脚本（monkeypatch MOCK_RESPONSES）→ 重试后双盲 → READY_FALLBACK + verification_status；双盲也不一致 → FAILED
- 全链路 CUJ2/3：一阶错→二阶→三阶→四阶（L>=2 方程模式）→ PENDING_RETEST + 覆盖分结算 + escrow 冻结 + 盲测窗口日期正确
- 一阶答对 → 直跳 STAGE_3；四阶错 → COOLDOWN；幂等：重复提交同阶段不重复结算 Elo
- 合成题不衍生：以合成题为 parent 的生成请求被拒绝

## 完成标准
门禁全绿。最终消息汇报：文件、测试数、门禁摘要、状态机边覆盖清单。
