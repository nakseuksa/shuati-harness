"""Repro (M1 Round 1 / HIGH): latex_to_plain 把 `{`/`}` 一律替换为 `(`/`)`，破坏 SET_OR_INTERVAL 有限集。

后果（均经公开入口 compare_answer_parts 实证）：
  1. `\\{1,2,3\\}` 与 `\\{3,2,1\\}`（同一有限集）被判不等，且逐字相同的 `\\{1,2,3\\}` 与自身也被判不等；
  2. `\\{1,2\\}`（有限集）与 `(1,2)`（开区间）被判等价（假阳性，会让双盲错误共识 → VERIFIED_CONSENSUS）。
期望：1 为 True，2 为 False。当前相反 → exit 1。
需 BypassSandbox（asyncio ProactorEventLoop 在沙盒下会阻塞）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shuati.models.domain import AnswerPart  # noqa: E402
from shuati.verify.equivalence import compare_answer_parts  # noqa: E402


def part(txt: str) -> AnswerPart:
    return AnswerPart(
        label="主问", answer_type="SET_OR_INTERVAL", verify_mode="SYMBOLIC",
        collection_mode="SINGLE", answer=txt, sympy_expr=txt,
    )


async def dual(a: str, b: str) -> bool:
    s = await compare_answer_parts([part(a)], [part(b)], grade_subject="高中数学", allow_pi_314_subjects=[])
    return s.all_dual_equal


async def main() -> int:
    cases = [
        (r"\{1,2,3\}", r"\{3,2,1\}", True, "同一有限集乱序应等价"),
        (r"\{1,2,3\}", r"\{1,2,3\}", True, "逐字相同应等价"),
        (r"\{1,2\}", r"(1,2)", False, "有限集 {1,2} 不应等于开区间 (1,2)"),
    ]
    bad = 0
    for a, b, expect, why in cases:
        got = await dual(a, b)
        flag = "OK " if got == expect else "BAD"
        print(f"[{flag}] {a} vs {b}: got={got} expect={expect} ({why})")
        bad += got != expect
    return 1 if bad else 0


sys.exit(asyncio.run(main()))
