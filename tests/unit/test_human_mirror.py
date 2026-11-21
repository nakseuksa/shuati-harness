"""测试 db/human_mirror.py 的全表快照导出、幂等 UPSERT 恢复、空库恢复与 VACUUM INTO 备份。"""

import json

import pytest

from shuati.db import human_mirror
from shuati.db.connection import get_connection
from shuati.db.schema import init_db


@pytest.fixture
def populated_db(tmp_path):
    db_file = tmp_path / "populated.db"
    conn = get_connection(db_file)
    init_db(conn)

    # 插入前置题目
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        )
        VALUES ('primary_olympiad:ch01:q1', '小学奥数', 'BATCH_JSONL', 'ch01', 'q1', '1+1=?', 'hash1');
        """
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format
        )
        VALUES ('primary_olympiad:ch01:q1', '小学奥数', '1+1=?', '解答题');
        """
    )
    # 插入人工覆写
    conn.execute(
        """
        INSERT INTO human_overrides(
            override_id, question_uid, field_name, override_value, reason,
            source, base_fingerprint, is_active
        )
        VALUES (1, 'primary_olympiad:ch01:q1', 'question_text', '1+1=多少？', '润色', 'PI_CLI', 'hash1', 1);
        """
    )
    # 插入练习记录 (含无 quiz_batches 的 quiz_batch_id 软引用)
    conn.execute(
        """
        INSERT INTO practice_log(
            log_id, learner_id, question_uid, outcome, quiz_batch_id, client_request_id
        )
        VALUES (100, 'default', 'primary_olympiad:ch01:q1', 'SOLVED_SOLO', 'qz_non_existent_batch', 'req_001');
        """
    )
    conn.commit()
    return conn, db_file


def test_export_and_verify_snapshot(populated_db, tmp_path):
    conn, _ = populated_db
    human_dir = tmp_path / "human"

    counts = human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=True)
    assert counts["learners"] >= 1
    assert counts["human_overrides"] == 1
    assert counts["practice_log"] == 1

    # 验证导出的 JSON 键已排序
    overrides_file = human_dir / "overrides.jsonl"
    assert overrides_file.exists()
    line = overrides_file.read_text(encoding="utf-8").strip()
    data = json.loads(line)
    assert data["field_name"] == "question_text"

    # 校验一致性
    ver = human_mirror.verify_snapshot(conn, human_dir=human_dir, include_practice_log=True)
    assert ver["is_consistent"] is True
    assert ver["tables"]["human_overrides"]["match"] is True
    assert ver["tables"]["practice_log"]["match"] is True


def test_restore_idempotent_upsert_on_existing_db(populated_db, tmp_path):
    conn, _ = populated_db
    human_dir = tmp_path / "human"
    human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=True)

    # 重复恢复多次，断言不触发 FOREIGN KEY 约束报错（严格依赖 UPSERT 而非 INSERT OR REPLACE）
    counts1 = human_mirror.restore_snapshot(conn, human_dir=human_dir, backups_dir=tmp_path / "backups")
    assert counts1["human_overrides"] == 1
    assert counts1["practice_log"] == 1

    counts2 = human_mirror.restore_snapshot(conn, human_dir=human_dir, backups_dir=tmp_path / "backups")
    assert counts2["human_overrides"] == 1


def test_restore_on_empty_db_with_soft_quiz_batch_ref(populated_db, tmp_path):
    conn, _ = populated_db
    human_dir = tmp_path / "human"
    human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=True)

    # 建立全新空库
    empty_db = tmp_path / "empty.db"
    new_conn = get_connection(empty_db)
    init_db(new_conn)

    # 空库先补上 raw_questions / questions，但不建 quiz_batches
    new_conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        )
        VALUES ('primary_olympiad:ch01:q1', '小学奥数', 'BATCH_JSONL', 'ch01', 'q1', '1+1=?', 'hash1');
        """
    )
    new_conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format
        )
        VALUES ('primary_olympiad:ch01:q1', '小学奥数', '1+1=?', '解答题');
        """
    )
    new_conn.commit()

    # 在空库上执行 restore
    counts = human_mirror.restore_snapshot(new_conn, human_dir=human_dir, backups_dir=tmp_path / "backups")
    assert counts["human_overrides"] == 1
    assert counts["practice_log"] == 1

    # 确认 practice_log 包含软引用的 quiz_batch_id 且未报错
    pl = new_conn.execute("SELECT * FROM practice_log WHERE log_id = 100;").fetchone()
    assert pl is not None
    assert pl["quiz_batch_id"] == "qz_non_existent_batch"


def test_vacuum_backup_rotation(populated_db, tmp_path):
    conn, _ = populated_db
    backups_dir = tmp_path / "backups"

    created = []
    for i in range(12):
        # 模拟多次备份
        p = human_mirror.vacuum_backup(conn, backups_dir, f"cmd_{i}", keep=5)
        created.append(p)

    remaining = list(backups_dir.glob("*.db"))
    # 最多保留 5 份
    assert len(remaining) <= 5
