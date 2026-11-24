"""v2.6 P1 数据安全与生命周期治理综合测试套件。

覆盖 ARCHITECTURE.md v2.6 P1-2 / P1-3 / P1-4：
1. 分组 base_fingerprint（effective.py & refresh.py）：仅验真类强绑 content_hash，
   标注类解绑 content_hash，词表类仅强绑非空 taxonomy_version。
2. taxonomy lock 事务原子性、practice_log.stuck_node_id 迁移/置空与维护降级模式
   （taxonomy.py & facade.py）。
3. D-Audit-6 外部错题 5 文件快照闭环、dry_run 外键预检、旧版缺列兼容与日志防误删保护
   （human_mirror.py）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from shuati.config import Settings
from shuati.core import effective, refresh
from shuati.core.taxonomy import (
    TaxonomyL1Entry,
    TaxonomyL2Entry,
    TaxonomySpec,
    get_taxonomy_maintenance_status,
    make_node_id,
    save_taxonomy_spec,
    transition_taxonomy,
)
from shuati.db import human_mirror
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.services import facade

GS = "小学奥数"
SLUG = "primary_olympiad"


def _make_conn(tmp_path: Path, name: str = "test.db") -> sqlite3.Connection:
    conn = get_connection(tmp_path / name)
    init_db(conn)
    return conn


def _seed_question(
    conn: sqlite3.Connection,
    uid: str,
    *,
    text: str = "已知三角形底为6高为4，求面积",
    content_hash: str = "hash_v1",
    ver_status: str = "VERIFIED_CONSENSUS",
    l1: str = "平面几何",
    source_type: str = "BATCH_JSONL",
    with_s2_ok: bool = True,
    s3_status: str = "OK",
) -> None:
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        ) VALUES (?, ?, ?, 'ch01', ?, ?, ?);
        """,
        (uid, GS, source_type, uid.split(":")[-1], text, content_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_primary_l1_module,
            m_c_score, m_s_score, m_t_score, m_l_score, m_abstract_skeleton
        ) VALUES (?, ?, ?, '解答题', ?, ?, 1, 1, 1, 1, '原始骨架');
        """,
        (uid, GS, text, ver_status, l1),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '标准解');
        """,
        (f"{uid}::branch_1", uid),
    )
    if with_s2_ok:
        conn.execute(
            """
            INSERT INTO compile_state(question_uid, stage, status)
            VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', ?), (?, 'S3D', ?), (?, 'S4', ?);
            """,
            (uid, uid, uid, s3_status, uid, s3_status, uid, s3_status),
        )
    effective.recompute(conn, uid)
    conn.commit()


# ===========================================================================
# 1. P1-3: 分组 base_fingerprint 策略 (effective.py & refresh.py)
# ===========================================================================


def test_p1_3_grouped_fingerprint_constants_partition_all_fields() -> None:
    """三组字段常量互不相交且完整覆盖 schema 中的 15 个可覆写字段。"""
    v = effective.VERIFICATION_OVERRIDE_FIELDS
    a = effective.ANNOTATION_OVERRIDE_FIELDS
    t = effective.TAXONOMY_OVERRIDE_FIELDS

    assert isinstance(v, frozenset)
    assert isinstance(a, frozenset)
    assert isinstance(t, frozenset)
    assert not (v & a)
    assert not (v & t)
    assert not (a & t)
    assert len(v | a | t) == 15
    assert "difficulty_override_json" in a
    assert "question_text" in v
    assert "primary_l1_module" in t


def test_p1_3_source_change_only_suspends_verification_and_bundle(tmp_path: Path) -> None:
    """源题 content_hash 微调时：仅挂起验真类及其同 bundle 成员，绝不误杀黄金锚点/标注类/词表类覆写。"""
    conn = _make_conn(tmp_path)
    conn.execute("UPDATE grade_subjects SET taxonomy_version='1.0', taxonomy_status='LOCKED' WHERE grade_subject=?;", (GS,))
    uid = "primary_olympiad:ch01:q_fp"
    _seed_question(conn, uid, content_hash="h_v1")

    # 1) 独立标注类覆写（黄金锚点 difficulty_override_json + abstract_skeleton + concepts）
    diff_val = json.dumps({"C": 3, "S": 2, "T": 2, "L": 1})
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, base_fingerprint, is_active
        )
        VALUES
            (?, 'difficulty_override_json', ?, 'ANCHOR_CALIBRATION', 'h_v1', 1),
            (?, 'abstract_skeleton', '人工黄金骨架', 'PI_CLI', 'h_v1', 1),
            (?, 'thinking_methods_json', '["构造法"]', 'PI_CLI', 'h_v1', 1),
            (?, 'primary_l1_module', '数论基础', 'PI_CLI', 'h_v1', 1);
        """,
        (uid, diff_val, uid, uid, uid),
    )
    # 更新 primary_l1_module 的 taxonomy_version='1.0'（与当前学段一致）
    conn.execute(
        "UPDATE human_overrides SET taxonomy_version='1.0' WHERE question_uid=? AND field_name='primary_l1_module';",
        (uid,),
    )
    effective.recompute(conn, uid)
    conn.commit()

    # 源题微调：content_hash 变为 h_v2，触发 handle_source_content_changed
    conn.execute("UPDATE raw_questions SET content_hash = 'h_v2' WHERE question_uid = ?;", (uid,))
    refresh.handle_source_content_changed(conn, uid, "h_v2")
    conn.commit()

    # 验证标注类与词表类全部保持激活且未挂起！
    rows = {
        r["field_name"]: (r["is_suspended"], r["suspended_reason"])
        for r in conn.execute(
            "SELECT field_name, is_suspended, suspended_reason FROM human_overrides WHERE question_uid = ?;",
            (uid,),
        ).fetchall()
    }
    assert rows["difficulty_override_json"] == (0, "")
    assert rows["abstract_skeleton"] == (0, "")
    assert rows["thinking_methods_json"] == (0, "")
    assert rows["primary_l1_module"] == (0, "")

    q = conn.execute(
        "SELECT eff_verification_status, eff_c_score, eff_abstract_skeleton, eff_primary_l1_module, eff_review_reason FROM questions WHERE question_uid = ?;",
        (uid,),
    ).fetchone()
    assert q["eff_verification_status"] == "VERIFIED_CONSENSUS"
    assert "OVERRIDE_STALE" not in q["eff_review_reason"]
    assert q["eff_c_score"] == 3
    assert q["eff_abstract_skeleton"] == "人工黄金骨架"
    assert q["eff_primary_l1_module"] == "数论基础"

    # 2) 新增验真类覆写（绑定旧指纹 h_v1）与同 bundle 的标注类 sibling：必须整组挂起并降级 NEEDS_REVIEW
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, bundle_id, base_fingerprint, is_active
        )
        VALUES
            (?, 'question_text', '手工修改题干', 'REVIEW_UI', 'bnd_mix', 'h_v1', 1),
            (?, 'classic_motifs_json', '["鸡兔模型"]', 'REVIEW_UI', 'bnd_mix', 'h_v2', 1);
        """,
        (uid, uid),
    )
    refresh.handle_source_content_changed(conn, uid, "h_v2")
    conn.commit()

    rows2 = {
        r["field_name"]: (r["is_suspended"], r["suspended_reason"])
        for r in conn.execute(
            "SELECT field_name, is_suspended, suspended_reason FROM human_overrides WHERE question_uid = ?;",
            (uid,),
        ).fetchall()
    }
    assert rows2["question_text"] == (1, "SOURCE_CHANGED")
    assert rows2["classic_motifs_json"] == (1, "SOURCE_CHANGED")
    # 独立标注类依然稳健存活
    assert rows2["difficulty_override_json"] == (0, "")

    q2 = conn.execute(
        "SELECT eff_verification_status, eff_review_reason, eff_is_servable FROM questions WHERE question_uid = ?;",
        (uid,),
    ).fetchone()
    assert q2["eff_verification_status"] == "NEEDS_REVIEW"
    assert "OVERRIDE_STALE" in q2["eff_review_reason"]
    assert q2["eff_is_servable"] == 0
    conn.close()


def test_p1_3_taxonomy_override_checks_taxonomy_version_not_content_hash(tmp_path: Path) -> None:
    """词表类覆写仅校验非空 taxonomy_version，不因 content_hash 挂起，版本不匹配时置 TAXONOMY_UNMAPPED。"""
    conn = _make_conn(tmp_path)
    conn.execute("UPDATE grade_subjects SET taxonomy_version='1.0', taxonomy_status='LOCKED' WHERE grade_subject=?;", (GS,))
    uid = "primary_olympiad:ch01:q_tax"
    _seed_question(conn, uid, content_hash="h_old")

    # 写入绑定 version='1.0' 的 primary_l1_module 与空 taxonomy_version 的 secondary_l1_modules_json
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, base_fingerprint, taxonomy_version, is_active
        )
        VALUES
            (?, 'primary_l1_module', '计算专题', 'PI_CLI', 'h_old', '1.0', 1),
            (?, 'secondary_l1_modules_json', '["几何"]', 'PI_CLI', 'h_old', '', 1);
        """,
        (uid, uid),
    )
    effective.recompute(conn, uid)
    conn.commit()

    # 修改学段 taxonomy_version -> 1.1
    conn.execute("UPDATE grade_subjects SET taxonomy_version='1.1' WHERE grade_subject=?;", (GS,))
    effective.recompute(conn, uid)
    conn.commit()

    ov_map = {
        r["field_name"]: (r["is_suspended"], r["suspended_reason"])
        for r in conn.execute(
            "SELECT field_name, is_suspended, suspended_reason FROM human_overrides WHERE question_uid=?;",
            (uid,),
        ).fetchall()
    }
    assert ov_map["primary_l1_module"] == (1, "TAXONOMY_UNMAPPED")
    # 空 taxonomy_version 不自动挂起
    assert ov_map["secondary_l1_modules_json"] == (0, "")
    # 词表挂起不触发验真类 OVERRIDE_STALE 降级
    q = conn.execute("SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid=?;", (uid,)).fetchone()
    assert q["eff_verification_status"] == "VERIFIED_CONSENSUS"
    assert "OVERRIDE_STALE" not in q["eff_review_reason"]
    conn.close()


# ===========================================================================
# 2. P1-4: taxonomy lock 事务原子性、stuck_node_id 迁移与维护降级模式
# ===========================================================================


def test_p1_4_taxonomy_lock_migrates_practice_log_and_yaml_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 lock 事务先建新 L2 骨架 -> 迁移/置空 practice_log.stuck_node_id -> 保留 stuck_sub_goal -> commit 后写 YAML。"""
    cfg_dir = tmp_path / "config"
    conn = _make_conn(tmp_path)
    uid = "primary_olympiad:ch01:q_lock"
    _seed_question(conn, uid, l1="平面几何", with_s2_ok=True, s3_status="OK")

    # 建两个旧 draft-1 节点：一个可映射到新词表（等高模型 -> 三角形等高模型），一个将被废除
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        VALUES
            ('old_n_mapped', '小学奥数', 'draft-1', 'L3', '平面几何', '等高模型', '同底等高'),
            ('old_n_dead', '小学奥数', 'draft-1', 'L3', '杂题', '废弃模块', '旧算子');
        """
    )
    conn.execute(
        """
        INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, stuck_step_index, stuck_node_id, stuck_sub_goal)
        VALUES
            (1, 'default', ?, 'WRONG', 1, 'old_n_mapped', '找等高底边比'),
            (2, 'default', ?, 'SKIPPED', 2, 'old_n_dead', '废弃算子文本快照');
        """,
        (uid, uid),
    )
    conn.commit()

    new_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="DRAFT",
        version="draft-1",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                l2_entities=[TaxonomyL2Entry(name="三角形等高模型", aliases=["等高模型"])],
            )
        ],
    )
    draft_yaml = cfg_dir / "taxonomies" / f"taxonomy_{SLUG}.draft.yaml"
    save_taxonomy_spec(new_spec, draft_yaml)
    locked_yaml = cfg_dir / "taxonomies" / f"taxonomy_{SLUG}.yaml"

    # 2.1 模拟事务内失败：验证回滚后磁盘 locked_yaml 绝不被创建/污染
    orig_mark = refresh.mark_stages_stale

    def _boom_mark(*args, **kwargs):
        raise RuntimeError("injected db failure before commit")

    monkeypatch.setattr(refresh, "mark_stages_stale", _boom_mark)
    with pytest.raises(RuntimeError, match="injected db failure"):
        transition_taxonomy(
            conn,
            grade_subject=GS,
            yaml_path=draft_yaml,
            target_version="1.0",
            target_status="LOCKED",
            config_dir=cfg_dir,
            backups_dir=tmp_path / "backups",
            human_dir=tmp_path / "human",
            reports_dir=tmp_path / "reports",
            lock_path=tmp_path / ".compile.lock",
        )
    assert not locked_yaml.exists(), "DB 事务回滚时不得提前写入 LOCKED YAML 文件"
    assert conn.execute("SELECT taxonomy_status FROM grade_subjects WHERE grade_subject=?;", (GS,)).fetchone()["taxonomy_status"] == "NONE"

    # 2.2 恢复函数后正常锁表
    monkeypatch.setattr(refresh, "mark_stages_stale", orig_mark)
    res = transition_taxonomy(
        conn,
        grade_subject=GS,
        yaml_path=draft_yaml,
        target_version="1.0",
        target_status="LOCKED",
        config_dir=cfg_dir,
        backups_dir=tmp_path / "backups",
        human_dir=tmp_path / "human",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )
    assert res["migrated_practice_logs"] == 1
    assert res["nulled_practice_logs"] == 1
    assert locked_yaml.exists()

    expected_l2_id = make_node_id(SLUG, "1.0", "L2", "平面几何", "三角形等高模型", "")
    # 验证新 L2 节点存在于 knowledge_nodes 且 practice_log.stuck_node_id 无悬空引用
    l2_row = conn.execute("SELECT node_id FROM knowledge_nodes WHERE node_id=?;", (expected_l2_id,)).fetchone()
    assert l2_row is not None

    pl1 = conn.execute("SELECT stuck_node_id, stuck_sub_goal FROM practice_log WHERE log_id=1;").fetchone()
    pl2 = conn.execute("SELECT stuck_node_id, stuck_sub_goal FROM practice_log WHERE log_id=2;").fetchone()
    assert pl1["stuck_node_id"] == expected_l2_id
    assert pl1["stuck_sub_goal"] == "找等高底边比"
    assert pl2["stuck_node_id"] is None
    assert pl2["stuck_sub_goal"] == "废弃算子文本快照"

    # 2.3 验证锁表后处于维护降级模式，且基础检索/详情/做题日志不阻断
    maint = get_taxonomy_maintenance_status(conn, GS)
    assert maint["degraded"] is True
    assert maint["code"] == "MAINTENANCE_DEGRADED"
    assert maint["stale_s3c_count"] == 1
    assert maint["stale_s3d_count"] == 1
    assert "MAINTENANCE_DEGRADED" in maint["message"]

    # browse_taxonomy 与 search_questions(l2/l3) 返回 maintenance_degraded=True + warnings
    b_res = facade.browse_taxonomy(conn, grade_subject=GS)
    assert b_res["maintenance_degraded"] is True
    assert any("MAINTENANCE_DEGRADED" in w for w in b_res["warnings"])

    settings = Settings()
    s_l2 = facade.search_questions(conn, settings, grade_subject=GS, l2="三角形等高模型")
    assert s_l2["maintenance_degraded"] is True
    assert any("MAINTENANCE_DEGRADED" in w for w in s_l2["warnings"])

    s_l3 = facade.search_questions(conn, settings, grade_subject=GS, l3="同底等高")
    assert s_l3["maintenance_degraded"] is True
    assert any("MAINTENANCE_DEGRADED" in w for w in s_l3["warnings"])

    # 基础纯文本搜索、detail 与 log_practice 保持完全不受阻断
    s_text = facade.search_questions(conn, settings, grade_subject=GS, query="三角形")
    assert s_text["returned"] == 1
    assert not any("MAINTENANCE_DEGRADED" in w for w in s_text["warnings"])

    detail = facade.get_question_detail(conn, uid, reveal="quiz")
    assert detail["question_uid"] == uid

    log_res = facade.log_practice(
        conn,
        uid_or_id=uid,
        outcome="SOLVED_SOLO",
        learner_id="default",
        human_dir=tmp_path / "human",
    )
    assert log_res["outcome"] == "SOLVED_SOLO"

    # 2.4 模拟 S3C/S3D 重编完成后，自动退出维护降级模式
    conn.execute("UPDATE compile_state SET status='OK' WHERE question_uid=? AND stage IN ('S3C', 'S3D');", (uid,))
    conn.commit()
    maint_ok = get_taxonomy_maintenance_status(conn, GS)
    assert maint_ok["degraded"] is False
    assert maint_ok["stale_s3c_count"] == 0
    assert maint_ok["stale_s3d_count"] == 0

    b_ok = facade.browse_taxonomy(conn, grade_subject=GS)
    assert b_ok["maintenance_degraded"] is False
    assert not any("MAINTENANCE_DEGRADED" in w for w in b_ok["warnings"])

    s_l2_ok = facade.search_questions(conn, settings, grade_subject=GS, l2="三角形等高模型")
    assert s_l2_ok["maintenance_degraded"] is False
    assert not any("MAINTENANCE_DEGRADED" in w for w in s_l2_ok["warnings"])
    conn.close()


# ===========================================================================
# 3. P1-2 (D-Audit-6): 外部错题快照闭环与安全恢复加固 (human_mirror.py)
# ===========================================================================


def test_p1_2_external_questions_full_mirror_cycle_and_protections(tmp_path: Path) -> None:
    """覆盖：空库 ingest -> restore 自举外部错题 -> 缺主键预检(dry_run 与实执行) -> include_practice_log/空文件防误删 -> verify 5文件一致。"""
    src = _make_conn(tmp_path, "src.db")
    q_batch = "primary_olympiad:ch01:q_batch"
    q_ext = "primary_olympiad:external:ext_001"
    _seed_question(src, q_batch, text="存量常规题", content_hash="hb1", source_type="BATCH_JSONL")
    _seed_question(src, q_ext, text="拍照错题求阴影面积", content_hash="he1", source_type="EXTERNAL_MISTAKE")

    src.execute(
        "INSERT INTO learners(learner_id, display_name, default_grade_subject, is_current) VALUES ('alice', 'Alice', ?, 0);",
        (GS,),
    )
    src.execute(
        """
        INSERT INTO human_overrides(
            override_id, question_uid, field_name, override_value, reason,
            source, base_fingerprint, is_active
        ) VALUES (1, ?, 'abstract_skeleton', '人工骨架', '标注', 'PI_CLI', 'he1', 1);
        """,
        (q_ext,),
    )
    src.execute(
        """
        INSERT INTO relation_overrides(
            override_id, source_question_uid, target_question_uid,
            relation_type, action, pedagogical_note, source, is_active
        ) VALUES (1, ?, ?, 'ISOMORPHIC', 'ASSERT', '同构关联', 'PI_CLI', 1);
        """,
        (q_batch, q_ext),
    )
    src.execute(
        """
        INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, score_ratio)
        VALUES (1, 'alice', ?, 'WRONG', 0.0);
        """,
        (q_ext,),
    )
    src.commit()

    h_dir = tmp_path / "human"
    exp_counts = human_mirror.export_snapshot(src, human_dir=h_dir, include_practice_log=True)
    assert exp_counts["external_questions"] == 1
    assert exp_counts["human_overrides"] == 1
    assert exp_counts["relation_overrides"] == 1
    assert exp_counts["practice_log"] == 1
    for fn in ("external_questions.jsonl", "learners.jsonl", "overrides.jsonl", "relation_overrides.jsonl", "practice_log.jsonl"):
        assert (h_dir / fn).exists()
    assert human_mirror.verify_snapshot(src, human_dir=h_dir)["is_consistent"] is True

    # 3.1 空库仅恢复常规存量题 q_batch（模拟 shuati ingest），不含 q_ext
    dst = _make_conn(tmp_path, "dst.db")
    _seed_question(dst, q_batch, text="存量常规题", content_hash="hb1", source_type="BATCH_JSONL")

    # dry_run 应通过预检且报告 fk_preflight_ok=True
    dry_ok = human_mirror.restore_snapshot(dst, human_dir=h_dir, backups_dir=None, dry_run=True)
    assert dry_ok["fk_preflight_ok"] is True
    assert dry_ok["missing_question_uids"] == []
    assert dry_ok["external_questions"] == 1

    # 3.2 缺主键预检拦截测试（含 dry_run 与实执行零脏写）
    bad_dir = tmp_path / "bad_human"
    bad_dir.mkdir()
    for fn in ("external_questions.jsonl", "learners.jsonl", "relation_overrides.jsonl", "practice_log.jsonl"):
        (bad_dir / fn).write_text((h_dir / fn).read_text(encoding="utf-8"), encoding="utf-8")
    (bad_dir / "overrides.jsonl").write_text(
        json.dumps(
            {
                "override_id": 99,
                "question_uid": "primary_olympiad:missing:q999",
                "field_name": "abstract_skeleton",
                "override_value": "bad",
                "reason": "",
                "source": "PI_CLI",
                "bundle_id": None,
                "base_fingerprint": "hx",
                "taxonomy_version": "",
                "is_active": 1,
                "is_suspended": 0,
                "suspended_reason": "",
                "revoked_at": None,
                "created_at": "2026-11-24 00:00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    dry_bad = human_mirror.restore_snapshot(dst, human_dir=bad_dir, backups_dir=None, dry_run=True)
    assert dry_bad["fk_preflight_ok"] is False
    assert dry_bad["missing_question_uids"] == ["primary_olympiad:missing:q999"]

    bk_bad_dir = tmp_path / "bk_bad"
    with pytest.raises(PreconditionError, match="外键预检失败"):
        human_mirror.restore_snapshot(dst, human_dir=bad_dir, backups_dir=bk_bad_dir, dry_run=False)
    # 预检失败零脏写、零冗余备份
    assert dst.execute("SELECT COUNT(*) FROM human_overrides;").fetchone()[0] == 0
    assert dst.execute("SELECT COUNT(*) FROM questions WHERE question_uid=?;", (q_ext,)).fetchone()[0] == 0
    assert not bk_bad_dir.exists() or not list(bk_bad_dir.glob("*.db"))

    # 3.3 正式 restore：自动自举 external_questions.jsonl + 全 5 表一致
    res = human_mirror.restore_snapshot(dst, human_dir=h_dir, backups_dir=tmp_path / "bk")
    assert res["external_questions"] == 1
    assert res["practice_log"] == 1
    ver = human_mirror.verify_snapshot(dst, human_dir=h_dir)
    assert ver["is_consistent"] is True
    assert set(ver["tables"].keys()) == {"learners", "human_overrides", "relation_overrides", "practice_log", "external_questions"}
    assert all(t["match"] is True for t in ver["tables"].values())

    # 3.4 include_practice_log=False 与空 practice_log.jsonl 防误清空保护
    empty_pl_dir = tmp_path / "empty_pl_human"
    empty_pl_dir.mkdir()
    for fn in ("external_questions.jsonl", "learners.jsonl", "overrides.jsonl", "relation_overrides.jsonl"):
        (empty_pl_dir / fn).write_text((h_dir / fn).read_text(encoding="utf-8"), encoding="utf-8")
    (empty_pl_dir / "practice_log.jsonl").write_text("", encoding="utf-8")

    human_mirror.restore_snapshot(dst, human_dir=empty_pl_dir, backups_dir=None, include_practice_log=False)
    assert dst.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0] == 1

    human_mirror.restore_snapshot(
        dst, human_dir=empty_pl_dir, backups_dir=None, include_practice_log=True, force_wipe_logs=False
    )
    assert dst.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0] == 1

    human_mirror.restore_snapshot(
        dst, human_dir=empty_pl_dir, backups_dir=None, include_practice_log=True, force_wipe_logs=True
    )
    assert dst.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0] == 0

    src.close()
    dst.close()


def test_p1_2_restore_legacy_minimal_external_question_defaults(tmp_path: Path) -> None:
    """验证旧版或精简字段的 external_questions.jsonl 缺失可选列时可安全补齐默认值并恢复成功。"""
    dst = _make_conn(tmp_path, "legacy_dst.db")
    h_dir = tmp_path / "legacy_human"
    h_dir.mkdir()
    (h_dir / "external_questions.jsonl").write_text(
        json.dumps(
            {
                "question_uid": "primary_olympiad:ext:legacy_1",
                "grade_subject": GS,
                "raw_question": "旧版最小字段外部错题：求 3+5",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    res = human_mirror.restore_snapshot(dst, human_dir=h_dir, backups_dir=None)
    assert res["external_questions"] == 1
    row = dst.execute(
        "SELECT eff_question_text, eff_format FROM questions WHERE question_uid='primary_olympiad:ext:legacy_1';"
    ).fetchone()
    assert row is not None
    assert row["eff_question_text"] == "旧版最小字段外部错题：求 3+5"
    assert row["eff_format"] == "解答题"
    dst.close()
