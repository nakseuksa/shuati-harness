"""Layer 3/4 集成测试：Stage Q 7大板块体检报告、--prune-runs 与 4 份标准 JSONL 导出。"""

from __future__ import annotations

import json
from pathlib import Path

from shuati.config import Settings
from shuati.core import effective
from shuati.db import human_mirror
from shuati.db.connection import get_connection
from shuati.db.exporter import export_all_jsonl
from shuati.db.schema import init_db
from shuati.pipeline.audit import run_stage_q_audit


def test_audit_prune_and_four_jsonl_exports(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "audit.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0' WHERE grade_subject = '小学奥数';")

    for uid, retired in (("q_ok", 0), ("q_ret", 1)):
        conn.execute(
            "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'c', ?, 'T', ?);",
            (uid, uid, uid),
        )
        conn.execute(
            "INSERT INTO questions(question_uid, grade_subject, is_retired, m_question_text, m_format, m_verification_status, m_primary_l1_module) VALUES (?, '小学奥数', ?, '测试题目', '解答题', 'VERIFIED_CONSENSUS', '平面几何');",
            (uid, retired),
        )
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK'), (?, 'S4', 'OK');",
            (uid, uid, uid, uid, uid),
        )
        effective.recompute(conn, uid)

    # 1 条未引用 FAILED run 与 1 条 OK run
    conn.execute("INSERT INTO stage1_vlm_runs(question_uid, role, model_name, prompt_version, input_hash, run_status) VALUES ('q_ok', 'PRIMARY', 'm', 'v', 'h', 'FAILED');")
    conn.execute("INSERT INTO stage1_vlm_runs(question_uid, role, model_name, prompt_version, input_hash, run_status) VALUES ('q_ok', 'PRIMARY', 'm', 'v', 'h', 'OK');")
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES ('l2_a', '小学奥数', '1.0', 'L2', '平面几何', '等高模型', ''), ('l2_b', '小学奥数', '1.0', 'L2', '数论基础', '整除', ''), ('l3_a', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '同底等高');"
    )
    conn.execute(
        "INSERT INTO knowledge_edges(source_node_id, target_node_id, grade_subject, edge_type, weight) VALUES ('l2_a', 'l2_b', '小学奥数', 'PREREQUISITE', 0.9);"
    )
    conn.commit()
    human_mirror.export_snapshot(conn, human_dir=tmp_path / "human")

    settings = Settings()
    audit_res = run_stage_q_audit(
        conn,
        settings,
        human_dir=tmp_path / "human",
        vectors_dir=tmp_path / "vectors",
        reports_dir=tmp_path / "reports",
        prune_runs=True,
    )
    rep = audit_res["report"]
    assert Path(audit_res["md_path"]).exists()
    assert Path(audit_res["json_path"]).exists()
    assert rep["section_1_compile_coverage"]["total_questions"] == 2
    assert rep["section_2_verification_sympy"]["pruned_failed_runs"] == 1
    assert rep["section_5_integrity_and_cost"]["effective_consistency"]["diff_count"] == 0
    assert rep["section_5_integrity_and_cost"]["human_mirror_consistency"]["is_consistent"] is True

    # 默认导出排除 retired 题，传 include_retired=True 全部导出
    exp1 = export_all_jsonl(conn, export_dir=tmp_path / "exports1")
    assert exp1["counts"]["questions_enriched"] == 1
    assert exp1["counts"]["knowledge_nodes"] == 3
    assert exp1["counts"]["knowledge_edges"] == 1
    q_line = json.loads((tmp_path / "exports1" / "questions_enriched.jsonl").read_text(encoding="utf-8").strip())
    assert q_line["question_uid"] == "q_ok"

    exp2 = export_all_jsonl(conn, export_dir=tmp_path / "exports2", include_retired=True)
    assert exp2["counts"]["questions_enriched"] == 2
    conn.close()
