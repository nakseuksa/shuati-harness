"""Standalone empirical repro for Worker B (B07-F3) defect in `mistake._parse_single_known_answer`.

Demonstrates deterministic failures in `src/shuati/services/mistake.py :: _build_known_part`:
1. `SYMBOL_FILL` + `LABELED_MAP` positional input `">=, ="` splits on `=` inside `>=` / `=`,
   producing `canonical="INVALID"` and causing `compare_answer_parts` to return `False`
   (and `ingest_external_mistake_async(..., known_answer=">=, =")` to falsely demote a verified
   question to `NEEDS_REVIEW` with `KNOWN_ANSWER_MISMATCH`).
2. `SYMBOL_FILL` + `LABELED_MAP` reordered keyed input `"空2: =, 空1: >="` fails comparison
   because items are not aligned to reference key order (unlike `practice.py:L975-L983`).
3. `SYMBOLIC` + `LABELED_MAP` checks `if "=" in tok` BEFORE `:` / `：`, corrupting standard
   `key: value` answers whenever the math value contains `=`, `>=`, `<=`, or `!=`
   (e.g., mixed `SYMBOL_FILL` item `"关系: >=, 差值: 5"`, inequality `"范围: x >= 2"`,
   equation `"直线: y = 2x + 1"`, and positional `">=, 5"`).
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

    # Case 1: SYMBOL_FILL + LABELED_MAP positional ">=, ="
    ref_sym_map = [
        AnswerPart(
            label="主问",
            answer_type="SYMBOL_FILL",
            verify_mode="EXACT",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "空1", "item_type": "SYMBOL_FILL", "canonical": ">="},
                {"label": "空2", "item_type": "SYMBOL_FILL", "canonical": "="},
            ],
        )
    ]
    parsed_pos = _parse_single_known_answer(">=, =", ref_sym_map)
    eq_pos = await compare_answer_parts(
        ref_sym_map, parsed_pos, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
    )
    if not eq_pos.all_dual_equal:
        failures.append(
            f"[FAIL-1] SYMBOL_FILL LABELED_MAP positional '>=, =' misparsed as {parsed_pos[0].items!r} "
            f"(all_dual_equal={eq_pos.all_dual_equal}, reasons={eq_pos.review_reasons})"
        )

    # Case 2: SYMBOL_FILL + LABELED_MAP reordered keyed "空2: =, 空1: >="
    parsed_reord = _parse_single_known_answer("空2: =, 空1: >=", ref_sym_map)
    eq_reord = await compare_answer_parts(
        ref_sym_map, parsed_reord, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
    )
    if not eq_reord.all_dual_equal:
        failures.append(
            f"[FAIL-2] SYMBOL_FILL LABELED_MAP reordered keyed '空2: =, 空1: >=' not aligned to ref keys: "
            f"{parsed_reord[0].items!r} (all_dual_equal={eq_reord.all_dual_equal})"
        )

    # Case 3: SYMBOLIC + LABELED_MAP mixed with SYMBOL_FILL item ("关系: >=, 差值: 5" and positional ">=, 5")
    ref_mixed_map = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "关系", "item_type": "SYMBOL_FILL", "canonical": ">="},
                {"label": "差值", "expr": "5"},
            ],
        )
    ]
    for raw in ("关系: >=, 差值: 5", ">=, 5"):
        parsed_mix = _parse_single_known_answer(raw, ref_mixed_map)
        eq_mix = await compare_answer_parts(
            ref_mixed_map, parsed_mix, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
        )
        if not eq_mix.all_dual_equal:
            failures.append(
                f"[FAIL-3] SYMBOLIC LABELED_MAP mixed SYMBOL_FILL {raw!r} misparsed as {parsed_mix[0].items!r} "
                f"(all_dual_equal={eq_mix.all_dual_equal})"
            )

    # Case 4: SYMBOLIC + LABELED_MAP with ':' separator whose math value contains '=' or '>='
    ref_ineq_map = [
        AnswerPart(
            label="主问",
            answer_type="SET_OR_INTERVAL",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[{"label": "范围", "expr": "x >= 2"}],
        )
    ]
    parsed_ineq = _parse_single_known_answer("范围: x >= 2", ref_ineq_map)
    eq_ineq = await compare_answer_parts(
        ref_ineq_map, parsed_ineq, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
    )
    if not eq_ineq.all_dual_equal:
        failures.append(
            f"[FAIL-4] SET_OR_INTERVAL LABELED_MAP '范围: x >= 2' misparsed as {parsed_ineq[0].items!r} "
            f"(all_dual_equal={eq_ineq.all_dual_equal})"
        )

    ref_eq_map = [
        AnswerPart(
            label="主问",
            answer_type="EXPRESSION",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[{"label": "直线", "expr": "y = 2*x + 1"}],
        )
    ]
    parsed_eq = _parse_single_known_answer("直线: y = 2*x + 1", ref_eq_map)
    eq_expr = await compare_answer_parts(
        ref_eq_map, parsed_eq, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
    )
    if not eq_expr.all_dual_equal:
        failures.append(
            f"[FAIL-5] EXPRESSION LABELED_MAP '直线: y = 2*x + 1' misparsed as {parsed_eq[0].items!r} "
            f"(all_dual_equal={eq_expr.all_dual_equal})"
        )

    # Case 6: End-to-end ingest_external_mistake_async falsely demotes verified SYMBOL_FILL LABELED_MAP to NEEDS_REVIEW
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
                    "answer_type": "SYMBOL_FILL",
                    "verify_mode": "EXACT",
                    "collection_mode": "LABELED_MAP",
                    "answer": "",
                    "items": [
                        {"label": "空1", "item_type": "SYMBOL_FILL", "canonical": ">="},
                        {"label": "空2", "item_type": "SYMBOL_FILL", "canonical": "="},
                    ],
                }
            ]
            diag = {
                "visual_facts": "",
                "visual_ambiguity_flag": False,
                "visual_ambiguity_note": "",
                "method_summary": "比较大小关系",
                "solution_markdown": "空1 填 >=，空2 填 =。",
                "final_answers": fa,
                "sympy_code": None,
                "steps": [
                    {
                        "step_index": 1,
                        "sub_goal": "比较两空大小",
                        "core_formula": "a >= b",
                        "hint": "分别化简比较。",
                        "raw_l1": "计算与数感",
                        "raw_l2": "大小比较",
                        "raw_l3": "作差比较",
                    }
                ],
                "abstract_skeleton": "根据代数式大小关系填写比较符号。",
                "primary_l1_module": "计算与数感",
                "secondary_l1_modules": [],
                "matched_l2": ["大小比较"],
                "thinking_methods": ["作差法"],
                "classic_motifs": ["不等式比较"],
                "trap_tags": [],
                "difficulty": {
                    "C_concept_count": {"evidence": "单一比较", "score": 1},
                    "S_chain_length": {"evidence": "一步推导", "score": 1},
                    "T_trickiness": {"evidence": "直接判断", "score": 1},
                    "L_computation_load": {"evidence": "简单计算", "score": 1},
                },
            }
            sec = {
                "visual_facts": "",
                "visual_ambiguity_flag": False,
                "visual_ambiguity_note": "",
                "method_summary": "直接比较",
                "solution_markdown": "依次为 >= 和 =。",
                "final_answers": fa,
            }
            res = await ingest_external_mistake_async(
                conn,
                settings,
                question_text="在空1、空2中依次填入合适的比较符号。",
                grade_subject="小学奥数",
                known_answer=">=, =",
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
                    f"[FAIL-6] E2E ingest with known_answer='>=, =' falsely demoted question: "
                    f"verification_status={res['verification_status']!r}, eff_is_servable={res['eff_is_servable']!r}"
                )
        finally:
            conn.close()

    if failures:
        for f in failures:
            print(f)
        raise AssertionError(f"{len(failures)} B07-F3 LABELED_MAP known-answer failures reproduced")
    print("ALL B07-F3 LABELED_MAP CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"ASSERTION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
