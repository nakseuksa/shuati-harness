"""Repro (M1 Round 2 / CRITICAL): V1 修复不完整 —— `sympy.external.import_module(<str>)` 可取得 os/subprocess 并执行任意代码。

Round 1 仅堵 sympify/parse_expr/eval_expr/stringify_expr 等 sympy 字符串求值入口（并在子进程内打补丁），
但 `sympy.external.import_module(name)` 内部直接调用真实 builtins.__import__(name)，返回任意模块。
AST 污点探针只检查 Call 实参是否为 str 常量 / JoinedStr / 已被污点标记的 Name，
因此将字符串放入下标 `["os"][0]` 或经用户函数形参透传即可绕过；`.getpid/.getcwd/.listdir/.remove` 等
属性均不在 `_FORBIDDEN_ATTR_NAMES`（甚至 `import_module(["subprocess"][0]).run` 亦可）。
此处仅以无害的 os.getpid()/os.getcwd() 作证明。
期望：run_verify_script 对下列脚本绝不返回 PASSED（应 AST_REJECTED）。当前 PASSED → exit 1。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shuati.verify.sympy_sandbox import run_verify_script, validate_script_ast  # noqa: E402

CASES = {
    "import_module_subscript": (
        "from sympy.external import import_module\n"
        "def verify():\n"
        "    return {'主问': str(import_module(['os'][0]).getpid())}\n"
    ),
    "import_module_user_fn_param": (
        "from sympy.external import import_module\n"
        "def load(name):\n"
        "    return import_module(name)\n"
        "def verify():\n"
        "    return {'主问': str(load('os').getcwd())}\n"
    ),
    "import_module_attr_chain": (
        "import sympy\n"
        "def verify():\n"
        "    m = sympy.external.import_module(['subprocess'][0])\n"
        "    return {'主问': str(m.run(['cmd', '/c', 'echo', '1'], capture_output=True).returncode)}\n"
    ),
}

bad = []
for name, code in CASES.items():
    ast_res = validate_script_ast(code)
    run_res = run_verify_script(code)
    print(f"[{name}] ast.ok={ast_res.ok}({ast_res.reason}) run.status={run_res.status} values={run_res.values} tail={run_res.stderr_tail[-100:]!r}")
    if run_res.status == "PASSED":
        bad.append(name)

if bad:
    print(f"FAIL: 沙盒执行了越权模块加载并 PASSED: {bad}")
    sys.exit(1)
print("OK")
