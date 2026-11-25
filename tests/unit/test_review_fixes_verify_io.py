"""Worker B (verify & I/O) 审查缺陷修复回归测试（B1-B4）。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from shuati.core.image_store import archive_image, prepare_external_image, validate_image
from shuati.core.text_norm import detect_and_extract_choice_options, normalize_text
from shuati.models.domain import AnswerPart
from shuati.services.practice import _has_wrapping_delimiters, _parse_student_answer_parts
from shuati.verify import equivalence
from shuati.verify.equivalence import (
    _verify_part_against_sandbox,
    compare_answer_parts,
    latex_to_plain,
    strip_units,
)
from shuati.verify.sympy_sandbox import (
    SympySandboxResult,
    run_verify_script,
    split_top_level,
    validate_script_ast,
)

# ---------------------------------------------------------------------------
# B1: text_norm.py 横向选项空白保护与单空格兜底
# ---------------------------------------------------------------------------


def test_b1_normalize_text_preserves_horizontal_choice_separators() -> None:
    raw_1x4 = "图中   有   正方形，求面积。\nA.  1000 米    B.  1200 米    C.  1300 米    D.  1700 米"
    norm = normalize_text(raw_1x4, num_images=0)
    # 普通多空格折叠为单空格
    assert "图中 有 正方形" in norm.normalized_text
    # 横向选项行保留双空格分隔
    assert "A. 1000 米  B. 1200 米  C. 1300 米  D. 1700 米" in norm.normalized_text

    is_choice, opts = detect_and_extract_choice_options(norm.normalized_text)
    assert is_choice is True
    assert opts == {"A": "1000 米", "B": "1200 米", "C": "1300 米", "D": "1700 米"}

    # 2x2 网格 + 全角选项标签
    raw_2x2 = "下列哪个数最小？\nＡ.  3    Ｂ.  4\nＣ.  5    Ｄ.  6"
    norm_2x2 = normalize_text(raw_2x2, num_images=0)
    is_choice_2x2, opts_2x2 = detect_and_extract_choice_options(norm_2x2.normalized_text)
    assert is_choice_2x2 is True
    assert opts_2x2 == {"A": "3", "B": "4", "C": "5", "D": "6"}


def test_b1_single_space_horizontal_choice_fallback_and_guards() -> None:
    # 单空格连续横向选项（如人工覆写直接录入）
    is_choice, opts = detect_and_extract_choice_options("请选择正确答案：\nA. 10 B. 20 C. 30 D. 40")
    assert is_choice is True
    assert opts == {"A": "10", "B": "20", "C": "30", "D": "40"}

    # 单空格 2x2 网格
    is_choice_grid, opts_grid = detect_and_extract_choice_options("A. 3 B. 4\nC. 5 D. 6")
    assert is_choice_grid is True
    assert opts_grid == {"A": "3", "B": "4", "C": "5", "D": "6"}

    # 负例1：非严格连续字母不触发单空格拆分
    is_choice_skip, opts_skip = detect_and_extract_choice_options("A. 10 C. 30")
    assert is_choice_skip is False
    assert opts_skip == {}

    # 负例2：非行首起始不触发单空格拆分
    is_choice_mid, opts_mid = detect_and_extract_choice_options("选择 A. 10 B. 20 C. 30")
    assert is_choice_mid is False
    assert opts_mid == {}

    # 负例3：几何声明过滤
    is_choice_geo, opts_geo = detect_and_extract_choice_options("A. 如图点B. 在边上 B. 20")
    assert is_choice_geo is False
    assert opts_geo == {}

    # 负例4：分隔符族混用不触发单空格拆分（如 "." 与 ")" 混用）
    is_choice_mixed, opts_mixed = detect_and_extract_choice_options("A. 10 B) 20 C. 30 D. 40")
    assert is_choice_mixed is False
    assert opts_mixed == {}

    # 正例：含 $...$ / 半开区间及同族 ")" 分隔符的横向选项仍可正确提取
    is_choice_math_h, opts_math_h = detect_and_extract_choice_options(
        "A. $[0, 1)$ B. $(1, 2]$ C. $[2, 3)$ D. $(3, 4]$"
    )
    assert is_choice_math_h is True
    assert opts_math_h == {"A": "$[0, 1)$", "B": "$(1, 2]$", "C": "$[2, 3)$", "D": "$(3, 4]$"}

    is_choice_paren_h, opts_paren_h = detect_and_extract_choice_options(
        "A) $(A + B) = 1$ B) $(A - B) = 1$ C) $A = 1$ D) $B = 1$"
    )
    assert is_choice_paren_h is True
    assert opts_paren_h == {
        "A": "$(A + B) = 1$",
        "B": "$(A - B) = 1$",
        "C": "$A = 1$",
        "D": "$B = 1$",
    }


def test_b1_repro_vertical_math_options_and_single_line_interval_not_corrupted() -> None:
    # Repro #1.1: 纵向 LaTeX 选择题不在公式内部误拆
    raw_latex = (
        "下列式子恒成立的是：\n"
        "A. $(A + B) = 1$\n"
        "B. $(A - B) = 1$\n"
        "C. $A = 1$\n"
        "D. $B = 1$"
    )
    norm_latex = normalize_text(raw_latex, num_images=0).normalized_text
    is_choice, opts = detect_and_extract_choice_options(norm_latex)
    assert is_choice is True
    assert opts == {
        "A": "$(A + B) = 1$",
        "B": "$(A - B) = 1$",
        "C": "$A = 1$",
        "D": "$B = 1$",
    }

    # Repro #1.2: 纵向坐标与集合选项不被圆括号内 B)/C)/D)/E) 破坏
    raw_coords = (
        "若点 P 满足条件，则：\n"
        "A. (x, B) 在第一象限\n"
        "B. (x, C) 在第二象限\n"
        "C. (x, D) 在第三象限\n"
        "D. (x, E) 在第四象限"
    )
    norm_coords = normalize_text(raw_coords, num_images=0).normalized_text
    is_choice_c, opts_c = detect_and_extract_choice_options(norm_coords)
    assert is_choice_c is True
    assert opts_c == {
        "A": "(x, B) 在第一象限",
        "B": "(x, C) 在第二象限",
        "C": "(x, D) 在第三象限",
        "D": "(x, E) 在第四象限",
    }

    raw_set = (
        "下列命题正确的是：\n"
        "A. (A ∩ B) ≠ ∅\n"
        "B. (A ∪ B) = U\n"
        "C. A ⊆ B\n"
        "D. B ⊆ A"
    )
    norm_set = normalize_text(raw_set, num_images=0).normalized_text
    is_choice_s, opts_s = detect_and_extract_choice_options(norm_set)
    assert is_choice_s is True
    assert opts_s == {
        "A": "(A ∩ B) ≠ ∅",
        "B": "(A ∪ B) = U",
        "C": "A ⊆ B",
        "D": "B ⊆ A",
    }

    # Repro #1.3: 单行含区间 (a, B) 不误判为选择题（含 ")" 同族分隔符）
    for raw_non_choice in (
        "A. 区间 (a, B) 上单调递增",
        "A) 区间 (a, B) 上单调递增",
        "A. 区间 [a, B) 上单调递增",
    ):
        norm_nc = normalize_text(raw_non_choice, num_images=0).normalized_text
        is_choice_nc, opts_nc = detect_and_extract_choice_options(norm_nc)
        assert is_choice_nc is False
        assert opts_nc == {}


# ---------------------------------------------------------------------------
# B2: sympy_sandbox.py split_top_level + 数值字符串构造白名单
# ---------------------------------------------------------------------------


def test_b2_split_top_level() -> None:
    assert split_top_level("(1, 2), (3, 4)") == ["(1, 2)", "(3, 4)"]
    assert split_top_level("A:(1, 2), B:[3, 4], C:{5, 6}") == ["A:(1, 2)", "B:[3, 4]", "C:{5, 6}"]
    assert split_top_level("1, 2, 3") == ["1", "2", "3"]
    assert split_top_level("") == []


def test_b2_ast_numeric_string_whitelist_and_rejection() -> None:
    allowed_script = """
from fractions import Fraction
import sympy as sp
from sympy import Rational, Float, Integer

def verify():
    a = Rational("1/3") + Rational("0.25") + sp.Rational("-1/12")
    b = Fraction("3/7") + Fraction("1/7")
    c = Float("0.25") * 4
    d = Integer("42") + 1
    return {"(1)": str(a), "(2)": str(b), "(3)": str(c), "(4)": str(d)}
"""
    probe = validate_script_ast(allowed_script)
    assert probe.ok, f"合法纯数字字符串构造应被放行，实际拒绝原因: {probe.reason}"

    sbx_res = run_verify_script(allowed_script, timeout_seconds=10.0)
    assert sbx_res.ok, sbx_res.stderr_tail
    assert sbx_res.values is not None
    assert sbx_res.values["(1)"] == "1/2"
    assert sbx_res.values["(2)"] == "4/7"
    assert sbx_res.values["(4)"] == "43"

    rejected_scripts = [
        'from sympy import Rational\ndef verify():\n    return {"主问": str(Rational("1+2"))}\n',
        "from sympy import Rational\ndef verify():\n    return {'主问': str(Rational(\"__import__('os')\"))}\n",
        'from sympy import Rational\ndef verify():\n    s = "1/3"\n    return {"主问": str(Rational(s))}\n',
        'from sympy import Rational\ndef verify():\n    n = 3\n    return {"主问": str(Rational(f"1/{n}"))}\n',
        'from sympy import Rational\ndef verify():\n    return {"主问": str(Rational("1" + "/3"))}\n',
        'from fractions import Fraction\ndef verify():\n    return {"主问": str(Fraction("1+2"))}\n',
        'from sympy import Float\ndef verify():\n    return {"主问": str(Float("1+2"))}\n',
        'from sympy import Integer\ndef verify():\n    return {"主问": str(Integer("1+2"))}\n',
    ]
    for bad in rejected_scripts:
        res = validate_script_ast(bad)
        assert not res.ok, f"危险数值字符串调用应被拒绝:\n{bad}"
        assert res.reason == "FORBIDDEN_CALL"

    # 确保 line 365 的 if/else 合法兜底语义未被破坏
    if_else_script = """
from sympy import Symbol, Eq, solve
def verify():
    x = Symbol('x')
    sols = solve(Eq(x**2 - 4, 0), x)
    if sols:
        ans = str(sols[0])
    else:
        ans = "无解"
    return {"主问": ans}
"""
    assert validate_script_ast(if_else_script).ok


# ---------------------------------------------------------------------------
# B3: equivalence.py & services/practice.py
# ---------------------------------------------------------------------------


def test_b3_latex_to_plain_text_and_mathrm_unwrap() -> None:
    assert latex_to_plain(r"3\times 4 \div 2 \text{平方厘米}") == "3 * 4 / 2"
    assert latex_to_plain(r"\text{A}") == "A"
    assert latex_to_plain(r"\mbox{B}") == "B"
    assert latex_to_plain(r"\mbox{厘米}") == ""
    assert latex_to_plain(r"\mathrm{x} + 1") == "x + 1"
    assert latex_to_plain(r"\operatorname{gcd}(12, 18)") == "gcd(12, 18)"
    # 公共 API strip_units 行为保持不变
    assert strip_units("15 m", allow_latin_unit=True) == "15"


@pytest.mark.asyncio
async def test_b3_latin_unit_stripping_restricted_to_numeric() -> None:
    # EXPRESSION: 2m vs 2 -> False; 2m vs 2t -> False; 2*m vs 2m -> True
    p_2m = [AnswerPart(label="主问", answer_type="EXPRESSION", verify_mode="SYMBOLIC", answer="2m", sympy_expr="2m")]
    s_2 = [AnswerPart(label="主问", answer_type="EXPRESSION", verify_mode="SYMBOLIC", answer="2", sympy_expr="2")]
    s_2t = [AnswerPart(label="主问", answer_type="EXPRESSION", verify_mode="SYMBOLIC", answer="2t", sympy_expr="2t")]
    s_2mulm = [AnswerPart(label="主问", answer_type="EXPRESSION", verify_mode="SYMBOLIC", answer="2*m", sympy_expr="2*m")]

    res_2m_vs_2 = await compare_answer_parts(
        p_2m, s_2, grade_subject="初中数学", allow_pi_314_subjects=[], timeout_seconds=10.0
    )
    assert not res_2m_vs_2.all_dual_equal

    res_2m_vs_2t = await compare_answer_parts(
        p_2m, s_2t, grade_subject="初中数学", allow_pi_314_subjects=[], timeout_seconds=10.0
    )
    assert not res_2m_vs_2t.all_dual_equal

    res_2m_vs_2mulm = await compare_answer_parts(
        p_2m, s_2mulm, grade_subject="初中数学", allow_pi_314_subjects=[], timeout_seconds=10.0
    )
    assert res_2m_vs_2mulm.all_dual_equal

    # EXPRESSION 沙盒校验：2m 不能匹配沙盒返回的 2，应匹配 2*m
    res_sbx_mismatch = await compare_answer_parts(
        p_2m,
        None,
        grade_subject="初中数学",
        allow_pi_314_subjects=[],
        sympy_result=SympySandboxResult(ok=True, status="PASSED", values={"主问": "2"}),
        timeout_seconds=10.0,
    )
    assert not res_sbx_mismatch.all_sympy_ok

    res_sbx_match = await compare_answer_parts(
        p_2m,
        None,
        grade_subject="初中数学",
        allow_pi_314_subjects=[],
        sympy_result=SympySandboxResult(ok=True, status="PASSED", values={"主问": "2*m"}),
        timeout_seconds=10.0,
    )
    assert res_sbx_match.all_sympy_ok

    # NUMERIC: "15 m" vs "15" -> True（双盲及沙盒均成立）
    p_15m = [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="15 m", sympy_expr="15 m")]
    s_15 = [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="15", sympy_expr="15")]
    res_num = await compare_answer_parts(
        p_15m,
        s_15,
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
        sympy_result=SympySandboxResult(ok=True, status="PASSED", values={"主问": "15"}),
        timeout_seconds=10.0,
    )
    assert res_num.all_dual_equal
    assert res_num.all_sympy_ok

    # UNORDERED_MULTISET 下 EXPRESSION 同样禁用单字母拉丁单位剥离（包括 Pass 1）
    p_mset = [
        AnswerPart(
            label="主问",
            answer_type="EXPRESSION",
            verify_mode="SYMBOLIC",
            collection_mode="UNORDERED_MULTISET",
            answer="2m, 3t",
            items=[{"expr": "2m"}, {"expr": "3t"}],
        )
    ]
    s_mset = [
        AnswerPart(
            label="主问",
            answer_type="EXPRESSION",
            verify_mode="SYMBOLIC",
            collection_mode="UNORDERED_MULTISET",
            answer="2, 3",
            items=[{"expr": "2"}, {"expr": "3"}],
        )
    ]
    res_mset = await compare_answer_parts(
        p_mset, s_mset, grade_subject="初中数学", allow_pi_314_subjects=[], timeout_seconds=10.0
    )
    assert not res_mset.all_dual_equal


@pytest.mark.asyncio
async def test_b3_sandbox_coordinate_split_and_multiset_pass1(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def _fail_if_called(a: str, b: str, **_kwargs: object) -> tuple[bool, str | None]:
        calls.append((a, b))
        raise AssertionError(f"Pass 1 应直接命中，不应派生子进程比对 ({a!r}, {b!r})")

    monkeypatch.setattr(equivalence, "run_symbolic_check_in_sandbox", _fail_if_called)

    # UNORDERED_MULTISET 重排坐标串 -> Pass 1 直接命中且零子进程调用
    p_coords = AnswerPart(
        label="主问",
        answer_type="EXPRESSION",
        verify_mode="SYMBOLIC",
        collection_mode="UNORDERED_MULTISET",
        answer="(3, 4), (1, 2)",
        items=[{"expr": "(3, 4)"}, {"expr": "(1, 2)"}],
    )
    ok_mset = await _verify_part_against_sandbox(
        p_coords,
        "(1, 2), (3, 4)",
        allow_pi_314=False,
        timeout_seconds=5.0,
        memory_limit_mb=256,
    )
    assert ok_mset is True
    assert calls == []

    # ORDERED_TUPLE 坐标串顶层切分
    p_tuple = AnswerPart(
        label="主问",
        answer_type="EXPRESSION",
        verify_mode="SYMBOLIC",
        collection_mode="ORDERED_TUPLE",
        answer="(1, 2), (3, 4)",
        items=[{"expr": "(1, 2)"}, {"expr": "(3, 4)"}],
    )
    # 针对 ORDERED_TUPLE，使用真实沙盒或验证顶层拆分后长度对齐
    monkeypatch.undo()
    ok_tup = await _verify_part_against_sandbox(
        p_tuple,
        "(1, 2), (3, 4)",
        allow_pi_314=False,
        timeout_seconds=10.0,
        memory_limit_mb=512,
    )
    assert ok_tup is True

    # LABELED_MAP 坐标串顶层切分
    p_map = AnswerPart(
        label="主问",
        answer_type="EXPRESSION",
        verify_mode="SYMBOLIC",
        collection_mode="LABELED_MAP",
        answer="A:(1, 2), B:(3, 4)",
        items=[{"label": "A", "expr": "(1, 2)"}, {"label": "B", "expr": "(3, 4)"}],
    )
    ok_map = await _verify_part_against_sandbox(
        p_map,
        "A:(1, 2), B:(3, 4)",
        allow_pi_314=False,
        timeout_seconds=10.0,
        memory_limit_mb=512,
    )
    assert ok_map is True


def test_b3_practice_parse_student_answer_coordinates_and_delimiters() -> None:
    assert _has_wrapping_delimiters("(1, 2), (3, 4)", "(", ")") is False
    assert _has_wrapping_delimiters("{1, 2}, {3, 4}", "{", "}") is False
    assert _has_wrapping_delimiters("(1, 2, 3)", "(", ")") is True
    assert _has_wrapping_delimiters("{1, 2, 3}", "{", "}") is True
    assert _has_wrapping_delimiters("{(1, 2), (3, 4)}", "{", "}") is True

    exp_mset = [
        AnswerPart(
            label="主问",
            answer_type="EXPRESSION",
            verify_mode="SYMBOLIC",
            collection_mode="UNORDERED_MULTISET",
            answer="(1, 2), (3, 4)",
            items=[{"expr": "(1, 2)"}, {"expr": "(3, 4)"}],
        )
    ]
    stu1, miss1, extra1 = _parse_student_answer_parts("(1, 2), (3, 4)", exp_mset)
    assert miss1 == [] and extra1 == []
    assert stu1[0].items == [{"expr": "(1, 2)"}, {"expr": "(3, 4)"}]

    stu2, miss2, extra2 = _parse_student_answer_parts("{(1, 2), (3, 4)}", exp_mset)
    assert miss2 == [] and extra2 == []
    assert stu2[0].items == [{"expr": "(1, 2)"}, {"expr": "(3, 4)"}]

    exp_tup = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="ORDERED_TUPLE",
            answer="1, 2, 3",
            items=[{"expr": "1"}, {"expr": "2"}, {"expr": "3"}],
        )
    ]
    stu_tup, _, _ = _parse_student_answer_parts("(1, 2, 3)", exp_tup)
    assert stu_tup[0].items == [{"expr": "1"}, {"expr": "2"}, {"expr": "3"}]

    exp_map = [
        AnswerPart(
            label="主问",
            answer_type="EXPRESSION",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="A:(1, 2), B:(3, 4)",
            items=[{"label": "A", "expr": "(1, 2)"}, {"label": "B", "expr": "(3, 4)"}],
        )
    ]
    stu_map, _, _ = _parse_student_answer_parts("A:(1, 2), B:(3, 4)", exp_map)
    assert stu_map[0].items == [{"label": "A", "expr": "(1, 2)"}, {"label": "B", "expr": "(3, 4)"}]


# ---------------------------------------------------------------------------
# B4: image_store.py 截断图检测、唯一原子临时文件清理、透明底白底合成
# ---------------------------------------------------------------------------


def test_b4_validate_image_rejects_truncated_jpeg_and_png(tmp_path: Path) -> None:
    good_jpg = tmp_path / "good.jpg"
    Image.new("RGB", (200, 200), color=(30, 120, 210)).save(good_jpg, format="JPEG")
    trunc_jpg = tmp_path / "trunc.jpg"
    raw_jpg = good_jpg.read_bytes()
    trunc_jpg.write_bytes(raw_jpg[: max(1, len(raw_jpg) // 2)])

    res_jpg = validate_image(trunc_jpg)
    assert res_jpg.is_valid is False
    assert "CORRUPTED_IMAGE" in res_jpg.error

    good_png = tmp_path / "good.png"
    img = Image.new("RGB", (200, 200))
    for y in range(200):
        for x in range(200):
            img.putpixel((x, y), ((x * 7) % 256, (y * 13) % 256, ((x + y) * 5) % 256))
    img.save(good_png, format="PNG")
    trunc_png = tmp_path / "trunc.png"
    raw_png = good_png.read_bytes()
    trunc_png.write_bytes(raw_png[: max(1, len(raw_png) // 2)])

    res_png = validate_image(trunc_png)
    assert res_png.is_valid is False
    assert "CORRUPTED_IMAGE" in res_png.error


def test_b4_archive_image_concurrent_and_cleanup_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src_img = tmp_path / "sample.jpg"
    Image.new("RGB", (160, 160), color="teal").save(src_img)
    cas_dir = tmp_path / "cas"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: archive_image(src_img, target_dir=cas_dir), range(12)))

    assert len(set(results)) == 1
    assert (cas_dir / results[0]).exists()
    assert list(cas_dir.glob("*.tmp")) == []

    # 模拟 os.replace 失败，确保临时文件被清理
    cas_fail_dir = tmp_path / "cas_fail"

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("shuati.core.image_store.os.replace", _boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        archive_image(src_img, target_dir=cas_fail_dir)

    assert list(cas_fail_dir.glob("*.tmp")) == []


def test_b4_prepare_external_image_alpha_white_background_and_modes(tmp_path: Path) -> None:
    # 1. RGBA 全透明像素 -> 合成后应为白色 (255, 255, 255)，而非黑色
    src_rgba = tmp_path / "trans_rgba.png"
    rgba = Image.new("RGBA", (100, 100), color=(0, 0, 0, 0))
    rgba.save(src_rgba)
    dst_rgba = tmp_path / "out_rgba.jpg"
    prepare_external_image(src_rgba, dst_rgba)
    with Image.open(dst_rgba) as out:
        assert out.mode == "RGB"
        px = out.getpixel((50, 50))
        assert isinstance(px, tuple)
        assert min(px) >= 250

    # 2. LA 透明灰度 -> 白底
    src_la = tmp_path / "trans_la.png"
    la = Image.new("LA", (100, 100), color=(0, 0))
    la.save(src_la)
    dst_la = tmp_path / "out_la.jpg"
    prepare_external_image(src_la, dst_la)
    with Image.open(dst_la) as out:
        assert out.mode == "RGB"
        px = out.getpixel((50, 50))
        assert isinstance(px, tuple)
        assert min(px) >= 250

    # 3. 调色板 P 带透明色索引 0（索引 0 为黑）-> 白底
    src_p = tmp_path / "trans_p.png"
    p_img = Image.new("P", (100, 100), color=0)
    p_img.putpalette([0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254)
    p_img.save(src_p, transparency=0)
    dst_p = tmp_path / "out_p.jpg"
    prepare_external_image(src_p, dst_p)
    with Image.open(dst_p) as out:
        assert out.mode == "RGB"
        px = out.getpixel((50, 50))
        assert isinstance(px, tuple)
        assert min(px) >= 250

    # 4. CMYK / 1-bit / L 模式均可安全转 RGB 保存为 JPEG
    for mode, color in (("CMYK", (0, 100, 100, 0)), ("1", 1), ("L", 200)):
        src_m = tmp_path / f"mode_{mode}.tiff"
        Image.new(mode, (80, 80), color=color).save(src_m)
        dst_m = tmp_path / f"mode_{mode}.jpg"
        prepare_external_image(src_m, dst_m)
        with Image.open(dst_m) as out:
            assert out.mode == "RGB"
            assert out.size == (80, 80)
