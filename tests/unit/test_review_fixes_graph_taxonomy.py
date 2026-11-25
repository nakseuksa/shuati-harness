"""Regression tests for third-party audit remediation (Worker A: A1-A4)."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from shuati.config import Settings
from shuati.core import effective
from shuati.core.knowledge_graph import (
    apply_cluster_assignments,
    apply_merge_pairs,
    compute_l3_distribution_entropy,
    revert_merge_batch,
)
from shuati.core.taxonomy import (
    TaxonomyChangeAction,
    TaxonomyChangelogEntry,
    TaxonomyL1Entry,
    TaxonomyL2Entry,
    TaxonomySpec,
    _compare_versions,
    _parse_version_tuple,
    make_node_id,
    migrate_taxonomy_incremental,
    save_taxonomy_spec,
)
from shuati.db.connection import get_connection
from shuati.db.exporter import export_all_jsonl
from shuati.db.schema import init_db
from shuati.llm.embeddings import EmbeddingClient
from shuati.vectors.store import VectorStore

GS = "小学奥数"
SLUG = "primary_olympiad"


def _seed_q(
    conn: sqlite3.Connection,
    uid: str,
    *,
    primary_l1: str,
    secondary_l1s: list[str] | None = None,
    steps: list[tuple[str, str, str, str | None]],
) -> None:
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id,
            raw_question, content_hash
        )
        VALUES (?, ?, 'BATCH_JSONL', 'c1', ?, '测试题干', ?);
        """,
        (uid, GS, uid, f"h_{uid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_primary_l1_module, m_secondary_l1_modules_json
        )
        VALUES (?, ?, '测试题干', '解答题', 'VERIFIED_CONSENSUS', ?, ?);
        """,
        (uid, GS, primary_l1, json.dumps(secondary_l1s or [], ensure_ascii=False)),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
        )
        VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '标准解答');
        """,
        (f"{uid}::b1", uid),
    )
    for idx, (l1, l2, l3, nid) in enumerate(steps, start=1):
        conn.execute(
            """
            INSERT INTO question_steps(
                step_id, question_uid, branch_id, step_index, sub_goal,
                raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id
            )
            VALUES (?, ?, 'branch_1', ?, '核心步骤', ?, ?, ?, ?, ?, ?);
            """,
            (f"{uid}::b1::{idx}", uid, idx, l1, l2, l3, l1, l2, nid),
        )
    conn.execute(
        """
        INSERT INTO compile_state(question_uid, stage, status)
        VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK'), (?, 'S4', 'OK');
        """,
        (uid, uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)


# ===========================================================================
# A1 & A2: Redirected L3 step aggregation in entropy & JSONL exporter
# ===========================================================================


def test_a1_a2_entropy_and_exporter_aggregate_redirected_l3_steps(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "a1_a2.db")
    init_db(conn)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0' WHERE grade_subject = ?;",
        (GS,),
    )
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        VALUES
        ('l2_eq', '小学奥数', '1.0', 'L2', '平面几何', '等高模型', ''),
        ('l2_nt', '小学奥数', '1.0', 'L2', '数论基础', '整除', ''),
        ('n_a', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '同底等高面积比'),
        ('n_b', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '等高三角形面积转化'),
        ('n_c', '小学奥数', '1.0', 'L3', '数论基础', '整除', '整除特征判别'),
        ('n_d', '小学奥数', '1.0', 'L3', '数论基础', '整除', '末位整除规律'),
        ('n_unused', '小学奥数', '1.0', 'L3', '数论基础', '整除', '未使用算子');
        """
    )

    # 3 steps under 平面几何/等高模型 (n_a: 2, n_b: 1) and 3 steps under 数论基础/整除 (n_c: 1, n_d: 2)
    _seed_q(
        conn,
        "q1",
        primary_l1="平面几何",
        steps=[
            ("平面几何", "等高模型", "同底等高面积比", "n_a"),
            ("平面几何", "等高模型", "同底等高面积比", "n_a"),
        ],
    )
    _seed_q(
        conn,
        "q2",
        primary_l1="平面几何",
        steps=[("平面几何", "等高模型", "等高三角形面积转化", "n_b")],
    )
    _seed_q(
        conn,
        "q3",
        primary_l1="数论基础",
        steps=[("数论基础", "整除", "整除特征判别", "n_c")],
    )
    _seed_q(
        conn,
        "q4",
        primary_l1="数论基础",
        steps=[
            ("数论基础", "整除", "末位整除规律", "n_d"),
            ("数论基础", "整除", "末位整除规律", "n_d"),
        ],
    )
    conn.commit()

    # Merge n_a, n_b into a NEW canonical master via Cluster-then-Assign (0 direct steps on master!)
    ca = apply_cluster_assignments(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict={
            "grade_subject": GS,
            "taxonomy_version": "1.0",
            "l1_module": "平面几何",
            "l2_entity": "等高模型",
        },
        canonical_operators=[{"canonical_name": "标准等高面积模型", "description": "新主节点"}],
        assignments=[
            {"item_id": "n_a", "raw_l3": "同底等高面积比", "canonical_name": "标准等高面积模型"},
            {"item_id": "n_b", "raw_l3": "等高三角形面积转化", "canonical_name": "标准等高面积模型"},
        ],
        batch_id="ca_geom",
        slug=SLUG,
        grade_subject=GS,
        taxonomy_version="1.0",
    )
    assert ca["canonical_nodes_created"] == 1

    # Merge n_d into EXISTING canonical node n_c (1 direct step + 2 redirected steps = 3)
    apply_merge_pairs(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict={
            "grade_subject": GS,
            "taxonomy_version": "1.0",
            "l1_module": "数论基础",
            "l2_entity": "整除",
        },
        merges=[{"from_id": "n_d", "to_id": "n_c", "evidence": "同义"}],
        batch_id="mp_nt",
    )

    # A1: Entropy must aggregate steps from both newly-created master (3) and existing master (3)
    ent = compute_l3_distribution_entropy(conn, GS, "1.0")
    assert ent["active_l3_nodes"] == 3  # new master + n_c + n_unused
    assert ent["used_l3_nodes"] == 2
    assert ent["total_step_bindings"] == 6
    assert ent["entropy"] == pytest.approx(round(math.log(2), 6))
    assert ent["normalized_entropy"] == pytest.approx(1.0)

    # A2: Exporter must report full aggregated step_binding_count on canonical L3s and 0 on L2s
    exp = export_all_jsonl(conn, export_dir=tmp_path / "exports")
    assert exp["counts"]["knowledge_nodes"] == 5  # 2 L2 + 3 active L3
    kn_records = [
        json.loads(line)
        for line in (tmp_path / "exports" / "knowledge_nodes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_label = {(r["node_level"], r["l2_entity"], r["l3_operator"]): r for r in kn_records}
    assert by_label[("L2", "等高模型", "")]["step_binding_count"] == 0
    assert by_label[("L2", "整除", "")]["step_binding_count"] == 0
    assert by_label[("L3", "等高模型", "标准等高面积模型")]["step_binding_count"] == 3
    assert by_label[("L3", "整除", "整除特征判别")]["step_binding_count"] == 3
    assert by_label[("L3", "整除", "未使用算子")]["step_binding_count"] == 0
    # Redirected children must not appear in knowledge_nodes.jsonl
    exported_ids = {r["node_id"] for r in kn_records}
    assert not ({"n_a", "n_b", "n_d"} & exported_ids)
    conn.close()


# ===========================================================================
# A3: CONCEPT lossless revert (pair merge, multi-batch chain, cluster master, legacy)
# ===========================================================================


def test_a3_concept_revert_lossless_pair_chain_cluster_and_legacy(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "a3.db")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO concepts(concept_id, kind, name, is_seed)
        VALUES
        ('c1', 'METHOD', '假设法A', 0),
        ('c2', 'METHOD', '假设法B', 0),
        ('c3', 'METHOD', '假设法C', 0),
        ('c_leg_from', 'MOTIF', '旧母题1', 0),
        ('c_leg_to', 'MOTIF', '旧母题2', 0);
        """
    )
    for qid in ("q1", "q2", "q3", "q4"):
        _seed_q(conn, qid, primary_l1="平面几何", steps=[])

    # Initial question_concepts:
    # q1: c1(MACHINE) only
    # q2: c1(MACHINE) + c2(HUMAN)
    # q3: c1(HUMAN) + c2(MACHINE)  -> merging c1->c2 upgrades (q3, c2) to HUMAN; revert must downgrade back to MACHINE!
    # q4: c3(MACHINE) only
    initial_qc = [
        ("q1", "c1", "METHOD", "MACHINE"),
        ("q2", "c1", "METHOD", "MACHINE"),
        ("q2", "c2", "METHOD", "HUMAN"),
        ("q3", "c1", "METHOD", "HUMAN"),
        ("q3", "c2", "METHOD", "MACHINE"),
        ("q4", "c3", "METHOD", "MACHINE"),
    ]
    conn.executemany(
        "INSERT INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, ?, ?);",
        initial_qc,
    )
    conn.commit()

    def _snapshot_method_qc() -> list[tuple[str, str, str, str]]:
        return [
            (str(r["question_uid"]), str(r["concept_id"]), str(r["kind"]), str(r["origin"]))
            for r in conn.execute(
                "SELECT question_uid, concept_id, kind, origin FROM question_concepts WHERE kind = 'METHOD' ORDER BY question_uid, concept_id;"
            ).fetchall()
        ]

    # Batch 1: c1 -> c2
    apply_merge_pairs(
        conn,
        target_kind="CONCEPT",
        scope_dict={"kind": "METHOD"},
        merges=[{"from_id": "c1", "to_id": "c2", "evidence": "step1"}],
        batch_id="c_b1",
    )
    after_b1 = _snapshot_method_qc()
    assert after_b1 == [
        ("q1", "c2", "METHOD", "MACHINE"),
        ("q2", "c2", "METHOD", "HUMAN"),
        ("q3", "c2", "METHOD", "HUMAN"),
        ("q4", "c3", "METHOD", "MACHINE"),
    ]

    # Batch 2: c2 -> c3 (chained merge)
    apply_merge_pairs(
        conn,
        target_kind="CONCEPT",
        scope_dict={"kind": "METHOD"},
        merges=[{"from_id": "c2", "to_id": "c3", "evidence": "step2"}],
        batch_id="c_b2",
    )
    assert _snapshot_method_qc() == [
        ("q1", "c3", "METHOD", "MACHINE"),
        ("q2", "c3", "METHOD", "HUMAN"),
        ("q3", "c3", "METHOD", "HUMAN"),
        ("q4", "c3", "METHOD", "MACHINE"),
    ]

    # Revert Batch 2 -> must restore exact post-Batch-1 state
    revert_merge_batch(conn, "c_b2", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    assert _snapshot_method_qc() == after_b1

    # Revert Batch 1 -> must restore exact initial state including (q3, c2, MACHINE)
    revert_merge_batch(conn, "c_b1", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    assert _snapshot_method_qc() == sorted(initial_qc)

    # Cluster-then-Assign creating a NEW canonical master concept -> revert must restore QC AND delete master!
    ca_c = apply_cluster_assignments(
        conn,
        target_kind="CONCEPT",
        scope_dict={"kind": "METHOD"},
        canonical_operators=[{"canonical_name": "标准假设法", "description": "新标准概念"}],
        assignments=[
            {"item_id": "c1", "raw_l3": "假设法A", "canonical_name": "标准假设法"},
            {"item_id": "c2", "raw_l3": "假设法B", "canonical_name": "标准假设法"},
        ],
        batch_id="c_cluster_1",
    )
    assert ca_c["canonical_nodes_created"] == 1
    assert (
        conn.execute("SELECT 1 FROM concepts WHERE concept_id = 'METHOD:标准假设法';").fetchone()
        is not None
    )
    assert _snapshot_method_qc() == [
        ("q1", "METHOD:标准假设法", "METHOD", "MACHINE"),
        ("q2", "METHOD:标准假设法", "METHOD", "HUMAN"),
        ("q3", "METHOD:标准假设法", "METHOD", "HUMAN"),
        ("q4", "c3", "METHOD", "MACHINE"),
    ]

    revert_merge_batch(conn, "c_cluster_1", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    assert _snapshot_method_qc() == sorted(initial_qc)
    # Newly created canonical master must be cleaned up!
    assert (
        conn.execute("SELECT 1 FROM concepts WHERE concept_id = 'METHOD:标准假设法';").fetchone()
        is None
    )

    # Legacy ledger row without qc_backup must still revert cleanly without crashing
    conn.execute("UPDATE concepts SET redirect_to_id = 'c_leg_to', aliases_json = '[]' WHERE concept_id = 'c_leg_from';")
    conn.execute("UPDATE concepts SET aliases_json = '[\"旧母题1\"]' WHERE concept_id = 'c_leg_to';")
    conn.execute(
        """
        INSERT INTO compaction_audit_ledger(
            batch_id, target_kind, scope_json, from_id, to_id,
            ai_judge_evidence, previous_aliases_to_json, affected_child_ids_json
        )
        VALUES ('legacy_c_b1', 'CONCEPT', '{"kind": "MOTIF"}', 'c_leg_from', 'c_leg_to', 'legacy', '[]', '[]');
        """
    )
    conn.commit()
    rev_leg = revert_merge_batch(conn, "legacy_c_b1", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    assert rev_leg["reverted_count"] == 1
    assert conn.execute("SELECT redirect_to_id FROM concepts WHERE concept_id = 'c_leg_from';").fetchone()[0] is None
    assert conn.execute("SELECT aliases_json FROM concepts WHERE concept_id = 'c_leg_to';").fetchone()[0] == "[]"
    conn.close()


# ===========================================================================
# A4: Version tuple comparison & full incremental migration (1.2 -> 1.11)
# ===========================================================================


def test_a4_version_parsing_and_comparison() -> None:
    assert _parse_version_tuple("1.10") == (1, 10)
    assert _parse_version_tuple("v1.2.3") == (1, 2, 3)
    assert _parse_version_tuple("V2.0") == (2, 0)
    assert _parse_version_tuple("draft-1") is None
    assert _parse_version_tuple("") is None

    assert _compare_versions("1.2", "1.10") == -1
    assert _compare_versions("1.10", "1.2") == 1
    assert _compare_versions("1.9", "1.11") == -1
    assert _compare_versions("v1.10", "1.10") == 0
    assert _compare_versions("1.2.2", "1.2.10") == -1
    assert _compare_versions("draft-1", "draft-2") == -1


@pytest.mark.asyncio
async def test_a4_migrate_taxonomy_incremental_multihop_full_graph_migration(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "a4.db")
    init_db(conn)
    cfg_dir = tmp_path / "config"
    vec_dir = tmp_path / "vectors"
    human_dir = tmp_path / "human"

    # Initial LOCKED taxonomy at version 1.2
    v12_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="LOCKED",
        version="1.2",
        modules=[
            TaxonomyL1Entry(
                name="旧几何",
                l2_entities=[
                    TaxonomyL2Entry(name="旧等高", aliases=["等高别名"]),
                    TaxonomyL2Entry(name="鸟头旧类"),
                    TaxonomyL2Entry(name="风筝旧类"),
                ],
            ),
            TaxonomyL1Entry(
                name="数论基础",
                l2_entities=[TaxonomyL2Entry(name="整除与余数", aliases=["旧余数别名"])],
            ),
        ],
    )
    v12_yaml = cfg_dir / "taxonomies" / f"taxonomy_{SLUG}.yaml"
    save_taxonomy_spec(v12_spec, v12_yaml)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.2', taxonomy_file = ? WHERE grade_subject = ?;",
        (str(v12_yaml), GS),
    )

    # Seed L2 and L3 nodes for v1.2
    def _ins_node(level: str, l1: str, l2: str, l3: str = "", aliases: list[str] | None = None) -> str:
        nid = make_node_id(SLUG, "1.2", level, l1, l2, l3)  # type: ignore[arg-type]
        conn.execute(
            """
            INSERT INTO knowledge_nodes(
                node_id, grade_subject, taxonomy_version, node_level,
                l1_module, l2_entity, l3_operator, aliases_json
            )
            VALUES (?, ?, '1.2', ?, ?, ?, ?, ?);
            """,
            (nid, GS, level, l1, l2, l3, json.dumps(aliases or [], ensure_ascii=False)),
        )
        return nid

    l2_eq_12 = _ins_node("L2", "旧几何", "旧等高", "", ["等高别名"])
    l2_nt_12 = _ins_node("L2", "旧几何", "鸟头旧类", "")
    l2_kt_12 = _ins_node("L2", "旧几何", "风筝旧类", "")
    l2_div_12 = _ins_node("L2", "数论基础", "整除与余数", "", ["旧余数别名"])

    l3_eq_child_a = _ins_node("L3", "旧几何", "旧等高", "底乘高折半算")
    l3_eq_child_b = _ins_node("L3", "旧几何", "旧等高", "等高面积比例法")
    l3_nt_shared = _ins_node("L3", "旧几何", "鸟头旧类", "共享比例算子", ["鸟头别名"])
    l3_kt_shared = _ins_node("L3", "旧几何", "风筝旧类", "共享比例算子", ["风筝别名"])
    l3_div_keep = _ins_node("L3", "数论基础", "整除与余数", "同余可加性")

    # Questions:
    # - q_rename: in 旧几何/旧等高 (bound to compacted child l3_eq_child_a) + secondary L1 旧几何
    # - q_merge_a & q_merge_b: in 鸟头旧类 & 风筝旧类 (both share l3_operator="共享比例算子", merged into 共角与风筝模型)
    # - q_unchanged: in 数论基础/整除与余数 (completely unchanged L1/L2, bound to l3_div_keep)
    _seed_q(
        conn,
        "q_rename",
        primary_l1="旧几何",
        secondary_l1s=["数论基础"],
        steps=[("旧几何", "旧等高", "底乘高折半算", l3_eq_child_a)],
    )
    _seed_q(
        conn,
        "q_merge_a",
        primary_l1="旧几何",
        steps=[("旧几何", "鸟头旧类", "共享比例算子", l3_nt_shared)],
    )
    _seed_q(
        conn,
        "q_merge_b",
        primary_l1="旧几何",
        steps=[("旧几何", "风筝旧类", "共享比例算子", l3_kt_shared)],
    )
    _seed_q(
        conn,
        "q_unchanged",
        primary_l1="数论基础",
        steps=[("数论基础", "整除与余数", "同余可加性", l3_div_keep)],
    )
    conn.commit()

    # Perform a Cluster-then-Assign merge in v1.2 on (旧几何, 旧等高) to create ledger + redirected children
    ca_12 = apply_cluster_assignments(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict={
            "grade_subject": GS,
            "taxonomy_version": "1.2",
            "l1_module": "旧几何",
            "l2_entity": "旧等高",
        },
        canonical_operators=[{"canonical_name": "标准等高算子", "description": "标准"}],
        assignments=[
            {"item_id": l3_eq_child_a, "raw_l3": "底乘高折半算", "canonical_name": "标准等高算子"},
            {"item_id": l3_eq_child_b, "raw_l3": "等高面积比例法", "canonical_name": "标准等高算子"},
        ],
        batch_id="compact_12_b1",
        slug=SLUG,
        grade_subject=GS,
        taxonomy_version="1.2",
    )
    assert ca_12["applied"] == 2
    l3_eq_master_12 = make_node_id(SLUG, "1.2", "L3", "旧几何", "旧等高", "标准等高算子")

    # Add knowledge_edges:
    # 1) L2 edge l2_eq_12 -> l2_div_12 (PREREQUISITE, must survive!)
    # 2) L2 edge l2_nt_12 -> l2_kt_12 (CO_OCCURRENCE, becomes self-loop after merge -> dropped)
    # 3) L2 cycle-forming edges when 鸟头旧类 & 风筝旧类 merge:
    #    l2_div_12 -> l2_nt_12 (weight 0.9) and l2_kt_12 -> l2_div_12 (weight 0.2) -> lowest weight (0.2) broken!
    conn.execute(
        """
        INSERT INTO knowledge_edges(source_node_id, target_node_id, grade_subject, edge_type, weight)
        VALUES
        (?, ?, ?, 'PREREQUISITE', 0.8),
        (?, ?, ?, 'CO_OCCURRENCE', 0.6),
        (?, ?, ?, 'PREREQUISITE', 0.9),
        (?, ?, ?, 'PREREQUISITE', 0.2);
        """,
        (
            l2_eq_12,
            l2_div_12,
            GS,
            l2_nt_12,
            l2_kt_12,
            GS,
            l2_div_12,
            l2_nt_12,
            GS,
            l2_kt_12,
            l2_div_12,
            GS,
        ),
    )

    # Add practice_log stuck on l3_eq_master_12
    conn.execute(
        """
        INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, stuck_step_index, stuck_node_id, stuck_sub_goal)
        VALUES (101, 'default', 'q_rename', 'WRONG', 1, ?, '等高比例卡点');
        """,
        (l3_eq_master_12,),
    )

    # Add active human_overrides on q_rename (primary_l1_module + taxonomy_override_json)
    conn.execute(
        """
        INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint, taxonomy_version, is_active)
        VALUES
        ('q_rename', 'primary_l1_module', '旧几何', 'PI_CLI', 'h_q_rename', '1.2', 1),
        ('q_rename', 'taxonomy_override_json', ?, 'PI_CLI', 'h_q_rename', '1.2', 1);
        """,
        (
            json.dumps(
                {
                    "taxonomy_version": "1.2",
                    "mode": "ADD",
                    "nodes": [{"l1": "旧几何", "l2": "旧等高", "l3": "标准等高算子", "node_id": l3_eq_master_12}],
                },
                ensure_ascii=False,
            ),
        ),
    )
    effective.recompute_all(conn, GS)
    conn.commit()

    # Build v1.2 l3_nodes vector store
    settings = Settings()
    settings.pipeline.use_mock_llm = True
    vstore = VectorStore(vec_dir)
    emb = EmbeddingClient(settings, conn=conn, use_mock=True)
    try:
        active_12_l3s = conn.execute(
            """
            SELECT node_id, l1_module, l2_entity, l3_operator
            FROM knowledge_nodes
            WHERE grade_subject = ? AND taxonomy_version = '1.2' AND node_level = 'L3' AND redirect_to_id IS NULL
            ORDER BY node_id;
            """,
            (GS,),
        ).fetchall()
        await vstore.sync_vectors(
            "l3_nodes",
            [(str(r["node_id"]), f"{r['l1_module']}/{r['l2_entity']}/{r['l3_operator']}") for r in active_12_l3s],
            emb,
            scope_slug=SLUG,
        )
    finally:
        await emb.aclose()

    # Target LOCKED taxonomy v1.11 with multi-hop, out-of-order changelog across .10 boundary!
    v111_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="LOCKED",
        version="1.11",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                l2_entities=[
                    TaxonomyL2Entry(name="终极等高模型", aliases=["等高新别名"]),
                    TaxonomyL2Entry(name="共角与风筝模型"),
                ],
            ),
            TaxonomyL1Entry(
                name="数论基础",
                l2_entities=[TaxonomyL2Entry(name="整除与余数", aliases=["全新余数别名"])],
            ),
        ],
        changelog=[
            # Out-of-range earlier entry (must be ignored!)
            TaxonomyChangelogEntry(
                from_version="1.0",
                to_version="1.1",
                actions=[
                    TaxonomyChangeAction(op="rename", level="L1", target="数论基础", new_name="不应触发数论改名")
                ],
            ),
            # Deliberately unsorted: 1.10 -> 1.11 placed BEFORE 1.2 -> 1.9 and 1.9 -> 1.10
            TaxonomyChangelogEntry(
                from_version="1.10",
                to_version="1.11",
                actions=[
                    TaxonomyChangeAction(
                        op="rename", level="L2", parent_l1="平面几何", target="中间等高", new_name="终极等高模型"
                    ),
                    TaxonomyChangeAction(
                        op="merge", level="L2", parent_l1="平面几何", target="风筝旧类", merge_into="共角与风筝模型"
                    ),
                ],
            ),
            TaxonomyChangelogEntry(
                from_version="1.9",
                to_version="1.10",
                actions=[
                    TaxonomyChangeAction(op="rename", level="L1", target="中间几何", new_name="平面几何"),
                    TaxonomyChangeAction(
                        op="rename", level="L2", parent_l1="平面几何", target="鸟头旧类", new_name="共角与风筝模型"
                    ),
                ],
            ),
            TaxonomyChangelogEntry(
                from_version="1.2",
                to_version="1.9",
                actions=[
                    TaxonomyChangeAction(op="rename", level="L1", target="旧几何", new_name="中间几何"),
                    TaxonomyChangeAction(
                        op="rename", level="L2", parent_l1="中间几何", target="旧等高", new_name="中间等高"
                    ),
                ],
            ),
            # Out-of-range future entry (must be ignored!)
            TaxonomyChangelogEntry(
                from_version="1.11",
                to_version="1.12",
                actions=[
                    TaxonomyChangeAction(op="rename", level="L1", target="平面几何", new_name="未来版本几何")
                ],
            ),
        ],
    )
    v111_yaml = cfg_dir / "taxonomies" / f"taxonomy_{SLUG}.v1.11.yaml"
    save_taxonomy_spec(v111_spec, v111_yaml)

    # 1. Dry-run verifies numeric version filtering & multi-hop transitive resolution
    dry = migrate_taxonomy_incremental(
        conn,
        grade_subject=GS,
        new_yaml_path=v111_yaml,
        dry_run=True,
        vectors_dir=vec_dir,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "bk",
        human_dir=human_dir,
        lock_path=tmp_path / ".lock",
    )
    assert dry["l1_rewrites"]["旧几何"] == "平面几何"
    assert "数论基础" not in dry["l1_rewrites"]
    assert dry["l2_rewrites"]["旧几何::旧等高"] == "平面几何::终极等高模型"
    assert dry["l2_rewrites"]["旧几何::鸟头旧类"] == "平面几何::共角与风筝模型"
    assert dry["l2_rewrites"]["旧几何::风筝旧类"] == "平面几何::共角与风筝模型"

    # 2. Execute incremental migration 1.2 -> 1.11
    res = migrate_taxonomy_incremental(
        conn,
        grade_subject=GS,
        new_yaml_path=v111_yaml,
        vectors_dir=vec_dir,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "bk",
        human_dir=human_dir,
        lock_path=tmp_path / ".lock",
    )
    assert res["from_version"] == "1.2"
    assert res["to_version"] == "1.11"
    assert res["affected_questions"] == 3  # q_rename, q_merge_a, q_merge_b; q_unchanged excluded!
    assert res["migrated_practice_logs"] == 1
    assert res["nulled_practice_logs"] == 0
    assert res["migrated_l3_vectors"] == 3  # 标准等高算子 + 共享比例算子 (merged) + 同余可加性

    # Zero FK violations and zero leftover v1.2 nodes or ghost 旧几何 subtrees
    assert conn.execute("PRAGMA foreign_key_check;").fetchall() == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE grade_subject = ? AND taxonomy_version != '1.11';",
            (GS,),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE grade_subject = ? AND l1_module = '旧几何';",
            (GS,),
        ).fetchone()[0]
        == 0
    )

    # Fresh target_ver node_ids & updated aliases on unchanged L2
    new_l2_div = make_node_id(SLUG, "1.11", "L2", "数论基础", "整除与余数", "")
    div_row = conn.execute(
        "SELECT aliases_json FROM knowledge_nodes WHERE node_id = ?;", (new_l2_div,)
    ).fetchone()
    assert div_row is not None
    assert json.loads(div_row["aliases_json"]) == ["全新余数别名"]

    # Merged L3 node retains combined aliases from both merged L2s
    new_l3_shared = make_node_id(SLUG, "1.11", "L3", "平面几何", "共角与风筝模型", "共享比例算子")
    shared_row = conn.execute(
        "SELECT aliases_json FROM knowledge_nodes WHERE node_id = ?;", (new_l3_shared,)
    ).fetchone()
    assert shared_row is not None
    assert set(json.loads(shared_row["aliases_json"])) == {"鸟头别名", "风筝别名"}

    # Step bindings preserved & remapped (NEVER needlessly cleared to NULL!)
    new_l3_eq_child_a = make_node_id(SLUG, "1.11", "L3", "平面几何", "终极等高模型", "底乘高折半算")
    new_l3_eq_master = make_node_id(SLUG, "1.11", "L3", "平面几何", "终极等高模型", "标准等高算子")
    new_l3_div_keep = make_node_id(SLUG, "1.11", "L3", "数论基础", "整除与余数", "同余可加性")

    st_ren = conn.execute(
        "SELECT m_l1, m_l2, m_knowledge_node_id FROM question_steps WHERE question_uid = 'q_rename';"
    ).fetchone()
    assert (st_ren["m_l1"], st_ren["m_l2"], st_ren["m_knowledge_node_id"]) == (
        "平面几何",
        "终极等高模型",
        new_l3_eq_child_a,
    )
    st_unc = conn.execute(
        "SELECT m_l1, m_l2, m_knowledge_node_id FROM question_steps WHERE question_uid = 'q_unchanged';"
    ).fetchone()
    assert (st_unc["m_l1"], st_unc["m_l2"], st_unc["m_knowledge_node_id"]) == (
        "数论基础",
        "整除与余数",
        new_l3_div_keep,
    )

    # Effective eff_node_ids_json refreshed for both affected and unchanged (node-remapped) questions
    assert json.loads(
        conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid = 'q_unchanged';").fetchone()[0]
    ) == [new_l3_div_keep]
    assert json.loads(
        conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid = 'q_rename';").fetchone()[0]
    ) == [new_l3_eq_master]

    # Compile state: affected marked S3D/S4 STALE; unchanged stays OK!
    assert (
        conn.execute(
            "SELECT status FROM compile_state WHERE question_uid = 'q_rename' AND stage = 'S3D';"
        ).fetchone()[0]
        == "STALE"
    )
    assert (
        conn.execute(
            "SELECT status FROM compile_state WHERE question_uid = 'q_unchanged' AND stage = 'S3D';"
        ).fetchone()[0]
        == "OK"
    )

    # Edges migrated (not cascade-deleted), self-loop skipped, cycle broken on lowest weight (0.2)
    new_l2_eq = make_node_id(SLUG, "1.11", "L2", "平面几何", "终极等高模型", "")
    new_l2_merged = make_node_id(SLUG, "1.11", "L2", "平面几何", "共角与风筝模型", "")
    edges = [
        (str(r["source_node_id"]), str(r["target_node_id"]), str(r["edge_type"]))
        for r in conn.execute(
            "SELECT source_node_id, target_node_id, edge_type FROM knowledge_edges WHERE grade_subject = ? ORDER BY source_node_id, target_node_id;",
            (GS,),
        ).fetchall()
    ]
    assert set(edges) == {
        (new_l2_eq, new_l2_div, "PREREQUISITE"),
        (new_l2_div, new_l2_merged, "PREREQUISITE"),
    }

    # practice_log migrated to v1.11 L3 node_id
    assert (
        conn.execute("SELECT stuck_node_id FROM practice_log WHERE log_id = 101;").fetchone()[0]
        == new_l3_eq_master
    )

    # human_overrides migrated with refreshed v1.11 node_id
    ov_prim = conn.execute(
        "SELECT override_value, taxonomy_version FROM human_overrides WHERE question_uid = 'q_rename' AND field_name = 'primary_l1_module';"
    ).fetchone()
    assert (ov_prim["override_value"], ov_prim["taxonomy_version"]) == ("平面几何", "1.11")
    ov_tax = conn.execute(
        "SELECT override_value, taxonomy_version FROM human_overrides WHERE question_uid = 'q_rename' AND field_name = 'taxonomy_override_json';"
    ).fetchone()
    assert ov_tax["taxonomy_version"] == "1.11"
    ov_tax_obj = json.loads(ov_tax["override_value"])
    assert ov_tax_obj["taxonomy_version"] == "1.11"
    assert ov_tax_obj["nodes"][0]["l1"] == "平面几何"
    assert ov_tax_obj["nodes"][0]["l2"] == "终极等高模型"
    assert ov_tax_obj["nodes"][0]["node_id"] == new_l3_eq_master

    # Vectors migrated to v1.11 active L3 IDs
    vec_ids, _ = vstore.load_vectors("l3_nodes", scope_slug=SLUG)
    assert set(vec_ids) == {new_l3_eq_master, new_l3_shared, new_l3_div_keep}

    # compaction_audit_ledger migrated to v1.11 and revert_merge_batch works 100% losslessly!
    # Clear practice_log & override refs to master so created_master cleanup can also be verified
    conn.execute("UPDATE practice_log SET stuck_node_id = NULL WHERE log_id = 101;")
    conn.execute("UPDATE human_overrides SET is_active = 0 WHERE question_uid = 'q_rename' AND field_name = 'taxonomy_override_json';")
    conn.commit()
    rev = revert_merge_batch(conn, "compact_12_b1", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    assert rev["reverted_count"] == 2
    assert json.loads(
        conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid = 'q_rename';").fetchone()[0]
    ) == [new_l3_eq_child_a]
    assert conn.execute("SELECT 1 FROM knowledge_nodes WHERE node_id = ?;", (new_l3_eq_master,)).fetchone() is None
    conn.close()


def test_a4_l1_merge_primary_into_existing_secondary_deduplicates_secondary(tmp_path: Path) -> None:
    """Regression absorbed from Repro #2: merging primary L1 into an already-listed secondary L1 deduplicates secondary."""
    conn = get_connection(tmp_path / "a4_l1_merge_sec_dup.db")
    init_db(conn)

    cfg_dir = tmp_path / "config"
    v1_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="LOCKED",
        version="1.0",
        modules=[
            TaxonomyL1Entry(name="L1_Old", l2_entities=[TaxonomyL2Entry(name="L2_A")]),
            TaxonomyL1Entry(name="L1_Target", l2_entities=[TaxonomyL2Entry(name="L2_B")]),
        ],
    )
    v1_yaml = cfg_dir / "taxonomies" / f"taxonomy_{SLUG}.yaml"
    save_taxonomy_spec(v1_spec, v1_yaml)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0', taxonomy_file = ? WHERE grade_subject = ?;",
        (str(v1_yaml), GS),
    )

    l2_a = make_node_id(SLUG, "1.0", "L2", "L1_Old", "L2_A", "")
    l2_b = make_node_id(SLUG, "1.0", "L2", "L1_Target", "L2_B", "")
    l3_a = make_node_id(SLUG, "1.0", "L3", "L1_Old", "L2_A", "算子A")
    l3_b = make_node_id(SLUG, "1.0", "L3", "L1_Target", "L2_B", "算子B")
    for nid, lvl, l1, l2, l3 in (
        (l2_a, "L2", "L1_Old", "L2_A", ""),
        (l2_b, "L2", "L1_Target", "L2_B", ""),
        (l3_a, "L3", "L1_Old", "L2_A", "算子A"),
        (l3_b, "L3", "L1_Target", "L2_B", "算子B"),
    ):
        conn.execute(
            """
            INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
            VALUES (?, ?, '1.0', ?, ?, ?, ?);
            """,
            (nid, GS, lvl, l1, l2, l3),
        )

    # q1: primary=L1_Old, secondary=["L1_Target"] (primary merges into existing secondary)
    # q2: primary=L1_Target, secondary=["L1_Old"] (secondary merges into existing primary)
    _seed_q(conn, "q1", primary_l1="L1_Old", secondary_l1s=["L1_Target"], steps=[("L1_Old", "L2_A", "算子A", l3_a)])
    _seed_q(conn, "q2", primary_l1="L1_Target", secondary_l1s=["L1_Old"], steps=[("L1_Target", "L2_B", "算子B", l3_b)])
    conn.commit()

    v2_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="LOCKED",
        version="1.1",
        modules=[
            TaxonomyL1Entry(
                name="L1_Target",
                l2_entities=[TaxonomyL2Entry(name="L2_A"), TaxonomyL2Entry(name="L2_B")],
            ),
        ],
        changelog=[
            TaxonomyChangelogEntry(
                from_version="1.0",
                to_version="1.1",
                actions=[
                    TaxonomyChangeAction(op="merge", level="L1", target="L1_Old", merge_into="L1_Target"),
                ],
            )
        ],
    )
    v2_yaml = tmp_path / "v1.1.yaml"
    save_taxonomy_spec(v2_spec, v2_yaml)

    res = migrate_taxonomy_incremental(
        conn,
        grade_subject=GS,
        new_yaml_path=v2_yaml,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "bk",
        human_dir=tmp_path / "human",
        lock_path=tmp_path / ".lock",
        vectors_dir=tmp_path / "vec",
    )
    assert res["affected_questions"] == 2

    for qid in ("q1", "q2"):
        row = conn.execute(
            """
            SELECT m_primary_l1_module, m_secondary_l1_modules_json,
                   eff_primary_l1_module, eff_secondary_l1_modules_json
            FROM questions WHERE question_uid = ?;
            """,
            (qid,),
        ).fetchone()
        assert row is not None
        assert row["m_primary_l1_module"] == "L1_Target"
        assert row["eff_primary_l1_module"] == "L1_Target"
        assert json.loads(row["m_secondary_l1_modules_json"]) == []
        assert json.loads(row["eff_secondary_l1_modules_json"]) == []
    conn.close()
