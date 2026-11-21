"""M1 Round 1 对抗审查回归集（固化 V1~V4 Repro 断言，防退化）。"""

from __future__ import annotations

import pytest

from shuati.models.domain import AnswerPart
from shuati.verify.equivalence import compare_answer_parts, latex_to_plain
from shuati.verify.sympy_sandbox import run_verify_script, validate_script_ast


def test_v1_sympy_string_eval_bypass_blocked() -> None:
    """V1: AST 探针与沙盒必须拦截 eval_expr / simplify("<str>") / expand(str_var)。"""
    cases = [
        (
            "from sympy.parsing.sympy_parser import eval_expr\n"
            "def verify():\n"
            "    return {'主问': str(eval_expr('__import__(\"os\").getpid()', {}, {}))}\n"
        ),
        (
            "from sympy import simplify\n"
            "def verify():\n"
            "    return {'主问': str(simplify('__import__(\"os\").getpid()'))}\n"
        ),
        (
            "import sympy\n"
            "def verify():\n"
            "    return {'主问': str(sympy.expand('__import__(\"os\").getpid()'))}\n"
        ),
        (
            "from sympy import simplify\n"
            "def verify():\n"
            "    expr_s = '__import__(\"os\").getpid()'\n"
            "    return {'主问': str(simplify(expr_s))}\n"
        ),
        (
            "from sympy.external import import_module\n"
            "def verify():\n"
            "    return {'主问': str(import_module(['os'][0]).getpid())}\n"
        ),
        (
            "from sympy.external import import_module\n"
            "def load(name):\n"
            "    return import_module(name)\n"
            "def verify():\n"
            "    return {'主问': str(load('os').getcwd())}\n"
        ),
        (
            "import sympy\n"
            "def verify():\n"
            "    m = sympy.external.import_module(['subprocess'][0])\n"
            "    return {'主问': str(m.run(['cmd', '/c', 'echo', '1'], capture_output=True).returncode)}\n"
        ),
    ]
    for code in cases:
        ast_res = validate_script_ast(code)
        assert not ast_res.ok
        run_res = run_verify_script(code)
        assert run_res.status == "AST_REJECTED"


@pytest.mark.asyncio
async def test_v2_set_brace_preserved_in_finite_sets() -> None:
    """V2: latex_to_plain 保留有限集花括号，不与开区间混淆。"""
    def _part(txt: str) -> AnswerPart:
        return AnswerPart(
            label="主问",
            answer_type="SET_OR_INTERVAL",
            verify_mode="SYMBOLIC",
            collection_mode="SINGLE",
            answer=txt,
            sympy_expr=txt,
        )

    s1 = await compare_answer_parts(
        [_part(r"\{1,2,3\}")],
        [_part(r"\{3,2,1\}")],
        grade_subject="高中数学",
        allow_pi_314_subjects=[],
    )
    assert s1.all_dual_equal

    s2 = await compare_answer_parts(
        [_part(r"\{1,2,3\}")],
        [_part(r"\{1,2,3\}")],
        grade_subject="高中数学",
        allow_pi_314_subjects=[],
    )
    assert s2.all_dual_equal

    s3 = await compare_answer_parts(
        [_part(r"\{1,2\}")],
        [_part("(1,2)")],
        grade_subject="高中数学",
        allow_pi_314_subjects=[],
    )
    assert not s3.all_dual_equal


@pytest.mark.asyncio
async def test_v3_inequality_geq_spacing_and_relational_expr() -> None:
    """V3: >=/<= 不被拆成 > =/< =，且 EXPRESSION 模式下相同不等式判等。"""
    assert ">=" in latex_to_plain(r"x\geq 3")
    assert "> =" not in latex_to_plain(r"x\geq 3")

    def _part(txt: str, atype: str) -> AnswerPart:
        return AnswerPart(
            label="主问",
            answer_type=atype,  # type: ignore[arg-type]
            verify_mode="SYMBOLIC",
            collection_mode="SINGLE",
            answer=txt,
            sympy_expr=txt,
        )

    for a, b, atype in [
        (r"x\geq 3", r"x\geq 3", "SET_OR_INTERVAL"),
        (r"x\ge 3", "x>=3", "SET_OR_INTERVAL"),
        (r"x\geq 3 \cup x\leq -1", r"x\leq -1 \cup x\geq 3", "SET_OR_INTERVAL"),
        ("x<3", "x<3", "EXPRESSION"),
    ]:
        summary = await compare_answer_parts(
            [_part(a, atype)],
            [_part(b, atype)],
            grade_subject="高中数学",
            allow_pi_314_subjects=[],
        )
        assert summary.all_dual_equal, f"{a!r} vs {b!r} ({atype}) should be equal"


def test_v4_vacuous_loop_and_subscript_const_rejected() -> None:
    """V4: 反空壳探针必须拦截循环内常量赋值与常量下标绕过。"""
    vacuous_cases = [
        "def verify():\n    for _ in range(1):\n        ans = 42\n    return {'主问': ans}\n",
        "def verify():\n    while True:\n        ans = 42\n        break\n    return {'主问': ans}\n",
        "def verify():\n    return {'主问': [42][0]}\n",
        "def verify():\n    d = {'a': 42}\n    return {'主问': d['a']}\n",
    ]
    for code in vacuous_cases:
        ast_res = validate_script_ast(code)
        assert not ast_res.ok
        assert ast_res.reason == "VACUOUS_SCRIPT_REJECTED"
        run_res = run_verify_script(code)
        assert run_res.status == "AST_REJECTED"


def test_v5_sympy_internal_gmpy_allowed_and_user_external_blocked() -> None:
    """真机压测 Bug1: SymPy 内部加减/solve 延迟导入 sympy.external.gmpy 放行，用户直接导入仍硬拦截。"""
    internal_gmpy_script = (
        "from sympy import Symbol, Eq, solve\n"
        "def verify():\n"
        "    x = Symbol('x', positive=True)\n"
        "    y = Symbol('y', positive=True)\n"
        "    sols = solve([Eq(x + y, 10), Eq(x**2 + y**2, 58)], [x, y])\n"
        "    return {'主问': str(sols[0][0] + sols[0][1] - 2)}\n"
    )
    res = run_verify_script(internal_gmpy_script, timeout_seconds=10.0)
    assert res.ok, res.stderr_tail
    assert res.status == "PASSED"
    assert res.values == {"主问": "8"}

    blocked_user_scripts = [
        "import sympy.external.gmpy\ndef verify():\n    return {'主问': str(1 + 2)}\n",
        "from sympy.external.gmpy import gcd\ndef verify():\n    return {'主问': str(gcd(12, 18))}\n",
        "from sympy.external.pythonmpq import PythonMPQ\ndef verify():\n    return {'主问': str(PythonMPQ(3, 4))}\n",
        "from sympy.external.importtools import import_module\ndef verify():\n    return {'主问': str(import_module('os').getpid())}\n",
    ]
    for code in blocked_user_scripts:
        ast_res = validate_script_ast(code)
        assert not ast_res.ok
        assert ast_res.reason == "FORBIDDEN_IMPORT"
        run_res = run_verify_script(code, timeout_seconds=10.0)
        assert run_res.status == "AST_REJECTED"


def test_v6_ch28_007_defensive_ifexp_and_anti_tautology_redlines() -> None:
    """真机压测 Bug2: 防御性无解兜底三元表达式放行，常量/数值兜底/空壳红线全拦截。"""
    ch28_007_script = (
        "from sympy import Symbol, Eq, solve\n"
        "def verify():\n"
        "    a = Symbol('a', positive=True)\n"
        "    valid_area = solve(Eq(a**2 - 16, 0), a)\n"
        "    return {'主问': str(valid_area[0]) if valid_area else '无解'}\n"
    )
    res = run_verify_script(ch28_007_script, timeout_seconds=10.0)
    assert res.ok, res.stderr_tail
    assert res.status == "PASSED"
    assert res.values == {"主问": "4"}

    empty_sol_script = (
        "from sympy import Symbol, Eq, solve\n"
        "def verify():\n"
        "    a = Symbol('a', positive=True)\n"
        "    valid_area = solve(Eq(a + 5, 0), a)\n"
        "    return {'主问': str(valid_area[0]) if valid_area else '无解'}\n"
    )
    empty_res = run_verify_script(empty_sol_script, timeout_seconds=10.0)
    assert empty_res.ok, empty_res.stderr_tail
    assert empty_res.values == {"主问": "无解"}

    vacuous_redlines = [
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': 42 if True else x + 1}\n",
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': 42 if x > 0 else x + 1}\n",
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': 3.14 if x > 0 else x + 1}\n",
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': '42' if x > 0 else str(x + 1)}\n",
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': '119/2' if x > 0 else str(x + 1)}\n",
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': str(x + 1) if False else '无解'}\n",
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': str(x + 1) if 1 < 2 else '无解'}\n",
        "def verify():\n    AB = 300\n    return {'主问': str(AB)}\n",
        "def verify():\n    x = 5\n    y = x * 2\n    return {'主问': 'A' if y > 3 else str(y)}\n",
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': '无解' if not x else str(x + 1)}\n",
        "from sympy import Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': str(x + 1) if x > 0 else 'A'}\n",
    ]
    for code in vacuous_redlines:
        ast_res = validate_script_ast(code)
        assert not ast_res.ok, f"应当被反空壳拦截: {code}"
        assert ast_res.reason == "VACUOUS_SCRIPT_REJECTED"
        run_res = run_verify_script(code, timeout_seconds=10.0)
        assert run_res.status == "AST_REJECTED"
