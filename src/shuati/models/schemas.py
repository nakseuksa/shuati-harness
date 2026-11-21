"""Stage 1 与裁判模型的严格结构化输出 Schema（M1 锁定契约）。"""

from __future__ import annotations

from typing import Literal

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
