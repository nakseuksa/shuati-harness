# P7-A 任务契约：录入答题区与掌握判定（后端）

## 上下文
项目：数理自适应刷题 Harness。用户需求：录入页增加文字答题区 + 「已掌握」选项；勾选、或回答「已掌握」、或填入正确答案时，视为已掌握题——直接入库 + 提取知识点 + Elo 结算，跳过练习流程。
现状：`ingest_question` 无文字作答入参；无草稿默认 CONCEPT_BOTTLENECK；`initial_state='已掌握'` 仍需走 confirm-gate 才结算。
必读：docs/designs/adaptive_practice_harness.md CUJ 1、§3.1；现有 backend/services/ingest_service.py 与 scaffold_engine.confirm_diagnostic_gate 的 ALREADY_MASTERED 分支（结算逻辑参照）。

## 锁定接口契约（前端 P7-B 并行按此对接，不得更改字段名）
### ingest_question 新增参数
```python
async def ingest_question(
    stem_md, stem_images, draft_images, target_grade,
    initial_state="错题", base_dir=None, db_path=None,
    user_answer: str = "", mastered_claim: bool = False,   # 新增
) -> GateCardDTO
```
### GateCardDTO 新增字段（向后兼容）
```python
auto_mastered: bool = False
mastery_explanation: str | None = None   # record_mastered_init 的账本解释条
```
### API /api/v1/questions/ingest（multipart）新增 Form 字段
`user_answer: str = ""`、`mastered_claim: bool = False`（FastAPI Form(bool) 接受 "true"/"false"）。

## 实现规格
1. **掌握判定**（诊断产出 trace.standard_answer_latex 之后）：
   ```
   mastered = mastered_claim or initial_state == "已掌握" or _is_mastered_text(user_answer)
   if not mastered and user_answer.strip():
       mastered = await asyncio.to_thread(answers_equivalent, user_answer, trace.standard_answer_latex)
   ```
   `_is_mastered_text`：strip 后长度 ≤12 且属于 {"已掌握","我已掌握","掌握了","我会做"}（模块级常量）。
2. **掌握路径（单事务，录入即结算）**：error_category='ALREADY_MASTERED'；落库 questions/steps/session 后同事务内：
   - `elo_engine.record_mastered_init(conn, question_uid, session_id)`（水位线门控等既有 Elo 规则不变）
   - questions.mastery_state='MASTERED'；session.micro_stage='SESSION_COMPLETED'、user_confirmed_gate=1
   - 所有 steps.is_blocked_step=0；floor_preview_elo=None
   - GateCardDTO.auto_mastered=True + mastery_explanation=账本解释条
   - 不启动预热、不进入门禁练习流
3. **未掌握路径**：行为与现状完全一致（含无草稿默认 CONCEPT_BOTTLENECK）；错误答案不改变默认流程。
4. **重入库幂等**：既有短路/增量逻辑不变，不重复结算（幂等键兜底）。
5. 等价判定必须 asyncio.to_thread（沙盒子进程阻塞）；判定异常不得击穿录入（捕获→按未掌握处理并 logger.warning）。

## 你拥有的文件
- `backend/services/ingest_service.py`
- `backend/main.py`（仅 ingest 路由 Form 字段）
- `tests/test_ingest_service.py`（追加）
- `tests/test_api_routes.py`（追加）

**禁止**触碰其他文件（sympy_verifier.py 正被 P6 worker 修改，只 import 不编辑）。**禁止** git commit/push。**禁止任何网络调用**（USE_MOCK_LLM=True）。

## 环境与门禁
- 项目根 /Users/wanghaichen/0_work/刷题harness；一律 .venv/bin/
- 基线 692 测试全绿（P6 可能并行追加，全量以实际为准，零回归）
- 门禁：pytest -q --cov=backend --cov-fail-under=85 + ruff + mypy 全绿

## 测试要求（追加）
- mastered_claim=True → auto_mastered + MASTERED + SESSION_COMPLETED + MASTERED_INIT 账本事件 + 步骤全 is_blocked_step=0 + user_confirmed_gate=1
- user_answer="已掌握" / "我已掌握" → 同上
- user_answer=正确答案（Mock 标准答案 v_{max}=20 m/s，提交 "20" 或 "vmax = 20"，用现有引擎能力，不依赖 P6 在途改动）→ auto_mastered
- user_answer=错误答案（"x=999"）→ 正常 ACTIVE + 需门禁 + CONCEPT_BOTTLENECK
- 空答案无草稿 → 默认行为零漂移（回归）
- initial_state="已掌握"（旧开关）→ 统一走 auto_mastered
- 重入库：同内容二次 ingest 不重复结算（账本事件数不变）
- API：multipart 新字段 → 响应 auto_mastered/mastery_explanation；未掌握响应结构不变

## 完成标准
门禁全绿。最终汇报：文件、新增测试数、覆盖率、判定矩阵结果。无法决策 ask_question 问 PM。
