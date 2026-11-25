"""Stage 4 流水线：向量与冻结分位表重建、五边拓扑织网、LLM 精裁与校准抽样。"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from shuati.config import Settings, TopologyConfig, resolve_subagent_model
from shuati.core import refresh
from shuati.core.topology import (
    RELATION_TYPES,
    CandidateEdge,
    QuestionFeature,
    apply_degree_caps,
    generate_candidate_edges,
    load_question_features,
    merge_relation_overrides,
    normalize_edge_endpoints,
    pretruncate_refine_candidates,
    write_relations_for_scope,
)
from shuati.db import repository
from shuati.db.connection import CompileLock, PreconditionError
from shuati.llm.embeddings import EmbeddingClient
from shuati.llm.gateway import LLMGateway
from shuati.llm.prompt_builder import build_stage4_refine_messages
from shuati.llm.seam_loader import load_prompt_seams
from shuati.models.schemas import Stage4RefineBatchOutput, Stage4RefineItem
from shuati.pipeline.subagent_bridge import export_task_batches, load_and_validate_results
from shuati.vectors.store import VectorStore, evaluate_scope_drift


def _compute_scope_baseline(
    conn: sqlite3.Connection,
    grade_subject: str,
) -> tuple[int, dict[str, float]]:
    """计算指定学段当前可服务题量与 L1 模块占比分布。"""
    rows = conn.execute(
        """
        SELECT eff_primary_l1_module, COUNT(*) AS cnt
        FROM questions
        WHERE grade_subject = ? AND is_retired = 0 AND eff_is_excluded = 0 AND eff_is_servable = 1
        GROUP BY eff_primary_l1_module
        ORDER BY eff_primary_l1_module;
        """,
        (grade_subject,),
    ).fetchall()
    servable_total = sum(int(r["cnt"]) for r in rows)
    if servable_total <= 0:
        return 0, {}
    l1_dist = {
        str(r["eff_primary_l1_module"] or ""): int(r["cnt"]) / float(servable_total)
        for r in rows
    }
    return servable_total, l1_dist


def _reconstruct_existing_edge(
    row: sqlite3.Row,
    features: dict[str, QuestionFeature],
    topology_cfg: TopologyConfig,
) -> CandidateEdge:
    """将 question_relations 既有数据库行还原为 CandidateEdge 以参与局部度数上限与覆写合并。"""
    su = str(row["source_question_uid"])
    tu = str(row["target_question_uid"])
    rtype = str(row["relation_type"])
    s_sim = float(row["surface_sim"])
    k_sim = float(row["skeleton_sim"])
    cont = float(row["containment_score"])
    trap_ov = float(row["trap_overlap"])
    fa = features.get(su)
    fb = features.get(tu)

    if rtype == "CLONE":
        rank_s = (s_sim + k_sim) * 0.5
    elif rtype == "ISOMORPHIC":
        rank_s = 0.6 * k_sim + 0.4 * cont
    elif rtype == "SCAFFOLD":
        elo_gap = (fb.elo - fa.elo) if (fa is not None and fb is not None) else 200
        rank_s = 0.7 * cont + 0.3 * min(1.0, max(0, elo_gap) / 400.0)
    elif rtype == "CONTRASTIVE":
        rank_s = s_sim * (1.0 - cont)
    else:
        cross_l1 = (
            fa is not None
            and fb is not None
            and bool(fa.primary_l1)
            and bool(fb.primary_l1)
            and fa.primary_l1 != fb.primary_l1
        )
        bonus = topology_cfg.thresholds.shared_trap.cross_l1_bonus if cross_l1 else 1.0
        rank_s = trap_ov * bonus

    return CandidateEdge(
        source_uid=su,
        target_uid=tu,
        relation_type=rtype,
        is_directed=int(row["is_directed"]),
        is_cross_scope=int(row["is_cross_scope"]),
        surface_sim=s_sim,
        skeleton_sim=k_sim,
        surface_pct=float(row["surface_pct"]),
        skeleton_pct=float(row["skeleton_pct"]),
        containment_score=cont,
        trap_overlap=trap_ov,
        rank_score=rank_s,
        k_jaccard=cont if rtype in ("ISOMORPHIC", "CONTRASTIVE", "SHARED_TRAP") else 0.0,
        confidence_source=str(row["confidence_source"]),
        pedagogical_note=str(row["pedagogical_note"]),
        rule_version=str(row["rule_version"]),
    )


def _cap_incremental_target_edges(
    untouched_edges: list[CandidateEdge],
    target_candidates: list[CandidateEdge],
    topology_cfg: TopologyConfig,
) -> list[CandidateEdge]:
    """在保留未触及全库既有边不超限的前提下，对触及目标题集的新边按度数上限截断。"""
    th = topology_cfg.thresholds
    undirected_caps = {
        "CLONE": th.clone.degree_cap,
        "ISOMORPHIC": th.isomorphic.degree_cap,
        "CONTRASTIVE": th.contrastive.degree_cap,
        "SHARED_TRAP": th.shared_trap.degree_cap,
    }

    untouched_by_type: dict[str, list[CandidateEdge]] = {}
    for e in untouched_edges:
        untouched_by_type.setdefault(e.relation_type, []).append(e)

    target_by_type: dict[str, list[CandidateEdge]] = {}
    for e in target_candidates:
        target_by_type.setdefault(e.relation_type, []).append(e)

    kept_target: list[CandidateEdge] = []
    for rtype, group in target_by_type.items():
        ranked = sorted(
            group,
            key=lambda x: (-float(x.rank_score), x.source_uid, x.target_uid),
        )
        if rtype == "SCAFFOLD":
            out_deg: dict[str, int] = {}
            in_deg: dict[str, int] = {}
            for ue in untouched_by_type.get(rtype, ()):
                out_deg[ue.source_uid] = out_deg.get(ue.source_uid, 0) + 1
                in_deg[ue.target_uid] = in_deg.get(ue.target_uid, 0) + 1
            for e in ranked:
                if (
                    out_deg.get(e.source_uid, 0) < th.scaffold.out_degree_cap
                    and in_deg.get(e.target_uid, 0) < th.scaffold.in_degree_cap
                ):
                    kept_target.append(e)
                    out_deg[e.source_uid] = out_deg.get(e.source_uid, 0) + 1
                    in_deg[e.target_uid] = in_deg.get(e.target_uid, 0) + 1
        else:
            cap = undirected_caps[rtype]
            deg: dict[str, int] = {}
            for ue in untouched_by_type.get(rtype, ()):
                u0, v0 = normalize_edge_endpoints(rtype, ue.source_uid, ue.target_uid)
                deg[u0] = deg.get(u0, 0) + 1
                deg[v0] = deg.get(v0, 0) + 1
            for e in ranked:
                u, v = normalize_edge_endpoints(rtype, e.source_uid, e.target_uid)
                if deg.get(u, 0) < cap and deg.get(v, 0) < cap:
                    kept_target.append(e)
                    deg[u] = deg.get(u, 0) + 1
                    deg[v] = deg.get(v, 0) + 1
    return kept_target


def _build_surface_text(question_text: str | None, visual_facts: str | None) -> str:
    """构造 Stage 4 表皮向量拼接文本：eff_question_text + ('\\n' + eff_visual_facts if non-empty)。"""
    qt = str(question_text or "")
    vf = str(visual_facts or "")
    return qt + ("\n" + vf if vf.strip() else "")


def _build_refine_pair_payload(
    pid: str,
    edge: CandidateEdge,
    fa: QuestionFeature,
    fb: QuestionFeature,
) -> dict[str, Any]:
    return {
        "pair_id": pid,
        "relation_type": edge.relation_type,
        "source_text": fa.eff_text,
        "source_skeleton": fa.eff_skeleton,
        "source_tags": fa.tag_labels,
        "target_text": fb.eff_text,
        "target_skeleton": fb.eff_skeleton,
        "target_tags": fb.tag_labels,
        "surface_pct": edge.surface_pct,
        "skeleton_pct": edge.skeleton_pct,
        "k_jaccard": edge.k_jaccard,
        "containment": edge.containment_score,
    }


async def reindex_vectors_and_quantiles_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str | None = None,
    rebuild_vectors: bool = True,
    rebuild_quantiles: bool = True,
    force_rebuild: bool = False,
    include_unservable: bool = False,
    use_mock: bool = False,
    vectors_dir: Path | str = Path("data/vectors"),
) -> dict[str, Any]:
    """同步 5 类本地向量矩阵并按学段与通道冻结 101 点经验分位表（meta.json）。"""
    store = VectorStore(vectors_dir)
    if force_rebuild:
        store.clear_model_binding_for_rebuild()

    if grade_subject:
        gs_rows = conn.execute(
            "SELECT grade_subject, slug, taxonomy_version FROM grade_subjects WHERE grade_subject = ?;",
            (grade_subject,),
        ).fetchall()
    else:
        gs_rows = conn.execute(
            "SELECT grade_subject, slug, taxonomy_version FROM grade_subjects ORDER BY grade_subject;"
        ).fetchall()

    if not gs_rows:
        raise PreconditionError("PRECONDITION_FAILED: 未找到待处理的学段学科")

    vector_stats: dict[str, Any] = {}
    quantile_stats: dict[str, Any] = {}

    if rebuild_vectors:
        where_serve = "" if include_unservable else "AND q.eff_is_servable = 1"
        client = EmbeddingClient(settings, conn=conn, use_mock=use_mock)
        try:
            for gsr in gs_rows:
                gs = str(gsr["grade_subject"])
                slug = str(gsr["slug"])
                tax_ver = str(gsr["taxonomy_version"] or "")
                q_rows = conn.execute(
                    f"""
                    SELECT q.question_uid, q.eff_question_text, q.eff_visual_facts, q.eff_abstract_skeleton
                    FROM questions q
                    JOIN compile_state cs ON cs.question_uid = q.question_uid AND cs.stage = 'S2'
                    WHERE q.grade_subject = ? AND q.is_retired = 0 AND q.eff_is_excluded = 0 {where_serve} AND cs.status = 'OK'
                    ORDER BY q.question_uid;
                    """,
                    (gs,),
                ).fetchall()
                vector_stats[f"surface_{slug}"] = await store.sync_vectors(
                    "surface",
                    [
                        (str(r["question_uid"]), _build_surface_text(r["eff_question_text"], r["eff_visual_facts"]))
                        for r in q_rows
                    ],
                    client,
                    scope_slug=slug,
                    force_rebuild=force_rebuild,
                    stage="REINDEX_EMBED",
                )
                vector_stats[f"skeleton_{slug}"] = await store.sync_vectors(
                    "skeleton",
                    [(str(r["question_uid"]), str(r["eff_abstract_skeleton"] or r["eff_question_text"] or "")) for r in q_rows],
                    client,
                    scope_slug=slug,
                    force_rebuild=force_rebuild,
                    stage="REINDEX_EMBED",
                )

                if tax_ver:
                    l3_rows = conn.execute(
                        """
                        SELECT node_id, l1_module, l2_entity, l3_operator
                        FROM knowledge_nodes
                        WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3' AND redirect_to_id IS NULL
                        ORDER BY node_id;
                        """,
                        (gs, tax_ver),
                    ).fetchall()
                    vector_stats[f"l3_nodes_{slug}"] = await store.sync_vectors(
                        "l3_nodes",
                        [(str(r["node_id"]), f"{r['l1_module']}/{r['l2_entity']}/{r['l3_operator']}") for r in l3_rows],
                        client,
                        scope_slug=slug,
                        force_rebuild=force_rebuild,
                        stage="REINDEX_EMBED",
                    )

            for ckind, ns in (("METHOD", "concepts_method"), ("MOTIF", "concepts_motif")):
                c_rows = conn.execute(
                    "SELECT concept_id, name FROM concepts WHERE kind = ? AND is_seed = 0 AND redirect_to_id IS NULL ORDER BY concept_id;",
                    (ckind,),
                ).fetchall()
                vector_stats[ns] = await store.sync_vectors(
                    ns,  # type: ignore[arg-type]
                    [(str(r["concept_id"]), str(r["name"])) for r in c_rows],
                    client,
                    force_rebuild=force_rebuild,
                    stage="REINDEX_EMBED",
                )
        finally:
            await client.aclose()

    if rebuild_quantiles:
        for gsr in gs_rows:
            gs = str(gsr["grade_subject"])
            slug = str(gsr["slug"])
            servable_total, cur_dist = _compute_scope_baseline(conn, gs)
            scope_baseline = {"n0": servable_total, "l1_dist": cur_dist}
            quantile_stats[slug] = store.rebuild_quantiles(
                slug,
                ("surface", "skeleton"),
                scope_baseline=scope_baseline,
            )
            s3d_ok_rows = conn.execute(
                """
                SELECT q.question_uid
                FROM questions q
                JOIN compile_state cs ON cs.question_uid = q.question_uid AND cs.stage = 'S3D'
                WHERE q.grade_subject = ?
                  AND q.is_retired = 0
                  AND q.eff_is_excluded = 0
                  AND q.eff_is_servable = 1
                  AND cs.status = 'OK'
                ORDER BY q.question_uid;
                """,
                (gs,),
            ).fetchall()
            for qr in s3d_ok_rows:
                refresh.mark_stages_stale(conn, str(qr["question_uid"]), ["S4"])
        conn.commit()

    meta = store.load_meta()
    return {
        "vectors_dir": str(store.vectors_dir),
        "embedding_model": meta.get("embedding_model", ""),
        "dimension": meta.get("dimension", 0),
        "meta_version": meta.get("meta_version", 0),
        "vector_stats": vector_stats,
        "quantile_stats": quantile_stats,
    }


def _mark_s4_blocked(conn: sqlite3.Connection, uid: str, reason: str) -> None:
    conn.execute(
        """
        INSERT INTO compile_state(question_uid, stage, status, last_error, updated_at)
        VALUES (?, 'S4', 'BLOCKED', ?, datetime('now'))
        ON CONFLICT(question_uid, stage) DO UPDATE SET
            status = 'BLOCKED', last_error = excluded.last_error, updated_at = datetime('now');
        """,
        (uid, reason),
    )


async def run_stage4_weave_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str,
    mode: Literal["api", "export", "import"] = "api",
    batch_size: int = 25,
    include_unservable: bool = False,
    tasks_dir: Path | str = Path("data/unified/stage3_4/stage4"),
    results_path: Path | str | None = None,
    vectors_dir: Path | str = Path("data/vectors"),
    use_mock: bool = False,
    lock_path: Path | str = Path("data/.compile.lock"),
    config_dir: Path | str = Path("config"),
    subagent_model: str | None = None,
    prompt_profile: str | None = None,
    overwrite_pending: bool = False,
    only_uids: list[str] | None = None,
    ignore_drift: bool = False,
) -> dict[str, Any]:
    """Stage 4 四通道五边拓扑织网（规则矩阵 + 边界带/CONTRASTIVE 精裁 + 度数截断 + relation_overrides 合并）。"""
    gs_row = conn.execute(
        "SELECT grade_subject, slug FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科不存在: {grade_subject!r}")
    slug = str(gs_row["slug"])

    valid_by_batch: dict[str, Stage4RefineBatchOutput] = {}
    loaded_map: dict[str, Any] = {}
    manifest_file = Path(tasks_dir) / "stage4_manifest.json"
    if mode == "import":
        if results_path is None:
            raise PreconditionError("PRECONDITION_FAILED: --mode import 必须提供 --results")
        task_map_file = Path(tasks_dir) / "stage4_task_map.json"
        valid_by_batch, loaded_map, errs = load_and_validate_results(
            "stage4", results_path, task_map_file, Stage4RefineBatchOutput
        )
        if errs:
            raise PreconditionError(
                f"PRECONDITION_FAILED: Stage 4 结果校验失败 {len(errs)} 批，详见 retry_tasks.jsonl"
            )
        if manifest_file.is_file():
            try:
                m_loaded = json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise PreconditionError(
                    f"PRECONDITION_FAILED: Stage 4 清单文件无法解析 ({manifest_file}): {exc}"
                ) from exc
            if not isinstance(m_loaded, dict):
                raise PreconditionError(
                    f"PRECONDITION_FAILED: Stage 4 清单文件根结构非对象 ({manifest_file})"
                )
            expected_batch_ids = [
                str(x).strip()
                for x in (m_loaded.get("batch_ids") or [])
                if isinstance(x, str) and x.strip()
            ]
            expected_batch_count = int(m_loaded.get("batch_count") or 0)
            if expected_batch_ids:
                single_ok = (
                    len(expected_batch_ids) == 1 and "stage4_single" in valid_by_batch
                )
                missing_batches = [
                    bid for bid in expected_batch_ids if bid not in valid_by_batch
                ]
                if missing_batches and not single_ok:
                    raise PreconditionError(
                        f"PRECONDITION_FAILED: Stage 4 回传批次不完整，缺失 {len(missing_batches)}/{len(expected_batch_ids)} 批: {missing_batches[:10]}"
                    )
            elif expected_batch_count > 0 and len(valid_by_batch) < expected_batch_count:
                raise PreconditionError(
                    f"PRECONDITION_FAILED: Stage 4 回传批次数不足 ({len(valid_by_batch)}/{expected_batch_count})"
                )
        elif loaded_map.get("pairs") and not valid_by_batch:
            raise PreconditionError(
                "PRECONDITION_FAILED: Stage 4 存在待精裁候选对但回传结果为空"
            )

    resolved_target_set: set[str] | None = None
    if only_uids:
        resolved_target_set = set()
        for raw_id in only_uids:
            s_id = str(raw_id).strip()
            if not s_id:
                continue
            uid_resolved = repository.resolve_question_uid(conn, s_id)
            q_gs_row = conn.execute(
                "SELECT grade_subject FROM raw_questions WHERE question_uid = ?;",
                (uid_resolved,),
            ).fetchone()
            if q_gs_row is not None and str(q_gs_row["grade_subject"]) != grade_subject:
                raise PreconditionError(
                    f"PRECONDITION_FAILED: 题目 {uid_resolved!r} 属于学段 {q_gs_row['grade_subject']!r}，与目标学段 {grade_subject!r} 不一致"
                )
            resolved_target_set.add(uid_resolved)
        if not resolved_target_set:
            resolved_target_set = None
    elif mode == "import" and isinstance(loaded_map.get("target_uids"), list):
        restored = {str(u).strip() for u in loaded_map["target_uids"] if str(u).strip()}
        if restored:
            resolved_target_set = restored

    # 1. 更新 compile_state 门禁：S3D != 'OK' 或默认不可服务题目置 BLOCKED（only_uids 模式下不改动无关题状态）
    q_rows = conn.execute(
        """
        SELECT q.question_uid, q.eff_is_servable, q.eff_is_excluded,
               cs3d.status AS s3d_status
        FROM questions q
        LEFT JOIN compile_state cs3d ON cs3d.question_uid = q.question_uid AND cs3d.stage = 'S3D'
        WHERE q.grade_subject = ? AND q.is_retired = 0
        ORDER BY q.question_uid;
        """,
        (grade_subject,),
    ).fetchall()

    eligible_uids: list[str] = []
    blocked_s3d = 0
    blocked_unservable = 0
    for r in q_rows:
        uid = str(r["question_uid"])
        in_target_scope = resolved_target_set is None or uid in resolved_target_set
        if r["s3d_status"] != "OK":
            if in_target_scope:
                _mark_s4_blocked(conn, uid, "上游 S3D 非 OK")
                blocked_s3d += 1
        elif not include_unservable and int(r["eff_is_servable"]) != 1:
            if in_target_scope:
                _mark_s4_blocked(conn, uid, "eff_is_servable = 0 (NEEDS_REVIEW/EXCLUDED)")
                blocked_unservable += 1
        else:
            eligible_uids.append(uid)
    conn.commit()

    store = VectorStore(vectors_dir)
    quantiles_rebuilt = False
    # 2. 差量补齐向量与冻结分位表（保证 meta.json 就绪，并在未忽略漂移时自动刷新失真分位表）
    if mode != "import":
        embed_client = EmbeddingClient(settings, conn=conn, use_mock=use_mock)
        try:
            store.validate_model_match(embed_client.model_name, allow_empty=True)
            where_serve = "" if include_unservable else "AND q.eff_is_servable = 1"
            all_active_rows = conn.execute(
                f"""
                SELECT q.question_uid, q.eff_question_text, q.eff_visual_facts, q.eff_abstract_skeleton
                FROM questions q
                JOIN compile_state cs ON cs.question_uid = q.question_uid AND cs.stage = 'S2'
                WHERE q.grade_subject = ? AND q.is_retired = 0 AND q.eff_is_excluded = 0 {where_serve} AND cs.status = 'OK'
                ORDER BY q.question_uid;
                """,
                (grade_subject,),
            ).fetchall()
            await store.sync_vectors(
                "surface",
                [
                    (str(r["question_uid"]), _build_surface_text(r["eff_question_text"], r["eff_visual_facts"]))
                    for r in all_active_rows
                ],
                embed_client,
                scope_slug=slug,
                stage="STAGE4_EMBED",
            )
            await store.sync_vectors(
                "skeleton",
                [(str(r["question_uid"]), str(r["eff_abstract_skeleton"] or r["eff_question_text"] or "")) for r in all_active_rows],
                embed_client,
                scope_slug=slug,
                stage="STAGE4_EMBED",
            )
        finally:
            await embed_client.aclose()

        servable_total, cur_dist = _compute_scope_baseline(conn, grade_subject)
        drift_info = evaluate_scope_drift(store.load_meta(), slug, servable_total, cur_dist)
        quantiles_missing = (
            not store.has_quantiles(slug, "surface")
            or not store.has_quantiles(slug, "skeleton")
        )
        if quantiles_missing or (
            not ignore_drift
            and (bool(drift_info["drifted"]) or not bool(drift_info["has_baseline"]))
        ):
            store.rebuild_quantiles(
                slug,
                ("surface", "skeleton"),
                scope_baseline={"n0": servable_total, "l1_dist": cur_dist},
            )
            quantiles_rebuilt = True

    features = load_question_features(
        conn, grade_subject=grade_subject, include_unservable=include_unservable
    )
    raw_candidates = generate_candidate_edges(
        conn,
        store,
        grade_subject=grade_subject,
        scope_slug=slug,
        topology_cfg=settings.topology,
        features=features,
        target_uids=resolved_target_set,
        include_unservable=include_unservable,
    )

    auto_edges: list[CandidateEdge] = []
    raw_refine_candidates: list[CandidateEdge] = []
    use_refine = bool(settings.pipeline.enable_stage4_llm_refine)
    for e in raw_candidates:
        if use_refine and e.needs_llm_refine:
            raw_refine_candidates.append(e)
        else:
            auto_edges.append(e)

    refine_candidates = pretruncate_refine_candidates(
        raw_refine_candidates, settings.topology
    )

    # 3. 处理二级 LLM 精裁（api / export / import）
    verified_refine_edges: list[CandidateEdge] = []
    rejected_refine_keys: set[tuple[tuple[str, str], str]] = set()
    if mode == "export":
        eff_bs = max(1, int(batch_size))
        batches: list[dict[str, Any]] = []
        task_map: dict[str, Any] = {
            "grade_subject": grade_subject,
            "include_unservable": include_unservable,
            "pairs": {},
        }
        if resolved_target_set is not None:
            task_map["target_uids"] = sorted(resolved_target_set)
        for b_idx, start in enumerate(range(0, len(refine_candidates), eff_bs), start=1):
            chunk = refine_candidates[start : start + eff_bs]
            p_items: list[dict[str, Any]] = []
            tids: list[str] = []
            for j, e in enumerate(chunk, start=1):
                pid = f"s4_{b_idx:04d}_{j:03d}"
                tids.append(pid)
                fa = features[e.source_uid]
                fb = features[e.target_uid]
                p_items.append(_build_refine_pair_payload(pid, e, fa, fb))
                task_map["pairs"][pid] = {
                    "source_uid": e.source_uid,
                    "target_uid": e.target_uid,
                    "relation_type": e.relation_type,
                }
            msgs = build_stage4_refine_messages(
                candidate_pairs=p_items,
                grade_subject=grade_subject,
                config_dir=Path(config_dir),
                prompt_profile=prompt_profile,
            )
            batches.append({"batch_id": f"stage4_batch_{b_idx:04d}", "messages": msgs, "task_item_ids": tids})

        resolved_model = resolve_subagent_model(settings, "stage4", subagent_model)
        cfg_p = Path(config_dir)
        if (cfg_p / "prompts" / "default.yaml").is_file() or prompt_profile is not None:
            bundle = load_prompt_seams(
                config_dir=cfg_p, grade_subject=grade_subject, profile=prompt_profile
            )
            p_ver = bundle.prompt_versions.get("stage4", "s4_v2")
            p_digest = bundle.prompt_digest
        else:
            p_ver = "s4_v2"
            p_digest = ""

        manifest = export_task_batches(
            "stage4",
            batches,
            task_map,
            tasks_dir,
            schema_cls=Stage4RefineBatchOutput,
            subagent_model=resolved_model,
            prompt_version=p_ver,
            prompt_digest=p_digest,
            prompt_profile=prompt_profile,
            max_parallel_workers=settings.subagent.max_parallel_workers,
            overwrite_pending=overwrite_pending,
        )
        target_eligible_cnt = (
            len([u for u in eligible_uids if u in resolved_target_set])
            if resolved_target_set is not None
            else len(eligible_uids)
        )
        return {
            "mode": "export",
            "grade_subject": grade_subject,
            "eligible_questions": target_eligible_cnt,
            "auto_candidates": len(auto_edges),
            "raw_refine_candidates": len(raw_refine_candidates),
            "refine_candidates": len(refine_candidates),
            "quantiles_rebuilt": quantiles_rebuilt,
            "target_uids": sorted(resolved_target_set) if resolved_target_set is not None else None,
            "manifest": manifest,
        }

    if mode == "import":
        pairs_map = loaded_map.get("pairs", {})
        edge_by_key = {
            (normalize_edge_endpoints(e.relation_type, e.source_uid, e.target_uid), e.relation_type): e
            for e in raw_refine_candidates
        }
        verdict_by_key: dict[tuple[tuple[str, str], str], tuple[bool, str]] = {}
        for out_model in valid_by_batch.values():
            for dec in out_model.decisions:
                pmeta = pairs_map.get(dec.pair_id)
                if not pmeta:
                    continue
                rt = str(pmeta["relation_type"])
                ep = normalize_edge_endpoints(rt, str(pmeta["source_uid"]), str(pmeta["target_uid"]))
                verdict_by_key[(ep, rt)] = (bool(dec.is_valid), dec.pedagogical_note.strip())
        for k, cand in edge_by_key.items():
            verdict = verdict_by_key.get(k)
            if verdict is not None:
                if verdict[0]:
                    cand.confidence_source = "LLM_VERIFIED"
                    cand.pedagogical_note = verdict[1]
                    verified_refine_edges.append(cand)
                else:
                    rejected_refine_keys.add(k)
    elif refine_candidates:
        cfg_p = Path(config_dir)
        if (cfg_p / "prompts" / "default.yaml").is_file() or prompt_profile is not None:
            bundle = load_prompt_seams(
                config_dir=cfg_p, grade_subject=grade_subject, profile=prompt_profile
            )
            p_ver = bundle.prompt_versions.get("stage4", "s4_v2")
        else:
            p_ver = "s4_v2"
        gateway = LLMGateway(settings, conn=conn)
        eff_bs = max(1, int(batch_size))
        chunks = [refine_candidates[i : i + eff_bs] for i in range(0, len(refine_candidates), eff_bs)]

        async def _refine_chunk(b_idx: int, chunk: list[CandidateEdge]) -> tuple[dict[str, CandidateEdge], Stage4RefineBatchOutput]:
            pid_to_edge: dict[str, CandidateEdge] = {}
            p_items: list[dict[str, Any]] = []
            for j, e in enumerate(chunk, start=1):
                pid = f"s4_{b_idx:04d}_{j:03d}"
                pid_to_edge[pid] = e
                fa = features[e.source_uid]
                fb = features[e.target_uid]
                p_items.append(_build_refine_pair_payload(pid, e, fa, fb))
            msgs = build_stage4_refine_messages(
                candidate_pairs=p_items,
                grade_subject=grade_subject,
                config_dir=Path(config_dir),
                prompt_profile=prompt_profile,
            )
            res = await gateway.call_structured(
                slot="judge_llm",
                stage="STAGE4_REFINE",
                messages=msgs,
                schema=Stage4RefineBatchOutput,
                prompt_version=p_ver,
            )
            return pid_to_edge, res.parsed

        try:
            refine_results = await asyncio.gather(
                *(_refine_chunk(b_idx, ch) for b_idx, ch in enumerate(chunks, start=1))
            )
        finally:
            await gateway.aclose()

        for pid_to_edge, out_model in refine_results:
            dec_map = {d.pair_id: d for d in out_model.decisions}
            for pid, cand in pid_to_edge.items():
                ep_key = (
                    normalize_edge_endpoints(cand.relation_type, cand.source_uid, cand.target_uid),
                    cand.relation_type,
                )
                refine_dec: Stage4RefineItem | None = dec_map.get(pid)
                if refine_dec is None:
                    # 仅对 BOUNDARY_BAND 且无显式拒绝时保守留存规则命中（Mock 默认返回空列表场景），CONTRASTIVE 需显式认可或 rule_matrix 仅在 mock 时兜底
                    if settings.pipeline.use_mock_llm and cand.refine_reason == "BOUNDARY_BAND":
                        verified_refine_edges.append(cand)
                    continue
                if refine_dec.is_valid:
                    cand.confidence_source = "LLM_VERIFIED"
                    cand.pedagogical_note = refine_dec.pedagogical_note.strip()
                    verified_refine_edges.append(cand)
                else:
                    rejected_refine_keys.add(ep_key)

    # 4. 度数上限截断 + 合并 relation_overrides + 落库
    if resolved_target_set is None:
        capped = apply_degree_caps([*auto_edges, *verified_refine_edges], settings.topology)
        final_edges = merge_relation_overrides(
            conn,
            capped,
            features=features,
            vector_store=store,
            scope_slug=slug,
            topology_cfg=settings.topology,
        )
        edges_to_write = final_edges
        uids_to_mark_ok = eligible_uids
    else:
        existing_rows = conn.execute(
            """
            SELECT *
            FROM question_relations
            WHERE source_question_uid IN (SELECT question_uid FROM questions WHERE grade_subject = ?)
               OR target_question_uid IN (SELECT question_uid FROM questions WHERE grade_subject = ?);
            """,
            (grade_subject, grade_subject),
        ).fetchall()
        raw_candidate_keys = {
            (normalize_edge_endpoints(e.relation_type, e.source_uid, e.target_uid), e.relation_type)
            for e in raw_candidates
        }
        untouched_edges: list[CandidateEdge] = []
        target_pool: dict[tuple[tuple[str, str], str], CandidateEdge] = {}
        for row in existing_rows:
            su = str(row["source_question_uid"])
            tu = str(row["target_question_uid"])
            rtype = str(row["relation_type"])
            rec_edge = _reconstruct_existing_edge(row, features, settings.topology)
            if su not in resolved_target_set and tu not in resolved_target_set:
                untouched_edges.append(rec_edge)
            elif str(row["confidence_source"]) == "LLM_VERIFIED":
                ep_key = (normalize_edge_endpoints(rtype, su, tu), rtype)
                if ep_key in raw_candidate_keys and ep_key not in rejected_refine_keys:
                    target_pool[ep_key] = rec_edge

        for e in [*auto_edges, *verified_refine_edges]:
            ep = normalize_edge_endpoints(e.relation_type, e.source_uid, e.target_uid)
            ep_key = (ep, e.relation_type)
            prev = target_pool.get(ep_key)
            if prev is not None and prev.confidence_source == "LLM_VERIFIED" and e.confidence_source != "LLM_VERIFIED":
                continue
            target_pool[ep_key] = CandidateEdge(
                **{**e.__dict__, "source_uid": ep[0], "target_uid": ep[1]}
            )

        capped_target = _cap_incremental_target_edges(
            untouched_edges,
            list(target_pool.values()),
            settings.topology,
        )
        merged_target = merge_relation_overrides(
            conn,
            capped_target,
            features=features,
            vector_store=store,
            scope_slug=slug,
            topology_cfg=settings.topology,
            restrict_uids=resolved_target_set,
        )
        edges_to_write = [
            e
            for e in merged_target
            if e.source_uid in resolved_target_set or e.target_uid in resolved_target_set
        ]
        final_edges = edges_to_write
        uids_to_mark_ok = [u for u in eligible_uids if u in resolved_target_set]

    meta_ver = store.load_meta().get("meta_version", 0)
    s4_hash = f"rule={settings.topology.rule_version}|meta={meta_ver}|unservable={int(include_unservable)}"

    with CompileLock(lock_path):
        conn.execute("BEGIN IMMEDIATE;")
        try:
            written = write_relations_for_scope(
                conn,
                edges_to_write,
                grade_subject=grade_subject,
                target_uids=resolved_target_set,
            )
            for uid in uids_to_mark_ok:
                conn.execute(
                    """
                    INSERT INTO compile_state(question_uid, stage, status, input_hash, prompt_version, model_name, attempts, last_error, updated_at)
                    VALUES (?, 'S4', 'OK', ?, ?, 'rule_matrix+refine', 1, '', datetime('now'))
                    ON CONFLICT(question_uid, stage) DO UPDATE SET
                        status = 'OK',
                        input_hash = excluded.input_hash,
                        prompt_version = excluded.prompt_version,
                        model_name = excluded.model_name,
                        attempts = compile_state.attempts + 1,
                        last_error = '',
                        updated_at = datetime('now');
                    """,
                    (uid, s4_hash, settings.topology.rule_version),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    if mode == "import" and manifest_file.is_file():
        try:
            m_post = json.loads(manifest_file.read_text(encoding="utf-8"))
            if isinstance(m_post, dict) and m_post.get("status") != "IMPORTED":
                m_post["status"] = "IMPORTED"
                m_post["imported_at"] = datetime.now(UTC).isoformat()
                tmp_mf = manifest_file.with_name(f".{manifest_file.name}.{os.getpid()}.tmp")
                try:
                    tmp_mf.write_text(
                        json.dumps(m_post, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    os.replace(tmp_mf, manifest_file)
                except BaseException:
                    tmp_mf.unlink(missing_ok=True)
                    raise
        except Exception:
            pass

    counts_by_type: dict[str, int] = {rt: 0 for rt in RELATION_TYPES}
    for e in final_edges:
        counts_by_type[e.relation_type] = counts_by_type.get(e.relation_type, 0) + 1

    return {
        "mode": mode,
        "grade_subject": grade_subject,
        "eligible_questions": len(uids_to_mark_ok),
        "blocked_s3d": blocked_s3d,
        "blocked_unservable": blocked_unservable,
        "raw_candidates": len(raw_candidates),
        "raw_refine_candidates": len(raw_refine_candidates),
        "refine_candidates": len(refine_candidates),
        "written_edges": written,
        "counts_by_type": counts_by_type,
        "meta_version": meta_ver,
        "quantiles_rebuilt": quantiles_rebuilt,
        "target_uids": sorted(resolved_target_set) if resolved_target_set is not None else None,
    }


def evaluate_calibration_pairs(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    stress_yaml_path: Path | str = Path("config/stress_test_25.yaml"),
    propose_candidates: bool = False,
    per_type_samples: int = 30,
    vectors_dir: Path | str = Path("data/vectors"),
    reports_dir: Path | str = Path("data/reports"),
) -> dict[str, Any]:
    """评估 stress_test_25.yaml 校准对命中与误报率，或抽样约 150 对五边候选供人工校准阈值。"""
    y_path = Path(stress_yaml_path)
    if not y_path.exists():
        raise PreconditionError(f"PRECONDITION_FAILED: 未找到校准配置 {y_path}")
    cfg_data = yaml.safe_load(y_path.read_text(encoding="utf-8")) or {}
    grade_subject = str(cfg_data.get("grade_subject") or "小学奥数")

    gs_row = conn.execute(
        "SELECT slug FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科不存在: {grade_subject!r}")
    slug = str(gs_row["slug"])
    store = VectorStore(vectors_dir)

    if propose_candidates:
        features = load_question_features(conn, grade_subject=grade_subject, include_unservable=False)
        raw_edges = generate_candidate_edges(
            conn,
            store,
            grade_subject=grade_subject,
            scope_slug=slug,
            topology_cfg=settings.topology,
            features=features,
            include_unservable=False,
        )
        # 按边类型分桶抽样（混合高分命中、边界带样本与现有库内关系）
        samples: dict[str, list[dict[str, Any]]] = {}
        by_type: dict[str, list[CandidateEdge]] = {rt: [] for rt in RELATION_TYPES}
        for e in raw_edges:
            by_type.setdefault(e.relation_type, []).append(e)

        total_exported = 0
        for rt in RELATION_TYPES:
            bucket = by_type.get(rt, [])
            boundary = [x for x in bucket if x.needs_llm_refine]
            strong = [x for x in bucket if not x.needs_llm_refine]
            strong_sorted = sorted(strong, key=lambda x: -x.rank_score)
            boundary_sorted = sorted(boundary, key=lambda x: -x.rank_score)
            take_b = min(len(boundary_sorted), per_type_samples // 2)
            take_s = min(len(strong_sorted), per_type_samples - take_b)
            chosen = strong_sorted[:take_s] + boundary_sorted[:take_b]
            if len(chosen) < per_type_samples:
                rem = [x for x in (strong_sorted[take_s:] + boundary_sorted[take_b:])]
                chosen.extend(rem[: per_type_samples - len(chosen)])

            out_list: list[dict[str, Any]] = []
            for e in chosen:
                fa = features[e.source_uid]
                fb = features[e.target_uid]
                out_list.append(
                    {
                        "source_uid": e.source_uid,
                        "target_uid": e.target_uid,
                        "expected_relation": rt,
                        "label": "UNLABELED",
                        "is_boundary": e.needs_llm_refine,
                        "metrics": {
                            "surface_sim": round(e.surface_sim, 4),
                            "skeleton_sim": round(e.skeleton_sim, 4),
                            "surface_pct": round(e.surface_pct, 4),
                            "skeleton_pct": round(e.skeleton_pct, 4),
                            "k_jaccard": round(e.k_jaccard, 4),
                            "containment_score": round(e.containment_score, 4),
                            "trap_overlap": round(e.trap_overlap, 4),
                        },
                        "source_preview": fa.eff_text[:220],
                        "target_preview": fb.eff_text[:220],
                        "source_elo": fa.elo,
                        "target_elo": fb.elo,
                    }
                )
            samples[rt] = out_list
            total_exported += len(out_list)

        out_dir = Path(reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        yaml_out = out_dir / f"calibration_candidates_{slug}.yaml"
        yaml_out.write_text(
            yaml.safe_dump(
                {"grade_subject": grade_subject, "total_candidates": total_exported, "samples_by_type": samples},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return {
            "propose_candidates": True,
            "grade_subject": grade_subject,
            "total_candidates": total_exported,
            "counts_by_type": {rt: len(samples[rt]) for rt in RELATION_TYPES},
            "output_file": str(yaml_out),
        }

    # 评估 calibration_pairs 命中率与误报率
    cal_pairs = cfg_data.get("calibration_pairs") or []
    evaluated: list[dict[str, Any]] = []
    pos_total = 0
    pos_hits = 0
    neg_total = 0
    neg_fp = 0

    for item in cal_pairs:
        src = str(item["source_uid"])
        dst = str(item["target_uid"])
        exp_rt = str(item["expected_relation"])
        lbl = str(item.get("label") or "POSITIVE").upper()
        u, v = normalize_edge_endpoints(exp_rt, src, dst)
        row = conn.execute(
            """
            SELECT relation_type, confidence_source, surface_sim, skeleton_sim, surface_pct, skeleton_pct
            FROM question_relations
            WHERE source_question_uid = ? AND target_question_uid = ? AND relation_type = ?;
            """,
            (u, v, exp_rt),
        ).fetchone()
        hit = row is not None
        if lbl == "POSITIVE":
            pos_total += 1
            if hit:
                pos_hits += 1
        elif lbl == "NEGATIVE":
            neg_total += 1
            if hit:
                neg_fp += 1
        evaluated.append(
            {
                "source_uid": u,
                "target_uid": v,
                "expected_relation": exp_rt,
                "label": lbl,
                "matched": hit,
                "note": item.get("note", ""),
            }
        )

    return {
        "propose_candidates": False,
        "grade_subject": grade_subject,
        "total_pairs": len(cal_pairs),
        "positive_total": pos_total,
        "positive_hits": pos_hits,
        "recall": round(pos_hits / pos_total, 4) if pos_total else None,
        "negative_total": neg_total,
        "negative_false_positives": neg_fp,
        "false_positive_rate": round(neg_fp / neg_total, 4) if neg_total else None,
        "details": evaluated,
    }
