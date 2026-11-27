"""Stage 1 与裁判模型的严格结构化输出 Schema（M1 锁定契约）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from shuati.models.domain import AnswerPart


class Stage1Input(BaseModel):
    """Stage 1 硬隔离输入 DTO：类型签名严禁包含 raw_chapter / legacy_tags / source_collection / raw_images。"""

    question_uid: str
    grade_subject: str
    question_text: str
    cas_images: list[str] = Field(default_factory=list)
    image_captions: list[str] = Field(default_factory=list)
    options: dict[str, str] = Field(default_factory=dict)
    has_images: bool = False


class Stage1PrimaryOutput(BaseModel):
    """主路 VLM 结构化输出。visual_facts 必须排在字段首位。"""

    visual_facts: str = ""
    visual_ambiguity_flag: bool = False
    visual_ambiguity_note: str = ""
    method_summary: str = ""
    solution_markdown: str = ""
    final_answers: list[AnswerPart] = Field(min_length=1)
    sympy_code: str | None = None

    @model_validator(mode="after")
    def validate_sympy_code_for_symbolic_parts(self) -> Stage1PrimaryOutput:
        has_symbolic = any(part.verify_mode == "SYMBOLIC" for part in self.final_answers)
        if has_symbolic and (not isinstance(self.sympy_code, str) or not self.sympy_code.strip()):
            raise ValueError("final_answers 中存在 verify_mode == 'SYMBOLIC' 的小问时，sympy_code 必须为非空字符串")
        return self


class Stage1SecondaryOutput(BaseModel):
    """辅路 VLM 结构化输出（跨家族独立双盲）。"""

    visual_facts: str = ""
    visual_ambiguity_flag: bool = False
    visual_ambiguity_note: str = ""
    method_summary: str = ""
    solution_markdown: str = ""
    final_answers: list[AnswerPart] = Field(min_length=1)


class Stage1SubagentItem(Stage1PrimaryOutput):
    """Subagent 脱敏批处理 Stage 1 单题输出（继承 Stage1PrimaryOutput，保持 visual_facts 首位）。"""

    task_item_id: str = Field(min_length=1)


class Stage1SubagentBatchOutput(BaseModel):
    """Subagent 脱敏批处理 Stage 1 批输出容器（用于 export/import 破除锚点冷启动死锁）。"""

    items: list[Stage1SubagentItem] = Field(default_factory=list)


class QualitativeJudgeOutput(BaseModel):
    """定性/作图小问等价裁判输出。"""

    equivalent: bool
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "HIGH"
    reason: str = ""


class MultiSolutionJudgeOutput(BaseModel):
    """一题多解本质路线差异裁判输出。"""

    is_distinct_method: bool
    reason: str = ""


class ReviewResolveOutput(BaseModel):
    """复核页 [R] 辅助重算参考解答草稿输出（纯文本 text_llm，零视觉 Token）。"""

    method_summary: str = ""
    solution_markdown: str = ""
    final_answers: list[AnswerPart] = Field(min_length=1)
    sympy_code: str | None = None


SeedTrapCode = Literal[
    "TRAP_DOMAIN_BOUNDARY",
    "TRAP_SIGN_DIRECTION",
    "TRAP_EXTRANEOUS_ROOT",
    "TRAP_FRAME_SHIFT",
    "TRAP_APPROX_CONDITION",
    "TRAP_ARITHMETIC_SLIP",
    "TRAP_AVERAGE_HARMONIC",
    "TRAP_EXTENDED_BODY_LENGTH",
    "TRAP_BRIDGE_ON_VS_PASS",
    "TRAP_ASYNC_SYNC_CONFUSION",
    "TRAP_DIRECTION_REF_POINT",
    "TRAP_MIDPOINT_OFFSET_DOUBLE",
]


class DifficultyDimension(BaseModel):
    """四维难度单项打分（evidence 必须置于 score 之前，CoT 优先；C 维范围 [1, 4]）。"""

    evidence: str = Field(min_length=2)
    score: int = Field(ge=1, le=4)


class DifficultyDimensionBounded3(BaseModel):
    """四维难度 S/T/L 单项打分（evidence 必须置于 score 之前，CoT 优先；范围严格限定 [1, 3]）。"""

    evidence: str = Field(min_length=2)
    score: int = Field(ge=1, le=3)


class DifficultyScoresOutput(BaseModel):
    """Stage 2 四维难度打分结构化输出（D4 契约：C ∈ [1, 4]，S/T/L ∈ [1, 3]）。"""

    C_concept_count: DifficultyDimension
    S_chain_length: DifficultyDimensionBounded3
    T_trickiness: DifficultyDimensionBounded3
    L_computation_load: DifficultyDimensionBounded3


class Stage2StepOutput(BaseModel):
    """Stage 2 单步逻辑拆解输出。"""

    step_index: int = Field(ge=1)
    sub_goal: str = Field(min_length=2)
    core_formula: str = ""
    hint: str = Field(min_length=2)
    raw_l1: str = Field(min_length=1)
    raw_l2: str = Field(min_length=1)
    raw_l3: str = Field(min_length=1)


class Stage2OnlyOutput(BaseModel):
    """已过 Stage 1 验真题目的 Stage 2 增量输出（STAGE2_ONLY）。"""

    task_id: str = Field(min_length=1)
    steps: list[Stage2StepOutput] = Field(min_length=1)
    abstract_skeleton: str = Field(min_length=5)
    primary_l1_module: str = Field(min_length=1)
    secondary_l1_modules: list[str] = Field(default_factory=list)
    thinking_methods: list[str] = Field(default_factory=list)
    classic_motifs: list[str] = Field(default_factory=list)
    trap_tags: list[str] = Field(default_factory=list)
    difficulty: DifficultyScoresOutput


class UnifiedS1S2Output(BaseModel):
    """合一初始化编译器输出（UNIFIED_S1_S2，visual_facts 必须排在首位）。"""

    task_id: str = Field(min_length=1)
    visual_facts: str = ""
    visual_ambiguity_flag: bool = False
    visual_ambiguity_note: str = ""
    method_summary: str = Field(min_length=2)
    solution_markdown: str = Field(min_length=5)
    final_answers: list[AnswerPart] = Field(min_length=1)
    sympy_code: str | None = None
    steps: list[Stage2StepOutput] = Field(min_length=1)
    abstract_skeleton: str = Field(min_length=5)
    primary_l1_module: str = Field(min_length=1)
    secondary_l1_modules: list[str] = Field(default_factory=list)
    thinking_methods: list[str] = Field(default_factory=list)
    classic_motifs: list[str] = Field(default_factory=list)
    trap_tags: list[str] = Field(default_factory=list)
    difficulty: DifficultyScoresOutput


class TaxonomyL2Proposal(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class TaxonomyL1Proposal(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    l2_entities: list[TaxonomyL2Proposal] = Field(default_factory=list)


class TaxonomyProposeOutput(BaseModel):
    grade_subject: str = ""
    modules: list[TaxonomyL1Proposal]
    rationale: str = ""


class Stage3cStepMapping(BaseModel):
    step_index: int
    mapped_l1: str
    mapped_l2: str
    is_novel_l2: bool = False


class Stage3cQuestionMapping(BaseModel):
    task_item_id: str
    primary_l1_module: str
    secondary_l1_modules: list[str] = Field(default_factory=list)
    steps: list[Stage3cStepMapping] = Field(default_factory=list)


class Stage3cBatchMappingOutput(BaseModel):
    items: list[Stage3cQuestionMapping]


class MergeJudgeItem(BaseModel):
    pair_id: str
    should_merge: bool
    canonical_name: str = ""
    reason: str = ""


class MergeJudgeBatchOutput(BaseModel):
    decisions: list[MergeJudgeItem]


class Stage3dCanonicalOperator(BaseModel):
    """Stage 3d 同 L2 域归纳出的标准原子转化算子。"""

    canonical_name: str = Field(min_length=2)
    description: str = ""


class Stage3dClusterAssignment(BaseModel):
    """Stage 3d 单条 raw_l3 / 概念到标准算子的归并映射。"""

    item_id: str = ""
    raw_l3: str = Field(min_length=1)
    canonical_name: str = Field(min_length=2)
    reason: str = ""


class Stage3dClusterAssignOutput(BaseModel):
    """Stage 3d 同 L2 域 Cluster-then-Assign 归纳输出（D-Audit-2）。"""

    canonical_operators: list[Stage3dCanonicalOperator] = Field(default_factory=list)
    assignments: list[Stage3dClusterAssignment] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_dict_assignments(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("assignments"), dict):
            coerced = [
                {"raw_l3": str(k), "canonical_name": str(v)}
                for k, v in data["assignments"].items()
                if str(k).strip() and str(v).strip()
            ]
            return {**data, "assignments": coerced}
        return data


class PrerequisiteEdgeCandidate(BaseModel):
    source_l1: str
    source_l2: str
    target_l1: str
    target_l2: str
    confidence: float = 0.8
    reason: str = ""


class PrerequisiteDagOutput(BaseModel):
    edges: list[PrerequisiteEdgeCandidate]


class Stage4RefineItem(BaseModel):
    pair_id: str
    is_valid: bool
    pedagogical_note: str = ""
    reason: str = ""


class Stage4RefineBatchOutput(BaseModel):
    decisions: list[Stage4RefineItem]


DiagnoseRecSlot = Literal[
    "SCAFFOLD",
    "ISOMORPHIC",
    "CONTRASTIVE_TRAP",
    "REJECT",
]


class DiagnoseRerankItem(BaseModel):
    """外部错题推荐候选二级轻量精裁单项裁决。"""

    candidate_id: str = Field(min_length=1)
    verdict: DiagnoseRecSlot
    pedagogical_reason: str = ""


class DiagnoseRerankBatchOutput(BaseModel):
    """外部错题推荐候选二级轻量精裁批量输出。"""

    decisions: list[DiagnoseRerankItem] = Field(default_factory=list)


class ExternalDiagnosisOutput(BaseModel):
    """外部新错题实时单路诊断输出（visual_facts 必须排首位）。"""

    visual_facts: str = ""
    visual_ambiguity_flag: bool = False
    visual_ambiguity_note: str = ""
    method_summary: str = Field(min_length=2)
    solution_markdown: str = Field(min_length=5)
    final_answers: list[AnswerPart] = Field(min_length=1)
    sympy_code: str | None = None
    steps: list[Stage2StepOutput] = Field(min_length=1)
    abstract_skeleton: str = Field(min_length=5)
    primary_l1_module: str = Field(min_length=1)
    secondary_l1_modules: list[str] = Field(default_factory=list)
    matched_l2: list[str] = Field(default_factory=list)
    thinking_methods: list[str] = Field(default_factory=list)
    classic_motifs: list[str] = Field(default_factory=list)
    trap_tags: list[SeedTrapCode] = Field(default_factory=list)
    difficulty: DifficultyScoresOutput

    @model_validator(mode="after")
    def validate_sympy_code_for_symbolic_parts(self) -> ExternalDiagnosisOutput:
        has_symbolic = any(part.verify_mode == "SYMBOLIC" for part in self.final_answers)
        if has_symbolic:
            if not isinstance(self.sympy_code, str) or not self.sympy_code.strip():
                raise ValueError("final_answers 中存在 verify_mode == 'SYMBOLIC' 的小问时，sympy_code 必须为非空字符串")
            if "def verify(" not in self.sympy_code:
                raise ValueError("sympy_code 必须定义 def verify(): 函数并返回各小问答案字典")
        return self
