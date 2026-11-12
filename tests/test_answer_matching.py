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
