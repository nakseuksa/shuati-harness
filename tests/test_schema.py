"""P1-A Schema 基座测试：建表完整性 / PRAGMA / 约束拦截 / 幂等 / 默认值 / 事务语义。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.config import settings
from backend.db.schema import TABLE_NAMES, get_db_connection, init_db

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
    """9 张核心表 + 1 张 PM 批准扩展表 blind_drill_attempts 全部存在且无多余业务表。"""
    with get_db_connection(tmp_db) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert {row["name"] for row in rows} == set(TABLE_NAMES)
    assert len(TABLE_NAMES) == 10


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
