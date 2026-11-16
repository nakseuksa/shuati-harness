"""P10-A grade_settings 服务测试：create / list / rename / delete / get_or_create。

契约 docs/tasks/P10-A_grade_schema_settings.md：幂等创建 / 计数列表（含 0 题条目）/
rename 五处单事务级联改标（冲突回滚无残留）/ delete 有题目拒绝（携带计数）/
get_or_create 规范化幂等。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.db.schema import get_db_connection
from backend.services.grade_settings import (
    GradeSubjectInUseError,
    GradeSubjectItem,
    create_grade_subject,
    delete_grade_subject,
    get_or_create_grade_subject,
    list_grade_subjects,
    rename_grade_subject,
)

_GRADE_A = "小学奥数"
_GRADE_B = "高中数学-高一"

_SCOPED_TABLES = (
    "knowledge_nodes",
    "elo_mutation_ledger",
    "compaction_audit_ledger",
    "concept_co_errors",
)

_INSERT_QUESTION_SQL = """
INSERT INTO questions (
    question_uid, origin_type, target_grade_and_subject, stem_md, stem_hash,
    standard_answer_latex, c_score, s_score, t_score, l_score, base_elo, tier,
    primary_l1_module
) VALUES (?, 'USER_CANONICAL', ?, '题干', 'hash', 'x=1',
          1, 1, 1, 1, 1000, 'L1_基础直达', '代数')
"""

_INSERT_NODE_SQL = """
INSERT INTO knowledge_nodes (node_id, l1_module, l2_entity, l3_operator, grade_subject)
VALUES (?, '代数', '函数与方程', '换元转化', ?)
"""

_INSERT_LEDGER_SQL = """
INSERT INTO elo_mutation_ledger (
    idempotency_key, l1_module, question_uid, mutation_trigger, eval_difficulty_elo,
    coverage_elo_before, coverage_elo_after, combat_elo_before, combat_elo_after,
    explanation_text, grade_subject
) VALUES (?, '代数', ?, 'MASTERED_INIT', 1000, 1200.0, 1224.0, 1200.0, 1224.0, '解释', ?)
"""

_INSERT_AUDIT_SQL = """
INSERT INTO compaction_audit_ledger (
    batch_id, l1_module, l2_entity, from_node_id, to_node_id, ai_judge_evidence,
    previous_aliases_to_json, previous_co_errors_snapshot_json, grade_subject
) VALUES ('batch-1', '代数', '函数与方程', ?, ?, '证据', '[]', '[]', ?)
"""

_INSERT_CO_ERROR_SQL = """
INSERT INTO concept_co_errors (
    node_u_id, node_v_id, l1_module_u, l1_module_v, source_question_uids_json, grade_subject
) VALUES (?, ?, '代数', '代数', '[]', ?)
"""


@pytest.fixture()
def seeded_db(tmp_db: Path) -> Path:
    """两学段分区数据 + 条目：A（2 题/2 节点/1 账本/1 审计/1 边），B（1 题/1 节点）。"""
    with get_db_connection(tmp_db) as conn:
        conn.execute(_INSERT_QUESTION_SQL, ("q-a1", _GRADE_A))
        conn.execute(_INSERT_QUESTION_SQL, ("q-a2", _GRADE_A))
        conn.execute(_INSERT_NODE_SQL, ("n-a1", _GRADE_A))
        conn.execute(_INSERT_NODE_SQL, ("n-a2", _GRADE_A))
        conn.execute(_INSERT_LEDGER_SQL, ("q-a1:sess:MASTERED_INIT", "q-a1", _GRADE_A))
        conn.execute(_INSERT_AUDIT_SQL, ("n-a1", "n-a2", _GRADE_A))
        conn.execute(_INSERT_CO_ERROR_SQL, ("n-a1", "n-a2", _GRADE_A))
        conn.execute(_INSERT_QUESTION_SQL, ("q-b1", _GRADE_B))
        conn.execute(_INSERT_NODE_SQL, ("n-b1", _GRADE_B))
        conn.execute(
            "INSERT INTO grade_subjects (grade_subject) VALUES (?), (?)", (_GRADE_A, _GRADE_B)
        )
    return tmp_db


# ---------------------------------------------------------------------------
# create / get_or_create
# ---------------------------------------------------------------------------


def test_create_new_then_idempotent(tmp_db: Path) -> None:
    """新建返回 (新项, True)；重复创建幂等返回 (既有项, False)，不产生重复行。"""
    with get_db_connection(tmp_db) as conn:
        item, created = create_grade_subject(conn, _GRADE_A)
        assert created is True
        assert isinstance(item, GradeSubjectItem)
        assert item.grade_subject == _GRADE_A
        assert item.question_count == 0
        assert item.created_at
        again, created_again = create_grade_subject(conn, _GRADE_A)
        assert created_again is False
        assert again == item
        count = conn.execute("SELECT COUNT(*) AS n FROM grade_subjects").fetchone()["n"]
    assert count == 1


def test_create_strips_and_normalizes(tmp_db: Path) -> None:
    """名称 strip 规范化：带空白创建与规范名创建命中同一条目。"""
    with get_db_connection(tmp_db) as conn:
        item, created = create_grade_subject(conn, f"  {_GRADE_A}  ")
        assert created is True
        assert item.grade_subject == _GRADE_A
        _, created_again = create_grade_subject(conn, _GRADE_A)
        assert created_again is False


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_create_rejects_blank(tmp_db: Path, blank: str) -> None:
    """空/纯空白名称 → ValueError。"""
    with pytest.raises(ValueError):
        with get_db_connection(tmp_db) as conn:
            create_grade_subject(conn, blank)


def test_create_counts_preexisting_questions(tmp_db: Path) -> None:
    """题目可能先于条目落库：创建时计数实时统计而非硬编码 0。"""
    with get_db_connection(tmp_db) as conn:
        conn.execute(_INSERT_QUESTION_SQL, ("q-a1", _GRADE_A))
        item, created = create_grade_subject(conn, _GRADE_A)
    assert created is True
    assert item.question_count == 1


def test_get_or_create_new_existing_and_idempotent(tmp_db: Path) -> None:
    """get_or_create：新建/既有均返回规范化名称，且不产生重复条目。"""
    with get_db_connection(tmp_db) as conn:
        assert get_or_create_grade_subject(conn, f"  {_GRADE_A} ") == _GRADE_A
        assert get_or_create_grade_subject(conn, _GRADE_A) == _GRADE_A
        count = conn.execute("SELECT COUNT(*) AS n FROM grade_subjects").fetchone()["n"]
    assert count == 1


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_get_or_create_rejects_blank(tmp_db: Path, blank: str) -> None:
    """空/纯空白名称 → ValueError。"""
    with pytest.raises(ValueError):
        with get_db_connection(tmp_db) as conn:
            get_or_create_grade_subject(conn, blank)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_counts_include_zero_and_order_by_created_at(seeded_db: Path) -> None:
    """LEFT JOIN 计数正确（含 0 题条目），按 created_at 升序返回。"""
    with get_db_connection(seeded_db) as conn:
        create_grade_subject(conn, "初中物理")  # 0 题条目
        conn.execute(
            "UPDATE grade_subjects SET created_at = '2026-01-02 00:00:00'"
            " WHERE grade_subject = ?",
            (_GRADE_A,),
        )
        conn.execute(
            "UPDATE grade_subjects SET created_at = '2026-01-01 00:00:00'"
            " WHERE grade_subject = ?",
            (_GRADE_B,),
        )
        conn.execute(
            "UPDATE grade_subjects SET created_at = '2026-01-03 00:00:00'"
            " WHERE grade_subject = '初中物理'"
        )
        items = list_grade_subjects(conn)
    assert [(i.grade_subject, i.question_count) for i in items] == [
        (_GRADE_B, 1),
        (_GRADE_A, 2),
        ("初中物理", 0),
    ]


def test_list_same_second_tie_breaks_by_name(tmp_db: Path) -> None:
    """created_at 秒级同秒条目以名称决序，返回稳定。"""
    with get_db_connection(tmp_db) as conn:
        create_grade_subject(conn, "学段B")
        create_grade_subject(conn, "学段A")
        conn.execute("UPDATE grade_subjects SET created_at = '2026-01-01 00:00:00'")
        items = list_grade_subjects(conn)
    assert [i.grade_subject for i in items] == ["学段A", "学段B"]


# ---------------------------------------------------------------------------
# rename（五处单事务级联改标）
# ---------------------------------------------------------------------------


def test_rename_cascades_all_five_places(seeded_db: Path) -> None:
    """条目主键 + questions + 四分区表全部改标；行数返回正确；另一学段分毫不动。"""
    new_name = "小学奥数-竞赛"
    with get_db_connection(seeded_db) as conn:
        counts = rename_grade_subject(conn, _GRADE_A, new_name)
    assert counts == {
        "grade_subjects": 1,
        "questions": 2,
        "knowledge_nodes": 2,
        "elo_mutation_ledger": 1,
        "compaction_audit_ledger": 1,
        "concept_co_errors": 1,
    }
    with get_db_connection(seeded_db) as conn:
        entries = {
            row["grade_subject"]
            for row in conn.execute("SELECT grade_subject FROM grade_subjects")
        }
        assert entries == {new_name, _GRADE_B}
        q_count = conn.execute(
            "SELECT COUNT(*) AS n FROM questions WHERE target_grade_and_subject = ?",
            (new_name,),
        ).fetchone()["n"]
        assert q_count == 2
        expected_new = {
            "knowledge_nodes": 2,
            "elo_mutation_ledger": 1,
            "compaction_audit_ledger": 1,
            "concept_co_errors": 1,
        }
        for table, n in expected_new.items():
            new_count = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE grade_subject = ?", (new_name,)
            ).fetchone()["n"]
            old_count = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE grade_subject = ?", (_GRADE_A,)
            ).fetchone()["n"]
            assert new_count == n, table
            assert old_count == 0, table
        # 学段 B 各表不受影响
        q_b = conn.execute(
            "SELECT target_grade_and_subject FROM questions WHERE question_uid = 'q-b1'"
        ).fetchone()[0]
        n_b = conn.execute(
            "SELECT grade_subject FROM knowledge_nodes WHERE node_id = 'n-b1'"
        ).fetchone()[0]
    assert q_b == _GRADE_B
    assert n_b == _GRADE_B


def test_rename_empty_grade_zero_counts(tmp_db: Path) -> None:
    """空条目改标：条目 1 行，其余各表 0 行。"""
    with get_db_connection(tmp_db) as conn:
        create_grade_subject(conn, "初中物理")
        counts = rename_grade_subject(conn, "初中物理", "初中化学")
    assert counts == {
        "grade_subjects": 1,
        "questions": 0,
        "knowledge_nodes": 0,
        "elo_mutation_ledger": 0,
        "compaction_audit_ledger": 0,
        "concept_co_errors": 0,
    }


def test_rename_conflict_rolls_back_whole_transaction(seeded_db: Path) -> None:
    """new 已存在 → ValueError；调用方同事务的其他写入一并回滚，五处零残留。"""
    with pytest.raises(ValueError, match="已存在"):
        with get_db_connection(seeded_db) as conn:
            conn.execute(_INSERT_QUESTION_SQL, ("q-marker", _GRADE_A))
            rename_grade_subject(conn, _GRADE_A, _GRADE_B)
    with get_db_connection(seeded_db) as conn:
        marker = conn.execute(
            "SELECT 1 FROM questions WHERE question_uid = 'q-marker'"
        ).fetchone()
        entries = {
            row["grade_subject"]
            for row in conn.execute("SELECT grade_subject FROM grade_subjects")
        }
        q_count = conn.execute(
            "SELECT COUNT(*) AS n FROM questions WHERE target_grade_and_subject = ?",
            (_GRADE_A,),
        ).fetchone()["n"]
        old_counts = {
            table: conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE grade_subject = ?", (_GRADE_A,)
            ).fetchone()["n"]
            for table in _SCOPED_TABLES
        }
    assert marker is None
    assert entries == {_GRADE_A, _GRADE_B}
    assert q_count == 2
    assert old_counts == {
        "knowledge_nodes": 2,
        "elo_mutation_ledger": 1,
        "compaction_audit_ledger": 1,
        "concept_co_errors": 1,
    }


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("不存在的学段", "新学段"),  # old 不存在
        (_GRADE_A, ""),  # new 为空
        (_GRADE_A, "   "),  # new 纯空白
        (_GRADE_A, _GRADE_A),  # new == old → 已存在
    ],
)
def test_rename_invalid_args_raise(seeded_db: Path, old: str, new: str) -> None:
    """old 不存在 / new 为空 / new 已存在 → ValueError。"""
    with pytest.raises(ValueError):
        with get_db_connection(seeded_db) as conn:
            rename_grade_subject(conn, old, new)


def test_rename_syncs_frontmatter_without_touching_body_or_hash(tmp_db: Path) -> None:
    """P11-C：改名同步改写 stem_md 顶部 frontmatter 学段，不误改正文、不改 stem_hash/counts。"""
    old_grade = _GRADE_A
    new_grade = "小学奥数-竞赛"
    body_text = (
        f"已知题目属性文本 target_grade_and_subject: {old_grade} 应原样保留。\n"
        f"---\ntarget_grade_and_subject: {old_grade}\n---\n"
    )
    stem_a = (
        f"---\nquestion_uid: uid-a1\ntarget_grade_and_subject: {old_grade}\n---\n\n{body_text}"
    )
    stem_a_dots = (
        f"---\nquestion_uid: uid-a2\ntarget_grade_and_subject: {old_grade}\n...\n\n纯正文"
    )
    stem_b = (
        f"---\nquestion_uid: uid-b1\ntarget_grade_and_subject: {_GRADE_B}\n---\n\n其他学段正文"
    )
    with get_db_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO grade_subjects (grade_subject) VALUES (?), (?)",
            (old_grade, _GRADE_B),
        )
        for uid, grade, stem, shash in (
            ("uid-a1", old_grade, stem_a, "hash-a1"),
            ("uid-a2", old_grade, stem_a_dots, "hash-a2"),
            ("uid-b1", _GRADE_B, stem_b, "hash-b1"),
        ):
            conn.execute(
                "INSERT INTO questions ("
                "question_uid, origin_type, target_grade_and_subject, stem_md, stem_hash, "
                "standard_answer_latex, c_score, s_score, t_score, l_score, base_elo, tier, "
                "primary_l1_module) VALUES (?, 'USER_CANONICAL', ?, ?, ?, 'x=1', "
                "1, 1, 1, 1, 1000, 'L1_基础直达', '代数')",
                (uid, grade, stem, shash),
            )
        counts = rename_grade_subject(conn, old_grade, new_grade)

    assert counts["questions"] == 2
    with get_db_connection(tmp_db) as conn:
        rows = {
            r["question_uid"]: r
            for r in conn.execute(
                "SELECT question_uid, target_grade_and_subject, stem_md, stem_hash FROM questions"
            ).fetchall()
        }
    assert rows["uid-a1"]["target_grade_and_subject"] == new_grade
    assert rows["uid-a1"]["stem_hash"] == "hash-a1"
    assert rows["uid-a1"]["stem_md"] == (
        f"---\nquestion_uid: uid-a1\ntarget_grade_and_subject: {new_grade}\n---\n\n{body_text}"
    )
    assert rows["uid-a2"]["target_grade_and_subject"] == new_grade
    assert rows["uid-a2"]["stem_hash"] == "hash-a2"
    assert rows["uid-a2"]["stem_md"] == (
        f"---\nquestion_uid: uid-a2\ntarget_grade_and_subject: {new_grade}\n...\n\n纯正文"
    )
    assert rows["uid-b1"]["target_grade_and_subject"] == _GRADE_B
    assert rows["uid-b1"]["stem_md"] == stem_b


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_in_use_raises_with_count(seeded_db: Path) -> None:
    """有题目 → GradeSubjectInUseError（携带名称与计数），条目保留。"""
    with pytest.raises(GradeSubjectInUseError) as exc_info:
        with get_db_connection(seeded_db) as conn:
            delete_grade_subject(conn, _GRADE_A)
    assert exc_info.value.grade_subject == _GRADE_A
    assert exc_info.value.question_count == 2
    with get_db_connection(seeded_db) as conn:
        entry = conn.execute(
            "SELECT 1 FROM grade_subjects WHERE grade_subject = ?", (_GRADE_A,)
        ).fetchone()
    assert entry is not None


def test_delete_empty_entry_and_idempotent(seeded_db: Path) -> None:
    """空条目删除成功；重复删除不存在条目幂等无错。"""
    with get_db_connection(seeded_db) as conn:
        create_grade_subject(conn, "初中物理")
        delete_grade_subject(conn, "初中物理")
        delete_grade_subject(conn, "初中物理")  # 第二次：不存在 → 零行幂等
        names = {
            row["grade_subject"]
            for row in conn.execute("SELECT grade_subject FROM grade_subjects")
        }
    assert names == {_GRADE_A, _GRADE_B}


def test_delete_rejects_blank(tmp_db: Path) -> None:
    """空/纯空白名称 → ValueError（与 create/rename 一致的规范化校验）。"""
    with pytest.raises(ValueError):
        with get_db_connection(tmp_db) as conn:
            delete_grade_subject(conn, "   ")