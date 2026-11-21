"""离线测试确定性 Mock 路由（支持 monkeypatch MOCK_RESPONSES 与 Stage 1/裁判默认响应）。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from shuati.models.schemas import (
    MultiSolutionJudgeOutput,
    QualitativeJudgeOutput,
    ReviewResolveOutput,
    Stage1PrimaryOutput,
    Stage1SecondaryOutput,
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
