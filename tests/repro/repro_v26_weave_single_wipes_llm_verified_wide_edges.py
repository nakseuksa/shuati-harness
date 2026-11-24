"""Repro v26: weave_single_question 过滤 WIDE_RECALL_* 后，会把该题已有的 LLM_VERIFIED 宽进精裁边整批删除（回归）。

完整 Stage 4 经 LLM 精裁落库的 WIDE_RECALL_SCAFFOLD/ISOMORPHIC 边（confidence_source=LLM_VERIFIED），
之后对其中一端题调用 weave_single_question（增量单题织网）：该函数先 DELETE 与 touched_peers 关联的全部边，
再只写回"existing 中非目标题的边 + 非 WIDE_RECALL 新候选"，
=> 目标题自身的 LLM_VERIFIED 宽进边既不在 existing(被 continue 跳过) 也不在 new_candidates(被过滤)，被永久抹除。
运行：.venv\\Scripts\\python.exe tests/repro/repro_v26_weave_single_wipes_llm_verified_wide_edges.py
期望修复后 Exit 0（已 LLM_VERIFIED 的边应被保留）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v26_common import GS, L1, L2, make_conn, seed_q  # noqa: E402

from shuati.config import Settings  # noqa: E402
from shuati.core import effective  # noqa: E402
from shuati.core.topology import generate_candidate_edges, load_question_features, weave_single_question  # noqa: E402
from shuati.vectors.store import VectorStore  # noqa: E402


def main() -> int:
    conn, d = make_conn("wipe")
    for nid, l3 in (("n1", "op_low"), ("n3", "op_high")):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) "
            "VALUES (?, ?, '1.0', 'L3', ?, ?, ?);",
            (nid, GS, L1, L2, l3),
        )
    seed_q(conn, "q_child", text="child", skel="base area skeleton", c=1, s=1, t=1, l_val=1, steps=[(L1, L2, "op_low", "n1")],
           motifs=["shared_motif"])
    seed_q(conn, "q_parent", text="parent", skel="extended area skeleton", c=3, s=2, t=2, l_val=1, steps=[(L1, L2, "op_high", "n3")],
           motifs=["shared_motif"])
    conn.commit()

    settings = Settings()
    store = VectorStore(d / "vecs")
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
    cands = generate_candidate_edges(conn, store, grade_subject=GS, scope_slug="primary_olympiad",
                                     topology_cfg=settings.topology, features=feats)
    wide = [e for e in cands if e.refine_reason.startswith("WIDE_RECALL_")]
    print("wide candidates:", [(e.source_uid, e.target_uid, e.relation_type, e.refine_reason) for e in wide])
    assert wide, "fixture 未产生宽进候选边（repro 夹具失效）"
    w = wide[0]
    # 模拟完整 Stage 4 LLM 精裁落库
    conn.execute(
        "INSERT INTO question_relations(source_question_uid, target_question_uid, relation_type, is_directed, is_cross_scope, "
        "surface_sim, skeleton_sim, surface_pct, skeleton_pct, containment_score, trap_overlap, confidence_source, "
        "pedagogical_note, rule_version) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'LLM_VERIFIED', 'llm ok', 'v')",
        (w.source_uid, w.target_uid, w.relation_type, 1 if w.relation_type == "SCAFFOLD" else 0, w.surface_sim, w.skeleton_sim,
         w.surface_pct, w.skeleton_pct, w.containment_score, w.trap_overlap),
    )
    conn.commit()
    n0 = conn.execute("SELECT COUNT(*) FROM question_relations").fetchone()[0]
    weave_single_question(conn, store, question_uid="q_child", topology_cfg=settings.topology)
    conn.commit()
    row = conn.execute(
        "SELECT confidence_source FROM question_relations WHERE source_question_uid=? AND target_question_uid=? AND relation_type=?;",
        (w.source_uid, w.target_uid, w.relation_type),
    ).fetchone()
    print("edges before:", n0, "| after:", conn.execute("SELECT COUNT(*) FROM question_relations").fetchone()[0],
          "| verified edge row:", tuple(row) if row else None)
    assert row is not None and row["confidence_source"] == "LLM_VERIFIED", "LLM_VERIFIED 宽进边被 weave_single_question 抹除"
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
