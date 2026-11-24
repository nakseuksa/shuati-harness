"""编译器全景态势巡检与挂起批扫描内核（Step 3，契约 §1.5）。"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import platform
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import yaml

from shuati.config import Settings, load_settings
from shuati.core import effective
from shuati.db import human_mirror
from shuati.db.connection import (
    CompileLock,
    PreconditionError,
    SingleWriterExecutor,
    get_connection,
)
from shuati.db.schema import init_db
from shuati.llm.embeddings import EmbeddingClient, _is_loopback_base_url
from shuati.llm.seam_loader import lint_prompt_seams
from shuati.vectors.store import VectorStore
from shuati.verify.sympy_sandbox import run_verify_script

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


def _run_async_sync(coro: Any) -> Any:
    """在同步上下文安全执行协程（兼容已存在事件循环）。"""
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _atomic_write_text_file(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, target)
    except BaseException:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


async def _probe_llm_slot_api(slot_name: str, slot_cfg: Any) -> dict[str, Any]:
    api_key = str(getattr(slot_cfg, "api_key", "") or "").strip()
    base_url = str(getattr(slot_cfg, "base_url", "") or "").strip()
    model = str(getattr(slot_cfg, "model", "") or "").strip()
    is_loopback = _is_loopback_base_url(base_url)

    if not model:
        return {
            "slot": slot_name,
            "ok": False,
            "model": model,
            "base_url": base_url,
            "error": f"{slot_name}.model 未配置",
        }
    if not api_key and not is_loopback:
        return {
            "slot": slot_name,
            "ok": False,
            "model": model,
            "base_url": base_url,
            "error": f"{slot_name}.api_key 未配置且非本地回环地址",
        }

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply OK"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    timeout_sec = min(float(getattr(slot_cfg, "timeout_seconds", 15.0) or 15.0), 15.0)
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec)) as client:
            resp = await client.post(url, json=body, headers=headers)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code >= 400:
            return {
                "slot": slot_name,
                "ok": False,
                "model": model,
                "base_url": base_url,
                "latency_ms": latency_ms,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
        return {
            "slot": slot_name,
            "ok": True,
            "model": model,
            "base_url": base_url,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "slot": slot_name,
            "ok": False,
            "model": model,
            "base_url": base_url,
            "latency_ms": latency_ms,
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_doctor(
    *,
    db_path: Path | str = Path("data/shuati.db"),
    config_dir: Path | str = Path("config"),
    check_llm_api: bool = False,
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """冷启动与环境健康自检（不依赖已存在的 SQLite 数据库）。"""
    cfg_dir = Path(config_dir)
    db_p = Path(db_path)
    settings: Settings = load_settings(cfg_dir / "settings.yaml")
    use_mock_flag = bool(use_mock) if use_mock is not None else bool(settings.pipeline.use_mock_llm)

    warnings: list[str] = []
    errors: list[str] = []
    checks: dict[str, Any] = {}

    # 1. Python 环境与核心依赖
    required_pkgs = ("sympy", "pydantic", "httpx", "yaml", "typer", "PIL", "numpy")
    missing_pkgs: list[str] = []
    for pkg in required_pkgs:
        try:
            importlib.import_module(pkg)
        except Exception:
            missing_pkgs.append(pkg)
    py_ok = len(missing_pkgs) == 0
    checks["python_env"] = {
        "ok": py_ok,
        "python_version": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "missing_packages": missing_pkgs,
    }
    if not py_ok:
        errors.append(f"python_env 缺少核心依赖包: {', '.join(missing_pkgs)}")

    # 2. SymPy 子进程沙盒小算式（2 + 3 == 5）
    try:
        sym_res = run_verify_script(
            "def verify():\n    return {'主问': str(2 + 3)}\n",
            timeout_seconds=float(settings.pipeline.sympy_timeout_seconds),
            memory_limit_mb=int(settings.pipeline.sympy_memory_limit_mb),
        )
        sym_ok = bool(sym_res.ok and sym_res.status == "PASSED" and (sym_res.values or {}).get("主问") == "5")
        checks["sympy_sandbox"] = {
            "ok": sym_ok,
            "status": sym_res.status,
            "values": sym_res.values,
            "stderr_tail": sym_res.stderr_tail,
        }
        if not sym_ok:
            errors.append(f"sympy_sandbox 自检失败 (status={sym_res.status}): {sym_res.stderr_tail}")
    except Exception as exc:
        checks["sympy_sandbox"] = {"ok": False, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
        errors.append(f"sympy_sandbox 执行异常: {exc}")

    # 3. 三层提示词接缝 lint_prompt_seams
    try:
        lint_res = lint_prompt_seams(config_dir=cfg_dir)
        seams_ok = bool(lint_res.get("ok"))
        checks["prompt_seams"] = {
            "ok": seams_ok,
            "checked_files_count": len(lint_res.get("checked_files") or []),
            "errors": lint_res.get("errors") or [],
            "warnings": lint_res.get("warnings") or [],
        }
        for w in lint_res.get("warnings") or []:
            if isinstance(w, dict):
                warnings.append(f"prompt_seams[{w.get('file', '')}]: {w.get('message', '')}")
        if not seams_ok:
            errors.append(f"prompt_seams 静态检查失败: {lint_res.get('errors')}")
    except Exception as exc:
        checks["prompt_seams"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        errors.append(f"prompt_seams 执行异常: {exc}")

    # 4. ingest.roots 与 collections.yaml
    roots_cfg = list(settings.ingest.roots or ["题库"])
    roots_detail: list[dict[str, Any]] = []
    total_problems_files = 0
    for r_str in roots_cfg:
        rp = Path(r_str)
        exists = rp.exists()
        p_count = 0
        if exists:
            if rp.is_file():
                p_count = 1
            elif rp.is_dir():
                p_count = len(list(rp.rglob("problems.jsonl")))
        total_problems_files += p_count
        roots_detail.append(
            {
                "root": r_str,
                "exists": exists,
                "problems_files": p_count,
            }
        )
        if not exists:
            warnings.append(f"ingest.roots 目录尚不存在: {r_str}")
        elif p_count == 0:
            warnings.append(f"ingest.roots 目录下未发现 problems.jsonl: {r_str}")

    col_yaml = cfg_dir / "collections.yaml"
    col_exists = col_yaml.is_file()
    col_rules_count = 0
    if col_exists:
        try:
            col_data = yaml.safe_load(col_yaml.read_text(encoding="utf-8")) or {}
            if isinstance(col_data, dict) and isinstance(col_data.get("rules"), list):
                col_rules_count = len(col_data["rules"])
        except Exception as exc:
            warnings.append(f"collections.yaml 解析失败: {exc}")
    else:
        warnings.append(f"缺少 collections.yaml: {col_yaml.as_posix()}")

    checks["ingest_roots"] = {
        "ok": all(item["exists"] for item in roots_detail) and col_exists,
        "roots": roots_detail,
        "total_problems_files": total_problems_files,
        "dedup_near_default": bool(settings.ingest.dedup_near_default),
        "collections_yaml_exists": col_exists,
        "collections_rules_count": col_rules_count,
    }

    # 5. Embedding 真实向量维度验证（无需 SQLite 库）
    async def _probe_embedding() -> dict[str, Any]:
        client = EmbeddingClient(settings, conn=None, writer=None, use_mock=use_mock_flag)
        t0 = time.perf_counter()
        try:
            vecs = await client.embed_texts(["三角形面积与比例模型"], stage="DOCTOR_SMOKE")
            latency_ms = int((time.perf_counter() - t0) * 1000)
            valid = (
                isinstance(vecs, np.ndarray)
                and vecs.ndim == 2
                and vecs.shape[0] == 1
                and vecs.shape[1] > 0
                and bool(np.isfinite(vecs).all())
            )
            return {
                "ok": valid,
                "provider": settings.llm.embedding.provider,
                "model": settings.llm.embedding.model,
                "base_url": settings.llm.embedding.base_url,
                "use_mock": use_mock_flag,
                "dimension": int(vecs.shape[1]) if vecs.ndim == 2 else 0,
                "latency_ms": latency_ms,
            }
        finally:
            await client.aclose()

    try:
        emb_res = _run_async_sync(_probe_embedding())
        checks["embedding"] = emb_res
        if not emb_res.get("ok"):
            errors.append(f"embedding 返回无效向量形状或非有限值: {emb_res}")
    except Exception as exc:
        checks["embedding"] = {
            "ok": False,
            "provider": settings.llm.embedding.provider,
            "model": settings.llm.embedding.model,
            "base_url": settings.llm.embedding.base_url,
            "use_mock": use_mock_flag,
            "dimension": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        errors.append(f"embedding 自检失败: {exc}")

    # 6. LLM API 槽位探测（仅 --check-llm-api 时发起真实网络探针）
    slot_summary = {
        slot_name: {
            "model": getattr(settings.llm, slot_name).model,
            "base_url": getattr(settings.llm, slot_name).base_url,
            "has_api_key_or_loopback": bool(
                (getattr(settings.llm, slot_name).api_key or "").strip()
                or _is_loopback_base_url(getattr(settings.llm, slot_name).base_url)
            ),
        }
        for slot_name in ("vlm_primary", "vlm_secondary", "text_llm", "judge_llm")
    }
    if not check_llm_api:
        checks["llm_api"] = {
            "ok": True,
            "checked": False,
            "skipped": True,
            "subagent_default_model": settings.subagent.default_model,
            "slots": slot_summary,
        }
    elif use_mock_flag:
        checks["llm_api"] = {
            "ok": True,
            "checked": True,
            "use_mock": True,
            "subagent_default_model": settings.subagent.default_model,
            "slots": slot_summary,
        }
    else:

        async def _probe_all_llm() -> dict[str, Any]:
            probes = await asyncio.gather(
                _probe_llm_slot_api("text_llm", settings.llm.text_llm),
                _probe_llm_slot_api("vlm_primary", settings.llm.vlm_primary),
            )
            return {p["slot"]: p for p in probes}

        probe_map = _run_async_sync(_probe_all_llm())
        llm_ok = all(bool(v.get("ok")) for v in probe_map.values())
        checks["llm_api"] = {
            "ok": llm_ok,
            "checked": True,
            "use_mock": False,
            "subagent_default_model": settings.subagent.default_model,
            "probes": probe_map,
        }
        if not llm_ok:
            for sname, pinfo in probe_map.items():
                if not pinfo.get("ok"):
                    errors.append(f"llm_api[{sname}] 探测失败: {pinfo.get('error')}")

    return {
        "ok": len(errors) == 0,
        "db_exists": db_p.exists(),
        "db_path": db_p.as_posix(),
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }


def sample_anchor_candidates(
    *,
    db_path: Path | str = Path("data/shuati.db"),
    grade_subject: str = "小学奥数",
    count: int = 6,
) -> dict[str, Any]:
    """只读分层抽样候选黄金难度锚点题目（跨长度分位与有图/无图，严禁输出 raw_chapter）。"""
    if count < 1:
        raise PreconditionError(f"--count 必须为正整数，收到 {count}")

    db_p = Path(db_path)
    if not db_p.exists():
        raise PreconditionError(f"数据库文件不存在: {db_p.as_posix()}，请先执行 shuati ingest")

    with closing(get_connection(db_p, readonly=True)) as conn:
        gs_row = conn.execute(
            "SELECT grade_subject FROM grade_subjects WHERE grade_subject = ?",
            (grade_subject,),
        ).fetchone()
        if gs_row is None:
            raise PreconditionError(f"学段学科 '{grade_subject}' 未在数据库中注册")

        rows = conn.execute(
            """
            SELECT
                q.question_uid,
                rq.source_id,
                rq.raw_chapter,
                q.has_images,
                q.cas_images_json,
                q.eff_format,
                q.eff_verification_status,
                q.eff_final_answers_json,
                q.eff_question_text,
                LENGTH(q.eff_question_text) AS char_length
            FROM questions q
            JOIN raw_questions rq ON rq.question_uid = q.question_uid
            WHERE q.grade_subject = ?
              AND q.is_retired = 0
              AND q.eff_is_excluded = 0
            ORDER BY q.question_uid ASC
            """,
            (grade_subject,),
        ).fetchall()

    total_pool = len(rows)
    if total_pool == 0:
        return {
            "grade_subject": grade_subject,
            "total_pool": 0,
            "count": 0,
            "candidates": [],
        }

    records: list[dict[str, Any]] = []
    for r in rows:
        try:
            cas_imgs = [str(x) for x in (json.loads(r["cas_images_json"] or "[]") or [])]
        except Exception:
            cas_imgs = []
        try:
            final_ans = json.loads(r["eff_final_answers_json"] or "[]")
            if not isinstance(final_ans, list):
                final_ans = []
        except Exception:
            final_ans = []
        q_text = str(r["eff_question_text"] or "")
        records.append(
            {
                "question_uid": str(r["question_uid"]),
                "source_id": str(r["source_id"]),
                "_raw_chapter": str(r["raw_chapter"] or "").strip(),
                "has_images": bool(r["has_images"] or len(cas_imgs) > 0),
                "cas_images": cas_imgs,
                "eff_format": str(r["eff_format"] or ""),
                "eff_verification_status": str(r["eff_verification_status"] or ""),
                "eff_final_answers": final_ans,
                "char_length": int(r["char_length"] or len(q_text)),
                "question_text": q_text,
            }
        )

    records.sort(key=lambda item: (item["char_length"], item["question_uid"]))
    target_n = min(count, total_pool)

    if total_pool <= target_n:
        selected = records
    else:
        num_buckets = target_n
        picked_indices: list[int] = []
        used: set[int] = set()
        seen_chapters: set[str] = set()
        pool_has_img = any(item["has_images"] for item in records)
        pool_no_img = any(not item["has_images"] for item in records)
        got_img = False
        got_no_img = False

        for b in range(num_buckets):
            start = (b * total_pool) // num_buckets
            end = ((b + 1) * total_pool) // num_buckets
            bucket_indices = [i for i in range(start, max(start + 1, end)) if i not in used]
            if not bucket_indices:
                bucket_indices = [i for i in range(total_pool) if i not in used]
            if not bucket_indices:
                break

            mid = (start + max(start, end - 1)) / 2.0

            def _score(idx: int) -> tuple[int, int, int, float, str]:
                rec = records[idx]
                chap = rec["_raw_chapter"]
                # 优先满足：1. 覆盖缺失的图片极性（含图/无图）；2. 新章节；3. 靠近分位桶中心
                need_img_bonus = 0
                if pool_has_img and not got_img and rec["has_images"]:
                    need_img_bonus = 1
                elif pool_no_img and not got_no_img and not rec["has_images"]:
                    need_img_bonus = 1
                new_chap_bonus = 1 if (chap and chap not in seen_chapters) else 0
                # 尽量分散分位
                return (
                    need_img_bonus,
                    new_chap_bonus,
                    1 if rec["eff_verification_status"] == "VERIFIED_CONSENSUS" else 0,
                    -abs(idx - mid),
                    rec["question_uid"],
                )

            best_idx = max(bucket_indices, key=_score)
            picked_indices.append(best_idx)
            used.add(best_idx)
            best_rec = records[best_idx]
            if best_rec["_raw_chapter"]:
                seen_chapters.add(best_rec["_raw_chapter"])
            if best_rec["has_images"]:
                got_img = True
            else:
                got_no_img = True

        picked_indices.sort(key=lambda idx: (records[idx]["char_length"], records[idx]["question_uid"]))
        selected = [records[idx] for idx in picked_indices]

    candidates = [
        {
            "question_uid": rec["question_uid"],
            "source_id": rec["source_id"],
            "has_images": rec["has_images"],
            "cas_images": rec["cas_images"],
            "eff_format": rec["eff_format"],
            "eff_verification_status": rec["eff_verification_status"],
            "eff_final_answers": rec["eff_final_answers"],
            "char_length": rec["char_length"],
            "question_text": rec["question_text"],
        }
        for rec in selected
    ]
    return {
        "grade_subject": grade_subject,
        "total_pool": total_pool,
        "count": len(candidates),
        "candidates": candidates,
    }


def reset_human_workspace(
    *,
    learner_id: str = "default",
    learner_name: str = "默认学习者",
    subject: str = "小学奥数",
    archive_dir: Path | str = Path("data/backups"),
    human_dir: Path | str | None = None,
    db_path: Path | str = Path("data/shuati.db"),
    config_dir: Path | str = Path("config"),
    dry_run: bool = False,
) -> dict[str, Any]:
    """无损归档旧 human/*.jsonl（及备份存在库），重置为指定单一学习者的干净起跑态。"""
    clean_lid = (learner_id or "").strip()
    if not clean_lid:
        raise PreconditionError("--learner-id 不能为空")
    clean_name = (learner_name or "").strip() or clean_lid
    clean_subject = (subject or "").strip() or "小学奥数"

    arch_dir = Path(archive_dir)
    db_p = Path(db_path)
    cfg_dir = Path(config_dir)
    settings = load_settings(cfg_dir / "settings.yaml")
    h_dir = Path(human_dir) if human_dir is not None else Path(settings.human_mirror.dir)

    existing_jsonl = sorted(h_dir.glob("*.jsonl")) if h_dir.is_dir() else []
    non_empty_jsonl = [p for p in existing_jsonl if p.stat().st_size > 0]

    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    archive_subdir = arch_dir / f"seed_human_{ts}"
    suffix_idx = 1
    while archive_subdir.exists():
        archive_subdir = arch_dir / f"seed_human_{ts}_{suffix_idx:02d}"
        suffix_idx += 1

    if dry_run:
        return {
            "dry_run": True,
            "learner_id": clean_lid,
            "learner_name": clean_name,
            "subject": clean_subject,
            "human_dir": h_dir.as_posix(),
            "db_exists": db_p.exists(),
            "db_path": db_p.as_posix(),
            "would_archive_dir": archive_subdir.as_posix() if non_empty_jsonl else None,
            "would_archive_files": [p.as_posix() for p in non_empty_jsonl],
        }

    def _archive_existing_jsonl() -> tuple[str | None, list[str]]:
        if not non_empty_jsonl:
            return None, []
        archive_subdir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for src in non_empty_jsonl:
            dst = archive_subdir / src.name
            shutil.copy2(src, dst)
            copied.append(dst.as_posix())
        return archive_subdir.as_posix(), copied

    archived_files: list[str] = []
    archived_path_str: str | None = None
    db_backup_path: str | None = None
    cleared_counts: dict[str, int] = {}

    if db_p.exists():
        from shuati.pipeline.stage0_ingest import _promote_orphan

        lock_path = db_p.parent / ".compile.lock"
        writer = SingleWriterExecutor()
        with CompileLock(lock_path), closing(get_connection(db_p)) as conn:
            init_db(conn, config_dir=cfg_dir)
            effective.install_eff_write_guard(conn)

            # 必须在创建归档目录与拷贝文件前校验全部前置条件，确保失败零磁盘残留
            gs_exists = conn.execute(
                "SELECT 1 FROM grade_subjects WHERE grade_subject = ?",
                (clean_subject,),
            ).fetchone()
            if gs_exists is None:
                raise PreconditionError(
                    f"学段学科 '{clean_subject}' 未在数据库中注册，请先使用 'shuati grade-subject add' 注册"
                )

            archived_path_str, archived_files = _archive_existing_jsonl()
            backup_file = human_mirror.vacuum_backup(
                conn,
                arch_dir,
                "human_reset",
                keep=int(settings.human_mirror.backups_keep),
            )
            db_backup_path = backup_file.as_posix()

            with writer.transaction(conn):
                affected_uids = {
                    str(r["question_uid"])
                    for r in conn.execute("SELECT DISTINCT question_uid FROM human_overrides;").fetchall()
                }
                ext_uids = [
                    str(r["question_uid"])
                    for r in conn.execute(
                        "SELECT question_uid FROM raw_questions WHERE source_type = 'EXTERNAL_MISTAKE';"
                    ).fetchall()
                ]
                ext_set = set(ext_uids)

                cleared_counts["practice_log"] = int(
                    conn.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0]
                )
                cleared_counts["quiz_batches"] = int(
                    conn.execute("SELECT COUNT(*) FROM quiz_batches;").fetchone()[0]
                )
                cleared_counts["relation_overrides"] = int(
                    conn.execute("SELECT COUNT(*) FROM relation_overrides;").fetchone()[0]
                )
                cleared_counts["human_overrides"] = int(
                    conn.execute("SELECT COUNT(*) FROM human_overrides;").fetchone()[0]
                )
                cleared_counts["learners"] = int(
                    conn.execute("SELECT COUNT(*) FROM learners;").fetchone()[0]
                )
                cleared_counts["external_questions"] = len(ext_uids)

                conn.execute("DELETE FROM practice_log WHERE voids_log_id IS NOT NULL;")
                conn.execute("DELETE FROM practice_log;")
                conn.execute("DELETE FROM quiz_batches;")
                conn.execute("DELETE FROM relation_overrides;")
                conn.execute("DELETE FROM human_overrides;")
                conn.execute("DELETE FROM learners;")

                if ext_uids:
                    ph = ",".join("?" for _ in ext_uids)
                    # 孤儿晋升：将挂载在待删 EXTERNAL_MISTAKE 下的 BATCH_JSONL 副本晋升或重指新主行
                    orphan_rows = conn.execute(
                        f"""
                        SELECT question_uid, grade_subject, raw_question, cas_images_json,
                               image_captions_json, legacy_tags_json, content_hash
                        FROM raw_questions
                        WHERE duplicate_of_uid IN ({ph})
                          AND source_type != 'EXTERNAL_MISTAKE'
                        ORDER BY ingested_at ASC, question_uid ASC;
                        """,
                        ext_uids,
                    ).fetchall()
                    grouped_orphans: dict[tuple[str, str], list[sqlite3.Row]] = {}
                    for orow in orphan_rows:
                        grouped_orphans.setdefault(
                            (str(orow["grade_subject"]), str(orow["content_hash"])), []
                        ).append(orow)

                    promoted_orphans = 0
                    for (o_gs, o_hash), group in grouped_orphans.items():
                        live_master = conn.execute(
                            """
                            SELECT question_uid
                            FROM raw_questions
                            WHERE grade_subject = ? AND content_hash = ?
                              AND duplicate_of_uid IS NULL
                              AND is_missing_from_source = 0
                              AND source_type != 'EXTERNAL_MISTAKE'
                            ORDER BY ingested_at ASC, question_uid ASC
                            LIMIT 1;
                            """,
                            (o_gs, o_hash),
                        ).fetchone()
                        if live_master is not None:
                            new_master_uid = str(live_master["question_uid"])
                            for dup_row in group:
                                conn.execute(
                                    "UPDATE raw_questions SET duplicate_of_uid = ?, updated_at = datetime('now') WHERE question_uid = ?;",
                                    (new_master_uid, dup_row["question_uid"]),
                                )
                        else:
                            first_orphan = group[0]
                            _promote_orphan(conn, first_orphan, o_gs)
                            promoted_orphans += 1
                            new_master_uid = str(first_orphan["question_uid"])
                            for dup_row in group[1:]:
                                conn.execute(
                                    "UPDATE raw_questions SET duplicate_of_uid = ?, updated_at = datetime('now') WHERE question_uid = ?;",
                                    (new_master_uid, dup_row["question_uid"]),
                                )
                    cleared_counts["promoted_orphans"] = promoted_orphans

                    conn.execute(f"DELETE FROM questions_fts WHERE question_uid IN ({ph});", ext_uids)
                    conn.execute(
                        f"UPDATE raw_questions SET duplicate_of_uid = NULL WHERE duplicate_of_uid IN ({ph});",
                        ext_uids,
                    )
                    conn.execute(f"DELETE FROM questions WHERE question_uid IN ({ph});", ext_uids)
                    conn.execute("DELETE FROM raw_questions WHERE source_type = 'EXTERNAL_MISTAKE';")

                conn.execute(
                    """
                    INSERT INTO learners(learner_id, display_name, default_grade_subject, is_current, notes)
                    VALUES (?, ?, ?, 1, '')
                    """,
                    (clean_lid, clean_name, clean_subject),
                )

                for uid in sorted(affected_uids - ext_set):
                    exists = conn.execute(
                        "SELECT 1 FROM questions WHERE question_uid = ?",
                        (uid,),
                    ).fetchone()
                    if exists is not None:
                        effective.recompute(conn, uid)

            human_mirror.export_snapshot(conn, human_dir=h_dir, include_practice_log=True)
    else:
        archived_path_str, archived_files = _archive_existing_jsonl()
        h_dir.mkdir(parents=True, exist_ok=True)
        now_sql = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        learner_row = {
            "created_at": now_sql,
            "default_grade_subject": clean_subject,
            "display_name": clean_name,
            "is_current": 1,
            "learner_id": clean_lid,
            "notes": "",
        }
        _atomic_write_text_file(
            h_dir / "learners.jsonl",
            json.dumps(learner_row, ensure_ascii=False, sort_keys=True) + "\n",
        )
        for empty_name in (
            "external_questions.jsonl",
            "overrides.jsonl",
            "relation_overrides.jsonl",
            "practice_log.jsonl",
        ):
            _atomic_write_text_file(h_dir / empty_name, "")

    written_files = [
        (h_dir / fname).as_posix()
        for fname in (
            "external_questions.jsonl",
            "learners.jsonl",
            "overrides.jsonl",
            "relation_overrides.jsonl",
            "practice_log.jsonl",
        )
    ]
    return {
        "dry_run": False,
        "learner_id": clean_lid,
        "learner_name": clean_name,
        "subject": clean_subject,
        "human_dir": h_dir.as_posix(),
        "archived_dir": archived_path_str,
        "archived_files": archived_files,
        "db_exists": db_p.exists(),
        "db_backup_path": db_backup_path,
        "cleared_counts": cleared_counts,
        "written_files": written_files,
    }
