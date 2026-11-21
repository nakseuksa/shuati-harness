"""LLM/VLM 多槽位异步网关、字段白名单 Prompt 构造器与离线 Mock 路由。"""

from shuati.llm.gateway import LLMCallResult, LLMGateway, LLMGatewayError
from shuati.llm.mocks import MOCK_RESPONSES, resolve_mock
from shuati.llm.prompt_builder import (
    STAGE1_PROMPT_VERSION,
    build_multi_solution_judge_messages,
    build_qualitative_judge_messages,
    build_review_resolve_messages,
    build_stage1_messages,
)

__all__ = [
    "LLMCallResult",
    "LLMGateway",
    "LLMGatewayError",
    "MOCK_RESPONSES",
    "STAGE1_PROMPT_VERSION",
    "build_multi_solution_judge_messages",
    "build_qualitative_judge_messages",
    "build_review_resolve_messages",
    "build_stage1_messages",
    "resolve_mock",
]
