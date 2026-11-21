"""核心领域模型、答案规范化契约与 CLI JSON 信封。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# 规范退出码常量（DESIGN.md §5.4-3）
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_PRECONDITION = 2  # 参数校验失败 / AMBIGUOUS_UID / PRECONDITION_FAILED
EXIT_NOT_FOUND = 3
EXIT_LLM_ERROR = 4


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class Envelope(BaseModel):
    ok: bool
    command: str
    data: Any = None
    warnings: list[str] = Field(default_factory=list)
    error: ErrorDetail | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


AnswerType = Literal[
    "NUMERIC",
    "EXPRESSION",
    "RATIO",
    "SET_OR_INTERVAL",
    "CHOICE",
    "SYMBOL_FILL",
    "TEXT",
    "PROOF",
]

VerifyMode = Literal["SYMBOLIC", "EXACT", "QUALITATIVE"]

CollectionMode = Literal[
    "SINGLE",
    "ORDERED_TUPLE",
    "UNORDERED_MULTISET",
    "LABELED_MAP",
]

ALLOWED_VERIFY_MODES: dict[str, set[str]] = {
    "NUMERIC": {"SYMBOLIC"},
    "EXPRESSION": {"SYMBOLIC"},
    "RATIO": {"SYMBOLIC"},
    "SET_OR_INTERVAL": {"SYMBOLIC"},
    "CHOICE": {"EXACT"},
    "SYMBOL_FILL": {"EXACT"},
    "TEXT": {"QUALITATIVE"},
    "PROOF": {"QUALITATIVE"},
}


class AnswerPart(BaseModel):
    """小问答案原子契约（决策 D3，DESIGN.md §3.2-3）。"""

    label: str = "主问"  # "主问" | "(1)" | "(2)" ...
    answer_type: AnswerType = "NUMERIC"
    verify_mode: VerifyMode = "SYMBOLIC"
    collection_mode: CollectionMode = "SINGLE"
    answer: str = ""
    unit: str | None = None
    sympy_expr: str | None = None
    items: list[Any] | None = None
    choice_label: str | None = None
    canonical: str | None = None

    @model_validator(mode="after")
    def validate_type_and_verify_mode(self) -> AnswerPart:
        allowed = ALLOWED_VERIFY_MODES.get(self.answer_type)
        if allowed is None or self.verify_mode not in allowed:
            raise ValueError(
                f"answer_type '{self.answer_type}' 与 verify_mode '{self.verify_mode}' 不匹配，"
                f"允许的 verify_mode 为: {allowed}"
            )
        if self.answer_type == "CHOICE" and not self.choice_label and not self.answer:
            raise ValueError("CHOICE 类型的 AnswerPart 必须提供 choice_label 或 answer")
        return self


class GradeSubject(BaseModel):
    grade_subject: str
    slug: str
    display_name: str
    subject_family: Literal["MATH", "PHYSICS", "CHEMISTRY", "STEM_OTHER"]
    taxonomy_status: Literal["NONE", "DRAFT", "LOCKED"] = "NONE"
    taxonomy_version: str = ""
    taxonomy_file: str | None = None
    anchors_file: str | None = None
    anchors_version: str = ""
    anchors_ready: int = 0
    created_at: str = ""


class HumanOverrideRecord(BaseModel):
    override_id: int | None = None
    question_uid: str
    field_name: str
    override_value: str
    reason: str = ""
    source: Literal["REVIEW_UI", "PI_CLI", "PI_MCP", "MANUAL_YAML", "ANCHOR_CALIBRATION"]
    bundle_id: str | None = None
    base_fingerprint: str
    taxonomy_version: str = ""
    is_active: int = 1
    is_suspended: int = 0
    suspended_reason: str = ""
    revoked_at: str | None = None
    created_at: str = ""


class RelationOverrideRecord(BaseModel):
    override_id: int | None = None
    source_question_uid: str
    target_question_uid: str
    relation_type: Literal["CLONE", "ISOMORPHIC", "SCAFFOLD", "CONTRASTIVE", "SHARED_TRAP"]
    action: Literal["ASSERT", "REJECT"]
    pedagogical_note: str = ""
    reason: str = ""
    source: Literal["REVIEW_UI", "PI_CLI", "PI_MCP", "MANUAL_YAML"]
    is_active: int = 1
    revoked_at: str | None = None
    created_at: str = ""


class LearnerRecord(BaseModel):
    learner_id: str
    display_name: str
    default_grade_subject: str = "小学奥数"
    is_current: int = 0
    notes: str = ""
    created_at: str = ""


class PracticeLogRecord(BaseModel):
    log_id: int | None = None
    learner_id: str = "default"
    question_uid: str
    practiced_at: str = ""
    outcome: Literal["SOLVED_SOLO", "SOLVED_WITH_HINT", "WRONG", "SKIPPED", "VOIDED"]
    score_ratio: float | None = None
    voids_log_id: int | None = None
    stuck_step_index: int | None = None
    stuck_node_id: str | None = None
    stuck_sub_goal: str | None = None
    hint_level_used: int | None = None
    user_note: str = ""
    submitted_answer: str | None = None
    quiz_batch_id: str | None = None
    client_request_id: str | None = None


class EffectiveQuestion(BaseModel):
    """题目主表物化视图（仅装载 eff_* 生效列，DESIGN.md §3.2-4）。"""

    question_uid: str
    grade_subject: str
    cas_images: list[str] = Field(default_factory=list)
    image_captions: list[str] = Field(default_factory=list)
    has_images: bool = False
    is_retired: bool = False
    retired_reason: str = ""

    question_text: str = ""
    options: dict[str, str] = Field(default_factory=dict)
    format: str = "解答题"
    visual_facts: str = ""
    verification_status: str = "PENDING"
    review_reason: str = ""
    verification_mode: str = "DETERMINISTIC"
    final_answers: list[AnswerPart] = Field(default_factory=list)
    abstract_skeleton: str = ""
    primary_l1_module: str = ""
    secondary_l1_modules: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)

    c_score: int | None = None
    s_score: int | None = None
    t_score: int | None = None
    l_score: int | None = None
    base_elo: int | None = None
    tier: str | None = None
    tier_rank: int | None = None

    is_excluded: bool = False
    is_servable: bool = False
    overridden_fields: list[str] = Field(default_factory=list)
    refreshed_at: str = ""
    updated_at: str = ""
