"""Repro for Worker A (B06-F2): SYMBOL_FILL defects across UNORDERED_MULTISET,
LABELED_MAP, and SINGLE collection modes in _parse_student_answer_parts.

Bugs proved:
1. SYMBOL_FILL + UNORDERED_MULTISET: submitting '<, >' against expected '>, <'
   returns all_correct=False instead of True.
2. SYMBOL_FILL + LABELED_MAP: submitting wrong labels '丙: >, 丁: =' or
   duplicate labels '甲: >, 甲: =' against expected '甲: >, 乙: =' returns
   all_correct=True (false-positive grading!) instead of False.
3. SYMBOL_FILL + SINGLE: submitting '$' crashes with uncaught ValueError inside
   normalize_answer_parts instead of treating the part as missing/incorrect.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from shuati.config import Settings
from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.services import practice


def _seed(conn, uid: str, fa: list[dict[str, Any]]) -> None:
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
        VALUES (?, '小学奥数', ?, '填空题', 'VERIFIED_CONSENSUS', '计算', '[]', 2, 2, 2, 1, ?);
        """,
        (uid, uid, json.dumps(fa, ensure_ascii=False)),
    )
    effective.recompute(conn, uid)


def test_repro_symbol_fill_multiset_labeled_map_and_dollar_crash() -> None:
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        conn = get_connection(tp / "repro_b06_f2.db")
        try:
            init_db(conn)
            human_dir = tp / "human"
            settings = Settings()
            settings.pipeline.use_mock_llm = True

            _seed(
                conn,
                "Q_SYM_MULTISET",
                [
                    {
                        "label": "主问",
                        "answer_type": "SYMBOL_FILL",
                        "verify_mode": "EXACT",
                        "collection_mode": "UNORDERED_MULTISET",
                        "answer": ">, <",
                        "items": [">", "<"],
                    }
                ],
            )
            _seed(
                conn,
                "Q_SYM_MAP",
                [
                    {
                        "label": "主问",
                        "answer_type": "SYMBOL_FILL",
                        "verify_mode": "EXACT",
                        "collection_mode": "LABELED_MAP",
                        "answer": "甲: >, 乙: =",
                        "items": [
                            {"label": "甲", "item_type": "SYMBOL_FILL", "canonical": ">"},
                            {"label": "乙", "item_type": "SYMBOL_FILL", "canonical": "="},
                        ],
                    }
                ],
            )
            _seed(
                conn,
                "Q_SYM_SINGLE",
                [
                    {
                        "label": "主问",
                        "answer_type": "SYMBOL_FILL",
                        "verify_mode": "EXACT",
                        "collection_mode": "SINGLE",
                        "answer": ">",
                        "canonical": ">",
                    }
                ],
            )
            conn.commit()

            # Bug 1: UNORDERED_MULTISET must accept reordered symbols '<, >'
            r_ms = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_SYM_MULTISET", answer_input="<, >", human_dir=human_dir
                )
            )
            assert r_ms["all_correct"] is True, (
                f"UNORDERED_MULTISET SYMBOL_FILL rejected valid permutation '<, >': {r_ms}"
            )

            # Bug 2a: LABELED_MAP must reject completely wrong keys '丙: >, 丁: ='
            r_wrong_keys = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_SYM_MAP", answer_input="丙: >, 丁: =", human_dir=human_dir
                )
            )
            assert r_wrong_keys["all_correct"] is False, (
                f"LABELED_MAP SYMBOL_FILL falsely accepted wrong labels '丙: >, 丁: =': {r_wrong_keys}"
            )

            # Bug 2b: LABELED_MAP must reject duplicate label '甲: >, 甲: ='
            r_dup_keys = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_SYM_MAP", answer_input="甲: >, 甲: =", human_dir=human_dir
                )
            )
            assert r_dup_keys["all_correct"] is False, (
                f"LABELED_MAP SYMBOL_FILL falsely accepted duplicate labels '甲: >, 甲: =': {r_dup_keys}"
            )

            # Bug 3: SINGLE SYMBOL_FILL with '$' must not crash with uncaught ValueError
            r_dollar = asyncio.run(
                practice.check_answer_async(
                    conn, settings, "Q_SYM_SINGLE", answer_input="$", human_dir=human_dir
                )
            )
            assert r_dollar["all_correct"] is False, (
                f"SINGLE SYMBOL_FILL '$' should be incorrect/missing, got: {r_dollar}"
            )
        finally:
            conn.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    test_repro_symbol_fill_multiset_labeled_map_and_dollar_crash()
    print("PASS")
