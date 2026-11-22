"""Layer 2 单元测试：同域硬隔离、全量双路召回、可逆并查集与路径压缩、乱序拒止与 Kahn DAG 破环。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shuati.config import Settings
from shuati.core import effective
from shuati.core.knowledge_graph import (
    apply_merge_pairs,
    check_dag_and_break_cycles,
    compute_l3_distribution_entropy,
    expand_canonical_concept_ids,
    expand_canonical_node_ids,
    recall_concept_merge_candidates,
    recall_l3_merge_candidates,
    revert_merge_batch,
)
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm.embeddings import EmbeddingClient
from shuati.vectors.store import VectorStore


@pytest.mark.asyncio
async def test_scope_isolation_and_path_compression_and_revert(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "kg.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status = 'DRAFT', taxonomy_version = 'draft-1' WHERE grade_subject = '小学奥数';")

    # 插入 L2 + 4 个 L3（其中 3 个同域，1 个跨 L2）
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        VALUES
        ('l2_eq', '小学奥数', 'draft-1', 'L2', '平面几何', '等高模型', ''),
        ('n1', '小学奥数', 'draft-1', 'L3', '平面几何', '等高模型', '同底等高三角形面积相等'),
        ('n2', '小学奥数', 'draft-1', 'L3', '平面几何', '等高模型', '同底等高面积相等性质'),
        ('n3', '小学奥数', 'draft-1', 'L3', '平面几何', '等高模型', '等高三角形面积转换'),
        ('n4_other', '小学奥数', 'draft-1', 'L3', '平面几何', '鸟头模型', '共角三角形面积比');
        """
    )

    for qid, nid in (("q1", "n1"), ("q2", "n2"), ("q3", "n3")):
        conn.execute(
            "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'c', ?, 'T', ?);",
            (qid, qid, qid),
        )
        conn.execute(
            "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status) VALUES (?, '小学奥数', 'T', '解答题', 'VERIFIED_CONSENSUS');",
            (qid,),
        )
        conn.execute(
            "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', 'S');",
            (f"{qid}::b1", qid),
        )
        conn.execute(
            "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, 'branch_1', 1, 'g', '平面几何', '等高模型', ?);",
            (f"{qid}::b1::1", qid, nid),
        )
        effective.recompute(conn, qid)

    # 非种子概念 + 种子 TRAP
    conn.execute(
        """
        INSERT INTO concepts(concept_id, kind, name, is_seed)
        VALUES
        ('c_m1', 'METHOD', '假设法', 0),
        ('c_m2', 'METHOD', '假设代入法', 0),
        ('c_mo1', 'MOTIF', '鸡兔同笼模型', 0);
        """
    )
    conn.execute("INSERT INTO question_concepts(question_uid, concept_id, kind, origin) VALUES ('q1', 'c_m1', 'METHOD', 'MACHINE'), ('q2', 'c_m1', 'METHOD', 'HUMAN'), ('q2', 'c_m2', 'METHOD', 'MACHINE');")
    conn.commit()

    # 1. 硬拒绝跨 L2 / 合并 L2 / 合并 TRAP / 跨 kind
    with pytest.raises(PreconditionError, match="L2"):
        apply_merge_pairs(conn, target_kind="KNOWLEDGE_NODE", scope_dict={}, merges=[{"from_id": "n1", "to_id": "l2_eq"}], batch_id="bad0")
    with pytest.raises(PreconditionError, match="跨作用域"):
        apply_merge_pairs(conn, target_kind="KNOWLEDGE_NODE", scope_dict={}, merges=[{"from_id": "n1", "to_id": "n4_other"}], batch_id="bad1")
    with pytest.raises(PreconditionError, match="is_seed=1"):
        apply_merge_pairs(conn, target_kind="CONCEPT", scope_dict={}, merges=[{"from_id": "c_m1", "to_id": "TRAP:TRAP_DOMAIN_BOUNDARY"}], batch_id="bad2")
    with pytest.raises(PreconditionError, match="TRAP"):
        recall_concept_merge_candidates(conn, VectorStore(tmp_path / "vecs"), kind="TRAP")  # type: ignore[arg-type]

    # 2. 全量双路召回验证严格作用域不跨组
    settings = Settings()
    client = EmbeddingClient(settings, use_mock=True)
    store = VectorStore(tmp_path / "vecs")
    await store.sync_vectors(
        "l3_nodes",
        [
            ("n1", "平面几何/等高模型/同底等高三角形面积相等"),
            ("n2", "平面几何/等高模型/同底等高面积相等性质"),
            ("n3", "平面几何/等高模型/等高三角形面积转换"),
            ("n4_other", "平面几何/鸟头模型/共角三角形面积比"),
        ],
        client,
        scope_slug="primary_olympiad",
    )
    await client.aclose()
    cands = recall_l3_merge_candidates(
        conn, store, grade_subject="小学奥数", scope_slug="primary_olympiad", taxonomy_version="draft-1"
    )
    assert len(cands) == 3  # n1,n2,n3 两两均在同 L2 组内，n4_other 孤立绝不混入
    assert all(c["id_a"] != "n4_other" and c["id_b"] != "n4_other" for c in cands)

    # 3. 连续两批合并验证路径压缩（跳数 <= 1）：n1 -> n2，再 n2 -> n3
    apply_merge_pairs(conn, target_kind="KNOWLEDGE_NODE", scope_dict={"l1": "平面几何"}, merges=[{"from_id": "n1", "to_id": "n2", "evidence": "b1"}], batch_id="batch_1")
    apply_merge_pairs(conn, target_kind="KNOWLEDGE_NODE", scope_dict={"l1": "平面几何"}, merges=[{"from_id": "n2", "to_id": "n3", "evidence": "b2"}], batch_id="batch_2")

    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = 'n1';").fetchone()["redirect_to_id"] == "n3"
    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = 'n2';").fetchone()["redirect_to_id"] == "n3"
    assert expand_canonical_node_ids(conn, "n3") == {"n1", "n2", "n3"}
    assert json.loads(conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid = 'q1';").fetchone()["eff_node_ids_json"]) == ["n3"]

    # 4. 概念归并去重 question_concepts 且优先保留 HUMAN 来源
    apply_merge_pairs(conn, target_kind="CONCEPT", scope_dict={"kind": "METHOD"}, merges=[{"from_id": "c_m2", "to_id": "c_m1", "evidence": "cm"}], batch_id="batch_c1")
    qc_q2 = conn.execute("SELECT concept_id, origin FROM question_concepts WHERE question_uid = 'q2';").fetchall()
    assert [(r["concept_id"], r["origin"]) for r in qc_q2] == [("c_m1", "HUMAN")]
    assert expand_canonical_concept_ids(conn, "c_m2") == {"c_m1", "c_m2"}

    # 5. 乱序回滚 batch_1 必须被拒绝；逆序 batch_2 -> batch_1 必须无损恢复
    with pytest.raises(PreconditionError, match="拒绝乱序回滚"):
        revert_merge_batch(conn, "batch_1", backups_dir=tmp_path / "backups", lock_path=tmp_path / ".lock")

    revert_merge_batch(conn, "batch_2", backups_dir=tmp_path / "backups", lock_path=tmp_path / ".lock")
    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = 'n1';").fetchone()["redirect_to_id"] == "n2"
    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = 'n2';").fetchone()["redirect_to_id"] is None

    revert_merge_batch(conn, "batch_1", backups_dir=tmp_path / "backups", lock_path=tmp_path / ".lock")
    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = 'n1';").fetchone()["redirect_to_id"] is None
    assert json.loads(conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid = 'q1';").fetchone()["eff_node_ids_json"]) == ["n1"]

    ent = compute_l3_distribution_entropy(conn, "小学奥数", "draft-1")
    assert ent["active_l3_nodes"] == 4
    assert ent["used_l3_nodes"] == 3
    assert ent["entropy"] > 1.0
    conn.close()


def test_kahn_dag_cycle_breaker() -> None:
    edges = [
        {"source_node_id": "A", "target_node_id": "B", "weight": 0.9},
        {"source_node_id": "B", "target_node_id": "C", "weight": 0.8},
        {"source_node_id": "C", "target_node_id": "A", "weight": 0.2},  # 最低权环边
        {"source_node_id": "C", "target_node_id": "D", "weight": 0.7},
    ]
    acyclic, broken = check_dag_and_break_cycles(edges)
    assert len(broken) == 1
    assert broken[0]["source_node_id"] == "C" and broken[0]["target_node_id"] == "A"
    assert len(acyclic) == 3
    # 二次检查应确认无环
    _, broken2 = check_dag_and_break_cycles(acyclic)
    assert broken2 == []
