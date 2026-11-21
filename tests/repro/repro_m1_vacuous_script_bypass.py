"""Repro (M1 Round 1 / MEDIUM): 反空壳探针可被 for/while 循环或字面量下标绕过。

契约 §1.3-5：verify() 返回值不得是"无计算的纯字面量常量"。当前实现：
  - 循环体内任何赋值一律标记 is_computed（in_loop=True），`for _ in range(1): ans = 42` 即放行；
  - Subscript 一律视为 computed，`[42][0]` 即放行。
二者返回的仍是硬编码常量 42，与 `{"主问": 42}` 等价，却通过 AST 探针并 PASSED → 可伪造 SymPy 验算。
期望：全部 VACUOUS_SCRIPT_REJECTED。当前 ok=True → exit 1。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shuati.verify.sympy_sandbox import run_verify_script, validate_script_ast  # noqa: E402

CASES = {
    "for_loop_const": "def verify():\n    for _ in range(1):\n        ans = 42\n    return {'主问': ans}\n",
    "while_const": "def verify():\n    while True:\n        ans = 42\n        break\n    return {'主问': ans}\n",
    "subscript_const": "def verify():\n    return {'主问': [42][0]}\n",
}
bad = []
for name, code in CASES.items():
    r = validate_script_ast(code)
    run = run_verify_script(code)
    print(f"[{name}] ast={r} run={run.status} values={run.values}")
    if r.ok:
        bad.append(name)
if bad:
    print(f"FAIL: 常量空壳脚本被放行: {bad}")
    sys.exit(1)
print("OK")
