"""NFR-1 标准 JSONL 导出器（脱离 SQLite 独立阅读与下游使用）。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from shuati.core.knowledge_graph import find_canonical_concept_id, find_canonical_node_id


def _atomic_write_jsonl(target: Path, lines: list[str]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    content = "\n".join(lines) + ("\n" if lines else "")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def export_all_jsonl(
    conn: sqlite3.Connection,
    *,
    export_dir: Path | str = Path("data/exports"),
    grade_subject: str | None = None,
    include_retired: bool = False,
) -> dict[str, Any]:
    """导出 4 份标准 JSONL：questions_enriched / knowledge_nodes / knowledge_edges / question_relations。"""
    out_dir = Path(export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. questions_enriched.jsonl（仅读 eff_* 与活跃归并实体）
    clauses = ["1=1"]
    params: list[Any] = []
    if grade_subject:
        clauses.append("q.grade_subject = ?")
        params.append(grade_subject)
    if not include_retired:
        clauses.append("q.is_retired = 0 AND q.eff_is_excluded = 0")

    where_sql = " AND ".join(clauses)
    q_rows = conn.execute(
        f"""
        SELECT q.question_uid, q.grade_subject, q.cas_images_json, q.image_captions_json,
               q.has_images, q.is_retired, q.retired_reason,
               q.eff_question_text, q.eff_options_json, q.eff_format, q.eff_visual_facts,
               q.eff_verification_status, q.eff_review_reason, q.eff_verification_mode,
               q.eff_final_answers_json, q.eff_abstract_skeleton,
               q.eff_primary_l1_module, q.eff_secondary_l1_modules_json,
               q.eff_node_ids_json, q.eff_c_score, q.eff_s_score, q.eff_t_score, q.eff_l_score,
               q.eff_base_elo, q.eff_tier, q.eff_tier_rank, q.eff_is_excluded, q.eff_is_servable,
               q.eff_overridden_fields_json
        FROM questions q
        WHERE {where_sql}
        ORDER BY q.question_uid;
        """,
        params,
    ).fetchall()

    exported_uids = [str(r["question_uid"]) for r in q_rows]
    exported_uid_set = set(exported_uids)

    # 预加载解决方案与步骤
    sol_by_q: dict[str, list[dict[str, Any]]] = {}
    steps_by_q: dict[str, list[dict[str, Any]]] = {}
    concepts_by_q: dict[str, dict[str, list[str]]] = {}
    concept_cache: dict[str, str | None] = {}
    concept_names: dict[str, str] = {}
    node_cache: dict[str, str | None] = {}

    def _canon_concept(cid: str) -> str | None:
        if cid not in concept_cache:
            concept_cache[cid] = find_canonical_concept_id(conn, cid)
        return concept_cache[cid]

    def _canon_node(nid: str) -> str | None:
        if nid not in node_cache:
            node_cache[nid] = find_canonical_node_id(conn, nid)
        return node_cache[nid]

    if exported_uids:
        for cr in conn.execute("SELECT concept_id, name FROM concepts;").fetchall():
            concept_names[str(cr["concept_id"])] = str(cr["name"])

        for sr in conn.execute(
            "SELECT question_uid, branch_id, is_optimal, source_origin, method_summary, solution_markdown, sympy_verified FROM question_solutions ORDER BY question_uid, is_optimal DESC, branch_id;"
        ).fetchall():
            uid = str(sr["question_uid"])
            if uid in exported_uid_set:
                sol_by_q.setdefault(uid, []).append(dict(sr))

        for str_row in conn.execute(
            "SELECT question_uid, branch_id, step_index, sub_goal, core_formula, hint, m_l1, m_l2, m_knowledge_node_id FROM question_steps ORDER BY question_uid, branch_id, step_index;"
        ).fetchall():
            uid = str(str_row["question_uid"])
            if uid in exported_uid_set:
                d = dict(str_row)
                raw_nid = d.get("m_knowledge_node_id")
                d["canonical_node_id"] = _canon_node(str(raw_nid)) if raw_nid else None
                steps_by_q.setdefault(uid, []).append(d)

        for qcr in conn.execute(
            "SELECT question_uid, concept_id, kind FROM question_concepts ORDER BY question_uid, concept_id;"
        ).fetchall():
            uid = str(qcr["question_uid"])
            if uid in exported_uid_set:
                canon_c = _canon_concept(str(qcr["concept_id"])) or str(qcr["concept_id"])
                cname = concept_names.get(canon_c, canon_c)
                bucket = concepts_by_q.setdefault(uid, {"METHOD": [], "MOTIF": [], "TRAP": []})
                kind_key = str(qcr["kind"])
                if cname not in bucket[kind_key]:
                    bucket[kind_key].append(cname)

    q_lines: list[str] = []
    for r in q_rows:
        uid = str(r["question_uid"])
        c_dict = concepts_by_q.get(uid, {"METHOD": [], "MOTIF": [], "TRAP": []})
        item = {
            "question_uid": uid,
            "grade_subject": str(r["grade_subject"]),
            "cas_images": json.loads(r["cas_images_json"] or "[]"),
            "image_captions": json.loads(r["image_captions_json"] or "[]"),
            "has_images": bool(r["has_images"]),
            "is_retired": bool(r["is_retired"]),
            "retired_reason": str(r["retired_reason"]),
            "eff_question_text": str(r["eff_question_text"]),
            "eff_options": json.loads(r["eff_options_json"] or "{}"),
            "eff_format": str(r["eff_format"]),
            "eff_visual_facts": str(r["eff_visual_facts"]),
            "eff_verification_status": str(r["eff_verification_status"]),
            "eff_review_reason": str(r["eff_review_reason"]),
            "eff_verification_mode": str(r["eff_verification_mode"]),
            "eff_final_answers": json.loads(r["eff_final_answers_json"] or "[]"),
            "eff_abstract_skeleton": str(r["eff_abstract_skeleton"]),
            "eff_primary_l1_module": str(r["eff_primary_l1_module"]),
            "eff_secondary_l1_modules": json.loads(r["eff_secondary_l1_modules_json"] or "[]"),
            "eff_node_ids": json.loads(r["eff_node_ids_json"] or "[]"),
            "eff_c_score": r["eff_c_score"],
            "eff_s_score": r["eff_s_score"],
            "eff_t_score": r["eff_t_score"],
            "eff_l_score": r["eff_l_score"],
            "eff_base_elo": r["eff_base_elo"],
            "eff_tier": r["eff_tier"],
            "eff_tier_rank": r["eff_tier_rank"],
            "eff_is_excluded": bool(r["eff_is_excluded"]),
            "eff_is_servable": bool(r["eff_is_servable"]),
            "eff_overridden_fields": json.loads(r["eff_overridden_fields_json"] or "[]"),
            "canonical_concepts": c_dict,
            "solutions": sol_by_q.get(uid, []),
            "steps": steps_by_q.get(uid, []),
        }
        q_lines.append(json.dumps(item, ensure_ascii=False))

    q_file = out_dir / "questions_enriched.jsonl"
    _atomic_write_jsonl(q_file, q_lines)

    # 2. knowledge_nodes.jsonl（活跃 L2 + L3 节点）
    kn_where = "kn.redirect_to_id IS NULL" + (" AND kn.grade_subject = ?" if grade_subject else "")
    kn_params = [grade_subject] if grade_subject else []
    kn_rows = conn.execute(
        f"""
        SELECT kn.node_id, kn.grade_subject, kn.taxonomy_version, kn.node_level,
               kn.l1_module, kn.l2_entity, kn.l3_operator, kn.aliases_json, kn.created_at,
               COUNT(DISTINCT s.step_id) AS step_binding_count
        FROM knowledge_nodes kn
        LEFT JOIN knowledge_nodes member
          ON member.grade_subject = kn.grade_subject
         AND member.taxonomy_version = kn.taxonomy_version
         AND member.node_level = kn.node_level
         AND (member.node_id = kn.node_id OR member.redirect_to_id = kn.node_id)
        LEFT JOIN question_steps s ON s.m_knowledge_node_id = member.node_id
        WHERE {kn_where}
        GROUP BY kn.node_id
        ORDER BY kn.grade_subject, kn.node_level, kn.l1_module, kn.l2_entity, kn.l3_operator;
        """,
        kn_params,
    ).fetchall()
    kn_lines = [
        json.dumps(
            {
                "node_id": str(r["node_id"]),
                "grade_subject": str(r["grade_subject"]),
                "taxonomy_version": str(r["taxonomy_version"]),
                "node_level": str(r["node_level"]),
                "l1_module": str(r["l1_module"]),
                "l2_entity": str(r["l2_entity"]),
                "l3_operator": str(r["l3_operator"]),
                "aliases": json.loads(r["aliases_json"] or "[]"),
                "step_binding_count": int(r["step_binding_count"]),
                "created_at": str(r["created_at"]),
            },
            ensure_ascii=False,
        )
        for r in kn_rows
    ]
    kn_file = out_dir / "knowledge_nodes.jsonl"
    _atomic_write_jsonl(kn_file, kn_lines)

    # 3. knowledge_edges.jsonl
    ke_where = "ke.grade_subject = ?" if grade_subject else "1=1"
    ke_params = [grade_subject] if grade_subject else []
    ke_rows = conn.execute(
        f"""
        SELECT ke.source_node_id, ke.target_node_id, ke.grade_subject, ke.edge_type,
               ke.weight, ke.evidence_json, ke.created_at,
               sn.l1_module AS source_l1, sn.l2_entity AS source_l2,
               tn.l1_module AS target_l1, tn.l2_entity AS target_l2
        FROM knowledge_edges ke
        JOIN knowledge_nodes sn ON sn.node_id = ke.source_node_id
        JOIN knowledge_nodes tn ON tn.node_id = ke.target_node_id
        WHERE {ke_where}
        ORDER BY ke.edge_type, ke.weight DESC, ke.source_node_id, ke.target_node_id;
        """,
        ke_params,
    ).fetchall()
    ke_lines = [
        json.dumps(
            {
                "source_node_id": str(r["source_node_id"]),
                "target_node_id": str(r["target_node_id"]),
                "grade_subject": str(r["grade_subject"]),
                "edge_type": str(r["edge_type"]),
                "weight": float(r["weight"]),
                "source_l1": str(r["source_l1"]),
                "source_l2": str(r["source_l2"]),
                "target_l1": str(r["target_l1"]),
                "target_l2": str(r["target_l2"]),
                "evidence": json.loads(r["evidence_json"] or "{}"),
                "created_at": str(r["created_at"]),
            },
            ensure_ascii=False,
        )
        for r in ke_rows
    ]
    ke_file = out_dir / "knowledge_edges.jsonl"
    _atomic_write_jsonl(ke_file, ke_lines)

    # 4. question_relations.jsonl
    qr_rows = conn.execute(
        """
        SELECT source_question_uid, target_question_uid, relation_type, is_directed,
               is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
               containment_score, trap_overlap, confidence_source, pedagogical_note,
               rule_version, created_at
        FROM question_relations
        ORDER BY relation_type, source_question_uid, target_question_uid;
        """
    ).fetchall()
    qr_lines: list[str] = []
    for r in qr_rows:
        if str(r["source_question_uid"]) in exported_uid_set and str(r["target_question_uid"]) in exported_uid_set:
            d = dict(r)
            d["is_directed"] = bool(d["is_directed"])
            d["is_cross_scope"] = bool(d["is_cross_scope"])
            qr_lines.append(json.dumps(d, ensure_ascii=False))
    qr_file = out_dir / "question_relations.jsonl"
    _atomic_write_jsonl(qr_file, qr_lines)

    return {
        "export_dir": str(out_dir),
        "counts": {
            "questions_enriched": len(q_lines),
            "knowledge_nodes": len(kn_lines),
            "knowledge_edges": len(ke_lines),
            "question_relations": len(qr_lines),
        },
        "files": {
            "questions_enriched": str(q_file),
            "knowledge_nodes": str(kn_file),
            "knowledge_edges": str(ke_file),
            "question_relations": str(qr_file),
        },
    }
