"""Layer 2 单元测试：五边拓扑判定、多解分支覆盖、SHARED_TRAP 加权截断、relation_overrides 与单题补边。"""

from __future__ import annotations

import numpy as np

from shuati.config import Settings
from shuati.core import effective
from shuati.core.topology import (
    apply_degree_caps,
    generate_candidate_edges,
    load_question_features,
    merge_relation_overrides,
    normalize_edge_endpoints,
    weave_single_question,
    write_relations_for_scope,
)
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.vectors.store import VectorStore


def _add_topo_question(
    conn,
    uid: str,
    *,
    text: str,
    skel: str,
    elo: int,
    primary_l1: str,
    nodes_by_branch: dict[str, list[str]],
    traps: list[str] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'c', ?, ?, ?);",
        (uid, uid, text, f"h_{uid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_abstract_skeleton, m_primary_l1_module, m_base_elo
        )
        VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', ?, ?, ?);
        """,
        (uid, text, skel, primary_l1, elo),
    ) if False else None
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_abstract_skeleton, m_primary_l1_module
        )
        VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', ?, ?);
        """,
        (uid, text, skel, primary_l1),
    )
    # 用 difficulty_override_json? 不，直接由 m_c/s/t/l 或 override 设置 elo，这里使用 ANCHOR_CALIBRATION 覆写 difficulty_override_json 最干净
    conn.execute(
        """
        INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint)
        VALUES (?, 'difficulty_override_json', ?, 'ANCHOR_CALIBRATION', ?);
        """,
        (uid, f'{{"base_elo": {elo}, "tier": "L3_综合中档", "tier_rank": 3}}', f"h_{uid}"),
    )
    for b_idx, (bid, nids) in enumerate(nodes_by_branch.items()):
        conn.execute(
            "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, ?, ?, 'PRIMARY_VLM', 'S');",
            (f"{uid}::{bid}", uid, bid, 1 if b_idx == 0 else 0),
        )
        for s_idx, nid in enumerate(nids, start=1):
            conn.execute(
                "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, ?, ?, 'g', ?, 'L2', ?);",
                (f"{uid}::{bid}::{s_idx}", uid, bid, s_idx, primary_l1, nid),
            )
    for tr in traps or []:
        conn.execute(
            "INSERT INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
            (uid, tr),
        )
    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK');",
        (uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)


def test_five_edge_topology_caps_overrides_and_single_weave(tmp_path) -> None:
    assert normalize_edge_endpoints("CLONE", "q2", "q1") == ("q1", "q2")
    assert normalize_edge_endpoints("SCAFFOLD", "q2", "q1") == ("q2", "q1")

    conn = get_connection(tmp_path / "topo.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0' WHERE grade_subject = '小学奥数';")

    for nid in ("nA", "nB", "nC", "nD", "nE", "nF"):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, '小学奥数', '1.0', 'L3', '平面几何', '综合', ?);",
            (nid, f"算子{nid}"),
        )

    # 构造 7 道题验证各种边：
    # q1 & q2: CLONE (表皮、骨架、Elo 全极近)
    # q3: CONTRASTIVE vs q1 (表皮极似但知识点零重合)
    # q4: ISOMORPHIC vs q1 (表皮远但骨架极似且 K 重合)
    # q5: SCAFFOLD (Elo=1850 包含 q1(Elo=1500) 的 nA,nB 并扩展 nC,nD)
    # q6: SHARED_TRAP vs q1 (跨 L1 + 共享高稀缺陷阱 TRAP_FRAME_SHIFT)
    _add_topo_question(conn, "q1", text="长方形草地求面积", skel="几何面积比例", elo=1500, primary_l1="平面几何", nodes_by_branch={"branch_1": ["nA", "nB"]}, traps=["TRAP:TRAP_FRAME_SHIFT"])
    _add_topo_question(conn, "q2", text="长方形花园求面积", skel="几何面积比例2", elo=1520, primary_l1="平面几何", nodes_by_branch={"branch_1": ["nA", "nB"]}, traps=[])
    _add_topo_question(conn, "q3", text="长方形草地植树间距", skel="完全不同数列", elo=1500, primary_l1="组合与计数", nodes_by_branch={"branch_1": ["nE"]}, traps=[])
    _add_topo_question(conn, "q4", text="完全无关物理运动题干", skel="几何面积比例3", elo=1510, primary_l1="平面几何", nodes_by_branch={"branch_1": ["nA", "nB"]}, traps=[])
    _add_topo_question(conn, "q5", text="复杂几何综合证明", skel="几何面积比例拓展", elo=1850, primary_l1="平面几何", nodes_by_branch={"branch_1": ["nA", "nB", "nC", "nD"]}, traps=[])
    _add_topo_question(conn, "q6", text="火车过桥相对速度问题", skel="行程参考系", elo=1480, primary_l1="行程问题", nodes_by_branch={"branch_1": ["nF"]}, traps=["TRAP:TRAP_FRAME_SHIFT"])
    conn.commit()

    # 手工写入可控单位向量精确命中分位数与阈值
    store = VectorStore(tmp_path / "vectors")
    dim = 6
    surf_vecs = np.asarray(
        [
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],    # q1
            [0.999, 0.0447, 0.0, 0.0, 0.0, 0.0],  # q2 ~ q1 surface
            [0.998, 0.063, 0.0, 0.0, 0.0, 0.0],   # q3 ~ q1 surface
            [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],    # q4 far surface
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],    # q5
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],    # q6
        ],
        dtype=np.float32,
    )
    skel_vecs = np.asarray(
        [
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],    # q1
            [0.999, 0.0447, 0.0, 0.0, 0.0, 0.0],  # q2 ~ q1 skel
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],    # q3 far skel
            [0.998, 0.063, 0.0, 0.0, 0.0, 0.0],   # q4 ~ q1 skel
            [0.90, 0.4358, 0.0, 0.0, 0.0, 0.0],   # q5 close skel
            [0.0, 0.0, 0.0, 0.0, 1.0, 0.0],    # q6
        ],
        dtype=np.float32,
    )
    uids = ["q1", "q2", "q3", "q4", "q5", "q6"]
    hashes = ["h"] * 6
    store._save_npz(store.npz_path("surface", "primary_olympiad"), uids, hashes, surf_vecs)
    store._save_npz(store.npz_path("skeleton", "primary_olympiad"), uids, hashes, skel_vecs)
    store._save_meta(
        {
            "embedding_model": "mock-unit",
            "dimension": dim,
            "meta_version": 1,
            "quantiles": {
                "primary_olympiad::surface": [i / 100.0 for i in range(101)],
                "primary_olympiad::skeleton": [i / 100.0 for i in range(101)],
            },
        }
    )

    settings = Settings()
    # 校准 ISOMORPHIC 门限在本小数据集线性分位上自然触发
    settings.topology.thresholds.isomorphic.skeleton_pct = 0.90
    settings.topology.thresholds.clone.surface_pct = 0.95
    settings.topology.thresholds.clone.skeleton_pct = 0.95

    feats = load_question_features(conn, grade_subject="小学奥数")
    cands = generate_candidate_edges(
        conn, store, grade_subject="小学奥数", scope_slug="primary_olympiad", topology_cfg=settings.topology, features=feats
    )
    triples = {(e.source_uid, e.target_uid, e.relation_type): e for e in cands}

    assert ("q1", "q2", "CLONE") in triples
    assert ("q1", "q4", "ISOMORPHIC") in triples
    assert ("q5", "q1", "SCAFFOLD") in triples
    assert ("q1", "q3", "CONTRASTIVE") in triples
    assert ("q1", "q6", "SHARED_TRAP") in triples

    # 验证 CONTRASTIVE 打标精裁标志与 SHARED_TRAP 跨 L1 bonus
    assert triples[("q1", "q3", "CONTRASTIVE")].needs_llm_refine is True
    assert triples[("q1", "q6", "SHARED_TRAP")].rank_score > 1.0

    # 验证 relation_overrides 合并：REJECT 删边，ASSERT 写 HUMAN_VERIFIED
    conn.execute(
        """
        INSERT INTO relation_overrides(source_question_uid, target_question_uid, relation_type, action, pedagogical_note, source)
        VALUES
        ('q1', 'q2', 'CLONE', 'REJECT', '', 'PI_CLI'),
        ('q2', 'q4', 'ISOMORPHIC', 'ASSERT', '人工补齐同构变式', 'REVIEW_UI');
        """
    )
    conn.commit()
    capped = apply_degree_caps(cands, settings.topology)
    final = merge_relation_overrides(
        conn, capped, features=feats, vector_store=store, scope_slug="primary_olympiad", topology_cfg=settings.topology
    )
    write_relations_for_scope(conn, final, grade_subject="小学奥数")
    conn.commit()

    assert conn.execute("SELECT 1 FROM question_relations WHERE source_question_uid='q1' AND target_question_uid='q2' AND relation_type='CLONE';").fetchone() is None
    assert conn.execute("SELECT confidence_source FROM question_relations WHERE source_question_uid='q2' AND target_question_uid='q4' AND relation_type='ISOMORPHIC';").fetchone()["confidence_source"] == "HUMAN_VERIFIED"

    # 验证单题增量 weave_single_question 保持图一致
    res_single = weave_single_question(conn, store, question_uid="q5", topology_cfg=settings.topology)
    assert res_single["written"] >= 1
    assert conn.execute("SELECT 1 FROM question_relations WHERE source_question_uid='q5' AND target_question_uid='q1' AND relation_type='SCAFFOLD';").fetchone() is not None
    conn.close()
