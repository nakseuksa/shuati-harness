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
from typing import Any

import yaml

from shuati.config import Settings, load_settings
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
from shuati.models.domain import AnswerPart
from shuati.models.schemas import (
    MultiSolutionJudgeOutput,
    QualitativeJudgeOutput,
    Stage1Input,
    Stage1PrimaryOutput,
    Stage1SecondaryOutput,
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
) -> Stage1BatchResult:
    """Stage 1 断点续跑编译引擎。"""
    db_p = Path(db_path)
    cfg_dir = Path(config_dir)
    settings: Settings = load_settings(cfg_dir / "settings.yaml")
    if use_mock is not None:
        settings.pipeline.use_mock_llm = use_mock

    assets_path, llm_raw_dir = _resolve_paths(db_p, assets_dir)
    lock_path = db_p.parent / ".compile.lock"
    writer = SingleWriterExecutor()

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
                cs_status == "OK" and cs_model == "subagent-unified" and cs_hash == rq_hash
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
