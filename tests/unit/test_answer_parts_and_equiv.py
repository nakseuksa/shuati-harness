"""小问标签归一、白名单校验与逐小问双盲等价判定单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shuati.models.domain import AnswerPart
from shuati.models.schemas import QualitativeJudgeOutput, Stage1PrimaryOutput
from shuati.verify.answer_parts import (
    derive_verification_mode,
    normalize_answer_parts,
    normalize_choice_label,
    normalize_part_label,
    normalize_symbol_fill,
)
from shuati.verify.equivalence import compare_answer_parts, latex_to_plain, strip_units
from shuati.verify.sympy_sandbox import SympySandboxResult


def test_normalize_part_label_and_reject_sublabels() -> None:
    assert normalize_part_label("（1）") == "(1)"
    assert normalize_part_label("①") == "(1)"
    assert normalize_part_label("1)") == "(1)"
    assert normalize_part_label("1.") == "(1)"
    assert normalize_part_label("2") == "(2)"
    assert normalize_part_label("主问") == "主问"
    assert normalize_part_label("") == "主问"

    for bad in ("(1)-1", "（1）-2", "(1)_a", "1-1", "①-1"):
        with pytest.raises(ValueError):
            normalize_part_label(bad)


def test_normalize_choice_and_symbol_fill() -> None:
    assert normalize_choice_label(" c, a ") == "AC"
    assert normalize_choice_label("ｂ、ａ") == "AB"

    assert normalize_symbol_fill("＞") == ">"
    assert normalize_symbol_fill("大于等于") == ">="
    assert normalize_symbol_fill("≤") == "<="
    assert normalize_symbol_fill("不等于") == "!="
    assert normalize_symbol_fill("×") == "*"
    assert normalize_symbol_fill("除以") == "/"


def test_whitelist_validation_and_derive_mode() -> None:
    with pytest.raises(ValidationError):
        AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="QUALITATIVE", answer="5")

    with pytest.raises(ValidationError):
        AnswerPart(label="(1)-1", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="5", sympy_expr="5")

    with pytest.raises(ValidationError):
        Stage1PrimaryOutput(
            visual_facts="",
            final_answers=[AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="5", sympy_expr="5")],
            sympy_code=None,
        )

    parts = [
        AnswerPart(label="（1）", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="15", sympy_expr="15"),
        AnswerPart(label="②", answer_type="TEXT", verify_mode="QUALITATIVE", answer="因为两三角形等底等高"),
    ]
    normed = normalize_answer_parts(parts)
    assert [p.label for p in normed] == ["(1)", "(2)"]
    assert derive_verification_mode(normed) == "MIXED"
    assert derive_verification_mode([normed[0]]) == "DETERMINISTIC"
    assert derive_verification_mode([normed[1]]) == "QUALITATIVE"

    with pytest.raises(ValueError):
        normalize_answer_parts([
            AnswerPart(label="(1)", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="1", sympy_expr="1"),
            AnswerPart(label="①", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="2", sympy_expr="2"),
        ])


def test_latex_to_plain_and_strip_units() -> None:
    assert latex_to_plain(r"\frac{3}{4}\pi + \sqrt{2}") == "((3)/(4)) pi + sqrt(2)"
    assert latex_to_plain(r"3\times 4 \div 2 \text{平方厘米}") == "3 * 4 / 2"
    assert latex_to_plain("１２＋３") == "12+3"

    assert strip_units("48平方厘米") == "48"
    assert strip_units("15 cm", allow_latin_unit=True) == "15"
    assert strip_units("15 m", allow_latin_unit=True) == "15"
    assert strip_units("15*m", allow_latin_unit=True) == "15*m"


@pytest.mark.asyncio
async def test_compare_ratio_and_pi_314() -> None:
    # ch28_020 RATIO 判等（比值与分数互认 + 多项连比）
    p_ratio = [
        AnswerPart(label="(1)", answer_type="RATIO", verify_mode="SYMBOLIC", answer="9:4", sympy_expr="9:4"),
        AnswerPart(label="(2)", answer_type="RATIO", verify_mode="SYMBOLIC", answer="1:2:3", sympy_expr="1:2:3"),
    ]
    s_ratio = [
        AnswerPart(label="(1)", answer_type="RATIO", verify_mode="SYMBOLIC", answer="9/4", sympy_expr="9/4"),
        AnswerPart(label="(2)", answer_type="RATIO", verify_mode="SYMBOLIC", answer="2:4:6", sympy_expr="2:4:6"),
    ]
    sbx = SympySandboxResult(ok=True, status="PASSED", values={"(1)": "9/4", "(2)": "1:2:3"})
    res = await compare_answer_parts(
        p_ratio,
        s_ratio,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=sbx,
        timeout_seconds=10.0,
    )
    assert res.all_dual_equal
    assert res.all_sympy_ok
    assert res.sympy_status == "PASSED"

    # pi ≈ 3.14 在小学奥数通过，在其他未开启学段拒绝
    p_pi = [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="4π", sympy_expr="4*pi")]
    s_pi = [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="12.56", sympy_expr="12.56")]
    sbx_pi = SympySandboxResult(ok=True, status="PASSED", values={"主问": "4*pi"})

    res_ok = await compare_answer_parts(
        p_pi,
        s_pi,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=sbx_pi,
        timeout_seconds=10.0,
    )
    assert res_ok.all_dual_equal

    res_no = await compare_answer_parts(
        p_pi,
        s_pi,
        grade_subject="初中数学",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=sbx_pi,
        timeout_seconds=10.0,
    )
    assert not res_no.all_dual_equal
    assert "ANSWER_MISMATCH" in res_no.review_reasons


@pytest.mark.asyncio
async def test_compare_ordered_tuple_multiset_labeled_map() -> None:
    # ch01_010: ORDERED_TUPLE 混填 NUMERIC + SYMBOL_FILL
    p_tup = [
        AnswerPart(
            label="主问",
            answer_type="EXPRESSION",
            verify_mode="SYMBOLIC",
            collection_mode="ORDERED_TUPLE",
            answer="20, -, 5",
            items=[
                {"item_type": "NUMERIC", "expr": "20"},
                {"item_type": "SYMBOL_FILL", "canonical": "－"},
                {"item_type": "NUMERIC", "expr": "5"},
            ],
        )
    ]
    s_tup = [
        AnswerPart(
            label="主问",
            answer_type="EXPRESSION",
            verify_mode="SYMBOLIC",
            collection_mode="ORDERED_TUPLE",
            answer="20, -, 5",
            items=[
                {"item_type": "NUMERIC", "expr": "20.0"},
                {"item_type": "SYMBOL_FILL", "canonical": "减"},
                {"item_type": "NUMERIC", "expr": "10/2"},
            ],
        )
    ]
    sbx_tup = SympySandboxResult(ok=True, status="PASSED", values={"主问": ["20", "-", "5"]})
    res_tup = await compare_answer_parts(
        p_tup,
        s_tup,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=sbx_tup,
        timeout_seconds=10.0,
    )
    assert res_tup.all_dual_equal
    assert res_tup.all_sympy_ok

    # ch28_025: UNORDERED_MULTISET 无序多重集
    p_mset = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="UNORDERED_MULTISET",
            answer="3, 5, 5, 12",
            items=[{"expr": "3"}, {"expr": "5"}, {"expr": "5"}, {"expr": "12"}],
        )
    ]
    s_mset = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="UNORDERED_MULTISET",
            answer="12, 5, 3, 5",
            items=[{"expr": "12"}, {"expr": "5"}, {"expr": "3"}, {"expr": "5"}],
        )
    ]
    sbx_mset = SympySandboxResult(ok=True, status="PASSED", values={"主问": ["5", "12", "3", "5"]})
    res_mset = await compare_answer_parts(
        p_mset,
        s_mset,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=sbx_mset,
        timeout_seconds=10.0,
    )
    assert res_mset.all_dual_equal
    assert res_mset.all_sympy_ok

    # LABELED_MAP
    p_map = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="鸡:12, 兔:8",
            items=[{"label": "鸡", "expr": "12"}, {"label": "兔", "expr": "8"}],
        )
    ]
    s_map = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="兔:8, 鸡:12",
            items=[{"label": "兔", "expr": "8"}, {"label": "鸡", "expr": "12"}],
        )
    ]
    sbx_map = SympySandboxResult(ok=True, status="PASSED", values={"主问": ["鸡:12", "兔:8"]})
    res_map = await compare_answer_parts(
        p_map,
        s_map,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=sbx_map,
        timeout_seconds=10.0,
    )
    assert res_map.all_dual_equal
    assert res_map.all_sympy_ok


@pytest.mark.asyncio
async def test_compare_choice_translation_and_mixed_qualitative() -> None:
    # ch28_003: 一方 CHOICE 字母，一方 NUMERIC 数值，经 options 安全互译 + sympy_expr 互校
    options = {"A": "1000 米", "B": "1200 米", "C": "1300 米", "D": "1700 米"}
    p_choice = [
        AnswerPart(
            label="主问",
            answer_type="CHOICE",
            verify_mode="EXACT",
            answer="C",
            choice_label="C",
            sympy_expr="1300",
        )
    ]
    s_num = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            answer="1300 米",
            sympy_expr="1300",
        )
    ]
    res = await compare_answer_parts(
        p_choice,
        s_num,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        options=options,
        timeout_seconds=10.0,
    )
    assert res.all_dual_equal
    assert res.all_sympy_ok

    # CHOICE 的 sympy_expr 与 options 不一致 -> SYMPY_FAILED
    p_bad_choice = [
        AnswerPart(
            label="主问",
            answer_type="CHOICE",
            verify_mode="EXACT",
            answer="C",
            choice_label="C",
            sympy_expr="1000",
        )
    ]
    res_bad = await compare_answer_parts(
        p_bad_choice,
        p_bad_choice,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        options=options,
        timeout_seconds=10.0,
    )
    assert not res_bad.all_sympy_ok
    assert "SYMPY_FAILED" in res_bad.review_reasons

    # MIXED (ch01_015 / ch28_031) + 定性裁判
    async def _judge_low(lbl: str, a: str, b: str) -> QualitativeJudgeOutput:
        return QualitativeJudgeOutput(equivalent=True, confidence="LOW", reason="表述略含糊")

    p_mix = [
        AnswerPart(label="(1)", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="12", sympy_expr="12"),
        AnswerPart(label="(2)", answer_type="TEXT", verify_mode="QUALITATIVE", answer="连接对角线平分面积"),
    ]
    s_mix = [
        AnswerPart(label="(1)", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="12", sympy_expr="12"),
        AnswerPart(label="(2)", answer_type="TEXT", verify_mode="QUALITATIVE", answer="作对角线把长方形等分为二"),
    ]
    sbx_mix = SympySandboxResult(ok=True, status="PASSED", values={"(1)": "12"})
    res_low = await compare_answer_parts(
        p_mix,
        s_mix,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=sbx_mix,
        qualitative_judge=_judge_low,
        timeout_seconds=10.0,
    )
    assert not res_low.all_dual_equal
    assert "QUALITATIVE_UNCERTAIN" in res_low.review_reasons

    # 数量不对齐 -> PART_COUNT_MISMATCH
    res_cnt = await compare_answer_parts(
        p_mix,
        [s_mix[0]],
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=sbx_mix,
        timeout_seconds=10.0,
    )
    assert res_cnt.part_count_mismatch
    assert "PART_COUNT_MISMATCH" in res_cnt.review_reasons
