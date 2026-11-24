"""Stage 4 流水线：向量与冻结分位表重建、五边拓扑织网、LLM 精裁与校准抽样。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Literal

import yaml

from shuati.config import Settings, resolve_subagent_model
from shuati.core.topology import (
    RELATION_TYPES,
    CandidateEdge,
    QuestionFeature,
    apply_degree_caps,
    generate_candidate_edges,
    load_question_features,
    merge_relation_overrides,
    normalize_edge_endpoints,
    write_relations_for_scope,
)
from shuati.db.connection import CompileLock, PreconditionError
from shuati.llm.embeddings import EmbeddingClient
from shuati.llm.gateway import LLMGateway
from shuati.llm.prompt_builder import build_stage4_refine_messages
from shuati.llm.seam_loader import load_prompt_seams
from shuati.models.schemas import Stage4RefineBatchOutput, Stage4RefineItem
from shuati.pipeline.subagent_bridge import export_task_batches, load_and_validate_results
from shuati.vectors.store import VectorStore


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
            slug = str(gsr["slug"])
            quantile_stats[slug] = store.rebuild_quantiles(slug, ("surface", "skeleton"))

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
) -> dict[str, Any]:
    """Stage 4 四通道五边拓扑织网（规则矩阵 + 边界带/CONTRASTIVE 精裁 + 度数截断 + relation_overrides 合并）。"""
    gs_row = conn.execute(
        "SELECT grade_subject, slug FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科不存在: {grade_subject!r}")
    slug = str(gs_row["slug"])

    # 1. 更新 compile_state 门禁：S3D != 'OK' 或默认不可服务题目置 BLOCKED
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
        if r["s3d_status"] != "OK":
            _mark_s4_blocked(conn, uid, "上游 S3D 非 OK")
            blocked_s3d += 1
        elif not include_unservable and int(r["eff_is_servable"]) != 1:
            _mark_s4_blocked(conn, uid, "eff_is_servable = 0 (NEEDS_REVIEW/EXCLUDED)")
            blocked_unservable += 1
        else:
            eligible_uids.append(uid)
    conn.commit()

    store = VectorStore(vectors_dir)
    # 2. 差量补齐向量与冻结分位表（保证 meta.json 就绪）
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

        if not store.has_quantiles(slug, "surface") or not store.has_quantiles(slug, "skeleton"):
            store.rebuild_quantiles(slug, ("surface", "skeleton"))

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
        include_unservable=include_unservable,
    )

    auto_edges: list[CandidateEdge] = []
    refine_candidates: list[CandidateEdge] = []
    use_refine = bool(settings.pipeline.enable_stage4_llm_refine)
    for e in raw_candidates:
        if use_refine and e.needs_llm_refine:
            refine_candidates.append(e)
        else:
            auto_edges.append(e)

    # 3. 处理二级 LLM 精裁（api / export / import）
    verified_refine_edges: list[CandidateEdge] = []
    if mode == "export":
        eff_bs = max(1, int(batch_size))
        batches: list[dict[str, Any]] = []
        task_map: dict[str, Any] = {
            "grade_subject": grade_subject,
            "include_unservable": include_unservable,
            "pairs": {},
        }
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
        return {
            "mode": "export",
            "grade_subject": grade_subject,
            "eligible_questions": len(eligible_uids),
            "auto_candidates": len(auto_edges),
            "refine_candidates": len(refine_candidates),
            "manifest": manifest,
        }

    errs: list[dict[str, Any]] = []
    if mode == "import":
        if results_path is None:
            raise PreconditionError("PRECONDITION_FAILED: --mode import 必须提供 --results")
        task_map_file = Path(tasks_dir) / "stage4_task_map.json"
        valid_by_batch, loaded_map, errs = load_and_validate_results(
            "stage4", results_path, task_map_file, Stage4RefineBatchOutput
        )
        if errs:
            raise PreconditionError(f"PRECONDITION_FAILED: Stage 4 结果校验失败 {len(errs)} 批，详见 retry_tasks.jsonl")
        pairs_map = loaded_map.get("pairs", {})
        edge_by_key = {
            (normalize_edge_endpoints(e.relation_type, e.source_uid, e.target_uid), e.relation_type): e
            for e in refine_candidates
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
            if verdict is not None and verdict[0]:
                cand.confidence_source = "LLM_VERIFIED"
                cand.pedagogical_note = verdict[1]
                verified_refine_edges.append(cand)
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

    # 4. 度数上限截断 + 合并 relation_overrides + 落库
    capped = apply_degree_caps([*auto_edges, *verified_refine_edges], settings.topology)
    final_edges = merge_relation_overrides(
        conn,
        capped,
        features=features,
        vector_store=store,
        scope_slug=slug,
        topology_cfg=settings.topology,
    )

    meta_ver = store.load_meta().get("meta_version", 0)
    s4_hash = f"rule={settings.topology.rule_version}|meta={meta_ver}|unservable={int(include_unservable)}"

    with CompileLock(lock_path):
        conn.execute("BEGIN IMMEDIATE;")
        try:
            written = write_relations_for_scope(conn, final_edges, grade_subject=grade_subject)
            for uid in eligible_uids:
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

    counts_by_type: dict[str, int] = {rt: 0 for rt in RELATION_TYPES}
    for e in final_edges:
        counts_by_type[e.relation_type] = counts_by_type.get(e.relation_type, 0) + 1

    return {
        "mode": mode,
        "grade_subject": grade_subject,
        "eligible_questions": len(eligible_uids),
        "blocked_s3d": blocked_s3d,
        "blocked_unservable": blocked_unservable,
        "raw_candidates": len(raw_candidates),
        "refine_candidates": len(refine_candidates),
        "written_edges": written,
        "counts_by_type": counts_by_type,
        "meta_version": meta_ver,
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
