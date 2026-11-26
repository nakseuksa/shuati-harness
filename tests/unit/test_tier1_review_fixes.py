"""Tier 1 (B01-B04) 外部审查修复回归测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shuati.cli.main import app
from shuati.config import Settings, _load_env_file, _resolve_env_vars
from shuati.core import effective
from shuati.core.difficulty import compute_difficulty
from shuati.db import human_mirror, repository
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.pipeline.audit import run_stage_q_audit


def _seed_question(
    conn: sqlite3.Connection,
    uid: str,
    *,
    grade_subject: str = "小学奥数",
    source_collection: str = "col_a",
    source_id: str | None = None,
    duplicate_of_uid: str | None = None,
    m_c: int | None = 2,
    m_s: int | None = 2,
    m_t: int | None = 2,
    m_l: int | None = 2,
    verification_status: str = "VERIFIED_CONSENSUS",
    primary_l1: str = "平面几何",
    recompute: bool = True,
) -> None:
    sid = source_id if source_id is not None else uid.split(":")[-1]
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash, duplicate_of_uid
        )
        VALUES (?, ?, 'BATCH_JSONL', ?, ?, ?, ?, ?);
        """,
        (uid, grade_subject, source_collection, sid, f"题干-{uid}", f"hash-{uid}", duplicate_of_uid),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_primary_l1_module,
            m_c_score, m_s_score, m_t_score, m_l_score
        )
        VALUES (?, ?, ?, '解答题', ?, ?, ?, ?, ?, ?);
        """,
        (uid, grade_subject, f"题干-{uid}", verification_status, primary_l1, m_c, m_s, m_t, m_l),
    )
    if recompute:
        effective.recompute(conn, uid)


# ---------------------------------------------------------------------------
# T1 / B01-F2: difficulty_override_json out-of-range & bool fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_override",
    [
        json.dumps({"C": 9, "S": 1, "T": 1, "L": 1}),
        json.dumps({"C": 0, "S": 2, "T": 2, "L": 2}),
        json.dumps({"C": 2, "S": 5, "T": 1, "L": 1}),
        json.dumps({"C": True, "S": 1, "T": 1, "L": 1}),
        json.dumps({"C": 2, "S": False, "T": 1, "L": 1}),
        json.dumps([2, 2, 2, 2]),
        json.dumps({"C": 2, "S": 2}),
        "not-valid-json",
    ],
)
def test_b01_f2_invalid_difficulty_override_falls_back_cleanly(
    tmp_path: Path, bad_override: str
) -> None:
    conn = get_connection(tmp_path / "b01_f2.db")
    try:
        init_db(conn)
        uid = "primary_olympiad:col_a:q1"
        _seed_question(conn, uid, m_c=2, m_s=2, m_t=2, m_l=2)
        expected = compute_difficulty(2, 2, 2, 2)

        conn.execute(
            """
            INSERT INTO human_overrides(
                question_uid, field_name, override_value, reason, source, base_fingerprint, is_active
            )
            VALUES (?, 'difficulty_override_json', ?, 'test', 'PI_CLI', ?, 1);
            """,
            (uid, bad_override, f"hash-{uid}"),
        )
        conn.commit()

        # recompute、recompute_all、verify_effective_consistency 均不得崩溃
        effective.recompute(conn, uid)
        assert effective.recompute_all(conn) == 1
        conn.commit()

        row = conn.execute(
            """
            SELECT eff_c_score, eff_s_score, eff_t_score, eff_l_score,
                   eff_base_elo, eff_tier, eff_tier_rank
            FROM questions WHERE question_uid = ?;
            """,
            (uid,),
        ).fetchone()
        assert (row["eff_c_score"], row["eff_s_score"], row["eff_t_score"], row["eff_l_score"]) == (
            2,
            2,
            2,
            2,
        )
        assert row["eff_base_elo"] == expected.base_elo
        assert row["eff_tier"] == expected.tier
        assert row["eff_tier_rank"] == expected.tier_rank

        cons = effective.verify_effective_consistency(conn)
        assert cons["total_checked"] == 1
        assert cons["diff_count"] == 0
    finally:
        conn.close()


def test_b01_f2_valid_difficulty_override_and_servable_contract_preserved(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "b01_valid.db")
    try:
        init_db(conn)
        uid = "primary_olympiad:col_a:q_stage1_only"
        # 模拟 Stage 1 刚完成：尚未有主模块与难度分，但已 VERIFIED_CONSENSUS
        _seed_question(
            conn,
            uid,
            m_c=None,
            m_s=None,
            m_t=None,
            m_l=None,
            primary_l1="",
            verification_status="VERIFIED_CONSENSUS",
        )
        row_s1 = conn.execute(
            "SELECT eff_is_servable, eff_base_elo FROM questions WHERE question_uid = ?;", (uid,)
        ).fetchone()
        # DESIGN.md:L459: eff_is_servable 不要求主模块或 eff_base_elo 齐备
        assert row_s1["eff_is_servable"] == 1
        assert row_s1["eff_base_elo"] is None

        # 合法难度覆写生效
        conn.execute(
            """
            INSERT INTO human_overrides(
                question_uid, field_name, override_value, reason, source, base_fingerprint, is_active
            )
            VALUES (?, 'difficulty_override_json', ?, 'calibrate', 'PI_CLI', ?, 1);
            """,
            (uid, json.dumps({"C": 3, "S": 2, "T": 3, "L": 1}), f"hash-{uid}"),
        )
        effective.recompute(conn, uid)
        expected = compute_difficulty(3, 2, 3, 1)
        row_ov = conn.execute(
            "SELECT eff_base_elo, eff_tier, eff_tier_rank FROM questions WHERE question_uid = ?;",
            (uid,),
        ).fetchone()
        assert row_ov["eff_base_elo"] == expected.base_elo
        assert row_ov["eff_tier"] == expected.tier
        assert row_ov["eff_tier_rank"] == expected.tier_rank
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T2 / B02-F1: resolve_question_uid duplicate-aware disambiguation
# ---------------------------------------------------------------------------


def test_b02_f1_resolve_question_uid_collapses_duplicates_and_keeps_true_ambiguity(
    tmp_path: Path,
) -> None:
    conn = get_connection(tmp_path / "b02_f1.db")
    try:
        init_db(conn)
        # 主题 A:12 + 同 source_id='12' 的副本 B:12 与二级副本 C:12
        _seed_question(conn, "A:12", source_collection="A", source_id="12", recompute=False)
        _seed_question(
            conn,
            "B:12",
            source_collection="B",
            source_id="12",
            duplicate_of_uid="A:12",
            recompute=False,
        )
        _seed_question(
            conn,
            "C:12",
            source_collection="C",
            source_id="12",
            duplicate_of_uid="B:12",
            recompute=False,
        )
        conn.commit()

        assert repository.resolve_question_uid(conn, "12") == "A:12"

        # 新增另一道独立主题 D:12（非副本），此时简写 '12' 必须抛 AmbiguousUidError 且候选已去重为 ['A:12', 'D:12']
        _seed_question(conn, "D:12", source_collection="D", source_id="12", recompute=False)
        conn.commit()

        with pytest.raises(repository.AmbiguousUidError) as exc_info:
            repository.resolve_question_uid(conn, "12")
        assert exc_info.value.candidates == ["A:12", "D:12"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T3 / B03-F1 + Single-Transaction Atomic restore_snapshot
# ---------------------------------------------------------------------------


def test_b03_f1_vacuum_backup_handles_single_quotes_and_sanitizes_command_name(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "src.db"
    conn = get_connection(db_path)
    try:
        init_db(conn)
        _seed_question(conn, "primary_olympiad:col_a:q1")
        conn.commit()

        quoted_dir = tmp_path / "O'Neil's backups"
        b1 = human_mirror.vacuum_backup(conn, quoted_dir, "human_restore", keep=5)
        assert b1.exists()
        assert b1.parent == quoted_dir
        assert b1.name.endswith("_human_restore.db")

        # 验证备份库可读且内容完整
        check_conn = sqlite3.connect(b1)
        try:
            cnt = check_conn.execute("SELECT COUNT(*) FROM questions;").fetchone()[0]
            assert cnt == 1
        finally:
            check_conn.close()

        # 非法/穿越字符 command_name 必须被清洗且停留在 backups_dir 内
        b2 = human_mirror.vacuum_backup(conn, quoted_dir, "../evil'name; DROP", keep=5)
        assert b2.exists()
        assert b2.parent.resolve() == quoted_dir.resolve()
        assert "'" not in b2.name
        assert "/" not in b2.name
        assert "\\" not in b2.name
        assert ".." not in b2.name

        # 全非法字符回退为 "backup"
        b3 = human_mirror.vacuum_backup(conn, quoted_dir, "...///'''", keep=5)
        assert b3.exists()
        assert b3.name.endswith("_backup.db")
    finally:
        conn.close()


def test_b03_restore_snapshot_atomic_rollback_on_recompute_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "atomic.db"
    conn = get_connection(db_path)
    try:
        init_db(conn)
        uid = "primary_olympiad:col_a:q1"
        _seed_question(conn, uid)
        conn.execute(
            """
            INSERT INTO human_overrides(
                override_id, question_uid, field_name, override_value, reason,
                source, base_fingerprint, is_active
            )
            VALUES (1, ?, 'question_text', '改后题干', 'test', 'PI_CLI', ?, 1);
            """,
            (uid, f"hash-{uid}"),
        )
        effective.recompute(conn, uid)
        conn.commit()

        human_dir = tmp_path / "human"
        human_mirror.export_snapshot(conn, human_dir=human_dir)

        # 删除库中该覆写并恢复为原题干，随后模拟 restore_snapshot 在 recompute 阶段抛错
        conn.execute("DELETE FROM human_overrides;")
        effective.recompute(conn, uid)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM human_overrides;").fetchone()[0] == 0

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("simulated recompute crash")

        monkeypatch.setattr(effective, "recompute", _boom)

        with pytest.raises(RuntimeError, match="simulated recompute crash"):
            human_mirror.restore_snapshot(
                conn,
                human_dir=human_dir,
                backups_dir=tmp_path / "backups",
            )

        # 单一事务整体回滚：human_overrides 不得出现半提交脏数据
        assert conn.execute("SELECT COUNT(*) FROM human_overrides;").fetchone()[0] == 0
        eff_text = conn.execute(
            "SELECT eff_question_text FROM questions WHERE question_uid = ?;", (uid,)
        ).fetchone()["eff_question_text"]
        assert eff_text == f"题干-{uid}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T4 / B03-F2 & B03-F3: config.py env var resolution & .env parsing
# ---------------------------------------------------------------------------


def test_b03_f2_resolve_env_vars_empty_string_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHUATI_TEST_UNSET", raising=False)
    monkeypatch.setenv("SHUATI_TEST_EMPTY", "")
    monkeypatch.setenv("SHUATI_TEST_VAL", "https://custom.example/v1")
    monkeypatch.setenv("SHUATI_TEST_ZERO", "0")

    assert _resolve_env_vars("${SHUATI_TEST_UNSET:-fallback}") == "fallback"
    assert _resolve_env_vars("${SHUATI_TEST_EMPTY:-fallback}") == "fallback"
    assert _resolve_env_vars("${SHUATI_TEST_VAL:-fallback}") == "https://custom.example/v1"
    assert _resolve_env_vars("${SHUATI_TEST_ZERO:-fallback}") == "0"
    assert _resolve_env_vars("${SHUATI_TEST_EMPTY}") == ""


def test_b03_f3_load_env_file_quoted_inline_comments_and_hash_in_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = [
        "SHUATI_T_Q1",
        "SHUATI_T_Q2",
        "SHUATI_T_Q3",
        "SHUATI_T_Q4",
        "SHUATI_T_Q5",
        "SHUATI_T_PLAIN",
        "SHUATI_T_URL",
        "SHUATI_T_EMPTY",
        "SHUATI_T_EXISTING",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SHUATI_T_EXISTING", "keep-me")

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                '# comment line',
                'SHUATI_T_Q1="sk-abc" # my key',
                'SHUATI_T_Q2="sk#123" # comment with # hash',
                "SHUATI_T_Q3='val#1' # single quoted",
                r'SHUATI_T_Q4="a\"b" # escaped quote',
                r'SHUATI_T_Q5="a\\b" # escaped backslash',
                "SHUATI_T_PLAIN=abc # trailing comment",
                "SHUATI_T_URL=https://a.com#frag",
                'SHUATI_T_EMPTY="" # empty string',
                'SHUATI_T_EXISTING="overwrite-attempt" # should not overwrite',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _load_env_file(env_file)

    import os

    assert os.environ["SHUATI_T_Q1"] == "sk-abc"
    assert os.environ["SHUATI_T_Q2"] == "sk#123"
    assert os.environ["SHUATI_T_Q3"] == "val#1"
    assert os.environ["SHUATI_T_Q4"] == 'a"b'
    assert os.environ["SHUATI_T_Q5"] == r"a\b"
    assert os.environ["SHUATI_T_PLAIN"] == "abc"
    assert os.environ["SHUATI_T_URL"] == "https://a.com#frag"
    assert os.environ["SHUATI_T_EMPTY"] == ""
    assert os.environ["SHUATI_T_EXISTING"] == "keep-me"


# ---------------------------------------------------------------------------
# T5 / B04-F1 & B04-F2: audit.py mirror config alignment & db_invariants
# ---------------------------------------------------------------------------


def test_b04_f2_audit_respects_human_mirror_dir_and_include_practice_log(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "b04_f2.db"
    custom_human_dir = tmp_path / "custom_human"
    conn = get_connection(db_path)
    try:
        init_db(conn)
        uid = "primary_olympiad:col_a:q1"
        _seed_question(conn, uid)
        # 插入做题日志，但配置 include_practice_log=False 且不导出 practice_log.jsonl
        conn.execute(
            """
            INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, client_request_id)
            VALUES (1, 'default', ?, 'SOLVED_SOLO', 'req_1');
            """,
            (uid,),
        )
        conn.commit()
        human_mirror.export_snapshot(
            conn, human_dir=custom_human_dir, include_practice_log=False
        )
        assert not (custom_human_dir / "practice_log.jsonl").exists()

        settings = Settings()
        settings.human_mirror.dir = str(custom_human_dir)
        settings.human_mirror.include_practice_log = False

        # human_dir=None 时自动读取 settings.human_mirror.dir 与 include_practice_log=False
        res = run_stage_q_audit(
            conn,
            settings,
            human_dir=None,
            vectors_dir=tmp_path / "vectors",
            reports_dir=tmp_path / "reports",
            config_dir=tmp_path / "config",
        )
        mirror = res["report"]["section_5_integrity_and_cost"]["human_mirror_consistency"]
        assert mirror["is_consistent"] is True
        assert "practice_log" not in mirror["tables"]
    finally:
        conn.close()

    # 同步验证 CLI `shuati audit` 省略 --human-dir 时尊重 config/settings.yaml
    cfg_dir = tmp_path / "cli_cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.yaml").write_text(
        f"human_mirror:\n  dir: {custom_human_dir.as_posix()}\n  include_practice_log: false\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    cli_res = runner.invoke(
        app,
        [
            "audit",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--vectors-dir",
            str(tmp_path / "vectors"),
            "--reports-dir",
            str(tmp_path / "reports_cli"),
            "--json",
        ],
    )
    assert cli_res.exit_code == 0, cli_res.stdout
    payload = json.loads(cli_res.stdout)
    assert payload["ok"] is True
    cli_mirror = payload["data"]["report"]["section_5_integrity_and_cost"][
        "human_mirror_consistency"
    ]
    assert cli_mirror["is_consistent"] is True
    assert "practice_log" not in cli_mirror["tables"]


def test_b04_f1_audit_db_invariants_scaffold_boundary_assert_and_violations(
    tmp_path: Path,
) -> None:
    conn = get_connection(tmp_path / "b04_f1.db")
    try:
        init_db(conn)
        conn.execute(
            "UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0' WHERE grade_subject = '小学奥数';"
        )

        # 精确构造所需 Elo：
        # q_1225: C=1, S=2, T=1, L=1 -> 750 + 85 + 210 + 115 + 65 = 1225
        # q_1405: C=2, S=1, T=2, L=1 -> 750 + 255 + 105 + 230 + 65 = 1405 (diff=180)
        # q_1445: C=2, S=2, T=1, L=2 -> 750 + 255 + 210 + 115 + 130 = 1460 (diff=235)
        # q_1395: C=1, S=3, T=2, L=1 -> 750 + 85 + 315 + 230 + 65 = 1445? 算一下:
        #   q_1120: C=1, S=1, T=1, L=1 -> 1120
        #   q_1290: C=1, S=2, T=1, L=2 -> 1290 (diff from 1120 = 170 < 180)
        #   q_null: 无难度分 (eff_base_elo = NULL)
        _seed_question(conn, "p:a:1120", m_c=1, m_s=1, m_t=1, m_l=1)  # 1120
        _seed_question(conn, "p:a:1225", m_c=1, m_s=2, m_t=1, m_l=1)  # 1225
        _seed_question(conn, "p:a:1290", m_c=1, m_s=2, m_t=1, m_l=2)  # 1290
        _seed_question(conn, "p:a:1405", m_c=2, m_s=1, m_t=2, m_l=1)  # 1405
        _seed_question(conn, "p:a:1460", m_c=2, m_s=2, m_t=1, m_l=2)  # 1460
        _seed_question(conn, "p:a:null", m_c=None, m_s=None, m_t=None, m_l=None)  # None

        def _add_scaffold(src: str, tgt: str, conf_src: str) -> None:
            conn.execute(
                """
                INSERT INTO question_relations(
                    source_question_uid, target_question_uid, relation_type, is_directed,
                    is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
                    containment_score, trap_overlap, confidence_source
                )
                VALUES (?, ?, 'SCAFFOLD', 1, 0, 0.5, 0.8, 0.5, 0.9, 0.85, 0.0, ?);
                """,
                (src, tgt, conf_src),
            )

        # (1) 合法边集合：
        # - LLM_VERIFIED diff=180 (1225 -> 1405, 落入 10% boundary_band)
        _add_scaffold("p:a:1225", "p:a:1405", "LLM_VERIFIED")
        # - RULE_MATRIX diff=235 >= 200 (1225 -> 1460)
        _add_scaffold("p:a:1225", "p:a:1460", "RULE_MATRIX")
        # - 活跃人工 ASSERT 覆写豁免低差边 (1120 -> 1290, diff=170)
        _add_scaffold("p:a:1120", "p:a:1290", "HUMAN_VERIFIED")
        conn.execute(
            """
            INSERT INTO relation_overrides(
                source_question_uid, target_question_uid, relation_type, action,
                reason, source, is_active
            )
            VALUES ('p:a:1120', 'p:a:1290', 'SCAFFOLD', 'ASSERT', 'human_assert', 'PI_CLI', 1);
            """
        )
        conn.commit()
        human_dir = tmp_path / "human"
        human_mirror.export_snapshot(conn, human_dir=human_dir)

        settings = Settings()
        clean_res = run_stage_q_audit(
            conn,
            settings,
            human_dir=human_dir,
            vectors_dir=tmp_path / "vectors",
            reports_dir=tmp_path / "reports_clean",
            config_dir=tmp_path / "config",
        )
        inv_clean = clean_res["report"]["section_5_integrity_and_cost"]["db_invariants"]
        assert inv_clean["foreign_key_violations"] == 0
        assert inv_clean["multi_hop_redirects"]["knowledge_nodes_gt1_hop"] == 0
        assert inv_clean["multi_hop_redirects"]["concepts_gt1_hop"] == 0
        assert inv_clean["scaffold_contract_violations"]["count"] == 0
        assert not any(
            r.startswith("DB_INVARIANT:")
            for r in clean_res["report"]["section_7_risks_and_actions"]["risks"]
        )

        # (2) 注入各类真实违例：
        # 2a. RULE_MATRIX diff=180 < 200 (1225 -> 1405 换用另一对：1120 -> 1290 已被占，用 1290 -> 1460 diff=170;
        #     另加 1225 -> 1405 已为 LLM_VERIFIED，用 RULE_MATRIX 1290 -> 1460 diff=170)
        _add_scaffold("p:a:1290", "p:a:1460", "RULE_MATRIX")  # diff=170 < 200
        # 2b. LLM_VERIFIED diff=115 < 180 (1290 -> 1405)
        _add_scaffold("p:a:1290", "p:a:1405", "LLM_VERIFIED")  # diff=115 < 180
        # 2c. 反向边 (1460 -> 1225, diff=-235 <= 0)
        _add_scaffold("p:a:1460", "p:a:1225", "LLM_VERIFIED")
        # 2d. 缺失 Elo (p:a:null -> p:a:1460)
        _add_scaffold("p:a:null", "p:a:1460", "RULE_MATRIX")

        # 2e. knowledge_nodes 2 跳重定向: n3 -> n2 -> n1
        conn.execute(
            """
            INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator, redirect_to_id)
            VALUES
                ('n1', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '同底等高', NULL),
                ('n2', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '同底等高_旧1', 'n1'),
                ('n3', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '同底等高_旧2', 'n2');
            """
        )
        # 2f. concepts 2 跳重定向: c3 -> c2 -> c1
        conn.execute(
            """
            INSERT INTO concepts(concept_id, kind, name, redirect_to_id)
            VALUES
                ('c1', 'METHOD', '主方法', NULL),
                ('c2', 'METHOD', '旧方法1', 'c1'),
                ('c3', 'METHOD', '旧方法2', 'c2');
            """
        )
        # 2g. 制造外键违例（临时关 foreign_keys 写入孤儿行后再恢复开启）
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute(
            """
            INSERT INTO compile_state(question_uid, stage, status)
            VALUES ('ghost:missing:q999', 'S1', 'OK');
            """
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON;")

        bad_res = run_stage_q_audit(
            conn,
            settings,
            human_dir=human_dir,
            vectors_dir=tmp_path / "vectors",
            reports_dir=tmp_path / "reports_bad",
            config_dir=tmp_path / "config",
        )
        inv_bad = bad_res["report"]["section_5_integrity_and_cost"]["db_invariants"]
        assert inv_bad["foreign_key_violations"] >= 1
        assert len(inv_bad["foreign_key_violation_samples"]) >= 1
        assert inv_bad["multi_hop_redirects"]["knowledge_nodes_gt1_hop"] == 1
        assert inv_bad["multi_hop_redirects"]["concepts_gt1_hop"] == 1
        assert inv_bad["scaffold_contract_violations"]["count"] == 4
        viol_pairs = {
            (s["source_question_uid"], s["target_question_uid"])
            for s in inv_bad["scaffold_contract_violations"]["samples"]
        }
        assert viol_pairs == {
            ("p:a:1290", "p:a:1460"),
            ("p:a:1290", "p:a:1405"),
            ("p:a:1460", "p:a:1225"),
            ("p:a:null", "p:a:1460"),
        }

        bad_risks = bad_res["report"]["section_7_risks_and_actions"]["risks"]
        assert any("外键完整性违例" in r for r in bad_risks)
        assert any("knowledge_nodes" in r and "redirect_to_id" in r for r in bad_risks)
        assert any("concepts" in r and "redirect_to_id" in r for r in bad_risks)
        assert any("SCAFFOLD 边契约违例" in r for r in bad_risks)

        md_text = Path(bad_res["md_path"]).read_text(encoding="utf-8")
        assert "`db_invariants`:" in md_text
        assert "scaffold_contract_violations=4" in md_text

        # (3) 撤销 ASSERT 覆写 (is_active=0) 后，原 HUMAN_VERIFIED 低差边 (1120 -> 1290, diff=170) 也必须被捕获
        conn.execute(
            """
            UPDATE relation_overrides
            SET is_active = 0
            WHERE source_question_uid = 'p:a:1120'
              AND target_question_uid = 'p:a:1290'
              AND relation_type = 'SCAFFOLD';
            """
        )
        conn.commit()
        revoked_res = run_stage_q_audit(
            conn,
            settings,
            human_dir=human_dir,
            vectors_dir=tmp_path / "vectors",
            reports_dir=tmp_path / "reports_revoked",
            config_dir=tmp_path / "config",
        )
        inv_revoked = revoked_res["report"]["section_5_integrity_and_cost"]["db_invariants"]
        assert inv_revoked["scaffold_contract_violations"]["count"] == 5
        revoked_pairs = {
            (s["source_question_uid"], s["target_question_uid"])
            for s in inv_revoked["scaffold_contract_violations"]["samples"]
        }
        assert ("p:a:1120", "p:a:1290") in revoked_pairs
    finally:
        conn.close()
