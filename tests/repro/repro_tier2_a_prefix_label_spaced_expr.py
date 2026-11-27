"""Repro for Worker A (B06-F1): _PREFIX_LABEL_RE mis-parses spaced arithmetic
expressions like '(2) * (3)', '(1) * 6', '(2) + 3' and implicit multiplication
'(2)(3)' as sub-question labels in multi-part answers.

Expected:
- Multi-part math expressions '(2) * (3); 5', '(1) * 6; (2) + 3',
  '(2) + 4; (1) + 4', and '(2)(3); 5' must NOT be split on '(1)'/'(2)' and must
  all grade as all_correct=True for expected (1)=6, (2)=5.
- Legitimate labeled negative/decimal answers like '(1) -5; (2) 3.14' must
  continue to grade as all_correct=True.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from shuati.config import Settings
from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.services import practice


def _seed_two_part(conn, uid: str, ans1: str, ans2: str) -> None:
    fa = [
        {
            "label": "(1)",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "SINGLE",
            "answer": ans1,
            "sympy_expr": ans1,
        },
        {
            "label": "(2)",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "SINGLE",
            "answer": ans2,
            "sympy_expr": ans2,
        },
    ]
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash
        )
        VALUES (?, '小学奥数', 'BATCH_JSONL', 'c', ?, ?, ?);
        """,
        (uid, uid, uid, f"h_{uid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format, m_verification_status,
            m_primary_l1_module, m_secondary_l1_modules_json,
            m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json
        )
        VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', '计算', '[]', 2, 2, 2, 1, ?);
        """,
        (uid, uid, json.dumps(fa, ensure_ascii=False)),
    )
    effective.recompute(conn, uid)


def test_repro_prefix_label_spaced_arithmetic_and_implicit_mul() -> None:
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        conn = get_connection(tp / "repro_b06_f1.db")
        try:
            init_db(conn)
            human_dir = tp / "human"
            settings = Settings()
            settings.pipeline.use_mock_llm = True

            _seed_two_part(conn, "Q_2P", "6", "5")
            _seed_two_part(conn, "Q_NEG", "-5", "3.14")
            conn.commit()

            # Sanity check: legitimate labeled negative/decimal answer must remain correct
            r_neg = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_NEG", answer_input="(1) -5; (2) 3.14", human_dir=human_dir
                )
            )
            assert r_neg["all_correct"] is True, f"Legitimate labeled negative broke: {r_neg}"

            # Case 1: '(2) * (3); 5' currently crashes with PreconditionError (duplicate label '(2)')
            r_mul = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_2P", answer_input="(2) * (3); 5", human_dir=human_dir
                )
            )
            assert r_mul["all_correct"] is True, f"Expected all_correct=True for '(2) * (3); 5', got {r_mul}"

            # Case 2: '(1) * 6; (2) + 3' currently strips '(1)'/'(2)' -> '* 6' / '+ 3' -> 0/2
            r_both = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_2P", answer_input="(1) * 6; (2) + 3", human_dir=human_dir
                )
            )
            assert r_both["all_correct"] is True, f"Expected all_correct=True for '(1) * 6; (2) + 3', got {r_both}"

            # Case 3: '(2) + 4; (1) + 4' currently swaps labels and strips operands -> 0/2
            r_swap = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_2P", answer_input="(2) + 4; (1) + 4", human_dir=human_dir
                )
            )
            assert r_swap["all_correct"] is True, f"Expected all_correct=True for '(2) + 4; (1) + 4', got {r_swap}"

            # Case 4: implicit multiplication '(2)(3); 5' currently crashes with duplicate label '(2)'
            r_imp = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_2P", answer_input="(2)(3); 5", human_dir=human_dir
                )
            )
            assert r_imp["all_correct"] is True, f"Expected all_correct=True for '(2)(3); 5', got {r_imp}"
        finally:
            conn.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    test_repro_prefix_label_spaced_arithmetic_and_implicit_mul()
    print("PASS")
