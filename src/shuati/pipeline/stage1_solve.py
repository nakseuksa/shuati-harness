"""Stage 1：视觉转译 + 双盲双 VLM + 分小问验真 + 一题多解物化（DESIGN.md §4.3）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from shuati.config import (
    Settings,
    load_settings,
    resolve_subagent_model,
    validate_subagent_model_id,
)
from shuati.core import effective, refresh
from shuati.db import repository
from shuati.db.connection import (
    CompileLock,
    PreconditionError,
    SingleWriterExecutor,
    get_connection,
)
from shuati.db.schema import init_db
from shuati.llm.gateway import LLMGateway, LLMGatewayError
from shuati.llm.prompt_builder import (
    STAGE1_PROMPT_VERSION,
    build_multi_solution_judge_messages,
    build_qualitative_judge_messages,
    build_stage1_messages,
)
from shuati.llm.seam_loader import load_prompt_seams
from shuati.models.domain import AnswerPart
from shuati.models.schemas import (
    MultiSolutionJudgeOutput,
    QualitativeJudgeOutput,
    Stage1Input,
    Stage1PrimaryOutput,
    Stage1SecondaryOutput,
    Stage1SubagentBatchOutput,
)
from shuati.pipeline.subagent_bridge import (
    _atomic_write_text,
    export_task_batches,
    load_and_validate_results,
)
from shuati.verify.answer_parts import normalize_answer_parts
from shuati.verify.equivalence import EquivalenceSummary, compare_answer_parts
from shuati.verify.sympy_sandbox import SympySandboxResult, run_verify_script


@dataclass
class Stage1BatchResult:
    """Stage 1 批处理执行汇总结果。"""

    total_candidates: int
    processed: int
    skipped_ok: int
    skipped_human_verified: int
    verified_consensus: int
    needs_review: int
    failed: int
    multi_solution_count: int
    errors: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "api"
    exported_questions: int = 0
    batch_count: int = 0
    subagent_model: str = ""
    manifest: dict[str, Any] | None = None
    ingested: int = 0
    skipped_locked: int = 0
    batch_errors: int = 0
    manifest_status: str = ""
    retry_file: str | None = None


def compute_stage1_input_hash(
    *,
    eff_question_text: str,
    cas_images: list[str],
    image_captions: list[str],
    vlm_primary_model: str,
    vlm_secondary_model: str,
    dual_vlm_policy: str,
    prompt_version: str,
) -> str:
    """计算 Stage 1 输入指纹（严禁包含 question_uid 与 source_collection）。"""
    payload = json.dumps(
        {
            "eff_question_text": eff_question_text,
            "cas_images": cas_images,
            "image_captions": image_captions,
            "vlm_primary_model": vlm_primary_model,
            "vlm_secondary_model": vlm_secondary_model,
            "dual_vlm_policy": dual_vlm_policy,
            "prompt_version": prompt_version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compare_parts_sync(
    parts_a: list[AnswerPart],
    parts_b: list[AnswerPart],
    *,
    grade_subject: str,
    allow_pi_314_subjects: list[str],
    options: dict[str, str] | None,
    timeout_seconds: float,
    memory_limit_mb: int,
) -> EquivalenceSummary:
    """同步上下文下安全调用 compare_answer_parts（兼容已有事件循环）。"""
    coro = compare_answer_parts(
        parts_a,
        parts_b,
        grade_subject=grade_subject,
        allow_pi_314_subjects=allow_pi_314_subjects,
        options=options,
        sympy_result=None,
        qualitative_judge=None,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    )
    try:
        asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        return asyncio.run(coro)


def _parts_structurally_equal(parts_a: list[AnswerPart], parts_b: list[AnswerPart]) -> bool:
    """快速判定两份归一化 AnswerPart 列表是否完全一致。"""
    return [p.model_dump() for p in parts_a] == [p.model_dump() for p in parts_b]


def _mark_exhausted_failure(
    conn: sqlite3.Connection,
    uid: str,
    result: Stage1BatchResult,
) -> None:
    """达到重试上限后追加 RUN_FAILED_EXHAUSTED；仅当生效态确实为 NEEDS_REVIEW 时阻断 S2。"""
    _append_review_reason(conn, uid, "RUN_FAILED_EXHAUSTED")
    effective.recompute(conn, uid)
    eff_row = conn.execute(
        "SELECT eff_verification_status FROM questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    if eff_row and eff_row["eff_verification_status"] == "NEEDS_REVIEW":
        conn.execute(
            """
            INSERT INTO compile_state(question_uid, stage, status, updated_at)
            VALUES (?, 'S2', 'BLOCKED', datetime('now'))
            ON CONFLICT(question_uid, stage) DO UPDATE SET
                status = 'BLOCKED',
                updated_at = datetime('now')
            """,
            (uid,),
        )
        result.needs_review += 1


def _append_review_reason(conn: sqlite3.Connection, question_uid: str, code: str) -> None:
    """向 questions.review_reason 追加原因码并把 m_verification_status 置为 NEEDS_REVIEW。"""
    row = conn.execute(
        "SELECT review_reason FROM questions WHERE question_uid = ?",
        (question_uid,),
    ).fetchone()
    if not row:
        return
    existing = [r.strip() for r in (row["review_reason"] or "").split(",") if r.strip()]
    if code not in existing:
        existing.append(code)
    conn.execute(
        """
        UPDATE questions
        SET m_verification_status = 'NEEDS_REVIEW',
            review_reason = ?,
            updated_at = datetime('now')
        WHERE question_uid = ?
        """,
        (",".join(existing), question_uid),
    )


def materialize_solutions(
    conn: sqlite3.Connection,
    question_uid: str,
    *,
    allow_pi_314_subjects: list[str] | None = None,
) -> None:
    """幂等重建 question_solutions：

    - 若存在激活且未挂起的 solutions_override_json 覆写，直接按自包含覆写重建（source_origin='HUMAN'）；
    - 否则按最新 OK 的 PRIMARY（及一题多解时 SECONDARY）stage1_vlm_runs 重建；
    - 答案—解法一致性校验：若机器 branch_1 声称答案与 eff_final_answers_json 不等价，
      追加 ANSWER_SOLUTION_CONFLICT。
    """
    q_row = conn.execute(
        """
        SELECT q.question_uid, q.grade_subject, q.eff_final_answers_json, q.eff_options_json,
               r.content_hash
        FROM questions q
        JOIN raw_questions r ON q.question_uid = r.question_uid
        WHERE q.question_uid = ?
        """,
        (question_uid,),
    ).fetchone()
    if not q_row:
        return

    current_hash = q_row["content_hash"]
    ov_row = conn.execute(
        """
        SELECT override_value, base_fingerprint, is_suspended
        FROM human_overrides
        WHERE question_uid = ?
          AND field_name = 'solutions_override_json'
          AND is_active = 1
        LIMIT 1
        """,
        (question_uid,),
    ).fetchone()

    conn.execute("DELETE FROM question_solutions WHERE question_uid = ?", (question_uid,))

    if (
        ov_row is not None
        and ov_row["is_suspended"] == 0
        and ov_row["base_fingerprint"] == current_hash
    ):
        payload = json.loads(ov_row["override_value"] or "{}")
        branches = payload.get("branches", []) if isinstance(payload, dict) else []
        for idx, branch in enumerate(branches):
            if not isinstance(branch, dict):
                continue
            branch_id = str(branch.get("branch_id") or f"branch_{idx + 1}")
            is_optimal = int(branch.get("is_optimal", 1 if idx == 0 else 0))
            origin_run_id = branch.get("origin_run_id")
            method_summary = str(branch.get("method_summary") or "")
            solution_markdown = str(branch.get("solution_markdown") or "")
            sympy_code = branch.get("sympy_code")
            sympy_verified = int(bool(branch.get("sympy_verified", 0)))
            conn.execute(
                """
                INSERT INTO question_solutions(
                    solution_id, question_uid, branch_id, is_optimal, source_origin,
                    origin_run_id, method_summary, solution_markdown, sympy_code, sympy_verified
                ) VALUES (?, ?, ?, ?, 'HUMAN', ?, ?, ?, ?, ?)
                """,
                (
                    f"{question_uid}::{branch_id}",
                    question_uid,
                    branch_id,
                    is_optimal,
                    origin_run_id,
                    method_summary,
                    solution_markdown,
                    sympy_code,
                    sympy_verified,
                ),
            )
        return

    p_run = conn.execute(
        """
        SELECT run_id, method_summary, solution_markdown, final_answers_json,
               sympy_code, sympy_status
        FROM stage1_vlm_runs
        WHERE question_uid = ? AND role = 'PRIMARY' AND run_status = 'OK'
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (question_uid,),
    ).fetchone()

    if p_run is None:
        return

    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin,
            origin_run_id, method_summary, solution_markdown, sympy_code, sympy_verified
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', ?, ?, ?, ?, ?)
        """,
        (
            f"{question_uid}::branch_1",
            question_uid,
            p_run["run_id"],
            p_run["method_summary"],
            p_run["solution_markdown"],
            p_run["sympy_code"],
            1 if p_run["sympy_status"] == "PASSED" else 0,
        ),
    )

    s_run = conn.execute(
        """
        SELECT run_id, method_summary, solution_markdown, sympy_output_json
        FROM stage1_vlm_runs
        WHERE question_uid = ? AND role = 'SECONDARY' AND run_status = 'OK'
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (question_uid,),
    ).fetchone()

    if s_run is not None and s_run["sympy_output_json"]:
        try:
            meta = json.loads(s_run["sympy_output_json"])
        except Exception:
            meta = {}
        if (
            isinstance(meta, dict)
            and meta.get("is_distinct_method") is True
            and meta.get("primary_run_id") == p_run["run_id"]
        ):
            conn.execute(
                """
                INSERT INTO question_solutions(
                    solution_id, question_uid, branch_id, is_optimal, source_origin,
                    origin_run_id, method_summary, solution_markdown, sympy_code, sympy_verified
                ) VALUES (?, ?, 'branch_2', 0, 'SECONDARY_VLM', ?, ?, ?, NULL, 0)
                """,
                (
                    f"{question_uid}::branch_2",
                    question_uid,
                    s_run["run_id"],
                    s_run["method_summary"],
                    s_run["solution_markdown"],
                ),
            )

    # 答案—解法一致性校验：若机器 branch_1 声称答案与 eff_final_answers_json 不等价，追加 ANSWER_SOLUTION_CONFLICT
    eff_raw = q_row["eff_final_answers_json"] or "[]"
    try:
        eff_list = json.loads(eff_raw)
        run_list = json.loads(p_run["final_answers_json"] or "[]")
    except Exception:
        return

    if not eff_list or not run_list:
        return

    try:
        eff_parts = normalize_answer_parts([AnswerPart.model_validate(x) for x in eff_list])
        run_parts = normalize_answer_parts([AnswerPart.model_validate(x) for x in run_list])
    except Exception:
        _append_review_reason(conn, question_uid, "ANSWER_SOLUTION_CONFLICT")
        return

    if _parts_structurally_equal(run_parts, eff_parts):
        return

    try:
        options = json.loads(q_row["eff_options_json"] or "{}")
    except Exception:
        options = {}
    subjects = allow_pi_314_subjects if allow_pi_314_subjects is not None else ["小学奥数"]
    summary = _compare_parts_sync(
        run_parts,
        eff_parts,
        grade_subject=q_row["grade_subject"],
        allow_pi_314_subjects=subjects,
        options=options or None,
        timeout_seconds=5.0,
        memory_limit_mb=512,
    )
    if not summary.all_dual_equal:
        _append_review_reason(conn, question_uid, "ANSWER_SOLUTION_CONFLICT")


def _load_stress_test_25_uids(config_dir: Path) -> list[str]:
    """从 config/stress_test_25.yaml 读取 25 题压力测试清单。"""
    cfg_path = config_dir / "stress_test_25.yaml"
    if not cfg_path.exists():
        raise PreconditionError(f"未找到 25 题压测清单配置: {cfg_path}")
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    uids: list[str] = []
    for group in data.get("groups", []):
        for uid in group.get("question_uids", []):
            if uid not in uids:
                uids.append(str(uid))
    return uids


def _resolve_paths(
    db_path: Path,
    assets_dir: Path | None,
) -> tuple[Path, Path]:
    """解析 CAS 图片目录与 LLM 原始响应落盘目录。"""
    if assets_dir is None:
        if db_path.name == "shuati.db" and db_path.parent.name == "data":
            resolved_assets = Path("data/assets/images")
        else:
            resolved_assets = db_path.parent / "assets" / "images"
    else:
        resolved_assets = Path(assets_dir)

    if db_path.name == "shuati.db" and db_path.parent.name == "data":
        resolved_llm_raw = Path("data/llm_raw")
    else:
        resolved_llm_raw = db_path.parent / "llm_raw"

    return resolved_assets, resolved_llm_raw


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


async def _run_sympy_for_parts(
    parts: list[AnswerPart],
    sympy_code: str | None,
    *,
    timeout_seconds: float,
    memory_limit_mb: int,
) -> SympySandboxResult | None:
    """若存在 SYMBOLIC 小问则执行沙盒验算。"""
    has_symbolic = any(p.verify_mode == "SYMBOLIC" for p in parts)
    if not has_symbolic:
        return None
    if not sympy_code or not sympy_code.strip():
        return SympySandboxResult(
            ok=False,
            status="FAILED",
            values=None,
            stderr_tail="Missing sympy_code for SYMBOLIC AnswerPart",
        )
    return await asyncio.to_thread(
        run_verify_script,
        sympy_code,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    )


def _resolve_stage1_seam_meta(
    config_dir: Path,
    grade_subject: str,
    prompt_profile: str | None,
) -> tuple[str, str]:
    if (config_dir / "prompts" / "default.yaml").is_file() or (
        prompt_profile is not None and prompt_profile.strip()
    ):
        bundle = load_prompt_seams(
            config_dir=config_dir,
            grade_subject=grade_subject,
            profile=prompt_profile,
        )
        return (
            bundle.prompt_versions.get("stage1", STAGE1_PROMPT_VERSION),
            bundle.prompt_digest,
        )
    return STAGE1_PROMPT_VERSION, ""


def _build_stage1_batch_messages(
    chunk_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将同批 Stage 1 题目组装为 Stage1SubagentBatchOutput 脱敏批处理消息。"""
    base_system = str(chunk_items[0]["messages"][0]["content"])
    batch_system = (
        f"{base_system}\n\n"
        "【批处理包装要求（Stage1SubagentBatchOutput）】\n"
        "本批次包含一道或多道独立题目（每题以 `task_item_id` 唯一标识）。\n"
        '请严格输出单个合法 JSON 对象：`{"items": [ <每题对应的 Stage1SubagentItem 对象，原样回填 task_item_id，且 visual_facts 必须排在每题对象首位> ]}`，'
        "确保本批次全部 `task_item_id` 100% 覆盖且无任何遗漏。"
    )

    has_images = any(isinstance(it["messages"][1]["content"], list) for it in chunk_items)
    if not has_images:
        blocks = [
            f"### 批次题目 {idx}（task_item_id={it['task_item_id']}）\n\n{it['messages'][1]['content']}"
            for idx, it in enumerate(chunk_items, start=1)
        ]
        return [
            {"role": "system", "content": batch_system},
            {"role": "user", "content": "\n\n========================================\n\n".join(blocks)},
        ]

    merged_parts: list[dict[str, Any]] = []
    for idx, it in enumerate(chunk_items, start=1):
        merged_parts.append(
            {
                "type": "text",
                "text": f"### 批次题目 {idx}（task_item_id={it['task_item_id']}）",
            }
        )
        content = it["messages"][1]["content"]
        if isinstance(content, list):
            merged_parts.extend(content)
        else:
            merged_parts.append({"type": "text", "text": str(content)})

    return [
        {"role": "system", "content": batch_system},
        {"role": "user", "content": merged_parts},
    ]


async def _run_stage1_export(
    *,
    db_p: Path,
    cfg_dir: Path,
    assets_path: Path,
    settings: Settings,
    grade_subject: str | None,
    only_uids: list[str] | None,
    stress_test_25: bool,
    force: bool,
    include_human_verified: bool,
    tasks_dir: Path | str,
    batch_size: int,
    subagent_model: str | None,
    prompt_profile: str | None,
    overwrite_pending: bool,
) -> Stage1BatchResult:
    if not db_p.exists():
        raise PreconditionError(f"PRECONDITION_FAILED: 数据库不存在: {db_p}")
    try:
        resolved_model = resolve_subagent_model(settings, "stage1", subagent_model)
    except ValueError as exc:
        raise PreconditionError(f"PRECONDITION_FAILED: {exc}") from exc

    with closing(get_connection(db_p, readonly=True)) as conn:
        target_uids: list[str] | None = None
        if stress_test_25:
            target_uids = _load_stress_test_25_uids(cfg_dir)
        if only_uids:
            resolved_only = _dedupe_preserve_order(
                [repository.resolve_question_uid(conn, u) for u in only_uids]
            )
            if target_uids is not None:
                allowed_set = set(resolved_only)
                target_uids = [u for u in target_uids if u in allowed_set]
            else:
                target_uids = resolved_only

        where_clauses = ["q.is_retired = 0", "q.eff_is_excluded = 0"]
        params: list[Any] = []
        if grade_subject:
            where_clauses.append("q.grade_subject = ?")
            params.append(grade_subject)
        if target_uids is not None:
            if not target_uids:
                return Stage1BatchResult(
                    0, 0, 0, 0, 0, 0, 0, 0, [],
                    mode="export", subagent_model=resolved_model,
                )
            placeholders = ",".join("?" for _ in target_uids)
            where_clauses.append(f"q.question_uid IN ({placeholders})")
            params.extend(target_uids)

        rows = conn.execute(
            f"""
            SELECT
                q.question_uid, q.grade_subject, q.cas_images_json, q.image_captions_json,
                q.has_images, q.eff_question_text, q.eff_options_json, q.eff_format,
                q.eff_verification_status,
                rq.content_hash AS rq_content_hash,
                cs.status AS cs_status, cs.input_hash AS cs_input_hash,
                cs.model_name AS cs_model_name
            FROM questions q
            JOIN raw_questions rq ON rq.question_uid = q.question_uid
            LEFT JOIN compile_state cs
                ON q.question_uid = cs.question_uid AND cs.stage = 'S1'
            WHERE {' AND '.join(where_clauses)}
            ORDER BY q.question_uid ASC
            """,
            params,
        ).fetchall()

    res_export = Stage1BatchResult(
        total_candidates=len(rows),
        processed=0,
        skipped_ok=0,
        skipped_human_verified=0,
        verified_consensus=0,
        needs_review=0,
        failed=0,
        multi_solution_count=0,
        errors=[],
        mode="export",
        subagent_model=resolved_model,
    )

    export_items: list[dict[str, Any]] = []
    task_items_meta: dict[str, dict[str, Any]] = {}
    seam_ver = STAGE1_PROMPT_VERSION
    seam_digest = ""

    for row in rows:
        uid = str(row["question_uid"])
        if row["eff_verification_status"] == "HUMAN_VERIFIED" and not include_human_verified:
            res_export.skipped_human_verified += 1
            continue

        cs_status = str(row["cs_status"] or "PENDING")
        cs_hash = str(row["cs_input_hash"] or "")
        cs_model = str(row["cs_model_name"] or "")
        rq_hash = str(row["rq_content_hash"] or "")
        is_subagent_locked = (
            cs_status == "OK"
            and (cs_model == "subagent-unified" or cs_model.startswith("subagent:"))
            and cs_hash == rq_hash
        )
        already_verified_ok = (
            cs_status == "OK" and row["eff_verification_status"] == "VERIFIED_CONSENSUS"
        )
        if not force and (is_subagent_locked or already_verified_ok):
            res_export.skipped_ok += 1
            res_export.skipped_locked += 1
            continue

        cas_images = [str(x) for x in (json.loads(row["cas_images_json"] or "[]") or [])]
        image_captions = [str(x) for x in (json.loads(row["image_captions_json"] or "[]") or [])]
        options: dict[str, str] = {}
        if row["eff_format"] == "选择题":
            try:
                parsed_opts = json.loads(row["eff_options_json"] or "{}")
                if isinstance(parsed_opts, dict):
                    options = {str(k): str(v) for k, v in parsed_opts.items()}
            except Exception:
                options = {}

        inp = Stage1Input(
            question_uid=uid,
            grade_subject=str(row["grade_subject"]),
            question_text=str(row["eff_question_text"]),
            cas_images=cas_images,
            image_captions=image_captions,
            options=options,
            has_images=bool(row["has_images"] or len(cas_images) > 0),
        )
        inp_text_only = inp.model_copy(
            update={"cas_images": [], "image_captions": [], "has_images": False}
        )
        base_msgs = build_stage1_messages(
            inp_text_only,
            role="PRIMARY",
            assets_dir=assets_path,
            config_dir=cfg_dir,
            prompt_profile=prompt_profile,
        )
        seam_ver, seam_digest = _resolve_stage1_seam_meta(
            cfg_dir, inp.grade_subject, prompt_profile
        )
        if cas_images:
            user_text = str(base_msgs[1]["content"])
            user_parts: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
            for idx, cas_img in enumerate(cas_images, start=1):
                cap = image_captions[idx - 1].strip() if idx - 1 < len(image_captions) else ""
                label_text = f"【配图 {idx}（{cap}）】" if cap else f"【配图 {idx}】"
                user_parts.append({"type": "text", "text": label_text})
                img_path_str = (
                    (assets_path / cas_img).as_posix()
                    if "/" not in cas_img and "\\" not in cas_img
                    else cas_img
                )
                user_parts.append(
                    {
                        "type": "image_path",
                        "image_path": img_path_str,
                        "index": idx,
                        "caption": cap,
                    }
                )
            base_msgs[1]["content"] = user_parts

        task_item_id = f"s1_{len(export_items) + 1:04d}"
        export_items.append({"task_item_id": task_item_id, "messages": base_msgs})
        task_items_meta[task_item_id] = {
            "question_uid": uid,
            "content_hash": rq_hash,
            "grade_subject": inp.grade_subject,
            "question_text": inp.question_text,
            "options": options,
            "has_images": inp.has_images,
        }

    eff_bs = max(1, int(batch_size))
    batches: list[dict[str, Any]] = []
    for b_idx, start in enumerate(range(0, len(export_items), eff_bs), start=1):
        chunk = export_items[start : start + eff_bs]
        batches.append(
            {
                "batch_id": f"stage1_batch_{b_idx:04d}",
                "messages": _build_stage1_batch_messages(chunk),
                "task_item_ids": [item["task_item_id"] for item in chunk],
            }
        )

    task_map = {
        "grade_subject": grade_subject or "",
        "prompt_version": seam_ver,
        "subagent_model": resolved_model,
        "items": task_items_meta,
    }
    manifest = export_task_batches(
        "stage1",
        batches,
        task_map,
        tasks_dir,
        schema_cls=Stage1SubagentBatchOutput,
        subagent_model=resolved_model,
        prompt_version=seam_ver,
        prompt_digest=seam_digest,
        prompt_profile=prompt_profile,
        max_parallel_workers=settings.subagent.max_parallel_workers,
        overwrite_pending=overwrite_pending,
    )
    res_export.exported_questions = len(export_items)
    res_export.batch_count = len(batches)
    res_export.manifest = manifest
    res_export.manifest_status = str(manifest.get("status") or "")
    return res_export


async def _run_stage1_import(
    *,
    db_p: Path,
    cfg_dir: Path,
    lock_path: Path,
    writer: SingleWriterExecutor,
    settings: Settings,
    force: bool,
    include_human_verified: bool,
    tasks_dir: Path | str,
    results_path: Path | str | None,
    subagent_model: str | None,
) -> Stage1BatchResult:
    if results_path is None:
        raise PreconditionError("PRECONDITION_FAILED: --mode import 必须提供 --results")
    tasks_base = Path(tasks_dir)
    task_map_file = tasks_base / "stage1_task_map.json"
    manifest_file = tasks_base / "stage1_manifest.json"

    valid_by_batch, loaded_map, bridge_errs = load_and_validate_results(
        "stage1",
        results_path,
        task_map_file,
        Stage1SubagentBatchOutput,
        retry_dir=tasks_base,
        verify_item_completeness=True,
        id_extractor=lambda out: {it.task_item_id for it in out.items},
    )

    manifest_data: dict[str, Any] = {}
    if manifest_file.is_file():
        try:
            loaded_mf = json.loads(manifest_file.read_text(encoding="utf-8"))
            if isinstance(loaded_mf, dict):
                manifest_data = loaded_mf
        except Exception:
            manifest_data = {}

    if subagent_model is not None and subagent_model.strip():
        try:
            resolved_model = validate_subagent_model_id(subagent_model)
        except ValueError as exc:
            raise PreconditionError(f"PRECONDITION_FAILED: {exc}") from exc
    elif str(manifest_data.get("subagent_model") or "").strip():
        try:
            resolved_model = validate_subagent_model_id(str(manifest_data["subagent_model"]))
        except ValueError as exc:
            raise PreconditionError(f"PRECONDITION_FAILED: {exc}") from exc
    else:
        resolved_model = resolve_subagent_model(settings, "stage1", None)

    model_name_to_write = f"subagent:{resolved_model}"
    prompt_version_to_write = str(manifest_data.get("prompt_version") or STAGE1_PROMPT_VERSION)
    items_meta = loaded_map.get("items") or {}

    res_import = Stage1BatchResult(
        total_candidates=len(items_meta) if isinstance(items_meta, dict) else 0,
        processed=0,
        skipped_ok=0,
        skipped_human_verified=0,
        verified_consensus=0,
        needs_review=0,
        failed=0,
        multi_solution_count=0,
        errors=[],
        mode="import",
        subagent_model=resolved_model,
    )
    item_errors: list[dict[str, Any]] = []

    with CompileLock(lock_path), closing(get_connection(db_p)) as conn:
        init_db(conn, config_dir=cfg_dir)
        effective.install_eff_write_guard(conn)

        for batch_id, batch_out in valid_by_batch.items():
            for out_item in batch_out.items:
                meta = items_meta.get(out_item.task_item_id)
                if not isinstance(meta, dict):
                    item_errors.append(
                        {
                            "batch_id": batch_id,
                            "task_item_id": out_item.task_item_id,
                            "error_code": "UNKNOWN_TASK_ID",
                            "error": f"task_map 中不存在 task_item_id={out_item.task_item_id!r}",
                        }
                    )
                    continue

                uid = str(meta["question_uid"])
                expected_hash = str(meta.get("content_hash") or "")
                db_row = conn.execute(
                    """
                    SELECT rq.content_hash,
                           q.is_retired, q.eff_is_excluded, q.eff_verification_status,
                           q.grade_subject, q.eff_options_json,
                           cs.status AS cs_status, cs.model_name AS cs_model, cs.input_hash AS cs_hash
                    FROM raw_questions rq
                    JOIN questions q ON q.question_uid = rq.question_uid
                    LEFT JOIN compile_state cs ON cs.question_uid = rq.question_uid AND cs.stage = 'S1'
                    WHERE rq.question_uid = ?
                    """,
                    (uid,),
                ).fetchone()
                if db_row is None:
                    item_errors.append(
                        {
                            "batch_id": batch_id,
                            "task_item_id": out_item.task_item_id,
                            "question_uid": uid,
                            "error_code": "QUESTION_NOT_FOUND",
                            "error": f"题目不存在: {uid}",
                        }
                    )
                    continue

                if int(db_row["is_retired"]) != 0 or int(db_row["eff_is_excluded"]) != 0:
                    item_errors.append(
                        {
                            "batch_id": batch_id,
                            "task_item_id": out_item.task_item_id,
                            "question_uid": uid,
                            "error_code": "INELIGIBLE_POST_EXPORT",
                            "error": "导出后题目已退库或被排除",
                        }
                    )
                    continue

                if db_row["eff_verification_status"] == "HUMAN_VERIFIED" and not include_human_verified:
                    res_import.skipped_human_verified += 1
                    continue

                current_hash = str(db_row["content_hash"] or "")
                if expected_hash and current_hash != expected_hash:
                    item_errors.append(
                        {
                            "batch_id": batch_id,
                            "task_item_id": out_item.task_item_id,
                            "question_uid": uid,
                            "error_code": "STALE_CONTENT_HASH",
                            "error": "源题指纹已变化，请重新导出",
                        }
                    )
                    continue

                cs_status = str(db_row["cs_status"] or "")
                cs_model = str(db_row["cs_model"] or "")
                cs_hash = str(db_row["cs_hash"] or "")
                if (
                    not force
                    and cs_status == "OK"
                    and (cs_model == "subagent-unified" or cs_model.startswith("subagent:"))
                    and cs_hash == current_hash
                ):
                    res_import.skipped_ok += 1
                    res_import.skipped_locked += 1
                    continue

                try:
                    primary_parts = normalize_answer_parts(out_item.final_answers)
                except ValueError as exc:
                    item_errors.append(
                        {
                            "batch_id": batch_id,
                            "task_item_id": out_item.task_item_id,
                            "question_uid": uid,
                            "error_code": "ANSWER_PART_NORMALIZE_ERROR",
                            "error": f"ANSWER_PART_NORMALIZE_ERROR: {exc}",
                        }
                    )
                    continue

                raw_opts = meta.get("options")
                item_options: dict[str, str] = (
                    {str(k): str(v) for k, v in raw_opts.items()}
                    if isinstance(raw_opts, dict)
                    else {}
                )
                gs_val = str(db_row["grade_subject"] or meta.get("grade_subject") or "小学奥数")
                sympy_result = await _run_sympy_for_parts(
                    primary_parts,
                    out_item.sympy_code,
                    timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                    memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
                )
                equiv = await compare_answer_parts(
                    primary_parts,
                    None,
                    grade_subject=gs_val,
                    allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
                    options=item_options or None,
                    sympy_result=sympy_result,
                    qualitative_judge=None,
                    timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                    memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
                )

                reasons: list[str] = []
                if not equiv.all_sympy_ok:
                    reasons.extend(equiv.review_reasons or ["SYMPY_FAILED"])
                if sympy_result is not None and sympy_result.status == "TIMEOUT":
                    reasons.append("SYMPY_TIMEOUT")
                if out_item.visual_ambiguity_flag:
                    reasons.append("VISUAL_AMBIGUITY")
                if any(p.verify_mode == "QUALITATIVE" for p in primary_parts):
                    reasons.append("SINGLE_RUN_ONLY")
                reasons = _dedupe_preserve_order(reasons)
                m_status = "NEEDS_REVIEW" if reasons else "VERIFIED_CONSENSUS"

                p_answers_json = json.dumps(
                    [p.model_dump() for p in primary_parts], ensure_ascii=False
                )
                p_sympy_out_json = json.dumps(equiv.sympy_output, ensure_ascii=False)
                raw_resp_json = json.dumps(out_item.model_dump(), ensure_ascii=False)
                usage_json = json.dumps({"source": "subagent-stage1"}, ensure_ascii=False)

                async with writer.async_transaction(conn):
                    conn.execute(
                        """
                        INSERT INTO stage1_vlm_runs(
                            question_uid, role, model_name, prompt_version, input_hash,
                            run_status, visual_facts, visual_ambiguity_flag, visual_ambiguity_note,
                            method_summary, solution_markdown, final_answers_json,
                            sympy_code, sympy_status, sympy_output_json, raw_response,
                            token_usage_json, latency_ms
                        ) VALUES (?, 'PRIMARY', ?, ?, ?, 'OK', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            uid,
                            model_name_to_write,
                            prompt_version_to_write,
                            current_hash,
                            out_item.visual_facts,
                            1 if out_item.visual_ambiguity_flag else 0,
                            out_item.visual_ambiguity_note,
                            out_item.method_summary,
                            out_item.solution_markdown,
                            p_answers_json,
                            out_item.sympy_code,
                            equiv.sympy_status,
                            p_sympy_out_json,
                            raw_resp_json,
                            usage_json,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE questions
                        SET m_visual_facts = ?,
                            m_verification_status = ?,
                            m_final_answers_json = ?,
                            review_reason = ?,
                            part_verdicts_json = ?,
                            updated_at = datetime('now')
                        WHERE question_uid = ?
                        """,
                        (
                            out_item.visual_facts,
                            m_status,
                            p_answers_json,
                            ",".join(reasons),
                            json.dumps(
                                [asdict(pv) for pv in equiv.part_verdicts], ensure_ascii=False
                            ),
                            uid,
                        ),
                    )
                    effective.recompute(conn, uid)
                    materialize_solutions(
                        conn,
                        uid,
                        allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
                    )
                    effective.recompute(conn, uid)

                    conn.execute(
                        """
                        INSERT INTO compile_state(
                            question_uid, stage, status, input_hash, prompt_version,
                            model_name, attempts, last_error, updated_at
                        ) VALUES (?, 'S1', 'OK', ?, ?, ?, 1, '', datetime('now'))
                        ON CONFLICT(question_uid, stage) DO UPDATE SET
                            status = 'OK',
                            input_hash = excluded.input_hash,
                            prompt_version = excluded.prompt_version,
                            model_name = excluded.model_name,
                            attempts = compile_state.attempts + 1,
                            last_error = '',
                            updated_at = datetime('now')
                        """,
                        (uid, current_hash, prompt_version_to_write, model_name_to_write),
                    )
                    refresh.propagate_stage_stale(conn, uid, "S1")
                    eff_row = conn.execute(
                        "SELECT eff_verification_status FROM questions WHERE question_uid = ?",
                        (uid,),
                    ).fetchone()
                    if eff_row and eff_row["eff_verification_status"] == "NEEDS_REVIEW":
                        conn.execute(
                            """
                            INSERT INTO compile_state(question_uid, stage, status, updated_at)
                            VALUES (?, 'S2', 'BLOCKED', datetime('now'))
                            ON CONFLICT(question_uid, stage) DO UPDATE SET
                                status = 'BLOCKED',
                                updated_at = datetime('now')
                            """,
                            (uid,),
                        )

                res_import.processed += 1
                res_import.ingested += 1
                if m_status == "VERIFIED_CONSENSUS":
                    res_import.verified_consensus += 1
                else:
                    res_import.needs_review += 1

    retry_path = tasks_base / "retry_tasks.jsonl"
    if item_errors:
        # 仅保留本次 load_and_validate_results 写入的 bridge_errs 行；绝不累加历史旧轮次残留错误
        existing_lines = (
            [ln for ln in retry_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if bridge_errs and retry_path.is_file()
            else []
        )
        extra_lines = [
            json.dumps({"step_kind": "stage1", **e}, ensure_ascii=False) for e in item_errors
        ]
        _atomic_write_text(retry_path, "\n".join([*existing_lines, *extra_lines]) + "\n")
        if manifest_data:
            manifest_data["status"] = "PENDING_IMPORT"
            manifest_data.pop("imported_at", None)
            _atomic_write_text(
                manifest_file, json.dumps(manifest_data, ensure_ascii=False, indent=2)
            )
    elif not bridge_errs and retry_path.is_file():
        retry_path.unlink(missing_ok=True)

    final_manifest_status = ""
    if manifest_file.is_file():
        try:
            final_manifest_status = str(
                json.loads(manifest_file.read_text(encoding="utf-8")).get("status") or ""
            )
        except Exception:
            final_manifest_status = ""

    res_import.failed = len(item_errors)
    res_import.batch_errors = len(bridge_errs)
    res_import.errors = [*bridge_errs, *item_errors]
    res_import.manifest_status = final_manifest_status
    res_import.retry_file = retry_path.as_posix() if retry_path.is_file() else None
    return res_import


async def run_stage1_compile(
    *,
    db_path: Path,
    config_dir: Path = Path("config"),
    assets_dir: Path | None = None,
    grade_subject: str | None = None,
    only_uids: list[str] | None = None,
    stress_test_25: bool = False,
    force: bool = False,
    include_human_verified: bool = False,
    use_mock: bool | None = None,
    mode: Literal["api", "export", "import"] = "api",
    tasks_dir: Path | str = Path("data/unified/stage3_4/stage1"),
    results_path: Path | str | None = None,
    batch_size: int = 10,
    subagent_model: str | None = None,
    prompt_profile: str | None = None,
    overwrite_pending: bool = False,
) -> Stage1BatchResult:
    """Stage 1 断点续跑编译引擎（支持 api / export / import 三通道）。"""
    norm_mode = mode.strip().lower()
    if norm_mode not in ("api", "export", "import"):
        raise PreconditionError(f"PRECONDITION_FAILED: 不支持的 Stage 1 执行模式: {mode!r}")

    db_p = Path(db_path)
    cfg_dir = Path(config_dir)
    settings: Settings = load_settings(cfg_dir / "settings.yaml")
    if use_mock is not None:
        settings.pipeline.use_mock_llm = use_mock

    assets_path, llm_raw_dir = _resolve_paths(db_p, assets_dir)
    lock_path = db_p.parent / ".compile.lock"
    writer = SingleWriterExecutor()

    if norm_mode == "export":
        return await _run_stage1_export(
            db_p=db_p,
            cfg_dir=cfg_dir,
            assets_path=assets_path,
            settings=settings,
            grade_subject=grade_subject,
            only_uids=only_uids,
            stress_test_25=stress_test_25,
            force=force,
            include_human_verified=include_human_verified,
            tasks_dir=tasks_dir,
            batch_size=batch_size,
            subagent_model=subagent_model,
            prompt_profile=prompt_profile,
            overwrite_pending=overwrite_pending,
        )

    if norm_mode == "import":
        return await _run_stage1_import(
            db_p=db_p,
            cfg_dir=cfg_dir,
            lock_path=lock_path,
            writer=writer,
            settings=settings,
            force=force,
            include_human_verified=include_human_verified,
            tasks_dir=tasks_dir,
            results_path=results_path,
            subagent_model=subagent_model,
        )

    # mode == "api"

    with CompileLock(lock_path), closing(get_connection(db_p)) as conn:
        init_db(conn, config_dir=cfg_dir)

        # 持锁后重置遗留 RUNNING -> PENDING（NFR-2）
        async with writer.async_transaction(conn):
            conn.execute(
                """
                UPDATE compile_state
                SET status = 'PENDING', updated_at = datetime('now')
                WHERE stage = 'S1' AND status = 'RUNNING'
                """
            )

        target_uids: list[str] | None = None
        if stress_test_25:
            target_uids = _load_stress_test_25_uids(cfg_dir)
        if only_uids:
            resolved_only = _dedupe_preserve_order(
                [repository.resolve_question_uid(conn, u) for u in only_uids]
            )
            if target_uids is not None:
                allowed_set = set(resolved_only)
                target_uids = [u for u in target_uids if u in allowed_set]
            else:
                target_uids = resolved_only

        where_clauses = ["q.is_retired = 0", "q.eff_is_excluded = 0"]
        params: list[Any] = []
        if grade_subject:
            where_clauses.append("q.grade_subject = ?")
            params.append(grade_subject)
        if target_uids is not None:
            if not target_uids:
                return Stage1BatchResult(0, 0, 0, 0, 0, 0, 0, 0, [])
            placeholders = ",".join("?" for _ in target_uids)
            where_clauses.append(f"q.question_uid IN ({placeholders})")
            params.extend(target_uids)

        sql = f"""
            SELECT
                q.question_uid, q.grade_subject, q.cas_images_json, q.image_captions_json,
                q.has_images, q.eff_question_text, q.eff_options_json, q.eff_format,
                q.eff_verification_status, q.m_verification_status, q.review_reason,
                rq.content_hash AS rq_content_hash,
                cs.status AS cs_status, cs.input_hash AS cs_input_hash,
                cs.model_name AS cs_model_name, cs.attempts AS cs_attempts
            FROM questions q
            JOIN raw_questions rq ON rq.question_uid = q.question_uid
            LEFT JOIN compile_state cs
                ON q.question_uid = cs.question_uid AND cs.stage = 'S1'
            WHERE {' AND '.join(where_clauses)}
            ORDER BY q.question_uid ASC
        """
        rows = conn.execute(sql, params).fetchall()

        result = Stage1BatchResult(
            total_candidates=len(rows),
            processed=0,
            skipped_ok=0,
            skipped_human_verified=0,
            verified_consensus=0,
            needs_review=0,
            failed=0,
            multi_solution_count=0,
            errors=[],
        )

        vlm_p_model = settings.llm.vlm_primary.model
        vlm_s_model = settings.llm.vlm_secondary.model
        dual_policy = settings.pipeline.dual_vlm_policy
        max_attempts = max(1, settings.pipeline.max_stage_attempts)

        work_items: list[tuple[Stage1Input, str, int, bool]] = []
        for row in rows:
            uid = row["question_uid"]
            if row["eff_verification_status"] == "HUMAN_VERIFIED" and not include_human_verified:
                result.skipped_human_verified += 1
                continue

            cas_images: list[str] = json.loads(row["cas_images_json"] or "[]")
            image_captions: list[str] = json.loads(row["image_captions_json"] or "[]")
            input_hash = compute_stage1_input_hash(
                eff_question_text=row["eff_question_text"],
                cas_images=cas_images,
                image_captions=image_captions,
                vlm_primary_model=vlm_p_model,
                vlm_secondary_model=vlm_s_model,
                dual_vlm_policy=dual_policy,
                prompt_version=STAGE1_PROMPT_VERSION,
            )

            cs_status = row["cs_status"] or "PENDING"
            cs_hash = row["cs_input_hash"] or ""
            cs_model = row["cs_model_name"] or ""
            rq_hash = row["rq_content_hash"] or ""
            prev_attempts = int(row["cs_attempts"] or 0)

            is_unified_locked = (
                cs_status == "OK"
                and (cs_model == "subagent-unified" or cs_model.startswith("subagent:"))
                and cs_hash == rq_hash
            )
            if not force and cs_status == "OK" and (is_unified_locked or cs_hash == input_hash):
                result.skipped_ok += 1
                continue

            # 若此前已达失败上限并转人工，未显式 --force 且指纹未变时不再重复烧重试
            if (
                not force
                and cs_status == "FAILED"
                and cs_hash == input_hash
                and row["m_verification_status"] == "NEEDS_REVIEW"
                and "RUN_FAILED_EXHAUSTED" in (row["review_reason"] or "")
                and prev_attempts >= max_attempts
            ):
                result.skipped_ok += 1
                continue

            options: dict[str, str] = {}
            if row["eff_format"] == "选择题":
                try:
                    parsed_opts = json.loads(row["eff_options_json"] or "{}")
                    if isinstance(parsed_opts, dict):
                        options = {str(k): str(v) for k, v in parsed_opts.items()}
                except Exception:
                    options = {}

            inp = Stage1Input(
                question_uid=uid,
                grade_subject=row["grade_subject"],
                question_text=row["eff_question_text"],
                cas_images=cas_images,
                image_captions=image_captions,
                options=options,
                has_images=bool(row["has_images"] or len(cas_images) > 0),
            )
            reset_attempts = force or (cs_hash != input_hash)
            next_attempt = 1 if reset_attempts else (prev_attempts + 1)
            work_items.append((inp, input_hash, next_attempt, reset_attempts))

        if not work_items:
            return result

        # 标记待处理题为 RUNNING
        async with writer.async_transaction(conn):
            for inp, input_hash, next_attempt, _ in work_items:
                conn.execute(
                    """
                    INSERT INTO compile_state(
                        question_uid, stage, status, input_hash, prompt_version,
                        model_name, attempts, last_error, updated_at
                    ) VALUES (?, 'S1', 'RUNNING', ?, ?, ?, ?, '', datetime('now'))
                    ON CONFLICT(question_uid, stage) DO UPDATE SET
                        status = 'RUNNING',
                        input_hash = excluded.input_hash,
                        prompt_version = excluded.prompt_version,
                        model_name = excluded.model_name,
                        attempts = excluded.attempts,
                        last_error = '',
                        updated_at = datetime('now')
                    """,
                    (
                        inp.question_uid,
                        input_hash,
                        STAGE1_PROMPT_VERSION,
                        vlm_p_model,
                        next_attempt,
                    ),
                )

        gateway = LLMGateway(
            settings,
            conn=conn,
            writer=writer,
            llm_raw_dir=llm_raw_dir,
        )
        task_sem = asyncio.Semaphore(max(4, settings.llm.vlm_primary.max_concurrency * 2))

        async def _process_one(inp: Stage1Input, input_hash: str, attempt_no: int) -> None:
            uid = inp.question_uid
            async with task_sem:
                result.processed += 1
                primary_call = None
                secondary_call = None
                sympy_result: SympySandboxResult | None = None
                api_error: str | None = None

                try:
                    p_msgs = build_stage1_messages(inp, role="PRIMARY", assets_dir=assets_path)
                    s_msgs = build_stage1_messages(inp, role="SECONDARY", assets_dir=assets_path)
                    must_dual = inp.has_images or dual_policy == "always"
                    if must_dual:
                        p_res, s_res = await asyncio.gather(
                            gateway.call_structured(
                                slot="vlm_primary",
                                stage="S1",
                                messages=p_msgs,
                                schema=Stage1PrimaryOutput,
                                question_uid=uid,
                                prompt_version=STAGE1_PROMPT_VERSION,
                            ),
                            gateway.call_structured(
                                slot="vlm_secondary",
                                stage="S1",
                                messages=s_msgs,
                                schema=Stage1SecondaryOutput,
                                question_uid=uid,
                                prompt_version=STAGE1_PROMPT_VERSION,
                            ),
                        )
                        primary_call = p_res
                        secondary_call = s_res
                    else:
                        primary_call = await gateway.call_structured(
                            slot="vlm_primary",
                            stage="S1",
                            messages=p_msgs,
                            schema=Stage1PrimaryOutput,
                            question_uid=uid,
                            prompt_version=STAGE1_PROMPT_VERSION,
                        )
                        p_parts_pre = normalize_answer_parts(primary_call.parsed.final_answers)
                        all_sym = all(p.verify_mode == "SYMBOLIC" for p in p_parts_pre)
                        sympy_result = await _run_sympy_for_parts(
                            p_parts_pre,
                            primary_call.parsed.sympy_code,
                            timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                            memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
                        )
                        if not (all_sym and sympy_result is not None and sympy_result.ok):
                            secondary_call = await gateway.call_structured(
                                slot="vlm_secondary",
                                stage="S1",
                                messages=s_msgs,
                                schema=Stage1SecondaryOutput,
                                question_uid=uid,
                                prompt_version=STAGE1_PROMPT_VERSION,
                            )
                except LLMGatewayError as exc:
                    api_error = f"{exc.error_kind}: {exc}"
                except Exception as exc:
                    api_error = f"{type(exc).__name__}: {exc}"

                if api_error is not None or primary_call is None:
                    exhausted = attempt_no >= max_attempts
                    result.failed += 1
                    result.errors.append(
                        {
                            "question_uid": uid,
                            "error": api_error or "Unknown Stage 1 failure",
                            "attempts": attempt_no,
                            "exhausted": exhausted,
                        }
                    )
                    async with writer.async_transaction(conn):
                        conn.execute(
                            """
                            INSERT INTO stage1_vlm_runs(
                                question_uid, role, model_name, prompt_version, input_hash,
                                run_status, error_message
                            ) VALUES (?, 'PRIMARY', ?, ?, ?, 'FAILED', ?)
                            """,
                            (
                                uid,
                                vlm_p_model,
                                STAGE1_PROMPT_VERSION,
                                input_hash,
                                api_error or "Unknown error",
                            ),
                        )
                        conn.execute(
                            """
                            UPDATE compile_state
                            SET status = 'FAILED', last_error = ?, updated_at = datetime('now')
                            WHERE question_uid = ? AND stage = 'S1'
                            """,
                            (api_error or "Unknown error", uid),
                        )
                        if exhausted:
                            _mark_exhausted_failure(conn, uid, result)
                    return

                primary_out = primary_call.parsed
                secondary_out = secondary_call.parsed if secondary_call is not None else None

                try:
                    primary_parts = normalize_answer_parts(primary_out.final_answers)
                    secondary_parts = (
                        normalize_answer_parts(secondary_out.final_answers)
                        if secondary_out is not None
                        else None
                    )
                except ValueError as exc:
                    err_msg = f"ANSWER_PART_NORMALIZE_ERROR: {exc}"
                    exhausted = attempt_no >= max_attempts
                    result.failed += 1
                    result.errors.append(
                        {
                            "question_uid": uid,
                            "error": err_msg,
                            "attempts": attempt_no,
                            "exhausted": exhausted,
                        }
                    )
                    async with writer.async_transaction(conn):
                        conn.execute(
                            """
                            UPDATE compile_state
                            SET status = 'FAILED', last_error = ?, updated_at = datetime('now')
                            WHERE question_uid = ? AND stage = 'S1'
                            """,
                            (err_msg, uid),
                        )
                        if exhausted:
                            _mark_exhausted_failure(conn, uid, result)
                    return

                if sympy_result is None:
                    sympy_result = await _run_sympy_for_parts(
                        primary_parts,
                        primary_out.sympy_code,
                        timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                        memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
                    )

                async def _qual_judge(label: str, ans_a: str, ans_b: str) -> QualitativeJudgeOutput:
                    q_msgs = build_qualitative_judge_messages(
                        label, ans_a, ans_b, question_text=inp.question_text
                    )
                    q_res = await gateway.call_structured(
                        slot="judge_llm",
                        stage="S1_JUDGE",
                        messages=q_msgs,
                        schema=QualitativeJudgeOutput,
                        question_uid=uid,
                        prompt_version=STAGE1_PROMPT_VERSION,
                    )
                    return q_res.parsed

                equiv = await compare_answer_parts(
                    primary_parts,
                    secondary_parts,
                    grade_subject=inp.grade_subject,
                    allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
                    options=inp.options or None,
                    sympy_result=sympy_result,
                    qualitative_judge=_qual_judge,
                    timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                    memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
                )

                reasons = list(equiv.review_reasons)
                if primary_out.visual_ambiguity_flag or (
                    secondary_out is not None and secondary_out.visual_ambiguity_flag
                ):
                    reasons.append("VISUAL_AMBIGUITY")
                if sympy_result is not None and sympy_result.status == "TIMEOUT":
                    reasons.append("SYMPY_TIMEOUT")
                if secondary_out is None and not (
                    equiv.all_sympy_ok and all(p.verify_mode == "SYMBOLIC" for p in primary_parts)
                ):
                    reasons.append("SINGLE_RUN_ONLY")

                reasons = _dedupe_preserve_order(reasons)
                is_consensus = (
                    len(reasons) == 0 and equiv.all_dual_equal and equiv.all_sympy_ok
                )
                m_status = "VERIFIED_CONSENSUS" if is_consensus else "NEEDS_REVIEW"

                is_distinct_method = False
                multi_reason = ""
                if is_consensus and secondary_out is not None:
                    try:
                        ms_msgs = build_multi_solution_judge_messages(
                            primary_out.method_summary,
                            primary_out.solution_markdown,
                            secondary_out.method_summary,
                            secondary_out.solution_markdown,
                        )
                        ms_res = await gateway.call_structured(
                            slot="judge_llm",
                            stage="S1_JUDGE",
                            messages=ms_msgs,
                            schema=MultiSolutionJudgeOutput,
                            question_uid=uid,
                            prompt_version=STAGE1_PROMPT_VERSION,
                        )
                        is_distinct_method = bool(ms_res.parsed.is_distinct_method)
                        multi_reason = ms_res.parsed.reason
                    except Exception:
                        is_distinct_method = False

                if is_consensus:
                    result.verified_consensus += 1
                else:
                    result.needs_review += 1
                if is_distinct_method:
                    result.multi_solution_count += 1

                p_answers_json = json.dumps(
                    [p.model_dump() for p in primary_parts], ensure_ascii=False
                )
                p_usage_json = json.dumps(
                    {
                        "prompt_tokens": primary_call.prompt_tokens,
                        "completion_tokens": primary_call.completion_tokens,
                        "call_id": primary_call.call_id,
                    },
                    ensure_ascii=False,
                )
                p_sympy_out_json = json.dumps(equiv.sympy_output, ensure_ascii=False)

                async with writer.async_transaction(conn):
                    cur_p = conn.execute(
                        """
                        INSERT INTO stage1_vlm_runs(
                            question_uid, role, model_name, prompt_version, input_hash,
                            run_status, visual_facts, visual_ambiguity_flag, visual_ambiguity_note,
                            method_summary, solution_markdown, final_answers_json,
                            sympy_code, sympy_status, sympy_output_json, raw_response,
                            token_usage_json, latency_ms
                        ) VALUES (?, 'PRIMARY', ?, ?, ?, 'OK', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uid,
                            primary_call.model_name,
                            STAGE1_PROMPT_VERSION,
                            input_hash,
                            primary_out.visual_facts,
                            1 if primary_out.visual_ambiguity_flag else 0,
                            primary_out.visual_ambiguity_note,
                            primary_out.method_summary,
                            primary_out.solution_markdown,
                            p_answers_json,
                            primary_out.sympy_code,
                            equiv.sympy_status,
                            p_sympy_out_json,
                            primary_call.raw_response,
                            p_usage_json,
                            primary_call.latency_ms,
                        ),
                    )
                    primary_run_id = cur_p.lastrowid

                    if secondary_call is not None and secondary_out is not None and secondary_parts is not None:
                        s_answers_json = json.dumps(
                            [p.model_dump() for p in secondary_parts], ensure_ascii=False
                        )
                        s_usage_json = json.dumps(
                            {
                                "prompt_tokens": secondary_call.prompt_tokens,
                                "completion_tokens": secondary_call.completion_tokens,
                                "call_id": secondary_call.call_id,
                            },
                            ensure_ascii=False,
                        )
                        s_meta_json = (
                            json.dumps(
                                {
                                    "is_distinct_method": True,
                                    "primary_run_id": primary_run_id,
                                    "reason": multi_reason,
                                },
                                ensure_ascii=False,
                            )
                            if is_distinct_method
                            else None
                        )
                        conn.execute(
                            """
                            INSERT INTO stage1_vlm_runs(
                                question_uid, role, model_name, prompt_version, input_hash,
                                run_status, visual_facts, visual_ambiguity_flag, visual_ambiguity_note,
                                method_summary, solution_markdown, final_answers_json,
                                sympy_code, sympy_status, sympy_output_json, raw_response,
                                token_usage_json, latency_ms
                            ) VALUES (?, 'SECONDARY', ?, ?, ?, 'OK', ?, ?, ?, ?, ?, ?, NULL, 'NOT_RUN', ?, ?, ?, ?)
                            """,
                            (
                                uid,
                                secondary_call.model_name,
                                STAGE1_PROMPT_VERSION,
                                input_hash,
                                secondary_out.visual_facts,
                                1 if secondary_out.visual_ambiguity_flag else 0,
                                secondary_out.visual_ambiguity_note,
                                secondary_out.method_summary,
                                secondary_out.solution_markdown,
                                s_answers_json,
                                s_meta_json,
                                secondary_call.raw_response,
                                s_usage_json,
                                secondary_call.latency_ms,
                            ),
                        )

                    conn.execute(
                        """
                        UPDATE questions
                        SET m_visual_facts = ?,
                            m_verification_status = ?,
                            m_final_answers_json = ?,
                            review_reason = ?,
                            part_verdicts_json = ?,
                            updated_at = datetime('now')
                        WHERE question_uid = ?
                        """,
                        (
                            primary_out.visual_facts,
                            m_status,
                            p_answers_json,
                            ",".join(reasons),
                            json.dumps(
                                [asdict(pv) for pv in equiv.part_verdicts], ensure_ascii=False
                            ),
                            uid,
                        ),
                    )

                    # 先重算 eff_* 再物化解法并检查解答-答案一致性，最后再重算一次确保冲突标记生效
                    effective.recompute(conn, uid)
                    materialize_solutions(
                        conn,
                        uid,
                        allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
                    )
                    effective.recompute(conn, uid)

                    conn.execute(
                        """
                        UPDATE compile_state
                        SET status = 'OK',
                            input_hash = ?,
                            prompt_version = ?,
                            model_name = ?,
                            last_error = '',
                            updated_at = datetime('now')
                        WHERE question_uid = ? AND stage = 'S1'
                        """,
                        (input_hash, STAGE1_PROMPT_VERSION, primary_call.model_name, uid),
                    )

                    refresh.propagate_stage_stale(conn, uid, "S1")
                    eff_row = conn.execute(
                        "SELECT eff_verification_status FROM questions WHERE question_uid = ?",
                        (uid,),
                    ).fetchone()
                    if eff_row and eff_row["eff_verification_status"] == "NEEDS_REVIEW":
                        conn.execute(
                            """
                            INSERT INTO compile_state(question_uid, stage, status, updated_at)
                            VALUES (?, 'S2', 'BLOCKED', datetime('now'))
                            ON CONFLICT(question_uid, stage) DO UPDATE SET
                                status = 'BLOCKED',
                                updated_at = datetime('now')
                            """,
                            (uid,),
                        )

        try:
            await asyncio.gather(*(_process_one(inp, h, att) for inp, h, att, _ in work_items))
        finally:
            await gateway.aclose()

    return result
