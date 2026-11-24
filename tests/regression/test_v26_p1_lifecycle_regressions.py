"""v2.6 P1 生命周期对抗审查回归测试套件（吸收 B4 与 B5 实证 Repro）。

- B4: `reconfirm_stale_overrides` 重签 `TAXONOMY_OVERRIDE_FIELDS` 时刷新 `taxonomy_version` 为当前学段版本，真正解挂。
- B5: `effective.recompute` 中词表类 `TAXONOMY_UNMAPPED` 挂起绝不连坐同 `bundle_id` 的验真类覆写；仅验真类触发全组挂起传播。
"""

from __future__ import annotations

import json
from pathlib import Path

from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.services import facade

GS = "小学奥数"


def _seed_q(conn, uid: str, *, content_hash: str = "h_q1") -> None:
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        ) VALUES (?, ?, 'BATCH_JSONL', 'ch01', ?, '1+1=?', ?);
        """,
        (uid, GS, uid, content_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_c_score, m_s_score, m_t_score, m_l_score
        ) VALUES (?, ?, '1+1=?', '解答题', 'VERIFIED_CONSENSUS', 1, 1, 1, 1);
        """,
        (uid, GS),
    )
    conn.commit()


def test_b4_reconfirm_clears_taxonomy_unmapped_with_current_version(tmp_path: Path) -> None:
    """B4 回归：reconfirm_stale_overrides 重签词表类覆写时刷新 taxonomy_version 至当前版本并解挂。"""
    conn = get_connection(tmp_path / "b4.db")
    init_db(conn)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;",
        (GS,),
    )
    _seed_q(conn, "q1", content_hash="h_q1")

    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, reason, source,
            bundle_id, base_fingerprint, taxonomy_version,
            is_active, is_suspended, suspended_reason
        ) VALUES ('q1', 'primary_l1_module', '平面几何', 'r', 'REVIEW_UI', NULL, 'h_q1', '0.9', 1, 0, '');
        """
    )
    conn.commit()

    effective.recompute(conn, "q1")
    conn.commit()
    ov_before = conn.execute(
        "SELECT is_suspended, suspended_reason FROM human_overrides WHERE is_active = 1;"
    ).fetchone()
    assert (ov_before["is_suspended"], ov_before["suspended_reason"]) == (1, "TAXONOMY_UNMAPPED")

    res = facade.reconfirm_stale_overrides(conn, "q1", human_dir=tmp_path / "human")
    assert res["reconfirmed_count"] == 1

    active = conn.execute(
        "SELECT is_suspended, suspended_reason, taxonomy_version FROM human_overrides WHERE is_active = 1;"
    ).fetchall()
    assert len(active) == 1
    assert active[0]["is_suspended"] == 0
    assert active[0]["suspended_reason"] == ""
    assert active[0]["taxonomy_version"] == "1.0"
    conn.close()


def test_b5_taxonomy_stale_never_poisons_verification_bundle(tmp_path: Path) -> None:
    """B5 回归：同 bundle 内词表类 TAXONOMY_UNMAPPED 不得连坐同 bundle 验真类覆写（含多次重算幂等）。"""
    conn = get_connection(tmp_path / "b5.db")
    init_db(conn)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;",
        (GS,),
    )
    _seed_q(conn, "q1", content_hash="h_q1")

    ans = json.dumps([{"verify_mode": "EXACT", "value": "1"}])
    for fname, val, tv in (
        ("final_answers_json", ans, ""),
        ("primary_l1_module", "平面几何", "0.9"),
    ):
        conn.execute(
            """
            INSERT INTO human_overrides(
                question_uid, field_name, override_value, reason, source,
                bundle_id, base_fingerprint, taxonomy_version,
                is_active, is_suspended, suspended_reason
            ) VALUES ('q1', ?, ?, 'r', 'REVIEW_UI', 'bnd_mixed', 'h_q1', ?, 1, 0, '');
            """,
            (fname, val, tv),
        )
    conn.commit()

    # 连续两次 recompute 均不得连坐 final_answers_json
    for _ in range(2):
        effective.recompute(conn, "q1")
        conn.commit()

    rows = {
        r["field_name"]: (r["is_suspended"], r["suspended_reason"])
        for r in conn.execute(
            "SELECT field_name, is_suspended, suspended_reason FROM human_overrides WHERE is_active = 1;"
        ).fetchall()
    }
    assert rows["primary_l1_module"] == (1, "TAXONOMY_UNMAPPED")
    assert rows["final_answers_json"] == (0, "")

    q = conn.execute(
        "SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid = 'q1';"
    ).fetchone()
    assert q["eff_verification_status"] == "HUMAN_VERIFIED"
    assert "OVERRIDE_STALE" not in q["eff_review_reason"]
    conn.close()
