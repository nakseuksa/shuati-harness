# P2-B 任务契约：LLM 网关（Mock 优先）+ VLM 诊断服务

## 上下文
项目：数理自适应刷题 Harness。你负责 Phase 2 的模型网关与多模态诊断服务。全项目离线开发依赖你提供的 Mock 机制。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.1 第 3/4 条（六大陷阱算子、一次性草稿转录、一题多解、Diff Gate）**、Target File **#8**、Risks 表「外部 LLM/VLM 网关」行、NFR 的 Token 经济学段。设计文档是唯一权威规格。
`llm_gateway.py` 与 `llm_mocks.py` 是 PM 批准的新增文件（设计文件清单之外，见 dev_plan §6.3）。

## 你拥有的文件
- `backend/services/llm_gateway.py`
- `backend/services/llm_mocks.py`
- `backend/services/vlm_diagnosis.py`
- `tests/test_llm_gateway.py`
- `tests/test_vlm_diagnosis.py`

**禁止**触碰其他文件。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：`.venv/bin/python -m pytest tests/test_llm_gateway.py tests/test_vlm_diagnosis.py -v` + ruff/mypy 零告警
- 测试全程 `USE_MOCK_LLM=True`（monkeypatch settings），不发真实网络请求

## API 契约（PM 锁定的跨模块接口，scaffold_engine 将复用网关）
```python
# llm_gateway.py
class LLMGatewayError(RuntimeError): ...

async def chat_json(
    messages: list[dict[str, Any]],      # OpenAI 格式；图片用 {"type":"image_url","image_url":{"url":"data:image/webp;base64,..."}}
    schema: type[BaseModel],             # 期望的结构化输出
    *,
    model: str,                          # 调用方显式传 settings.VLM_MODEL_NAME / SOLVER_LLM_MODEL_NAME
    purpose: str,                        # Mock 路由键：'diagnosis' | 'scaffold_kernel' | 'variant_kernel' | 'solver_blind' | ...
    temperature: float | None = None,    # 缺省 settings.LLM_TEMPERATURE
) -> BaseModel: ...
# 真实模式：httpx.AsyncClient POST {LLM_BASE_URL}/chat/completions，Bearer LLM_API_KEY；
#   response_format={"type":"json_object"}；网络错误/5xx/429 指数退避重试 settings.LLM_MAX_RETRIES 次；
#   JSON 解析或 Pydantic 校验失败 → 附校验错误信息修复重试 1 次；最终失败抛 LLMGatewayError
# Mock 模式（settings.USE_MOCK_LLM）：从 llm_mocks.MOCK_RESPONSES[purpose] 取 payload，
#   经 schema 校验后返回；purpose 未注册抛 LLMGatewayError（不静默）

def image_path_to_data_url(path: Path) -> str: ...  # 读文件 → base64 data URL（供 VLM 消息构造）

# llm_mocks.py
MOCK_RESPONSES: dict[str, dict[str, Any]] = {...}
# 内置默认 fixture：'diagnosis'（完整 DiagnosisPayload，含 3 步物理题骨架、草稿对齐卡在 Step 2、
#   四维 (3,3,2,3)、trap_tags=['TRAP_DOMAIN_BOUNDARY']、遮罩条件与 probe_question_md）
# 'scaffold_kernel' / 'variant_kernel'（内核先行 JSON：parametric_stem_tpl / sympy_solver_code / constraints_check，
#   solver 代码必须能过 AST 探针且真实可解——用简单几何/代数题）
# 'solver_blind'（双盲求解答案 {"answer_latex": "...", "reasoning_brief": "..."}，与内核题答案一致）
# 测试可 monkeypatch 本字典注入畸形/失败场景

# vlm_diagnosis.py（Pydantic Schema，字段名与设计 #8 对齐）
class StepSkeletonItem(BaseModel):
    step_index: int; step_goal_desc: str; step_formula_latex: str
    l1_module: str; l2_entity: str; l3_operator: str
    c_sub: int; s_sub: int; t_sub: int; l_sub: int
    masked_conditions: list[str]; probe_question_md: str

class OptimalSolutionTrace(BaseModel):
    steps: list[StepSkeletonItem]
    standard_answer_latex: str
    c_score: int; s_score: int; t_score: int; l_score: int
    trap_tags: list[str]

class DraftAlignmentResult(BaseModel):
    draft_steps_latex: list[str]
    matched_branch_id: str                       # 'canonical' | 'user_alt_1'
    alt_branch_steps: list[StepSkeletonItem] | None
    last_valid_step_index: int
    stalled_step_index: int
    error_category: Literal["ALREADY_MASTERED", "TRAP_OR_CALC_ERROR", "CONCEPT_BOTTLENECK"]
    trap_taxonomy_tag: str | None

class DiagnosisPayload(BaseModel):
    trace: OptimalSolutionTrace
    alignment: DraftAlignmentResult

async def analyze_question_and_draft(
    stem_md: str,
    stem_images: list[Path],
    draft_images: list[Path],
    target_grade: str,
    existing_l1_l2_tree: dict[str, list[str]],
) -> DiagnosisPayload: ...
```

## 实现规格
1. **单次 VLM 调用**：trace + alignment 合并为一个结构化请求（purpose='diagnosis'），题干/草稿图片以 data URL 附在消息中；Prompt 注入 existing_l1_l2_tree 骨架与六大 TRAP 算子枚举、四维行为级锚点定义（照设计 §3.1）。
2. **空白草稿短路**：draft_images 为空时，alignment 直接置 `draft_steps_latex=[], matched_branch_id='canonical', alt_branch_steps=None, last_valid_step_index=0, stalled_step_index=1, error_category='CONCEPT_BOTTLENECK', trap_taxonomy_tag=None`，不依赖 VLM 的对齐输出（trace 仍取 VLM 结果）。
3. **后处理校验**：stalled_step_index ∈ [1, len(steps)]；last_valid < stalled；error_category 与 trap_taxonomy_tag 一致性（TRAP_OR_CALC_ERROR 时 tag 必填，否则置 None）；维度分交给下游 rubric 校验，此处只做范围断言。
4. **一题多解**：matched_branch_id='user_alt_1' 时 alt_branch_steps 必须非空，否则降级为 canonical 并在 payload 中修正。
5. 网关超时：httpx.Timeout(60.0)；重试退避 1s/2s；所有异常收敛为 LLMGatewayError。

## 测试要求
- 网关 Mock 路由：已注册 purpose 返回校验后模型；未注册抛错；USE_MOCK_LLM=False 时用 monkeypatch 假 httpx  transport 验证请求体/重试/修复重试/最终失败（不发真网络）
- 畸形 JSON → 修复重试路径；schema 不匹配 → LLMGatewayError
- 诊断：默认 fixture 全链路；空白草稿短路；stalled 越界修正；alt 分支缺失降级；image_path_to_data_url 格式
- 断言 Mock 的 sympy_solver_code 能通过 `backend.sandbox.sympy_verifier.validate_script_ast`（若该模块尚未就位，用 importorskip 跳过此条并注明）

## 完成标准
门禁全绿。最终消息汇报：文件、测试数、门禁摘要、Mock fixture 覆盖的 purpose 清单。
