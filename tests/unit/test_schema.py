"""测试 20 表 + 1 FTS5 虚表 DDL、外键约束、CHECK 约束与 bootstrap 顺序。"""

import sqlite3

import pytest

from shuati.db.connection import get_connection
from shuati.db.schema import SCHEMA_VERSION, init_db


def test_schema_tables_and_fts_creation(tmp_path):
    db_file = tmp_path / "test_shuati.db"
    conn = get_connection(db_file)
    init_db(conn)

    # 验证 user_version
    cur = conn.execute("PRAGMA user_version;")
    assert cur.fetchone()[0] == SCHEMA_VERSION

    # 验证全量 20 张核心表存在
    expected_tables = {
        "grade_subjects",
        "raw_questions",
        "stage1_vlm_runs",
        "questions",
        "compile_state",
        "question_solutions",
        "question_steps",
        "human_overrides",
        "knowledge_nodes",
        "knowledge_edges",
        "taxonomy_candidates",
        "compaction_audit_ledger",
        "concepts",
        "question_concepts",
        "question_relations",
        "relation_overrides",
        "learners",
        "quiz_batches",
        "practice_log",
        "llm_calls",
    }
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table';")
    tables = {row[0] for row in cur.fetchall()}
    assert expected_tables.issubset(tables), f"缺失表: {expected_tables - tables}"

    # 验证 FTS5 trigram 虚表存在
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'questions_fts';")
    assert cur.fetchone() is not None, "未找到 questions_fts 虚表"


def test_schema_bootstrap_order(tmp_path):
    db_file = tmp_path / "test_bootstrap.db"
    conn = get_connection(db_file)
    init_db(conn)

    # Step 1: grade_subjects 种子
    gs = conn.execute("SELECT * FROM grade_subjects WHERE slug = 'primary_olympiad';").fetchone()
    assert gs is not None
    assert gs["grade_subject"] == "小学奥数"
    assert gs["subject_family"] == "MATH"

    # Step 2: 6 个预置 TRAP 概念
    traps = conn.execute("SELECT * FROM concepts WHERE kind = 'TRAP' AND is_seed = 1;").fetchall()
    assert len(traps) == 6
    trap_names = {t["name"] for t in traps}
    assert "TRAP_DOMAIN_BOUNDARY" in trap_names
    assert "TRAP_ARITHMETIC_SLIP" in trap_names

    # Step 3: learners 默认角色
    lrn = conn.execute("SELECT * FROM learners WHERE learner_id = 'default';").fetchone()
    assert lrn is not None
    assert lrn["display_name"] == "默认"
    assert lrn["is_current"] == 1


def test_foreign_key_and_check_constraints(tmp_path):
    db_file = tmp_path / "test_constraints.db"
    conn = get_connection(db_file)
    init_db(conn)

    # 外键约束测试：插入不存在的 grade_subject
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection,
                source_id, raw_question, content_hash
            )
            VALUES ('invalid:uid', '不存在的学科', 'BATCH_JSONL', 'ch01', 'q1', 'test', 'hash1');
            """
        )

    # CHECK 约束测试：questions 表 m_c_score 必须在 1..4
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        )
        VALUES ('primary_olympiad:ch01:q1', '小学奥数', 'BATCH_JSONL', 'ch01', 'q1', 'test', 'hash1');
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_format, m_c_score
            )
            VALUES ('primary_olympiad:ch01:q1', '小学奥数', 'test', '解答题', 5);
            """
        )

    # 正常插入
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format, m_c_score, m_s_score, m_t_score, m_l_score
        )
        VALUES ('primary_olympiad:ch01:q1', '小学奥数', 'test', '解答题', 2, 2, 2, 2);
        """
    )
    q = conn.execute("SELECT * FROM questions WHERE question_uid = 'primary_olympiad:ch01:q1';").fetchone()
    assert q["m_c_score"] == 2
