"""Repro for Worker A (B05-F9): composer.compose_quiz still uses SQLite string
ORDER BY created_at DESC LIMIT ? for quiz_batches instead of normalizing
quiz_batches.created_at to UTC datetime via _parse_utc_dt before ordering.

Because ASCII 'T' (84) > ' ' (32), an older ISO-8601 batch
('2026-05-20T14:00:00Z' or '2026-05-20T20:00:00+08:00' == 12:00 UTC) sorts
AFTER a newer naive SQLite UTC batch ('2026-05-20 15:00:00'), causing
exclude_recent_quiz_batches=1 to exclude the OLDER batch and re-serve the
NEWEST unlogged question!
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from shuati.config import Settings
from shuati.core import composer, effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db


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
        VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', '平面几何', '[]', 2, 2, 2, 1,
                '[{"label":"主问","answer_type":"NUMERIC","verify_mode":"SYMBOLIC","collection_mode":"SINGLE","answer":"1","sympy_expr":"1"}]');
        """,
        (uid, uid),
    )
    effective.recompute(conn, uid)
    with effective.authorized_effective_writer():
        conn.execute(
            "UPDATE questions SET eff_base_elo = 1300, eff_tier = 'L2_常规巩固', eff_tier_rank = 2 WHERE question_uid = ?;",
            (uid,),
        )


def test_repro_quiz_batches_iso_and_offset_ordering() -> None:
    settings = Settings()
    now_utc = datetime(2026, 5, 20, 16, 0, 0, tzinfo=UTC)

    # Case 1: ISO 'T'/'Z' (14:00 UTC) vs naive SQLite UTC (15:00 UTC, newer by 1h)
    with tempfile.TemporaryDirectory() as td:
        conn = get_connection(Path(td) / "repro_b05_f9_iso.db")
        try:
            init_db(conn)
            _seed(conn, "q_old")
            _seed(conn, "q_new")
            conn.execute(
                "INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json, created_at) "
                "VALUES ('qb_old', 'default', '小学奥数', '{}', ?, '2026-05-20T14:00:00Z');",
                (json.dumps([{"question_uid": "q_old"}]),),
            )
            conn.execute(
                "INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json, created_at) "
                "VALUES ('qb_new', 'default', '小学奥数', '{}', ?, '2026-05-20 15:00:00');",
                (json.dumps([{"question_uid": "q_new"}]),),
            )
            conn.commit()

            res = composer.compose_quiz(
                conn,
                learner_id="default",
                grade_subject="小学奥数",
                elo_cfg=settings.learner_elo,
                composer_cfg=settings.composer,
                count=1,
                mix="1:0:0",
                exclude_recent_quiz_batches=1,
                now_utc=now_utc,
            )
            picked = [it["question_uid"] for it in res["items"]]
        finally:
            conn.close()
        assert picked == ["q_old"], (
            f"Expected newest unlogged batch 'qb_new' (15:00 UTC) to be excluded and 'q_old' picked, "
            f"but ASCII 'T' > ' ' ordering caused picked={picked}"
        )

    # Case 2: Timezone offset '+08:00' (20:00+08:00 == 12:00 UTC) vs naive SQLite UTC (15:00 UTC, newer by 3h)
    with tempfile.TemporaryDirectory() as td:
        conn = get_connection(Path(td) / "repro_b05_f9_offset.db")
        try:
            init_db(conn)
            _seed(conn, "q_old")
            _seed(conn, "q_new")
            conn.execute(
                "INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json, created_at) "
                "VALUES ('qb_old', 'default', '小学奥数', '{}', ?, '2026-05-20T20:00:00+08:00');",
                (json.dumps([{"question_uid": "q_old"}]),),
            )
            conn.execute(
                "INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json, created_at) "
                "VALUES ('qb_new', 'default', '小学奥数', '{}', ?, '2026-05-20 15:00:00');",
                (json.dumps([{"question_uid": "q_new"}]),),
            )
            conn.commit()

            res = composer.compose_quiz(
                conn,
                learner_id="default",
                grade_subject="小学奥数",
                elo_cfg=settings.learner_elo,
                composer_cfg=settings.composer,
                count=1,
                mix="1:0:0",
                exclude_recent_quiz_batches=1,
                now_utc=now_utc,
            )
            picked = [it["question_uid"] for it in res["items"]]
        finally:
            conn.close()
        assert picked == ["q_old"], (
            f"Expected newest unlogged batch 'qb_new' (15:00 UTC) to be excluded over 20:00+08:00 (12:00 UTC), "
            f"got picked={picked}"
        )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    test_repro_quiz_batches_iso_and_offset_ordering()
    print("PASS")
