"""统一应用服务层（M1 范围：复核队列、采纳、手工编辑、下架、重签、撤销与 [R] 重算草稿）。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from shuati.config import Settings
from shuati.core import effective, refresh, taxonomy
from shuati.db import human_mirror, repository
from shuati.db.connection import PreconditionError, SingleWriterExecutor, get_connection
from shuati.db.repository import NotFoundError
from shuati.llm.gateway import LLMGateway
from shuati.llm.prompt_builder import STAGE1_PROMPT_VERSION, build_review_resolve_messages
from shuati.models.domain import AnswerPart
from shuati.models.schemas import ReviewResolveOutput
from shuati.pipeline.stage1_solve import materialize_solutions
from shuati.verify.answer_parts import normalize_answer_parts
from shuati.verify.sympy_sandbox import SympySandboxResult, run_verify_script

OverrideSource = Literal["REVIEW_UI", "PI_CLI", "PI_MCP", "MANUAL_YAML", "ANCHOR_CALIBRATION"]

_DEFAULT_WRITER = SingleWriterExecutor()


def _safe_json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _insert_active_override(
    conn: sqlite3.Connection,
    *,
    question_uid: str,
    field_name: str,
    override_value: str,
    reason: str,
    source: OverrideSource,
    bundle_id: str | None,
    base_fingerprint: str,
    taxonomy_version: str = "",
) -> None:
    """停用同字段旧激活覆写并插入新的激活覆写行。"""
    conn.execute(
        """
        UPDATE human_overrides
        SET is_active = 0
        WHERE question_uid = ? AND field_name = ? AND is_active = 1
        """,
        (question_uid, field_name),
    )
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, reason, source,
            bundle_id, base_fingerprint, taxonomy_version,
            is_active, is_suspended, suspended_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, '')
        """,
        (
            question_uid,
            field_name,
            override_value,
            reason,
            source,
            bundle_id,
            base_fingerprint,
            taxonomy_version,
        ),
    )


def _sync_s2_state_after_recompute(conn: sqlite3.Connection, question_uid: str) -> None:
    """依据生效验真状态更新下游 compile_state（NEEDS_REVIEW 置 S2=BLOCKED，否则置 S2..S4=STALE）。"""
    row = conn.execute(
        "SELECT eff_verification_status FROM questions WHERE question_uid = ?",
        (question_uid,),
    ).fetchone()
    if not row:
        return
    if row["eff_verification_status"] == "NEEDS_REVIEW":
        conn.execute(
            """
            INSERT INTO compile_state(question_uid, stage, status, updated_at)
            VALUES (?, 'S2', 'BLOCKED', datetime('now'))
            ON CONFLICT(question_uid, stage) DO UPDATE SET
                status = 'BLOCKED',
                updated_at = datetime('now')
            """,
            (question_uid,),
        )
    else:
        refresh.mark_stages_stale(conn, question_uid, ["S2", "S3C", "S3D", "S4"])


def list_review_queue(
    conn: sqlite3.Connection,
    *,
    grade_subject: str | None = None,
    audit_mode: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """查询待复核队列（或 audit_mode=True 时的已共识抽检队列）。"""
    target_status = "VERIFIED_CONSENSUS" if audit_mode else "NEEDS_REVIEW"
    where_clauses = [
        "q.is_retired = 0",
        "q.eff_is_excluded = 0",
        "q.eff_verification_status = ?",
    ]
    params: list[Any] = [target_status]
    if grade_subject:
        where_clauses.append("q.grade_subject = ?")
        params.append(grade_subject)

    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            q.question_uid, r.source_id, q.grade_subject, q.eff_format,
            q.eff_question_text, q.eff_verification_status, q.eff_review_reason,
            q.has_images, q.cas_images_json, q.image_captions_json,
            q.updated_at
        FROM questions q
        JOIN raw_questions r ON q.question_uid = r.question_uid
        WHERE {' AND '.join(where_clauses)}
        ORDER BY q.question_uid ASC
        LIMIT ?
        """,
        params,
    ).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "question_uid": r["question_uid"],
                "source_id": r["source_id"],
                "grade_subject": r["grade_subject"],
                "eff_format": r["eff_format"],
                "eff_question_text": r["eff_question_text"],
                "eff_verification_status": r["eff_verification_status"],
                "eff_review_reason": r["eff_review_reason"],
                "review_reasons": [x for x in (r["eff_review_reason"] or "").split(",") if x],
                "has_images": bool(r["has_images"]),
                "cas_images": _safe_json_loads(r["cas_images_json"], []),
                "image_captions": _safe_json_loads(r["image_captions_json"], []),
                "updated_at": r["updated_at"],
            }
        )
    return items


def _serialize_vlm_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "role": row["role"],
        "model_name": row["model_name"],
        "prompt_version": row["prompt_version"],
        "run_status": row["run_status"],
        "error_message": row["error_message"],
        "visual_facts": row["visual_facts"],
        "visual_ambiguity_flag": bool(row["visual_ambiguity_flag"]),
        "visual_ambiguity_note": row["visual_ambiguity_note"],
        "method_summary": row["method_summary"],
        "solution_markdown": row["solution_markdown"],
        "final_answers": _safe_json_loads(row["final_answers_json"], []),
        "sympy_code": row["sympy_code"],
        "sympy_status": row["sympy_status"],
        "sympy_output": _safe_json_loads(row["sympy_output_json"], None),
        "latency_ms": row["latency_ms"],
        "created_at": row["created_at"],
    }


def get_review_item(conn: sqlite3.Connection, uid_or_id: str) -> dict[str, Any]:
    """获取单题完整复核视图（含 eff_*、CAS 图与题注、主/辅最新 run、差异与活跃覆写）。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    q_row = conn.execute(
        """
        SELECT q.*, r.source_id, r.content_hash
        FROM questions q
        JOIN raw_questions r ON q.question_uid = r.question_uid
        WHERE q.question_uid = ?
        """,
        (uid,),
    ).fetchone()
    if not q_row:
        raise NotFoundError(f"题目 '{uid}' 尚未初始化主表记录")

    p_run = conn.execute(
        """
        SELECT * FROM stage1_vlm_runs
        WHERE question_uid = ? AND role = 'PRIMARY'
        ORDER BY run_id DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()
    s_run = conn.execute(
        """
        SELECT * FROM stage1_vlm_runs
        WHERE question_uid = ? AND role = 'SECONDARY'
        ORDER BY run_id DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()

    solutions = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM question_solutions WHERE question_uid = ? ORDER BY is_optimal DESC, branch_id ASC",
            (uid,),
        ).fetchall()
    ]
    overrides = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM human_overrides WHERE question_uid = ? AND is_active = 1 ORDER BY override_id ASC",
            (uid,),
        ).fetchall()
    ]

    return {
        "question_uid": q_row["question_uid"],
        "source_id": q_row["source_id"],
        "grade_subject": q_row["grade_subject"],
        "content_hash": q_row["content_hash"],
        "has_images": bool(q_row["has_images"]),
        "cas_images": _safe_json_loads(q_row["cas_images_json"], []),
        "image_captions": _safe_json_loads(q_row["image_captions_json"], []),
        "is_retired": bool(q_row["is_retired"]),
        "eff_question_text": q_row["eff_question_text"],
        "eff_options": _safe_json_loads(q_row["eff_options_json"], {}),
        "eff_format": q_row["eff_format"],
        "eff_visual_facts": q_row["eff_visual_facts"],
        "eff_verification_status": q_row["eff_verification_status"],
        "eff_review_reason": q_row["eff_review_reason"],
        "review_reasons": [x for x in (q_row["eff_review_reason"] or "").split(",") if x],
        "eff_verification_mode": q_row["eff_verification_mode"],
        "eff_final_answers": _safe_json_loads(q_row["eff_final_answers_json"], []),
        "eff_is_excluded": bool(q_row["eff_is_excluded"]),
        "eff_is_servable": bool(q_row["eff_is_servable"]),
        "eff_overridden_fields": _safe_json_loads(q_row["eff_overridden_fields_json"], []),
        "part_verdicts": _safe_json_loads(q_row["part_verdicts_json"], []),
        "primary_run": _serialize_vlm_run(p_run),
        "secondary_run": _serialize_vlm_run(s_run),
        "solutions": solutions,
        "active_overrides": overrides,
    }


def adopt_run(
    conn: sqlite3.Connection,
    uid_or_id: str,
    *,
    role: Literal["PRIMARY", "SECONDARY", "BOTH"],
    reason: str = "",
    source: OverrideSource = "REVIEW_UI",
    human_dir: Path | str = Path("human"),
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """采纳指定路的 Stage 1 结果并在单事务内以同一 bundle_id 写入 4 条自包含覆写。"""
    if role not in ("PRIMARY", "SECONDARY", "BOTH"):
        raise PreconditionError(f"不支持的采纳角色: {role}")

    uid = repository.resolve_question_uid(conn, uid_or_id)
    raw_row = conn.execute(
        "SELECT content_hash FROM raw_questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    if not raw_row:
        raise NotFoundError(f"题目 '{uid}' 不存在")
    content_hash = raw_row["content_hash"]

    p_run = conn.execute(
        """
        SELECT * FROM stage1_vlm_runs
        WHERE question_uid = ? AND role = 'PRIMARY' AND run_status = 'OK'
        ORDER BY run_id DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()
    s_run = conn.execute(
        """
        SELECT * FROM stage1_vlm_runs
        WHERE question_uid = ? AND role = 'SECONDARY' AND run_status = 'OK'
        ORDER BY run_id DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()

    if role in ("PRIMARY", "BOTH") and p_run is None:
        raise PreconditionError(f"题目 '{uid}' 缺少可用的 PRIMARY OK 运行记录")
    if role in ("SECONDARY", "BOTH") and s_run is None:
        raise PreconditionError(f"题目 '{uid}' 缺少可用的 SECONDARY OK 运行记录")

    chosen_run = s_run if role == "SECONDARY" else p_run
    assert chosen_run is not None

    raw_answers = _safe_json_loads(chosen_run["final_answers_json"], [])
    if not isinstance(raw_answers, list) or len(raw_answers) == 0:
        raise PreconditionError(f"题目 '{uid}' 采纳路答案为空")
    try:
        parts = normalize_answer_parts([AnswerPart.model_validate(x) for x in raw_answers])
    except Exception as exc:
        raise PreconditionError(f"采纳路答案格式非法: {exc}") from exc

    normalized_answers_json = json.dumps([p.model_dump() for p in parts], ensure_ascii=False)

    branches: list[dict[str, Any]] = [
        {
            "branch_id": "branch_1",
            "is_optimal": 1,
            "origin_run_id": chosen_run["run_id"],
            "method_summary": chosen_run["method_summary"],
            "solution_markdown": chosen_run["solution_markdown"],
            "sympy_code": chosen_run["sympy_code"],
            "sympy_verified": 1 if chosen_run["sympy_status"] == "PASSED" else 0,
        }
    ]
    if role == "BOTH":
        assert s_run is not None
        branches.append(
            {
                "branch_id": "branch_2",
                "is_optimal": 0,
                "origin_run_id": s_run["run_id"],
                "method_summary": s_run["method_summary"],
                "solution_markdown": s_run["solution_markdown"],
                "sympy_code": None,
                "sympy_verified": 0,
            }
        )
    solutions_override_json = json.dumps({"branches": branches}, ensure_ascii=False)

    bundle_id = f"bnd_{uuid.uuid4().hex[:12]}"
    active_writer = writer or _DEFAULT_WRITER
    adopt_reason = reason or f"ADOPT_RUN:{role}"

    with active_writer.transaction(conn):
        for field_name, override_val in [
            ("visual_facts", chosen_run["visual_facts"]),
            ("final_answers_json", normalized_answers_json),
            ("solutions_override_json", solutions_override_json),
            ("verification_status", "HUMAN_VERIFIED"),
        ]:
            _insert_active_override(
                conn,
                question_uid=uid,
                field_name=field_name,
                override_value=override_val,
                reason=adopt_reason,
                source=source,
                bundle_id=bundle_id,
                base_fingerprint=content_hash,
            )

        materialize_solutions(conn, uid)
        effective.recompute(conn, uid)
        _sync_s2_state_after_recompute(conn, uid)

    human_mirror.export_snapshot(conn, human_dir=human_dir)
    return {
        "question_uid": uid,
        "bundle_id": bundle_id,
        "role": role,
        "eff_verification_status": "HUMAN_VERIFIED",
        "overridden_fields": [
            "visual_facts",
            "final_answers_json",
            "solutions_override_json",
            "verification_status",
        ],
    }


def submit_manual_review(
    conn: sqlite3.Connection,
    uid_or_id: str,
    *,
    visual_facts: str,
    final_answers: list[AnswerPart | dict[str, Any]],
    solution_markdown: str,
    method_summary: str = "",
    sympy_code: str | None = None,
    reason: str = "",
    source: OverrideSource = "REVIEW_UI",
    human_dir: Path | str = Path("human"),
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """人工编辑并定稿为 HUMAN_VERIFIED（单事务 4 条覆写同 bundle_id + 镜像导出）。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    raw_row = conn.execute(
        "SELECT content_hash FROM raw_questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    if not raw_row:
        raise NotFoundError(f"题目 '{uid}' 不存在")
    content_hash = raw_row["content_hash"]

    if not isinstance(final_answers, list) or len(final_answers) == 0:
        raise PreconditionError("final_answers 不能为空")
    if sympy_code is not None and not isinstance(sympy_code, str):
        raise PreconditionError("sympy_code 必须为字符串或 None")

    try:
        parsed_parts = [
            p if isinstance(p, AnswerPart) else AnswerPart.model_validate(p)
            for p in final_answers
        ]
        normalized_parts = normalize_answer_parts(parsed_parts)
    except Exception as exc:
        raise PreconditionError(f"答案格式校验失败: {exc}") from exc

    normalized_answers_json = json.dumps(
        [p.model_dump() for p in normalized_parts], ensure_ascii=False
    )
    solutions_override_json = json.dumps(
        {
            "branches": [
                {
                    "branch_id": "branch_1",
                    "is_optimal": 1,
                    "origin_run_id": None,
                    "method_summary": method_summary,
                    "solution_markdown": solution_markdown,
                    "sympy_code": sympy_code,
                    "sympy_verified": 0,
                }
            ]
        },
        ensure_ascii=False,
    )

    bundle_id = f"bnd_{uuid.uuid4().hex[:12]}"
    active_writer = writer or _DEFAULT_WRITER
    edit_reason = reason or "MANUAL_REVIEW_EDIT"

    with active_writer.transaction(conn):
        for field_name, override_val in [
            ("visual_facts", visual_facts),
            ("final_answers_json", normalized_answers_json),
            ("solutions_override_json", solutions_override_json),
            ("verification_status", "HUMAN_VERIFIED"),
        ]:
            _insert_active_override(
                conn,
                question_uid=uid,
                field_name=field_name,
                override_value=override_val,
                reason=edit_reason,
                source=source,
                bundle_id=bundle_id,
                base_fingerprint=content_hash,
            )

        materialize_solutions(conn, uid)
        effective.recompute(conn, uid)
        _sync_s2_state_after_recompute(conn, uid)

    human_mirror.export_snapshot(conn, human_dir=human_dir)
    return {
        "question_uid": uid,
        "bundle_id": bundle_id,
        "eff_verification_status": "HUMAN_VERIFIED",
        "final_answers": [p.model_dump() for p in normalized_parts],
    }


def exclude_question(
    conn: sqlite3.Connection,
    uid_or_id: str,
    *,
    reason: str = "",
    source: OverrideSource = "REVIEW_UI",
    human_dir: Path | str = Path("human"),
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """标记坏题或重扫重题下架（is_excluded='1'）。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    raw_row = conn.execute(
        "SELECT content_hash FROM raw_questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    if not raw_row:
        raise NotFoundError(f"题目 '{uid}' 不存在")
    content_hash = raw_row["content_hash"]

    bundle_id = f"bnd_{uuid.uuid4().hex[:12]}"
    active_writer = writer or _DEFAULT_WRITER
    with active_writer.transaction(conn):
        _insert_active_override(
            conn,
            question_uid=uid,
            field_name="is_excluded",
            override_value="1",
            reason=reason or "EXCLUDE_BAD_QUESTION",
            source=source,
            bundle_id=bundle_id,
            base_fingerprint=content_hash,
        )
        effective.recompute(conn, uid)

    human_mirror.export_snapshot(conn, human_dir=human_dir)
    return {
        "question_uid": uid,
        "bundle_id": bundle_id,
        "eff_is_excluded": 1,
        "eff_is_servable": 0,
    }


def reconfirm_stale_overrides(
    conn: sqlite3.Connection,
    uid_or_id: str,
    *,
    reason: str = "",
    source: OverrideSource = "REVIEW_UI",
    human_dir: Path | str = Path("human"),
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """[K] 键：以当前 content_hash 复制重签挂起/过期覆写并停用旧行。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    raw_row = conn.execute(
        "SELECT content_hash FROM raw_questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    if not raw_row:
        raise NotFoundError(f"题目 '{uid}' 不存在")
    content_hash = raw_row["content_hash"]

    q_gs_row = conn.execute(
        "SELECT grade_subject FROM questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    current_tax_ver = ""
    if q_gs_row:
        gs_row = conn.execute(
            "SELECT taxonomy_version FROM grade_subjects WHERE grade_subject = ?",
            (q_gs_row["grade_subject"],),
        ).fetchone()
        if gs_row:
            current_tax_ver = str(gs_row["taxonomy_version"] or "").strip()

    ver_fields = sorted(effective.VERIFICATION_OVERRIDE_FIELDS)
    ph = ",".join("?" for _ in ver_fields)
    tax_fields = sorted(effective.TAXONOMY_OVERRIDE_FIELDS)
    tax_ph = ",".join("?" for _ in tax_fields)
    stale_rows = conn.execute(
        f"""
        SELECT * FROM human_overrides
        WHERE question_uid = ?
          AND is_active = 1
          AND (
              is_suspended = 1
              OR (field_name IN ({ph}) AND base_fingerprint != ?)
              OR (field_name IN ({tax_ph}) AND taxonomy_version != '' AND ? != '' AND taxonomy_version != ?)
          )
        ORDER BY override_id ASC
        """,
        (uid, *ver_fields, content_hash, *tax_fields, current_tax_ver, current_tax_ver),
    ).fetchall()
    if not stale_rows:
        raise PreconditionError(f"题目 '{uid}' 没有待确认的挂起/过期覆写")

    bundle_map: dict[str, str] = {}
    new_bundle_ids: list[str] = []
    active_writer = writer or _DEFAULT_WRITER

    with active_writer.transaction(conn):
        for old in stale_rows:
            old_bid = old["bundle_id"]
            if old_bid:
                if old_bid not in bundle_map:
                    bundle_map[old_bid] = f"bnd_{uuid.uuid4().hex[:12]}"
                    new_bundle_ids.append(bundle_map[old_bid])
                new_bid: str | None = bundle_map[old_bid]
            else:
                new_bid = f"bnd_{uuid.uuid4().hex[:12]}"
                new_bundle_ids.append(new_bid)

            conn.execute(
                "UPDATE human_overrides SET is_active = 0 WHERE override_id = ?",
                (old["override_id"],),
            )
            new_tax_ver = (
                current_tax_ver
                if old["field_name"] in effective.TAXONOMY_OVERRIDE_FIELDS
                else old["taxonomy_version"]
            )
            conn.execute(
                """
                INSERT INTO human_overrides(
                    question_uid, field_name, override_value, reason, source,
                    bundle_id, base_fingerprint, taxonomy_version,
                    is_active, is_suspended, suspended_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, '')
                """,
                (
                    uid,
                    old["field_name"],
                    old["override_value"],
                    reason or old["reason"] or "RECONFIRM_STALE",
                    source,
                    new_bid,
                    content_hash,
                    new_tax_ver,
                ),
            )

        materialize_solutions(conn, uid)
        effective.recompute(conn, uid)
        _sync_s2_state_after_recompute(conn, uid)

    human_mirror.export_snapshot(conn, human_dir=human_dir)
    q_after = conn.execute(
        "SELECT eff_verification_status FROM questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    return {
        "question_uid": uid,
        "reconfirmed_count": len(stale_rows),
        "bundle_ids": new_bundle_ids,
        "eff_verification_status": q_after["eff_verification_status"] if q_after else "",
    }


def undo_override_bundle(
    conn: sqlite3.Connection,
    bundle_id: str,
    *,
    human_dir: Path | str = Path("human"),
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """[U] 键：按 bundle_id 整组撤回覆写（is_active=0, revoked_at=now），重算生效层与物化表。"""
    rows = conn.execute(
        """
        SELECT DISTINCT question_uid FROM human_overrides
        WHERE bundle_id = ? AND is_active = 1
        """,
        (bundle_id,),
    ).fetchall()
    if not rows:
        raise NotFoundError(f"未找到激活的覆写批次 '{bundle_id}'")

    uids = [r["question_uid"] for r in rows]
    active_writer = writer or _DEFAULT_WRITER

    with active_writer.transaction(conn):
        cur = conn.execute(
            """
            UPDATE human_overrides
            SET is_active = 0, revoked_at = datetime('now')
            WHERE bundle_id = ? AND is_active = 1
            """,
            (bundle_id,),
        )
        revoked_count = cur.rowcount

        for uid in uids:
            materialize_solutions(conn, uid)
            effective.recompute(conn, uid)
            _sync_s2_state_after_recompute(conn, uid)

    human_mirror.export_snapshot(conn, human_dir=human_dir)
    return {
        "bundle_id": bundle_id,
        "revoked_count": revoked_count,
        "question_uids": uids,
    }


def resolve_review_draft(
    conn: sqlite3.Connection,
    uid_or_id: str,
    *,
    visual_facts: str,
    settings: Settings,
    llm_raw_dir: Path = Path("data/llm_raw"),
    writer: SingleWriterExecutor | None = None,
) -> dict[str, Any]:
    """[R] 键：依据人工修正的 visual_facts 单次调用 text_llm + SymPy 沙盒生成草稿（不落覆写）。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    q_row = conn.execute(
        "SELECT grade_subject, eff_question_text FROM questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    if not q_row:
        raise NotFoundError(f"题目 '{uid}' 不存在")

    msgs = build_review_resolve_messages(
        q_row["eff_question_text"],
        visual_facts,
        q_row["grade_subject"],
    )
    active_writer = writer or _DEFAULT_WRITER

    async def _run_on_conn(target_conn: sqlite3.Connection) -> Any:
        gw = LLMGateway(
            settings,
            conn=target_conn,
            writer=active_writer,
            llm_raw_dir=llm_raw_dir,
        )
        try:
            return await gw.call_structured(
                slot="text_llm",
                stage="REVIEW_RESOLVE",
                messages=msgs,
                schema=ReviewResolveOutput,
                question_uid=uid,
                prompt_version=STAGE1_PROMPT_VERSION,
            )
        finally:
            await gw.aclose()

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        call_res = asyncio.run(_run_on_conn(conn))
    else:
        db_list = conn.execute("PRAGMA database_list").fetchall()
        db_file = db_list[0]["file"] if db_list else ""

        def _worker() -> Any:
            if db_file:
                with closing(get_connection(db_file)) as w_conn:
                    return asyncio.run(_run_on_conn(w_conn))
            return asyncio.run(_run_on_conn(conn))

        with ThreadPoolExecutor(max_workers=1) as pool:
            call_res = pool.submit(_worker).result()

    parsed: ReviewResolveOutput = call_res.parsed
    normalized_parts = normalize_answer_parts(parsed.final_answers)
    has_symbolic = any(p.verify_mode == "SYMBOLIC" for p in normalized_parts)

    sympy_res: SympySandboxResult | None = None
    if has_symbolic:
        if parsed.sympy_code and parsed.sympy_code.strip():
            sympy_res = run_verify_script(
                parsed.sympy_code,
                timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
            )
        else:
            sympy_res = SympySandboxResult(
                ok=False,
                status="FAILED",
                values=None,
                stderr_tail="Missing sympy_code for SYMBOLIC AnswerPart",
            )

    sympy_status = (
        sympy_res.status if sympy_res is not None else "SKIPPED_NO_SYMBOLIC"
    )
    return {
        "question_uid": uid,
        "visual_facts": visual_facts,
        "method_summary": parsed.method_summary,
        "solution_markdown": parsed.solution_markdown,
        "final_answers": [p.model_dump() for p in normalized_parts],
        "sympy_code": parsed.sympy_code,
        "sympy_status": sympy_status,
        "sympy_output": (
            {"values": sympy_res.values, "stderr_tail": sympy_res.stderr_tail}
            if sympy_res is not None
            else None
        ),
        "call_id": call_res.call_id,
    }


# ---------------------------------------------------------------------------
# M3 统一业务服务导出契约（cli 与可选 mcp 唯一入口）
# ---------------------------------------------------------------------------
from shuati.core.composer import compose_quiz  # noqa: E402, F401
from shuati.core.profiler import compute_learner_profile  # noqa: E402, F401
from shuati.render.sheet_renderer import render_practice_sheet  # noqa: E402, F401
from shuati.services.diagnose_batch import (  # noqa: E402, F401
    diagnose_external_file_async,
    parse_question_list_markdown,
)
from shuati.services.mistake import (  # noqa: E402, F401
    diagnose_external_mistake_async,
    ingest_external_mistake_async,
)
from shuati.services.practice import (  # noqa: E402, F401
    apply_single_override,
    check_answer_async,
    create_learner,
    get_related_questions,
    hydrate_question_dict,
    list_learners,
    log_batch_practice,
    log_practice,
    resolve_learner_id,
    use_learner,
    void_practice_log,
)
from shuati.services.practice import browse_taxonomy as _practice_browse_taxonomy  # noqa: E402
from shuati.services.practice import search_questions as _practice_search_questions  # noqa: E402
from shuati.services.reveal import RevealMode, apply_reveal  # noqa: E402, F401


def browse_taxonomy(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    learner_id: str | None = None,
    l1: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """浏览学段知识树、先修 DAG、题量与学习者掌握热力（含维护降级检测）。"""
    res = _practice_browse_taxonomy(
        conn,
        grade_subject=grade_subject,
        learner_id=learner_id,
        l1=l1,
        settings=settings,
    )
    maint = taxonomy.get_taxonomy_maintenance_status(conn, grade_subject)
    warnings = list(res.get("warnings") or [])
    if maint["degraded"]:
        res["maintenance_degraded"] = True
        if maint["message"] and not any(str(w).startswith("MAINTENANCE_DEGRADED") for w in warnings):
            warnings.append(maint["message"])
    res["warnings"] = warnings
    res["maintenance_status"] = maint
    return res


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
    """多维条件 + FTS5 + 骨架语义综合检索（对 --l2/--l3 附加词表维护降级检测）。"""
    res = _practice_search_questions(
        conn,
        settings,
        grade_subject=grade_subject,
        l1=l1,
        l2=l2,
        l3=l3,
        motif=motif,
        method=method,
        trap=trap,
        tier=tier,
        elo_min=elo_min,
        elo_max=elo_max,
        query=query,
        semantic=semantic,
        exclude_solved=exclude_solved,
        learner_id=learner_id,
        reveal=reveal,
        hint_level=hint_level,
        include_visual_facts=include_visual_facts,
        limit=limit,
        assets_dir=assets_dir,
        vectors_dir=vectors_dir,
        use_mock=use_mock,
    )
    if l2 or l3:
        target_subjects = (
            [grade_subject]
            if grade_subject
            else [
                str(r["grade_subject"])
                for r in conn.execute(
                    "SELECT grade_subject FROM grade_subjects WHERE taxonomy_status IN ('LOCKED', 'DRAFT');"
                ).fetchall()
            ]
        )
        warnings = list(res.get("warnings") or [])
        degraded_any = False
        for gs_name in target_subjects:
            maint = taxonomy.get_taxonomy_maintenance_status(conn, gs_name)
            if maint["degraded"]:
                degraded_any = True
                if maint["message"] and maint["message"] not in warnings:
                    warnings.append(maint["message"])
        res["maintenance_degraded"] = degraded_any
        res["warnings"] = warnings
    return res


def get_question_detail(
    conn: sqlite3.Connection,
    uid_or_id: str,
    *,
    assets_dir: Path | str = Path("data/assets/images"),
    reveal: str = "quiz",
    hint_level: int = 1,
    include_visual_facts: bool = False,
) -> dict[str, Any]:
    """查询单题并执行防剧透裁剪。"""
    uid = repository.resolve_question_uid(conn, uid_or_id)
    raw = hydrate_question_dict(conn, uid, assets_dir=assets_dir)
    return apply_reveal(
        raw,
        reveal=reveal,  # type: ignore[arg-type]
        hint_level=hint_level,
        include_visual_facts=include_visual_facts,
    )
