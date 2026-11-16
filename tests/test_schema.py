"""P1-A Schema 基座测试：建表完整性 / PRAGMA / 约束拦截 / 幂等 / 默认值 / 事务语义。

P10-A 追加：grade_subjects 表结构 / 四表加列 / 学段回填迁移（幂等 / 跨学段违约 /
redirect 继承 / 自动注册），契约 docs/tasks/P10-A_grade_schema_settings.md。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from backend.config import settings
from backend.db.schema import (
    TABLE_NAMES,
    _migrate_grade_scoping,
    get_db_connection,
    init_db,
)

# 最小合法 questions 行（其余字段走 DEFAULT）
_INSERT_QUESTION_SQL = """
INSERT INTO questions (
    question_uid, origin_type, target_grade_and_subject, stem_md, stem_hash,
    standard_answer_latex, c_score, s_score, t_score, l_score, base_elo, tier,
    primary_l1_module
) VALUES (?, 'USER_CANONICAL', '高中数学-高三', '题干', 'hash', 'x=1',
          ?, 1, 1, 1, 1000, 'L1_基础直达', '代数')
"""

_INSERT_NODE_SQL = """
INSERT INTO knowledge_nodes (node_id, l1_module, l2_entity, l3_operator)
VALUES (?, '代数', '函数与方程', '换元转化')
"""

_INSERT_STEP_SQL = """
INSERT INTO question_steps (
    step_id, question_uid, step_index, step_goal_desc, step_formula_latex,
    knowledge_node_id, masked_conditions_json, probe_question_md,
    c_sub, s_sub, t_sub, l_sub, sub_elo
) VALUES (?, ?, 1, '子目标', 'x=1', ?, '[]', '反问句', 1, 1, 1, 1, 850)
"""

_INSERT_CO_ERROR_SQL = """
INSERT INTO concept_co_errors (
    node_u_id, node_v_id, l1_module_u, l1_module_v, source_question_uids_json
) VALUES (?, ?, '代数', '代数', '["q-1"]')
"""


def _insert_question(conn: sqlite3.Connection, uid: str, c_score: int = 1) -> None:
    """插入一条最小合法题目记录。"""
    conn.execute(_INSERT_QUESTION_SQL, (uid, c_score))


def _insert_node(conn: sqlite3.Connection, node_id: str) -> None:
    """插入一条最小合法 L3 知识节点。"""
    conn.execute(_INSERT_NODE_SQL, (node_id,))


def test_all_core_and_extension_tables_exist(tmp_db: Path) -> None:
    """9 张核心表 + 2 张 PM 批准扩展表（blind_drill_attempts / grade_subjects）全部存在。"""
    with get_db_connection(tmp_db) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert {row["name"] for row in rows} == set(TABLE_NAMES)
    # P10-A 预授权期望演进：+ grade_subjects（10 → 11）
    assert len(TABLE_NAMES) == 11


def test_required_indexes_exist(tmp_db: Path) -> None:
    """契约要求的最小结构化索引覆盖集全部建立。"""
    expected = {
        "idx_questions_mastery_state",
        "idx_questions_primary_l1_module",
        "idx_questions_blind_test_unlock_at",
        "idx_questions_slated_drill_date",
        "idx_question_steps_question_uid",
        "idx_knowledge_nodes_l1_l2",
        "idx_diagnostic_sessions_question_uid",
        "idx_elo_mutation_ledger_l1_module",
        "idx_question_relations_target_type",
        # P10-A 学段学科分区索引
        "idx_questions_target_grade_and_subject",
        "idx_knowledge_nodes_grade_l1_l2",
        "idx_elo_mutation_ledger_grade_l1",
        "idx_concept_co_errors_grade",
        "idx_compaction_audit_ledger_grade",
    }
    with get_db_connection(tmp_db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    assert expected <= {row["name"] for row in rows}


def test_pragmas_enforced(tmp_db: Path) -> None:
    """每次连接强制 WAL / foreign_keys=ON / busy_timeout=5000，且 row_factory 为 Row。"""
    with get_db_connection(tmp_db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.row_factory is sqlite3.Row


def test_foreign_key_enforcement_on_question_steps(tmp_db: Path) -> None:
    """向 question_steps 插入不存在的 question_uid 必须触发 IntegrityError。"""
    with get_db_connection(tmp_db) as conn:
        _insert_node(conn, "kn-1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT_STEP_SQL, ("step-1", "no-such-question", "kn-1"))


def test_check_rejects_c_score_out_of_range(tmp_db: Path) -> None:
    """c_score=5 超出 [1,4] 必须被 CHECK 拦截。"""
    with get_db_connection(tmp_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_question(conn, "q-bad-c", c_score=5)


def test_check_rejects_co_error_self_loop(tmp_db: Path) -> None:
    """concept_co_errors 自环边 (u == v) 必须被 CHECK 拦截。"""
    with get_db_connection(tmp_db) as conn:
        _insert_node(conn, "n-a")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT_CO_ERROR_SQL, ("n-a", "n-a"))


def test_check_rejects_co_error_wrong_order(tmp_db: Path) -> None:
    """concept_co_errors u > v 的无序边必须被 CHECK 拦截。"""
    with get_db_connection(tmp_db) as conn:
        _insert_node(conn, "n-a")
        _insert_node(conn, "n-b")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT_CO_ERROR_SQL, ("n-b", "n-a"))


def test_co_error_valid_order_accepted(tmp_db: Path) -> None:
    """u < v 的合法标红边可正常写入（正向对照，确认 CHECK 未误伤）。"""
    with get_db_connection(tmp_db) as conn:
        _insert_node(conn, "n-a")
        _insert_node(conn, "n-b")
        conn.execute(_INSERT_CO_ERROR_SQL, ("n-a", "n-b"))
        row = conn.execute(
            "SELECT active_error_count, total_historical_count FROM concept_co_errors"
        ).fetchone()
    assert row["active_error_count"] == 1
    assert row["total_historical_count"] == 1


def test_init_db_idempotent_and_preserves_data(tmp_db: Path) -> None:
    """连续两次 init_db 不报错且既有数据不丢失。"""
    with get_db_connection(tmp_db) as conn:
        _insert_question(conn, "q-keep")
    init_db(tmp_db)  # tmp_db 夹具已初始化过一次，此处为第二次
    with get_db_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT question_uid FROM questions WHERE question_uid='q-keep'"
        ).fetchone()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert row is not None
    assert {t["name"] for t in tables} == set(TABLE_NAMES)


def test_init_db_default_path_and_parent_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省路径取 settings.SQLITE_PATH，且自动创建多级父目录。"""
    target = tmp_path / "nested" / "dir" / "harness.db"
    monkeypatch.setattr(settings, "SQLITE_PATH", target)
    init_db()
    assert target.exists()
    with get_db_connection() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert {t["name"] for t in tables} == set(TABLE_NAMES)


def test_default_mastery_and_verification_status(tmp_db: Path) -> None:
    """questions 关键默认值：mastery_state='ACTIVE'、verification_status='HUMAN_OR_CANONICAL'。"""
    with get_db_connection(tmp_db) as conn:
        _insert_question(conn, "q-1")
        row = conn.execute(
            "SELECT mastery_state, verification_status, trap_tags_json,"
            " system_deferred_days, slated_exposure_count FROM questions WHERE question_uid='q-1'"
        ).fetchone()
    assert row["mastery_state"] == "ACTIVE"
    assert row["verification_status"] == "HUMAN_OR_CANONICAL"
    assert row["trap_tags_json"] == "[]"
    assert row["system_deferred_days"] == 0
    assert row["slated_exposure_count"] == 0


def test_default_escrow_decay_factor(tmp_db: Path) -> None:
    """question_escrow_pool 关键默认值：decay_factor=1.0 及零增量初始态。"""
    with get_db_connection(tmp_db) as conn:
        _insert_question(conn, "q-1")
        conn.execute(
            "INSERT INTO question_escrow_pool"
            " (question_uid, l1_module, initial_combat_elo_snapshot, escrow_status)"
            " VALUES ('q-1', '代数', 1200.0, 'FLOOR_ONLY')"
        )
        row = conn.execute(
            "SELECT decay_factor, floor_elo_applied, escrow_combat_delta, is_cov_settled"
            " FROM question_escrow_pool WHERE question_uid='q-1'"
        ).fetchone()
    assert row["decay_factor"] == 1.0
    assert row["floor_elo_applied"] == 0.0
    assert row["escrow_combat_delta"] == 0.0
    assert row["is_cov_settled"] == 0


def test_default_micro_stage(tmp_db: Path) -> None:
    """diagnostic_sessions 关键默认值：micro_stage='STAGE_0_CONFIRM_GATE' 及初始状态机字段。"""
    with get_db_connection(tmp_db) as conn:
        _insert_question(conn, "q-1")
        conn.execute(
            "INSERT INTO diagnostic_sessions"
            " (session_id, question_uid, draft_image_cas_uris_json,"
            "  draft_steps_latex_json, error_category)"
            " VALUES ('sess-1', 'q-1', '[]', '[]', 'CONCEPT_BOTTLENECK')"
        )
        row = conn.execute(
            "SELECT micro_stage, matched_branch_id, last_valid_step_index,"
            " stalled_step_index, user_confirmed_gate, prewarm_status"
            " FROM diagnostic_sessions WHERE session_id='sess-1'"
        ).fetchone()
    assert row["micro_stage"] == "STAGE_0_CONFIRM_GATE"
    assert row["matched_branch_id"] == "canonical"
    assert row["last_valid_step_index"] == 0
    assert row["stalled_step_index"] == 1
    assert row["user_confirmed_gate"] == 0
    assert row["prewarm_status"] == "IDLE"


def test_connection_commits_on_normal_exit(tmp_db: Path) -> None:
    """正常退出上下文管理器时提交写入。"""
    with get_db_connection(tmp_db) as conn:
        _insert_question(conn, "q-commit")
    with get_db_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT question_uid FROM questions WHERE question_uid='q-commit'"
        ).fetchone()
    assert row is not None


def test_connection_rolls_back_on_exception(tmp_db: Path) -> None:
    """异常路径回滚：块内写入不得落库，且不影响此前已提交数据。"""
    with get_db_connection(tmp_db) as conn:
        _insert_question(conn, "q-keep")
    with pytest.raises(RuntimeError, match="boom"):
        with get_db_connection(tmp_db) as conn:
            _insert_question(conn, "q-rollback")
            raise RuntimeError("boom")
    with get_db_connection(tmp_db) as conn:
        kept = conn.execute(
            "SELECT question_uid FROM questions WHERE question_uid='q-keep'"
        ).fetchone()
        rolled = conn.execute(
            "SELECT question_uid FROM questions WHERE question_uid='q-rollback'"
        ).fetchone()
    assert kept is not None
    assert rolled is None


# ---------------------------------------------------------------------------
# P10-A 学段学科分区基座：grade_subjects 表结构 / 四表加列 / 回填迁移
# 契约 docs/tasks/P10-A_grade_schema_settings.md
# ---------------------------------------------------------------------------

_GRADE_SCOPED_TABLES = (
    "knowledge_nodes",
    "elo_mutation_ledger",
    "compaction_audit_ledger",
    "concept_co_errors",
)

_INSERT_QUESTION_GRADED_SQL = """
INSERT INTO questions (
    question_uid, origin_type, target_grade_and_subject, stem_md, stem_hash,
    standard_answer_latex, c_score, s_score, t_score, l_score, base_elo, tier,
    primary_l1_module
) VALUES (?, 'USER_CANONICAL', ?, '题干', 'hash', 'x=1',
          1, 1, 1, 1, 1000, 'L1_基础直达', '代数')
"""

_INSERT_NODE_GRADED_SQL = """
INSERT INTO knowledge_nodes (
    node_id, l1_module, l2_entity, l3_operator, redirect_to_id, grade_subject
) VALUES (?, '代数', '函数与方程', '换元转化', ?, ?)
"""

_INSERT_STEP_INDEXED_SQL = """
INSERT INTO question_steps (
    step_id, question_uid, step_index, step_goal_desc, step_formula_latex,
    knowledge_node_id, masked_conditions_json, probe_question_md,
    c_sub, s_sub, t_sub, l_sub, sub_elo
) VALUES (?, ?, ?, '子目标', 'x=1', ?, '[]', '反问句', 1, 1, 1, 1, 850)
"""

_INSERT_LEDGER_GRADED_SQL = """
INSERT INTO elo_mutation_ledger (
    idempotency_key, l1_module, question_uid, mutation_trigger, eval_difficulty_elo,
    coverage_elo_before, coverage_elo_after, combat_elo_before, combat_elo_after,
    explanation_text, grade_subject
) VALUES (?, '代数', ?, 'MASTERED_INIT', 1000, 1200.0, 1224.0, 1200.0, 1224.0, '解释', ?)
"""

_INSERT_AUDIT_GRADED_SQL = """
INSERT INTO compaction_audit_ledger (
    batch_id, l1_module, l2_entity, from_node_id, to_node_id, ai_judge_evidence,
    previous_aliases_to_json, previous_co_errors_snapshot_json, grade_subject
) VALUES ('batch-1', '代数', '函数与方程', ?, ?, '证据', '[]', '[]', ?)
"""

_INSERT_CO_ERROR_GRADED_SQL = """
INSERT INTO concept_co_errors (
    node_u_id, node_v_id, l1_module_u, l1_module_v, source_question_uids_json, grade_subject
) VALUES (?, ?, '代数', '代数', '[]', ?)
"""


def _seed_legacy_partition_rows(conn: sqlite3.Connection) -> None:
    """种子两学段 legacy 行（grade_subject=''）：节点/步骤/题目/账本/审计/边。

    学段 A「小学奥数」：q-a1/q-a2 → n-a1/n-a2；redirect 子 n-a-child → n-a1，
    二跳 n-a-grandchild → n-a-child；账本 1 条；审计 2 条（含 from redirect 子）；边 1 条。
    学段 B「高中数学-高一」：q-b1 → n-b1/n-b2（同题两步）；账本 1 条；边 1 条。
    另有孤儿节点 n-orphan（无步骤无 redirect，应保持 ''）。
    """
    conn.execute(_INSERT_QUESTION_GRADED_SQL, ("q-a1", "小学奥数"))
    conn.execute(_INSERT_QUESTION_GRADED_SQL, ("q-a2", "小学奥数"))
    conn.execute(_INSERT_QUESTION_GRADED_SQL, ("q-b1", "高中数学-高一"))
    for node_id in ("n-a1", "n-a2", "n-b1", "n-b2", "n-orphan"):
        conn.execute(_INSERT_NODE_GRADED_SQL, (node_id, None, ""))
    conn.execute(_INSERT_NODE_GRADED_SQL, ("n-a-child", "n-a1", ""))
    conn.execute(_INSERT_NODE_GRADED_SQL, ("n-a-grandchild", "n-a-child", ""))
    conn.execute(_INSERT_STEP_INDEXED_SQL, ("s-a1", "q-a1", 1, "n-a1"))
    conn.execute(_INSERT_STEP_INDEXED_SQL, ("s-a2", "q-a2", 1, "n-a2"))
    conn.execute(_INSERT_STEP_INDEXED_SQL, ("s-b1", "q-b1", 1, "n-b1"))
    conn.execute(_INSERT_STEP_INDEXED_SQL, ("s-b2", "q-b1", 2, "n-b2"))
    conn.execute(_INSERT_LEDGER_GRADED_SQL, ("q-a1:sess:MASTERED_INIT", "q-a1", ""))
    conn.execute(_INSERT_LEDGER_GRADED_SQL, ("q-b1:sess:MASTERED_INIT", "q-b1", ""))
    conn.execute(_INSERT_AUDIT_GRADED_SQL, ("n-a1", "n-a2", ""))
    conn.execute(_INSERT_AUDIT_GRADED_SQL, ("n-a-child", "n-a1", ""))
    conn.execute(_INSERT_CO_ERROR_GRADED_SQL, ("n-a1", "n-a2", ""))
    conn.execute(_INSERT_CO_ERROR_GRADED_SQL, ("n-b1", "n-b2", ""))


def _grade_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """四表学段标签 + 条目全量快照（用于重复迁移零变化比对）。"""
    snapshot: dict[str, list[tuple[object, ...]]] = {}
    for table, key in (
        ("knowledge_nodes", "node_id"),
        ("elo_mutation_ledger", "event_id"),
        ("compaction_audit_ledger", "merge_id"),
        ("concept_co_errors", "node_u_id || '|' || node_v_id"),
    ):
        rows = conn.execute(
            f"SELECT {key} AS k, grade_subject FROM {table} ORDER BY k"
        ).fetchall()
        snapshot[table] = [(row["k"], row["grade_subject"]) for row in rows]
    entries = conn.execute(
        "SELECT grade_subject, created_at FROM grade_subjects ORDER BY grade_subject"
    ).fetchall()
    snapshot["grade_subjects"] = [(row["grade_subject"], row["created_at"]) for row in entries]
    return snapshot


def test_grade_subjects_table_structure(tmp_db: Path) -> None:
    """P10-A：grade_subjects 列结构（grade_subject TEXT 主键 + created_at 默认值）。"""
    with get_db_connection(tmp_db) as conn:
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(grade_subjects)")}
    assert set(cols) == {"grade_subject", "created_at"}
    assert cols["grade_subject"]["type"] == "TEXT"
    assert cols["grade_subject"]["pk"] == 1
    assert cols["created_at"]["type"] == "DATETIME"
    assert cols["created_at"]["notnull"] == 1
    assert cols["created_at"]["dflt_value"] == "CURRENT_TIMESTAMP"


def test_grade_subject_columns_added_to_four_tables(tmp_db: Path) -> None:
    """P10-A：四张分区表均含 grade_subject TEXT NOT NULL DEFAULT ''。"""
    with get_db_connection(tmp_db) as conn:
        for table in _GRADE_SCOPED_TABLES:
            cols = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "grade_subject" in cols, table
            assert cols["grade_subject"]["type"] == "TEXT", table
            assert cols["grade_subject"]["notnull"] == 1, table
            assert cols["grade_subject"]["dflt_value"] == "''", table


def test_migration_backfills_legacy_rows_per_table(tmp_db: Path) -> None:
    """P10-A：legacy '' 行逐表按来源回填；redirect 子继承目标学段；DISTINCT 自动注册。"""
    with get_db_connection(tmp_db) as conn:
        _seed_legacy_partition_rows(conn)
    with get_db_connection(tmp_db) as conn:
        _migrate_grade_scoping(conn)
    with get_db_connection(tmp_db) as conn:
        node_grades = {
            row["node_id"]: row["grade_subject"]
            for row in conn.execute("SELECT node_id, grade_subject FROM knowledge_nodes")
        }
        ledger_grades = {
            row["question_uid"]: row["grade_subject"]
            for row in conn.execute("SELECT question_uid, grade_subject FROM elo_mutation_ledger")
        }
        audit_grades = [
            row["grade_subject"]
            for row in conn.execute(
                "SELECT grade_subject FROM compaction_audit_ledger ORDER BY merge_id"
            )
        ]
        edge_grades = {
            (row["node_u_id"], row["node_v_id"]): row["grade_subject"]
            for row in conn.execute(
                "SELECT node_u_id, node_v_id, grade_subject FROM concept_co_errors"
            )
        }
        question_grades = {
            row["question_uid"]: row["target_grade_and_subject"]
            for row in conn.execute("SELECT question_uid, target_grade_and_subject FROM questions")
        }
        registered = conn.execute(
            "SELECT grade_subject, created_at FROM grade_subjects ORDER BY grade_subject"
        ).fetchall()
    assert node_grades == {
        "n-a1": "小学奥数",
        "n-a2": "小学奥数",
        "n-a-child": "小学奥数",  # 无自身步骤的 redirect 子继承目标
        "n-a-grandchild": "小学奥数",  # 二跳链传递继承
        "n-b1": "高中数学-高一",
        "n-b2": "高中数学-高一",
        "n-orphan": "",  # 无步骤且无目标学段 → 保持 ''
    }
    assert ledger_grades == {"q-a1": "小学奥数", "q-b1": "高中数学-高一"}
    assert audit_grades == ["小学奥数", "小学奥数"]  # 第二条 from redirect 子，节点回填后继承
    assert edge_grades == {
        ("n-a1", "n-a2"): "小学奥数",
        ("n-b1", "n-b2"): "高中数学-高一",
    }
    # 迁移纯增量：questions 学段值不变
    assert question_grades == {
        "q-a1": "小学奥数",
        "q-a2": "小学奥数",
        "q-b1": "高中数学-高一",
    }
    # DISTINCT 自动注册：学段 A 两题只注册一条条目
    assert [row["grade_subject"] for row in registered] == ["小学奥数", "高中数学-高一"]
    assert all(row["created_at"] for row in registered)


def test_migration_idempotent_second_run_zero_change(tmp_db: Path) -> None:
    """P10-A：回填完成后重复 init_db / 迁移零变化（含条目 created_at 不被刷新）。"""
    with get_db_connection(tmp_db) as conn:
        _seed_legacy_partition_rows(conn)
    with get_db_connection(tmp_db) as conn:
        _migrate_grade_scoping(conn)
    with get_db_connection(tmp_db) as conn:
        before = _grade_snapshot(conn)
    init_db(tmp_db)  # 第二次完整路径（建表/迁移/索引）
    with get_db_connection(tmp_db) as conn:
        _migrate_grade_scoping(conn)  # 第三次：直接调迁移亦零变化
        after = _grade_snapshot(conn)
    assert after == before


def test_migration_cross_grade_node_raises_and_rolls_back(tmp_db: Path) -> None:
    """P10-A：同一节点挂载两学段题目 → ValueError（绝不静默择一），且无任何部分回填残留。"""
    with get_db_connection(tmp_db) as conn:
        conn.execute(_INSERT_QUESTION_GRADED_SQL, ("q-a1", "小学奥数"))
        conn.execute(_INSERT_QUESTION_GRADED_SQL, ("q-b1", "高中数学-高一"))
        conn.execute(_INSERT_NODE_GRADED_SQL, ("n-ok", None, ""))
        conn.execute(_INSERT_NODE_GRADED_SQL, ("n-x", None, ""))
        conn.execute(_INSERT_STEP_INDEXED_SQL, ("s-ok", "q-a1", 1, "n-ok"))
        conn.execute(_INSERT_STEP_INDEXED_SQL, ("s-xa", "q-a1", 2, "n-x"))
        conn.execute(_INSERT_STEP_INDEXED_SQL, ("s-xb", "q-b1", 1, "n-x"))
        conn.execute(_INSERT_LEDGER_GRADED_SQL, ("q-a1:sess:MASTERED_INIT", "q-a1", ""))
    with pytest.raises(ValueError, match="跨学段"):
        with get_db_connection(tmp_db) as conn:
            _migrate_grade_scoping(conn)
    with get_db_connection(tmp_db) as conn:
        grades = {
            row["node_id"]: row["grade_subject"]
            for row in conn.execute("SELECT node_id, grade_subject FROM knowledge_nodes")
        }
        ledger_grade = conn.execute(
            "SELECT grade_subject FROM elo_mutation_ledger"
        ).fetchone()["grade_subject"]
        entry_count = conn.execute("SELECT COUNT(*) AS n FROM grade_subjects").fetchone()["n"]
    # 校验先于写入 + 异常整体回滚：合法节点 n-ok 也不回填，条目不注册
    assert grades == {"n-ok": "", "n-x": ""}
    assert ledger_grade == ""
    assert entry_count == 0


def test_migration_only_touches_empty_grade_rows(tmp_db: Path) -> None:
    """P10-A：回填仅针对 grade_subject='' 的行；预填行绝不被覆盖（纯增量非破坏）。"""
    with get_db_connection(tmp_db) as conn:
        conn.execute(_INSERT_QUESTION_GRADED_SQL, ("q-a1", "小学奥数"))
        conn.execute(_INSERT_NODE_GRADED_SQL, ("n-pre", None, "预填学段"))
        conn.execute(_INSERT_STEP_INDEXED_SQL, ("s-pre", "q-a1", 1, "n-pre"))
        conn.execute(_INSERT_LEDGER_GRADED_SQL, ("q-a1:sess:MASTERED_INIT", "q-a1", "预填学段"))
    with get_db_connection(tmp_db) as conn:
        _migrate_grade_scoping(conn)
    with get_db_connection(tmp_db) as conn:
        node_grade = conn.execute(
            "SELECT grade_subject FROM knowledge_nodes WHERE node_id='n-pre'"
        ).fetchone()["grade_subject"]
        ledger_grade = conn.execute(
            "SELECT grade_subject FROM elo_mutation_ledger"
        ).fetchone()["grade_subject"]
    assert node_grade == "预填学段"
    assert ledger_grade == "预填学段"


def test_migration_orphan_nodes_stay_empty_and_warn(
    tmp_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """P10-A：无步骤且无 redirect 目标学段的节点保持 ''，并计数告警汇报。"""
    with get_db_connection(tmp_db) as conn:
        conn.execute(_INSERT_NODE_GRADED_SQL, ("n-orphan", None, ""))
    with caplog.at_level(logging.WARNING, logger="backend.db.schema"):
        with get_db_connection(tmp_db) as conn:
            _migrate_grade_scoping(conn)
    with get_db_connection(tmp_db) as conn:
        grade = conn.execute(
            "SELECT grade_subject FROM knowledge_nodes WHERE node_id='n-orphan'"
        ).fetchone()["grade_subject"]
    assert grade == ""
    assert any("1 个知识节点" in rec.getMessage() for rec in caplog.records)


def test_diagnostic_sessions_prewarm_excluded_uids_default(tmp_db: Path) -> None:
    """P11-B：diagnostic_sessions 含 prewarm_excluded_uids_json 列且默认值为 '[]'。"""
    with get_db_connection(tmp_db) as conn:
        cols = {
            row["name"]: row for row in conn.execute("PRAGMA table_info(diagnostic_sessions)")
        }
        assert "prewarm_excluded_uids_json" in cols
        col = cols["prewarm_excluded_uids_json"]
        assert col["type"] == "TEXT"
        assert col["notnull"] == 1
        assert col["dflt_value"] == "'[]'"

        _insert_question(conn, "q-excl")
        conn.execute(
            "INSERT INTO diagnostic_sessions"
            " (session_id, question_uid, draft_image_cas_uris_json,"
            "  draft_steps_latex_json, error_category)"
            " VALUES ('sess-excl', 'q-excl', '[]', '[]', 'CONCEPT_BOTTLENECK')"
        )
        row = conn.execute(
            "SELECT prewarm_excluded_uids_json FROM diagnostic_sessions"
            " WHERE session_id = 'sess-excl'"
        ).fetchone()
    assert row["prewarm_excluded_uids_json"] == "[]"


def test_init_db_adds_prewarm_excluded_uids_column_idempotently(tmp_path: Path) -> None:
    """P11-B：旧库缺少 prewarm_excluded_uids_json 时，init_db 可幂等补列且保留历史行。"""
    from backend.db.schema import TABLE_DDLS

    legacy_db = tmp_path / "legacy_p11b.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute(TABLE_DDLS["questions"])
        conn.execute(
            """
            CREATE TABLE diagnostic_sessions (
                session_id TEXT PRIMARY KEY,
                question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
                draft_image_cas_uris_json TEXT NOT NULL,
                draft_steps_latex_json TEXT NOT NULL,
                matched_branch_id TEXT NOT NULL DEFAULT 'canonical',
                last_valid_step_index INTEGER NOT NULL DEFAULT 0,
                stalled_step_index INTEGER NOT NULL DEFAULT 1,
                error_category TEXT NOT NULL,
                trap_taxonomy_tag TEXT,
                user_confirmed_gate INTEGER NOT NULL DEFAULT 0,
                user_overridden INTEGER NOT NULL DEFAULT 0,
                micro_stage TEXT NOT NULL DEFAULT 'STAGE_0_CONFIRM_GATE',
                prewarm_scaffold_uid TEXT REFERENCES questions(question_uid),
                prewarm_variant_uid TEXT REFERENCES questions(question_uid),
                prewarm_status TEXT NOT NULL DEFAULT 'IDLE',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(_INSERT_QUESTION_SQL, ("q-legacy", 1))
        conn.execute(
            "INSERT INTO diagnostic_sessions"
            " (session_id, question_uid, draft_image_cas_uris_json,"
            "  draft_steps_latex_json, error_category)"
            " VALUES ('sess-legacy', 'q-legacy', '[]', '[]', 'CONCEPT_BOTTLENECK')"
        )
        conn.commit()

    init_db(legacy_db)
    init_db(legacy_db)

    with get_db_connection(legacy_db) as conn:
        cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(diagnostic_sessions)")
        }
        assert "prewarm_excluded_uids_json" in cols
        row = conn.execute(
            "SELECT prewarm_excluded_uids_json FROM diagnostic_sessions"
            " WHERE session_id = 'sess-legacy'"
        ).fetchone()
    assert row["prewarm_excluded_uids_json"] == "[]"
