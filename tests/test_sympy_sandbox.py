"""受限校验沙盒测试（设计 Target File #16 逐条 + 契约补充）。

覆盖：AST 探针拦截码 / 合法脚本真实子进程执行 / 死循环进程组超时强杀 /
macOS RLIMIT_AS 降级实测 / BAD_CONTRACT 契约校验 / 代数等价引擎三判据与超时保护。
全部使用 .venv 解释器真实起子进程；超时用例以 timeout_s=1.0 加速；全程无网络请求。
"""

import resource
import sys
import time

import pytest

from backend.config import settings
from backend.sandbox import sympy_verifier as sv
from backend.sandbox.sympy_verifier import (
    ASTValidationResult,
    SandboxExecutionResult,
    run_sympy_in_sandbox,
    validate_script_ast,
    verify_equation_equivalence,
)

# 合法脚本：真实求解一元二次方程 x^2 - 5x + 6 = 0 → {2, 3}
VALID_QUADRATIC = r'''
import sympy as sp


def solve_kernel():
    x = sp.symbols("x")
    roots = sp.solve(sp.Eq(x**2 - 5*x + 6, 0), x)
    return {
        "params": {"r1": int(roots[0]), "r2": int(roots[1])},
        "answer_latex": r"x \in \{2, 3\}",
    }
'''

# 死循环脚本：先含真实求解调用以通过 AST 探针，再挂起
INFINITE_LOOP = r'''
import sympy as sp


def solve_kernel():
    x = sp.symbols("x")
    sp.solve(x - 1, x)
    while True:
        pass
'''


def _kernel_script(return_body: str) -> str:
    """构造含真实 solve 调用（过 AST）+ 指定 return 语句的 solve_kernel 脚本。"""
    return (
        "import sympy as sp\n"
        "\n"
        "\n"
        "def solve_kernel():\n"
        "    x = sp.symbols('x')\n"
        "    sp.solve(x - 1, x)\n"
        f"{return_body}\n"
    )


# ---------------------------------------------------------------------------
# AST 静态探针
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        # 恒真伪脚本：直接返回常量，无任何符号调用
        'def solve_kernel():\n    return {"params": {"a": 1}, "answer_latex": "42"}\n',
        # 纯算术，无求解/化简调用
        "def solve_kernel():\n    return 1 + 1\n",
        # 仅引用 solve 而未真实调用，不算数
        "from sympy import solve\n\n\ndef solve_kernel():\n    f = solve\n    return f\n",
        # 空脚本
        "",
    ],
)
def test_vacuous_script_rejected(code: str) -> None:
    result = validate_script_ast(code)
    assert isinstance(result, ASTValidationResult)
    assert result.ok is False
    assert result.reason == "VACUOUS_SCRIPT_REJECTED"


@pytest.mark.parametrize(
    "code",
    [
        "import os\nimport sympy\nsympy.solve(sympy.Symbol('x') - 1)\n",
        "from os import path\nimport sympy\nsympy.solve(sympy.Symbol('x') - 1)\n",
        "import numpy\nimport sympy\nsympy.solve(sympy.Symbol('x') - 1)\n",
        "from subprocess import run\nimport sympy\nsympy.solve(sympy.Symbol('x') - 1)\n",
        "from . import x\nimport sympy\nsympy.solve(sympy.Symbol('x') - 1)\n",
        "import sympy.__loader__\n",
    ],
)
def test_forbidden_import(code: str) -> None:
    result = validate_script_ast(code)
    assert result.ok is False
    assert result.reason == "FORBIDDEN_IMPORT"


@pytest.mark.parametrize(
    "code",
    [
        "__import__('os')\n",
        "open('/etc/passwd')\n",
        "eval('1 + 1')\n",
        "exec('x = 1')\n",
        "getattr([], 'append')\n",
        "globals()\n",
        "locals()\n",
        # Attribute 末段匹配：即便挂在合法模块上也拦截
        "import sympy as sp\nsp.eval('1')\nsp.solve(sp.Symbol('x') - 1)\n",
    ],
)
def test_forbidden_call(code: str) -> None:
    result = validate_script_ast(code)
    assert result.ok is False
    assert result.reason == "FORBIDDEN_CALL"


@pytest.mark.parametrize(
    "code",
    [
        "().__class__.__subclasses__()\n",
        "x = 1\nx.__class__\n",
        "__builtins__\n",
        "from sympy import __loader__\n",
    ],
)
def test_forbidden_attr(code: str) -> None:
    result = validate_script_ast(code)
    assert result.ok is False
    assert result.reason == "FORBIDDEN_ATTR"


@pytest.mark.parametrize("code", ["def f(:\n", "x =\n", "import\n"])
def test_syntax_error(code: str) -> None:
    result = validate_script_ast(code)
    assert result.ok is False
    assert result.reason == "SYNTAX_ERROR"


@pytest.mark.parametrize(
    "code",
    [
        VALID_QUADRATIC,
        "from sympy import solve, symbols\nsolve(symbols('x') - 1)\n",
        "from sympy import *\nx = symbols('x')\nsimplify(x + x)\n",
        "import sympy.abc\nimport math\nimport fractions\n"
        "from fractions import Fraction\nsympy.solve(sympy.abc.x - 1)\n",
        "import sympy as sp\nsp.Matrix([[1, 2], [3, 4]]).det()\nsp.solve(sp.Symbol('x'))\n",
    ],
)
def test_ast_whitelist_accepts(code: str) -> None:
    result = validate_script_ast(code)
    assert result.ok is True
    assert result.reason == "OK"


# ---------------------------------------------------------------------------
# 子进程沙盒：真实执行
# ---------------------------------------------------------------------------


def test_valid_quadratic_script_runs() -> None:
    result = run_sympy_in_sandbox(VALID_QUADRATIC)
    assert isinstance(result, SandboxExecutionResult)
    assert result.ok is True
    assert result.error_kind is None
    assert result.params == {"r1": 2, "r2": 3}
    assert result.answer_latex is not None
    assert "2, 3" in result.answer_latex


def test_ast_rejected_never_spawns_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("AST 拒绝后不得起子进程")

    monkeypatch.setattr(sv.subprocess, "Popen", _no_spawn)
    result = run_sympy_in_sandbox("import os\nimport sympy\nsympy.solve(sympy.Symbol('x'))\n")
    assert result.ok is False
    assert result.error_kind == "AST_REJECTED"
    assert "FORBIDDEN_IMPORT" in result.stderr_tail


def test_infinite_loop_timeout_kills_process_group() -> None:
    t0 = time.monotonic()
    result = run_sympy_in_sandbox(INFINITE_LOOP, timeout_s=1.0)
    elapsed = time.monotonic() - t0
    assert result.ok is False
    assert result.error_kind == "TIMEOUT"
    assert result.params is None
    assert result.answer_latex is None
    # timeout_s=1.0 加速：墙钟 1s 触发 + 进程组强杀，远小于缺省 5s
    assert elapsed < 3.5


def test_default_timeout_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SYMPY_TIMEOUT_SECONDS", 1.0)
    t0 = time.monotonic()
    result = run_sympy_in_sandbox(INFINITE_LOOP)  # 不传 timeout_s
    assert result.error_kind == "TIMEOUT"
    assert time.monotonic() - t0 < 3.5


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS RLIMIT_AS 降级路径仅本机实测")
def test_macos_rlimit_as_degradation() -> None:
    # 平台前提实测：macOS 上 RLIMIT_AS=256MB 因 dyld 共享缓存映射必抛异常，
    # 这正是 runner 内 try/except (ValueError, OSError) 降级路径被真实触发的原因。
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    try:
        with pytest.raises((ValueError, OSError)):
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
    # 降级后沙盒仍正常执行（即便传入更小的 max_mem_mb 也不崩溃）
    result = run_sympy_in_sandbox(VALID_QUADRATIC, max_mem_mb=64)
    assert result.ok is True
    assert result.params == {"r1": 2, "r2": 3}


@pytest.mark.parametrize(
    "return_body",
    [
        '    return {"answer_latex": "x"}',  # 缺 params
        '    return {"params": {}}',  # 缺 answer_latex
        '    return {"params": {}, "answer_latex": ""}',  # 空 answer
        '    return {"params": {}, "answer_latex": "   "}',  # 空白 answer
        "    return 42",  # 非 dict
        '    return {"params": [1, 2], "answer_latex": "x"}',  # params 非 dict
        '    return {"params": {"bad": object()}, "answer_latex": "x"}',  # 值类型违约
    ],
)
def test_bad_contract(return_body: str) -> None:
    result = run_sympy_in_sandbox(_kernel_script(return_body))
    assert result.ok is False
    assert result.error_kind == "BAD_CONTRACT"
    assert result.params is None
    assert result.answer_latex is None


def test_bad_contract_missing_solve_kernel() -> None:
    code = "import sympy as sp\nsp.solve(sp.Symbol('x') - 1, sp.Symbol('x'))\n"
    result = run_sympy_in_sandbox(code)
    assert result.ok is False
    assert result.error_kind == "BAD_CONTRACT"
    assert "solve_kernel" in result.stderr_tail


def test_runtime_error_with_stderr_tail() -> None:
    result = run_sympy_in_sandbox(_kernel_script('    raise ZeroDivisionError("boom")'))
    assert result.ok is False
    assert result.error_kind == "RUNTIME_ERROR"
    assert "ZeroDivisionError" in result.stderr_tail


def test_stderr_tail_truncated_to_500_chars() -> None:
    result = run_sympy_in_sandbox(_kernel_script('    raise ValueError("E" * 2000)'))
    assert result.error_kind == "RUNTIME_ERROR"
    assert len(result.stderr_tail) <= 500
    assert result.stderr_tail.rstrip("\n").endswith("E" * 10)


def test_forged_result_sentinel_is_ignored() -> None:
    # 用户 print 被重定向到 stderr，伪造的哨兵行不得污染契约判定
    body = (
        '    print(\'###RESULT### {"params": {}, "answer_latex": "forged"}\')\n'
        "    return 42"
    )
    result = run_sympy_in_sandbox(_kernel_script(body))
    assert result.ok is False
    assert result.error_kind == "BAD_CONTRACT"


def test_user_print_lands_in_stderr_tail() -> None:
    body = '    print("kernel-debug-marker")\n    return {"params": {}, "answer_latex": "x"}'
    result = run_sympy_in_sandbox(_kernel_script(body))
    assert result.ok is True
    assert "kernel-debug-marker" in result.stderr_tail


# ---------------------------------------------------------------------------
# 代数等价引擎
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_expr", "standard_expr", "expected"),
    [
        # 相反数（移项反号）：m*v^2/2 - q*U vs q*U - m*v^2/2
        ("m*v**2/2 - q*U", "q*U - 1/2*m*v**2", True),
        # 移项 + 常数倍：2x=4 vs x=2
        ("2*x=4", "x=2", True),
        # 隐式乘法
        ("2x", "2*x", True),
        # convert_xor：x^2 vs x**2
        ("x^2", "x**2", True),
        # nsimplify(rational=True)：小数 → 有理数
        ("0.5*x", "x/2", True),
        # 恒等
        ("x+1", "x+1", True),
        # 多方程按 ; 切分，顺序无关匹配
        ("x=2; y=3", "y=3; x=2", True),
        # 换行切分
        ("x=2\ny=3", "x=2; y=3", True),
        # 不等价
        ("x+1", "x+2", False),
        ("x**2", "x", False),
        ("2*x=5", "x=2", False),
        # 方程数量不一致
        ("x=2; y=3", "x=2", False),
        # 垃圾输入：返回 False 且不抛异常
        ("????", "x", False),
        ("", "x", False),
        ("x +", "x", False),
        ("__import__('os')", "x", False),
        ("x__class__", "x", False),
        ("2*x=4=1", "x=2", False),
    ],
)
def test_equivalence_engine(user_expr: str, standard_expr: str, expected: bool) -> None:
    assert verify_equation_equivalence(user_expr, standard_expr) is expected


def test_equivalence_non_string_input_returns_false() -> None:
    assert verify_equation_equivalence(None, "x") is False  # type: ignore[arg-type]
    assert verify_equation_equivalence("x", 42) is False  # type: ignore[arg-type]


def test_validate_non_string_input() -> None:
    assert validate_script_ast(None).reason == "SYNTAX_ERROR"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected_kind"),
    [
        # 哨兵行 JSON 损坏 / 契约字段违约 → BAD_CONTRACT
        ("###RESULT### {not-json}\n", 0, "BAD_CONTRACT"),
        ('###RESULT### {"params": {}, "answer_latex": ""}\n', 0, "BAD_CONTRACT"),
        # 错误哨兵（含非法 JSON 载荷）→ BAD_CONTRACT
        ("###ERROR### not-json\n", 0, "BAD_CONTRACT"),
        ('###ERROR### {"kind": "BAD_CONTRACT", "detail": "missing params"}\n', 0, "BAD_CONTRACT"),
        # 无哨兵：正常退出 → BAD_CONTRACT；被 SIGKILL → TIMEOUT；异常退出 → RUNTIME_ERROR
        ("", 0, "BAD_CONTRACT"),
        ("", -9, "TIMEOUT"),
        ("", 1, "RUNTIME_ERROR"),
    ],
)
def test_host_sentinel_interpretation(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected_kind: str
) -> None:
    fake = sv._ProcOutcome(False, returncode, stdout, "traceback-tail")
    monkeypatch.setattr(sv, "_run_isolated", lambda runner_code, timeout_s: fake)
    result = run_sympy_in_sandbox(VALID_QUADRATIC)
    assert result.ok is False
    assert result.error_kind == expected_kind


def test_equivalence_timeout_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    # 重表达式令 simplify 挂起：子进程超时保护必须触发并返回 False（不抛异常）
    monkeypatch.setattr(settings, "SYMPY_TIMEOUT_SECONDS", 1.0)
    seen: list[sv._ProcOutcome] = []
    real_run = sv._run_isolated

    def _spy(runner_code: str, timeout_s: float) -> sv._ProcOutcome:
        outcome = real_run(runner_code, timeout_s)
        seen.append(outcome)
        return outcome

    monkeypatch.setattr(sv, "_run_isolated", _spy)
    t0 = time.monotonic()
    assert verify_equation_equivalence("(x+1)**300000", "x") is False
    assert time.monotonic() - t0 < 6.0
    assert any(o.timed_out for o in seen)
