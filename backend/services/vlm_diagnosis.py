"""多模态错题诊断服务：一次性草稿转录 + 最优解骨架 + 步骤对齐（Target File #8）。

唯一权威规格：docs/designs/adaptive_practice_harness.md
§3.1 第 3/4 条（六大陷阱算子、一次性草稿转录、一题多解、空白草稿短路）与 Target File #8。
Schema 字段名与 PM 锁定契约（docs/tasks/P2-B_gateway_vlm.md）逐字对齐。

Token 经济学（NFR）：题干/草稿图片仅在本诊断首轮以 data URL 携带一次；
后续三阶脚手架所有轮次严禁重复携带原始图片 Base64（由下游遵守）。
"""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from backend.config import settings
from backend.services.llm_gateway import LLMGatewayError, chat_json, image_path_to_data_url

__all__ = [
    "TRAP_TAXONOMY",
    "DiagnosisPayload",
    "DraftAlignmentResult",
    "OptimalSolutionTrace",
    "StepSkeletonItem",
    "analyze_question_and_draft",
]

# 六大设错/易错陷阱算子（设计 §3.1 第 3 条，逐字注入 Prompt）
TRAP_TAXONOMY: dict[str, str] = {
    "TRAP_DOMAIN_BOUNDARY": (
        "定义域/物理隐含临界边界遗漏（偶次根式、对数真数、分母非零、绳杆松弛临界 N=0）"
    ),
    "TRAP_SIGN_DIRECTION": "矢量方向/正负号反转（功与冲量正负、楞次定律方向、不等式乘负数变号）",
    "TRAP_EXTRANEOUS_ROOT": (
        "增根与物理非解未舍（平方去根号、三角形钝角多解、碰撞后速度穿透违背常识）"
    ),
    "TRAP_FRAME_SHIFT": "参考系或研究对象暗换（整体法与隔离法混用质量 M 与 m）",
    "TRAP_APPROX_CONDITION": "近似或守恒前提越界（小角近似、动量守恒忽略重力/摩擦力冲量）",
    "TRAP_ARITHMETIC_SLIP": "纯移项、通分或符号抄写失误",
}

# 四维行为级锚点（设计 §3.1 第 2 条）
_FOUR_DIM_ANCHORS = (
    "C∈[1,4] 概念耦合与建模深度：1=单一直接物理模型/公式直推；2=两个考点串联（如动能定理+平抛）；"
    "3=跨模块综合（如复合场有界磁场+临界几何）；4=原创隐蔽复合场/导数多极值分类讨论。\n"
    "S∈[1,3] 推理链路长度：1=1～2 步直达；2=3～4 步递进推导；3=5 步以上长链路推导。\n"
    "T∈[1,3] 转化跨度与技巧隐蔽度：1=标准常规套路；2=辅助线/变量代换/参考系变换；"
    "3=高隐蔽极端构造。\n"
    "L∈[1,3] 运算与分类讨论负荷：1=少量整数代数运算；2=多未知量联立立式；"
    "3=高繁复字母代数化简或多情形分类讨论。"
)


class StepSkeletonItem(BaseModel):
    """单步骨架（对齐 question_steps 表字段；step_index 为 1-based）。"""

    step_index: int
    step_goal_desc: str
    step_formula_latex: str
    l1_module: str
    l2_entity: str
    l3_operator: str
    c_sub: int
    s_sub: int
    t_sub: int
    l_sub: int
    masked_conditions: list[str]
    probe_question_md: str


class OptimalSolutionTrace(BaseModel):
    """最优解法步骤骨架 + 整题四维打分与陷阱标签。"""

    steps: list[StepSkeletonItem]
    standard_answer_latex: str
    c_score: int
    s_score: int
    t_score: int
    l_score: int
    trap_tags: list[str]


class DraftAlignmentResult(BaseModel):
    """一次性草稿转录与步骤对齐结论（含一题多解分支）。"""

    draft_steps_latex: list[str]
    matched_branch_id: str  # 'canonical' | 'user_alt_1'
    alt_branch_steps: list[StepSkeletonItem] | None
    last_valid_step_index: int
    stalled_step_index: int
    error_category: Literal["ALREADY_MASTERED", "TRAP_OR_CALC_ERROR", "CONCEPT_BOTTLENECK"]
    trap_taxonomy_tag: str | None


class DiagnosisPayload(BaseModel):
    """单次 VLM 调用的合并结构化输出：trace + alignment。"""

    trace: OptimalSolutionTrace
    alignment: DraftAlignmentResult


async def analyze_question_and_draft(
    stem_md: str,
    stem_images: list[Path],
    draft_images: list[Path],
    target_grade: str,
    existing_l1_l2_tree: dict[str, list[str]],
) -> DiagnosisPayload:
    """单次 VLM 调用完成题干解析 + 草稿一次性转录对齐（PM 锁定签名）。

    - 题干/草稿图片以 data URL 附在消息中，分区标注避免混淆；
    - Prompt 注入既有 L1/L2 树骨架、六大 TRAP 算子枚举与四维行为级锚点；
    - ``draft_images`` 为空时 alignment 走空白草稿短路（trace 仍取 VLM 结果）；
    - 后处理：stalled 范围修正、last_valid < stalled、类别/标签一致性、
      alt 分支缺失降级、四维范围断言（深度 rubric 校验交给下游）。

    Raises:
        LLMGatewayError: 网关调用失败（含 Mock purpose 未注册）。
        ValueError: 后处理硬校验失败（空骨架 / TRAP 缺标签 / 维度分越界）。
    """
    messages = _build_messages(
        stem_md, stem_images, draft_images, target_grade, existing_l1_l2_tree
    )
    result = await chat_json(
        messages, DiagnosisPayload, model=settings.VLM_MODEL_NAME, purpose="diagnosis"
    )
    if not isinstance(result, DiagnosisPayload):  # 防御：schema 固定，理论不可达
        raise LLMGatewayError("diagnosis 网关返回了非 DiagnosisPayload 类型")

    if not draft_images:
        # 空白草稿短路（设计 §3.1 第 4 条）：不依赖 VLM 的对齐输出
        result.alignment = DraftAlignmentResult(
            draft_steps_latex=[],
            matched_branch_id="canonical",
            alt_branch_steps=None,
            last_valid_step_index=0,
            stalled_step_index=1,
            error_category="CONCEPT_BOTTLENECK",
            trap_taxonomy_tag=None,
        )
    _postprocess(result)
    return result


def _build_messages(
    stem_md: str,
    stem_images: list[Path],
    draft_images: list[Path],
    target_grade: str,
    existing_l1_l2_tree: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """构造 OpenAI 格式多模态消息（system 规格 + user 分区图文）。"""
    tree_json = json.dumps(existing_l1_l2_tree, ensure_ascii=False)
    user_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"【学段目标】{target_grade}\n"
                f"【既有 L1/L2 知识树骨架（优先复用既有 L1/L2，避免顶层碎片化）】\n{tree_json}\n"
                f"【题干 Markdown】\n{stem_md}"
            ),
        }
    ]
    if stem_images:
        user_parts.append(
            {"type": "text", "text": "【题干配图槽（与手写草稿分离，勿将涂改线误认为配图）】"}
        )
        user_parts.extend(
            {"type": "image_url", "image_url": {"url": image_path_to_data_url(p)}}
            for p in stem_images
        )
    if draft_images:
        user_parts.append(
            {
                "type": "text",
                "text": (
                    "【手写草稿截图槽（仅本轮一次性转录为 draft_steps_latex，"
                    "后续轮次不再携带）】"
                ),
            }
        )
        user_parts.extend(
            {"type": "image_url", "image_url": {"url": image_path_to_data_url(p)}}
            for p in draft_images
        )
    else:
        user_parts.append(
            {
                "type": "text",
                "text": (
                    "【手写草稿】空白：用户未上传草稿。alignment 请输出空白短路默认值："
                    "draft_steps_latex=[], matched_branch_id='canonical', alt_branch_steps=null, "
                    "last_valid_step_index=0, stalled_step_index=1, "
                    "error_category='CONCEPT_BOTTLENECK', trap_taxonomy_tag=null。"
                ),
            }
        )
    return [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": user_parts},
    ]


def _build_system_prompt() -> str:
    """注入六大 TRAP 算子、四维锚点与输出 JSON Schema（设计 §3.1）。"""
    trap_lines = "\n".join(f"- {tag}: {desc}" for tag, desc in TRAP_TAXONOMY.items())
    schema_json = json.dumps(DiagnosisPayload.model_json_schema(), ensure_ascii=False)
    return (
        "你是数理自适应刷题系统的多模态错题诊断专家。请一次性完成：\n"
        "1. 输出最优解法步骤骨架 trace（step_index 从 1 开始；每步含子目标描述、核心公式 "
        "LaTeX、L1/L2/L3 知识标签、四维子分 c_sub/s_sub/t_sub/l_sub、一阶盲探遮罩条件 "
        "masked_conditions 与隐去最终问题的反问句 probe_question_md）；\n"
        "2. 将手写草稿图片一次性转录为 draft_steps_latex 有序数组，并与 trace 逐步对齐定位卡点；"
        "若草稿前置推导在数理上成立但不属于默认解法，作为 user_alt_1 分支补全 alt_branch_steps；\n"
        "3. 判定 error_category ∈ {ALREADY_MASTERED, TRAP_OR_CALC_ERROR, CONCEPT_BOTTLENECK}；"
        "TRAP_OR_CALC_ERROR 时必须给出六大算子之一 trap_taxonomy_tag，其余类别置 null。\n\n"
        f"【六大设错/易错陷阱算子】\n{trap_lines}\n\n"
        f"【四维行为级锚点】\n{_FOUR_DIM_ANCHORS}\n\n"
        "【输出格式】仅输出一个 JSON 对象（无 Markdown 代码块、无解释），"
        f"严格符合以下 JSON Schema：\n{schema_json}"
    )


def _postprocess(payload: DiagnosisPayload) -> None:
    """后处理校验与修正（契约实现规格 3/4；原地修改 payload）。"""
    trace = payload.trace
    alignment = payload.alignment
    if not trace.steps:
        raise ValueError("诊断 trace.steps 不能为空")
    step_count = len(trace.steps)

    # 1) stalled_step_index ∈ [1, len(steps)]，越界修正
    alignment.stalled_step_index = min(max(alignment.stalled_step_index, 1), step_count)
    # 2) 0 ≤ last_valid_step_index < stalled_step_index
    alignment.last_valid_step_index = max(
        0, min(alignment.last_valid_step_index, alignment.stalled_step_index - 1)
    )
    # 3) error_category 与 trap_taxonomy_tag 一致性
    if alignment.error_category == "TRAP_OR_CALC_ERROR":
        if alignment.trap_taxonomy_tag is None:
            raise ValueError("error_category=TRAP_OR_CALC_ERROR 时 trap_taxonomy_tag 必填")
    else:
        alignment.trap_taxonomy_tag = None
    # 4) 一题多解：alt 分支缺失/为空 → 降级 canonical 并修正 payload
    if alignment.matched_branch_id != "canonical":
        if not alignment.alt_branch_steps:
            alignment.matched_branch_id = "canonical"
            alignment.alt_branch_steps = None
    else:
        alignment.alt_branch_steps = None

    # 5) 四维范围断言（深度 rubric/单调性校验交给下游 difficulty_rubric）
    _assert_dim_range("c_score", trace.c_score, 4)
    _assert_dim_range("s_score", trace.s_score, 3)
    _assert_dim_range("t_score", trace.t_score, 3)
    _assert_dim_range("l_score", trace.l_score, 3)
    for step in trace.steps:
        _assert_dim_range(f"step{step.step_index}.c_sub", step.c_sub, 4)
        _assert_dim_range(f"step{step.step_index}.s_sub", step.s_sub, 3)
        _assert_dim_range(f"step{step.step_index}.t_sub", step.t_sub, 3)
        _assert_dim_range(f"step{step.step_index}.l_sub", step.l_sub, 3)


def _assert_dim_range(name: str, value: int, upper: int) -> None:
    """维度分必须为 [1, upper] 内整数（拒绝 bool），越界抛 ValueError。"""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
        raise ValueError(f"维度分 {name}={value!r} 越界（合法范围 [1, {upper}]）")
