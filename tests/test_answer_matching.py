"""P4-F：LaTeX 答案规范化与等价判定共享栈单测（迁移自 scaffold_engine，行为零漂移）。

规格：docs/tasks/P4-F_integration_fixes.md 测试要求 +
docs/designs/adaptive_practice_harness.md CUJ 2/3（代数等价判据）。

共享栈现居 backend/sandbox/sympy_verifier.py（latex_to_plain / normalize_answer /
answers_equivalent），scaffold_engine 与 blind_drill_scheduler 均从此导入。本文件
从 scaffold 既有测试语义平移：LaTeX 分数/根号/下标/希腊命令归一化、蕴含链取结论、
链式等式首尾、裸值对单方程右端、字符串相等快路径。等价判定走真实 SymPy 隔离子进程
（无网络）；快路径用 monkeypatch 证明不触发沙盒。
"""

from __future__ import annotations

import pytest

from backend.sandbox import sympy_verifier
from backend.sandbox.sympy_verifier import (
    answers_equivalent,
    latex_to_plain,
    normalize_answer,
)
from backend.services import scaffold_engine

# ---------------------------------------------------------------------------
# 迁移接线：scaffold 私有副本已删除，改为共享栈同一对象（零漂移证明）
# ---------------------------------------------------------------------------


def test_scaffold_imports_shared_stack_not_private_copy() -> None:
    # scaffold 调用点绑定到 sympy_verifier 的公开函数（同一对象）
    assert scaffold_engine.normalize_answer is sympy_verifier.normalize_answer
    assert scaffold_engine.answers_equivalent is sympy_verifier.answers_equivalent
    # 私有副本已彻底删除
    assert not hasattr(scaffold_engine, "_latex_to_plain")
    assert not hasattr(scaffold_engine, "_normalize_answer")
    assert not hasattr(scaffold_engine, "_answers_equivalent")


# ---------------------------------------------------------------------------
# latex_to_plain：分数 / 根号 / 下标 / 上标 / 希腊命令 / 单位 / 间距
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("latex", "plain"),
    [
        # 分数 → 函数式除法
        (r"\frac{mv^2}{2}", "((mv^2)/(2))"),
        # 根号 → sqrt(...)
        (r"\sqrt{gR}", "sqrt(gR)"),
        # 嵌套 frac/sqrt 交替展开至不动点
        (r"\frac{\sqrt{gR}}{2}", "((sqrt(gR))/(2))"),
        (r"\sqrt{\frac{a}{b}}", "sqrt(((a)/(b)))"),
        # 下标扁平化（封死下划线内省面）
        (r"v_{max}", "vmax"),
        (r"v_0", "v0"),
        # 上标保留 ^（convert_xor）
        (r"x^{2}", "x^(2)"),
        # 希腊字母命令 → 命令名
        (r"\theta", "theta"),
        (r"\alpha + \beta", "alpha + beta"),
        # 单位 \text{...} 去除
        (r"20\ \text{m/s}", "20"),
        # \left/\right 去除
        (r"\left(x+1\right)", "(x+1)"),
        # \cdot / \times → *（原有行为：保留运算符后的空格）
        (r"a\cdot b", "a* b"),
        (r"a\times b", "a* b"),
        # 等号两侧规范化间距
        ("x=2", "x = 2"),
    ],
)
def test_latex_to_plain(latex: str, plain: str) -> None:
    assert latex_to_plain(latex) == plain


# ---------------------------------------------------------------------------
# normalize_answer：蕴含链取结论 / 链式等式首尾
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        # 蕴含链取结论段（各种蕴含分隔符）
        (r"mg - N = 0 \Rightarrow N = mg", "N = mg"),
        (r"a = b \Longrightarrow b = c", "b = c"),
        (r"a = b \implies b = c", "b = c"),
        ("a = b ⇒ b = c", "b = c"),
        ("a = b → b = c", "b = c"),
        # 链式等式 a=b=c 取首尾 a=c
        (r"v_{max} = \sqrt{gR} = 20", "vmax = 20"),
        ("a = b = c", "a = c"),
        # 多式（; 或换行）不折叠首尾
        ("x = 2; y = 3", "x = 2; y = 3"),
        # 空/纯空白
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_answer(raw: str, normalized: str) -> None:
    assert normalize_answer(raw) == normalized


# ---------------------------------------------------------------------------
# answers_equivalent：字符串快路径 / LaTeX↔纯文本 / 裸值对单方程右端 / 空输入
# ---------------------------------------------------------------------------


def test_string_equality_fast_path_skips_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """规范化后字符串相等 → 直接 True，绝不触发 SymPy 沙盒。"""

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("字符串快路径不应调用 verify_equation_equivalence")

    monkeypatch.setattr(sympy_verifier, "verify_equation_equivalence", _boom)
    # 规范化后相等（LaTeX vs 纯文本，下标/间距差异被抹平）
    assert answers_equivalent(r"v_{max} = 20", "vmax=20") is True
    assert answers_equivalent("x = 2", "x=2") is True


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        # LaTeX 标准答案 vs 纯文本提交（缺口2 核心：真实 LaTeX 可判）
        ("mv^2/2 = qU", r"\frac{mv^2}{2} = qU", True),
        (r"\frac{1}{2}mv^2 = qU", r"\frac{mv^2}{2} = qU", True),
        # 根号 LaTeX vs 纯文本
        (r"v_{max} = \sqrt{gR}", "vmax = sqrt(g*R)", True),
        # 下标扁平化后等价
        (r"v_{max} = 20", "vmax = 20", True),
        # 裸值对单方程右端
        ("20", "vmax = 20", True),
        ("20", r"v_{max} = 20\ \text{m/s}", True),
        # 蕴含链结论匹配
        (r"mg - N = 0 \Rightarrow N = mg", "N = mg", True),
        # 不等价
        (r"v_{max} = \sqrt{2gR}", r"v_{max} = \sqrt{gR}", False),
        ("x = 9", "x = 2", False),
        # 纯数字答案严格数值相等（PM 批准自由符号守卫：反号/常数比率仅限含符号表达式）
        ("5", "7", False),
        ("5", "-5", False),
        ("5", "5", True),
        ("1/2", "0.5", True),
        # 方程缩放语义保留（含自由符号：反号与常数比率仍等价）
        ("2*x = 4", "x = 2", True),
        ("x - 2", "2 - x", True),
        # 符号裸值不匹配（非常数比率）
        ("y", "vmax = 20", False),
        # 空输入一律 False
        ("", "x = 2", False),
        ("x = 2", "", False),
        ("", "", False),
    ],
)
def test_answers_equivalent(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected


def test_bare_value_not_matched_against_multi_equation_standard() -> None:
    """标准答案为多方程时，裸值不享受「对单方程右端」豁免。"""
    assert answers_equivalent("2", "x = 2; y = 3") is False


# ---------------------------------------------------------------------------
# PM 紧急修复（2026-11-13 用户实测）：带单位答案误判 / 全角归一
# 规格：normalize_answer 全角→半角 + 尾部中文单位剥离；answers_equivalent
# 拉丁单位豁免（仅纯数字标准答案）；既有判据语义零漂移。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        # 裸中文单位 / 拉丁单位 / 全角数字 vs \text{米} 标准答案 → 全部 True
        ("15米", r"15\ \text{米}", True),
        ("15 米", r"15\ \text{米}", True),
        ("15 m", r"15\ \text{米}", True),
        ("15m", r"15\ \text{米}", True),
        ("１５", r"15\ \text{米}", True),
        # 复合中文单位与自匹配
        ("20米/秒", "20", True),
        ("15米", "15米", True),
        # 符号题回归：代数语境绝不剥离（'15*m' 的 '*' 不满足 lookbehind）
        ("15*m", "15", False),
        ("m*v", "v*m", True),
        ("R=mv/(qB)", "R = m*v/(q*B)", True),
        # 全角方程归一
        ("ｘ＝５", "x=5", True),
        # 标准答案含符号时 '15m' 保留隐式乘法语义（15*m）
        ("15m", "15*m", True),
        # 单位豁免不放行错误数值（自由符号守卫 5≠7 不动）
        ("15米", "16", False),
        ("5m", "7", False),
        # 复合单位 LaTeX 标准答案
        ("20米/秒", r"20\ \text{m/s}", True),
        # 尾随标点容忍
        ("15米。", "15", True),
        ("15m.", "15", True),
    ],
)
def test_unit_and_fullwidth_answers(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        # 全角数字/字母/符号 → 半角
        ("１５", "15"),
        ("ｘ＝５", "x = 5"),
        ("（１＋２）／３", "(1+2)/3"),
        # 尾部中文单位剥离（最长优先）
        ("20米/秒", "20"),
        ("15 米", "15"),
        ("9.8米", "9.8"),
        ("3平方厘米", "3"),
        ("2千米每小时", "2"),
        ("5千克。", "5"),
        # 中文单位不在尾部 → 不剥离（非单位语境不动）
        ("米 = 100厘米", "米 = 100"),
    ],
)
def test_normalize_answer_units_and_fullwidth(raw: str, normalized: str) -> None:
    assert normalize_answer(raw) == normalized


def test_latin_unit_exemption_skips_sandbox_on_string_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯数字标准答案 + 拉丁单位剥离后字符串相等 → 快路径，不触发沙盒。"""

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("单位豁免字符串快路径不应调用 verify_equation_equivalence")

    monkeypatch.setattr(sympy_verifier, "verify_equation_equivalence", _boom)
    assert answers_equivalent("15m", "15") is True
    assert answers_equivalent("20米/秒", "20") is True


def test_algebraic_context_never_strips_latin_unit() -> None:
    """标准答案含自由符号时，'15m' 绝不剥离为 '15'（代数语境保留）。"""
    # 若误剥离，'15' 对 '15*m' 会判 False；保留隐式乘法才能判 True
    assert answers_equivalent("15m", "15*m") is True
    # 反向：纯数字标准答案下 '15*m' 仍不豁免（lookbehind 拦截 '*'）
    assert answers_equivalent("15*m", "15") is False


# ---------------------------------------------------------------------------
# P6：复合数量答案解析（2026-11-13 用户报告：「9小时36分」误判）
# 规格：docs/tasks/P6-compound-quantity.md —— 复合量必须在 normalize 之前解析
# （\text 剥离会把数字拼接成 "936"）；任一方为数量即权威判定，绝不回落
# normalize 字符串比对；既有判据（自由符号守卫/拉丁豁免/中文剥离）零漂移。
# ---------------------------------------------------------------------------

_P6_STD_TIME = r"9\ \text{小时}\ 36\ \text{分}"  # = 576 分钟 = 9.6 小时


@pytest.mark.parametrize(
    "user",
    [
        "9小时36分",
        "9小时36分钟",
        "9.6小时",
        "576分",
        "576分钟",
        "9.6",  # 裸数字：小时表示
        "576",  # 裸数字：分钟表示
        "9小时36",  # 小时后尾部裸数字按分钟推断
        "９小时３６分",  # 全角
        r"9\ \text{小时}\ 36\ \text{分}",  # LaTeX 原样
    ],
)
def test_p6_compound_time_accepted(user: str) -> None:
    assert answers_equivalent(user, _P6_STD_TIME) is True


@pytest.mark.parametrize(
    "user",
    [
        "9小时35分",
        "10小时",
        "936",  # 关键红线：破损拼接值绝不通过
        "9.6分",
        "576小时",
        "9小时",
        "36分",
    ],
)
def test_p6_compound_time_rejected(user: str) -> None:
    assert answers_equivalent(user, _P6_STD_TIME) is False


def test_p6_user_real_case() -> None:
    """用户真实题目 a01b47d7 端到端：「9小时36分」对 LaTeX 标准答案必须判对。"""
    assert answers_equivalent("9小时36分", "9\\ \\text{小时}\\ 36\\ \\text{分}") is True


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        # 长度/质量复合量
        ("2米30厘米", "2.3米", True),
        ("230厘米", r"2\ \text{米}\ 30\ \text{厘米}", True),
        ("1千克50克", "1050克", True),
        ("2米31厘米", "2.3米", False),
        ("2米30厘米", "230厘米", True),
        ("230", "2米30厘米", True),  # 裸数字：厘米表示
        ("2300", "2米30厘米", True),  # 裸数字：毫米表示
        ("1.05", "1千克50克", True),  # 裸数字：千克表示
        ("1050", "1千克50克", True),  # 裸数字：克表示
        # 符号回归：零漂移既有判据
        ("m*v", "v*m", True),
        ("15*m", "15", False),
        ("15米", "15", True),
        ("x=5", "x = 5", True),
        ("20米/秒", "20", True),
        ("15m", "15*m", True),  # 代数语境隐式乘法保留（单字母拉丁不进数量解析）
    ],
)
def test_p6_length_mass_and_symbol_regression(
    user: str, standard: str, expected: bool
) -> None:
    assert answers_equivalent(user, standard) is expected


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        # 货币：「分」在同串出现 元/角 时按货币（0.01元）
        ("5元3角", "5.3元", True),
        ("5元30分", "5.3元", True),
        ("0.36元", "3角6分", True),
        ("5.3", "5元3角", True),  # 裸数字：元表示
        ("53", "5元3角", True),  # 裸数字：角表示
        ("530", "5元3角", True),  # 裸数字：分表示
        # 「分」歧义默认时间：无货币上下文不跨量纲放行
        ("36分", "0.36元", False),
        ("5元3角", "530分", False),
        ("36分", "36分", True),
    ],
)
def test_p6_money_units(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        ("9时36分", _P6_STD_TIME, True),  # 「时」别名
        ("9小时36分。", _P6_STD_TIME, True),  # 尾随标点
        (r"9\,\text{小时}\,36\,\text{分}", _P6_STD_TIME, True),
        (r"9\mathrm{小时}36\mathrm{分}", _P6_STD_TIME, True),
        (r"9\quad\text{小时}\quad36\quad\text{分}", _P6_STD_TIME, True),
        ("9小时36分", "９小时３６分", True),  # 全角标准答案
        (_P6_STD_TIME, "9小时36分", True),  # 方向反转
        ("576分钟", "9.6小时", True),
        ("34560", _P6_STD_TIME, True),  # 裸数字：秒表示
        (r"540分 \Rightarrow 9\text{小时}36\text{分}", _P6_STD_TIME, True),  # 蕴含链结论
        ("90min", "1.5小时", True),  # 多字母拉丁单位无歧义
        ("2km", "2000米", True),
        ("1.5kg", "1500克", True),
        ("500mm", "0.5米", True),
    ],
)
def test_p6_aliases_and_latex_variants(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        # 尾随单字母拉丁单位：数量语境按量纲换算（非量纲盲剥）
        ("9.6h", _P6_STD_TIME, True),
        ("34560s", _P6_STD_TIME, True),
        ("576s", _P6_STD_TIME, False),  # 576秒=9.6分 ≠ 576分
        ("9.6h", "9.6分", False),
        ("1500g", "1.5千克", True),
        ("15g", "15米", False),  # 异量纲拒绝
        ("15m", "15克", False),
        ("15*m", r"15\ \text{米}", False),  # 代数式绝不进入数量换算
    ],
)
def test_p6_latin_suffix_dimension_aware(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        ("-5米", "-500厘米", True),
        ("-5米", "-5", True),
        ("-5", "-5米", True),
        ("-5米", "5米", False),
    ],
)
def test_p6_negative_quantities(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected


def test_p6_quantity_path_authoritative_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """双方均为数量时权威判定：绝不回落 normalize 比对，也不触发沙盒。"""

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("数量权威路径不应调用 normalize/沙盒")

    monkeypatch.setattr(sympy_verifier, "normalize_answer", _boom)
    monkeypatch.setattr(sympy_verifier, "verify_equation_equivalence", _boom)
    assert answers_equivalent("9小时36分", _P6_STD_TIME) is True
    assert answers_equivalent("9小时35分", _P6_STD_TIME) is False
    assert answers_equivalent("9小时36分", "2米30厘米") is False  # 异量纲


def test_p6_broken_concat_never_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """关键红线：破损拼接值 '936' 绝不通过（裸值分支 float 快路径，不触沙盒）。"""

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("裸值数量分支不应调用沙盒")

    monkeypatch.setattr(sympy_verifier, "verify_equation_equivalence", _boom)
    assert answers_equivalent("936", _P6_STD_TIME) is False
    assert answers_equivalent("936", "9小时36分") is False
    assert answers_equivalent("9小时36分", "936") is False
    assert answers_equivalent("576", _P6_STD_TIME) is True


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        # 纯数字表达式裸答案：沙盒数值等价（保留旧管线能力，仍不回落字符串比对）
        ("1152/2", _P6_STD_TIME, True),
        ("2^9", "512分", True),
        ("(576)", _P6_STD_TIME, True),
        ("936/1", _P6_STD_TIME, False),
    ],
)
def test_p6_numeric_expression_bare_answer(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        ("", _P6_STD_TIME, False),
        (_P6_STD_TIME, "", False),
        ("9小时36米", _P6_STD_TIME, False),  # 混合量纲非法
        ("x=576", _P6_STD_TIME, False),  # 含自由符号方程不享裸值匹配
    ],
)
def test_p6_empty_and_non_quantity(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9小时36分", (576.0, "time")),
        ("9小时36", (576.0, "time")),
        ("9时36分", (576.0, "time")),
        ("９小时３６分", (576.0, "time")),
        (r"9\ \text{小时}\ 36\ \text{分}", (576.0, "time")),
        ("2米30厘米", (2.3, "length")),
        ("1千克50克", (1050.0, "mass")),
        ("5元3角", (5.3, "money")),
        ("5元30分", (5.3, "money")),
        ("36分", (36.0, "time")),
        ("-5米", (-5.0, "length")),
        ("90min", (90.0, "time")),
        ("2km", (2000.0, "length")),
    ],
)
def test_p6_parse_quantity_values(text: str, expected: tuple[float, str]) -> None:
    parsed = sympy_verifier._parse_quantity(text)
    assert parsed is not None
    assert parsed[0] == pytest.approx(expected[0])
    assert parsed[1] == expected[1]


@pytest.mark.parametrize(
    "text",
    [
        "15*m",  # 代数乘法
        "m*v",
        "x=5",
        "20米/秒",  # 速率单位
        "米/秒",
        "936",  # 无单位裸数字不是数量
        "",
        "   ",
        "9小时36米",  # 混合量纲
        "1/2小时",  # 分数写法回落代数管线
        "9小时36分x",
        # 单字母拉丁单位与代数符号同形 → 绝不解析为数量（零漂移红线）
        "15m",
        "5g",
        "2t",
        "3h",
        "10s",
        # 后续分段出现负号（非合法复合量）
        "9小时-36分",
        "-9小时-36分",
    ],
)
def test_p6_parse_quantity_rejects_non_quantity(text: str) -> None:
    assert sympy_verifier._parse_quantity(text) is None


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        (r"x = \dfrac{1}{2}", r"x = \frac{1}{2}", True),
        (r"x = \tfrac{3}{4}", "x = 3/4", True),
        (r"x = \sqrt[3]{8}", "x = 2", True),
        (r"x = \frac12", "x = 0.5", True),
        (r"x = a \div b", "x = a/b", True),
        ("9小时-36分", _P6_STD_TIME, False),
    ],
)
def test_p8_latex_extended_and_negative_segment(
    user: str, standard: str, expected: bool
) -> None:
    assert answers_equivalent(user, standard) is expected


_CHICK_RABBIT_STD = r"\text{鸡 } 40 \text{ 只，兔 } 60 \text{ 只}"


@pytest.mark.parametrize(
    ("user", "standard", "expected"),
    [
        ("鸡40，兔60", _CHICK_RABBIT_STD, True),
        ("鸡 40 只，兔 60 只", _CHICK_RABBIT_STD, True),
        ("鸡有40只，兔有60只", _CHICK_RABBIT_STD, True),
        ("鸡：40，兔：60", _CHICK_RABBIT_STD, True),
        ("鸡=40，兔=60", _CHICK_RABBIT_STD, True),
        ("兔60，鸡40", _CHICK_RABBIT_STD, True),
        ("鸡40兔60", _CHICK_RABBIT_STD, True),
        ("40, 60", _CHICK_RABBIT_STD, True),
        ("40 60", _CHICK_RABBIT_STD, True),
        ("鸡60，兔40", _CHICK_RABBIT_STD, False),  # 实体数值对调必须拒绝
        ("60, 40", _CHICK_RABBIT_STD, False),  # 裸数字顺序错误必须拒绝
        ("鸡40，兔59", _CHICK_RABBIT_STD, False),
        ("鸡40", _CHICK_RABBIT_STD, False),  # 缺项必须拒绝
        ("x = 40, y = 60", _CHICK_RABBIT_STD, False),  # 未指明实体不得误放行
        ("甲 12 个", r"\text{甲 } 12 \text{ 个}", True),
        ("甲 15 个", r"\text{甲 } 12 \text{ 个}", False),
    ],
)
def test_p8_labeled_chinese_answers(user: str, standard: str, expected: bool) -> None:
    assert answers_equivalent(user, standard) is expected
