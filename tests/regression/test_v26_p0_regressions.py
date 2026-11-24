"""v2.6 P0 对抗审查回归测试（吸收 B1 / B6 / B7 实证 Repro 用例）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from shuati.config import Settings
from shuati.core import effective
from shuati.core.knowledge_graph import apply_cluster_assignments, revert_merge_batch
from shuati.core.topology import (
    generate_candidate_edges,
    load_question_features,
    weave_single_question,
)
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.vectors.store import VectorStore

GS = "小学奥数"
L1 = "平面几何"
L2 = "等高模型"


def _seed_q(
    conn: Any,
    uid: str,
    *,
    text: str = "T",
    skel: str = "S",
    c: int = 2,
    s: int = 2,
    t: int = 2,
    l_val: int = 1,
    steps: list[tuple[str, str, str, str | None]] | None = None,
    motifs: list[str] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, ?, 'BATCH_JSONL', 'c', ?, ?, ?);",
        (uid, GS, uid, text, f"h_{uid}"),
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status, m_abstract_skeleton, m_primary_l1_module, m_c_score, m_s_score, m_t_score, m_l_score) VALUES (?, ?, ?, '解答题', 'VERIFIED_CONSENSUS', ?, ?, ?, ?, ?, ?);",
        (uid, GS, text, skel, L1, c, s, t, l_val),
    )
    conn.execute(
        "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', 'S');",
        (f"{uid}::b1", uid),
    )
    for idx, (sl1, sl2, sl3, nid) in enumerate(steps or [], start=1):
        conn.execute(
            "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, 'branch_1', ?, 'g', ?, ?, ?, ?, ?, ?);",
            (f"{uid}::b1::{idx}", uid, idx, sl1, sl2, sl3, sl1, sl2, nid),
        )
    for m in motifs or []:
        cid = f"MOTIF:{m}"
        conn.execute("INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed) VALUES (?, 'MOTIF', ?, 0);", (cid, m))
        conn.execute("INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'MOTIF', 'MACHINE');", (uid, cid))
    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK');",
        (uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)


def test_b1_weave_single_preserves_llm_verified_wide_edges(tmp_path: Path) -> None:
    """B1: weave_single_question 不得抹除目标题既有 LLM_VERIFIED 宽进精裁边。"""
    conn = get_connection(tmp_path / "b1.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;", (GS,))
    for nid, l3 in (("n1", "op_low"), ("n3", "op_high")):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, ?, '1.0', 'L3', ?, ?, ?);",
            (nid, GS, L1, L2, l3),
        )
    _seed_q(conn, "q_child", text="child", skel="base area skeleton", c=1, s=1, t=1, l_val=1, steps=[(L1, L2, "op_low", "n1")], motifs=["shared_motif"])
    _seed_q(conn, "q_parent", text="parent", skel="extended area skeleton", c=3, s=2, t=2, l_val=1, steps=[(L1, L2, "op_high", "n3")], motifs=["shared_motif"])
    conn.commit()

    settings = Settings()
    store = VectorStore(tmp_path / "vecs")
    uids = ["q_child", "q_parent"]
    surf = np.asarray([[1, 0, 0, 0], [0, 0, 0, 1]], dtype=np.float32)
    skel = np.asarray([[1, 0, 0, 0], [0.86, 0, 0, 0.51]], dtype=np.float32)
    store._save_npz(store.npz_path("surface", "primary_olympiad"), uids, ["h"] * 2, surf)
    store._save_npz(store.npz_path("skeleton", "primary_olympiad"), uids, ["h"] * 2, skel)
    store._save_meta(
        {
            "embedding_model": settings.llm.embedding.model,
            "dimension": 4,
            "meta_version": 1,
            "quantiles": {
                "primary_olympiad::surface": [i / 100.0 for i in range(101)],
                "primary_olympiad::skeleton": [i / 100.0 for i in range(101)],
            },
        }
    )

    feats = load_question_features(conn, grade_subject=GS)
    cands = generate_candidate_edges(
        conn, store, grade_subject=GS, scope_slug="primary_olympiad", topology_cfg=settings.topology, features=feats
    )
    wide = [e for e in cands if e.refine_reason.startswith("WIDE_RECALL_")]
    assert wide
    w = wide[0]
    conn.execute(
        "INSERT INTO question_relations(source_question_uid, target_question_uid, relation_type, is_directed, is_cross_scope, "
        "surface_sim, skeleton_sim, surface_pct, skeleton_pct, containment_score, trap_overlap, confidence_source, "
        "pedagogical_note, rule_version) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'LLM_VERIFIED', 'llm ok', 'v')",
        (
            w.source_uid,
            w.target_uid,
            w.relation_type,
            1 if w.relation_type == "SCAFFOLD" else 0,
            w.surface_sim,
            w.skeleton_sim,
            w.surface_pct,
            w.skeleton_pct,
            w.containment_score,
            w.trap_overlap,
        ),
    )
    conn.commit()

    weave_single_question(conn, store, question_uid="q_child", topology_cfg=settings.topology)
    conn.commit()
    row = conn.execute(
        "SELECT confidence_source, pedagogical_note FROM question_relations WHERE source_question_uid=? AND target_question_uid=? AND relation_type=?;",
        (w.source_uid, w.target_uid, w.relation_type),
    ).fetchone()
    assert row is not None
    assert row["confidence_source"] == "LLM_VERIFIED"
    assert row["pedagogical_note"] == "llm ok"
    conn.close()


def test_b6_revert_cluster_assign_deletes_empty_created_master_nodes(tmp_path: Path) -> None:
    """B6: revert_merge_batch 无损回滚时应清理本批新建且无残余引用的空规范主节点（L3 & CONCEPT）。"""
    conn = get_connection(tmp_path / "b6.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;", (GS,))
    for nid, nm in (("n1", "op_alpha"), ("n2", "op_beta")):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, ?, '1.0', 'L3', ?, ?, ?);",
            (nid, GS, L1, L2, nm),
        )
    _seed_q(conn, "q1", steps=[(L1, L2, "op_alpha", "n1")])
    _seed_q(conn, "q2", steps=[(L1, L2, "op_beta", "n2")])
    conn.execute("INSERT INTO concepts(concept_id, kind, name, is_seed) VALUES ('c1', 'METHOD', 'meth_a', 0), ('c2', 'METHOD', 'meth_b', 0);")
    conn.commit()

    before_l3 = {
        r[0]
        for r in conn.execute("SELECT l3_operator FROM knowledge_nodes WHERE node_level='L3' AND redirect_to_id IS NULL;")
    }
    before_c = {
        r[0]
        for r in conn.execute("SELECT name FROM concepts WHERE kind='METHOD' AND redirect_to_id IS NULL;")
    }

    scope = {"grade_subject": GS, "taxonomy_version": "1.0", "l1_module": L1, "l2_entity": L2}
    res_l3 = apply_cluster_assignments(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict=scope,
        canonical_operators=[{"canonical_name": "canon_new", "description": ""}],
        assignments=[
            {"raw_l3": "op_alpha", "canonical_name": "canon_new"},
            {"raw_l3": "op_beta", "canonical_name": "canon_new"},
        ],
        batch_id="ghost_b1",
        slug="primary_olympiad",
        grade_subject=GS,
        taxonomy_version="1.0",
    )
    assert res_l3["canonical_nodes_created"] == 1
    revert_merge_batch(conn, "ghost_b1", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    after_l3 = {
        r[0]
        for r in conn.execute("SELECT l3_operator FROM knowledge_nodes WHERE node_level='L3' AND redirect_to_id IS NULL;")
    }
    assert after_l3 == before_l3

    res_c = apply_cluster_assignments(
        conn,
        target_kind="CONCEPT",
        scope_dict={"kind": "METHOD"},
        canonical_operators=[{"canonical_name": "canon_meth_new", "description": ""}],
        assignments=[
            {"raw_l3": "meth_a", "canonical_name": "canon_meth_new"},
            {"raw_l3": "meth_b", "canonical_name": "canon_meth_new"},
        ],
        batch_id="ghost_c1",
    )
    assert res_c["canonical_nodes_created"] == 1
    revert_merge_batch(conn, "ghost_c1", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    after_c = {
        r[0]
        for r in conn.execute("SELECT name FROM concepts WHERE kind='METHOD' AND redirect_to_id IS NULL;")
    }
    assert after_c == before_c
    conn.close()


def test_b7_cluster_assign_stale_master_cache_keeps_max_hop_le_1(tmp_path: Path) -> None:
    """B7: 即使批内某规范节点随后被二次重定向，master_cache 须实时追踪根代表以保跳数 <= 1。"""
    conn = get_connection(tmp_path / "b7.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;", (GS,))
    for nid, nm in (("a", "op_a"), ("b", "op_b"), ("x", "op_x"), ("y", "op_y")):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, ?, '1.0', 'L3', ?, ?, ?);",
            (nid, GS, L1, L2, nm),
        )
        _seed_q(conn, f"q_{nid}", steps=[(L1, L2, nm, nid)])
    conn.execute(
        "INSERT INTO concepts(concept_id, kind, name, is_seed) VALUES "
        "('ca', 'MOTIF', 'm_a', 0), ('cb', 'MOTIF', 'm_b', 0), ('cx', 'MOTIF', 'm_x', 0), ('cy', 'MOTIF', 'm_y', 0);"
    )
    conn.commit()

    scope = {"grade_subject": GS, "taxonomy_version": "1.0", "l1_module": L1, "l2_entity": L2}
    apply_cluster_assignments(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict=scope,
        canonical_operators=[],
        assignments=[
            {"raw_l3": "op_a", "canonical_name": "op_x"},
            {"raw_l3": "op_x", "canonical_name": "op_y"},
            {"raw_l3": "op_b", "canonical_name": "op_x"},
        ],
        batch_id="hop_l3",
        slug="primary_olympiad",
        grade_subject=GS,
        taxonomy_version="1.0",
    )
    redirects_l3 = {
        r[0]: r[1]
        for r in conn.execute("SELECT node_id, redirect_to_id FROM knowledge_nodes WHERE node_level='L3';").fetchall()
    }
    assert redirects_l3 == {"a": "y", "b": "y", "x": "y", "y": None}

    apply_cluster_assignments(
        conn,
        target_kind="CONCEPT",
        scope_dict={"kind": "MOTIF"},
        canonical_operators=[],
        assignments=[
            {"raw_l3": "m_a", "canonical_name": "m_x"},
            {"raw_l3": "m_x", "canonical_name": "m_y"},
            {"raw_l3": "m_b", "canonical_name": "m_x"},
        ],
        batch_id="hop_motif",
    )
    redirects_c = {
        r[0]: r[1]
        for r in conn.execute("SELECT concept_id, redirect_to_id FROM concepts WHERE kind='MOTIF';").fetchall()
    }
    assert redirects_c == {"ca": "cy", "cb": "cy", "cx": "cy", "cy": None}
    conn.close()
