"""Layer 2 单元测试：TaxonomySpec、四步锁表事务、human_overrides 迁移与 changelog 增量迁移。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shuati.core import effective
from shuati.core.taxonomy import (
    TaxonomyChangeAction,
    TaxonomyChangelogEntry,
    TaxonomyL1Entry,
    TaxonomyL2Entry,
    TaxonomySpec,
    build_lookup_index,
    migrate_taxonomy_incremental,
    save_taxonomy_spec,
    transition_taxonomy,
)
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db


def _seed_question_with_step(conn, uid: str, *, raw_l1: str, raw_l2: str, raw_l3: str) -> None:
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id,
            raw_question, content_hash
        )
        VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', ?, '测试题干', ?);
        """,
        (uid, uid, f"hash_{uid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_primary_l1_module, m_base_elo
        )
        VALUES (?, '小学奥数', '测试题干', '解答题', 'VERIFIED_CONSENSUS', '几何', 1500);
        """,
        (uid,),
    ) if False else None
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_primary_l1_module
        )
        VALUES (?, '小学奥数', '测试题干', '解答题', 'VERIFIED_CONSENSUS', ?);
        """,
        (uid, raw_l1),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
        )
        VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解答');
        """,
        (f"{uid}::b1", uid),
    )
    conn.execute(
        """
        INSERT INTO question_steps(
            step_id, question_uid, branch_id, step_index, sub_goal,
            raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id
        )
        VALUES (?, ?, 'branch_1', 1, '求面积', ?, ?, ?, ?, ?, NULL);
        """,
        (f"{uid}::b1::1", uid, raw_l1, raw_l2, raw_l3, "平面几何", "等高模型"),
    )
    conn.execute(
        """
        INSERT INTO compile_state(question_uid, stage, status)
        VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK'), (?, 'S4', 'OK');
        """,
        (uid, uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)


def test_taxonomy_uniqueness_and_lookup() -> None:
    with pytest.raises(ValueError, match="重复 L2"):
        TaxonomySpec(
            grade_subject="小学奥数",
            slug="primary_olympiad",
            modules=[
                TaxonomyL1Entry(
                    name="平面几何",
                    l2_entities=[TaxonomyL2Entry(name="等高模型"), TaxonomyL2Entry(name="等高模型")],
                )
            ],
        )

    spec = TaxonomySpec(
        grade_subject="小学奥数",
        slug="primary_olympiad",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                aliases=["直线型几何"],
                l2_entities=[TaxonomyL2Entry(name="三角形等高模型", aliases=["等高模型"])],
            )
        ],
    )
    lookup = build_lookup_index(spec)
    assert lookup.resolve_l1("直线型几何") == "平面几何"
    assert lookup.resolve_l2("未知L1", "等高模型") == ("平面几何", "三角形等高模型")


def test_taxonomy_lock_4step_and_override_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "shuati.db"
    cfg_dir = tmp_path / "config"
    conn = get_connection(db_path)
    init_db(conn, cfg_dir)

    _seed_question_with_step(conn, "q1", raw_l1="几何原词", raw_l2="旧等高", raw_l3="同底等高面积相等")
    _seed_question_with_step(conn, "q2", raw_l1="杂题", raw_l2="彻底废除类目", raw_l3="旧技巧")

    # 创建旧 draft-1 节点，并添加两条人工覆写（q1 可自动迁移，q2 无法映射）
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        VALUES ('old_l3_1', '小学奥数', 'draft-1', 'L3', '平面几何', '等高模型', '同底等高面积相等');
        """
    )
    conn.execute("UPDATE question_steps SET m_knowledge_node_id = 'old_l3_1' WHERE question_uid = 'q1';")
    conn.execute(
        """
        INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint, taxonomy_version)
        VALUES
        ('q1', 'taxonomy_override_json', ?, 'REVIEW_UI', 'hash_q1', 'draft-1'),
        ('q2', 'primary_l1_module', '即将删除模块', 'REVIEW_UI', 'hash_q2', 'draft-1');
        """,
        (
            json.dumps(
                {
                    "taxonomy_version": "draft-1",
                    "mode": "REPLACE",
                    "nodes": [{"l1": "平面几何", "l2": "等高模型", "l3": "同底等高面积相等", "node_id": "old_l3_1"}],
                },
                ensure_ascii=False,
            ),
        ),
    )
    effective.recompute_all(conn, "小学奥数")
    conn.commit()

    new_spec = TaxonomySpec(
        grade_subject="小学奥数",
        slug="primary_olympiad",
        status="DRAFT",
        version="draft-1",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                aliases=["直线型几何"],
                l2_entities=[TaxonomyL2Entry(name="三角形比例与等高模型", aliases=["等高模型"])],
            ),
            TaxonomyL1Entry(
                name="数论基础",
                l2_entities=[TaxonomyL2Entry(name="整除与余数")],
            ),
        ],
    )
    draft_yaml = cfg_dir / "taxonomies" / "taxonomy_primary_olympiad.draft.yaml"
    save_taxonomy_spec(new_spec, draft_yaml)

    # 1. Dry-run 不写库，准确报告 1 条可迁 / 1 条不可迁
    dry_res = transition_taxonomy(
        conn,
        grade_subject="小学奥数",
        yaml_path=draft_yaml,
        target_version="1.0",
        target_status="LOCKED",
        dry_run=True,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "backups",
        human_dir=tmp_path / "human",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )
    assert dry_res["dry_run"] is True
    assert dry_res["migrated_count"] == 1
    assert dry_res["unmapped_count"] == 1
    assert conn.execute("SELECT taxonomy_status FROM grade_subjects WHERE grade_subject = '小学奥数';").fetchone()["taxonomy_status"] == "NONE"

    # 2. 不传 --allow-stale-overrides 默认中止并导出迁移报告
    with pytest.raises(PreconditionError, match="allow-stale-overrides"):
        transition_taxonomy(
            conn,
            grade_subject="小学奥数",
            yaml_path=draft_yaml,
            target_version="1.0",
            target_status="LOCKED",
            allow_stale_overrides=False,
            config_dir=cfg_dir,
            backups_dir=tmp_path / "backups",
            human_dir=tmp_path / "human",
            reports_dir=tmp_path / "reports",
            lock_path=tmp_path / ".compile.lock",
        )
    assert (tmp_path / "reports" / "taxonomy_override_migration.json").exists()

    # 3. 传 allow_stale_overrides=True 完成 4 步事务
    res = transition_taxonomy(
        conn,
        grade_subject="小学奥数",
        yaml_path=draft_yaml,
        target_version="1.0",
        target_status="LOCKED",
        allow_stale_overrides=True,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "backups",
        human_dir=tmp_path / "human",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )
    assert res["target_status"] == "LOCKED"
    assert res["migrated_overrides"] == 1
    assert res["suspended_overrides"] == 1

    # raw_l1/raw_l2/raw_l3 必须丝毫无损，m_l1/m_l2/m_knowledge_node_id 必须清空
    st1 = conn.execute("SELECT raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id FROM question_steps WHERE question_uid = 'q1';").fetchone()
    assert (st1["raw_l1"], st1["raw_l2"], st1["raw_l3"]) == ("几何原词", "旧等高", "同底等高面积相等")
    assert (st1["m_l1"], st1["m_l2"], st1["m_knowledge_node_id"]) == ("", "", None)

    # q1 的 taxonomy_override_json 自动重映射到 1.0 新节点且 eff_node_ids_json 非空；q2 覆写被置 TAXONOMY_UNMAPPED 挂起
    q1_eff = json.loads(conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid = 'q1';").fetchone()["eff_node_ids_json"])
    assert len(q1_eff) == 1
    q2_ov = conn.execute("SELECT is_suspended, suspended_reason FROM human_overrides WHERE question_uid = 'q2';").fetchone()
    assert q2_ov["is_suspended"] == 1
    assert q2_ov["suspended_reason"] == "TAXONOMY_UNMAPPED"

    # compile_state S3C/S3D/S4 全置 STALE
    statuses = {
        (r["question_uid"], r["stage"]): r["status"]
        for r in conn.execute("SELECT question_uid, stage, status FROM compile_state WHERE stage IN ('S3C','S3D','S4');").fetchall()
    }
    assert all(v == "STALE" for v in statuses.values())

    # 4. 测试 LOCKED 1.0 -> 1.1 changelog 增量迁移
    conn.execute("UPDATE question_steps SET m_l1 = '平面几何', m_l2 = '三角形比例与等高模型' WHERE question_uid = 'q1';")
    conn.execute("UPDATE compile_state SET status = 'OK' WHERE question_uid IN ('q1', 'q2');")
    conn.commit()

    v11_spec = TaxonomySpec(
        grade_subject="小学奥数",
        slug="primary_olympiad",
        status="LOCKED",
        version="1.1",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                l2_entities=[TaxonomyL2Entry(name="等高与鸟头模型")],
            ),
            TaxonomyL1Entry(
                name="数论基础",
                l2_entities=[TaxonomyL2Entry(name="整除与余数")],
            ),
        ],
        changelog=[
            TaxonomyChangelogEntry(
                from_version="1.0",
                to_version="1.1",
                actions=[
                    TaxonomyChangeAction(
                        op="rename",
                        level="L2",
                        parent_l1="平面几何",
                        target="三角形比例与等高模型",
                        new_name="等高与鸟头模型",
                    )
                ],
            )
        ],
    )
    v11_yaml = cfg_dir / "taxonomies" / "taxonomy_primary_olympiad.v1.1.yaml"
    save_taxonomy_spec(v11_spec, v11_yaml)
    mig = migrate_taxonomy_incremental(
        conn,
        grade_subject="小学奥数",
        new_yaml_path=v11_yaml,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "backups",
        human_dir=tmp_path / "human",
        lock_path=tmp_path / ".compile.lock",
    )
    assert mig["to_version"] == "1.1"
    assert mig["affected_questions"] == 1
    assert conn.execute("SELECT m_l2 FROM question_steps WHERE question_uid = 'q1';").fetchone()["m_l2"] == "等高与鸟头模型"
    assert conn.execute("SELECT status FROM compile_state WHERE question_uid = 'q1' AND stage = 'S3D';").fetchone()["status"] == "STALE"
    assert conn.execute("SELECT status FROM compile_state WHERE question_uid = 'q2' AND stage = 'S3D';").fetchone()["status"] == "OK"
    conn.close()
