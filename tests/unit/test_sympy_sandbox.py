"""SymPy 沙盒单元测试：AST 白名单/反空壳探针、Windows Job Object/超时强杀、隔离子进程等价判定。"""

from __future__ import annotations

import pytest

from shuati.verify.sympy_sandbox import (
    _run_isolated,
    run_symbolic_check_in_sandbox,
    run_verify_script,
    validate_script_ast,
)


def test_ast_probe_blocks_dangerous_apis_and_allows_safe_math() -> None:
    forbidden_scripts = [
        ("FORBIDDEN_IMPORT", "import os\ndef verify():\n    return {'主问': str(1+2)}\n"),
        ("FORBIDDEN_IMPORT", "from sympy import __loader__\ndef verify():\n    return {'主问': str(1+2)}\n"),
        ("FORBIDDEN_IMPORT", "from . import foo\ndef verify():\n    return {'主问': str(1+2)}\n"),
        ("FORBIDDEN_CALL", "from sympy import sympify\ndef verify():\n    return {'主问': str(sympify('1+2'))}\n"),
        ("FORBIDDEN_IMPORT", "from sympy.parsing.sympy_parser import parse_expr\ndef verify():\n    return {'主问': str(parse_expr('1+2'))}\n"),
        ("FORBIDDEN_CALL", "from sympy import lambdify, Symbol\ndef verify():\n    x = Symbol('x')\n    return {'主问': str(lambdify(x, x+1)(2))}\n"),
        ("FORBIDDEN_CALL", "from sympy import S\ndef verify():\n    return {'主问': str(S('1+2'))}\n"),
        ("FORBIDDEN_CALL", "from sympy import S\ndef verify():\n    t = '3'\n    return {'主问': str(S(t))}\n"),
        ("FORBIDDEN_CALL", "def verify():\n    return {'主问': str(eval('1+2'))}\n"),
        ("FORBIDDEN_CALL", "def verify():\n    open('x', 'w')\n    return {'主问': str(1+2)}\n"),
        ("FORBIDDEN_CALL", "def verify():\n    return {'主问': str(__import__('os'))}\n"),
        ("FORBIDDEN_ATTR", "def verify():\n    return {'主问': str((1).__class__)}\n"),
    ]
    for expected_reason, script in forbidden_scripts:
        res = validate_script_ast(script)
        assert not res.ok, f"应当拦截脚本: {script}"
        assert res.reason == expected_reason

    # 放行 S.Half / S(3) 以及算术/数论/几何函数
    safe_scripts = [
        """
from sympy import S, Rational
def verify():
    ans = S(3) * S.Half + Rational(1, 2)
    return {"主问": str(ans)}
""",
        """
import math
from sympy import factorint, divisor_count, gcd
def verify():
    n = 360
    return {"(1)": str(divisor_count(n)), "(2)": str(gcd(24, 36))}
""",
        """
from sympy import Polygon, Point, Triangle
def verify():
    tri = Triangle(Point(0, 0), Point(4, 0), Point(0, 3))
    return {"主问": str(tri.area)}
""",
    ]
    for script in safe_scripts:
        res = validate_script_ast(script)
        assert res.ok, f"应当放行合法数学脚本，实际被拒: {res.reason}\n{script}"


def test_ast_anti_tautology_rejects_vacuous_and_allows_computed() -> None:
    vacuous_scripts = [
        "x = 1 + 2\n",  # 无 def verify
        "def verify():\n    pass\n",  # 无 return
        "def verify():\n    return {}\n",  # 空字典
        'def verify():\n    return {"主问": "42"}\n',
        'def verify():\n    return {"主问": 42}\n',
        'def verify():\n    return {"主问": -5}\n',
        'def verify():\n    ans = "42"\n    return {"主问": ans}\n',
        'def verify():\n    return {"主问": str(42)}\n',
        'def verify():\n    return {"主问": ["12", "24"]}\n',
        'def verify():\n    return {"(1)": str(3 + 4), "(2)": "10"}\n',  # 其中一项为空壳常量
    ]
    for script in vacuous_scripts:
        res = validate_script_ast(script)
        assert not res.ok, f"空壳脚本应当被拒: {script}"
        assert res.reason == "VACUOUS_SCRIPT_REJECTED"

    computed_scripts = [
        'def verify():\n    return {"主问": str(6 * 7)}\n',
        """
def verify():
    total = 0
    for i in range(1, 11):
        total += i
    return {"主问": str(total)}
""",
        """
from sympy import Eq, Symbol, solve
def verify():
    x = Symbol('x')
    sols = solve(Eq(x**2 - 5*x + 6, 0), x)
    return {"主问": [str(s) for s in sols]}
""",
        """
def verify():
    d = {}
    d["(1)"] = str(2**5)
    return d
""",
    ]
    for script in computed_scripts:
        res = validate_script_ast(script)
        assert res.ok, f"真实计算脚本应当放行: {res.reason}"


def test_run_verify_script_arithmetic_number_theory_geometry() -> None:
    script = """
import math
from sympy import Rational, Polygon, Point, divisor_count

def verify():
    print("###RESULT### {'fake': '123'}")
    p = Polygon(Point(0, 0), Point(6, 0), Point(6, 4), Point(0, 4))
    area = p.area
    cnt = divisor_count(12)
    frac = Rational(3, 4) + Rational(1, 2)
    return {
        "(1)": str(area),
        "(2)": str(cnt),
        "(3)": [str(frac), str(math.gcd(18, 24))],
    }
"""
    res = run_verify_script(script, timeout_seconds=10.0)
    assert res.ok
    assert res.status == "PASSED"
    assert res.values == {"(1)": "24", "(2)": "6", "(3)": ["5/4", "6"]}
    # 用户 print 被重定向到 stderr，不能伪造 ###RESULT###
    assert "fake" in res.stderr_tail


def test_run_verify_script_timeout_kills_process() -> None:
    loop_script = """
def verify():
    i = 0
    while True:
        i += 1
    return {"主问": str(i)}
"""
    res = run_verify_script(loop_script, timeout_seconds=0.8, memory_limit_mb=256)
    assert not res.ok
    assert res.status == "TIMEOUT"
    assert res.values is None


def test_isolated_process_tree_kill_on_timeout_and_memory_limit() -> None:
    # 1. 测试子进程树派生孙进程时超时强杀不遗留进程
    tree_runner = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(30)
"""
    outcome = _run_isolated(tree_runner, timeout_seconds=0.6, memory_limit_mb=256)
    assert outcome.timed_out

    # 2. 测试内存限制触发失败（Windows Job Object / Linux RLIMIT_AS）
    mem_script = """
def verify():
    buf = []
    for i in range(100):
        buf.append(bytearray(10 * 1024 * 1024))
    return {"主问": str(len(buf))}
"""
    res = run_verify_script(mem_script, timeout_seconds=5.0, memory_limit_mb=96)
    assert not res.ok
    assert res.status in ("FAILED", "TIMEOUT")


@pytest.mark.parametrize(
    ("expr_a", "expr_b", "mode", "allow_pi", "expected"),
    [
        ("3/4", "0.75", "EXPRESSION", False, True),
        ("sqrt(8)", "2*sqrt(2)", "EXPRESSION", False, True),
        ("5", "-5", "EXPRESSION", False, False),
        ("5", "10", "EXPRESSION", False, False),
        ("2*x + 1", "-(2*x + 1)", "EXPRESSION", False, False),
        ("4*pi", "12.56", "EXPRESSION", True, True),
        ("4*pi", "12.56", "EXPRESSION", False, False),
        ("4*pi", "12.57", "EXPRESSION", True, False),
        ("9:4", "18:8", "RATIO", False, True),
        ("9：4", "9/4", "RATIO", False, True),
        ("1:2:3", "2:4:6", "RATIO", False, True),
        ("9:4", "4:9", "RATIO", False, False),
        ("1:2", "-1:2", "RATIO", False, False),
        ("(1/2):(1/3)", "3:2", "RATIO", False, True),
        ("(1, 3]", "1 < x <= 3", "SET_OR_INTERVAL", False, True),
        ("(1, 3)", "[1, 3]", "SET_OR_INTERVAL", False, False),
        ("{1, 2}", "x^2 - 3*x + 2 = 0", "SET_OR_INTERVAL", False, True),
        ("(-oo, 1) U (2, oo)", "Union(Interval(-oo, 1, right_open=True), Interval(2, oo, left_open=True))", "SET_OR_INTERVAL", False, True),
    ],
)
def test_symbolic_check_in_sandbox(
    expr_a: str,
    expr_b: str,
    mode: str,
    allow_pi: bool,
    expected: bool,
) -> None:
    eq, err = run_symbolic_check_in_sandbox(
        expr_a,
        expr_b,
        mode=mode,  # type: ignore[arg-type]
        allow_pi_314=allow_pi,
        timeout_seconds=10.0,
    )
    assert err is None
    assert eq is expected


def test_symbolic_check_rejects_unsafe_expression() -> None:
    eq, err = run_symbolic_check_in_sandbox("__import__('os').system('echo 1')", "1")
    assert not eq
    assert err == "UNSAFE_EXPRESSION"
