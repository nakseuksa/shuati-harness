"""Stage 4 四通道五边拓扑判定引擎（IDF Tversky + 冻结分位 + 度数截断 + 人工覆写合并）。"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from shuati.config import TopologyConfig
from shuati.core.knowledge_graph import find_canonical_concept_id, find_canonical_node_id
from shuati.vectors.store import VectorStore

RELATION_TYPES = ("CLONE", "ISOMORPHIC", "SCAFFOLD", "CONTRASTIVE", "SHARED_TRAP")


def normalize_edge_endpoints(relation_type: str, u: str, v: str) -> tuple[str, str]:
    """除 SCAFFOLD 为有向外，其余四类无向边强制 source_question_uid < target_question_uid。"""
    if relation_type == "SCAFFOLD":
        return u, v
    return (u, v) if u < v else (v, u)


@dataclass
class QuestionFeature:
    uid: str
    grade_subject: str
    elo: int
    primary_l1: str
    has_replace_override: bool
    k_union: set[str]
    k_branches: list[set[str]]  # 多解分支无 REPLACE 覆写时用于最大覆盖计算
    e_traps: set[str]
    eff_text: str
    eff_skeleton: str
    tag_labels: list[str]


@dataclass
class CandidateEdge:
    source_uid: str
    target_uid: str
    relation_type: str
    is_directed: int
    is_cross_scope: int
    surface_sim: float
    skeleton_sim: float
    surface_pct: float
    skeleton_pct: float
    containment_score: float
    trap_overlap: float
    rank_score: float
    k_jaccard: float = 0.0
    confidence_source: str = "RULE_MATRIX"
    pedagogical_note: str = ""
    needs_llm_refine: bool = False
    refine_reason: str = ""
    rule_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def load_question_features(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    include_unservable: bool = False,
) -> dict[str, QuestionFeature]:
    """加载题目指纹与多解分支知识点集合（仅读生效层 eff_* 与并查集归一实体）。"""
    where_serve = "" if include_unservable else "AND eff_is_servable = 1"
    q_rows = conn.execute(
        f"""
        SELECT question_uid, grade_subject, eff_base_elo, eff_primary_l1_module,
               eff_node_ids_json, eff_question_text, eff_abstract_skeleton, eff_overridden_fields_json
        FROM questions
        WHERE grade_subject = ? AND is_retired = 0 AND eff_is_excluded = 0 {where_serve}
        ORDER BY question_uid;
        """,
        (grade_subject,),
    ).fetchall()
    if not q_rows:
        return {}

    node_cache: dict[str, str | None] = {}
    concept_cache: dict[str, str | None] = {}

    def _canon_node(nid: str) -> str | None:
        if nid not in node_cache:
            node_cache[nid] = find_canonical_node_id(conn, nid)
        return node_cache[nid]

    def _canon_concept(cid: str) -> str | None:
        if cid not in concept_cache:
            concept_cache[cid] = find_canonical_concept_id(conn, cid)
        return concept_cache[cid]

    uids = [str(r["question_uid"]) for r in q_rows]
    ph = ",".join("?" for _ in uids)

    # 加载激活的 MOTIF 与 TRAP 概念
    motif_by_q: dict[str, set[str]] = {}
    trap_by_q: dict[str, set[str]] = {}
    motif_names_by_q: dict[str, list[str]] = {}
    qc_rows = conn.execute(
        f"""
        SELECT qc.question_uid, qc.concept_id, qc.kind, c.name
        FROM question_concepts qc
        JOIN concepts c ON c.concept_id = qc.concept_id
        WHERE qc.question_uid IN ({ph}) AND qc.kind IN ('MOTIF', 'TRAP');
        """,
        uids,
    ).fetchall()
    for r in qc_rows:
        uid = str(r["question_uid"])
        cid = _canon_concept(str(r["concept_id"])) or str(r["concept_id"])
        if r["kind"] == "MOTIF":
            motif_by_q.setdefault(uid, set()).add(f"M:{cid}")
            motif_names_by_q.setdefault(uid, []).append(str(r["name"]))
        else:
            trap_by_q.setdefault(uid, set()).add(f"T:{cid}")

    # 加载步骤 L3 节点按分支分组
    branch_nodes_by_q: dict[str, dict[str, set[str]]] = {}
    st_rows = conn.execute(
        f"""
        SELECT question_uid, branch_id, m_knowledge_node_id
        FROM question_steps
        WHERE question_uid IN ({ph}) AND m_knowledge_node_id IS NOT NULL;
        """,
        uids,
    ).fetchall()
    for r in st_rows:
        cn = _canon_node(str(r["m_knowledge_node_id"]))
        if cn:
            branch_nodes_by_q.setdefault(str(r["question_uid"]), {}).setdefault(str(r["branch_id"]), set()).add(f"N:{cn}")

    # 判断是否存在 REPLACE 模式 taxonomy_override_json
    replace_uids: set[str] = set()
    ov_rows = conn.execute(
        f"""
        SELECT question_uid, override_value
        FROM human_overrides
        WHERE question_uid IN ({ph}) AND field_name = 'taxonomy_override_json' AND is_active = 1 AND is_suspended = 0;
        """,
        uids,
    ).fetchall()
    for r in ov_rows:
        try:
            obj = json.loads(r["override_value"] or "{}")
            if obj.get("mode", "REPLACE") == "REPLACE":
                replace_uids.add(str(r["question_uid"]))
        except ValueError:
            pass

    node_labels_cache: dict[str, str] = {}
    features: dict[str, QuestionFeature] = {}
    for r in q_rows:
        uid = str(r["question_uid"])
        eff_nids = [f"N:{x}" for x in json.loads(r["eff_node_ids_json"] or "[]")]
        motifs = motif_by_q.get(uid, set())
        k_union = set(eff_nids) | motifs
        is_rep = uid in replace_uids
        if is_rep or uid not in branch_nodes_by_q or len(branch_nodes_by_q[uid]) <= 1:
            k_branches = [k_union]
        else:
            k_branches = [b_nodes | motifs for b_nodes in branch_nodes_by_q[uid].values()]

        raw_nids = json.loads(r["eff_node_ids_json"] or "[]")
        labels: list[str] = []
        for nid in raw_nids:
            if nid not in node_labels_cache:
                nr = conn.execute(
                    "SELECT l1_module, l2_entity, l3_operator FROM knowledge_nodes WHERE node_id = ?;",
                    (nid,),
                ).fetchone()
                node_labels_cache[nid] = (
                    f"{nr['l1_module']}/{nr['l2_entity']}/{nr['l3_operator']}" if nr else nid
                )
            labels.append(node_labels_cache[nid])
        labels.extend(motif_names_by_q.get(uid, []))

        features[uid] = QuestionFeature(
            uid=uid,
            grade_subject=str(r["grade_subject"]),
            elo=int(r["eff_base_elo"] if r["eff_base_elo"] is not None else 1500),
            primary_l1=str(r["eff_primary_l1_module"] or ""),
            has_replace_override=is_rep,
            k_union=k_union,
            k_branches=k_branches,
            e_traps=trap_by_q.get(uid, set()),
            eff_text=str(r["eff_question_text"] or ""),
            eff_skeleton=str(r["eff_abstract_skeleton"] or ""),
            tag_labels=labels,
        )

    return features


def compute_idf_weights(features: dict[str, QuestionFeature]) -> tuple[dict[str, float], dict[str, float]]:
    """计算 K_set（L3 + MOTIF）与 E_trap（TRAP）的 IDF 权重字典。"""
    n_docs = max(1, len(features))
    df_k: dict[str, int] = {}
    df_t: dict[str, int] = {}
    for f in features.values():
        for tok in f.k_union:
            df_k[tok] = df_k.get(tok, 0) + 1
        for tr in f.e_traps:
            df_t[tr] = df_t.get(tr, 0) + 1
    idf_k = {k: math.log((n_docs + 1.0) / (df + 1.0)) + 1e-6 for k, df in df_k.items()}
    idf_t = {k: math.log((n_docs + 1.0) / (df + 1.0)) + 1e-6 for k, df in df_t.items()}
    return idf_k, idf_t


def _weighted_containment(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    """计算 I_w(A subset B) = sum_{k in A & B} idf(k) / sum_{k in A} idf(k)。"""
    if not a or not b:
        return 0.0
    denom = sum(idf.get(x, 1.0) for x in a)
    if denom <= 1e-12:
        return 0.0
    inter = sum(idf.get(x, 1.0) for x in (a & b))
    return inter / denom


def _set_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a) + len(b) - inter)


def _best_branch_metrics(
    fa: QuestionFeature,
    fb: QuestionFeature,
    idf_k: dict[str, float],
) -> tuple[float, float, float]:
    """考虑多解分支最大覆盖，返回 (k_jaccard, cont_a_in_b, cont_b_in_a)。"""
    if len(fa.k_branches) == 1 and len(fb.k_branches) == 1:
        sa, sb = fa.k_branches[0], fb.k_branches[0]
        return (
            _set_jaccard(sa, sb),
            _weighted_containment(sa, sb, idf_k),
            _weighted_containment(sb, sa, idf_k),
        )
    best_j = 0.0
    best_a_in_b = 0.0
    best_b_in_a = 0.0
    for sa in fa.k_branches:
        for sb in fb.k_branches:
            j = _set_jaccard(sa, sb)
            cab = _weighted_containment(sa, sb, idf_k)
            cba = _weighted_containment(sb, sa, idf_k)
            if j > best_j:
                best_j = j
            if cab > best_a_in_b:
                best_a_in_b = cab
            if cba > best_b_in_a:
                best_b_in_a = cba
    return best_j, best_a_in_b, best_b_in_a


def _trap_idf_overlap(ea: set[str], eb: set[str], idf_t: dict[str, float]) -> float:
    if not ea or not eb:
        return 0.0
    shared = ea & eb
    if not shared:
        return 0.0
    sum_shared = sum(idf_t.get(t, 1.0) for t in shared)
    sum_a = sum(idf_t.get(t, 1.0) for t in ea)
    sum_b = sum(idf_t.get(t, 1.0) for t in eb)
    denom = min(sum_a, sum_b)
    if denom <= 1e-12:
        return 0.0
    return min(1.0, sum_shared / denom)


def _near_threshold(val: float, threshold: float, band: float) -> bool:
    if threshold <= 0.0 or band <= 0.0:
        return False
    return abs(val - threshold) <= threshold * band


def _compute_knn_ranks(vecs: np.ndarray, top_k: int) -> dict[int, dict[int, int]]:
    """计算每位向量的 Top-K 近邻 rank（1-based），返回 {row_i: {col_j: rank}}。"""
    n = vecs.shape[0]
    if n < 2 or top_k <= 0:
        return {}
    k_eff = min(top_k, n - 1)
    ranks: dict[int, dict[int, int]] = {}
    block = 256
    for start in range(0, n, block):
        end = min(n, start + block)
        sims = vecs[start:end] @ vecs.T
        for offset in range(end - start):
            i = start + offset
            row = sims[offset].copy()
            row[i] = -2.0
            if n - 1 > k_eff:
                cols = np.argpartition(row, -k_eff)[-k_eff:]
            else:
                cols = np.asarray([c for c in range(n) if c != i], dtype=np.int64)
            ordered = sorted(cols.tolist(), key=lambda c: float(row[c]), reverse=True)
            ranks[i] = {int(c): r_idx + 1 for r_idx, c in enumerate(ordered)}
    return ranks


def _pair_similarities(
    uid_a: str,
    uid_b: str,
    surf_pos: dict[str, int],
    surf_vecs: np.ndarray,
    skel_pos: dict[str, int],
    skel_vecs: np.ndarray,
    vector_store: VectorStore,
    scope_slug: str,
) -> tuple[float, float, float, float]:
    s_sim = 0.0
    k_sim = 0.0
    if uid_a in surf_pos and uid_b in surf_pos:
        s_sim = float(np.dot(surf_vecs[surf_pos[uid_a]], surf_vecs[surf_pos[uid_b]]))
    if uid_a in skel_pos and uid_b in skel_pos:
        k_sim = float(np.dot(skel_vecs[skel_pos[uid_a]], skel_vecs[skel_pos[uid_b]]))
    s_pct = vector_store.lookup_percentile(scope_slug, "surface", s_sim)
    k_pct = vector_store.lookup_percentile(scope_slug, "skeleton", k_sim)
    return s_sim, k_sim, s_pct, k_pct


def generate_candidate_edges(
    conn: sqlite3.Connection,
    vector_store: VectorStore,
    *,
    grade_subject: str,
    scope_slug: str,
    topology_cfg: TopologyConfig,
    features: dict[str, QuestionFeature] | None = None,
    target_uids: set[str] | None = None,
    include_unservable: bool = False,
) -> list[CandidateEdge]:
    """执行四通道五边拓扑判定（若提供 target_uids 则仅召回至少一端在 target_uids 中的边）。"""
    if features is None:
        features = load_question_features(
            conn,
            grade_subject=grade_subject,
            include_unservable=include_unservable,
        )
    if len(features) < 2:
        return []

    surf_ids, surf_vecs = vector_store.load_vectors("surface", scope_slug=scope_slug)
    skel_ids, skel_vecs = vector_store.load_vectors("skeleton", scope_slug=scope_slug)
    surf_pos = {uid: idx for idx, uid in enumerate(surf_ids) if uid in features}
    skel_pos = {uid: idx for idx, uid in enumerate(skel_ids) if uid in features}

    th = topology_cfg.thresholds
    max_knn_surf = max(th.clone.knn_k, th.contrastive.knn_k)
    max_knn_skel = max(th.clone.knn_k, th.isomorphic.knn_k)

    # 基于对齐子矩阵计算互近邻 rank
    surf_rank_uid: dict[str, dict[str, int]] = {}
    if len(surf_ids) >= 2:
        raw_surf_ranks = _compute_knn_ranks(surf_vecs, max_knn_surf)
        for i, cols in raw_surf_ranks.items():
            ua = surf_ids[i]
            if ua not in features:
                continue
            surf_rank_uid[ua] = {surf_ids[j]: r for j, r in cols.items() if surf_ids[j] in features}

    skel_rank_uid: dict[str, dict[str, int]] = {}
    if len(skel_ids) >= 2:
        raw_skel_ranks = _compute_knn_ranks(skel_vecs, max_knn_skel)
        for i, cols in raw_skel_ranks.items():
            ua = skel_ids[i]
            if ua not in features:
                continue
            skel_rank_uid[ua] = {skel_ids[j]: r for j, r in cols.items() if skel_ids[j] in features}

    idf_k, idf_t = compute_idf_weights(features)

    # 倒排索引：共享 K_set（用于 SCAFFOLD 召回）与共享 E_trap（用于 SHARED_TRAP 召回）
    k_inverted: dict[str, list[str]] = {}
    t_inverted: dict[str, list[str]] = {}
    for uid, feat in features.items():
        for tok in feat.k_union:
            k_inverted.setdefault(tok, []).append(uid)
        for tr in feat.e_traps:
            t_inverted.setdefault(tr, []).append(uid)

    # 汇总所有待评估无序对 (u < v)
    candidate_pairs: set[tuple[str, str]] = set()

    def _add_pair(a: str, b: str) -> None:
        if a == b or a not in features or b not in features:
            return
        if target_uids is not None and a not in target_uids and b not in target_uids:
            return
        lo, hi = (a, b) if a < b else (b, a)
        candidate_pairs.add((lo, hi))

    # 1. Surface Top-K 与 Skeleton Top-K 邻域召回（含互近邻及单向近邻，具体互近邻门槛由规则判断）
    for ua, nbrs in surf_rank_uid.items():
        for ub in nbrs:
            _add_pair(ua, ub)
    for ua, nbrs in skel_rank_uid.items():
        for ub in nbrs:
            _add_pair(ua, ub)

    # 2. 倒排召回：共享至少 min_k_size 个知识点对 / 共享 E_trap 对
    uid_list = sorted(features.keys())
    for uid in uid_list:
        if target_uids is not None and uid not in target_uids:
            # 目标外节点仅需通过倒排映射到目标内节点，若目标集较大仍可自然覆盖
            pass
        k_counts: dict[str, int] = {}
        for tok in features[uid].k_union:
            for other in k_inverted.get(tok, ()):
                if other != uid:
                    k_counts[other] = k_counts.get(other, 0) + 1
        for other, cnt in k_counts.items():
            if cnt >= th.scaffold.min_k_size:
                _add_pair(uid, other)

        t_counts: dict[str, int] = {}
        for tr in features[uid].e_traps:
            for other in t_inverted.get(tr, ()):
                if other != uid:
                    t_counts[other] = t_counts.get(other, 0) + 1
        for other, cnt in t_counts.items():
            if cnt >= th.shared_trap.min_shared:
                _add_pair(uid, other)

    out_edges: list[CandidateEdge] = []
    rule_ver = topology_cfg.rule_version

    for ua, ub in sorted(candidate_pairs):
        fa = features[ua]
        fb = features[ub]
        cross_scope = int(fa.grade_subject != fb.grade_subject)
        s_sim, k_sim, s_pct, k_pct = _pair_similarities(
            ua, ub, surf_pos, surf_vecs, skel_pos, skel_vecs, vector_store, scope_slug
        )
        k_jacc, cont_a_in_b, cont_b_in_a = _best_branch_metrics(fa, fb, idf_k)
        trap_ov = _trap_idf_overlap(fa.e_traps, fb.e_traps, idf_t)
        elo_diff = abs(fa.elo - fb.elo)

        sr_ab = surf_rank_uid.get(ua, {}).get(ub)
        sr_ba = surf_rank_uid.get(ub, {}).get(ua)
        kr_ab = skel_rank_uid.get(ua, {}).get(ub)
        kr_ba = skel_rank_uid.get(ub, {}).get(ua)

        # (1) CLONE：表皮与骨架均极似（互 Top-k + 分位 + 硬下界 + Elo 接近）
        is_mutual_clone = (
            sr_ab is not None and sr_ba is not None and sr_ab <= th.clone.knn_k and sr_ba <= th.clone.knn_k
            and kr_ab is not None and kr_ba is not None and kr_ab <= th.clone.knn_k and kr_ba <= th.clone.knn_k
        )
        if (
            is_mutual_clone
            and s_pct >= th.clone.surface_pct
            and s_sim >= th.clone.surface_sim_min
            and k_pct >= th.clone.skeleton_pct
            and k_sim >= th.clone.skeleton_sim_min
            and elo_diff <= th.clone.max_elo_diff
        ):
            out_edges.append(
                CandidateEdge(
                    source_uid=ua,
                    target_uid=ub,
                    relation_type="CLONE",
                    is_directed=0,
                    is_cross_scope=cross_scope,
                    surface_sim=s_sim,
                    skeleton_sim=k_sim,
                    surface_pct=s_pct,
                    skeleton_pct=k_pct,
                    containment_score=max(cont_a_in_b, cont_b_in_a),
                    trap_overlap=trap_ov,
                    rank_score=(s_sim + k_sim) * 0.5,
                    k_jaccard=k_jacc,
                    rule_version=rule_ver,
                )
            )

        # (2) ISOMORPHIC：互 Top-K 骨架极似且表皮不雷同，或骨架硬阈值+K_Jaccard 达标
        is_mutual_iso = (
            kr_ab is not None and kr_ba is not None and kr_ab <= th.isomorphic.knn_k and kr_ba <= th.isomorphic.knn_k
        )
        iso_boundary = th.isomorphic.boundary_band
        iso_struct_ok = is_mutual_iso and s_pct < th.isomorphic.surface_pct_max and elo_diff <= th.isomorphic.max_elo_diff
        iso_core_hit = (
            iso_struct_ok
            and k_pct >= th.isomorphic.skeleton_pct
            and k_sim >= th.isomorphic.skeleton_sim_min
            and k_jacc >= th.isomorphic.k_jaccard_min
        )
        iso_band_hit = (
            iso_struct_ok
            and not iso_core_hit
            and iso_boundary > 0
            and k_pct >= th.isomorphic.skeleton_pct * (1.0 - iso_boundary)
            and k_sim >= th.isomorphic.skeleton_sim_min * (1.0 - iso_boundary)
            and k_jacc >= th.isomorphic.k_jaccard_min * (1.0 - iso_boundary)
        )
        if iso_core_hit or iso_band_hit:
            near_b = iso_band_hit or (
                _near_threshold(k_pct, th.isomorphic.skeleton_pct, iso_boundary)
                or _near_threshold(k_sim, th.isomorphic.skeleton_sim_min, iso_boundary)
                or _near_threshold(k_jacc, th.isomorphic.k_jaccard_min, iso_boundary)
            )
            out_edges.append(
                CandidateEdge(
                    source_uid=ua,
                    target_uid=ub,
                    relation_type="ISOMORPHIC",
                    is_directed=0,
                    is_cross_scope=cross_scope,
                    surface_sim=s_sim,
                    skeleton_sim=k_sim,
                    surface_pct=s_pct,
                    skeleton_pct=k_pct,
                    containment_score=k_jacc,
                    trap_overlap=trap_ov,
                    rank_score=0.6 * k_sim + 0.4 * k_jacc,
                    k_jaccard=k_jacc,
                    needs_llm_refine=near_b,
                    refine_reason="BOUNDARY_BAND" if near_b else "",
                    rule_version=rule_ver,
                )
            )

        # (3) SCAFFOLD：有向边 A -> B 表示 B 是 A 的前置/低阶支架（A 难度显著更高，B 知识被 A 包含）
        for high_f, low_f, low_in_high, high_in_low in (
            (fa, fb, cont_b_in_a, cont_a_in_b),
            (fb, fa, cont_a_in_b, cont_b_in_a),
        ):
            elo_gap = high_f.elo - low_f.elo
            if len(low_f.k_union) < th.scaffold.min_k_size:
                continue
            if high_in_low >= th.scaffold.reverse_containment_max:
                continue
            sc_band = th.scaffold.boundary_band
            sc_core = (
                low_in_high >= th.scaffold.containment_min
                and (elo_gap >= th.scaffold.min_elo_diff or k_pct >= th.scaffold.skeleton_pct_min)
            )
            sc_boundary_hit = (
                not sc_core
                and sc_band > 0
                and low_in_high >= th.scaffold.containment_min * (1.0 - sc_band)
                and (
                    elo_gap >= th.scaffold.min_elo_diff * (1.0 - sc_band)
                    or k_pct >= th.scaffold.skeleton_pct_min * (1.0 - sc_band)
                )
            )
            if sc_core or sc_boundary_hit:
                near_b = sc_boundary_hit or (
                    _near_threshold(low_in_high, th.scaffold.containment_min, sc_band)
                    or (elo_gap > 0 and _near_threshold(float(elo_gap), float(th.scaffold.min_elo_diff), sc_band))
                )
                out_edges.append(
                    CandidateEdge(
                        source_uid=high_f.uid,
                        target_uid=low_f.uid,
                        relation_type="SCAFFOLD",
                        is_directed=1,
                        is_cross_scope=cross_scope,
                        surface_sim=s_sim,
                        skeleton_sim=k_sim,
                        surface_pct=s_pct,
                        skeleton_pct=k_pct,
                        containment_score=low_in_high,
                        trap_overlap=trap_ov,
                        rank_score=0.7 * low_in_high + 0.3 * min(1.0, max(0, elo_gap) / 400.0),
                        k_jaccard=k_jacc,
                        needs_llm_refine=near_b,
                        refine_reason="BOUNDARY_BAND" if near_b else "",
                        rule_version=rule_ver,
                    )
                )

        # (4) CONTRASTIVE：表皮互 Top-K 极似但考点体系迥异（全量送 LLM 精裁验证教学区分度）
        is_mutual_surf = (
            sr_ab is not None and sr_ba is not None and sr_ab <= th.contrastive.knn_k and sr_ba <= th.contrastive.knn_k
        )
        if is_mutual_surf and s_pct >= th.contrastive.surface_pct_min and k_jacc < th.contrastive.k_jaccard_max:
            out_edges.append(
                CandidateEdge(
                    source_uid=ua,
                    target_uid=ub,
                    relation_type="CONTRASTIVE",
                    is_directed=0,
                    is_cross_scope=cross_scope,
                    surface_sim=s_sim,
                    skeleton_sim=k_sim,
                    surface_pct=s_pct,
                    skeleton_pct=k_pct,
                    containment_score=k_jacc,
                    trap_overlap=trap_ov,
                    rank_score=s_sim * (1.0 - k_jacc),
                    k_jaccard=k_jacc,
                    needs_llm_refine=True,
                    refine_reason="CONTRASTIVE",
                    rule_version=rule_ver,
                )
            )

        # (5) SHARED_TRAP：高 IDF 共同易错陷阱且骨架分离，跨 L1 模块乘以 cross_l1_bonus 加权排序
        if (fa.e_traps & fb.e_traps) and len(fa.e_traps & fb.e_traps) >= th.shared_trap.min_shared:
            if trap_ov >= th.shared_trap.idf_overlap_min and k_pct < th.shared_trap.skeleton_pct_max:
                bonus = th.shared_trap.cross_l1_bonus if (fa.primary_l1 and fb.primary_l1 and fa.primary_l1 != fb.primary_l1) else 1.0
                out_edges.append(
                    CandidateEdge(
                        source_uid=ua,
                        target_uid=ub,
                        relation_type="SHARED_TRAP",
                        is_directed=0,
                        is_cross_scope=cross_scope,
                        surface_sim=s_sim,
                        skeleton_sim=k_sim,
                        surface_pct=s_pct,
                        skeleton_pct=k_pct,
                        containment_score=k_jacc,
                        trap_overlap=trap_ov,
                        rank_score=trap_ov * bonus,
                        k_jaccard=k_jacc,
                        rule_version=rule_ver,
                    )
                )

    return out_edges


def apply_degree_caps(edges: list[CandidateEdge], topology_cfg: TopologyConfig) -> list[CandidateEdge]:
    """按边类型度数上限截断（无向边贪心双端均不超限；SCAFFOLD 区分出度/入度上限）。"""
    th = topology_cfg.thresholds
    undirected_caps = {
        "CLONE": th.clone.degree_cap,
        "ISOMORPHIC": th.isomorphic.degree_cap,
        "CONTRASTIVE": th.contrastive.degree_cap,
        "SHARED_TRAP": th.shared_trap.degree_cap,
    }

    by_type: dict[str, list[CandidateEdge]] = {}
    for e in edges:
        by_type.setdefault(e.relation_type, []).append(e)

    kept: list[CandidateEdge] = []
    for rtype, group in by_type.items():
        ranked = sorted(
            group,
            key=lambda x: (-float(x.rank_score), x.source_uid, x.target_uid),
        )
        if rtype == "SCAFFOLD":
            out_deg: dict[str, int] = {}
            in_deg: dict[str, int] = {}
            for e in ranked:
                if (
                    out_deg.get(e.source_uid, 0) < th.scaffold.out_degree_cap
                    and in_deg.get(e.target_uid, 0) < th.scaffold.in_degree_cap
                ):
                    kept.append(e)
                    out_deg[e.source_uid] = out_deg.get(e.source_uid, 0) + 1
                    in_deg[e.target_uid] = in_deg.get(e.target_uid, 0) + 1
        else:
            cap = undirected_caps[rtype]
            deg: dict[str, int] = {}
            for e in ranked:
                if deg.get(e.source_uid, 0) < cap and deg.get(e.target_uid, 0) < cap:
                    kept.append(e)
                    deg[e.source_uid] = deg.get(e.source_uid, 0) + 1
                    deg[e.target_uid] = deg.get(e.target_uid, 0) + 1
    return kept


def merge_relation_overrides(
    conn: sqlite3.Connection,
    machine_edges: list[CandidateEdge],
    *,
    features: dict[str, QuestionFeature],
    vector_store: VectorStore,
    scope_slug: str,
    topology_cfg: TopologyConfig,
    restrict_uids: set[str] | None = None,
) -> list[CandidateEdge]:
    """合并 relation_overrides 人工裁定：REJECT 删边，ASSERT 强制置 HUMAN_VERIFIED。"""
    edge_map: dict[tuple[str, str, str], CandidateEdge] = {}
    for e in machine_edges:
        u, v = normalize_edge_endpoints(e.relation_type, e.source_uid, e.target_uid)
        edge_map[(u, v, e.relation_type)] = CandidateEdge(
            **{
                **e.__dict__,
                "source_uid": u,
                "target_uid": v,
                "is_directed": 1 if e.relation_type == "SCAFFOLD" else 0,
            }
        )

    ov_rows = conn.execute(
        """
        SELECT source_question_uid, target_question_uid, relation_type, action, pedagogical_note
        FROM relation_overrides
        WHERE is_active = 1;
        """
    ).fetchall()

    surf_ids, surf_vecs = vector_store.load_vectors("surface", scope_slug=scope_slug)
    skel_ids, skel_vecs = vector_store.load_vectors("skeleton", scope_slug=scope_slug)
    surf_pos = {uid: i for i, uid in enumerate(surf_ids)}
    skel_pos = {uid: i for i, uid in enumerate(skel_ids)}
    idf_k, idf_t = compute_idf_weights(features)

    for r in ov_rows:
        rtype = str(r["relation_type"])
        u, v = normalize_edge_endpoints(
            rtype, str(r["source_question_uid"]), str(r["target_question_uid"])
        )
        if restrict_uids is not None and u not in restrict_uids and v not in restrict_uids:
            continue
        if u not in features or v not in features:
            continue

        key = (u, v, rtype)
        if r["action"] == "REJECT":
            edge_map.pop(key, None)
            continue

        fa, fb = features[u], features[v]
        if key in edge_map:
            existing = edge_map[key]
            existing.confidence_source = "HUMAN_VERIFIED"
            if r["pedagogical_note"]:
                existing.pedagogical_note = str(r["pedagogical_note"])
        else:
            s_sim, k_sim, s_pct, k_pct = _pair_similarities(
                u, v, surf_pos, surf_vecs, skel_pos, skel_vecs, vector_store, scope_slug
            )
            k_jacc, cont_a_b, cont_b_a = _best_branch_metrics(fa, fb, idf_k)
            trap_ov = _trap_idf_overlap(fa.e_traps, fb.e_traps, idf_t)
            cont_score = cont_b_a if rtype == "SCAFFOLD" else max(cont_a_b, cont_b_a)
            edge_map[key] = CandidateEdge(
                source_uid=u,
                target_uid=v,
                relation_type=rtype,
                is_directed=1 if rtype == "SCAFFOLD" else 0,
                is_cross_scope=int(fa.grade_subject != fb.grade_subject),
                surface_sim=s_sim,
                skeleton_sim=k_sim,
                surface_pct=s_pct,
                skeleton_pct=k_pct,
                containment_score=cont_score,
                trap_overlap=trap_ov,
                rank_score=1.0,
                k_jaccard=k_jacc,
                confidence_source="HUMAN_VERIFIED",
                pedagogical_note=str(r["pedagogical_note"] or ""),
                rule_version=topology_cfg.rule_version,
            )

    return list(edge_map.values())


def write_relations_for_scope(
    conn: sqlite3.Connection,
    edges: list[CandidateEdge],
    *,
    grade_subject: str,
    target_uids: set[str] | None = None,
) -> int:
    """持久化边表（全学段重建或仅替换触及 target_uids 的边）。"""
    if target_uids is None:
        conn.execute(
            """
            DELETE FROM question_relations
            WHERE source_question_uid IN (SELECT question_uid FROM questions WHERE grade_subject = ?)
               OR target_question_uid IN (SELECT question_uid FROM questions WHERE grade_subject = ?);
            """,
            (grade_subject, grade_subject),
        )
    else:
        uids = sorted(target_uids)
        if uids:
            ph = ",".join("?" for _ in uids)
            conn.execute(
                f"DELETE FROM question_relations WHERE source_question_uid IN ({ph}) OR target_question_uid IN ({ph});",
                [*uids, *uids],
            )

    for e in edges:
        u, v = normalize_edge_endpoints(e.relation_type, e.source_uid, e.target_uid)
        conn.execute(
            """
            INSERT OR REPLACE INTO question_relations(
                source_question_uid, target_question_uid, relation_type, is_directed,
                is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
                containment_score, trap_overlap, confidence_source, pedagogical_note, rule_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                u,
                v,
                e.relation_type,
                1 if e.relation_type == "SCAFFOLD" else 0,
                e.is_cross_scope,
                e.surface_sim,
                e.skeleton_sim,
                e.surface_pct,
                e.skeleton_pct,
                e.containment_score,
                e.trap_overlap,
                e.confidence_source,
                e.pedagogical_note,
                e.rule_version,
            ),
        )
    return len(edges)


def weave_single_question(
    conn: sqlite3.Connection,
    vector_store: VectorStore,
    *,
    question_uid: str,
    topology_cfg: TopologyConfig,
    include_unservable: bool = False,
) -> dict[str, Any]:
    """单题 1 vs N 增量拓扑补边，对新边端点执行局部度数上限重截断并合并人工覆写。"""
    q_row = conn.execute(
        """
        SELECT q.question_uid, q.grade_subject, gs.slug
        FROM questions q
        JOIN grade_subjects gs ON gs.grade_subject = q.grade_subject
        WHERE q.question_uid = ?;
        """,
        (question_uid,),
    ).fetchone()
    if q_row is None:
        return {"question_uid": question_uid, "written": 0}

    grade_subject = str(q_row["grade_subject"])
    scope_slug = str(q_row["slug"])
    features = load_question_features(
        conn,
        grade_subject=grade_subject,
        include_unservable=include_unservable,
    )
    if question_uid not in features:
        conn.execute(
            "DELETE FROM question_relations WHERE source_question_uid = ? OR target_question_uid = ?;",
            (question_uid, question_uid),
        )
        return {"question_uid": question_uid, "written": 0}

    new_candidates = generate_candidate_edges(
        conn,
        vector_store,
        grade_subject=grade_subject,
        scope_slug=scope_slug,
        topology_cfg=topology_cfg,
        features=features,
        target_uids={question_uid},
        include_unservable=include_unservable,
    )
    # 先对与目标题关联的所有机器边连同对端现有同类型边一并校验度数上限
    touched_peers = {question_uid}
    for e in new_candidates:
        touched_peers.add(e.source_uid)
        touched_peers.add(e.target_uid)

    existing_rows = conn.execute(
        """
        SELECT * FROM question_relations
        WHERE source_question_uid IN ({ph}) OR target_question_uid IN ({ph});
        """.format(ph=",".join("?" for _ in touched_peers)),
        [*sorted(touched_peers), *sorted(touched_peers)],
    ).fetchall()

    combined: dict[tuple[str, str, str], CandidateEdge] = {}
    for r in existing_rows:
        if r["source_question_uid"] == question_uid or r["target_question_uid"] == question_uid:
            continue
        rtype = str(r["relation_type"])
        key = (str(r["source_question_uid"]), str(r["target_question_uid"]), rtype)
        rank_s = (
            (float(r["surface_sim"]) + float(r["skeleton_sim"])) * 0.5
            if rtype == "CLONE"
            else float(r["containment_score"]) or float(r["trap_overlap"]) or float(r["skeleton_sim"])
        )
        combined[key] = CandidateEdge(
            source_uid=key[0],
            target_uid=key[1],
            relation_type=rtype,
            is_directed=int(r["is_directed"]),
            is_cross_scope=int(r["is_cross_scope"]),
            surface_sim=float(r["surface_sim"]),
            skeleton_sim=float(r["skeleton_sim"]),
            surface_pct=float(r["surface_pct"]),
            skeleton_pct=float(r["skeleton_pct"]),
            containment_score=float(r["containment_score"]),
            trap_overlap=float(r["trap_overlap"]),
            rank_score=rank_s,
            confidence_source=str(r["confidence_source"]),
            pedagogical_note=str(r["pedagogical_note"]),
            rule_version=str(r["rule_version"]),
        )
    for e in new_candidates:
        u, v = normalize_edge_endpoints(e.relation_type, e.source_uid, e.target_uid)
        combined[(u, v, e.relation_type)] = CandidateEdge(
            **{**e.__dict__, "source_uid": u, "target_uid": v}
        )

    capped = apply_degree_caps(list(combined.values()), topology_cfg)
    final_edges = merge_relation_overrides(
        conn,
        capped,
        features=features,
        vector_store=vector_store,
        scope_slug=scope_slug,
        topology_cfg=topology_cfg,
        restrict_uids=touched_peers,
    )

    # 仅重写关联边集合
    ph = ",".join("?" for _ in touched_peers)
    sorted_peers = sorted(touched_peers)
    conn.execute(
        f"DELETE FROM question_relations WHERE source_question_uid IN ({ph}) OR target_question_uid IN ({ph});",
        [*sorted_peers, *sorted_peers],
    )
    for e in final_edges:
        u, v = normalize_edge_endpoints(e.relation_type, e.source_uid, e.target_uid)
        conn.execute(
            """
            INSERT OR REPLACE INTO question_relations(
                source_question_uid, target_question_uid, relation_type, is_directed,
                is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
                containment_score, trap_overlap, confidence_source, pedagogical_note, rule_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                u,
                v,
                e.relation_type,
                1 if e.relation_type == "SCAFFOLD" else 0,
                e.is_cross_scope,
                e.surface_sim,
                e.skeleton_sim,
                e.surface_pct,
                e.skeleton_pct,
                e.containment_score,
                e.trap_overlap,
                e.confidence_source,
                e.pedagogical_note,
                e.rule_version,
            ),
        )
    return {
        "question_uid": question_uid,
        "written": sum(1 for e in final_edges if e.source_uid == question_uid or e.target_uid == question_uid),
    }
