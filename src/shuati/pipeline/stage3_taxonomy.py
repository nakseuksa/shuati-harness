"""Stage 3 流水线：Stage 3a 词表涌现、Stage 3c 词表回填、Stage 3d L3挂载与归并、Stage 3e 先修/共现边。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import numpy as np

from shuati.config import Settings
from shuati.core import effective, refresh
from shuati.core.knowledge_graph import (
    apply_merge_pairs,
    check_dag_and_break_cycles,
    recall_concept_merge_candidates,
    recall_l3_merge_candidates,
)
from shuati.core.taxonomy import (
    TaxonomyL1Entry,
    TaxonomyL2Entry,
    TaxonomyLookup,
    TaxonomySpec,
    build_lookup_index,
    default_taxonomy_path,
    format_taxonomy_enum,
    load_taxonomy_spec,
    make_node_id,
    normalize_label,
    save_taxonomy_spec,
    transition_taxonomy,
)
from shuati.db.connection import CompileLock, PreconditionError
from shuati.llm.embeddings import EmbeddingClient
from shuati.llm.gateway import LLMGateway
from shuati.llm.prompt_builder import (
    build_merge_judge_messages,
    build_prerequisite_dag_messages,
    build_stage3c_mapping_messages,
    build_taxonomy_propose_messages,
)
from shuati.models.schemas import (
    MergeJudgeBatchOutput,
    MergeJudgeItem,
    PrerequisiteDagOutput,
    Stage3cBatchMappingOutput,
    Stage3cQuestionMapping,
    TaxonomyProposeOutput,
)
from shuati.pipeline.subagent_bridge import export_task_batches, load_and_validate_results
from shuati.vectors.store import VectorStore

STAGE3C_PROMPT_VERSION = "s3c_v1"
STAGE3D_PROMPT_VERSION = "s3d_v1"


def _sha256_json(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _weighted_spherical_kmeans(
    vecs: np.ndarray,
    weights: np.ndarray,
    k: int,
    *,
    iters: int = 8,
) -> np.ndarray:
    """频次加权球面 K-Means 聚类，返回每条样本的簇编号 [0..k-1]。"""
    n = vecs.shape[0]
    if n <= k:
        return np.arange(n, dtype=np.int64)

    # 选最高频样本为第一个中心，其余通过最小化最大余弦相似度实现多样化初始化
    order = np.argsort(-weights)
    centers_idx = [int(order[0])]
    max_sim = vecs @ vecs[centers_idx[0]]
    for _ in range(1, k):
        # 优先在尚未足够分散的高频候选里选最远点
        score = max_sim - 0.05 * np.log1p(weights)
        nxt = int(np.argmin(score))
        centers_idx.append(nxt)
        sim_new = vecs @ vecs[nxt]
        max_sim = np.maximum(max_sim, sim_new)

    centers = vecs[centers_idx].copy()
    labels = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        sims = vecs @ centers.T  # (n, k)
        labels = np.argmax(sims, axis=1)
        new_centers = np.zeros_like(centers)
        for cid in range(k):
            mask = labels == cid
            if np.any(mask):
                w = weights[mask][:, None]
                c = np.sum(vecs[mask] * w, axis=0)
                norm = float(np.linalg.norm(c))
                new_centers[cid] = (c / norm) if norm > 1e-12 else centers[cid]
            else:
                new_centers[cid] = centers[cid]
        centers = new_centers
    return labels


def _next_draft_version(current_version: str) -> str:
    if current_version.startswith("draft-"):
        tail = current_version.split("draft-", 1)[1]
        if tail.isdigit():
            return f"draft-{int(tail) + 1}"
    return "draft-1"


async def propose_taxonomy_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str,
    mode: Literal["api", "export", "import"] = "api",
    tasks_dir: Path | str = Path("data/unified/stage3_4/propose"),
    results_path: Path | str | None = None,
    config_dir: Path | str = Path("config"),
    use_mock: bool = False,
    n_clusters: int | None = None,
) -> dict[str, Any]:
    """Stage 3a：对全量 S2='OK' 有效题的 (raw_l1, raw_l2) Embedding 聚类并涌现标准词表草案。"""
    gs_row = conn.execute(
        "SELECT grade_subject, slug, taxonomy_status, taxonomy_version FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科不存在: {grade_subject!r}")

    slug = str(gs_row["slug"])
    cur_status = str(gs_row["taxonomy_status"])
    cur_version = str(gs_row["taxonomy_version"] or "")
    draft_ver = "draft-1" if cur_status == "NONE" else _next_draft_version(cur_version)

    rows = conn.execute(
        """
        SELECT s.raw_l1, s.raw_l2, COUNT(*) AS cnt
        FROM question_steps s
        JOIN questions q ON q.question_uid = s.question_uid
        JOIN compile_state cs ON cs.question_uid = q.question_uid AND cs.stage = 'S2'
        WHERE q.grade_subject = ?
          AND q.is_retired = 0
          AND cs.status = 'OK'
          AND (s.raw_l1 != '' OR s.raw_l2 != '')
        GROUP BY s.raw_l1, s.raw_l2
        ORDER BY cnt DESC, s.raw_l1, s.raw_l2;
        """,
        (grade_subject,),
    ).fetchall()
    if not rows:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段 {grade_subject!r} 暂无可用的 S2='OK' 步骤标签")

    pair_records: list[dict[str, Any]] = [
        {"raw_l1": str(r["raw_l1"]), "raw_l2": str(r["raw_l2"]), "cnt": int(r["cnt"])}
        for r in rows
    ]

    if mode == "import":
        if results_path is None:
            raise PreconditionError("PRECONDITION_FAILED: --mode import 必须提供 --results")
        task_map_file = Path(tasks_dir) / "propose_task_map.json"
        valid_by_batch, _, errs = load_and_validate_results(
            "propose", results_path, task_map_file, TaxonomyProposeOutput
        )
        if errs:
            raise PreconditionError(f"PRECONDITION_FAILED: Stage 3a 结果存在 {len(errs)} 批校验失败，详见 retry_tasks.jsonl")
        if not valid_by_batch:
            raise PreconditionError("PRECONDITION_FAILED: 未读取到有效 TaxonomyProposeOutput")
        proposed: TaxonomyProposeOutput = next(iter(valid_by_batch.values()))
    else:
        pair_texts = [f"{r['raw_l1']} :: {r['raw_l2']}" for r in pair_records]
        embed_client = EmbeddingClient(settings, conn=conn, use_mock=use_mock)
        try:
            vecs = await embed_client.embed_texts(pair_texts, stage="STAGE3A_EMBED")
        finally:
            await embed_client.aclose()

        n_pairs = len(pair_records)
        if n_clusters is not None:
            k_target = max(1, min(n_pairs, int(n_clusters)))
        else:
            k_target = min(max(60, n_pairs // 40), 100) if n_pairs >= 60 else min(n_pairs, 30)

        weights = np.asarray([float(int(r["cnt"])) for r in pair_records], dtype=np.float32)
        labels = _weighted_spherical_kmeans(vecs, weights, k_target)

        cluster_buckets: dict[int, list[dict[str, Any]]] = {}
        for idx, cid in enumerate(labels.tolist()):
            cluster_buckets.setdefault(cid, []).append(pair_records[idx])

        clusters_summary: list[dict[str, Any]] = []
        for cid, items in cluster_buckets.items():
            sorted_items = sorted(items, key=lambda x: (-x["cnt"], x["raw_l1"], x["raw_l2"]))
            l1_counts: Counter[str] = Counter()
            for it in sorted_items:
                if it["raw_l1"]:
                    l1_counts[it["raw_l1"]] += int(it["cnt"])
            clusters_summary.append(
                {
                    "cluster_id": int(cid),
                    "total_freq": sum(int(x["cnt"]) for x in sorted_items),
                    "distinct_pairs": len(sorted_items),
                    "representative": f"{sorted_items[0]['raw_l1']} / {sorted_items[0]['raw_l2']}",
                    "associated_l1": [k for k, _ in l1_counts.most_common(3)],
                    "top_examples": [
                        f"{x['raw_l1']} / {x['raw_l2']} ({x['cnt']})" for x in sorted_items[:6]
                    ],
                }
            )
        clusters_summary.sort(key=lambda x: (-x["total_freq"], -x["distinct_pairs"]))
        top_pairs = pair_records[:100]
        messages = build_taxonomy_propose_messages(
            grade_subject=grade_subject,
            clusters_summary=clusters_summary,
            top_pairs=top_pairs,
        )

        if mode == "export":
            manifest = export_task_batches(
                "propose",
                [
                    {
                        "batch_id": "propose_batch_0001",
                        "messages": messages,
                        "task_item_ids": ["propose_0001"],
                    }
                ],
                {"grade_subject": grade_subject, "draft_version": draft_ver, "distinct_pairs": n_pairs},
                tasks_dir,
                schema_cls=TaxonomyProposeOutput,
            )
            return {
                "mode": "export",
                "grade_subject": grade_subject,
                "distinct_pairs": n_pairs,
                "clusters": len(clusters_summary),
                "manifest": manifest,
            }

        gateway = LLMGateway(settings, conn=conn)
        try:
            res = await gateway.call_structured(
                slot="text_llm",
                stage="STAGE3A_PROPOSE",
                messages=messages,
                schema=TaxonomyProposeOutput,
                prompt_version="s3a_v1",
            )
            proposed = res.parsed
        finally:
            await gateway.aclose()

    # 去重规范化构造 TaxonomySpec
    seen_l1: set[str] = set()
    clean_modules: list[TaxonomyL1Entry] = []
    for mod in proposed.modules:
        k1 = normalize_label(mod.name)
        if not k1 or k1 in seen_l1:
            continue
        seen_l1.add(k1)
        seen_l2: set[str] = set()
        clean_l2: list[TaxonomyL2Entry] = []
        for l2 in mod.l2_entities:
            k2 = normalize_label(l2.name)
            if not k2 or k2 in seen_l2:
                continue
            seen_l2.add(k2)
            clean_l2.append(
                TaxonomyL2Entry(
                    name=l2.name.strip(),
                    aliases=list(dict.fromkeys(a.strip() for a in l2.aliases if a.strip() and a.strip() != l2.name.strip())),
                    description=l2.description.strip(),
                )
            )
        if clean_l2:
            clean_modules.append(
                TaxonomyL1Entry(
                    name=mod.name.strip(),
                    aliases=list(dict.fromkeys(a.strip() for a in mod.aliases if a.strip() and a.strip() != mod.name.strip())),
                    l2_entities=clean_l2,
                )
            )

    spec = TaxonomySpec(
        schema_version=1,
        grade_subject=grade_subject,
        slug=slug,
        status="DRAFT",
        version=draft_ver,
        modules=clean_modules,
    )
    draft_path = default_taxonomy_path(config_dir, slug, "DRAFT")
    save_taxonomy_spec(spec, draft_path)

    if cur_status == "NONE":
        with conn:
            for mod_entry in spec.modules:
                for l2_entry in mod_entry.l2_entities:
                    l2_id = make_node_id(slug, draft_ver, "L2", mod_entry.name, l2_entry.name, "")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_nodes(
                            node_id, grade_subject, taxonomy_version, node_level,
                            l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                        )
                        VALUES (?, ?, ?, 'L2', ?, ?, '', ?, NULL);
                        """,
                        (l2_id, grade_subject, draft_ver, mod_entry.name, l2_entry.name, json.dumps(l2_entry.aliases, ensure_ascii=False)),
                    )
            conn.execute(
                """
                UPDATE grade_subjects
                SET taxonomy_status = 'DRAFT', taxonomy_version = ?, taxonomy_file = ?
                WHERE grade_subject = ?;
                """,
                (draft_ver, str(draft_path), grade_subject),
            )
    else:
        transition_taxonomy(
            conn,
            grade_subject=grade_subject,
            yaml_path=draft_path,
            target_version=draft_ver,
            target_status="DRAFT",
            allow_stale_overrides=True,
            config_dir=config_dir,
        )

    return {
        "mode": mode,
        "grade_subject": grade_subject,
        "taxonomy_status": "DRAFT",
        "taxonomy_version": draft_ver,
        "taxonomy_file": str(draft_path),
        "l1_count": len(spec.modules),
        "l2_count": sum(len(m.l2_entities) for m in spec.modules),
    }


def _load_active_taxonomy(conn: sqlite3.Connection, grade_subject: str) -> tuple[str, str, TaxonomySpec, TaxonomyLookup]:
    gs_row = conn.execute(
        "SELECT slug, taxonomy_status, taxonomy_version, taxonomy_file FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科不存在: {grade_subject!r}")
    status = str(gs_row["taxonomy_status"])
    if status not in ("DRAFT", "LOCKED") or not gs_row["taxonomy_file"]:
        raise PreconditionError(
            f"PRECONDITION_FAILED: 学段 {grade_subject!r} 尚未准备词表 (taxonomy_status={status!r})，请先执行 `shuati taxonomy propose`。"
        )
    spec = load_taxonomy_spec(str(gs_row["taxonomy_file"]))
    return str(gs_row["slug"]), str(gs_row["taxonomy_version"]), spec, build_lookup_index(spec)


def _best_fallback_l2(lookup: TaxonomyLookup, l1_name: str, candidate_text: str) -> str:
    l2s = lookup.l2_names(l1_name)
    if not l2s:
        return candidate_text or "综合应用"
    cand_set = set(candidate_text.strip())
    return max(l2s, key=lambda x: (len(set(x) & cand_set), -abs(len(x) - len(candidate_text))))


def _persist_stage3c_question(
    conn: sqlite3.Connection,
    *,
    uid: str,
    grade_subject: str,
    slug: str,
    taxonomy_version: str,
    taxonomy_status: str,
    lookup: TaxonomyLookup,
    s3c_hash: str,
    model_name: str,
    primary_l1: str,
    secondary_l1s: list[str],
    step_mappings: list[dict[str, Any]],
) -> int:
    """持久化单题 S3C 映射（保护 raw_*，记录新型 L2 候选，刷新生效层与下游 STALE）。"""
    novel_added = 0
    l1_choices = lookup.l1_names()
    resolved_prim = lookup.resolve_l1(primary_l1) or (l1_choices[0] if l1_choices else primary_l1)

    clean_sec: list[str] = []
    for s in secondary_l1s:
        rl1 = lookup.resolve_l1(s)
        if rl1 and rl1 != resolved_prim and rl1 not in clean_sec:
            clean_sec.append(rl1)

    for st in step_mappings:
        step_id = str(st["step_id"])
        raw_ml1 = str(st["mapped_l1"])
        raw_ml2 = str(st["mapped_l2"])
        is_novel = bool(st.get("is_novel_l2", False))

        pair_hit = lookup.resolve_l2(raw_ml1, raw_ml2)
        if pair_hit is not None:
            fin_l1, fin_l2 = pair_hit
        else:
            fin_l1 = lookup.resolve_l1(raw_ml1) or resolved_prim
            fin_l2 = _best_fallback_l2(lookup, fin_l1, raw_ml2)
            if is_novel or raw_ml2.strip():
                proposed = raw_ml2.strip() or fin_l2
                existing_cand = conn.execute(
                    """
                    SELECT candidate_id, example_question_uids_json
                    FROM taxonomy_candidates
                    WHERE grade_subject = ? AND base_taxonomy_version = ? AND level = 'L2'
                      AND parent_l1 = ? AND proposed_name = ? AND status = 'PENDING';
                    """,
                    (grade_subject, taxonomy_version, fin_l1, proposed),
                ).fetchone()
                if existing_cand is not None:
                    ex_uids = json.loads(existing_cand["example_question_uids_json"] or "[]")
                    if uid not in ex_uids and len(ex_uids) < 10:
                        ex_uids.append(uid)
                        conn.execute(
                            "UPDATE taxonomy_candidates SET example_question_uids_json = ? WHERE candidate_id = ?;",
                            (json.dumps(ex_uids, ensure_ascii=False), existing_cand["candidate_id"]),
                        )
                else:
                    conn.execute(
                        """
                        INSERT INTO taxonomy_candidates(
                            grade_subject, base_taxonomy_version, level, parent_l1,
                            proposed_name, example_question_uids_json, status
                        )
                        VALUES (?, ?, 'L2', ?, ?, ?, 'PENDING');
                        """,
                        (grade_subject, taxonomy_version, fin_l1, proposed, json.dumps([uid], ensure_ascii=False)),
                    )
                    novel_added += 1

        # 确保 L2 汇总节点存在
        l2_id = make_node_id(slug, taxonomy_version, "L2", fin_l1, fin_l2, "")
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_nodes(
                node_id, grade_subject, taxonomy_version, node_level,
                l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
            )
            VALUES (?, ?, ?, 'L2', ?, ?, '', '[]', NULL);
            """,
            (l2_id, grade_subject, taxonomy_version, fin_l1, fin_l2),
        )
        conn.execute(
            "UPDATE question_steps SET m_l1 = ?, m_l2 = ?, m_knowledge_node_id = NULL WHERE step_id = ?;",
            (fin_l1, fin_l2, step_id),
        )

    conn.execute(
        """
        UPDATE questions
        SET m_primary_l1_module = ?, m_secondary_l1_modules_json = ?
        WHERE question_uid = ?;
        """,
        (resolved_prim, json.dumps(clean_sec, ensure_ascii=False), uid),
    )
    conn.execute(
        """
        INSERT INTO compile_state(question_uid, stage, status, input_hash, prompt_version, model_name, attempts, last_error, updated_at)
        VALUES (?, 'S3C', 'OK', ?, ?, ?, 1, '', datetime('now'))
        ON CONFLICT(question_uid, stage) DO UPDATE SET
            status = 'OK',
            input_hash = excluded.input_hash,
            prompt_version = excluded.prompt_version,
            model_name = excluded.model_name,
            attempts = compile_state.attempts + 1,
            last_error = '',
            updated_at = datetime('now');
        """,
        (uid, s3c_hash, STAGE3C_PROMPT_VERSION, model_name),
    )
    refresh.mark_stages_stale(conn, uid, ["S3D", "S4"])
    effective.recompute(conn, uid)
    return novel_added


async def run_stage3c_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str,
    mode: Literal["api", "export", "import"] = "api",
    batch_size: int = 20,
    tasks_dir: Path | str = Path("data/unified/stage3_4/3c"),
    results_path: Path | str | None = None,
    lock_path: Path | str = Path("data/.compile.lock"),
) -> dict[str, Any]:
    """Stage 3c：零成本精确/别名命中 + 脱敏逐题上下文批映射。"""
    slug, tax_ver, spec, lookup = _load_active_taxonomy(conn, grade_subject)
    taxonomy_enum = format_taxonomy_enum(spec)

    q_rows = conn.execute(
        """
        SELECT q.question_uid, q.m_abstract_skeleton, q.m_primary_l1_module, q.raw_l1_candidates_json,
               cs2.status AS s2_status,
               cs3.status AS s3c_status, cs3.input_hash AS s3c_hash
        FROM questions q
        LEFT JOIN compile_state cs2 ON cs2.question_uid = q.question_uid AND cs2.stage = 'S2'
        LEFT JOIN compile_state cs3 ON cs3.question_uid = q.question_uid AND cs3.stage = 'S3C'
        WHERE q.grade_subject = ? AND q.is_retired = 0
        ORDER BY q.question_uid;
        """,
        (grade_subject,),
    ).fetchall()

    steps_by_q: dict[str, list[sqlite3.Row]] = {}
    for sr in conn.execute(
        """
        SELECT s.step_id, s.question_uid, s.branch_id, s.step_index, s.sub_goal, s.raw_l1, s.raw_l2
        FROM question_steps s
        JOIN questions q ON q.question_uid = s.question_uid
        WHERE q.grade_subject = ?
        ORDER BY s.question_uid, s.branch_id, s.step_index;
        """,
        (grade_subject,),
    ).fetchall():
        steps_by_q.setdefault(str(sr["question_uid"]), []).append(sr)

    blocked_uids: list[str] = []
    skipped = 0
    exact_hits: list[dict[str, Any]] = []
    llm_needed: list[dict[str, Any]] = []
    hash_by_uid: dict[str, str] = {}

    for qr in q_rows:
        uid = str(qr["question_uid"])
        if qr["s2_status"] != "OK":
            blocked_uids.append(uid)
            continue

        q_steps = steps_by_q.get(uid, [])
        s3c_hash = _sha256_json(
            {
                "pv": STAGE3C_PROMPT_VERSION,
                "tv": tax_ver,
                "prim": qr["m_primary_l1_module"],
                "raw_l1s": qr["raw_l1_candidates_json"],
                "steps": [
                    (str(s["step_id"]), str(s["raw_l1"]), str(s["raw_l2"]), str(s["sub_goal"]))
                    for s in q_steps
                ],
            }
        )
        hash_by_uid[uid] = s3c_hash

        if qr["s3c_status"] == "OK" and qr["s3c_hash"] == s3c_hash:
            skipped += 1
            continue

        # 尝试零成本全步精确/别名命中
        all_resolved = True
        resolved_steps: list[dict[str, Any]] = []
        l1_counter: Counter[str] = Counter()
        for s in q_steps:
            hit = lookup.resolve_l2(str(s["raw_l1"]), str(s["raw_l2"]))
            if hit is None:
                all_resolved = False
                break
            l1_counter[hit[0]] += 1
            resolved_steps.append(
                {
                    "step_id": str(s["step_id"]),
                    "step_index": int(s["step_index"]),
                    "mapped_l1": hit[0],
                    "mapped_l2": hit[1],
                    "is_novel_l2": False,
                }
            )

        prim_hit = lookup.resolve_l1(str(qr["m_primary_l1_module"]))
        if all_resolved and (prim_hit is not None or l1_counter):
            prim_final = prim_hit or l1_counter.most_common(1)[0][0]
            sec_final = [l1 for l1, _ in l1_counter.most_common() if l1 != prim_final]
            exact_hits.append(
                {
                    "uid": uid,
                    "s3c_hash": s3c_hash,
                    "primary_l1": prim_final,
                    "secondary_l1s": sec_final,
                    "steps": resolved_steps,
                }
            )
        else:
            llm_needed.append(
                {
                    "uid": uid,
                    "s3c_hash": s3c_hash,
                    "abstract_skeleton": str(qr["m_abstract_skeleton"] or ""),
                    "raw_primary_l1": str(qr["m_primary_l1_module"] or ""),
                    "steps": q_steps,
                }
            )

    blocked = len(blocked_uids)
    novel_candidates = 0
    updated = 0

    with CompileLock(lock_path):
        # 1. 先落库阻断状态与零成本命中项（幂等可续跑）
        if blocked_uids or exact_hits:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for b_uid in blocked_uids:
                    conn.execute(
                        """
                        INSERT INTO compile_state(question_uid, stage, status, last_error, updated_at)
                        VALUES (?, 'S3C', 'BLOCKED', '上游 S2 非 OK', datetime('now'))
                        ON CONFLICT(question_uid, stage) DO UPDATE SET
                            status = 'BLOCKED', last_error = '上游 S2 非 OK', updated_at = datetime('now');
                        """,
                        (b_uid,),
                    )
                for eh in exact_hits:
                    novel_candidates += _persist_stage3c_question(
                        conn,
                        uid=eh["uid"],
                        grade_subject=grade_subject,
                        slug=slug,
                        taxonomy_version=tax_ver,
                        taxonomy_status=spec.status,
                        lookup=lookup,
                        s3c_hash=eh["s3c_hash"],
                        model_name="exact_alias_lookup",
                        primary_l1=eh["primary_l1"],
                        secondary_l1s=eh["secondary_l1s"],
                        step_mappings=eh["steps"],
                    )
                    updated += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        if mode == "export":
            batches: list[dict[str, Any]] = []
            task_map: dict[str, Any] = {"items": {}, "grade_subject": grade_subject, "taxonomy_version": tax_ver}
            eff_bs = max(1, int(batch_size))
            for b_idx, start in enumerate(range(0, len(llm_needed), eff_bs), start=1):
                chunk = llm_needed[start : start + eff_bs]
                prompt_items: list[dict[str, Any]] = []
                item_ids: list[str] = []
                for j, qitem in enumerate(chunk, start=1):
                    tid = f"3c_{b_idx:04d}_{j:03d}"
                    item_ids.append(tid)
                    # 去重各分支步骤序号上下文同时记录完整映射
                    prompt_items.append(
                        {
                            "task_item_id": tid,
                            "abstract_skeleton": qitem["abstract_skeleton"],
                            "raw_primary_l1": qitem["raw_primary_l1"],
                            "steps": [
                                {
                                    "step_index": int(s["step_index"]),
                                    "raw_l1": str(s["raw_l1"]),
                                    "raw_l2": str(s["raw_l2"]),
                                    "sub_goal": str(s["sub_goal"]),
                                }
                                for s in qitem["steps"]
                            ],
                        }
                    )
                    task_map["items"][tid] = {
                        "question_uid": qitem["uid"],
                        "s3c_hash": qitem["s3c_hash"],
                        "steps": [
                            {
                                "step_id": str(s["step_id"]),
                                "step_index": int(s["step_index"]),
                                "raw_l1": str(s["raw_l1"]),
                                "raw_l2": str(s["raw_l2"]),
                            }
                            for s in qitem["steps"]
                        ],
                    }
                msgs = build_stage3c_mapping_messages(
                    grade_subject=grade_subject,
                    taxonomy_enum_text=taxonomy_enum,
                    question_items=prompt_items,
                )
                batches.append(
                    {
                        "batch_id": f"3c_batch_{b_idx:04d}",
                        "messages": msgs,
                        "task_item_ids": item_ids,
                    }
                )
            manifest = export_task_batches(
                "3c", batches, task_map, tasks_dir, schema_cls=Stage3cBatchMappingOutput
            )
            return {
                "mode": "export",
                "grade_subject": grade_subject,
                "blocked": blocked,
                "skipped": skipped,
                "exact_hits": len(exact_hits),
                "exported_questions": len(llm_needed),
                "manifest": manifest,
            }

        if mode == "import":
            if results_path is None:
                raise PreconditionError("PRECONDITION_FAILED: --mode import 必须提供 --results")
            task_map_file = Path(tasks_dir) / "3c_task_map.json"
            valid_by_batch, task_map, errs = load_and_validate_results(
                "3c", results_path, task_map_file, Stage3cBatchMappingOutput
            )
            items_meta = task_map.get("items", {})
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for batch_out in valid_by_batch.values():
                    for qmap in batch_out.items:
                        meta = items_meta.get(qmap.task_item_id)
                        if not meta:
                            continue
                        uid = str(meta["question_uid"])
                        if uid not in hash_by_uid:
                            continue
                        by_idx = {int(s.step_index): s for s in qmap.steps}
                        step_payloads = []
                        for orig in meta["steps"]:
                            sidx = int(orig["step_index"])
                            matched = by_idx.get(sidx)
                            if matched:
                                step_payloads.append(
                                    {
                                        "step_id": orig["step_id"],
                                        "mapped_l1": matched.mapped_l1,
                                        "mapped_l2": matched.mapped_l2,
                                        "is_novel_l2": matched.is_novel_l2,
                                    }
                                )
                            else:
                                step_payloads.append(
                                    {
                                        "step_id": orig["step_id"],
                                        "mapped_l1": qmap.primary_l1_module,
                                        "mapped_l2": str(orig.get("raw_l2") or ""),
                                        "is_novel_l2": False,
                                    }
                                )
                        novel_candidates += _persist_stage3c_question(
                            conn,
                            uid=uid,
                            grade_subject=grade_subject,
                            slug=slug,
                            taxonomy_version=tax_ver,
                            taxonomy_status=spec.status,
                            lookup=lookup,
                            s3c_hash=str(meta["s3c_hash"]),
                            model_name="subagent_import",
                            primary_l1=qmap.primary_l1_module,
                            secondary_l1s=qmap.secondary_l1_modules,
                            step_mappings=step_payloads,
                        )
                        updated += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return {
                "mode": "import",
                "grade_subject": grade_subject,
                "blocked": blocked,
                "skipped": skipped,
                "exact_hits": len(exact_hits),
                "updated": updated,
                "novel_candidates": novel_candidates,
                "batch_errors": len(errs),
            }

        # mode == 'api'
        if llm_needed:
            gateway = LLMGateway(settings, conn=conn)
            eff_bs = max(1, int(batch_size))
            chunks = [llm_needed[i : i + eff_bs] for i in range(0, len(llm_needed), eff_bs)]

            async def _run_batch(b_idx: int, chunk: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Stage3cBatchMappingOutput, str]:
                tid_map: dict[str, dict[str, Any]] = {}
                prompt_items: list[dict[str, Any]] = []
                for j, qitem in enumerate(chunk, start=1):
                    tid = f"3c_{b_idx:04d}_{j:03d}"
                    tid_map[tid] = qitem
                    prompt_items.append(
                        {
                            "task_item_id": tid,
                            "abstract_skeleton": qitem["abstract_skeleton"],
                            "raw_primary_l1": qitem["raw_primary_l1"],
                            "steps": [
                                {
                                    "step_index": int(s["step_index"]),
                                    "raw_l1": str(s["raw_l1"]),
                                    "raw_l2": str(s["raw_l2"]),
                                    "sub_goal": str(s["sub_goal"]),
                                }
                                for s in qitem["steps"]
                            ],
                        }
                    )
                msgs = build_stage3c_mapping_messages(
                    grade_subject=grade_subject,
                    taxonomy_enum_text=taxonomy_enum,
                    question_items=prompt_items,
                )
                res = await gateway.call_structured(
                    slot="text_llm",
                    stage="STAGE3C",
                    messages=msgs,
                    schema=Stage3cBatchMappingOutput,
                    prompt_version=STAGE3C_PROMPT_VERSION,
                )
                return chunk, res.parsed, res.model_name

            try:
                batch_results = await asyncio.gather(
                    *(_run_batch(b_idx, chunk) for b_idx, chunk in enumerate(chunks, start=1))
                )
            finally:
                await gateway.aclose()

            conn.execute("BEGIN IMMEDIATE;")
            try:
                for b_idx, (chunk, out_model, model_used) in enumerate(batch_results, start=1):
                    tid_lookup = {f"3c_{b_idx:04d}_{j:03d}": qitem for j, qitem in enumerate(chunk, start=1)}
                    resp_by_tid = {it.task_item_id: it for it in out_model.items}
                    for tid, qitem in tid_lookup.items():
                        qmap_resp: Stage3cQuestionMapping | None = resp_by_tid.get(tid)
                        if qmap_resp is not None:
                            by_idx = {int(s.step_index): s for s in qmap_resp.steps}
                            prim = qmap_resp.primary_l1_module
                            secs = qmap_resp.secondary_l1_modules
                        else:
                            # Mock 空响应或漏项时确定性兜底映射，保证流水线鲁棒推进
                            by_idx = {}
                            prim = qitem["raw_primary_l1"]
                            secs = []
                        step_payloads = []
                        for s in qitem["steps"]:
                            matched = by_idx.get(int(s["step_index"]))
                            if matched:
                                step_payloads.append(
                                    {
                                        "step_id": str(s["step_id"]),
                                        "mapped_l1": matched.mapped_l1,
                                        "mapped_l2": matched.mapped_l2,
                                        "is_novel_l2": matched.is_novel_l2,
                                    }
                                )
                            else:
                                hit = lookup.resolve_l2(str(s["raw_l1"]), str(s["raw_l2"]))
                                step_payloads.append(
                                    {
                                        "step_id": str(s["step_id"]),
                                        "mapped_l1": hit[0] if hit else (lookup.resolve_l1(prim) or lookup.l1_names()[0]),
                                        "mapped_l2": hit[1] if hit else str(s["raw_l2"] or ""),
                                        "is_novel_l2": bool(s["raw_l2"]) and hit is None,
                                    }
                                )
                        novel_candidates += _persist_stage3c_question(
                            conn,
                            uid=qitem["uid"],
                            grade_subject=grade_subject,
                            slug=slug,
                            taxonomy_version=tax_ver,
                            taxonomy_status=spec.status,
                            lookup=lookup,
                            s3c_hash=qitem["s3c_hash"],
                            model_name=model_used,
                            primary_l1=prim,
                            secondary_l1s=secs,
                            step_mappings=step_payloads,
                        )
                        updated += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    return {
        "mode": mode,
        "grade_subject": grade_subject,
        "blocked": blocked,
        "skipped": skipped,
        "exact_hits": len(exact_hits),
        "updated": updated,
        "novel_candidates": novel_candidates,
    }


def run_stage3d_mount(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    lock_path: Path | str = Path("data/.compile.lock"),
) -> dict[str, Any]:
    """Stage 3d 确定性步骤 L3 节点挂载（零 LLM 调用，写入 eff_node_ids_json 并触发 S4=STALE）。"""
    slug, tax_ver, _, lookup = _load_active_taxonomy(conn, grade_subject)
    default_l1 = lookup.l1_names()[0]
    default_l2 = lookup.l2_names(default_l1)[0]

    q_rows = conn.execute(
        """
        SELECT q.question_uid, q.m_primary_l1_module,
               cs3c.status AS s3c_status,
               cs3d.status AS s3d_status, cs3d.input_hash AS s3d_hash
        FROM questions q
        LEFT JOIN compile_state cs3c ON cs3c.question_uid = q.question_uid AND cs3c.stage = 'S3C'
        LEFT JOIN compile_state cs3d ON cs3d.question_uid = q.question_uid AND cs3d.stage = 'S3D'
        WHERE q.grade_subject = ? AND q.is_retired = 0
        ORDER BY q.question_uid;
        """,
        (grade_subject,),
    ).fetchall()

    steps_by_q: dict[str, list[sqlite3.Row]] = {}
    for sr in conn.execute(
        """
        SELECT s.step_id, s.question_uid, s.m_l1, s.m_l2, s.raw_l3, s.sub_goal
        FROM question_steps s
        JOIN questions q ON q.question_uid = s.question_uid
        WHERE q.grade_subject = ?
        ORDER BY s.question_uid, s.branch_id, s.step_index;
        """,
        (grade_subject,),
    ).fetchall():
        steps_by_q.setdefault(str(sr["question_uid"]), []).append(sr)

    ov_tax_by_q: dict[str, str] = {}
    for orow in conn.execute(
        """
        SELECT o.question_uid, o.override_value
        FROM human_overrides o
        JOIN questions q ON q.question_uid = o.question_uid
        WHERE q.grade_subject = ? AND o.field_name = 'taxonomy_override_json'
          AND o.is_active = 1 AND o.is_suspended = 0;
        """,
        (grade_subject,),
    ).fetchall():
        ov_tax_by_q[str(orow["question_uid"])] = str(orow["override_value"])

    # 构建当前同域 L3 精确/别名查找索引
    l3_index: dict[tuple[str, str, str], str] = {}
    for nr in conn.execute(
        """
        SELECT node_id, l1_module, l2_entity, l3_operator, aliases_json
        FROM knowledge_nodes
        WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3' AND redirect_to_id IS NULL;
        """,
        (grade_subject, tax_ver),
    ).fetchall():
        nid = str(nr["node_id"])
        l1m = str(nr["l1_module"])
        l2e = str(nr["l2_entity"])
        l3_index[(l1m, l2e, normalize_label(str(nr["l3_operator"])))] = nid
        for al in json.loads(nr["aliases_json"] or "[]"):
            k = normalize_label(str(al))
            if k:
                l3_index.setdefault((l1m, l2e, k), nid)

    blocked = 0
    skipped = 0
    mounted = 0
    created_nodes = 0

    with CompileLock(lock_path):
        conn.execute("BEGIN IMMEDIATE;")
        try:
            for qr in q_rows:
                uid = str(qr["question_uid"])
                if qr["s3c_status"] != "OK":
                    conn.execute(
                        """
                        INSERT INTO compile_state(question_uid, stage, status, last_error, updated_at)
                        VALUES (?, 'S3D', 'BLOCKED', '上游 S3C 非 OK', datetime('now'))
                        ON CONFLICT(question_uid, stage) DO UPDATE SET
                            status = 'BLOCKED', last_error = '上游 S3C 非 OK', updated_at = datetime('now');
                        """,
                        (uid,),
                    )
                    blocked += 1
                    continue

                q_steps = steps_by_q.get(uid, [])
                s3d_hash = _sha256_json(
                    {
                        "pv": STAGE3D_PROMPT_VERSION,
                        "tv": tax_ver,
                        "steps": [
                            (str(s["step_id"]), str(s["m_l1"]), str(s["m_l2"]), str(s["raw_l3"]))
                            for s in q_steps
                        ],
                        "ov": ov_tax_by_q.get(uid, ""),
                    }
                )
                if qr["s3d_status"] == "OK" and qr["s3d_hash"] == s3d_hash:
                    skipped += 1
                    continue

                for s in q_steps:
                    m_l1 = str(s["m_l1"] or qr["m_primary_l1_module"] or default_l1)
                    if m_l1 not in lookup.l1_names():
                        m_l1 = lookup.resolve_l1(m_l1) or default_l1
                    m_l2 = str(s["m_l2"] or "")
                    pair_hit = lookup.resolve_l2(m_l1, m_l2)
                    if pair_hit is not None:
                        m_l1, m_l2 = pair_hit
                    else:
                        l2_cands = lookup.l2_names(m_l1)
                        m_l2 = l2_cands[0] if l2_cands else default_l2

                    l3_op = (str(s["raw_l3"] or "")).strip() or (str(s["sub_goal"] or "")).strip()[:32] or f"{m_l2}基础应用"
                    norm_l3 = normalize_label(l3_op)
                    step_nid: str | None = l3_index.get((m_l1, m_l2, norm_l3))
                    if step_nid is None:
                        l2_id = make_node_id(slug, tax_ver, "L2", m_l1, m_l2, "")
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO knowledge_nodes(
                                node_id, grade_subject, taxonomy_version, node_level,
                                l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                            )
                            VALUES (?, ?, ?, 'L2', ?, ?, '', '[]', NULL);
                            """,
                            (l2_id, grade_subject, tax_ver, m_l1, m_l2),
                        )
                        step_nid = make_node_id(slug, tax_ver, "L3", m_l1, m_l2, l3_op)
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO knowledge_nodes(
                                node_id, grade_subject, taxonomy_version, node_level,
                                l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                            )
                            VALUES (?, ?, ?, 'L3', ?, ?, ?, '[]', NULL);
                            """,
                            (step_nid, grade_subject, tax_ver, m_l1, m_l2, l3_op),
                        )
                        if cur.rowcount > 0:
                            created_nodes += 1
                        l3_index[(m_l1, m_l2, norm_l3)] = step_nid

                    conn.execute(
                        "UPDATE question_steps SET m_knowledge_node_id = ? WHERE step_id = ?;",
                        (step_nid, s["step_id"]),
                    )

                conn.execute(
                    """
                    INSERT INTO compile_state(question_uid, stage, status, input_hash, prompt_version, model_name, attempts, last_error, updated_at)
                    VALUES (?, 'S3D', 'OK', ?, ?, 'deterministic', 1, '', datetime('now'))
                    ON CONFLICT(question_uid, stage) DO UPDATE SET
                        status = 'OK',
                        input_hash = excluded.input_hash,
                        prompt_version = excluded.prompt_version,
                        model_name = 'deterministic',
                        attempts = compile_state.attempts + 1,
                        last_error = '',
                        updated_at = datetime('now');
                    """,
                    (uid, s3d_hash, STAGE3D_PROMPT_VERSION),
                )
                refresh.mark_stages_stale(conn, uid, ["S4"])
                effective.recompute(conn, uid)
                mounted += 1

            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    return {
        "grade_subject": grade_subject,
        "taxonomy_version": tax_ver,
        "blocked": blocked,
        "skipped": skipped,
        "mounted_questions": mounted,
        "created_l3_nodes": created_nodes,
    }


async def run_compaction_async(
    conn: sqlite3.Connection,
    settings: Settings,
    vector_store: VectorStore,
    *,
    grade_subject: str,
    target: Literal["all", "l3", "concepts"] = "all",
    mode: Literal["api", "export", "import"] = "api",
    batch_size: int = 40,
    tasks_dir: Path | str = Path("data/unified/stage3_4/compact"),
    results_path: Path | str | None = None,
    use_mock: bool = False,
    lock_path: Path | str = Path("data/.compile.lock"),
) -> dict[str, Any]:
    """Stage 3d 同域无预筛全量召回并查集归并（L3 节点 + METHOD/MOTIF 全局概念）。"""
    slug, tax_ver, _, _ = _load_active_taxonomy(conn, grade_subject)
    embed_client = EmbeddingClient(settings, conn=conn, use_mock=use_mock)

    try:
        # 1. 增量同步待归并实体的向量库
        if target in ("all", "l3"):
            l3_rows = conn.execute(
                """
                SELECT node_id, l1_module, l2_entity, l3_operator
                FROM knowledge_nodes
                WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3' AND redirect_to_id IS NULL
                ORDER BY node_id;
                """,
                (grade_subject, tax_ver),
            ).fetchall()
            await vector_store.sync_vectors(
                "l3_nodes",
                [(str(r["node_id"]), f"{r['l1_module']}/{r['l2_entity']}/{r['l3_operator']}") for r in l3_rows],
                embed_client,
                scope_slug=slug,
                stage="STAGE3D_EMBED_L3",
            )

        if target in ("all", "concepts"):
            for kind_name, ns in (("METHOD", "concepts_method"), ("MOTIF", "concepts_motif")):
                c_rows = conn.execute(
                    """
                    SELECT concept_id, name
                    FROM concepts
                    WHERE kind = ? AND is_seed = 0 AND redirect_to_id IS NULL
                    ORDER BY concept_id;
                    """,
                    (kind_name,),
                ).fetchall()
                await vector_store.sync_vectors(
                    ns,  # type: ignore[arg-type]
                    [(str(r["concept_id"]), str(r["name"])) for r in c_rows],
                    embed_client,
                    stage=f"STAGE3D_EMBED_{kind_name}",
                )
    finally:
        await embed_client.aclose()

    # 2. 按作用域分组构造全量无预筛候选对
    grouped_candidates: list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]] = []
    if target in ("all", "l3"):
        l3_cands = recall_l3_merge_candidates(
            conn, vector_store, grade_subject=grade_subject, scope_slug=slug, taxonomy_version=tax_ver
        )
        by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for p in l3_cands:
            sc = p["scope"]
            by_scope.setdefault((str(sc["l1_module"]), str(sc["l2_entity"])), []).append(p)
        for (l1m, l2e), plist in sorted(by_scope.items()):
            grouped_candidates.append(
                (
                    "KNOWLEDGE_NODE",
                    f"L3({grade_subject}/{tax_ver}/{l1m}/{l2e})",
                    {"grade_subject": grade_subject, "taxonomy_version": tax_ver, "l1_module": l1m, "l2_entity": l2e},
                    plist,
                )
            )

    if target in ("all", "concepts"):
        for ckind in ("METHOD", "MOTIF"):
            c_cands = recall_concept_merge_candidates(conn, vector_store, kind=ckind)  # type: ignore[arg-type]
            if c_cands:
                grouped_candidates.append(
                    ("CONCEPT", f"CONCEPT({ckind})", {"kind": ckind}, c_cands)
                )

    eff_bs = max(1, int(batch_size))
    all_batches: list[dict[str, Any]] = []
    task_map: dict[str, Any] = {"pairs": {}, "grade_subject": grade_subject, "taxonomy_version": tax_ver}
    batch_counter = 0
    total_pairs = 0

    for t_kind, scope_desc, scope_dict, pairs in grouped_candidates:
        for start in range(0, len(pairs), eff_bs):
            batch_counter += 1
            chunk = pairs[start : start + eff_bs]
            total_pairs += len(chunk)
            bid = f"compact_batch_{batch_counter:04d}"
            p_msgs_in: list[dict[str, Any]] = []
            tids: list[str] = []
            for j, p in enumerate(chunk, start=1):
                pid = f"cmp_{batch_counter:04d}_{j:03d}"
                tids.append(pid)
                p_msgs_in.append(
                    {
                        "pair_id": pid,
                        "name_a": p["name_a"],
                        "name_b": p["name_b"],
                        "cosine_sim": p["cosine_sim"],
                        "jaccard": p["jaccard"],
                    }
                )
                task_map["pairs"][pid] = {
                    "target_kind": t_kind,
                    "scope_dict": scope_dict,
                    "id_a": p["id_a"],
                    "name_a": p["name_a"],
                    "id_b": p["id_b"],
                    "name_b": p["name_b"],
                }
            msgs = build_merge_judge_messages(
                target_kind=t_kind, scope_desc=scope_desc, candidate_pairs=p_msgs_in
            )
            all_batches.append(
                {
                    "batch_id": bid,
                    "target_kind": t_kind,
                    "scope_dict": scope_dict,
                    "messages": msgs,
                    "task_item_ids": tids,
                }
            )

    if mode == "export":
        manifest = export_task_batches(
            "compact", all_batches, task_map, tasks_dir, schema_cls=MergeJudgeBatchOutput
        )
        return {
            "mode": "export",
            "grade_subject": grade_subject,
            "candidate_pairs": total_pairs,
            "batch_count": len(all_batches),
            "manifest": manifest,
        }

    decisions_by_pair: list[tuple[dict[str, Any], MergeJudgeItem]] = []
    if mode == "import":
        if results_path is None:
            raise PreconditionError("PRECONDITION_FAILED: --mode import 必须提供 --results")
        task_map_file = Path(tasks_dir) / "compact_task_map.json"
        valid_by_batch, loaded_map, errs = load_and_validate_results(
            "compact", results_path, task_map_file, MergeJudgeBatchOutput
        )
        pairs_map = loaded_map.get("pairs", {})
        for out_model in valid_by_batch.values():
            for dec in out_model.decisions:
                if dec.should_merge and dec.pair_id in pairs_map:
                    decisions_by_pair.append((pairs_map[dec.pair_id], dec))
    else:
        errs = []
        if all_batches:
            gateway = LLMGateway(settings, conn=conn)

            async def _judge_one(b: dict[str, Any]) -> MergeJudgeBatchOutput:
                res = await gateway.call_structured(
                    slot="judge_llm",
                    stage="STAGE3D_COMPACT",
                    messages=b["messages"],
                    schema=MergeJudgeBatchOutput,
                    prompt_version="s3d_compact_v1",
                )
                return res.parsed

            try:
                judge_outs = await asyncio.gather(*(_judge_one(b) for b in all_batches))
            finally:
                await gateway.aclose()

            pairs_map = task_map["pairs"]
            for out_model in judge_outs:
                for dec in out_model.decisions:
                    if dec.should_merge and dec.pair_id in pairs_map:
                        decisions_by_pair.append((pairs_map[dec.pair_id], dec))

    # 统计使用频次用于选定更稳定的 canonical to_id
    l3_freq = {
        str(r["m_knowledge_node_id"]): int(r["c"])
        for r in conn.execute(
            "SELECT m_knowledge_node_id, COUNT(*) AS c FROM question_steps WHERE m_knowledge_node_id IS NOT NULL GROUP BY m_knowledge_node_id;"
        ).fetchall()
    }
    concept_freq = {
        str(r["concept_id"]): int(r["c"])
        for r in conn.execute(
            "SELECT concept_id, COUNT(*) AS c FROM question_concepts GROUP BY concept_id;"
        ).fetchall()
    }

    merge_groups: dict[tuple[str, str], tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for pmeta, dec in decisions_by_pair:
        tk = str(pmeta["target_kind"])
        freq = l3_freq if tk == "KNOWLEDGE_NODE" else concept_freq
        fa = freq.get(str(pmeta["id_a"]), 0)
        fb = freq.get(str(pmeta["id_b"]), 0)
        if dec.canonical_name and dec.canonical_name == pmeta["name_b"] and fa == fb:
            from_id, to_id = pmeta["id_a"], pmeta["id_b"]
        elif fb > fa:
            from_id, to_id = pmeta["id_a"], pmeta["id_b"]
        elif fa > fb:
            from_id, to_id = pmeta["id_b"], pmeta["id_a"]
        else:
            from_id, to_id = (pmeta["id_b"], pmeta["id_a"]) if pmeta["id_a"] < pmeta["id_b"] else (pmeta["id_a"], pmeta["id_b"])

        sc_key = (tk, json.dumps(pmeta["scope_dict"], sort_keys=True, ensure_ascii=False))
        bucket = merge_groups.setdefault(sc_key, (pmeta["scope_dict"], []))
        bucket[1].append(
            {
                "from_id": from_id,
                "to_id": to_id,
                "evidence": f"{dec.canonical_name}: {dec.reason}".strip(": "),
            }
        )

    batch_id = f"compact_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}"
    total_applied = 0
    total_skipped = 0
    with CompileLock(lock_path):
        for (tk, _), (sc_dict, mlist) in merge_groups.items():
            stat = apply_merge_pairs(
                conn,
                target_kind=tk,  # type: ignore[arg-type]
                scope_dict=sc_dict,
                merges=mlist,
                batch_id=batch_id,
            )
            total_applied += int(stat["applied"])
            total_skipped += int(stat["skipped"])

    return {
        "mode": mode,
        "grade_subject": grade_subject,
        "batch_id": batch_id if total_applied > 0 else "",
        "candidate_pairs": total_pairs,
        "accepted_merges": len(decisions_by_pair),
        "applied_merges": total_applied,
        "skipped_merges": total_skipped,
        "batch_errors": len(errs),
    }


async def run_stage3e_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str,
    mode: Literal["api", "export", "import"] = "api",
    tasks_dir: Path | str = Path("data/unified/stage3_4/3e"),
    results_path: Path | str | None = None,
    lock_path: Path | str = Path("data/.compile.lock"),
) -> dict[str, Any]:
    """Stage 3e：构建 L2 先修 DAG（Kahn 破环）与跨模块共现知识边。"""
    slug, tax_ver, spec, lookup = _load_active_taxonomy(conn, grade_subject)
    taxonomy_enum = format_taxonomy_enum(spec)

    # 1. 确保全部 L2 节点已写入 knowledge_nodes 并建立 (l1, l2) -> node_id 映射
    l2_node_map: dict[tuple[str, str], str] = {}
    for mod in spec.modules:
        for l2 in mod.l2_entities:
            nid = make_node_id(slug, tax_ver, "L2", mod.name, l2.name, "")
            l2_node_map[(mod.name, l2.name)] = nid

    # 2. 扫描全学段有效题步骤共现与有序转移
    q_steps_rows = conn.execute(
        """
        SELECT s.question_uid, s.branch_id, s.step_index, s.m_l1, s.m_l2
        FROM question_steps s
        JOIN questions q ON q.question_uid = s.question_uid
        JOIN compile_state cs ON cs.question_uid = q.question_uid AND cs.stage = 'S3C'
        WHERE q.grade_subject = ? AND q.is_retired = 0 AND cs.status = 'OK'
          AND s.m_l1 != '' AND s.m_l2 != ''
        ORDER BY s.question_uid, s.branch_id, s.step_index;
        """,
        (grade_subject,),
    ).fetchall()

    steps_by_branch: dict[tuple[str, str], list[tuple[str, str]]] = {}
    l2_by_q: dict[str, set[tuple[str, str]]] = {}
    for r in q_steps_rows:
        pair = (str(r["m_l1"]), str(r["m_l2"]))
        if pair not in l2_node_map:
            hit = lookup.resolve_l2(pair[0], pair[1])
            if hit is None or hit not in l2_node_map:
                continue
            pair = hit
        steps_by_branch.setdefault((str(r["question_uid"]), str(r["branch_id"])), []).append(pair)
        l2_by_q.setdefault(str(r["question_uid"]), set()).add(pair)

    co_counts: Counter[tuple[tuple[str, str], tuple[str, str]]] = Counter()
    for l2_set in l2_by_q.values():
        sorted_l2 = sorted(l2_set)
        for i in range(len(sorted_l2)):
            for j in range(i + 1, len(sorted_l2)):
                co_counts[(sorted_l2[i], sorted_l2[j])] += 1

    trans_counts: Counter[tuple[tuple[str, str], tuple[str, str]]] = Counter()
    for seq in steps_by_branch.values():
        for i in range(len(seq)):
            for j in range(i + 1, len(seq)):
                if seq[i] != seq[j]:
                    trans_counts[(seq[i], seq[j])] += 1

    ordered_transitions = [
        {
            "source_l1": src[0],
            "source_l2": src[1],
            "target_l1": dst[0],
            "target_l2": dst[1],
            "count": int(cnt),
        }
        for (src, dst), cnt in trans_counts.most_common(120)
    ]

    if mode == "export":
        msgs = build_prerequisite_dag_messages(
            grade_subject=grade_subject,
            taxonomy_enum_text=taxonomy_enum,
            ordered_transitions=ordered_transitions,
        )
        manifest = export_task_batches(
            "3e",
            [{"batch_id": "3e_batch_0001", "messages": msgs, "task_item_ids": ["3e_dag_0001"]}],
            {"grade_subject": grade_subject, "taxonomy_version": tax_ver},
            tasks_dir,
            schema_cls=PrerequisiteDagOutput,
        )
        return {
            "mode": "export",
            "grade_subject": grade_subject,
            "transitions": len(ordered_transitions),
            "co_occurrence_pairs": len(co_counts),
            "manifest": manifest,
        }

    if mode == "import":
        if results_path is None:
            raise PreconditionError("PRECONDITION_FAILED: --mode import 必须提供 --results")
        task_map_file = Path(tasks_dir) / "3e_task_map.json"
        valid_by_batch, _, errs = load_and_validate_results(
            "3e", results_path, task_map_file, PrerequisiteDagOutput
        )
        if errs:
            raise PreconditionError(f"PRECONDITION_FAILED: Stage 3e 结果校验失败 {len(errs)} 批")
        dag_out = next(iter(valid_by_batch.values())) if valid_by_batch else PrerequisiteDagOutput(edges=[])
    else:
        msgs = build_prerequisite_dag_messages(
            grade_subject=grade_subject,
            taxonomy_enum_text=taxonomy_enum,
            ordered_transitions=ordered_transitions,
        )
        gateway = LLMGateway(settings, conn=conn)
        try:
            res = await gateway.call_structured(
                slot="text_llm",
                stage="STAGE3E_DAG",
                messages=msgs,
                schema=PrerequisiteDagOutput,
                prompt_version="s3e_v1",
            )
            dag_out = res.parsed
        finally:
            await gateway.aclose()

    # 将 LLM 返回的 L2 先修边映射为 L2 node_id 并执行 Kahn 环检测断环
    raw_prereq_edges: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in dag_out.edges:
        src_pair = lookup.resolve_l2(edge.source_l1, edge.source_l2)
        dst_pair = lookup.resolve_l2(edge.target_l1, edge.target_l2)
        if src_pair is None or dst_pair is None or src_pair == dst_pair:
            continue
        src_id = l2_node_map.get(src_pair)
        dst_id = l2_node_map.get(dst_pair)
        if not src_id or not dst_id or src_id == dst_id:
            continue
        raw_prereq_edges[(src_id, dst_id)] = {
            "source_node_id": src_id,
            "target_node_id": dst_id,
            "weight": max(0.05, min(1.0, float(edge.confidence))),
            "evidence": {"reason": edge.reason, "observed_count": trans_counts.get((src_pair, dst_pair), 0)},
        }

    acyclic_prereq, broken_prereq = check_dag_and_break_cycles(list(raw_prereq_edges.values()))

    with CompileLock(lock_path):
        conn.execute("BEGIN IMMEDIATE;")
        try:
            for mod in spec.modules:
                for l2 in mod.l2_entities:
                    nid = l2_node_map[(mod.name, l2.name)]
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_nodes(
                            node_id, grade_subject, taxonomy_version, node_level,
                            l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                        )
                        VALUES (?, ?, ?, 'L2', ?, ?, '', ?, NULL);
                        """,
                        (nid, grade_subject, tax_ver, mod.name, l2.name, json.dumps(l2.aliases, ensure_ascii=False)),
                    )

            conn.execute("DELETE FROM knowledge_edges WHERE grade_subject = ?;", (grade_subject,))

            for e in acyclic_prereq:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO knowledge_edges(
                        source_node_id, target_node_id, grade_subject, edge_type, weight, evidence_json
                    )
                    VALUES (?, ?, ?, 'PREREQUISITE', ?, ?);
                    """,
                    (
                        e["source_node_id"],
                        e["target_node_id"],
                        grade_subject,
                        float(e["weight"]),
                        json.dumps(e["evidence"], ensure_ascii=False),
                    ),
                )

            co_written = 0
            for (p_a, p_b), cnt in co_counts.most_common():
                id_a = l2_node_map.get(p_a)
                id_b = l2_node_map.get(p_b)
                if not id_a or not id_b or id_a == id_b:
                    continue
                u, v = (id_a, id_b) if id_a < id_b else (id_b, id_a)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO knowledge_edges(
                        source_node_id, target_node_id, grade_subject, edge_type, weight, evidence_json
                    )
                    VALUES (?, ?, ?, 'CO_OCCURRENCE', ?, ?);
                    """,
                    (u, v, grade_subject, float(cnt), json.dumps({"co_count": int(cnt)}, ensure_ascii=False)),
                )
                co_written += 1

            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    return {
        "mode": mode,
        "grade_subject": grade_subject,
        "taxonomy_version": tax_ver,
        "prerequisite_edges": len(acyclic_prereq),
        "broken_cycles_edges": len(broken_prereq),
        "co_occurrence_edges": co_written,
    }
