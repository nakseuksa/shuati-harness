"""Repro (M1 Round 1 / HIGH): latex_to_plain 末尾 `re.sub(r"\\s*=\\s*", " = ")` 把 `>=`/`<=` 拆成 `> =`/`< =`。

后果：含 ≥/≤ 的不等式答案（SET_OR_INTERVAL）无法被 `_parse_single_set_token` 解析（split 不到 `>=`），
逐字相同的 `x\\geq 3` vs `x\\geq 3` 也被判不等 → 大量合法 ≥/≤ 解集题必然 ANSWER_MISMATCH → NEEDS_REVIEW。
另：EXPRESSION 模式下逐字相同的不等式 `x<3` vs `x<3` 亦因绕过 identity 快路径而被判不等。
期望：相同答案 dual_equal=True。当前 False → exit 1。需 BypassSandbox。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shuati.models.domain import AnswerPart  # noqa: E402
from shuati.verify.equivalence import compare_answer_parts, latex_to_plain  # noqa: E402


def part(txt: str, atype: str) -> AnswerPart:
    return AnswerPart(
        label="主问", answer_type=atype, verify_mode="SYMBOLIC",
        collection_mode="SINGLE", answer=txt, sympy_expr=txt,
    )


async def dual(a: str, b: str, atype: str) -> bool:
    s = await compare_answer_parts([part(a, atype)], [part(b, atype)], grade_subject="高中数学", allow_pi_314_subjects=[])
    return s.all_dual_equal


async def main() -> int:
    print("latex_to_plain(r'x\\geq 3') =", repr(latex_to_plain(r"x\geq 3")), "(期望保留 '>=')")
    cases = [
        (r"x\geq 3", r"x\geq 3", "SET_OR_INTERVAL"),
        (r"x\ge 3", r"x>=3", "SET_OR_INTERVAL"),
        (r"x\geq 3 \cup x\leq -1", r"x\leq -1 \cup x\geq 3", "SET_OR_INTERVAL"),
        ("x<3", "x<3", "EXPRESSION"),
    ]
    bad = 0
    for a, b, t in cases:
        got = await dual(a, b, t)
        print(f"[{'OK ' if got else 'BAD'}] {t}: {a!r} vs {b!r}: dual_equal={got} expect=True")
        bad += not got
    return 1 if bad else 0


sys.exit(asyncio.run(main()))
