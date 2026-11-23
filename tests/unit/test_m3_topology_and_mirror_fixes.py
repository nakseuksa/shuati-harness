"""M3 单元回归测试：v2.6 SCAFFOLD 方向、SHARED_TRAP 剔除计算笔误、并集 Jaccard 与 external_questions 镜像。"""

from __future__ import annotations

import numpy as np
import pytest

from shuati.config import Settings
from shuati.core import effective
from shuati.core.topology import (
    compute_idf_weights,
    generate_candidate_edges,
    load_question_features,
)
from shuati.db import human_mirror
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.vectors.store import VectorStore


def _seed_q(conn, uid: str, *, text: str, skel: str, elo: int, traps: list[str], nids: list[str]) -> None:
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'c', ?, ?, ?);",
        (uid, uid, text, f"h_{uid}"),
    )
    c, s, t, l_val = (
        (3, 1, 2, 1) if elo >= 1650 else
        (2, 2, 2, 1) if elo >= 1450 else
        (2, 1, 2, 1)
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status, m_abstract_skeleton, m_primary_l1_module, m_c_score, m_s_score, m_t_score, m_l_score) VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', ?, '平面几何', ?, ?, ?, ?);",
        (uid, text, skel, c, s, t, l_val),
    )
    conn.execute(
        "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解');",
        (f"{uid}::branch_1", uid),
    )
    for idx, nid in enumerate(nids, start=1):
        conn.execute(
            "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, 'branch_1', ?, 'step', '平面几何', '等高模型', ?);",
            (f"{uid}::branch_1::{idx}", uid, idx, nid),
        )
    for tr in traps:
        conn.execute(
            "INSERT INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
            (uid, tr),
        )
    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK');",
        (uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)


def test_scaffold_direction_and_arithmetic_slip_excluded(tmp_path) -> None:
    conn = get_connection(tmp_path / "t.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject='小学奥数';")
    for nid in ("n1", "n2", "n3", "n4"):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, '小学奥数', '1.0', 'L3', '平面几何', '等高模型', ?);",
            (nid, f"op_{nid}"),
        )

    # child (elo=1400) -> parent (elo=1700), 以及同分 q_same (elo=1400) 不得连 SCAFFOLD
    # t1 & t2 仅共享 TRAP_ARITHMETIC_SLIP，不得产生 SHARED_TRAP
    # t3 & t4 共享 TRAP_DOMAIN_BOUNDARY 且 t4 另带 TRAP_SIGN_DIRECTION，验证并集分母 < 1.0
    _seed_q(conn, "child", text="子题", skel="几何骨架A", elo=1400, traps=["TRAP:TRAP_ARITHMETIC_SLIP"], nids=["n1", "n2"])
    _seed_q(conn, "q_same", text="同分题", skel="几何骨架A2", elo=1400, traps=["TRAP:TRAP_ARITHMETIC_SLIP"], nids=["n1", "n2"])
    _seed_q(conn, "parent", text="母题", skel="几何骨架A拓展", elo=1700, traps=["TRAP:TRAP_DOMAIN_BOUNDARY"], nids=["n1", "n2", "n3", "n4"])
    _seed_q(conn, "t4", text="端点与符号题", skel="完全不同骨架Z", elo=1500, traps=["TRAP:TRAP_DOMAIN_BOUNDARY", "TRAP:TRAP_SIGN_DIRECTION"], nids=["n3"])
    conn.commit()

    store = VectorStore(tmp_path / "vec")
    uids = ["child", "parent", "q_same", "t4"]
    surf = np.eye(4, 6, dtype=np.float32)
    skel = np.asarray(
        [
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.95, 0.31, 0.0, 0.0, 0.0, 0.0],
            [0.99, 0.14, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    store._save_npz(store.npz_path("surface", "primary_olympiad"), uids, ["h"] * 4, surf)
    store._save_npz(store.npz_path("skeleton", "primary_olympiad"), uids, ["h"] * 4, skel)
    store._save_meta(
        {
            "embedding_model": "mock",
            "dimension": 6,
            "meta_version": 1,
            "quantiles": {
                "primary_olympiad::surface": [i / 100.0 for i in range(101)],
                "primary_olympiad::skeleton": [i / 100.0 for i in range(101)],
            },
        }
    )

    feats = load_question_features(conn, grade_subject="小学奥数")
    assert "T:TRAP:TRAP_ARITHMETIC_SLIP" not in feats["child"].e_traps
    idf_k, _ = compute_idf_weights(feats)
    assert all(v >= 1.0 for v in idf_k.values())

    settings = Settings()
    settings.topology.thresholds.shared_trap.idf_overlap_min = 0.30
    edges = generate_candidate_edges(
        conn, store, grade_subject="小学奥数", scope_slug="primary_olympiad", topology_cfg=settings.topology, features=feats
    )
    triples = {(e.source_uid, e.target_uid, e.relation_type): e for e in edges}

    # 1. SCAFFOLD 严格为 child -> parent，且无同分边与反向边
    assert ("child", "parent", "SCAFFOLD") in triples
    assert ("parent", "child", "SCAFFOLD") not in triples
    assert ("child", "q_same", "SCAFFOLD") not in triples
    assert ("q_same", "child", "SCAFFOLD") not in triples

    # 2. 仅共享 TRAP_ARITHMETIC_SLIP 的 (child, q_same) 绝不生成 SHARED_TRAP
    assert ("child", "q_same", "SHARED_TRAP") not in triples

    # 3. parent 与 t4 共享 1/2 并集陷阱，trap_overlap < 1.0（非旧版 min=1.0）
    st = triples.get(("parent", "t4", "SHARED_TRAP"))
    assert st is not None
    assert 0.3 < st.trap_overlap < 0.8
    conn.close()


def test_external_questions_mirror_and_restore_safety(tmp_path) -> None:
    conn = get_connection(tmp_path / "src.db")
    init_db(conn)
    uid = "primary_olympiad:ext:ext001"
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'EXTERNAL_MISTAKE', 'external_mistake', 'ext_001', '外部错题求面积', 'h_ext');",
        (uid,),
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status) VALUES (?, '小学奥数', '外部错题求面积', '解答题', 'VERIFIED_CONSENSUS');",
        (uid,),
    )
    effective.recompute(conn, uid)
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, outcome, score_ratio) VALUES ('default', ?, 'WRONG', 0.0);",
        (uid,),
    )
    conn.commit()

    h_dir = tmp_path / "human"
    counts = human_mirror.export_snapshot(conn, human_dir=h_dir)
    assert counts["external_questions"] == 1
    assert (h_dir / "external_questions.jsonl").exists()
    assert human_mirror.verify_snapshot(conn, human_dir=h_dir)["is_consistent"] is True

    # 空库仅执行 init_db，直接 restore 应自动恢复 external_questions 且不断链
    dst = get_connection(tmp_path / "dst.db")
    init_db(dst)
    res_counts = human_mirror.restore_snapshot(dst, human_dir=h_dir, backups_dir=None)
    assert res_counts["external_questions"] == 1
    assert res_counts["practice_log"] == 1
    assert dst.execute("SELECT eff_question_text FROM questions WHERE question_uid=?;", (uid,)).fetchone()["eff_question_text"] == "外部错题求面积"

    # 若快照含缺失题库外键引用，外键预检必须中止恢复
    bad_dir = tmp_path / "bad_human"
    bad_dir.mkdir()
    (bad_dir / "learners.jsonl").write_text((h_dir / "learners.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    (bad_dir / "practice_log.jsonl").write_text('{"log_id": 999, "learner_id": "default", "question_uid": "non_existent:q", "practiced_at": "2026-01-01", "outcome": "WRONG", "score_ratio": 0.0, "voids_log_id": null, "stuck_step_index": null, "stuck_node_id": null, "stuck_sub_goal": null, "hint_level_used": null, "user_note": "", "submitted_answer": null, "quiz_batch_id": null, "client_request_id": null}\n', encoding="utf-8")
    with pytest.raises(PreconditionError):
        human_mirror.restore_snapshot(dst, human_dir=bad_dir, backups_dir=None)

    # 若 practice_log.jsonl 为空且未传 force_wipe_logs，不得误删库中已有记录
    empty_pl_dir = tmp_path / "empty_pl_human"
    empty_pl_dir.mkdir()
    for fname in ("external_questions.jsonl", "learners.jsonl", "overrides.jsonl", "relation_overrides.jsonl"):
        (empty_pl_dir / fname).write_text((h_dir / fname).read_text(encoding="utf-8"), encoding="utf-8")
    (empty_pl_dir / "practice_log.jsonl").write_text("", encoding="utf-8")
    human_mirror.restore_snapshot(dst, human_dir=empty_pl_dir, backups_dir=None, include_practice_log=True, force_wipe_logs=False)
    assert dst.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0] == 1

    conn.close()
    dst.close()
