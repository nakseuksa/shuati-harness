"""Stage 3d/3e 知识网域内归并（可逆并查集 + 审计账本）、DAG 环检测与熵统计。"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any, Literal

import numpy as np

from shuati.core import effective
from shuati.db import human_mirror
from shuati.db.connection import CompileLock, PreconditionError
from shuati.vectors.store import VectorStore


def char_ngram_set(text: str) -> set[str]:
    """生成用于同义召回的字符 2-gram 与 3-gram 集合。"""
    cleaned = "".join((text or "").split()).lower()
    if not cleaned:
        return set()
    grams: set[str] = set()
    if len(cleaned) <= 2:
        grams.add(cleaned)
    for i in range(max(0, len(cleaned) - 1)):
        grams.add(cleaned[i : i + 2])
    for i in range(max(0, len(cleaned) - 2)):
        grams.add(cleaned[i : i + 3])
    return grams


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / float(len(a) + len(b) - inter)


def find_canonical_node_id(conn: sqlite3.Connection, node_id: str) -> str | None:
    """沿 redirect_to_id 追踪并查集根活跃 L3 node_id。"""
    curr: str | None = node_id
    visited: set[str] = set()
    while curr is not None:
        if curr in visited:
            return None
        visited.add(curr)
        row = conn.execute(
            "SELECT node_id, node_level, redirect_to_id FROM knowledge_nodes WHERE node_id = ?;",
            (curr,),
        ).fetchone()
        if row is None:
            return None
        if row["redirect_to_id"] is None:
            return str(row["node_id"]) if row["node_level"] == "L3" else None
        curr = str(row["redirect_to_id"])
    return None


def find_canonical_concept_id(conn: sqlite3.Connection, concept_id: str) -> str | None:
    """沿 redirect_to_id 追踪并查集根活跃 concept_id。"""
    curr: str | None = concept_id
    visited: set[str] = set()
    while curr is not None:
        if curr in visited:
            return None
        visited.add(curr)
        row = conn.execute(
            "SELECT concept_id, redirect_to_id FROM concepts WHERE concept_id = ?;",
            (curr,),
        ).fetchone()
        if row is None:
            return None
        if row["redirect_to_id"] is None:
            return str(row["concept_id"])
        curr = str(row["redirect_to_id"])
    return None


def expand_canonical_node_ids(conn: sqlite3.Connection, node_id: str) -> set[str]:
    """给定主节点（或子节点），一次展开主节点与所有直属重定向子节点 ID 集合（跳数 <= 1）。"""
    root = find_canonical_node_id(conn, node_id)
    if root is None:
        return set()
    rows = conn.execute(
        "SELECT node_id FROM knowledge_nodes WHERE node_id = ? OR redirect_to_id = ?;",
        (root, root),
    ).fetchall()
    return {str(r["node_id"]) for r in rows}


def expand_canonical_concept_ids(conn: sqlite3.Connection, concept_id: str) -> set[str]:
    """给定主概念（或子概念），一次展开主概念与所有直属重定向子概念 ID 集合（跳数 <= 1）。"""
    root = find_canonical_concept_id(conn, concept_id)
    if root is None:
        return set()
    rows = conn.execute(
        "SELECT concept_id FROM concepts WHERE concept_id = ? OR redirect_to_id = ?;",
        (root, root),
    ).fetchall()
    return {str(r["concept_id"]) for r in rows}


def _recall_pairs_within_group(
    items: list[dict[str, Any]],
    vec_map: dict[str, np.ndarray],
    *,
    top_k: int = 5,
    jaccard_threshold: float = 0.15,
) -> list[dict[str, Any]]:
    """在严格同域组内执行无预筛双路召回（Top-K 余弦 ∪ 字符 2/3-gram Jaccard > threshold）。"""
    n = len(items)
    if n < 2:
        return []

    id_list = [str(it["id"]) for it in items]
    grams = [char_ngram_set(str(it["name"])) for it in items]

    # 1. 字符 2/3-gram 倒排索引避免大规模全 O(N^2) 字符串比对
    inverted: dict[str, list[int]] = {}
    for idx, gs in enumerate(grams):
        for g in gs:
            inverted.setdefault(g, []).append(idx)

    pair_scores: dict[tuple[int, int], tuple[float, float]] = {}

    def _add_pair(i: int, j: int, cos_v: float | None = None, jac_v: float | None = None) -> None:
        lo, hi = (i, j) if i < j else (j, i)
        if lo == hi:
            return
        prev = pair_scores.get((lo, hi))
        c = cos_v if cos_v is not None else (prev[0] if prev else 0.0)
        jc = jac_v if jac_v is not None else (prev[1] if prev else jaccard_similarity(grams[lo], grams[hi]))
        pair_scores[(lo, hi)] = (c, jc)

    for idx, gs in enumerate(grams):
        seen_cands: set[int] = set()
        for g in gs:
            for other in inverted.get(g, ()):
                if other > idx:
                    seen_cands.add(other)
        for other in seen_cands:
            jac = jaccard_similarity(gs, grams[other])
            if jac > jaccard_threshold:
                _add_pair(idx, other, jac_v=jac)

    # 2. 矩阵分块向量 Top-K 召回（无余弦硬截断，全量取 Top-K）
    vec_indices = [idx for idx, iid in enumerate(id_list) if iid in vec_map]
    if len(vec_indices) >= 2:
        mat = np.stack([vec_map[id_list[idx]] for idx in vec_indices], axis=0).astype(np.float32)
        m = len(vec_indices)
        k_eff = min(top_k, m - 1)
        block = 256
        for start in range(0, m, block):
            end = min(m, start + block)
            sims = mat[start:end] @ mat.T  # shape (end-start, m)
            for row_offset in range(end - start):
                pos_a = start + row_offset
                row_sims = sims[row_offset].copy()
                row_sims[pos_a] = -2.0
                if m - 1 > k_eff:
                    top_cols = np.argpartition(row_sims, -k_eff)[-k_eff:]
                else:
                    top_cols = np.asarray([c for c in range(m) if c != pos_a], dtype=np.int64)
                for pos_b in top_cols.tolist():
                    if pos_b == pos_a:
                        continue
                    idx_a = vec_indices[pos_a]
                    idx_b = vec_indices[pos_b]
                    _add_pair(idx_a, idx_b, cos_v=float(row_sims[pos_b]))

    # 补全 Jaccard 路径命中但缺少余弦值的真实余弦
    out: list[dict[str, Any]] = []
    for (lo, hi), (cos_v, jac_v) in sorted(pair_scores.items()):
        id_a, id_b = id_list[lo], id_list[hi]
        if cos_v == 0.0 and id_a in vec_map and id_b in vec_map:
            cos_v = float(np.dot(vec_map[id_a], vec_map[id_b]))
        out.append(
            {
                "id_a": id_a,
                "name_a": str(items[lo]["name"]),
                "id_b": id_b,
                "name_b": str(items[hi]["name"]),
                "scope": dict(items[lo]["scope"]),
                "cosine_sim": round(cos_v, 6),
                "jaccard": round(jac_v, 6),
            }
        )
    return out


def recall_l3_merge_candidates(
    conn: sqlite3.Connection,
    vector_store: VectorStore,
    *,
    grade_subject: str,
    scope_slug: str,
    taxonomy_version: str,
    top_k: int = 5,
    jaccard_threshold: float = 0.15,
) -> list[dict[str, Any]]:
    """在严格同 (grade_subject, taxonomy_version, L3, l1_module, l2_entity) 内全量召回合并候选。"""
    rows = conn.execute(
        """
        SELECT node_id, l1_module, l2_entity, l3_operator
        FROM knowledge_nodes
        WHERE grade_subject = ?
          AND taxonomy_version = ?
          AND node_level = 'L3'
          AND redirect_to_id IS NULL
        ORDER BY l1_module, l2_entity, node_id;
        """,
        (grade_subject, taxonomy_version),
    ).fetchall()

    ids, vecs = vector_store.load_vectors("l3_nodes", scope_slug=scope_slug)
    vec_map = {ids[i]: vecs[i] for i in range(len(ids))} if len(ids) == len(vecs) else {}

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (str(r["l1_module"]), str(r["l2_entity"]))
        groups.setdefault(key, []).append(
            {
                "id": str(r["node_id"]),
                "name": str(r["l3_operator"]),
                "scope": {
                    "grade_subject": grade_subject,
                    "taxonomy_version": taxonomy_version,
                    "l1_module": key[0],
                    "l2_entity": key[1],
                },
            }
        )

    all_pairs: list[dict[str, Any]] = []
    for key in sorted(groups.keys()):
        all_pairs.extend(
            _recall_pairs_within_group(
                groups[key],
                vec_map,
                top_k=top_k,
                jaccard_threshold=jaccard_threshold,
            )
        )
    return all_pairs


def recall_concept_merge_candidates(
    conn: sqlite3.Connection,
    vector_store: VectorStore,
    *,
    kind: Literal["METHOD", "MOTIF"],
    top_k: int = 5,
    jaccard_threshold: float = 0.15,
) -> list[dict[str, Any]]:
    """在严格同 kind ∈ {METHOD, MOTIF} 且非种子概念域内全量召回合并候选（禁止 TRAP）。"""
    if kind not in ("METHOD", "MOTIF"):
        raise PreconditionError(f"PRECONDITION_FAILED: 禁止归并概念类型 {kind!r}（TRAP 永不自动归并）")

    rows = conn.execute(
        """
        SELECT concept_id, name
        FROM concepts
        WHERE kind = ?
          AND is_seed = 0
          AND redirect_to_id IS NULL
        ORDER BY concept_id;
        """,
        (kind,),
    ).fetchall()

    ns: Literal["concepts_method", "concepts_motif"] = (
        "concepts_method" if kind == "METHOD" else "concepts_motif"
    )
    ids, vecs = vector_store.load_vectors(ns)
    vec_map = {ids[i]: vecs[i] for i in range(len(ids))} if len(ids) == len(vecs) else {}

    items = [
        {
            "id": str(r["concept_id"]),
            "name": str(r["name"]),
            "scope": {"kind": kind},
        }
        for r in rows
    ]
    return _recall_pairs_within_group(
        items,
        vec_map,
        top_k=top_k,
        jaccard_threshold=jaccard_threshold,
    )


def apply_merge_pairs(
    conn: sqlite3.Connection,
    *,
    target_kind: Literal["KNOWLEDGE_NODE", "CONCEPT"],
    scope_dict: dict[str, Any],
    merges: list[dict[str, Any]],
    batch_id: str,
) -> dict[str, Any]:
    """在 SAVEPOINT 隔离下执行可逆并查集归并（路径压缩跳数 <= 1）并更新生效层。"""
    applied = 0
    skipped = 0
    affected_uids: set[str] = set()

    for idx, item in enumerate(merges):
        raw_from = str(item["from_id"])
        raw_to = str(item["to_id"])
        evidence = str(item.get("evidence") or "")

        if target_kind == "KNOWLEDGE_NODE":
            for raw_chk in (raw_from, raw_to):
                chk_row = conn.execute(
                    "SELECT node_level FROM knowledge_nodes WHERE node_id = ?;",
                    (raw_chk,),
                ).fetchone()
                if chk_row is not None and chk_row["node_level"] != "L3":
                    raise PreconditionError("PRECONDITION_FAILED: 严禁合并 L2 汇总节点")
            from_id = find_canonical_node_id(conn, raw_from)
            to_id = find_canonical_node_id(conn, raw_to)
        else:
            from_id = find_canonical_concept_id(conn, raw_from)
            to_id = find_canonical_concept_id(conn, raw_to)

        if from_id is None or to_id is None or from_id == to_id:
            skipped += 1
            continue

        sp_name = f"sp_merge_{idx}"
        conn.execute(f"SAVEPOINT {sp_name};")
        try:
            if target_kind == "KNOWLEDGE_NODE":
                row_from = conn.execute(
                    "SELECT node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator, aliases_json FROM knowledge_nodes WHERE node_id = ?;",
                    (from_id,),
                ).fetchone()
                row_to = conn.execute(
                    "SELECT node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator, aliases_json FROM knowledge_nodes WHERE node_id = ?;",
                    (to_id,),
                ).fetchone()
                if row_from is None or row_to is None:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
                    conn.execute(f"RELEASE SAVEPOINT {sp_name};")
                    skipped += 1
                    continue
                if row_from["node_level"] != "L3" or row_to["node_level"] != "L3":
                    raise PreconditionError("PRECONDITION_FAILED: 严禁合并 L2 汇总节点")
                same_scope = (
                    row_from["grade_subject"] == row_to["grade_subject"]
                    and row_from["taxonomy_version"] == row_to["taxonomy_version"]
                    and row_from["l1_module"] == row_to["l1_module"]
                    and row_from["l2_entity"] == row_to["l2_entity"]
                )
                if not same_scope:
                    raise PreconditionError(
                        f"PRECONDITION_FAILED: 严禁跨作用域合并知识节点: {from_id} vs {to_id}"
                    )

                prev_aliases = json.loads(row_to["aliases_json"] or "[]")
                from_aliases = json.loads(row_from["aliases_json"] or "[]")
                merged_aliases = list(dict.fromkeys([*prev_aliases, row_from["l3_operator"], *from_aliases]))
                merged_aliases = [a for a in merged_aliases if a and a != row_to["l3_operator"]][:12]

                child_rows = conn.execute(
                    "SELECT node_id FROM knowledge_nodes WHERE redirect_to_id = ?;",
                    (from_id,),
                ).fetchall()
                child_ids = [str(r["node_id"]) for r in child_rows]

                # 路径压缩：将 from 及所有直属子节点一次指向 to
                conn.execute(
                    "UPDATE knowledge_nodes SET redirect_to_id = ? WHERE node_id = ?;",
                    (to_id, from_id),
                )
                if child_ids:
                    ph = ",".join("?" for _ in child_ids)
                    conn.execute(
                        f"UPDATE knowledge_nodes SET redirect_to_id = ? WHERE node_id IN ({ph});",
                        [to_id, *child_ids],
                    )
                conn.execute(
                    "UPDATE knowledge_nodes SET aliases_json = ? WHERE node_id = ?;",
                    (json.dumps(merged_aliases, ensure_ascii=False), to_id),
                )

                # 记录受影响题目（step 绑定了 from 或其子节点）
                ref_ids = [from_id, *child_ids]
                ph = ",".join("?" for _ in ref_ids)
                step_qs = conn.execute(
                    f"SELECT DISTINCT question_uid FROM question_steps WHERE m_knowledge_node_id IN ({ph});",
                    ref_ids,
                ).fetchall()
                affected_uids.update(str(r["question_uid"]) for r in step_qs)

                node_scope = {
                    **scope_dict,
                    "grade_subject": row_to["grade_subject"],
                    "taxonomy_version": row_to["taxonomy_version"],
                    "l1_module": row_to["l1_module"],
                    "l2_entity": row_to["l2_entity"],
                }
                conn.execute(
                    """
                    INSERT INTO compaction_audit_ledger(
                        batch_id, target_kind, scope_json, from_id, to_id,
                        ai_judge_evidence, previous_aliases_to_json, affected_child_ids_json
                    )
                    VALUES (?, 'KNOWLEDGE_NODE', ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        batch_id,
                        json.dumps(node_scope, ensure_ascii=False),
                        from_id,
                        to_id,
                        evidence,
                        json.dumps(prev_aliases, ensure_ascii=False),
                        json.dumps(child_ids, ensure_ascii=False),
                    ),
                )
            else:
                row_from = conn.execute(
                    "SELECT concept_id, kind, name, aliases_json, is_seed FROM concepts WHERE concept_id = ?;",
                    (from_id,),
                ).fetchone()
                row_to = conn.execute(
                    "SELECT concept_id, kind, name, aliases_json, is_seed FROM concepts WHERE concept_id = ?;",
                    (to_id,),
                ).fetchone()
                if row_from is None or row_to is None:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
                    conn.execute(f"RELEASE SAVEPOINT {sp_name};")
                    skipped += 1
                    continue
                if row_from["is_seed"] == 1 or row_to["is_seed"] == 1:
                    raise PreconditionError("PRECONDITION_FAILED: 严禁合并 is_seed=1 概念")
                if row_from["kind"] != row_to["kind"] or row_from["kind"] not in ("METHOD", "MOTIF"):
                    raise PreconditionError(
                        f"PRECONDITION_FAILED: 严禁跨 kind 或对 TRAP 合并概念: {row_from['kind']} vs {row_to['kind']}"
                    )

                prev_aliases = json.loads(row_to["aliases_json"] or "[]")
                from_aliases = json.loads(row_from["aliases_json"] or "[]")
                merged_aliases = list(dict.fromkeys([*prev_aliases, row_from["name"], *from_aliases]))
                merged_aliases = [a for a in merged_aliases if a and a != row_to["name"]][:12]

                child_rows = conn.execute(
                    "SELECT concept_id FROM concepts WHERE redirect_to_id = ?;",
                    (from_id,),
                ).fetchall()
                child_ids = [str(r["concept_id"]) for r in child_rows]

                conn.execute(
                    "UPDATE concepts SET redirect_to_id = ? WHERE concept_id = ?;",
                    (to_id, from_id),
                )
                if child_ids:
                    ph = ",".join("?" for _ in child_ids)
                    conn.execute(
                        f"UPDATE concepts SET redirect_to_id = ? WHERE concept_id IN ({ph});",
                        [to_id, *child_ids],
                    )
                conn.execute(
                    "UPDATE concepts SET aliases_json = ? WHERE concept_id = ?;",
                    (json.dumps(merged_aliases, ensure_ascii=False), to_id),
                )

                # 去重迁移 question_concepts 避免主键冲突，优先保留 HUMAN 来源
                ref_ids = [from_id, *child_ids]
                ph = ",".join("?" for _ in ref_ids)
                qc_rows = conn.execute(
                    f"SELECT question_uid, concept_id, kind, origin FROM question_concepts WHERE concept_id IN ({ph});",
                    ref_ids,
                ).fetchall()
                for qcr in qc_rows:
                    quid = str(qcr["question_uid"])
                    affected_uids.add(quid)
                    conn.execute(
                        """
                        INSERT INTO question_concepts(question_uid, concept_id, kind, origin)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(question_uid, concept_id) DO UPDATE SET
                            origin = CASE WHEN question_concepts.origin = 'HUMAN' OR excluded.origin = 'HUMAN' THEN 'HUMAN' ELSE 'MACHINE' END;
                        """,
                        (quid, to_id, row_to["kind"], qcr["origin"]),
                    )
                    conn.execute(
                        "DELETE FROM question_concepts WHERE question_uid = ? AND concept_id = ?;",
                        (quid, qcr["concept_id"]),
                    )

                c_scope = {**scope_dict, "kind": row_to["kind"]}
                conn.execute(
                    """
                    INSERT INTO compaction_audit_ledger(
                        batch_id, target_kind, scope_json, from_id, to_id,
                        ai_judge_evidence, previous_aliases_to_json, affected_child_ids_json
                    )
                    VALUES (?, 'CONCEPT', ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        batch_id,
                        json.dumps(c_scope, ensure_ascii=False),
                        from_id,
                        to_id,
                        evidence,
                        json.dumps(prev_aliases, ensure_ascii=False),
                        json.dumps(child_ids, ensure_ascii=False),
                    ),
                )

            conn.execute(f"RELEASE SAVEPOINT {sp_name};")
            applied += 1
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
            conn.execute(f"RELEASE SAVEPOINT {sp_name};")
            raise

    conn.commit()

    # 若影响知识节点挂载，统一调用 effective.recompute 刷新生效层 eff_node_ids_json
    if target_kind == "KNOWLEDGE_NODE" and affected_uids:
        for uid in sorted(affected_uids):
            effective.recompute(conn, uid)
        conn.commit()

    return {
        "batch_id": batch_id,
        "target_kind": target_kind,
        "applied": applied,
        "skipped": skipped,
        "affected_questions": len(affected_uids),
    }


def revert_merge_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    backups_dir: Path | str = Path("data/backups"),
    lock_path: Path | str = Path("data/.compile.lock"),
) -> dict[str, Any]:
    """按 merge_id 严格逆序无损回滚指定归并批次；检测并拒绝乱序回滚。"""
    batch_rows = conn.execute(
        """
        SELECT * FROM compaction_audit_ledger
        WHERE batch_id = ? AND is_reverted = 0
        ORDER BY merge_id DESC;
        """,
        (batch_id,),
    ).fetchall()
    if not batch_rows:
        raise PreconditionError(f"PRECONDITION_FAILED: 未找到可回滚的活跃归并批次 batch_id={batch_id!r}")

    batch_merge_ids = {int(r["merge_id"]) for r in batch_rows}
    min_batch_id = min(batch_merge_ids)

    # 检验是否存在更晚且未回滚的依赖批次（任意 later row 的 from_id/to_id/children 与本批关联节点相交）
    involved_ids: set[str] = set()
    for r in batch_rows:
        involved_ids.add(str(r["from_id"]))
        involved_ids.add(str(r["to_id"]))
        involved_ids.update(json.loads(r["affected_child_ids_json"] or "[]"))

    later_rows = conn.execute(
        """
        SELECT merge_id, batch_id, from_id, to_id, affected_child_ids_json
        FROM compaction_audit_ledger
        WHERE is_reverted = 0 AND merge_id > ?;
        """,
        (min_batch_id,),
    ).fetchall()
    for lr in later_rows:
        if int(lr["merge_id"]) in batch_merge_ids:
            continue
        later_ids = {
            str(lr["from_id"]),
            str(lr["to_id"]),
            *json.loads(lr["affected_child_ids_json"] or "[]"),
        }
        if involved_ids & later_ids:
            raise PreconditionError(
                f"PRECONDITION_FAILED: 拒绝乱序回滚 {batch_id!r}：存在依赖后续活跃批次 "
                f"{lr['batch_id']!r} (merge_id={lr['merge_id']})，请先按逆序回滚后续批次。"
            )

    backup_path = human_mirror.vacuum_backup(conn, backups_dir, "revert_merge")
    affected_node_uids: set[str] = set()

    with CompileLock(lock_path):
        conn.execute("BEGIN IMMEDIATE;")
        try:
            for r in batch_rows:
                kind = str(r["target_kind"])
                from_id = str(r["from_id"])
                to_id = str(r["to_id"])
                prev_aliases = str(r["previous_aliases_to_json"] or "[]")
                child_ids: list[str] = list(json.loads(r["affected_child_ids_json"] or "[]"))

                if kind == "KNOWLEDGE_NODE":
                    conn.execute(
                        "UPDATE knowledge_nodes SET redirect_to_id = NULL WHERE node_id = ?;",
                        (from_id,),
                    )
                    if child_ids:
                        ph = ",".join("?" for _ in child_ids)
                        conn.execute(
                            f"UPDATE knowledge_nodes SET redirect_to_id = ? WHERE node_id IN ({ph});",
                            [from_id, *child_ids],
                        )
                    conn.execute(
                        "UPDATE knowledge_nodes SET aliases_json = ? WHERE node_id = ?;",
                        (prev_aliases, to_id),
                    )
                    ref_ids = [from_id, to_id, *child_ids]
                    ph = ",".join("?" for _ in ref_ids)
                    qs = conn.execute(
                        f"SELECT DISTINCT question_uid FROM question_steps WHERE m_knowledge_node_id IN ({ph});",
                        ref_ids,
                    ).fetchall()
                    affected_node_uids.update(str(q["question_uid"]) for q in qs)
                else:
                    conn.execute(
                        "UPDATE concepts SET redirect_to_id = NULL WHERE concept_id = ?;",
                        (from_id,),
                    )
                    if child_ids:
                        ph = ",".join("?" for _ in child_ids)
                        conn.execute(
                            f"UPDATE concepts SET redirect_to_id = ? WHERE concept_id IN ({ph});",
                            [from_id, *child_ids],
                        )
                    conn.execute(
                        "UPDATE concepts SET aliases_json = ? WHERE concept_id = ?;",
                        (prev_aliases, to_id),
                    )

                conn.execute(
                    "UPDATE compaction_audit_ledger SET is_reverted = 1, reverted_at = datetime('now') WHERE merge_id = ?;",
                    (r["merge_id"],),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        for uid in sorted(affected_node_uids):
            effective.recompute(conn, uid)
        if affected_node_uids:
            conn.commit()

    return {
        "batch_id": batch_id,
        "reverted_count": len(batch_rows),
        "affected_questions": len(affected_node_uids),
        "backup_path": str(backup_path),
    }


def check_dag_and_break_cycles(
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Kahn 拓扑排序检测有向环，迭代剥离当前环中权重最低边直至强无环。"""
    # 去重自环
    remaining: list[dict[str, Any]] = [
        dict(e) for e in edges if str(e["source_node_id"]) != str(e["target_node_id"])
    ]
    broken: list[dict[str, Any]] = []

    while True:
        nodes: set[str] = set()
        out_adj: dict[str, list[str]] = {}
        in_adj: dict[str, list[str]] = {}
        in_deg: dict[str, int] = {}
        out_deg: dict[str, int] = {}

        for e in remaining:
            u = str(e["source_node_id"])
            v = str(e["target_node_id"])
            nodes.add(u)
            nodes.add(v)
            out_adj.setdefault(u, []).append(v)
            in_adj.setdefault(v, []).append(u)
            in_deg[v] = in_deg.get(v, 0) + 1
            in_deg.setdefault(u, in_deg.get(u, 0))
            out_deg[u] = out_deg.get(u, 0) + 1
            out_deg.setdefault(v, out_deg.get(v, 0))

        # 正向 Kahn 剥离入度为 0 的节点
        q_in = deque([n for n in sorted(nodes) if in_deg.get(n, 0) == 0])
        alive = set(nodes)
        while q_in:
            curr = q_in.popleft()
            if curr not in alive:
                continue
            alive.remove(curr)
            for nxt in out_adj.get(curr, ()):
                in_deg[nxt] -= 1
                if in_deg[nxt] == 0 and nxt in alive:
                    q_in.append(nxt)

        if not alive:
            return remaining, broken

        # 反向 Kahn 剥离出度为 0 的节点，剩余节点均位于强连通/环核中
        q_out = deque([n for n in sorted(alive) if sum(1 for v in out_adj.get(n, ()) if v in alive) == 0])
        while q_out:
            curr = q_out.popleft()
            if curr not in alive:
                continue
            alive.remove(curr)
            for prev_n in in_adj.get(curr, ()):
                if prev_n in alive and all(v not in alive for v in out_adj.get(prev_n, ())):
                    q_out.append(prev_n)

        cycle_edges_idx = [
            i
            for i, e in enumerate(remaining)
            if str(e["source_node_id"]) in alive and str(e["target_node_id"]) in alive
        ]
        if not cycle_edges_idx:
            # 极端兜底：任意选剩余权重最小边
            cycle_edges_idx = list(range(len(remaining)))

        victim_idx = min(
            cycle_edges_idx,
            key=lambda i: (
                float(remaining[i].get("weight", 1.0)),
                str(remaining[i]["source_node_id"]),
                str(remaining[i]["target_node_id"]),
            ),
        )
        broken.append(remaining.pop(victim_idx))


def compute_l3_distribution_entropy(
    conn: sqlite3.Connection,
    grade_subject: str,
    taxonomy_version: str,
) -> dict[str, Any]:
    """仅统计活跃 node_level='L3' 节点在题目步骤上的分布熵（H 与归一化熵）。"""
    rows = conn.execute(
        """
        SELECT kn.node_id, COUNT(s.step_id) AS cnt
        FROM knowledge_nodes kn
        LEFT JOIN question_steps s ON s.m_knowledge_node_id = kn.node_id
        WHERE kn.grade_subject = ?
          AND kn.taxonomy_version = ?
          AND kn.node_level = 'L3'
          AND kn.redirect_to_id IS NULL
        GROUP BY kn.node_id;
        """,
        (grade_subject, taxonomy_version),
    ).fetchall()

    active_nodes = len(rows)
    counts = [int(r["cnt"]) for r in rows if int(r["cnt"]) > 0]
    total_uses = sum(counts)
    if total_uses <= 0 or len(counts) <= 1:
        return {
            "active_l3_nodes": active_nodes,
            "used_l3_nodes": len(counts),
            "total_step_bindings": total_uses,
            "entropy": 0.0,
            "normalized_entropy": 0.0,
        }

    entropy = -sum((c / total_uses) * math.log(c / total_uses) for c in counts)
    max_entropy = math.log(len(counts))
    return {
        "active_l3_nodes": active_nodes,
        "used_l3_nodes": len(counts),
        "total_step_bindings": total_uses,
        "entropy": round(entropy, 6),
        "normalized_entropy": round(entropy / max_entropy, 6) if max_entropy > 0 else 0.0,
    }
