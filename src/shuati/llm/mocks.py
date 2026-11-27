"""离线测试确定性 Mock 路由（支持 monkeypatch MOCK_RESPONSES 与 Stage 1/裁判默认响应）。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from shuati.models.schemas import (
    DiagnoseRerankBatchOutput,
    ExternalDiagnosisOutput,
    MergeJudgeBatchOutput,
    MultiSolutionJudgeOutput,
    PrerequisiteDagOutput,
    QualitativeJudgeOutput,
    ReviewResolveOutput,
    Stage1PrimaryOutput,
    Stage1SecondaryOutput,
    Stage2OnlyOutput,
    Stage3cBatchMappingOutput,
    Stage3dClusterAssignOutput,
    Stage4RefineBatchOutput,
    TaxonomyProposeOutput,
)

# 可由测试按需 monkeypatch 覆盖或追加；键可以是 mock_key、"{question_uid}:{slot}" 或 "{stage}:{slot}"
MOCK_RESPONSES: dict[str, Any] = {}


def _default_stage1_payload(primary: bool) -> dict[str, Any]:
    base: dict[str, Any] = {
        "visual_facts": "Mock 视觉事实：图中为标准平面几何图形，边长与顶点标注清晰。",
        "visual_ambiguity_flag": False,
        "visual_ambiguity_note": "",
        "method_summary": "利用面积与比例关系列式求解",
        "solution_markdown": "由题意可得：$6 \\times 8 \\div 2 = 24$。",
        "final_answers": [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "24",
                "sympy_expr": "24",
            }
        ],
    }
    if primary:
        base["sympy_code"] = (
            "def verify():\n"
            "    ans = 6 * 8 // 2\n"
            "    return {'主问': str(ans)}\n"
        )
    return base


def _default_payload_for_schema(schema: type[BaseModel], slot: str) -> dict[str, Any] | None:
    if schema is Stage1PrimaryOutput:
        return _default_stage1_payload(primary=True)
    if schema is Stage1SecondaryOutput:
        return _default_stage1_payload(primary=False)
    if schema is QualitativeJudgeOutput:
        return {
            "equivalent": True,
            "confidence": "HIGH",
            "reason": "Mock 语义裁判：核心结论一致",
        }
    if schema is MultiSolutionJudgeOutput:
        return {
            "is_distinct_method": False,
            "reason": "Mock 一题多解裁判：本质思路一致",
        }
    if schema is ReviewResolveOutput:
        return {
            "method_summary": "依据已核定视觉事实重算求解",
            "solution_markdown": "由核定视觉事实得：$6 \\times 8 \\div 2 = 24$。",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "24",
                    "sympy_expr": "24",
                }
            ],
            "sympy_code": "def verify():\n    ans = 6 * 8 // 2\n    return {'主问': str(ans)}\n",
        }
    if schema is TaxonomyProposeOutput:
        return {
            "grade_subject": "小学奥数",
            "modules": [
                {
                    "name": "平面几何",
                    "aliases": ["几何图形", "直线型几何"],
                    "l2_entities": [
                        {"name": "三角形面积与等高模型", "aliases": ["等高模型", "面积比例"]},
                        {"name": "圆与扇形周长面积", "aliases": ["圆与扇形"]},
                    ],
                },
                {
                    "name": "数论基础",
                    "aliases": ["整数问题", "数论"],
                    "l2_entities": [
                        {"name": "整除与同余", "aliases": ["整除问题", "余数问题"]},
                        {"name": "质因数分解", "aliases": ["分解质因数"]},
                    ],
                },
                {
                    "name": "行程问题",
                    "aliases": ["行程"],
                    "l2_entities": [
                        {"name": "相遇与追及", "aliases": ["相遇问题", "追及问题"]},
                    ],
                },
                {
                    "name": "组合与计数",
                    "aliases": ["计数问题", "组合"],
                    "l2_entities": [
                        {"name": "排列组合基础", "aliases": ["乘法原理", "加法原理"]},
                        {"name": "抽屉原理与容斥", "aliases": ["抽屉原理", "容斥原理"]},
                    ],
                },
            ],
            "rationale": "Mock Stage 3a 标准词表提案",
        }
    if schema is Stage3cBatchMappingOutput:
        return {"items": []}
    if schema is MergeJudgeBatchOutput:
        return {"decisions": []}
    if schema is Stage3dClusterAssignOutput:
        return {"canonical_operators": [], "assignments": []}
    if schema is Stage2OnlyOutput:
        return {
            "task_id": "MOCK_S2_TASK",
            "steps": [
                {
                    "step_index": 1,
                    "sub_goal": "建立几何量比例转化关系",
                    "core_formula": "S = a * h / 2",
                    "hint": "先观察已知底边与高的对应关系",
                    "raw_l1": "平面几何",
                    "raw_l2": "三角形面积与等高模型",
                    "raw_l3": "底乘高除二模型",
                }
            ],
            "abstract_skeleton": "利用平面图形面积比例关系建立方程求解未知量",
            "primary_l1_module": "平面几何",
            "secondary_l1_modules": [],
            "thinking_methods": ["转化思想"],
            "classic_motifs": ["等高模型"],
            "trap_tags": [],
            "difficulty": {
                "C_concept_count": {"evidence": "单一等高模型直接求解", "score": 2},
                "S_chain_length": {"evidence": "两步比例换算", "score": 1},
                "T_trickiness": {"evidence": "条件显式无隐蔽陷阱", "score": 1},
                "L_computation_load": {"evidence": "常规整数乘法运算", "score": 1},
            },
        }
    if schema is PrerequisiteDagOutput:
        return {"edges": []}
    if schema is Stage4RefineBatchOutput:
        return {"decisions": []}
    if schema is DiagnoseRerankBatchOutput:
        return {"decisions": []}
    if schema is ExternalDiagnosisOutput:
        return {
            "visual_facts": "",
            "visual_ambiguity_flag": False,
            "visual_ambiguity_note": "",
            "method_summary": "依据面积转化与设元方程求解",
            "solution_markdown": "由题意可得答案：$24$。",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "24",
                    "sympy_expr": "24",
                }
            ],
            "sympy_code": "def verify():\n    ans = 6 * 8 // 2\n    return {'主问': str(ans)}\n",
            "steps": [
                {
                    "step_index": 1,
                    "sub_goal": "建立几何量比例转化关系",
                    "core_formula": "S = a * h / 2",
                    "hint": "先观察已知底边与高的对应关系",
                    "raw_l1": "平面几何",
                    "raw_l2": "三角形面积与等高模型",
                    "raw_l3": "底乘高除二模型",
                }
            ],
            "abstract_skeleton": "利用平面图形面积比例关系建立方程求解未知量",
            "primary_l1_module": "平面几何",
            "secondary_l1_modules": [],
            "matched_l2": ["三角形面积与等高模型"],
            "thinking_methods": ["转化思想"],
            "classic_motifs": ["等高模型"],
            "trap_tags": [],
            "difficulty": {
                "C_concept_count": {"evidence": "单一等高模型直接求解", "score": 2},
                "S_chain_length": {"evidence": "两步比例换算", "score": 1},
                "T_trickiness": {"evidence": "条件显式无隐蔽陷阱", "score": 1},
                "L_computation_load": {"evidence": "常规整数乘法运算", "score": 1},
            },
        }
    return None


def resolve_mock(
    schema: type[BaseModel],
    *,
    slot: str,
    stage: str,
    question_uid: str | None = None,
    mock_key: str | None = None,
) -> tuple[BaseModel, str]:
    """根据 mock_key / question_uid / stage / schema 解析确定性 Mock 响应并校验。

    返回 (validated_model, raw_json_str)。
    """
    from shuati.llm.gateway import LLMGatewayError

    payload: Any = None
    if mock_key is not None:
        if mock_key not in MOCK_RESPONSES:
            raise LLMGatewayError(
                f"Mock key 未注册: {mock_key!r}（已注册: {sorted(MOCK_RESPONSES)}）",
                error_kind="MOCK_KEY_UNREGISTERED",
            )
        entry = MOCK_RESPONSES[mock_key]
        payload = entry() if callable(entry) else entry
    else:
        candidates = [
            f"{question_uid}:{slot}" if question_uid else None,
            f"{stage}:{slot}",
            stage,
            schema.__name__,
        ]
        for key in candidates:
            if key and key in MOCK_RESPONSES:
                entry = MOCK_RESPONSES[key]
                payload = entry() if callable(entry) else entry
                break
        if payload is None:
            payload = _default_payload_for_schema(schema, slot)

    if payload is None:
        raise LLMGatewayError(
            f"未找到适配 {schema.__name__} 的 Mock 响应 (stage={stage!r}, slot={slot!r})",
            error_kind="MOCK_KEY_UNREGISTERED",
        )

    if isinstance(payload, BaseException):
        raise payload

    if isinstance(payload, str):
        raw_str = payload
        try:
            parsed_obj = json.loads(raw_str)
            model = schema.model_validate(parsed_obj)
            return model, raw_str
        except (ValueError, ValidationError) as exc:
            raise LLMGatewayError(
                f"Mock 字符串未通过 {schema.__name__} 校验: {exc}",
                error_kind="MOCK_VALIDATION_FAILED",
            ) from exc

    raw_str = json.dumps(payload, ensure_ascii=False)
    try:
        model = schema.model_validate(payload)
        return model, raw_str
    except ValidationError as exc:
        raise LLMGatewayError(
            f"Mock fixture 未通过 {schema.__name__} 校验: {exc}",
            error_kind="MOCK_VALIDATION_FAILED",
        ) from exc
