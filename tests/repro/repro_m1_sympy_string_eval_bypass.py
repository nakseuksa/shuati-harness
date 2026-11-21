"""Repro (M1 Round 1 / CRITICAL): sympy_sandbox AST 探针可被 sympy 内部的字符串求值入口绕过。

契约 §1.3-3 要求拦截字符串求值 API。当前仅按名字拦截 sympify/parse_expr/lambdify/S(<str>)，
但 `from sympy import simplify; simplify("<str>")`（simplify/expand/solve/N 等均内部 sympify(str)）
与 `sympy.parsing.sympy_parser.eval_expr("<str>", {}, {})` 均可通过 AST 探针，并在子进程中
以真实 builtins 执行任意 Python（此处仅调用 os.getcwd()/os.getpid() 作无害证明）。

期望：以下脚本全部应 status == "AST_REJECTED"。当前：至少有一个 PASSED 或 FAILED（代码已被执行）→ exit 1。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shuati.verify.sympy_sandbox import run_verify_script, validate_script_ast  # noqa: E402

CASES = {
    "eval_expr": (
        "from sympy.parsing.sympy_parser import eval_expr\n"
        "def verify():\n"
        "    return {'主问': str(eval_expr('__import__(\"os\").getpid()', {}, {}))}\n"
    ),
    "simplify_str": (
        "from sympy import simplify\n"
        "def verify():\n"
        "    return {'主问': str(simplify('__import__(\"os\").getpid()'))}\n"
    ),
    "sympy_Add_str": (
        "import sympy\n"
        "def verify():\n"
        "    return {'主问': str(sympy.expand('__import__(\"os\").getpid()'))}\n"
    ),
}

bad = []
for name, code in CASES.items():
    ast_res = validate_script_ast(code)
    run_res = run_verify_script(code)
    print(f"[{name}] ast={ast_res} run.status={run_res.status} values={run_res.values} tail={run_res.stderr_tail[-120:]!r}")
    if ast_res.ok:
        bad.append(name)

if bad:
    print(f"FAIL: AST 探针放行了字符串求值入口: {bad}")
    sys.exit(1)
print("OK")
