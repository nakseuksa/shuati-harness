"""Repro for Worker A (B06-F5): log_batch_practice fails to reject duplicate
client_request_id within the same batch (both explicit duplicates and
request_id_prefix collisions), causing silent data loss of later entries and
partial commit with duplicated records in the response.

Expected:
- Duplicate request_id inside a single log_batch_practice call must raise
  PreconditionError in pre-validation BEFORE mutating practice_log or exporting
  human_mirror snapshots (0 rows committed).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from shuati.core import effective
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.services import practice


def _seed(conn, uid: str) -> None:
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
        VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', '计算', '[]', 2, 2, 2, 1,
                '[{"label":"主问","answer_type":"NUMERIC","verify_mode":"SYMBOLIC","collection_mode":"SINGLE","answer":"1","sympy_expr":"1"}]');
        """,
        (uid, uid),
    )
    effective.recompute(conn, uid)


def test_repro_log_batch_practice_intra_batch_duplicate_request_id() -> None:
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        conn = get_connection(tp / "repro_b06_f5.db")
        try:
            init_db(conn)
            human_dir = tp / "human"

            _seed(conn, "Q1")
            _seed(conn, "Q2")
            conn.execute(
                "INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json) "
                "VALUES ('QB_DUP', 'default', '小学奥数', '{}', '[]');"
            )
            conn.commit()

            # Case 1: explicit duplicate request_id across two distinct entries
            raised = False
            res = None
            try:
                res = practice.log_batch_practice(
                    conn,
                    quiz_batch_id="QB_DUP",
                    entries=[
                        {"uid": "Q1", "outcome": "SOLVED_SOLO", "request_id": "dup_req"},
                        {"uid": "Q2", "outcome": "WRONG", "request_id": "dup_req"},
                    ],
                    human_dir=human_dir,
                )
            except PreconditionError:
                raised = True

            row_count = conn.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0]
            assert raised, (
                f"Expected PreconditionError on intra-batch duplicate request_id, "
                f"instead committed {row_count} row(s) and silently dropped Q2: {res}"
            )
            assert row_count == 0, f"Expected atomic rollback (0 rows), found {row_count}"

            # Case 2: explicit request_id colliding with auto-generated request_id_prefix:2
            raised_pfx = False
            res_pfx = None
            try:
                res_pfx = practice.log_batch_practice(
                    conn,
                    quiz_batch_id="QB_DUP",
                    entries=[
                        {"uid": "Q1", "outcome": "SOLVED_SOLO", "request_id": "pfx:2"},
                        {"uid": "Q2", "outcome": "WRONG"},
                    ],
                    request_id_prefix="pfx",
                    human_dir=human_dir,
                )
            except PreconditionError:
                raised_pfx = True

            row_count_2 = conn.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0]
            assert raised_pfx, (
                f"Expected PreconditionError on prefix collision 'pfx:2', "
                f"instead committed {row_count_2} row(s): {res_pfx}"
            )
            assert row_count_2 == 0, f"Expected 0 rows after prefix collision, found {row_count_2}"
        finally:
            conn.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    test_repro_log_batch_practice_intra_batch_duplicate_request_id()
    print("PASS")
