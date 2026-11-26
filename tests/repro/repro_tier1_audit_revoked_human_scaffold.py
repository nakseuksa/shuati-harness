"""Repro for Tier 1 B04-F1: audit.py misses revoked/unbacked HUMAN_VERIFIED SCAFFOLD edges.

In topology.py (L889, L913), every ASSERT relation override creates/promotes the
question_relations edge with confidence_source='HUMAN_VERIFIED'. When that ASSERT
override is later revoked (is_active=0), the stale HUMAN_VERIFIED SCAFFOLD edge
with 0 < elo_diff < 180 (and < 200) is no longer exempted by an active ASSERT,
yet audit.py fails to flag it because its SQL WHERE clause only checks threshold
violations for RULE_MATRIX and LLM_VERIFIED.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from shuati.config import Settings
from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.pipeline.audit import run_stage_q_audit


def _seed_question(conn, uid: str, *, c: int, s: int, t: int, l: int) -> None:
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        )
        VALUES (?, '小学奥数', 'BATCH_JSONL', 'col_a', ?, ?, ?);
        """,
        (uid, uid.split(":")[-1], f"题干-{uid}", f"hash-{uid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_primary_l1_module,
            m_c_score, m_s_score, m_t_score, m_l_score
        )
        VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', '平面几何', ?, ?, ?, ?);
        """,
        (uid, f"题干-{uid}", c, s, t, l),
    )
    effective.recompute(conn, uid)


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = get_connection(tmp / "repro_b04_f1.db")
        try:
            init_db(conn)
            # p:a:1120 -> Elo 1120; p:a:1290 -> Elo 1290 (diff = 170 < 180 < 200)
            _seed_question(conn, "p:a:1120", c=1, s=1, t=1, l=1)
            _seed_question(conn, "p:a:1290", c=1, s=2, t=1, l=2)

            # Insert SCAFFOLD edge as Stage 4 writes it for an ASSERT override:
            # confidence_source = 'HUMAN_VERIFIED', diff = 170.
            conn.execute(
                """
                INSERT INTO question_relations(
                    source_question_uid, target_question_uid, relation_type, is_directed,
                    is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
                    containment_score, trap_overlap, confidence_source
                )
                VALUES ('p:a:1120', 'p:a:1290', 'SCAFFOLD', 1, 0, 0.5, 0.8, 0.5, 0.9, 0.85, 0.0, 'HUMAN_VERIFIED');
                """
            )
            # Active ASSERT first -> must be exempted (0 violations)
            conn.execute(
                """
                INSERT INTO relation_overrides(
                    override_id, source_question_uid, target_question_uid, relation_type,
                    action, reason, source, is_active
                )
                VALUES (1, 'p:a:1120', 'p:a:1290', 'SCAFFOLD', 'ASSERT', 'human_assert', 'PI_CLI', 1);
                """
            )
            conn.commit()

            settings = Settings()
            res_active = run_stage_q_audit(
                conn,
                settings,
                human_dir=tmp / "human",
                vectors_dir=tmp / "vectors",
                reports_dir=tmp / "reports_active",
                config_dir=tmp / "config",
            )
            active_viol = res_active["report"]["section_5_integrity_and_cost"]["db_invariants"][
                "scaffold_contract_violations"
            ]
            assert active_viol["count"] == 0, f"Active ASSERT should be exempt, got {active_viol}"

            # Now REVOKE the ASSERT override (is_active = 0):
            # The stale HUMAN_VERIFIED edge (diff = 170 < 180 < 200) has no active ASSERT
            # and MUST be flagged as a SCAFFOLD contract violation!
            conn.execute("UPDATE relation_overrides SET is_active = 0 WHERE override_id = 1;")
            conn.commit()

            res_revoked = run_stage_q_audit(
                conn,
                settings,
                human_dir=tmp / "human",
                vectors_dir=tmp / "vectors",
                reports_dir=tmp / "reports_revoked",
                config_dir=tmp / "config",
            )
            revoked_viol = res_revoked["report"]["section_5_integrity_and_cost"]["db_invariants"][
                "scaffold_contract_violations"
            ]
            assert revoked_viol["count"] == 1, (
                "BUG REPRODUCED: Revoked ASSERT on HUMAN_VERIFIED SCAFFOLD edge "
                f"(p:a:1120 -> p:a:1290, diff=170 < 180) was NOT flagged! got={revoked_viol}"
            )
            risks = res_revoked["report"]["section_7_risks_and_actions"]["risks"]
            assert any("SCAFFOLD" in r for r in risks), (
                f"Expected SCAFFOLD violation risk after revoking ASSERT, got risks={risks}"
            )
        finally:
            conn.close()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"ASSERTION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print("PASS: revoked ASSERT HUMAN_VERIFIED low-diff SCAFFOLD edge properly flagged.")
