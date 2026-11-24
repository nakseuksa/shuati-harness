"""编译器全景态势巡检与挂起批扫描内核（Step 3，契约 §1.5）。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from shuati.config import load_settings
from shuati.db.connection import get_connection
from shuati.vectors.store import VectorStore

_PIPELINE_STAGES: tuple[str, ...] = ("S1", "S2", "S3C", "S3D", "S4")
_SUBAGENT_STAGE_KEYS: tuple[str, ...] = (
    "unified",
    "stage1",
    "stage2",
    "propose",
    "3c",
    "3d",
    "3e",
    "stage4",
    "diagnose",
)


def _safe_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _count_jsonl_records(path: Path) -> int:
    try:
        cnt = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                cnt += 1
        return cnt
    except Exception:
        return 0


def _collect_unified_result_task_ids(results_files: list[Path]) -> set[str]:
    found_ids: set[str] = set()
    for rf in results_files:
        try:
            for line in rf.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict) and obj.get("task_id"):
                        found_ids.add(str(obj["task_id"]).strip())
                except Exception:
                    continue
        except Exception:
            continue
    return found_ids


def _count_stage34_results(results_files: list[Path]) -> int:
    total = 0
    for rf in results_files:
        if rf.suffix.lower() == ".json":
            obj = _safe_read_json(rf)
            if isinstance(obj, list):
                total += sum(1 for x in obj if isinstance(x, dict))
            elif isinstance(obj, dict):
                total += 1
        else:
            total += _count_jsonl_records(rf)
    return total


def scan_pending_batches(unified_root: Path | str = Path("data/unified")) -> list[dict[str, Any]]:
    """扫描 data/unified 与 data/unified/stage3_4/* 中的挂起/待回灌批任务清单。"""
    root = Path(unified_root)
    if not root.exists() or not root.is_dir():
        return []

    pending: list[dict[str, Any]] = []

    # 1. 检查根目录 unified (UNIFIED_S1_S2 / STAGE2_ONLY)
    tmap_path = root / "task_map.json"
    batches_dir = root / "batches"
    root_batch_files = sorted(batches_dir.glob("batch_*.jsonl")) if batches_dir.is_dir() else []
    if tmap_path.is_file() or root_batch_files:
        tmap_data = _safe_read_json(tmap_path) if tmap_path.is_file() else {}
        task_count = len(tmap_data) if isinstance(tmap_data, dict) else 0
        results_files = sorted(root.glob("results*.jsonl"))
        result_ids = _collect_unified_result_task_ids(results_files)
        if isinstance(tmap_data, dict) and tmap_data:
            matched_results = len(result_ids & set(tmap_data.keys()))
        else:
            matched_results = len(result_ids)
        retry_files = sorted(root.glob("retry*.jsonl"))
        retry_count = sum(_count_jsonl_records(rf) for rf in retry_files)

        if retry_count > 0 and matched_results == 0:
            status = "HAS_RETRY"
        elif matched_results == 0:
            status = "NO_RESULTS"
        elif task_count > 0 and matched_results < task_count:
            status = "PARTIAL_RESULTS"
        else:
            status = "READY_TO_IMPORT"

        pending.append(
            {
                "dir": root.as_posix(),
                "kind": "unified_s1_s2",
                "step_kind": "unified",
                "manifest_file": None,
                "task_map_file": tmap_path.as_posix() if tmap_path.is_file() else None,
                "batch_count": len(root_batch_files),
                "task_count": task_count,
                "results_files": [p.as_posix() for p in results_files],
                "result_count": matched_results,
                "retry_files": [p.as_posix() for p in retry_files if _count_jsonl_records(p) > 0],
                "retry_count": retry_count,
                "status": status,
            }
        )

    # 2. 检查 stage3_4/* 子目录
    stage34_dir = root / "stage3_4"
    if stage34_dir.is_dir():
        for sub in sorted(stage34_dir.iterdir(), key=lambda p: p.name):
            if not sub.is_dir():
                continue
            manifests = sorted(sub.glob("*_manifest.json"))
            task_maps = sorted(sub.glob("*_task_map.json"))
            sub_batches_dir = sub / "batches"
            sub_batch_files = sorted(sub_batches_dir.glob("batch_*.jsonl")) if sub_batches_dir.is_dir() else []

            if not manifests and not task_maps and not sub_batch_files:
                continue

            manifest_path = manifests[0] if manifests else None
            task_map_path = task_maps[0] if task_maps else None
            manifest_data = _safe_read_json(manifest_path) if manifest_path else {}
            task_map_data = _safe_read_json(task_map_path) if task_map_path else {}

            step_kind = ""
            if isinstance(manifest_data, dict) and manifest_data.get("step_kind"):
                step_kind = str(manifest_data["step_kind"])
            elif task_map_path is not None:
                step_kind = task_map_path.stem.removesuffix("_task_map")
            else:
                step_kind = sub.name

            batch_count = (
                int(manifest_data.get("batch_count", 0))
                if isinstance(manifest_data, dict) and manifest_data.get("batch_count") is not None
                else len(sub_batch_files)
            )
            task_count = len(task_map_data) if isinstance(task_map_data, dict) else 0

            results_files = sorted(list(sub.glob("results*.jsonl")) + list(sub.glob("results*.json")))
            result_count = _count_stage34_results(results_files)
            retry_files = sorted(sub.glob("retry*.jsonl"))
            retry_count = sum(_count_jsonl_records(rf) for rf in retry_files)

            if retry_count > 0 and result_count == 0:
                status = "HAS_RETRY"
            elif result_count == 0:
                status = "NO_RESULTS"
            elif batch_count > 0 and result_count < batch_count:
                status = "PARTIAL_RESULTS"
            else:
                status = "READY_TO_IMPORT"

            pending.append(
                {
                    "dir": sub.as_posix(),
                    "kind": "stage3_4",
                    "step_kind": step_kind,
                    "manifest_file": manifest_path.as_posix() if manifest_path else None,
                    "task_map_file": task_map_path.as_posix() if task_map_path else None,
                    "batch_count": batch_count,
                    "task_count": task_count,
                    "results_files": [p.as_posix() for p in results_files],
                    "result_count": result_count,
                    "retry_files": [p.as_posix() for p in retry_files if _count_jsonl_records(p) > 0],
                    "retry_count": retry_count,
                    "status": status,
                }
            )

    return pending


def _resolve_subagent_routing(config_dir: Path) -> tuple[dict[str, Any], str]:
    settings = load_settings(config_dir / "settings.yaml")
    subagent_cfg = getattr(settings, "subagent", None)
    default_model = str(getattr(subagent_cfg, "default_model", "gemini-4-neon-medium") or "gemini-4-neon-medium")
    max_parallel_workers = int(getattr(subagent_cfg, "max_parallel_workers", 4) or 4)
    raw_stage_models = getattr(subagent_cfg, "stage_models", {}) or {}
    stage_models = {str(k): str(v) for k, v in raw_stage_models.items()} if isinstance(raw_stage_models, dict) else {}
    resolved_stages = {stg: stage_models.get(stg, default_model) for stg in _SUBAGENT_STAGE_KEYS}
    routing = {
        "default_model": default_model,
        "max_parallel_workers": max_parallel_workers,
        "stage_models": stage_models,
        "resolved_stages": resolved_stages,
    }
    return routing, str(settings.llm.embedding.model or "")


def _check_vectors_status(
    vectors_dir: Path,
    configured_embedding_model: str,
    subjects_info: dict[str, dict[str, Any]],
    conn: sqlite3.Connection | None,
) -> dict[str, Any]:
    store = VectorStore(vectors_dir)
    meta = store.load_meta()
    recorded_model = str(meta.get("embedding_model") or "")
    model_mismatch = bool(recorded_model and configured_embedding_model and recorded_model != configured_embedding_model)
    quantile_keys = sorted(list((meta.get("quantiles") or {}).keys()))

    npz_row_counts: dict[str, int] = {}
    if store.vectors_dir.exists():
        for npz_p in sorted(store.vectors_dir.glob("*.npz")):
            try:
                with np.load(npz_p, allow_pickle=False) as data:
                    npz_row_counts[npz_p.name] = int(len(data["ids"]))
            except Exception:
                npz_row_counts[npz_p.name] = -1

    drift_warnings: list[str] = []
    needs_reindex = False
    if model_mismatch:
        needs_reindex = True
        drift_warnings.append(
            f"Embedding 模型与向量仓不一致 ({recorded_model!r} != {configured_embedding_model!r})，需重算向量仓。"
        )

    scopes_meta = meta.get("scopes") if isinstance(meta.get("scopes"), dict) else {}
    for gs, s_info in subjects_info.items():
        slug = str(s_info["slug"])
        servable_total = int(s_info["servable_total"])
        if servable_total == 0:
            continue

        surf_name = f"surface_{slug}.npz"
        skel_name = f"skeleton_{slug}.npz"
        surf_cnt = npz_row_counts.get(surf_name)
        skel_cnt = npz_row_counts.get(skel_name)
        if surf_cnt is None or skel_cnt is None:
            needs_reindex = True
            drift_warnings.append(f"{gs} ({slug}) 存在 {servable_total} 道可服务题但缺失 surface/skeleton 向量文件。")
        elif surf_cnt != servable_total or skel_cnt != servable_total:
            needs_reindex = True
            drift_warnings.append(
                f"{gs} ({slug}) 向量条数 (surface={surf_cnt}, skeleton={skel_cnt}) 与可服务题量 ({servable_total}) 不一致。"
            )

        if f"{slug}::surface" not in quantile_keys or f"{slug}::skeleton" not in quantile_keys:
            needs_reindex = True
            drift_warnings.append(f"{gs} ({slug}) 缺失冻结分位表 quantiles。")

        scope_m = scopes_meta.get(slug) if isinstance(scopes_meta, dict) else None
        if isinstance(scope_m, dict):
            n0 = int(scope_m.get("n0") or 0)
            if n0 > 0 and abs(servable_total - n0) / n0 >= 0.20:
                needs_reindex = True
                drift_warnings.append(
                    f"{gs} ({slug}) 可服务题量相对冻结基准增长/变化 >= 20% (n0={n0} -> N={servable_total})，建议重建分位表。"
                )
            p0 = scope_m.get("l1_dist")
            if isinstance(p0, dict) and conn is not None:
                l1_rows = conn.execute(
                    """
                    SELECT eff_primary_l1_module, COUNT(*) AS cnt
                    FROM questions
                    WHERE grade_subject = ? AND is_retired = 0 AND eff_is_servable = 1
                    GROUP BY eff_primary_l1_module
                    """,
                    (gs,),
                ).fetchall()
                cur_dist = {
                    str(r["eff_primary_l1_module"] or ""): int(r["cnt"]) / servable_total
                    for r in l1_rows
                }
                all_keys = set(p0.keys()) | set(cur_dist.keys())
                max_delta = max((abs(cur_dist.get(k, 0.0) - float(p0.get(k, 0.0))) for k in all_keys), default=0.0)
                tvd = 0.5 * sum(abs(cur_dist.get(k, 0.0) - float(p0.get(k, 0.0))) for k in all_keys)
                if max_delta >= 0.10 or tvd >= 0.15:
                    needs_reindex = True
                    drift_warnings.append(
                        f"{gs} ({slug}) L1 分布发生漂移 (max_delta={max_delta:.3f}, TVD={tvd:.3f})，建议重算分位表。"
                    )

    return {
        "embedding_model": recorded_model,
        "configured_embedding_model": configured_embedding_model,
        "model_mismatch": model_mismatch,
        "dimension": int(meta.get("dimension") or 0),
        "meta_version": int(meta.get("meta_version") or 0),
        "updated_at": str(meta.get("updated_at") or ""),
        "quantile_keys": quantile_keys,
        "npz_row_counts": npz_row_counts,
        "needs_reindex": needs_reindex,
        "drift_warnings": drift_warnings,
    }


def _derive_recommended_commands(
    *,
    db_exists: bool,
    subjects_info: dict[str, dict[str, Any]],
    pending_batches: list[dict[str, Any]],
    vectors_info: dict[str, Any],
) -> list[str]:
    cmds: list[str] = []
    if not db_exists:
        return ["shuati ingest --dry-run --json", "shuati ingest --json"]

    total_active = sum(int(s["active_total"]) for s in subjects_info.values())
    if total_active == 0:
        cmds.append("shuati ingest --json")

    for batch in pending_batches:
        st = batch.get("status")
        if st == "READY_TO_IMPORT":
            if batch.get("kind") == "unified_s1_s2":
                cmds.append("shuati unified import --results data/unified/results.jsonl --task-map data/unified/task_map.json --json")
            else:
                sk = batch.get("step_kind")
                bdir = batch.get("dir")
                if sk in ("3c", "3e", "stage4"):
                    st_arg = "4" if sk == "stage4" else sk
                    cmds.append(f"shuati compile --stage {st_arg} --mode import --tasks-dir {bdir} --results {bdir}/results.jsonl --json")
                elif sk == "propose":
                    cmds.append(f"shuati taxonomy propose --mode import --tasks-dir {bdir} --results {bdir}/results.jsonl --json")
                elif sk == "compact":
                    cmds.append(f"shuati graph compact --mode import --tasks-dir {bdir} --results {bdir}/results.jsonl --json")
        elif st == "HAS_RETRY":
            cmds.append(f"# 检查并重跑挂起重试队列: {batch.get('dir')}")

    for gs, s_info in subjects_info.items():
        if int(s_info["active_total"]) == 0:
            continue
        verif = s_info["verification_counts"]
        s1_map = s_info["stage_matrix"]["S1"]
        s2_map = s_info["stage_matrix"]["S2"]
        s3c_map = s_info["stage_matrix"]["S3C"]
        s3d_map = s_info["stage_matrix"]["S3D"]
        s4_map = s_info["stage_matrix"]["S4"]

        if verif.get("NEEDS_REVIEW", 0) > 0:
            cmds.append(f"shuati review --grade-subject {gs}")
        if not s_info["anchors_ready"]:
            cmds.append(f"shuati calibrate-anchors --grade-subject {gs} --json")

        s1_pending = sum(s1_map.get(k, 0) for k in ("PENDING", "STALE", "FAILED"))
        s2_pending = sum(s2_map.get(k, 0) for k in ("PENDING", "STALE", "FAILED"))
        if s1_pending > 0 or s2_pending > 0:
            if s_info["anchors_ready"]:
                cmds.append(f"shuati unified export --grade-subject {gs} --json")
            else:
                cmds.append(f"shuati compile --stage 1 --grade-subject {gs} --json")

        tax_status = s_info["taxonomy_status"]
        s2_ok = s2_map.get("OK", 0)
        if tax_status == "NONE" and s2_ok > 0:
            cmds.append(f"shuati taxonomy propose --grade-subject {gs} --json")
        elif tax_status == "DRAFT":
            cmds.append(f"shuati taxonomy lock --grade-subject {gs} --json")

        if int(s_info["pending_taxonomy_candidates"]) > 0:
            cmds.append(f"shuati taxonomy candidates --grade-subject {gs} --json")

        if tax_status == "LOCKED":
            if sum(s3c_map.get(k, 0) for k in ("PENDING", "STALE", "FAILED")) > 0:
                cmds.append(f"shuati compile --stage 3c --grade-subject {gs} --json")
            if sum(s3d_map.get(k, 0) for k in ("PENDING", "STALE", "FAILED")) > 0:
                cmds.append(f"shuati compile --stage 3d --grade-subject {gs} --json")
            if sum(s4_map.get(k, 0) for k in ("PENDING", "STALE", "FAILED")) > 0:
                cmds.append(f"shuati compile --stage 4 --grade-subject {gs} --json")

    if vectors_info.get("needs_reindex"):
        cmds.append("shuati reindex --vectors --quantiles --json")

    return list(dict.fromkeys(cmds))


def get_pipeline_status(
    *,
    db_path: Path | str = Path("data/shuati.db"),
    config_dir: Path | str = Path("config"),
    vectors_dir: Path | str = Path("data/vectors"),
    unified_dir: Path | str = Path("data/unified"),
    grade_subject: str | None = None,
) -> dict[str, Any]:
    """只读采集流水线全景态势：学段 Stage 0→4 矩阵、NEEDS_REVIEW 拆解、词表/锚点、向量漂移与挂起批。"""
    db_p = Path(db_path)
    cfg_dir = Path(config_dir)
    vec_dir = Path(vectors_dir)
    uni_dir = Path(unified_dir)

    subagent_routing, configured_embed_model = _resolve_subagent_routing(cfg_dir)
    pending_batches = scan_pending_batches(uni_dir)
    warnings: list[str] = []
    subjects_info: dict[str, dict[str, Any]] = {}

    if not db_p.exists():
        warnings.append(f"数据库文件不存在: {db_p}")
        vectors_info = _check_vectors_status(vec_dir, configured_embed_model, subjects_info, None)
        warnings.extend(vectors_info["drift_warnings"])
        return {
            "db_exists": False,
            "db_initialized": False,
            "db_path": db_p.as_posix(),
            "grade_subject_filter": grade_subject,
            "subjects": subjects_info,
            "subagent_routing": subagent_routing,
            "vectors": vectors_info,
            "pending_batches": pending_batches,
            "recommended_next_commands": _derive_recommended_commands(
                db_exists=False,
                subjects_info=subjects_info,
                pending_batches=pending_batches,
                vectors_info=vectors_info,
            ),
            "warnings": warnings,
        }

    with closing(get_connection(db_p, readonly=True)) as conn:
        required_tables = {"grade_subjects", "raw_questions", "questions", "compile_state", "taxonomy_candidates"}
        try:
            existing_tables = {
                str(r["name"])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        except sqlite3.DatabaseError as exc:
            existing_tables = set()
            warnings.append(f"数据库无法读取或未初始化: {exc}")

        if not required_tables.issubset(existing_tables):
            missing = sorted(required_tables - existing_tables)
            warnings.append(f"数据库尚未初始化核心表（缺少 {missing}）")
            vectors_info = _check_vectors_status(vec_dir, configured_embed_model, subjects_info, None)
            warnings.extend(vectors_info["drift_warnings"])
            return {
                "db_exists": True,
                "db_initialized": False,
                "db_path": db_p.as_posix(),
                "grade_subject_filter": grade_subject,
                "subjects": subjects_info,
                "subagent_routing": subagent_routing,
                "vectors": vectors_info,
                "pending_batches": pending_batches,
                "recommended_next_commands": _derive_recommended_commands(
                    db_exists=False,
                    subjects_info=subjects_info,
                    pending_batches=pending_batches,
                    vectors_info=vectors_info,
                ),
                "warnings": warnings,
            }

        if grade_subject is not None:
            gs_rows = conn.execute(
                """
                SELECT grade_subject, slug, taxonomy_status, taxonomy_version,
                       anchors_ready, anchors_version
                FROM grade_subjects
                WHERE grade_subject = ?
                """,
                (grade_subject,),
            ).fetchall()
            if not gs_rows:
                warnings.append(f"学段学科 '{grade_subject}' 尚未在数据库中注册。")
        else:
            gs_rows = conn.execute(
                """
                SELECT grade_subject, slug, taxonomy_status, taxonomy_version,
                       anchors_ready, anchors_version
                FROM grade_subjects
                ORDER BY grade_subject ASC
                """
            ).fetchall()

        for gsr in gs_rows:
            gs = str(gsr["grade_subject"])
            raw_total = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM raw_questions WHERE grade_subject = ?",
                    (gs,),
                ).fetchone()["c"]
            )
            counts_row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN is_retired = 0 AND eff_is_excluded = 0 THEN 1 ELSE 0 END) AS active_total,
                    SUM(CASE WHEN is_retired = 0 AND eff_is_servable = 1 THEN 1 ELSE 0 END) AS servable_total
                FROM questions
                WHERE grade_subject = ?
                """,
                (gs,),
            ).fetchone()
            active_total = int(counts_row["active_total"] or 0)
            servable_total = int(counts_row["servable_total"] or 0)

            verification_counts: dict[str, int] = {}
            for vr in conn.execute(
                """
                SELECT eff_verification_status, COUNT(*) AS cnt
                FROM questions
                WHERE grade_subject = ? AND is_retired = 0 AND eff_is_excluded = 0
                GROUP BY eff_verification_status
                """,
                (gs,),
            ).fetchall():
                verification_counts[str(vr["eff_verification_status"])] = int(vr["cnt"])

            needs_review_by_reason: dict[str, int] = {}
            for nr in conn.execute(
                """
                SELECT eff_review_reason, review_reason
                FROM questions
                WHERE grade_subject = ?
                  AND is_retired = 0
                  AND eff_is_excluded = 0
                  AND eff_verification_status = 'NEEDS_REVIEW'
                """,
                (gs,),
            ).fetchall():
                raw_reason = str(nr["eff_review_reason"] or nr["review_reason"] or "").strip()
                parts = [p.strip() for p in raw_reason.split(",") if p.strip()]
                if not parts:
                    needs_review_by_reason["UNSPECIFIED"] = needs_review_by_reason.get("UNSPECIFIED", 0) + 1
                else:
                    for part in parts:
                        needs_review_by_reason[part] = needs_review_by_reason.get(part, 0) + 1

            stage_matrix: dict[str, dict[str, int]] = {stg: {} for stg in _PIPELINE_STAGES}
            for sr in conn.execute(
                """
                SELECT cs.stage, cs.status, COUNT(*) AS cnt
                FROM compile_state cs
                JOIN questions q ON q.question_uid = cs.question_uid
                WHERE q.grade_subject = ? AND q.is_retired = 0 AND q.eff_is_excluded = 0
                GROUP BY cs.stage, cs.status
                ORDER BY cs.stage, cs.status
                """,
                (gs,),
            ).fetchall():
                stg = str(sr["stage"])
                if stg in stage_matrix:
                    stage_matrix[stg][str(sr["status"])] = int(sr["cnt"])

            pending_cands = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM taxonomy_candidates
                    WHERE grade_subject = ? AND status = 'PENDING'
                    """,
                    (gs,),
                ).fetchone()["c"]
            )

            subjects_info[gs] = {
                "slug": str(gsr["slug"]),
                "raw_total": raw_total,
                "active_total": active_total,
                "servable_total": servable_total,
                "verification_counts": verification_counts,
                "needs_review_by_reason": needs_review_by_reason,
                "stage_matrix": stage_matrix,
                "anchors_ready": bool(gsr["anchors_ready"]),
                "anchors_version": str(gsr["anchors_version"] or ""),
                "taxonomy_status": str(gsr["taxonomy_status"]),
                "taxonomy_version": str(gsr["taxonomy_version"] or ""),
                "pending_taxonomy_candidates": pending_cands,
            }

        vectors_info = _check_vectors_status(vec_dir, configured_embed_model, subjects_info, conn)
        warnings.extend(vectors_info["drift_warnings"])

        recommended_next_commands = _derive_recommended_commands(
            db_exists=True,
            subjects_info=subjects_info,
            pending_batches=pending_batches,
            vectors_info=vectors_info,
        )

    return {
        "db_exists": True,
        "db_initialized": True,
        "db_path": db_p.as_posix(),
        "grade_subject_filter": grade_subject,
        "subjects": subjects_info,
        "subagent_routing": subagent_routing,
        "vectors": vectors_info,
        "pending_batches": pending_batches,
        "recommended_next_commands": recommended_next_commands,
        "warnings": warnings,
    }
