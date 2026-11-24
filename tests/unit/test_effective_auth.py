"""测试 core/effective.py 的写入拦截守卫 (set_authorizer) 与生效层重算一致性。"""

import json
import sqlite3

import pytest

from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db


@pytest.fixture
def db_conn(tmp_path):
    db_file = tmp_path / "test_effective.db"
    conn = get_connection(db_file)
    init_db(conn)
    effective.install_eff_write_guard(conn)
    return conn


def _seed_sample_question(conn, uid="primary_olympiad:ch01:q1"):
    source_id = uid.split(":")[-1]
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        )
        VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', ?, '1+1=?', 'hash_v1');
        """,
        (uid, source_id),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_c_score, m_s_score, m_t_score, m_l_score,
            m_verification_status, review_reason
        )
        VALUES (?, '小学奥数', '1+1=?', '计算题', 1, 1, 1, 1, 'VERIFIED_CONSENSUS', '');
        """,
        (uid,),
    )
    conn.commit()


def test_eff_write_guard_blocks_unauthorized_update(db_conn):
    _seed_sample_question(db_conn)

    # 1. 尝试在非授权上下文下直接 UPDATE questions.eff_base_elo
    with pytest.raises(sqlite3.DatabaseError) as exc_info:
        db_conn.execute(
            "UPDATE questions SET eff_base_elo = 2000 WHERE question_uid = 'primary_olympiad:ch01:q1';"
        )
    assert "not authorized" in str(exc_info.value).lower() or "denied" in str(exc_info.value).lower()

    # 2. 普通列更新不被拦截
    db_conn.execute(
        "UPDATE questions SET m_question_text = '1+2=?' WHERE question_uid = 'primary_olympiad:ch01:q1';"
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT m_question_text FROM questions WHERE question_uid = 'primary_olympiad:ch01:q1';"
    ).fetchone()
    assert row["m_question_text"] == "1+2=?"

    # 3. 在授权上下文下允许更新
    with effective.authorized_effective_writer():
        db_conn.execute(
            "UPDATE questions SET eff_base_elo = 1200 WHERE question_uid = 'primary_olympiad:ch01:q1';"
        )
        db_conn.commit()

    row = db_conn.execute(
        "SELECT eff_base_elo FROM questions WHERE question_uid = 'primary_olympiad:ch01:q1';"
    ).fetchone()
    assert row["eff_base_elo"] == 1200


def test_recompute_and_override_lifecycle(db_conn):
    uid = "primary_olympiad:ch01:q1"
    _seed_sample_question(db_conn, uid)

    # 初次重算生效层
    effective.recompute(db_conn, uid)
    db_conn.commit()

    q = db_conn.execute("SELECT * FROM questions WHERE question_uid = ?", (uid,)).fetchone()
    assert q["eff_base_elo"] == 1120
    assert q["eff_tier"] == "L1_基础直达"
    assert q["eff_is_servable"] == 1
    assert q["eff_verification_status"] == "VERIFIED_CONSENSUS"

    # FTS5 同步验证
    fts = db_conn.execute(
        "SELECT * FROM questions_fts WHERE question_uid = ?", (uid,)
    ).fetchone()
    assert fts is not None
    assert fts["question_text"] == "1+1=?"

    # 写入人工覆写：修改四维难度为 C=3, S=3, T=3, L=1 (应当得到 L4-B)
    override_val = json.dumps({"C": 3, "S": 3, "T": 3, "L": 1})
    db_conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, reason, source,
            base_fingerprint, is_active
        )
        VALUES (?, 'difficulty_override_json', ?, '手工调难度', 'PI_CLI', 'hash_v1', 1);
        """,
        (uid, override_val),
    )
    db_conn.commit()

    effective.recompute(db_conn, uid)
    db_conn.commit()

    q2 = db_conn.execute("SELECT * FROM questions WHERE question_uid = ?", (uid,)).fetchone()
    assert q2["eff_c_score"] == 3
    assert q2["eff_base_elo"] == 2070
    assert q2["eff_tier"] == "L4-B_进阶拔高(巧)"
    assert "difficulty_override_json" in json.loads(q2["eff_overridden_fields_json"])

    # 测试 v2.6 分组指纹策略：标注类 difficulty_override_json 解绑 content_hash，
    # 源题 content_hash 变更为 hash_v2 时不得误杀黄金锚点/难度覆写
    db_conn.execute("UPDATE raw_questions SET content_hash = 'hash_v2' WHERE question_uid = ?", (uid,))
    db_conn.commit()

    effective.recompute(db_conn, uid)
    db_conn.commit()

    ov_ann = db_conn.execute(
        "SELECT is_suspended, suspended_reason FROM human_overrides WHERE question_uid = ? AND field_name = 'difficulty_override_json'",
        (uid,),
    ).fetchone()
    assert ov_ann["is_suspended"] == 0
    assert ov_ann["suspended_reason"] == ""

    q3 = db_conn.execute("SELECT * FROM questions WHERE question_uid = ?", (uid,)).fetchone()
    assert q3["eff_base_elo"] == 2070
    assert q3["eff_tier"] == "L4-B_进阶拔高(巧)"

    # 验真类 question_text 强绑 content_hash：携带旧指纹 hash_v1 时自动挂起为 SOURCE_CHANGED
    db_conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, reason, source,
            base_fingerprint, is_active
        )
        VALUES (?, 'question_text', '旧版人工题干', '微调', 'REVIEW_UI', 'hash_v1', 1);
        """,
        (uid,),
    )
    db_conn.commit()

    effective.recompute(db_conn, uid)
    db_conn.commit()

    ov_ver = db_conn.execute(
        "SELECT is_suspended, suspended_reason FROM human_overrides WHERE question_uid = ? AND field_name = 'question_text'",
        (uid,),
    ).fetchone()
    assert ov_ver["is_suspended"] == 1
    assert ov_ver["suspended_reason"] == "SOURCE_CHANGED"

    q4 = db_conn.execute("SELECT * FROM questions WHERE question_uid = ?", (uid,)).fetchone()
    assert q4["eff_verification_status"] == "NEEDS_REVIEW"
    assert "OVERRIDE_STALE" in q4["eff_review_reason"]
    assert q4["eff_is_servable"] == 0


def test_recompute_all_and_verify_consistency(db_conn):
    _seed_sample_question(db_conn, "primary_olympiad:ch01:q1")
    _seed_sample_question(db_conn, "primary_olympiad:ch01:q2")

    count = effective.recompute_all(db_conn)
    assert count == 2

    res = effective.verify_effective_consistency(db_conn)
    assert res["total_checked"] == 2
    assert res["diff_count"] == 0
    assert len(res["diffs"]) == 0
