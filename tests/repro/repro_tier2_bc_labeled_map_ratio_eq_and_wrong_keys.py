"""Standalone empirical repro for Round 2 regressions in `mistake._split_labeled_map_token`.

Demonstrates two deterministic regressions introduced by Round 2 in `src/shuati/services/mistake.py`:
1. `RATIO` + `LABELED_MAP` with `=` key-value separator (`"甲=3:4, 乙=1:2"`, `"甲 = 3:4, 乙 = 1:2"`,
   `"乙=1:2, 甲=3:4"`): `if ":" in tok:` runs before `=` without rejecting `=` inside `k_clean`,
   splitting `"甲=3:4"` at the ratio colon into `label="甲=3", expr="4"` and causing
   `compare_answer_parts` to return `False` (and E2E `ingest_external_mistake_async` to falsely
   demote a verified `RATIO` `LABELED_MAP` question to `NEEDS_REVIEW`).
2. `NUMERIC` + `LABELED_MAP` with wrong keys using `=` (`"丙=12, 丁=8"` against
   `[{"label": "鸡", "expr": "12"}, {"label": "兔", "expr": "8"}]`): requiring `norm_k in ref_label_set`
   for `NUMERIC` causes `"丙=12"` to fall back to positional key `"鸡"` with `expr="丙=12"`, which
   the SymPy sandbox strips to `12 == 12`, producing a false positive `all_dual_equal=True`.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from shuati.config import Settings
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.models.domain import AnswerPart
from shuati.services.mistake import _parse_single_known_answer, ingest_external_mistake_async
from shuati.verify.equivalence import compare_answer_parts


async def main() -> None:
    failures: list[str] = []

    # Regression 1: RATIO LABELED_MAP with '=' separator ("甲=3:4, 乙=1:2", etc.)
    ref_ratio = [
        AnswerPart(
            label="主问",
            answer_type="RATIO",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "甲", "expr": "3:4"},
                {"label": "乙", "expr": "1:2"},
            ],
        )
    ]
    for raw in ("甲=3:4, 乙=1:2", "甲 = 3:4, 乙 = 1:2", "乙=1:2, 甲=3:4"):
        parsed = _parse_single_known_answer(raw, ref_ratio)
        eq = await compare_answer_parts(
            ref_ratio, parsed, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
        )
        if not eq.all_dual_equal:
            failures.append(
                f"[FAIL-1] RATIO LABELED_MAP {raw!r} misparsed as {parsed[0].items!r} "
                f"(all_dual_equal={eq.all_dual_equal}, reasons={eq.review_reasons})"
            )

    # Regression 2: Wrong keys in NUMERIC LABELED_MAP via '=' must NOT falsely pass as equal
    ref_num = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "鸡", "expr": "12"},
                {"label": "兔", "expr": "8"},
            ],
        )
    ]
    parsed_wrong = _parse_single_known_answer("丙=12, 丁=8", ref_num)
    eq_wrong = await compare_answer_parts(
        ref_num, parsed_wrong, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
    )
    if eq_wrong.all_dual_equal:
        failures.append(
            f"[FAIL-2] NUMERIC LABELED_MAP wrong keys '丙=12, 丁=8' falsely matched ref '鸡=12, 兔=8': "
            f"parsed={parsed_wrong[0].items!r} (all_dual_equal={eq_wrong.all_dual_equal})"
        )

    # Regression 3: E2E ingest with RATIO LABELED_MAP known_answer="甲=3:4, 乙=1:2" falsely demoted
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        cfg = tp / "config"
        shutil.copytree(Path("config"), cfg)
        conn = get_connection(tp / "data" / "shuati.db")
        try:
            init_db(conn, config_dir=cfg)
            settings = Settings()
            settings.pipeline.use_mock_llm = True
            fa = [
                {
                    "label": "主问",
                    "answer_type": "RATIO",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "LABELED_MAP",
                    "answer": "",
                    "items": [
                        {"label": "甲", "expr": "3:4"},
                        {"label": "乙", "expr": "1:2"},
                    ],
                }
            ]
            diag = {
                "visual_facts": "",
                "visual_ambiguity_flag": False,
                "visual_ambiguity_note": "",
                "method_summary": "化简两段比值",
                "solution_markdown": "甲为 3:4，乙为 1:2。",
                "final_answers": fa,
                "sympy_code": (
                    "from fractions import Fraction\n"
                    "def verify():\n"
                    "    a = Fraction(6, 8)\n"
                    "    b = Fraction(2, 4)\n"
                    "    return {'主问': [f'甲:{a.numerator}:{a.denominator}', f'乙:{b.numerator}:{b.denominator}']}\n"
                ),
                "steps": [
                    {
                        "step_index": 1,
                        "sub_goal": "求甲乙两组最简整数比",
                        "core_formula": "a:b",
                        "hint": "约去最大公因数。",
                        "raw_l1": "应用题",
                        "raw_l2": "比和比例",
                        "raw_l3": "化简比",
                    }
                ],
                "abstract_skeleton": "根据数量关系求多组最简整数比。",
                "primary_l1_module": "应用题",
                "secondary_l1_modules": [],
                "matched_l2": ["比和比例"],
                "thinking_methods": ["份数法"],
                "classic_motifs": ["比例化简"],
                "trap_tags": [],
                "difficulty": {
                    "C_concept_count": {"evidence": "比的概念", "score": 1},
                    "S_chain_length": {"evidence": "一步化简", "score": 1},
                    "T_trickiness": {"evidence": "直接求解", "score": 1},
                    "L_computation_load": {"evidence": "简单约分", "score": 1},
                },
            }
            sec = {
                "visual_facts": "",
                "visual_ambiguity_flag": False,
                "visual_ambiguity_note": "",
                "method_summary": "直接约分",
                "solution_markdown": "甲=3:4，乙=1:2。",
                "final_answers": fa,
            }
            res = await ingest_external_mistake_async(
                conn,
                settings,
                question_text="分别求甲、乙两组的最简整数比。",
                grade_subject="小学奥数",
                known_answer="甲=3:4, 乙=1:2",
                diagnosis_json=diag,
                secondary_json=sec,
                assets_dir=tp / "data" / "assets" / "images",
                config_dir=cfg,
                vectors_dir=tp / "data" / "vectors",
                drafts_dir=tp / "data" / "drafts",
                human_dir=tp / "human",
                lock_path=tp / "data" / ".compile.lock",
                use_mock=True,
            )
            if res["verification_status"] != "HUMAN_VERIFIED" or not res["eff_is_servable"]:
                failures.append(
                    f"[FAIL-3] E2E ingest with known_answer='甲=3:4, 乙=1:2' falsely demoted question: "
                    f"verification_status={res['verification_status']!r}, eff_is_servable={res['eff_is_servable']!r}"
                )
        finally:
            conn.close()

    if failures:
        for f in failures:
            print(f)
        raise AssertionError(
            f"{len(failures)} Round 2 _split_labeled_map_token regressions reproduced"
        )
    print("ALL ROUND 2 LABELED_MAP REGRESSION CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"ASSERTION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
