"""M3 交互式刷题核心应用服务（角色、检索、图谱导航、防剧透判题、做题日志与单字段覆写）。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from shuati.config import Settings
from shuati.core import effective, profiler, refresh
from shuati.core.knowledge_graph import expand_canonical_concept_ids, expand_canonical_node_ids
from shuati.db import human_mirror, repository
from shuati.db.connection import PreconditionError, SingleWriterExecutor
from shuati.db.repository import NotFoundError
from shuati.llm.embeddings import EmbeddingClient
from shuati.llm.gateway import LLMGateway
from shuati.llm.prompt_builder import build_qualitative_judge_messages
from shuati.models.domain import AnswerPart
from shuati.models.schemas import QualitativeJudgeOutput
from shuati.services.reveal import (
    RevealMode,
    apply_check_reveal,
    apply_related_reveal,
    apply_reveal,
    resolve_cas_image_paths,
)
from shuati.vectors.store import VectorStore
from shuati.verify.answer_parts import (
    normalize_answer_parts,
    normalize_choice_label,
    normalize_part_label,
    normalize_symbol_fill,
)
from shuati.verify.equivalence import compare_answer_parts
from shuati.verify.sympy_sandbox import split_top_level


def _run_coro_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()

_DEFAULT_WRITER = SingleWriterExecutor()

_ALLOWED_OVERRIDE_FIELDS: set[str] = {
    "question_text",
    "visual_facts",
    "final_answers_json",
    "solutions_override_json",
    "difficulty_override_json",
    "primary_l1_module",
    "secondary_l1_modules_json",
    "taxonomy_override_json",
    "thinking_methods_json",
    "classic_motifs_json",
    "trap_tags_json",
    "abstract_skeleton",
    "format",
    "verification_status",
    "is_excluded",
}

_PAREN_EXPR_GUARD = (
    r"(?!\s*(?:[*/^×÷·]|\\(?:times|cdot|div)\b)\s*\S|\s+[+\-]\s+\S|[+\-]\s*\S|[(（])"
)
_PREFIX_LABEL_RE = re.compile(
    rf"^\s*(主问|\(\d+\){_PAREN_EXPR_GUARD}|（\d+）{_PAREN_EXPR_GUARD}|[①-⑳]|\d+\){_PAREN_EXPR_GUARD}|\d+\.(?!\d)(?=\s|[:：]|[^\d.])|\d+、|第\s*\(?\d+\)?\s*问|问\s*\(?\d+\)?)\s*[:：]?\s*(.*)$"
)


def _safe_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def resolve_learner_id(conn: sqlite3.Connection, learner_id: str | None = None) -> str:
    """解析目标学习者（未传时取当前 is_current=1 角色，回退 default）。"""
    if learner_id:
        row = conn.execute(
            "SELECT learner_id FROM learners WHERE learner_id = ?;",
            (learner_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"学习者角色 '{learner_id}' 不存在")
        return str(row["learner_id"])

    cur = conn.execute("SELECT learner_id FROM learners WHERE is_current = 1 LIMIT 1;").fetchone()
    if cur:
        return str(cur["learner_id"])
    fallback = conn.execute("SELECT learner_id FROM learners WHERE learner_id = 'default';").fetchone()
    if fallback:
        return "default"
    raise NotFoundError("库内尚无任何学习者角色，请先执行 learner create")


def list_learners(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM learners ORDER BY is_current DESC, learner_id ASC;").fetchall()
    return [dict(r) for r in rows]


def create_learner(
    conn: sqlite3.Connection,
    *,
    learner_id: str,
    display_name: str,
    default_grade_subject: str = "小学奥数",
    set_current: bool = False,
    notes: str = "",
    human_dir: Path | str = Path("human"),
    include_practice_log: bool = True,
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    lid = learner_id.strip()
    if not lid:
        raise PreconditionError("learner_id 不能为空")
    gs_exists = conn.execute(
        "SELECT 1 FROM grade_subjects WHERE grade_subject = ?;",
        (default_grade_subject,),
    ).fetchone()
    if not gs_exists:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科 '{default_grade_subject}' 不存在")
    if conn.execute("SELECT 1 FROM learners WHERE learner_id = ?;", (lid,)).fetchone():
        raise PreconditionError(f"学习者 '{lid}' 已存在")

    active_writer = writer or _DEFAULT_WRITER
    with active_writer.transaction(conn):
        if set_current:
            conn.execute("UPDATE learners SET is_current = 0 WHERE is_current = 1;")
        conn.execute(
            """
            INSERT INTO learners(learner_id, display_name, default_grade_subject, is_current, notes)
            VALUES (?, ?, ?, ?, ?);
            """,
            (lid, display_name.strip() or lid, default_grade_subject, 1 if set_current else 0, notes),
        )
    human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=include_practice_log)
    row = conn.execute("SELECT * FROM learners WHERE learner_id = ?;", (lid,)).fetchone()
    return dict(row)


def use_learner(
    conn: sqlite3.Connection,
    learner_id: str,
    *,
    human_dir: Path | str = Path("human"),
    include_practice_log: bool = True,
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    lid = resolve_learner_id(conn, learner_id)
    active_writer = writer or _DEFAULT_WRITER
    with active_writer.transaction(conn):
        conn.execute("UPDATE learners SET is_current = 0 WHERE is_current = 1;")
        conn.execute("UPDATE learners SET is_current = 1 WHERE learner_id = ?;", (lid,))
    human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=include_practice_log)
    row = conn.execute("SELECT * FROM learners WHERE learner_id = ?;", (lid,)).fetchone()
    return dict(row)


def hydrate_question_dict(
    conn: sqlite3.Connection,
    question_uid: str,
    *,
    assets_dir: Path | str = Path("data/assets/images"),
    reason_public: str = "",
    reason_full: str = "",
    pedagogical_note: str = "",
) -> dict[str, Any]:
    """加载完整题面字典（供 apply_reveal 按档裁剪）。"""
    q_row = conn.execute(
        """
        SELECT q.*, r.source_id
        FROM questions q
        JOIN raw_questions r ON r.question_uid = q.question_uid
        WHERE q.question_uid = ?;
        """,
        (question_uid,),
    ).fetchone()
    if not q_row:
        raise NotFoundError(f"题目 '{question_uid}' 不存在")

    sol_rows = conn.execute(
        """
        SELECT branch_id, is_optimal, source_origin, method_summary, solution_markdown, sympy_verified
        FROM question_solutions
        WHERE question_uid = ?
        ORDER BY is_optimal DESC, branch_id ASC;
        """,
        (question_uid,),
    ).fetchall()

    solutions: list[dict[str, Any]] = []
    for s in sol_rows:
        bid = str(s["branch_id"])
        st_rows = conn.execute(
            """
            SELECT step_index, sub_goal, core_formula, hint, m_l1, m_l2, m_knowledge_node_id
            FROM question_steps
            WHERE question_uid = ? AND branch_id = ?
            ORDER BY step_index ASC;
            """,
            (question_uid, bid),
        ).fetchall()
        solutions.append(
            {
                "branch_id": bid,
                "is_optimal": bool(s["is_optimal"]),
                "source_origin": s["source_origin"],
                "method_summary": s["method_summary"],
                "solution_markdown": s["solution_markdown"],
                "sympy_verified": bool(s["sympy_verified"]),
                "steps": [dict(st) for st in st_rows],
            }
        )

    eff_node_ids = _safe_json(q_row["eff_node_ids_json"], [])
    l2_entities: list[str] = []
    l3_nodes: list[dict[str, str]] = []
    if eff_node_ids:
        ph = ",".join("?" for _ in eff_node_ids)
        for nr in conn.execute(
            f"SELECT node_id, l1_module, l2_entity, l3_operator FROM knowledge_nodes WHERE node_id IN ({ph});",
            eff_node_ids,
        ).fetchall():
            if nr["l2_entity"] and nr["l2_entity"] not in l2_entities:
                l2_entities.append(str(nr["l2_entity"]))
            l3_nodes.append(
                {
                    "node_id": str(nr["node_id"]),
                    "l1_module": str(nr["l1_module"]),
                    "l2_entity": str(nr["l2_entity"]),
                    "l3_operator": str(nr["l3_operator"]),
                }
            )

    ov_fields_set = set(_safe_json(q_row["eff_overridden_fields_json"], []))
    methods: list[str] = []
    motifs: list[str] = []
    traps: list[str] = []
    if ov_fields_set & {"thinking_methods_json", "classic_motifs_json", "trap_tags_json"}:
        for ov_r in conn.execute(
            """
            SELECT field_name, override_value
            FROM human_overrides
            WHERE question_uid = ?
              AND field_name IN ('thinking_methods_json', 'classic_motifs_json', 'trap_tags_json')
              AND is_active = 1 AND is_suspended = 0;
            """,
            (question_uid,),
        ).fetchall():
            fn_ov = str(ov_r["field_name"])
            parsed_ov = _safe_json(ov_r["override_value"], [])
            raw_seq = (
                (parsed_ov.get("items") or parsed_ov.get("tags") or [])
                if isinstance(parsed_ov, dict)
                else (parsed_ov if isinstance(parsed_ov, list) else [])
            )
            c_kind_ov = (
                "METHOD"
                if fn_ov == "thinking_methods_json"
                else ("MOTIF" if fn_ov == "classic_motifs_json" else "TRAP")
            )
            target_list = methods if c_kind_ov == "METHOD" else (motifs if c_kind_ov == "MOTIF" else traps)
            for item_v in raw_seq:
                raw_name = str(item_v or "").strip()
                if not raw_name:
                    continue
                canon_row = conn.execute(
                    """
                    SELECT COALESCE(m.name, c.name) AS canon_name
                    FROM concepts c
                    LEFT JOIN concepts m ON m.concept_id = c.redirect_to_id
                    WHERE c.kind = ? AND c.name = ?
                    LIMIT 1;
                    """,
                    (c_kind_ov, raw_name),
                ).fetchone()
                cname_ov = str(canon_row["canon_name"]) if canon_row else raw_name
                if cname_ov not in target_list:
                    target_list.append(cname_ov)

    for cr in conn.execute(
        """
        SELECT qc.kind, COALESCE(m.name, c.name) AS canon_name
        FROM question_concepts qc
        JOIN concepts c ON c.concept_id = qc.concept_id
        LEFT JOIN concepts m ON m.concept_id = c.redirect_to_id
        WHERE qc.question_uid = ?
        ORDER BY qc.kind ASC, canon_name ASC;
        """,
        (question_uid,),
    ).fetchall():
        cname = str(cr["canon_name"])
        if cr["kind"] == "METHOD":
            if "thinking_methods_json" in ov_fields_set:
                continue
            if cname not in methods:
                methods.append(cname)
        elif cr["kind"] == "MOTIF":
            if "classic_motifs_json" in ov_fields_set:
                continue
            if cname not in motifs:
                motifs.append(cname)
        elif cr["kind"] == "TRAP":
            if "trap_tags_json" in ov_fields_set:
                continue
            if cname not in traps:
                traps.append(cname)

    cas_images = _safe_json(q_row["cas_images_json"], [])
    return {
        "question_uid": question_uid,
        "source_id": str(q_row["source_id"] or ""),
        "grade_subject": str(q_row["grade_subject"]),
        "format": str(q_row["eff_format"] or ""),
        "question_text": str(q_row["eff_question_text"] or ""),
        "options": _safe_json(q_row["eff_options_json"], {}),
        "has_images": bool(q_row["has_images"]),
        "cas_images": cas_images,
        "image_paths": resolve_cas_image_paths(cas_images, assets_dir=assets_dir),
        "image_captions": _safe_json(q_row["image_captions_json"], []),
        "base_elo": int(q_row["eff_base_elo"] or 1500),
        "tier": str(q_row["eff_tier"] or ""),
        "tier_rank": int(q_row["eff_tier_rank"] or 3),
        "primary_l1_module": str(q_row["eff_primary_l1_module"] or ""),
        "secondary_l1_modules": _safe_json(q_row["eff_secondary_l1_modules_json"], []),
        "verification_status": str(q_row["eff_verification_status"] or ""),
        "is_servable": bool(q_row["eff_is_servable"]),
        "visual_facts": str(q_row["eff_visual_facts"] or ""),
        "abstract_skeleton": str(q_row["eff_abstract_skeleton"] or ""),
        "final_answers": _safe_json(q_row["eff_final_answers_json"], []),
        "solutions": solutions,
        "difficulty_evidence": _safe_json(q_row["difficulty_evidence_json"], {}),
        "c_score": q_row["eff_c_score"],
        "s_score": q_row["eff_s_score"],
        "t_score": q_row["eff_t_score"],
        "l_score": q_row["eff_l_score"],
        "l2_entities": l2_entities,
        "l3_nodes": l3_nodes,
        "thinking_methods": methods,
        "classic_motifs": motifs,
        "trap_tags": traps,
        "reason_public": reason_public,
        "reason_full": reason_full,
        "pedagogical_note": pedagogical_note,
    }


def browse_taxonomy(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    learner_id: str | None = None,
    l1: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """浏览学段知识树、先修 DAG、题量与学习者掌握热力。"""
    gs = conn.execute(
        "SELECT * FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if not gs:
        raise NotFoundError(f"学段学科 '{grade_subject}' 不存在")

    stale_cnt = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM compile_state cs
        JOIN questions q ON q.question_uid = cs.question_uid
        WHERE q.grade_subject = ? AND cs.stage IN ('S3C', 'S3D') AND cs.status = 'STALE';
        """,
        (grade_subject,),
    ).fetchone()["cnt"]
    maint_degraded = (str(gs["taxonomy_status"]) != "LOCKED") or int(stale_cnt) > 0

    where_l1 = "AND l1_module = ?" if l1 else ""
    params: list[Any] = [grade_subject]
    if l1:
        params.append(l1.strip())

    nodes = conn.execute(
        f"""
        SELECT node_id, node_level, l1_module, l2_entity, l3_operator, aliases_json
        FROM knowledge_nodes
        WHERE grade_subject = ? AND redirect_to_id IS NULL {where_l1}
        ORDER BY l1_module ASC, l2_entity ASC, node_level ASC, l3_operator ASC;
        """,
        params,
    ).fetchall()

    # 预加载本学段单跳并查集归集映射：node_id -> COALESCE(redirect_to_id, node_id)
    node_canon_map: dict[str, str] = {
        str(r["node_id"]): str(r["canon_id"])
        for r in conn.execute(
            """
            SELECT node_id, COALESCE(redirect_to_id, node_id) AS canon_id
            FROM knowledge_nodes
            WHERE grade_subject = ?;
            """,
            (grade_subject,),
        ).fetchall()
    }

    # 统计可出题挂载题量（单题内按归集后主节点去重）
    q_counts_l1: dict[str, int] = {}
    q_counts_l3: dict[str, int] = {}
    for qr in conn.execute(
        """
        SELECT eff_primary_l1_module, eff_node_ids_json
        FROM questions
        WHERE grade_subject = ? AND is_retired = 0 AND eff_is_excluded = 0 AND eff_is_servable = 1;
        """,
        (grade_subject,),
    ).fetchall():
        pl1 = str(qr["eff_primary_l1_module"] or "")
        if pl1:
            q_counts_l1[pl1] = q_counts_l1.get(pl1, 0) + 1
        seen_canon_nids: set[str] = set()
        for raw_nid in _safe_json(qr["eff_node_ids_json"], []):
            nid_str = str(raw_nid)
            canon_nid = node_canon_map.get(nid_str, nid_str)
            if canon_nid not in seen_canon_nids:
                seen_canon_nids.add(canon_nid)
                q_counts_l3[canon_nid] = q_counts_l3.get(canon_nid, 0) + 1

    # 可选叠加学习者水位与薄弱节点热力
    profile_data: dict[str, Any] | None = None
    weak_l3_map: dict[str, float] = {}
    if learner_id:
        lid = resolve_learner_id(conn, learner_id)
        elo_cfg = (settings or Settings()).learner_elo
        profile_data = profiler.compute_learner_profile(
            conn, learner_id=lid, grade_subject=grade_subject, elo_cfg=elo_cfg
        )
        for item in profile_data["weak_radar"].get("l3", []):
            weak_l3_map[str(item["node_id"])] = float(item["weighted_score"])

    tree: dict[str, dict[str, Any]] = {}
    for nr in nodes:
        mod = str(nr["l1_module"])
        l2_ent = str(nr["l2_entity"])
        m_bucket = tree.setdefault(
            mod,
            {
                "l1_module": mod,
                "servable_question_count": q_counts_l1.get(mod, 0),
                "rating": (
                    profile_data["modules"].get(mod, {}).get("rating")
                    if profile_data
                    else None
                ),
                "l2_entities": {},
            },
        )
        l2_bucket = m_bucket["l2_entities"].setdefault(
            l2_ent,
            {"l2_entity": l2_ent, "l2_node_id": None, "l3_operators": []},
        )
        if nr["node_level"] == "L2":
            l2_bucket["l2_node_id"] = str(nr["node_id"])
        else:
            nid = str(nr["node_id"])
            l2_bucket["l3_operators"].append(
                {
                    "node_id": nid,
                    "l3_operator": str(nr["l3_operator"]),
                    "aliases": _safe_json(nr["aliases_json"], []),
                    "question_count": q_counts_l3.get(nid, 0),
                    "weak_score": weak_l3_map.get(nid, 0.0),
                }
            )

    modules_out: list[dict[str, Any]] = []
    for mod_entry in tree.values():
        mod_entry["l2_entities"] = list(mod_entry["l2_entities"].values())
        modules_out.append(mod_entry)

    edges = [
        dict(er)
        for er in conn.execute(
            """
            SELECT e.source_node_id, s.l1_module AS source_l1, s.l2_entity AS source_l2,
                   e.target_node_id, t.l1_module AS target_l1, t.l2_entity AS target_l2,
                   e.edge_type, e.weight
            FROM knowledge_edges e
            JOIN knowledge_nodes s ON s.node_id = e.source_node_id
            JOIN knowledge_nodes t ON t.node_id = e.target_node_id
            WHERE e.grade_subject = ?
            ORDER BY e.edge_type ASC, e.weight DESC;
            """,
            (grade_subject,),
        ).fetchall()
    ]

    return {
        "grade_subject": grade_subject,
        "taxonomy_status": gs["taxonomy_status"],
        "taxonomy_version": gs["taxonomy_version"],
        "maintenance_degraded": maint_degraded,
        "modules": modules_out,
        "knowledge_edges": edges,
    }


def search_questions(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str | None = None,
    l1: str | None = None,
    l2: str | None = None,
    l3: str | None = None,
    motif: str | None = None,
    method: str | None = None,
    trap: str | None = None,
    tier: str | None = None,
    elo_min: int | None = None,
    elo_max: int | None = None,
    query: str | None = None,
    semantic: str | None = None,
    exclude_solved: bool = False,
    learner_id: str | None = None,
    reveal: RevealMode = "quiz",
    hint_level: int = 1,
    include_visual_facts: bool = False,
    limit: int = 20,
    assets_dir: Path | str = Path("data/assets/images"),
    vectors_dir: Path | str = Path("data/vectors"),
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """多维条件 + FTS5 + 骨架语义综合检索（统一防剧透裁剪）。"""
    warnings: list[str] = []
    if (l2 or l3) and grade_subject:
        gs = conn.execute(
            "SELECT taxonomy_status FROM grade_subjects WHERE grade_subject = ?;",
            (grade_subject,),
        ).fetchone()
        if gs and gs["taxonomy_status"] != "LOCKED":
            warnings.append("MAINTENANCE_DEGRADED: 当前词表尚未锁定，--l2/--l3 检索仅反映当前节点挂载")

    where_sql = ["is_retired = 0", "eff_is_excluded = 0", "eff_is_servable = 1"]
    params: list[Any] = []
    if grade_subject:
        where_sql.append("grade_subject = ?")
        params.append(grade_subject)
    if l1:
        where_sql.append("eff_primary_l1_module = ?")
        params.append(l1.strip())
    if tier:
        where_sql.append("eff_tier = ?")
        params.append(tier.strip())
    if elo_min is not None:
        where_sql.append("eff_base_elo >= ?")
        params.append(int(elo_min))
    if elo_max is not None:
        where_sql.append("eff_base_elo <= ?")
        params.append(int(elo_max))

    rows = conn.execute(
        f"SELECT question_uid, grade_subject, eff_node_ids_json, eff_base_elo FROM questions WHERE {' AND '.join(where_sql)} ORDER BY eff_base_elo ASC, question_uid ASC;",
        params,
    ).fetchall()
    cand_uids = [str(r["question_uid"]) for r in rows]

    # 1. FTS 关键词过滤
    if query and query.strip():
        fts_hits = repository.search_fts(
            conn,
            query_text=query.strip(),
            grade_subject=grade_subject,
            servable_only=True,
            limit=max(200, limit * 10),
        )
        fts_set = {str(h["question_uid"]) for h in fts_hits}
        cand_uids = [u for u in cand_uids if u in fts_set]

    # 2. L2 / L3 节点过滤（并查集展开）
    if (l2 or l3) and cand_uids:
        n_clauses = ["1=1"]
        n_params: list[Any] = []
        if grade_subject:
            n_clauses.append("grade_subject = ?")
            n_params.append(grade_subject)
        if l2:
            n_clauses.append("l2_entity = ?")
            n_params.append(l2.strip())
        if l3:
            n_clauses.append("(node_id = ? OR l3_operator = ?)")
            n_params.extend([l3.strip(), l3.strip()])
        n_rows = conn.execute(
            f"SELECT node_id FROM knowledge_nodes WHERE {' AND '.join(n_clauses)};",
            n_params,
        ).fetchall()
        target_nids: set[str] = set()
        for nr in n_rows:
            target_nids.update(expand_canonical_node_ids(conn, str(nr["node_id"])))
        if not target_nids:
            cand_uids = []
        else:
            node_matched: set[str] = set()
            for r in rows:
                nids = set(_safe_json(r["eff_node_ids_json"], []))
                if nids & target_nids:
                    node_matched.add(str(r["question_uid"]))
            cand_uids = [u for u in cand_uids if u in node_matched]

    # 3. 概念过滤（MOTIF / METHOD / TRAP）
    for kind, needle in (("MOTIF", motif), ("METHOD", method), ("TRAP", trap)):
        if needle and needle.strip() and cand_uids:
            target_v = needle.strip()
            c_rows = conn.execute("SELECT concept_id, name, aliases_json FROM concepts WHERE kind = ?;", (kind,)).fetchall()
            cids: set[str] = set()
            for cr in c_rows:
                aliases = [str(a) for a in _safe_json(cr["aliases_json"], [])]
                if target_v in (str(cr["concept_id"]), str(cr["name"]), f"{kind}:{target_v}") or target_v in aliases:
                    cids.update(expand_canonical_concept_ids(conn, str(cr["concept_id"])))
            if not cids:
                cand_uids = []
                break
            ph = ",".join("?" for _ in cids)
            q_match = {
                str(x["question_uid"])
                for x in conn.execute(f"SELECT DISTINCT question_uid FROM question_concepts WHERE concept_id IN ({ph});", sorted(cids)).fetchall()
            }
            cand_uids = [u for u in cand_uids if u in q_match]

    # 4. 排除已 SOLVED_SOLO
    if exclude_solved and cand_uids:
        lid = resolve_learner_id(conn, learner_id)
        voided_ids = {
            int(r["voids_log_id"])
            for r in conn.execute(
                "SELECT voids_log_id FROM practice_log WHERE learner_id = ? AND outcome = 'VOIDED' AND voids_log_id IS NOT NULL;",
                (lid,),
            ).fetchall()
        }
        solved_set = {
            str(r["question_uid"])
            for r in conn.execute(
                "SELECT log_id, question_uid FROM practice_log WHERE learner_id = ? AND outcome = 'SOLVED_SOLO';",
                (lid,),
            ).fetchall()
            if int(r["log_id"]) not in voided_ids
        }
        cand_uids = [u for u in cand_uids if u not in solved_set]

    # 5. 语义向量重排（--semantic，按剩余候选实际学段分组查询后归并排序）
    sim_by_uid: dict[str, float] = {}
    if semantic and semantic.strip() and cand_uids:
        uid_to_gs = {str(r["question_uid"]): str(r["grade_subject"]) for r in rows}
        gs_to_uids: dict[str, set[str]] = {}
        if grade_subject:
            gs_to_uids[grade_subject] = set(cand_uids)
        else:
            for u in cand_uids:
                gs_name = uid_to_gs.get(u, "")
                if gs_name:
                    gs_to_uids.setdefault(gs_name, set()).add(u)

        store = VectorStore(vectors_dir)
        available_scopes: list[tuple[str, str, set[str]]] = []
        missing_scopes: list[str] = []
        for gs_name, scope_uids in gs_to_uids.items():
            gs_row = conn.execute(
                "SELECT slug FROM grade_subjects WHERE grade_subject = ?;",
                (gs_name,),
            ).fetchone()
            if not gs_row:
                continue
            slug = str(gs_row["slug"])
            if store.npz_path("skeleton", slug).exists():
                available_scopes.append((gs_name, slug, scope_uids))
            else:
                missing_scopes.append(gs_name)

        if available_scopes:
            eff_mock = settings.pipeline.use_mock_llm if use_mock is None else bool(use_mock)

            async def _embed_sem() -> Any:
                client = EmbeddingClient(settings, conn=None, use_mock=eff_mock)
                try:
                    return await client.embed_texts([semantic.strip()], stage="SEARCH_SEMANTIC")
                finally:
                    await client.aclose()

            vecs = _run_coro_sync(_embed_sem())
            all_hits: list[tuple[str, float]] = []
            top_k_each = max(limit * 3, 50)
            for _gs_name, slug, scope_uids in available_scopes:
                hits = store.query_top_k(
                    "skeleton",
                    vecs[0],
                    scope_slug=slug,
                    top_k=top_k_each,
                    allowed_uids=scope_uids,
                )
                all_hits.extend((u, float(s)) for u, s in hits)
            all_hits.sort(key=lambda pair: (-pair[1], pair[0]))
            sim_by_uid = {u: round(s, 4) for u, s in all_hits}
            cand_uids = [u for u, _ in all_hits]
            for ms in missing_scopes:
                warnings.append(f"学段 '{ms}' 骨架向量文件尚未建立，已跳过其语义排序")
        elif missing_scopes:
            warnings.append("骨架向量文件尚未建立，已跳过语义排序")

    out_items: list[dict[str, Any]] = []
    for uid in cand_uids[: max(1, int(limit))]:
        raw_d = hydrate_question_dict(conn, uid, assets_dir=assets_dir)
        trimmed = apply_reveal(
            raw_d,
            reveal=reveal,
            hint_level=hint_level,
            include_visual_facts=include_visual_facts,
        )
        if uid in sim_by_uid:
            trimmed["semantic_similarity"] = sim_by_uid[uid]
        out_items.append(trimmed)

    return {
        "total_matched": len(cand_uids),
        "returned": len(out_items),
        "reveal": reveal,
        "items": out_items,
        "warnings": warnings,
    }


def get_related_questions(
    conn: sqlite3.Connection,
    uid_or_id: str,
    *,
    types: list[str] | None = None,
    direction: str = "both",
    exclude_solved: bool = False,
    learner_id: str | None = None,
    reveal: RevealMode = "quiz",
    hint_level: int = 1,
    include_visual_facts: bool = False,
    limit: int = 20,
    assets_dir: Path | str = Path("data/assets/images"),
) -> dict[str, Any]:
    """沿五边拓扑查询关联题（v2.6 SCAFFOLD 方向：pre=低阶子题，post=升维母题）。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    dir_norm = (direction or "both").strip().lower()
    if dir_norm not in ("pre", "post", "both"):
        raise PreconditionError(f"direction 仅支持 pre/post/both，收到 {direction!r}")

    type_filter = {t.strip().upper() for t in types} if types else None
    rows = conn.execute(
        """
        SELECT * FROM question_relations
        WHERE source_question_uid = ? OR target_question_uid = ?;
        """,
        (uid, uid),
    ).fetchall()

    solved_set: set[str] = set()
    if exclude_solved:
        lid = resolve_learner_id(conn, learner_id)
        voided_ids = {
            int(r["voids_log_id"])
            for r in conn.execute(
                "SELECT voids_log_id FROM practice_log WHERE learner_id = ? AND outcome = 'VOIDED' AND voids_log_id IS NOT NULL;",
                (lid,),
            ).fetchall()
        }
        solved_set = {
            str(r["question_uid"])
            for r in conn.execute(
                "SELECT log_id, question_uid FROM practice_log WHERE learner_id = ? AND outcome = 'SOLVED_SOLO';",
                (lid,),
            ).fetchall()
            if int(r["log_id"]) not in voided_ids
        }

    items: list[dict[str, Any]] = []
    for r in rows:
        rtype = str(r["relation_type"])
        if type_filter and rtype not in type_filter:
            continue

        src = str(r["source_question_uid"])
        tgt = str(r["target_question_uid"])
        if rtype == "SCAFFOLD":
            # source = 子题 -> target = 母题
            if src == uid:
                edge_dir = "post"  # 对端是高阶母题
                peer_uid = tgt
            else:
                edge_dir = "pre"  # 对端是前置低阶子题
                peer_uid = src
            if dir_norm != "both" and edge_dir != dir_norm:
                continue
        else:
            edge_dir = "undirected"
            peer_uid = tgt if src == uid else src

        if peer_uid in solved_set:
            continue
        p_status = conn.execute(
            "SELECT eff_is_servable, is_retired, eff_is_excluded FROM questions WHERE question_uid = ?;",
            (peer_uid,),
        ).fetchone()
        if not p_status or p_status["eff_is_servable"] != 1 or p_status["is_retired"] == 1 or p_status["eff_is_excluded"] == 1:
            continue

        peer_q = hydrate_question_dict(
            conn,
            peer_uid,
            assets_dir=assets_dir,
            pedagogical_note=str(r["pedagogical_note"] or ""),
        )
        edge_item = {
            "relation_type": rtype,
            "direction": edge_dir,
            "is_directed": bool(r["is_directed"]),
            "is_cross_scope": bool(r["is_cross_scope"]),
            "surface_sim": round(float(r["surface_sim"]), 4),
            "skeleton_sim": round(float(r["skeleton_sim"]), 4),
            "containment_score": round(float(r["containment_score"]), 4),
            "trap_overlap": round(float(r["trap_overlap"]), 4),
            "confidence_source": str(r["confidence_source"]),
            "pedagogical_note": str(r["pedagogical_note"] or ""),
            "question": peer_q,
        }
        items.append(
            apply_related_reveal(
                edge_item,
                reveal=reveal,
                hint_level=hint_level,
                include_visual_facts=include_visual_facts,
            )
        )

    items.sort(key=lambda x: (-float(x["skeleton_sim"]), x["relation_type"], x["question"]["question_uid"]))
    return {
        "anchor_question_uid": uid,
        "direction": dir_norm,
        "reveal": reveal,
        "count": min(len(items), limit),
        "items": items[: max(1, int(limit))],
    }


def _has_wrapping_delimiters(text: str, left: str, right: str) -> bool:
    """校验首尾成对分隔符是否真正包裹整个字符串（中间深度不提前归零）。"""
    if len(text) < 2 or text[0] != left or text[-1] != right:
        return False
    depth = 0
    for idx, ch in enumerate(text):
        if ch == left:
            depth += 1
        elif ch == right:
            depth -= 1
            if depth == 0 and idx < len(text) - 1:
                return False
            if depth < 0:
                return False
    return depth == 0


def _is_numeric_ratio_lhs(text: str) -> bool:
    """判断 RATIO 冒号左侧是否为纯数值/分数分量（如 '3'、'1.5'、'1/2'），避免把位置比值 '3:4' 误拆为键值。"""
    s = text.strip()
    if not s:
        return False
    return re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:\s*/\s*\d+(?:\.\d+)?)?", s) is not None


def _try_match_label_prefix(seg: str) -> tuple[str, str] | None:
    """尝试提取显式小问标签前缀并归一；若前缀非法（如 '0.'）则安全返回 None。"""
    m = _PREFIX_LABEL_RE.match(seg)
    if not m:
        return None
    try:
        return normalize_part_label(m.group(1)), m.group(2).strip()
    except ValueError:
        return None


def _split_labeled_segments(
    raw: str,
    expected_parts: list[AnswerPart],
) -> list[tuple[str, str]]:
    """按分号切分学生作答，并对齐到小问标签（避免小数/括号算式被误拆为小问号）。"""
    exp_map = {p.label: p for p in expected_parts}
    segments = [s.strip() for s in raw.replace("；", ";").split(";") if s.strip()]
    parsed_pairs: list[tuple[str, str]] = []

    if len(expected_parts) == 1 and len(segments) == 1:
        matched = _try_match_label_prefix(segments[0])
        if matched is not None and matched[0] in exp_map:
            lbl, val = matched
        else:
            lbl = expected_parts[0].label
            val = segments[0]
        parsed_pairs.append((lbl, val))
        return parsed_pairs

    matched_segs = [_try_match_label_prefix(s) for s in segments]
    any_explicit = any(item is not None for item in matched_segs)
    if any_explicit:
        for seg, matched in zip(segments, matched_segs, strict=True):
            if matched is not None:
                lbl, val = matched
            else:
                idx = len(parsed_pairs)
                lbl = expected_parts[idx].label if idx < len(expected_parts) else f"({idx + 1})"
                val = seg
            parsed_pairs.append((lbl, val))
    else:
        for idx, seg in enumerate(segments):
            lbl = expected_parts[idx].label if idx < len(expected_parts) else f"({idx + 1})"
            parsed_pairs.append((lbl, seg))
    return parsed_pairs


def _parse_student_answer_parts(
    raw_input: str,
    expected_parts: list[AnswerPart],
) -> tuple[list[AnswerPart], list[str], list[str]]:
    """把学习者自由格式作答文本解析为与 expected_parts 对齐的 AnswerPart 列表。"""
    exp_map = {p.label: p for p in expected_parts}
    raw = (raw_input or "").strip()
    if not raw:
        return [], list(exp_map.keys()), []

    parsed_pairs = _split_labeled_segments(raw, expected_parts)

    stu_parts: list[AnswerPart] = []
    seen_labels: set[str] = set()
    for lbl, raw_val in parsed_pairs:
        if lbl in seen_labels:
            raise PreconditionError(f"提交答案存在重复小问标签: {lbl!r}")
        seen_labels.add(lbl)
        val = raw_val.strip()
        if not val:
            # 标签后作答为空时计入缺失小问（incorrect），不构造非法 AnswerPart 触发 ValidationError
            continue

        ref = exp_map.get(lbl)
        if ref is None:
            stu_parts.append(
                AnswerPart(
                    label=lbl,
                    answer_type="EXPRESSION",
                    verify_mode="SYMBOLIC",
                    collection_mode="SINGLE",
                    answer=val,
                    sympy_expr=val,
                )
            )
            continue

        if ref.answer_type == "CHOICE":
            norm_ch = normalize_choice_label(val) or "INVALID"
            stu_parts.append(
                AnswerPart(
                    label=lbl,
                    answer_type="CHOICE",
                    verify_mode="EXACT",
                    collection_mode="SINGLE",
                    answer=val,
                    choice_label=norm_ch,
                )
            )
        elif ref.answer_type == "SYMBOL_FILL":
            if ref.collection_mode == "SINGLE":
                norm_sym = normalize_symbol_fill(val)
                if not norm_sym.strip():
                    continue
                stu_parts.append(
                    AnswerPart(
                        label=lbl,
                        answer_type="SYMBOL_FILL",
                        verify_mode="EXACT",
                        collection_mode="SINGLE",
                        answer=val,
                        canonical=norm_sym,
                    )
                )
            elif ref.collection_mode == "LABELED_MAP":
                raw_tokens = [t.strip() for t in split_top_level(val.replace("，", ","), ",") if t.strip()]
                if not raw_tokens:
                    continue
                ref_labels = [
                    str(it.get("label") or it.get("key") or it.get("name") or it.get("entity") or "")
                    for it in (ref.items or [])
                    if isinstance(it, dict)
                ]
                sym_map_items: list[dict[str, str]] = []
                for idx, tok in enumerate(raw_tokens):
                    k_str: str | None = None
                    v_str: str = tok
                    if ":" in tok or "：" in tok:
                        k_part, v_part = re.split(r"[:：]", tok, maxsplit=1)
                        if k_part.strip():
                            k_str = k_part.strip()
                            v_str = v_part.strip()
                    else:
                        # 仅在 '=' / '＝' 左侧存在非符号键名时按等号切分，防止把 '=' / '>=' / '<=' / '!=' 误拆
                        m_eq = re.match(r"^\s*([^=＝<>!+\-*/×÷:：]+?)\s*[=＝]\s*(.+)$", tok)
                        if m_eq:
                            k_str = m_eq.group(1).strip()
                            v_str = m_eq.group(2).strip()
                    if k_str is None:
                        k_str = (
                            ref_labels[idx]
                            if idx < len(ref_labels) and ref_labels[idx]
                            else f"k{idx + 1}"
                        )
                    norm_v = normalize_symbol_fill(v_str)
                    sym_map_items.append(
                        {"label": k_str, "item_type": "SYMBOL_FILL", "canonical": norm_v}
                    )
                stu_parts.append(
                    AnswerPart(
                        label=lbl,
                        answer_type="SYMBOL_FILL",
                        verify_mode="EXACT",
                        collection_mode="LABELED_MAP",
                        answer=val,
                        items=sym_map_items,
                    )
                )
            else:
                cleaned = val.strip()
                if _has_wrapping_delimiters(cleaned, "{", "}") or _has_wrapping_delimiters(cleaned, "(", ")"):
                    cleaned = cleaned[1:-1]
                raw_tokens = [t.strip() for t in split_top_level(cleaned.replace("，", ","), ",") if t.strip()]
                if not raw_tokens:
                    continue
                stu_parts.append(
                    AnswerPart(
                        label=lbl,
                        answer_type="SYMBOL_FILL",
                        verify_mode="EXACT",
                        collection_mode=ref.collection_mode,
                        answer=val,
                        items=[
                            {"item_type": "SYMBOL_FILL", "canonical": normalize_symbol_fill(t)}
                            for t in raw_tokens
                        ],
                    )
                )
        elif ref.verify_mode == "SYMBOLIC":
            if ref.collection_mode == "SINGLE":
                stu_parts.append(
                    AnswerPart(
                        label=lbl,
                        answer_type=ref.answer_type,
                        verify_mode="SYMBOLIC",
                        collection_mode="SINGLE",
                        answer=val,
                        sympy_expr=val,
                    )
                )
            elif ref.collection_mode == "LABELED_MAP":
                raw_tokens = [t.strip() for t in split_top_level(val.replace("，", ","), ",") if t.strip()]
                if not raw_tokens:
                    continue
                map_items: list[dict[str, str]] = []
                ref_labels = [
                    str(it.get("label") or it.get("key") or it.get("name") or it.get("entity") or "")
                    for it in (ref.items or [])
                    if isinstance(it, dict)
                ]
                ref_label_set = {re.sub(r"\s+", "", rl) for rl in ref_labels if rl.strip()}
                for idx, tok in enumerate(raw_tokens):
                    if ref.answer_type == "RATIO":
                        if "=" in tok or "＝" in tok:
                            k, v = re.split(r"[=＝]", tok, maxsplit=1)
                            map_items.append({"label": k.strip(), "expr": v.strip()})
                        elif ":" in tok or "：" in tok:
                            cand_k, cand_v = re.split(r"[:：]", tok, maxsplit=1)
                            cand_k_clean = cand_k.strip()
                            norm_cand_k = re.sub(r"\s+", "", cand_k_clean)
                            if norm_cand_k in ref_label_set or (
                                cand_k_clean and not _is_numeric_ratio_lhs(cand_k_clean)
                            ):
                                map_items.append({"label": cand_k_clean, "expr": cand_v.strip()})
                            else:
                                rl = (
                                    ref_labels[idx]
                                    if idx < len(ref_labels) and ref_labels[idx]
                                    else f"k{idx + 1}"
                                )
                                map_items.append({"label": rl, "expr": tok})
                        else:
                            rl = (
                                ref_labels[idx]
                                if idx < len(ref_labels) and ref_labels[idx]
                                else f"k{idx + 1}"
                            )
                            map_items.append({"label": rl, "expr": tok})
                    elif "=" in tok or "＝" in tok or ":" in tok or "：" in tok:
                        k, v = re.split(r"[=＝:：]", tok, maxsplit=1)
                        map_items.append({"label": k.strip(), "expr": v.strip()})
                    else:
                        rl = (
                            ref_labels[idx]
                            if idx < len(ref_labels) and ref_labels[idx]
                            else f"k{idx + 1}"
                        )
                        map_items.append({"label": rl, "expr": tok})
                stu_parts.append(
                    AnswerPart(
                        label=lbl,
                        answer_type=ref.answer_type,
                        verify_mode="SYMBOLIC",
                        collection_mode="LABELED_MAP",
                        answer=val,
                        items=map_items,
                    )
                )
            else:
                cleaned = val.strip()
                if ref.answer_type == "SET_OR_INTERVAL":
                    # 开区间如 "(1, 3)" 不得剥除首尾圆括号，仅剥除集合花括号 "{...}"
                    if _has_wrapping_delimiters(cleaned, "{", "}"):
                        cleaned = cleaned[1:-1]
                elif _has_wrapping_delimiters(cleaned, "{", "}") or _has_wrapping_delimiters(cleaned, "(", ")"):
                    cleaned = cleaned[1:-1]
                raw_tokens = [t.strip() for t in split_top_level(cleaned.replace("，", ","), ",") if t.strip()]
                if not raw_tokens:
                    continue
                stu_parts.append(
                    AnswerPart(
                        label=lbl,
                        answer_type=ref.answer_type,
                        verify_mode="SYMBOLIC",
                        collection_mode=ref.collection_mode,
                        answer=val,
                        items=[{"expr": t} for t in raw_tokens],
                    )
                )
        else:
            stu_parts.append(
                AnswerPart(
                    label=lbl,
                    answer_type=ref.answer_type,
                    verify_mode="QUALITATIVE",
                    collection_mode="SINGLE",
                    answer=val,
                )
            )

    stu_norm = normalize_answer_parts(stu_parts)
    stu_labels = {p.label for p in stu_norm}
    missing = [lbl for lbl in exp_map if lbl not in stu_labels]
    extra = [p.label for p in stu_norm if p.label not in exp_map]
    return stu_norm, missing, extra


async def check_answer_async(
    conn: sqlite3.Connection,
    settings: Settings,
    uid_or_id: str,
    *,
    answer_input: str,
    learner_id: str | None = None,
    hint_level: int = 0,
    log: bool = False,
    request_id: str | None = None,
    quiz_batch_id: str | None = None,
    judge_override_json: dict[str, Any] | str | None = None,
    human_dir: Path | str = Path("human"),
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """不泄露答案的逐小问判题（可选 --log 同步幂等登记 practice_log）。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    q_row = conn.execute(
        """
        SELECT q.question_uid, r.source_id, q.grade_subject, q.eff_is_servable,
               q.eff_question_text, q.eff_options_json, q.eff_final_answers_json
        FROM questions q
        JOIN raw_questions r ON r.question_uid = q.question_uid
        WHERE q.question_uid = ?;
        """,
        (uid,),
    ).fetchone()
    if not q_row:
        raise NotFoundError(f"题目 '{uid}' 不存在")
    if int(q_row["eff_is_servable"] or 0) != 1:
        raise PreconditionError(f"PRECONDITION_FAILED: 题目 '{uid}' 当前不可出题（eff_is_servable=0），禁止判题")

    raw_exp = _safe_json(q_row["eff_final_answers_json"], [])
    if not raw_exp:
        raise PreconditionError(f"PRECONDITION_FAILED: 题目 '{uid}' 缺少有效标准答案数据")
    expected_parts = normalize_answer_parts([AnswerPart.model_validate(x) for x in raw_exp])

    stu_parts, missing_labels, extra_labels = _parse_student_answer_parts(answer_input, expected_parts)
    stu_map = {p.label: p for p in stu_parts}

    eff_mock = settings.pipeline.use_mock_llm if use_mock is None else bool(use_mock)
    gw_settings = settings.model_copy(deep=True)
    gw_settings.pipeline.use_mock_llm = eff_mock
    override_judge: QualitativeJudgeOutput | None = None
    if judge_override_json is not None:
        raw_j = json.loads(judge_override_json) if isinstance(judge_override_json, str) else judge_override_json
        override_judge = QualitativeJudgeOutput.model_validate(raw_j)

    gw = LLMGateway(gw_settings, conn=conn if log else None)
    qual_confidence_map: dict[str, str] = {}

    async def _qual_judge(lbl: str, exp_ans: str, stu_ans: str) -> QualitativeJudgeOutput:
        if override_judge is not None:
            qual_confidence_map[lbl] = override_judge.confidence
            return override_judge
        msgs = build_qualitative_judge_messages(
            lbl,
            exp_ans,
            stu_ans,
            question_text=str(q_row["eff_question_text"] or ""),
            grade_subject=str(q_row["grade_subject"]),
        )
        jr = await gw.call_structured(
            slot="judge_llm",
            stage="CHECK_QUALITATIVE",
            messages=msgs,
            schema=QualitativeJudgeOutput,
            question_uid=uid,
            prompt_version="judge_v1",
        )
        qual_confidence_map[lbl] = jr.parsed.confidence
        return jr.parsed

    part_results: list[dict[str, str]] = []
    try:
        # 对已作答小问逐题比对，缺失小问直接标 incorrect
        aligned_exp = [p for p in expected_parts if p.label in stu_map]
        aligned_stu = [stu_map[p.label] for p in aligned_exp]
        verdict_by_lbl: dict[str, str] = {}
        if aligned_exp:
            eq_res = await compare_answer_parts(
                aligned_exp,
                aligned_stu,
                grade_subject=str(q_row["grade_subject"]),
                allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
                options=_safe_json(q_row["eff_options_json"], {}),
                sympy_result=None,
                qualitative_judge=_qual_judge,
                timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
            )
            for pv in eq_res.part_verdicts:
                if pv.dual_equal:
                    verdict_by_lbl[pv.label] = "correct"
                elif pv.verify_mode == "QUALITATIVE" and qual_confidence_map.get(pv.label) == "LOW":
                    verdict_by_lbl[pv.label] = "uncertain"
                else:
                    verdict_by_lbl[pv.label] = "incorrect"

        for ep in expected_parts:
            part_results.append(
                {
                    "label": ep.label,
                    "verdict": verdict_by_lbl.get(ep.label, "incorrect"),
                }
            )
    finally:
        await gw.aclose()

    parts_total = len(expected_parts)
    parts_correct = sum(1 for pr in part_results if pr["verdict"] == "correct")
    all_correct = (len(missing_labels) == 0 and len(extra_labels) == 0 and parts_correct == parts_total and parts_total > 0)

    out_payload: dict[str, Any] = {
        "question_uid": uid,
        "source_id": str(q_row["source_id"] or ""),
        "all_correct": all_correct,
        "parts_correct": parts_correct,
        "parts_total": parts_total,
        "missing_labels": missing_labels,
        "extra_labels": extra_labels,
        "part_results": part_results,
        "logged": False,
    }

    if log:
        eff_hint = max(0, int(hint_level))
        if all_correct and eff_hint == 0:
            outcome = "SOLVED_SOLO"
            ratio = 1.0
        elif all_correct and eff_hint >= 1:
            outcome = "SOLVED_WITH_HINT"
            ratio = 1.0
        else:
            outcome = "WRONG"
            ratio = round(parts_correct / parts_total, 4) if parts_total > 0 else 0.0

        log_res = log_practice(
            conn,
            uid_or_id=uid,
            outcome=outcome,
            learner_id=learner_id,
            score_ratio=ratio,
            hint_level_used=eff_hint if eff_hint > 0 else None,
            submitted_answer=answer_input,
            quiz_batch_id=quiz_batch_id,
            request_id=request_id,
            human_dir=human_dir,
            include_practice_log=settings.human_mirror.include_practice_log,
        )
        out_payload["logged"] = True
        out_payload["log_id"] = log_res["log_id"]
        out_payload["outcome"] = log_res["outcome"]
        out_payload["score_ratio"] = log_res["score_ratio"]

    return apply_check_reveal(out_payload)


def _validate_practice_log_args(
    *,
    outcome: str,
    score_ratio: float | None,
    stuck_step_index: int | None,
    hint_level_used: int | None,
    request_id: str | None,
) -> tuple[str, float | None, int | None, int | None, str | None]:
    """统一校验单条打卡参数（对齐 practice_log 物理 CHECK/UNIQUE 约束）。"""
    norm_out = str(outcome or "").strip().upper()
    if norm_out not in ("SOLVED_SOLO", "SOLVED_WITH_HINT", "WRONG", "SKIPPED"):
        raise PreconditionError(
            f"outcome 仅允许 SOLVED_SOLO / SOLVED_WITH_HINT / WRONG / SKIPPED，收到 {outcome!r}"
        )

    norm_req_id = (str(request_id).strip() or None) if request_id is not None else None

    norm_hint: int | None = None
    if hint_level_used is not None:
        if isinstance(hint_level_used, bool):
            raise PreconditionError(f"hint_level_used 必须为非负整数，收到 {hint_level_used!r}")
        try:
            norm_hint = int(hint_level_used)
        except (TypeError, ValueError) as exc:
            raise PreconditionError(f"hint_level_used 必须为非负整数，收到 {hint_level_used!r}") from exc
        if norm_hint < 0:
            raise PreconditionError(f"hint_level_used 不能为负数，收到 {hint_level_used!r}")
        if norm_out == "SOLVED_SOLO" and norm_hint > 0:
            raise PreconditionError("SOLVED_SOLO 不可携带 hint_level_used > 0（请改用 SOLVED_WITH_HINT）")

    norm_ratio: float | None = None
    if score_ratio is not None:
        if isinstance(score_ratio, bool):
            raise PreconditionError(f"score_ratio 必须为 [0.0, 1.0] 实数，收到 {score_ratio!r}")
        try:
            norm_ratio = float(score_ratio)
        except (TypeError, ValueError) as exc:
            raise PreconditionError(f"score_ratio 必须为 [0.0, 1.0] 实数，收到 {score_ratio!r}") from exc
        if math.isnan(norm_ratio) or norm_ratio < 0.0 or norm_ratio > 1.0:
            raise PreconditionError(f"score_ratio 必须在 [0.0, 1.0] 范围内，收到 {score_ratio!r}")

    norm_stuck: int | None = None
    if stuck_step_index is not None:
        if isinstance(stuck_step_index, bool):
            raise PreconditionError(f"stuck_step_index 必须为整数，收到 {stuck_step_index!r}")
        try:
            norm_stuck = int(stuck_step_index)
        except (TypeError, ValueError) as exc:
            raise PreconditionError(f"stuck_step_index 必须为整数，收到 {stuck_step_index!r}") from exc

    return norm_out, norm_ratio, norm_stuck, norm_hint, norm_req_id


def _resolve_stuck_step(
    conn: sqlite3.Connection,
    question_uid: str,
    stuck_step_index: int | None,
) -> tuple[str | None, str | None]:
    """按最优解分支（is_optimal DESC, branch_id ASC）解析卡点步骤节点与子目标快照。"""
    if stuck_step_index is None:
        return None, None
    st_row = conn.execute(
        """
        SELECT st.m_knowledge_node_id, st.sub_goal
        FROM question_steps st
        JOIN question_solutions qs
          ON qs.question_uid = st.question_uid AND qs.branch_id = st.branch_id
        WHERE st.question_uid = ? AND st.step_index = ?
        ORDER BY qs.is_optimal DESC, qs.branch_id ASC
        LIMIT 1;
        """,
        (question_uid, int(stuck_step_index)),
    ).fetchone()
    if not st_row:
        return None, None
    return st_row["m_knowledge_node_id"], st_row["sub_goal"]


def log_practice(
    conn: sqlite3.Connection,
    *,
    uid_or_id: str,
    outcome: str,
    learner_id: str | None = None,
    score_ratio: float | None = None,
    stuck_step_index: int | None = None,
    hint_level_used: int | None = None,
    user_note: str = "",
    submitted_answer: str | None = None,
    quiz_batch_id: str | None = None,
    request_id: str | None = None,
    practiced_at: str | None = None,
    human_dir: Path | str = Path("human"),
    include_practice_log: bool = True,
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """登记单题做题足迹（支持 client_request_id 幂等与最优分支卡点 stuck_node_id 快照）。"""
    norm_out, norm_ratio, norm_stuck, norm_hint, norm_req_id = _validate_practice_log_args(
        outcome=outcome,
        score_ratio=score_ratio,
        stuck_step_index=stuck_step_index,
        hint_level_used=hint_level_used,
        request_id=request_id,
    )

    lid = resolve_learner_id(conn, learner_id)
    uid = repository.resolve_question_uid(conn, uid_or_id)

    if norm_req_id is not None:
        existing = conn.execute(
            "SELECT * FROM practice_log WHERE learner_id = ? AND client_request_id = ?;",
            (lid, norm_req_id),
        ).fetchone()
        if existing:
            d = dict(existing)
            d["idempotent_replay"] = True
            return d

    stuck_node_id, stuck_sub_goal = _resolve_stuck_step(conn, uid, norm_stuck)

    eff_ts = practiced_at or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    active_writer = writer or _DEFAULT_WRITER
    replayed_row: sqlite3.Row | None = None
    new_id = 0
    with active_writer.transaction(conn):
        if norm_req_id is not None:
            replayed_row = conn.execute(
                "SELECT * FROM practice_log WHERE learner_id = ? AND client_request_id = ?;",
                (lid, norm_req_id),
            ).fetchone()
        if replayed_row is None:
            cur = conn.execute(
                """
                INSERT INTO practice_log(
                    learner_id, question_uid, practiced_at, outcome, score_ratio,
                    stuck_step_index, stuck_node_id, stuck_sub_goal,
                    hint_level_used, user_note, submitted_answer, quiz_batch_id, client_request_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    lid,
                    uid,
                    eff_ts,
                    norm_out,
                    norm_ratio,
                    norm_stuck,
                    stuck_node_id,
                    stuck_sub_goal,
                    norm_hint,
                    user_note,
                    submitted_answer,
                    quiz_batch_id,
                    norm_req_id,
                ),
            )
            new_id = int(cur.lastrowid or 0)

    if replayed_row is not None:
        d = dict(replayed_row)
        d["idempotent_replay"] = True
        return d

    human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=include_practice_log)
    row = conn.execute("SELECT * FROM practice_log WHERE log_id = ?;", (new_id,)).fetchone()
    d = dict(row)
    d["idempotent_replay"] = False
    return d


def log_batch_practice(
    conn: sqlite3.Connection,
    *,
    quiz_batch_id: str,
    entries: list[dict[str, Any]],
    learner_id: str | None = None,
    request_id_prefix: str | None = None,
    human_dir: Path | str = Path("human"),
    include_practice_log: bool = True,
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """按 quiz_batch_id 原子批量登记整卷做题记录（全量预检 -> 单事务写入 -> 单次快照导出）。"""
    qb = conn.execute("SELECT * FROM quiz_batches WHERE quiz_batch_id = ?;", (quiz_batch_id,)).fetchone()
    if not qb:
        raise NotFoundError(f"组卷批次 '{quiz_batch_id}' 不存在")
    eff_lid = resolve_learner_id(conn, learner_id or str(qb["learner_id"]))

    norm_prefix = (request_id_prefix or "").strip() or None
    default_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    # 1. 全量前置预检（所有条目校验、批内 request_id 去重与外键解析必须在开启写事务前完成）
    seen_req_ids: set[str] = set()
    prepared: list[dict[str, Any]] = []
    for idx, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            raise PreconditionError(f"第 {idx} 条批量记录必须为 JSON 对象")
        uid_ref = str(item.get("uid") or item.get("question_uid") or item.get("source_id") or "").strip()
        if not uid_ref:
            raise PreconditionError(f"第 {idx} 条批量记录缺少 question_uid/uid")
        uid = repository.resolve_question_uid(conn, uid_ref)

        raw_item_req = item.get("request_id")
        clean_item_req = str(raw_item_req).strip() if raw_item_req is not None else ""
        req_id = clean_item_req or (f"{norm_prefix}:{idx}" if norm_prefix else None)

        norm_out, norm_ratio, norm_stuck, norm_hint, norm_req_id = _validate_practice_log_args(
            outcome=str(item.get("outcome") or "SOLVED_SOLO"),
            score_ratio=item.get("score_ratio"),
            stuck_step_index=item.get("stuck_step_index"),
            hint_level_used=item.get("hint_level_used"),
            request_id=req_id,
        )
        if norm_req_id is not None:
            if norm_req_id in seen_req_ids:
                raise PreconditionError(f"批量提交存在重复 request_id: {norm_req_id!r}")
            seen_req_ids.add(norm_req_id)
        stuck_node_id, stuck_sub_goal = _resolve_stuck_step(conn, uid, norm_stuck)
        eff_ts = str(item.get("practiced_at") or "").strip() or default_ts
        prepared.append(
            {
                "question_uid": uid,
                "practiced_at": eff_ts,
                "outcome": norm_out,
                "score_ratio": norm_ratio,
                "stuck_step_index": norm_stuck,
                "stuck_node_id": stuck_node_id,
                "stuck_sub_goal": stuck_sub_goal,
                "hint_level_used": norm_hint,
                "user_note": str(item.get("user_note") or ""),
                "submitted_answer": item.get("submitted_answer"),
                "request_id": norm_req_id,
            }
        )

    # 2. 单事务原子执行全部幂等检查与写入
    active_writer = writer or _DEFAULT_WRITER
    entry_refs: list[tuple[int, bool]] = []
    any_inserted = False
    with active_writer.transaction(conn):
        for p in prepared:
            req_key = p["request_id"]
            if req_key is not None:
                existing = conn.execute(
                    "SELECT log_id FROM practice_log WHERE learner_id = ? AND client_request_id = ?;",
                    (eff_lid, req_key),
                ).fetchone()
                if existing:
                    entry_refs.append((int(existing["log_id"]), True))
                    continue
            cur = conn.execute(
                """
                INSERT INTO practice_log(
                    learner_id, question_uid, practiced_at, outcome, score_ratio,
                    stuck_step_index, stuck_node_id, stuck_sub_goal,
                    hint_level_used, user_note, submitted_answer, quiz_batch_id, client_request_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    eff_lid,
                    p["question_uid"],
                    p["practiced_at"],
                    p["outcome"],
                    p["score_ratio"],
                    p["stuck_step_index"],
                    p["stuck_node_id"],
                    p["stuck_sub_goal"],
                    p["hint_level_used"],
                    p["user_note"],
                    p["submitted_answer"],
                    quiz_batch_id,
                    req_key,
                ),
            )
            entry_refs.append((int(cur.lastrowid or 0), False))
            any_inserted = True

    # 3. 事务提交成功后仅导出一次人工资产镜像
    if any_inserted:
        human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=include_practice_log)

    logged: list[dict[str, Any]] = []
    for log_id, is_replay in entry_refs:
        row = conn.execute("SELECT * FROM practice_log WHERE log_id = ?;", (log_id,)).fetchone()
        if row:
            rec = dict(row)
            rec["idempotent_replay"] = is_replay
            logged.append(rec)

    return {
        "quiz_batch_id": quiz_batch_id,
        "learner_id": eff_lid,
        "logged_count": len(logged),
        "records": logged,
    }


def void_practice_log(
    conn: sqlite3.Connection,
    log_id: int,
    *,
    reason: str = "",
    human_dir: Path | str = Path("human"),
    include_practice_log: bool = True,
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """插入一条 outcome='VOIDED' 冲销记录，保留原日志审计轨迹并立即剥离出在线水位。"""
    target = conn.execute("SELECT * FROM practice_log WHERE log_id = ?;", (int(log_id),)).fetchone()
    if not target:
        raise NotFoundError(f"作答日志 log_id={log_id} 不存在")
    if target["outcome"] == "VOIDED":
        raise PreconditionError(f"log_id={log_id} 本身已经是冲销记录")
    already_voided = conn.execute(
        "SELECT log_id FROM practice_log WHERE voids_log_id = ?;",
        (int(log_id),),
    ).fetchone()
    if already_voided:
        raise PreconditionError(f"log_id={log_id} 已被 log_id={already_voided['log_id']} 冲销")

    active_writer = writer or _DEFAULT_WRITER
    with active_writer.transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO practice_log(learner_id, question_uid, outcome, voids_log_id, user_note)
            VALUES (?, ?, 'VOIDED', ?, ?);
            """,
            (target["learner_id"], target["question_uid"], int(log_id), reason or "USER_VOID"),
        )
        void_id = int(cur.lastrowid or 0)

    human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=include_practice_log)
    return {
        "void_log_id": void_id,
        "voided_log_id": int(log_id),
        "question_uid": str(target["question_uid"]),
        "learner_id": str(target["learner_id"]),
        "reason": reason,
    }


def _mark_override_field_stale(conn: sqlite3.Connection, uid: str, field_name: str) -> None:
    """按覆写字段影响面统一向下游 compile_state 传播 STALE（覆写与撤回共用）。"""
    if field_name in (
        "question_text",
        "visual_facts",
        "final_answers_json",
        "format",
        "solutions_override_json",
        "verification_status",
    ):
        refresh.mark_stages_stale(conn, uid, ["S2", "S3C", "S3D", "S4"])
    elif field_name in effective.TAXONOMY_OVERRIDE_FIELDS:
        refresh.mark_stages_stale(conn, uid, ["S3D", "S4"])
    elif field_name == "is_excluded" or field_name in effective.ANNOTATION_OVERRIDE_FIELDS:
        refresh.mark_stages_stale(conn, uid, ["S4"])


def apply_single_override(
    conn: sqlite3.Connection,
    uid_or_id: str,
    *,
    field_name: str,
    value: str | None = None,
    revoke: bool = False,
    reconfirm: bool = False,
    reason: str = "",
    source: str = "PI_CLI",
    dry_run: bool = False,
    human_dir: Path | str = Path("human"),
    include_practice_log: bool = True,
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """单字段人工覆写写入/撤回/重确认（支持 --dry-run 预览与联动刷新）。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    if field_name not in _ALLOWED_OVERRIDE_FIELDS:
        raise PreconditionError(f"不支持的覆写字段 '{field_name}'，允许范围: {sorted(_ALLOWED_OVERRIDE_FIELDS)}")

    raw_row = conn.execute(
        "SELECT content_hash FROM raw_questions WHERE question_uid = ?;",
        (uid,),
    ).fetchone()
    if not raw_row:
        raise NotFoundError(f"题目 '{uid}' 不存在")
    content_hash = str(raw_row["content_hash"])

    if reconfirm:
        from shuati.services.facade import OverrideSource, reconfirm_stale_overrides

        if dry_run:
            stale_rows = conn.execute(
                "SELECT field_name FROM human_overrides WHERE question_uid = ? AND is_active = 1 AND is_suspended = 1;",
                (uid,),
            ).fetchall()
            return {"dry_run": True, "action": "reconfirm", "question_uid": uid, "fields": [r["field_name"] for r in stale_rows]}
        valid_sources: tuple[OverrideSource, ...] = (
            "REVIEW_UI",
            "PI_CLI",
            "PI_MCP",
            "MANUAL_YAML",
            "ANCHOR_CALIBRATION",
        )
        if source not in valid_sources:
            raise PreconditionError(f"不支持的覆写来源 '{source}'，允许范围: {list(valid_sources)}")
        typed_source = cast(OverrideSource, source)
        return reconfirm_stale_overrides(
            conn,
            uid,
            reason=reason,
            source=typed_source,
            human_dir=human_dir,
            include_practice_log=include_practice_log,
            writer=writer,
        )

    if revoke:
        existing = conn.execute(
            "SELECT override_id, bundle_id FROM human_overrides WHERE question_uid = ? AND field_name = ? AND is_active = 1;",
            (uid, field_name),
        ).fetchone()
        if not existing:
            raise NotFoundError(f"题目 '{uid}' 字段 '{field_name}' 当前无激活人工覆写")
        if dry_run:
            return {"dry_run": True, "action": "revoke", "question_uid": uid, "field_name": field_name, "override_id": existing["override_id"]}
        active_writer = writer or _DEFAULT_WRITER
        with active_writer.transaction(conn):
            conn.execute(
                "UPDATE human_overrides SET is_active = 0, revoked_at = datetime('now') WHERE override_id = ?;",
                (existing["override_id"],),
            )
            effective.recompute(conn, uid)
            repository.refresh_search_index(conn, uid)
            _mark_override_field_stale(conn, uid, field_name)
        human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=include_practice_log)
        return {"dry_run": False, "action": "revoke", "question_uid": uid, "field_name": field_name}

    if value is None:
        raise PreconditionError("写入覆写时必须提供 --value（或使用 --revoke / --reconfirm）")

    # 仅词表类覆写强绑 taxonomy_version，验真类与标注类写 ""；
    # 全部分组均保留写入时 content_hash 作为 base_fingerprint（标注类供 Stage Q 发出轻度抽检提醒，effective.py 不自动挂起）
    tax_ver = ""
    if field_name in effective.TAXONOMY_OVERRIDE_FIELDS:
        q_gs = conn.execute("SELECT grade_subject FROM questions WHERE question_uid = ?;", (uid,)).fetchone()
        if q_gs:
            gs_row = conn.execute(
                "SELECT taxonomy_version FROM grade_subjects WHERE grade_subject = ?;",
                (q_gs["grade_subject"],),
            ).fetchone()
            if gs_row:
                tax_ver = str(gs_row["taxonomy_version"] or "")

    if dry_run:
        return {
            "dry_run": True,
            "action": "set",
            "question_uid": uid,
            "field_name": field_name,
            "new_value": value,
            "base_fingerprint": content_hash,
            "taxonomy_version": tax_ver,
        }

    active_writer = writer or _DEFAULT_WRITER
    with active_writer.transaction(conn):
        conn.execute(
            "UPDATE human_overrides SET is_active = 0 WHERE question_uid = ? AND field_name = ? AND is_active = 1;",
            (uid, field_name),
        )
        conn.execute(
            """
            INSERT INTO human_overrides(
                question_uid, field_name, override_value, reason, source,
                base_fingerprint, taxonomy_version, is_active, is_suspended, suspended_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, '');
            """,
            (uid, field_name, value, reason or f"PI_OVERRIDE:{field_name}", source, content_hash, tax_ver),
        )
        effective.recompute(conn, uid)
        repository.refresh_search_index(conn, uid)
        _mark_override_field_stale(conn, uid, field_name)

    human_mirror.export_snapshot(conn, human_dir=human_dir, include_practice_log=include_practice_log)
    return {"dry_run": False, "action": "set", "question_uid": uid, "field_name": field_name}
