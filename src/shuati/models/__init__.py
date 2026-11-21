"""领域数据模型与统一信封。"""

from .domain import (
    EXIT_INTERNAL,
    EXIT_LLM_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PRECONDITION,
    AnswerPart,
    EffectiveQuestion,
    Envelope,
    ErrorDetail,
    GradeSubject,
    HumanOverrideRecord,
    LearnerRecord,
    PracticeLogRecord,
    RelationOverrideRecord,
)

__all__ = [
    "EXIT_OK",
    "EXIT_INTERNAL",
    "EXIT_PRECONDITION",
    "EXIT_NOT_FOUND",
    "EXIT_LLM_ERROR",
    "ErrorDetail",
    "Envelope",
    "AnswerPart",
    "GradeSubject",
    "HumanOverrideRecord",
    "RelationOverrideRecord",
    "LearnerRecord",
    "PracticeLogRecord",
    "EffectiveQuestion",
]
