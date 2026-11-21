"""测试局部刷新引擎与覆写按 bundle_id 整组挂起机制。"""

from pathlib import Path

from shuati.core import effective
from shuati.core.refresh import handle_source_content_changed, mark_stages_stale
from shuati.db.connection import get_connection
from shuati.db.schema import init_db


def test_mark_stages_stale(tmp_path: Path):
    db_file = tmp_path / "test_refresh.db"
    conn = get_connection(db_file)
    init_db(conn)

    uid = "primary_olympiad:test:q001"
    # 插入 raw_questions 满足外键
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_file, source_id, raw_question, content_hash
        ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'test', 'test/p.jsonl', 'q001', '题干', 'hash1')
        """,
        (uid,),
    )

    # 1. compile_state 初始为空，调用 mark_stages_stale 应自动创建
    mark_stages_stale(conn, uid, ["S1", "S2"])
    rows = conn.execute(
        "SELECT stage, status FROM compile_state WHERE question_uid = ? ORDER BY stage",
        (uid,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["stage"] == "S1" and rows[0]["status"] == "STALE"
    assert rows[1]["stage"] == "S2" and rows[1]["status"] == "STALE"

    # 2. 将 S1 改为 OK 后再次调用，应被更新回 STALE
    conn.execute(
        "UPDATE compile_state SET status = 'OK' WHERE question_uid = ? AND stage = 'S1'",
        (uid,),
    )
    mark_stages_stale(conn, uid, ["S1", "S3C"])
    row_s1 = conn.execute(
        "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S1'",
        (uid,),
    ).fetchone()
    assert row_s1["status"] == "STALE"


def test_handle_source_content_changed_bundle_suspension(tmp_path: Path):
    db_file = tmp_path / "test_source_changed.db"
    conn = get_connection(db_file)
    init_db(conn)
    effective.install_eff_write_guard(conn)

    uid = "primary_olympiad:ch28:q001"
    old_hash = "old_content_hash_12345"
    new_hash = "new_content_hash_67890"

    # 插入原始题目与主表
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_file, source_id, raw_question, content_hash
        ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch28', 'ch28/p.jsonl', 'q001', '原题干', ?)
        """,
        (uid, old_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_options_json, m_format, m_verification_status
        ) VALUES (?, '小学奥数', '原题干', '{}', '解答题', 'VERIFIED_CONSENSUS')
        """,
        (uid,),
    )
    effective.recompute(conn, uid)

    # 插入 2 个共享 bundle_id 的覆写（ADOPT_RUN 组合操作）
    # override 1: question_text，指纹为 old_hash
    # override 2: verification_status，指纹也为 old_hash
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, bundle_id, base_fingerprint, is_active
        ) VALUES (?, 'question_text', '人工清洗后题干', 'REVIEW_UI', 'bundle_101', ?, 1)
        """,
        (uid, old_hash),
    )
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, bundle_id, base_fingerprint, is_active
        ) VALUES (?, 'verification_status', 'HUMAN_VERIFIED', 'REVIEW_UI', 'bundle_101', ?, 1)
        """,
        (uid, old_hash),
    )

    # 先重算一次，此时状态为 HUMAN_VERIFIED
    effective.recompute(conn, uid)
    q_before = conn.execute(
        "SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    assert q_before["eff_verification_status"] == "HUMAN_VERIFIED"

    # 更新 raw_questions 的 content_hash 为 new_hash 并触发 handle_source_content_changed
    conn.execute(
        "UPDATE raw_questions SET content_hash = ? WHERE question_uid = ?",
        (new_hash, uid),
    )
    handle_source_content_changed(conn, uid, new_hash)

    # 验证：
    # 1. human_overrides 中同 bundle_id 的 2 条覆写均已被挂起
    ov_rows = conn.execute(
        "SELECT field_name, is_suspended, suspended_reason FROM human_overrides WHERE question_uid = ?",
        (uid,),
    ).fetchall()
    assert len(ov_rows) == 2
    for r in ov_rows:
        assert r["is_suspended"] == 1
        assert r["suspended_reason"] == "SOURCE_CHANGED"

    # 2. compile_state S1..S4 均置为 STALE
    st_rows = conn.execute(
        "SELECT stage, status FROM compile_state WHERE question_uid = ?",
        (uid,),
    ).fetchall()
    assert len(st_rows) == 5
    for r in st_rows:
        assert r["status"] == "STALE"

    # 3. questions.eff_verification_status 降为 NEEDS_REVIEW，且 eff_review_reason 包含 OVERRIDE_STALE
    q_after = conn.execute(
        "SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    assert q_after["eff_verification_status"] == "NEEDS_REVIEW"
    assert "OVERRIDE_STALE" in q_after["eff_review_reason"]
