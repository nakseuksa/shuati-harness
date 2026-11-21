"""Repro: IfExp 非数值哨兵对称放行，允许“主分支硬编码 'A'/'sqrt' 等字面量 + 计算条件 + 计算 else”偷渡硬编码答案。

期望（反空壳红线）：主结果分支（body）为非数值字面量、仅 else 为计算表达式的脚本，
其返回值恒为硬编码字面量，应被 VACUOUS_SCRIPT_REJECTED。当前补丁放行 => 本脚本 Exit != 0。
"""

from __future__ import annotations

import sys

from shuati.verify.sympy_sandbox import validate_script_ast

SCRIPT = """
def verify():
    x = 5
    y = x * 2
    return {'主问': 'A' if y > 3 else str(y)}
"""


def main() -> int:
    res = validate_script_ast(SCRIPT)
    if res.ok:
        print("FAIL: hardcoded 'A' (always-taken body branch) accepted as computed; reason=", res.reason)
        return 1
    print("OK rejected:", res.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
