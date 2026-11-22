"""Stage 1.5 黄金锚点标定 + 主会话 Subagent Stage 1+2 合一编译器导出、确定性验真与回灌入库。

设计目标：
1. 严格隔离：导出至 Subagent/Workflow 的任务 JSONL 不包含 question_uid、source_id、source_collection、
   legacy_tags 或 题库/ 原始路径，仅使用匿名 task_id 与 CAS 相对图片路径；
2. 确定性验真守门：Python 端执行 Pydantic 校验、AnswerPart 归一、SymPy 沙盒隔离验算、
   反剧透 hint / raw_l3 / 抽象骨架去数 / 黑名单与 TRAP 词表校验；
3. NEEDS_REVIEW 不阻断 Stage 2：仅标记 eff_is_servable=0（由 effective.recompute 派生），
   已通过 Stage 2 校验的步骤、骨架、维度打分与概念照常落库（S2='OK'）；
4. 防覆盖锁定：写入 compile_state 时记录 model_name='subagent-unified' 与 content_hash，
   后续普通编译默认跳过（锚点升级或 .env 换模型也不覆盖，仅 --force 或源题变更解锁）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from shuati.config import Settings, load_settings
from shuati.core import effective, refresh
from shuati.core.difficulty import compute_difficulty
from shuati.db import human_mirror, repository
from shuati.db.connection import (
    CompileLock,
    PreconditionError,
    SingleWriterExecutor,
    get_connection,
)
from shuati.db.schema import init_db
from shuati.models.domain import AnswerPart
from shuati.models.schemas import Stage2OnlyOutput, UnifiedS1S2Output
from shuati.pipeline.stage1_solve import _run_sympy_for_parts, materialize_solutions
from shuati.verify.answer_parts import normalize_answer_parts
from shuati.verify.equivalence import compare_answer_parts

SUBAGENT_MODEL_NAME = "subagent-unified"
UNIFIED_PROMPT_VERSION = "unified-v1.0"

_EXEMPT_ANGLE_NUMS = frozenset({"45", "60", "90", "180", "360"})
_SPOILER_PHRASE_RE = re.compile(
    r"(?:答案|最终结果|解得结果|所以等于|故选|选)\s*[:：=是为]?\s*(?:\$?\d|[A-D]\b)"
)
_DIGIT_RE = re.compile(r"\d")
_MULTI_DIGIT_RE = re.compile(r"(?<![A-Za-z_])\d{2,}(?![A-Za-z_])")


@dataclass
class AnchorCalibrateResult:
    grade_subject: str
    anchors_file: str
    anchors_version: str
    anchor_count: int
    uids: list[str] = field(default_factory=list)


@dataclass
class UnifiedExportResult:
    grade_subject: str
    out_dir: str
    total_candidates: int
    exported_unified: int
    exported_stage2_only: int
    skipped_locked: int
    skipped_other_status: int
    batch_files: list[str] = field(default_factory=list)


@dataclass
class UnifiedImportResult:
    total_records: int
    ingested_verified: int
    ingested_needs_review: int
    ingested_stage2_only: int
    retry_count: int
    failed_final_count: int
    skipped_locked: int
    retry_file: str | None = None
    failed_details: list[dict[str, Any]] = field(default_factory=list)


def is_compile_locked(
    status: str | None,
    model_name: str | None,
    recorded_hash: str | None,
    current_content_hash: str,
    *,
    force: bool = False,
) -> bool:
    """检查阶段是否已被 subagent-unified 锁定且源题未变。"""
    if force:
        return False
    return (
        (status or "") == "OK"
        and (model_name or "") == SUBAGENT_MODEL_NAME
        and bool(current_content_hash)
        and (recorded_hash or "") == current_content_hash
    )


def _load_blacklist_and_traps(config_dir: Path) -> tuple[set[str], set[str]]:
    cfg_path = config_dir / "method_blacklist.yaml"
    if not cfg_path.exists():
        return set(), set()
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    blacklist = {str(t).strip() for t in data.get("blacklist_terms", []) if str(t).strip()}
    traps = {str(item["code"]).strip() for item in data.get("seed_traps", []) if item.get("code")}
    return blacklist, traps


def _upsert_active_human_override(
    conn: sqlite3.Connection,
    *,
    question_uid: str,
    field_name: str,
    override_value: str,
    reason: str,
    source: str,
    base_fingerprint: str,
) -> None:
    """幂等更新单字段激活覆写，遵守 uq_overrides_active 唯一约束。"""
    existing = conn.execute(
        """
        SELECT override_id
        FROM human_overrides
        WHERE question_uid = ? AND field_name = ? AND is_active = 1
        LIMIT 1
        """,
        (question_uid, field_name),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE human_overrides
            SET override_value = ?,
                reason = ?,
                source = ?,
                base_fingerprint = ?,
                is_suspended = 0,
                suspended_reason = '',
                revoked_at = NULL
            WHERE override_id = ?
            """,
            (override_value, reason, source, base_fingerprint, existing["override_id"]),
        )
        return
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, reason, source,
            base_fingerprint, is_active, is_suspended
        ) VALUES (?, ?, ?, ?, ?, ?, 1, 0)
        """,
        (question_uid, field_name, override_value, reason, source, base_fingerprint),
    )


def register_anchors_from_yaml(
    *,
    db_path: Path,
    config_dir: Path = Path("config"),
    grade_subject: str = "小学奥数",
    anchors_file: Path | None = None,
) -> AnchorCalibrateResult:
    """从 YAML 加载并校验 Stage 1.5 黄金难度锚点，写入数据库与 human 快照。"""
    cfg_dir = Path(config_dir)
    settings = load_settings(cfg_dir / "settings.yaml")
    lock_path = Path(db_path).parent / ".compile.lock"
    writer = SingleWriterExecutor()

    with CompileLock(lock_path), closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        gs_row = conn.execute(
            "SELECT grade_subject, slug FROM grade_subjects WHERE grade_subject = ?",
            (grade_subject,),
        ).fetchone()
        if gs_row is None:
            raise PreconditionError(f"PRECONDITION_FAILED: 未注册学段学科 '{grade_subject}'")

        slug = gs_row["slug"]
        yaml_path = Path(anchors_file) if anchors_file else (cfg_dir / "anchors" / f"anchors_{slug}.yaml")
        if not yaml_path.exists():
            raise PreconditionError(f"PRECONDITION_FAILED: 黄金锚点文件不存在: {yaml_path}")

        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        version = str(data.get("version") or "1.0")
        anchors = data.get("anchors") or []
        if not isinstance(anchors, list) or len(anchors) < 4:
            raise PreconditionError(f"PRECONDITION_FAILED: 黄金锚点数量不足（至少 4 题），当前 {len(anchors)}")

        calibrated_uids: list[str] = []
        with writer.transaction(conn):
            for entry in anchors:
                uid = str(entry["question_uid"])
                scores = entry["scores"]
                c_val = int(scores["C"])
                s_val = int(scores["S"])
                t_val = int(scores["T"])
                l_val = int(scores["L"])
                diff = compute_difficulty(c_val, s_val, t_val, l_val)
                expected_elo = entry.get("base_elo")
                if expected_elo is not None and int(expected_elo) != diff.base_elo:
                    raise PreconditionError(
                        f"PRECONDITION_FAILED: 锚点 {uid} base_elo={expected_elo} 与确定性计算 {diff.base_elo} 不符"
                    )
                target_tier = str(entry.get("target_tier") or "")
                if target_tier and target_tier != diff.tier:
                    raise PreconditionError(
                        f"PRECONDITION_FAILED: 锚点 {uid} target_tier={target_tier} 与确定性计算 {diff.tier} 不符"
                    )

                q_row = conn.execute(
                    """
                    SELECT q.eff_is_excluded, q.eff_verification_status, rq.content_hash
                    FROM questions q
                    JOIN raw_questions rq ON rq.question_uid = q.question_uid
                    WHERE q.question_uid = ?
                    """,
                    (uid,),
                ).fetchone()
                if q_row is None:
                    raise PreconditionError(f"PRECONDITION_FAILED: 锚点题目不存在于库中: {uid}")
                if q_row["eff_is_excluded"] != 0 or q_row["eff_verification_status"] not in (
                    "VERIFIED_CONSENSUS",
                    "HUMAN_VERIFIED",
                ):
                    raise PreconditionError(
                        f"PRECONDITION_FAILED: 锚点题目 {uid} 未处于有效已验真状态 ({q_row['eff_verification_status']})"
                    )

                ov_json = json.dumps(
                    {"C": c_val, "S": s_val, "T": t_val, "L": l_val},
                    ensure_ascii=False,
                )
                _upsert_active_human_override(
                    conn,
                    question_uid=uid,
                    field_name="difficulty_override_json",
                    override_value=ov_json,
                    reason=str(entry.get("rationale") or f"Stage 1.5 黄金锚点 v{version}"),
                    source="ANCHOR_CALIBRATION",
                    base_fingerprint=str(q_row["content_hash"]),
                )
                effective.recompute(conn, uid)
                calibrated_uids.append(uid)

            conn.execute(
                """
                UPDATE grade_subjects
                SET anchors_file = ?,
                    anchors_version = ?,
                    anchors_ready = 1
                WHERE grade_subject = ?
                """,
                (yaml_path.as_posix(), version, grade_subject),
            )
            human_mirror.export_snapshot(
                conn,
                human_dir=settings.human_mirror.dir,
                include_practice_log=settings.human_mirror.include_practice_log,
            )

        return AnchorCalibrateResult(
            grade_subject=grade_subject,
            anchors_file=yaml_path.as_posix(),
            anchors_version=version,
            anchor_count=len(calibrated_uids),
            uids=calibrated_uids,
        )


def _format_sanitized_anchors_text(
    conn: sqlite3.Connection,
    anchors_file: Path,
    blacklist_terms: set[str],
    seed_traps: set[str],
) -> str:
    data = yaml.safe_load(anchors_file.read_text(encoding="utf-8")) or {}
    anchors = data.get("anchors") or []
    lines: list[str] = ["## Stage 1.5 黄金难度锚点标尺（严禁修改标尺标准）"]
    for idx, entry in enumerate(anchors, 1):
        uid = str(entry["question_uid"])
        q_row = conn.execute(
            "SELECT eff_question_text FROM questions WHERE question_uid = ?",
            (uid,),
        ).fetchone()
        q_text = (q_row["eff_question_text"] if q_row else "").strip().replace("\n", " ")
        if len(q_text) > 160:
            q_text = q_text[:160] + "..."
        scores = entry["scores"]
        lines.append(
            f"- Anchor-{idx} [{entry['target_tier']} | base_elo={entry.get('base_elo', '')}] "
            f"C={scores['C']}, S={scores['S']}, T={scores['T']}, L={scores['L']}: "
            f"题干「{q_text}」；标定依据：{entry.get('rationale', '')}"
        )
    if blacklist_terms:
        lines.append(f"## 黑名单泛词（严禁写入 thinking_methods/classic_motifs/raw_l1/l2/l3）: {sorted(blacklist_terms)}")
    if seed_traps:
        lines.append(f"## 允许的 TRAP 代码枚举（trap_tags 仅限从中选择）: {sorted(seed_traps)}")
    return "\n".join(lines)


def export_unified_tasks(
    *,
    db_path: Path,
    out_dir: Path = Path("data/unified"),
    config_dir: Path = Path("config"),
    grade_subject: str = "小学奥数",
    only_uids: list[str] | None = None,
    limit: int | None = None,
    batch_size: int = 10,
    force: bool = False,
) -> UnifiedExportResult:
    """导出脱敏任务卡（UNIFIED_S1_S2 / STAGE2_ONLY）与隔离 task_map.json。"""
    cfg_dir = Path(config_dir)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    batches_dir = out_path / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    for old_b in batches_dir.glob("batch_*.jsonl"):
        old_b.unlink()

    settings: Settings = load_settings(cfg_dir / "settings.yaml")
    blacklist, seed_traps = _load_blacklist_and_traps(cfg_dir)

    with closing(get_connection(db_path, readonly=True)) as conn:
        gs_row = conn.execute(
            "SELECT grade_subject, slug, anchors_file, anchors_ready FROM grade_subjects WHERE grade_subject = ?",
            (grade_subject,),
        ).fetchone()
        if gs_row is None:
            raise PreconditionError(f"PRECONDITION_FAILED: 未注册学段学科 '{grade_subject}'")
        if gs_row["anchors_ready"] != 1 or not gs_row["anchors_file"]:
            raise PreconditionError(
                f"PRECONDITION_FAILED: '{grade_subject}' 的 Stage 1.5 黄金锚点尚未就绪，请先运行 calibrate-anchors"
            )

        anchors_path = Path(gs_row["anchors_file"])
        if not anchors_path.exists():
            raise PreconditionError(f"PRECONDITION_FAILED: 锚点文件不存在: {anchors_path}")

        anchors_prompt_text = _format_sanitized_anchors_text(conn, anchors_path, blacklist, seed_traps)

        target_uids: list[str] | None = None
        if only_uids:
            target_uids = [repository.resolve_question_uid(conn, u) for u in only_uids]

        where_clauses = [
            "q.is_retired = 0",
            "q.eff_is_excluded = 0",
            "q.grade_subject = ?",
        ]
        params: list[Any] = [grade_subject]
        if target_uids is not None:
            if not target_uids:
                return UnifiedExportResult(grade_subject, out_path.as_posix(), 0, 0, 0, 0, 0, [])
            ph = ",".join("?" for _ in target_uids)
            where_clauses.append(f"q.question_uid IN ({ph})")
            params.extend(target_uids)

        sql = f"""
            SELECT
                q.question_uid, q.grade_subject, q.cas_images_json, q.image_captions_json,
                q.has_images, q.eff_question_text, q.eff_options_json, q.eff_format,
                q.eff_visual_facts, q.eff_verification_status, q.eff_final_answers_json,
                rq.content_hash,
                cs1.status AS s1_status, cs1.model_name AS s1_model, cs1.input_hash AS s1_hash,
                cs2.status AS s2_status, cs2.model_name AS s2_model, cs2.input_hash AS s2_hash
            FROM questions q
            JOIN raw_questions rq ON rq.question_uid = q.question_uid
            LEFT JOIN compile_state cs1 ON cs1.question_uid = q.question_uid AND cs1.stage = 'S1'
            LEFT JOIN compile_state cs2 ON cs2.question_uid = q.question_uid AND cs2.stage = 'S2'
            WHERE {' AND '.join(where_clauses)}
            ORDER BY q.question_uid ASC
        """
        rows = conn.execute(sql, params).fetchall()

        task_map: dict[str, dict[str, Any]] = {}
        sanitized_tasks: list[dict[str, Any]] = []
        exported_unified = 0
        exported_stage2_only = 0
        skipped_locked = 0
        skipped_other = 0
        allow_pi_314 = grade_subject in settings.pipeline.allow_pi_314_subjects

        for row in rows:
            if limit is not None and len(sanitized_tasks) >= limit:
                break

            uid = row["question_uid"]
            content_hash = row["content_hash"]
            eff_status = row["eff_verification_status"]

            if eff_status in ("VERIFIED_CONSENSUS", "HUMAN_VERIFIED"):
                if is_compile_locked(
                    row["s2_status"], row["s2_model"], row["s2_hash"], content_hash, force=force
                ) or (not force and row["s2_status"] == "OK"):
                    skipped_locked += 1
                    continue
                mode = "STAGE2_ONLY"
            elif eff_status == "PENDING":
                s1_locked = is_compile_locked(
                    row["s1_status"], row["s1_model"], row["s1_hash"], content_hash, force=force
                )
                s2_locked = is_compile_locked(
                    row["s2_status"], row["s2_model"], row["s2_hash"], content_hash, force=force
                )
                if s1_locked and s2_locked:
                    skipped_locked += 1
                    continue
                mode = "UNIFIED_S1_S2"
            else:
                skipped_other += 1
                continue

            cas_images = [f"data/assets/images/{h}" if "/" not in str(h) else str(h) for h in json.loads(row["cas_images_json"] or "[]")]
            image_captions = list(json.loads(row["image_captions_json"] or "[]"))
            options = json.loads(row["eff_options_json"] or "{}")
            if not isinstance(options, dict):
                options = {}

            seq = len(sanitized_tasks) + 1
            task_id = f"T{seq:04d}"

            task_payload: dict[str, Any] = {
                "task_id": task_id,
                "mode": mode,
                "grade_subject": grade_subject,
                "allow_pi_314": allow_pi_314,
                "question_text": row["eff_question_text"],
                "format": row["eff_format"],
                "options": options,
                "has_images": bool(row["has_images"] or len(cas_images) > 0),
                "image_paths": cas_images,
                "image_captions": image_captions,
            }
            if mode == "STAGE2_ONLY":
                sol_row = conn.execute(
                    """
                    SELECT method_summary, solution_markdown
                    FROM question_solutions
                    WHERE question_uid = ? AND is_optimal = 1
                    ORDER BY solution_id ASC
                    LIMIT 1
                    """,
                    (uid,),
                ).fetchone()
                task_payload["verified_visual_facts"] = row["eff_visual_facts"] or ""
                task_payload["verified_final_answers"] = json.loads(row["eff_final_answers_json"] or "[]")
                task_payload["verified_method_summary"] = sol_row["method_summary"] if sol_row else ""
                task_payload["verified_solution_markdown"] = sol_row["solution_markdown"] if sol_row else ""
                exported_stage2_only += 1
            else:
                exported_unified += 1

            sanitized_tasks.append(task_payload)
            task_map[task_id] = {
                "question_uid": uid,
                "content_hash": content_hash,
                "mode": mode,
                "grade_subject": grade_subject,
                "question_text": row["eff_question_text"],
                "format": row["eff_format"],
                "options": options,
                "allow_pi_314": allow_pi_314,
                "eff_final_answers_json": row["eff_final_answers_json"] or "[]",
            }

        (out_path / "anchors_prompt.txt").write_text(anchors_prompt_text, encoding="utf-8")
        (out_path / "task_map.json").write_text(
            json.dumps(task_map, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with open(out_path / "tasks.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for item in sanitized_tasks:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

        batch_files: list[str] = []
        eff_bs = max(1, batch_size)
        for idx in range(0, len(sanitized_tasks), eff_bs):
            chunk = sanitized_tasks[idx : idx + eff_bs]
            b_file = batches_dir / f"batch_{(idx // eff_bs) + 1:03d}.jsonl"
            with open(b_file, "w", encoding="utf-8", newline="\n") as bf:
                for item in chunk:
                    bf.write(json.dumps(item, ensure_ascii=False) + "\n")
            batch_files.append(b_file.as_posix())

        return UnifiedExportResult(
            grade_subject=grade_subject,
            out_dir=out_path.as_posix(),
            total_candidates=len(rows),
            exported_unified=exported_unified,
            exported_stage2_only=exported_stage2_only,
            skipped_locked=skipped_locked,
            skipped_other_status=skipped_other,
            batch_files=batch_files,
        )


def _extract_spoiler_tokens(parts: list[AnswerPart], question_text: str) -> list[str]:
    """抽取应在 step.hint 中禁止直出的最终答案特征串。"""
    tokens: list[str] = []
    for p in parts:
        for cand in (p.answer, p.sympy_expr, p.canonical):
            if not cand:
                continue
            s = cand.strip()
            if not s or s.upper() in {"A", "B", "C", "D", "E"}:
                continue
            # 仅对多字符/分数/比值或未在题干原样出现的数字串做严格防剧透匹配
            is_fraction_or_ratio = "/" in s or ":" in s
            if (len(s) >= 2 and s not in question_text) or len(s) >= 3 or is_fraction_or_ratio:
                tokens.append(s)
    return list(dict.fromkeys(tokens))


def _validate_stage2_fields(
    out: Stage2OnlyOutput | UnifiedS1S2Output,
    *,
    question_text: str,
    answer_parts: list[AnswerPart],
    blacklist_terms: set[str],
    seed_traps: set[str],
) -> list[str]:
    errors: list[str] = []

    diff = out.difficulty
    for name, dim, upper in (
        ("C_concept_count", diff.C_concept_count, 4),
        ("S_chain_length", diff.S_chain_length, 3),
        ("T_trickiness", diff.T_trickiness, 3),
        ("L_computation_load", diff.L_computation_load, 3),
    ):
        if not 1 <= dim.score <= upper:
            errors.append(f"DIFFICULTY_RANGE_ERROR: {name}.score={dim.score} 超出 [1, {upper}]")

    step_indices = [s.step_index for s in out.steps]
    if step_indices != list(range(1, len(out.steps) + 1)):
        errors.append(f"STEP_INDEX_NON_CONTIGUOUS: 收到 {step_indices}")

    spoiler_tokens = _extract_spoiler_tokens(answer_parts, question_text)
    for s in out.steps:
        if _DIGIT_RE.search(s.raw_l3):
            errors.append(f"RAW_L3_CONTAINS_DIGIT: step {s.step_index} raw_l3='{s.raw_l3}' 不得包含数字")
        for field_val in (s.raw_l1, s.raw_l2, s.raw_l3):
            if field_val.strip() in blacklist_terms:
                errors.append(f"BLACKLIST_TERM_HIT: step {s.step_index} 命中黑名单泛词 '{field_val}'")
        if _SPOILER_PHRASE_RE.search(s.hint):
            errors.append(f"HINT_SPOILER_PHRASE: step {s.step_index} hint='{s.hint}' 疑似直接剧透答案")
        for tok in spoiler_tokens:
            if re.search(rf"(?<![\d.]){re.escape(tok)}(?![\d.])", s.hint):
                errors.append(f"HINT_LEAKS_ANSWER: step {s.step_index} hint 泄露终值 '{tok}'")

    for m in out.thinking_methods:
        if m.strip() in blacklist_terms:
            errors.append(f"BLACKLIST_TERM_HIT: thinking_methods 包含 '{m}'")
    for m in out.classic_motifs:
        if m.strip() in blacklist_terms:
            errors.append(f"BLACKLIST_TERM_HIT: classic_motifs 包含 '{m}'")

    if seed_traps:
        for trap in out.trap_tags:
            if trap not in seed_traps:
                errors.append(f"INVALID_TRAP_TAG: '{trap}' 不在允许的 seed_traps 中")

    # 检查抽象骨架是否夹带题干具体大数字（排除标准角度值）
    q_specific_nums = {
        m.group(0)
        for m in _MULTI_DIGIT_RE.finditer(question_text)
        if m.group(0) not in _EXEMPT_ANGLE_NUMS
    }
    for num_tok in q_specific_nums:
        if re.search(rf"(?<!\d){re.escape(num_tok)}(?!\d)", out.abstract_skeleton):
            errors.append(
                f"SKELETON_LEAKS_NUMBER: abstract_skeleton 夹带题中具体数值 '{num_tok}'"
            )

    return errors


def _get_or_create_concept(conn: sqlite3.Connection, *, kind: str, name: str) -> str:
    clean_name = name.strip()
    row = conn.execute(
        """
        SELECT concept_id
        FROM concepts
        WHERE kind = ? AND name = ? AND redirect_to_id IS NULL
        LIMIT 1
        """,
        (kind, clean_name),
    ).fetchone()
    if row is not None:
        return str(row["concept_id"])

    if kind == "TRAP":
        cid = f"TRAP:{clean_name}"
    else:
        digest = hashlib.sha256(f"{kind}:{clean_name}".encode()).hexdigest()[:12]
        cid = f"{kind}:{digest}"

    conn.execute(
        """
        INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed)
        VALUES (?, ?, ?, 0)
        """,
        (cid, kind, clean_name),
    )
    return cid


def _write_stage2_db(
    conn: sqlite3.Connection,
    *,
    uid: str,
    content_hash: str,
    out: Stage2OnlyOutput | UnifiedS1S2Output,
) -> None:
    diff = out.difficulty
    c_val = diff.C_concept_count.score
    s_val = diff.S_chain_length.score
    t_val = diff.T_trickiness.score
    l_val = diff.L_computation_load.score

    raw_l1s = sorted({s.raw_l1.strip() for s in out.steps if s.raw_l1.strip()})
    raw_l2s = sorted({s.raw_l2.strip() for s in out.steps if s.raw_l2.strip()})
    methods = [m.strip() for m in out.thinking_methods if m.strip()]
    motifs = [m.strip() for m in out.classic_motifs if m.strip()]
    traps = [t.strip() for t in out.trap_tags if t.strip()]

    diff_evidence_json = json.dumps(
        {
            "C_concept_count": diff.C_concept_count.model_dump(),
            "S_chain_length": diff.S_chain_length.model_dump(),
            "T_trickiness": diff.T_trickiness.model_dump(),
            "L_computation_load": diff.L_computation_load.model_dump(),
        },
        ensure_ascii=False,
    )

    conn.execute(
        """
        UPDATE questions
        SET m_abstract_skeleton = ?,
            m_primary_l1_module = ?,
            m_secondary_l1_modules_json = ?,
            m_c_score = ?,
            m_s_score = ?,
            m_t_score = ?,
            m_l_score = ?,
            raw_l1_candidates_json = ?,
            raw_l2_candidates_json = ?,
            raw_thinking_methods_json = ?,
            raw_classic_motifs_json = ?,
            raw_trap_tags_json = ?,
            difficulty_evidence_json = ?,
            updated_at = datetime('now')
        WHERE question_uid = ?
        """,
        (
            out.abstract_skeleton.strip(),
            out.primary_l1_module.strip(),
            json.dumps([x.strip() for x in out.secondary_l1_modules if x.strip()], ensure_ascii=False),
            c_val,
            s_val,
            t_val,
            l_val,
            json.dumps(raw_l1s, ensure_ascii=False),
            json.dumps(raw_l2s, ensure_ascii=False),
            json.dumps(methods, ensure_ascii=False),
            json.dumps(motifs, ensure_ascii=False),
            json.dumps(traps, ensure_ascii=False),
            diff_evidence_json,
            uid,
        ),
    )

    conn.execute(
        "DELETE FROM question_steps WHERE question_uid = ? AND branch_id = 'branch_1'",
        (uid,),
    )
    for st in out.steps:
        step_id = f"{uid}::branch_1::{st.step_index}"
        conn.execute(
            """
            INSERT INTO question_steps(
                step_id, question_uid, branch_id, step_index,
                sub_goal, core_formula, hint, raw_l1, raw_l2, raw_l3
            ) VALUES (?, ?, 'branch_1', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step_id,
                uid,
                st.step_index,
                st.sub_goal.strip(),
                st.core_formula.strip(),
                st.hint.strip(),
                st.raw_l1.strip(),
                st.raw_l2.strip(),
                st.raw_l3.strip(),
            ),
        )

    conn.execute(
        "DELETE FROM question_concepts WHERE question_uid = ? AND origin = 'MACHINE'",
        (uid,),
    )
    for kind, items in (("METHOD", methods), ("MOTIF", motifs), ("TRAP", traps)):
        for name in items:
            cid = _get_or_create_concept(conn, kind=kind, name=name)
            conn.execute(
                """
                INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin)
                VALUES (?, ?, ?, 'MACHINE')
                """,
                (uid, cid, kind),
            )

    conn.execute(
        """
        INSERT INTO compile_state(
            question_uid, stage, status, input_hash, prompt_version,
            model_name, attempts, last_error, updated_at
        ) VALUES (?, 'S2', 'OK', ?, ?, ?, 1, '', datetime('now'))
        ON CONFLICT(question_uid, stage) DO UPDATE SET
            status = 'OK',
            input_hash = excluded.input_hash,
            prompt_version = excluded.prompt_version,
            model_name = excluded.model_name,
            attempts = compile_state.attempts + 1,
            last_error = '',
            updated_at = datetime('now')
        """,
        (uid, content_hash, UNIFIED_PROMPT_VERSION, SUBAGENT_MODEL_NAME),
    )

    effective.recompute(conn, uid)
    refresh.propagate_stage_stale(conn, uid, "S2")


def _run_coro_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _verify_unified_stage1(
    out: UnifiedS1S2Output,
    *,
    meta: dict[str, Any],
    settings: Settings,
) -> tuple[list[AnswerPart] | None, Any | None, list[str], bool]:
    """执行 Stage 1 规范化 + SymPy 沙盒隔离验算。

    返回 (norm_parts, equiv_summary, blocking_errors, sympy_only_fail)。
    """
    try:
        norm_parts = normalize_answer_parts(out.final_answers)
    except ValueError as exc:
        return None, None, [f"ANSWER_PART_NORMALIZE_ERROR: {exc}"], False

    sympy_result = await _run_sympy_for_parts(
        norm_parts,
        out.sympy_code,
        timeout_seconds=settings.pipeline.sympy_timeout_seconds,
        memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
    )
    equiv = await compare_answer_parts(
        norm_parts,
        None,
        grade_subject=str(meta["grade_subject"]),
        allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
        options=meta.get("options") or None,
        sympy_result=sympy_result,
        timeout_seconds=settings.pipeline.sympy_timeout_seconds,
        memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
    )

    if not equiv.all_sympy_ok:
        detail = sympy_result.stderr_tail if sympy_result is not None else "SYMPY_NOT_OK"
        return (
            norm_parts,
            equiv,
            [f"SYMPY_FAILED ({equiv.sympy_status}): {detail}"],
            True,
        )
    return norm_parts, equiv, [], False


def import_unified_results(
    *,
    db_path: Path,
    results_file: Path,
    task_map_file: Path,
    config_dir: Path = Path("config"),
    retry_out: Path | None = None,
    final_round: bool = False,
    force: bool = False,
    worker_tag: str = "main-session",
) -> UnifiedImportResult:
    """校验并事务回灌主会话 Subagent 产出的 JSONL 结果。"""
    cfg_dir = Path(config_dir)
    settings: Settings = load_settings(cfg_dir / "settings.yaml")
    blacklist, seed_traps = _load_blacklist_and_traps(cfg_dir)

    tmap_path = Path(task_map_file)
    if not tmap_path.exists():
        raise PreconditionError(f"PRECONDITION_FAILED: 未找到 task_map.json: {tmap_path}")
    task_map: dict[str, dict[str, Any]] = json.loads(tmap_path.read_text(encoding="utf-8"))

    res_path = Path(results_file)
    if not res_path.exists():
        raise PreconditionError(f"PRECONDITION_FAILED: 未找到结果文件: {res_path}")

    raw_records: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(res_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("JSON 行必须为对象")
            raw_records.append(obj)
        except Exception as exc:
            raise PreconditionError(f"PRECONDITION_FAILED: 第 {line_no} 行 JSON 解析失败: {exc}") from exc

    retry_path = retry_out or (tmap_path.parent / "retry_tasks.jsonl")
    retry_items: list[dict[str, Any]] = []

    stats = UnifiedImportResult(
        total_records=len(raw_records),
        ingested_verified=0,
        ingested_needs_review=0,
        ingested_stage2_only=0,
        retry_count=0,
        failed_final_count=0,
        skipped_locked=0,
        retry_file=None,
    )

    lock_path = Path(db_path).parent / ".compile.lock"
    writer = SingleWriterExecutor()

    with CompileLock(lock_path), closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)

        for raw_rec in raw_records:
            task_id = str(raw_rec.get("task_id") or "").strip()
            meta = task_map.get(task_id)
            if meta is None:
                stats.failed_final_count += 1
                stats.failed_details.append({"task_id": task_id, "errors": ["UNKNOWN_TASK_ID"]})
                continue

            uid = str(meta["question_uid"])
            mode = str(meta["mode"])
            expected_hash = str(meta["content_hash"])

            db_row = conn.execute(
                """
                SELECT rq.content_hash,
                       cs1.status AS s1_status, cs1.model_name AS s1_model, cs1.input_hash AS s1_hash,
                       cs2.status AS s2_status, cs2.model_name AS s2_model, cs2.input_hash AS s2_hash
                FROM raw_questions rq
                LEFT JOIN compile_state cs1 ON cs1.question_uid = rq.question_uid AND cs1.stage = 'S1'
                LEFT JOIN compile_state cs2 ON cs2.question_uid = rq.question_uid AND cs2.stage = 'S2'
                WHERE rq.question_uid = ?
                """,
                (uid,),
            ).fetchone()
            if db_row is None:
                stats.failed_final_count += 1
                stats.failed_details.append({"task_id": task_id, "errors": ["QUESTION_NOT_FOUND"]})
                continue

            current_hash = str(db_row["content_hash"])
            if current_hash != expected_hash:
                stats.failed_final_count += 1
                stats.failed_details.append({"task_id": task_id, "errors": ["STALE_CONTENT_HASH"]})
                continue

            if mode == "STAGE2_ONLY":
                if is_compile_locked(
                    db_row["s2_status"], db_row["s2_model"], db_row["s2_hash"], current_hash, force=force
                ):
                    stats.skipped_locked += 1
                    continue
                try:
                    out_s2 = Stage2OnlyOutput.model_validate(raw_rec)
                except ValidationError as exc:
                    errs = [f"SCHEMA_ERROR: {exc}"]
                    if not final_round:
                        retry_items.append({"task_id": task_id, "mode": mode, "validation_errors": errs, "previous_output": raw_rec})
                        stats.retry_count += 1
                    else:
                        stats.failed_final_count += 1
                        stats.failed_details.append({"task_id": task_id, "errors": errs})
                    continue

                eff_parts = [
                    AnswerPart.model_validate(x)
                    for x in json.loads(str(meta.get("eff_final_answers_json") or "[]"))
                ]
                s2_errors = _validate_stage2_fields(
                    out_s2,
                    question_text=str(meta["question_text"]),
                    answer_parts=eff_parts,
                    blacklist_terms=blacklist,
                    seed_traps=seed_traps,
                )
                if s2_errors:
                    if not final_round:
                        retry_items.append({"task_id": task_id, "mode": mode, "validation_errors": s2_errors, "previous_output": raw_rec})
                        stats.retry_count += 1
                    else:
                        stats.failed_final_count += 1
                        stats.failed_details.append({"task_id": task_id, "errors": s2_errors})
                    continue

                with writer.transaction(conn):
                    _write_stage2_db(conn, uid=uid, content_hash=current_hash, out=out_s2)
                stats.ingested_stage2_only += 1
                continue

            # mode == "UNIFIED_S1_S2"
            s1_locked = is_compile_locked(
                db_row["s1_status"], db_row["s1_model"], db_row["s1_hash"], current_hash, force=force
            )
            s2_locked = is_compile_locked(
                db_row["s2_status"], db_row["s2_model"], db_row["s2_hash"], current_hash, force=force
            )
            if s1_locked and s2_locked:
                stats.skipped_locked += 1
                continue

            try:
                out_u = UnifiedS1S2Output.model_validate(raw_rec)
            except ValidationError as exc:
                errs = [f"SCHEMA_ERROR: {exc}"]
                if not final_round:
                    retry_items.append({"task_id": task_id, "mode": mode, "validation_errors": errs, "previous_output": raw_rec})
                    stats.retry_count += 1
                else:
                    stats.failed_final_count += 1
                    stats.failed_details.append({"task_id": task_id, "errors": errs})
                continue

            norm_parts, equiv, s1_errors, sympy_only_fail = _run_coro_sync(
                _verify_unified_stage1(out_u, meta=meta, settings=settings)
            )
            s2_errors = _validate_stage2_fields(
                out_u,
                question_text=str(meta["question_text"]),
                answer_parts=norm_parts or [],
                blacklist_terms=blacklist,
                seed_traps=seed_traps,
            )
            all_errors = s1_errors + s2_errors

            allow_final_sympy_needs_review = (
                final_round and sympy_only_fail and not s2_errors and norm_parts is not None and equiv is not None
            )
            if all_errors and not allow_final_sympy_needs_review:
                if not final_round:
                    retry_items.append(
                        {
                            "task_id": task_id,
                            "mode": mode,
                            "validation_errors": all_errors,
                            "previous_output": raw_rec,
                        }
                    )
                    stats.retry_count += 1
                else:
                    stats.failed_final_count += 1
                    stats.failed_details.append({"task_id": task_id, "errors": all_errors})
                continue

            assert norm_parts is not None and equiv is not None

            reasons: list[str] = []
            if allow_final_sympy_needs_review:
                reasons.extend(equiv.review_reasons or ["SYMPY_FAILED"])
            if out_u.visual_ambiguity_flag:
                reasons.append("VISUAL_AMBIGUITY")
            if any(p.verify_mode == "QUALITATIVE" for p in norm_parts):
                reasons.append("SINGLE_RUN_ONLY")
            dedup_reasons = list(dict.fromkeys(reasons))

            m_status = "NEEDS_REVIEW" if dedup_reasons else "VERIFIED_CONSENSUS"
            p_answers_json = json.dumps([p.model_dump() for p in norm_parts], ensure_ascii=False)
            raw_resp_json = json.dumps(raw_rec, ensure_ascii=False)
            usage_json = json.dumps({"source": worker_tag}, ensure_ascii=False)
            sympy_out_json = json.dumps(equiv.sympy_output, ensure_ascii=False)
            part_verdicts_json = json.dumps(
                [asdict(pv) for pv in equiv.part_verdicts], ensure_ascii=False
            )

            with writer.transaction(conn):
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
                        SUBAGENT_MODEL_NAME,
                        UNIFIED_PROMPT_VERSION,
                        current_hash,
                        out_u.visual_facts,
                        1 if out_u.visual_ambiguity_flag else 0,
                        out_u.visual_ambiguity_note,
                        out_u.method_summary,
                        out_u.solution_markdown,
                        p_answers_json,
                        out_u.sympy_code,
                        equiv.sympy_status,
                        sympy_out_json,
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
                        out_u.visual_facts,
                        m_status,
                        p_answers_json,
                        ",".join(dedup_reasons),
                        part_verdicts_json,
                        uid,
                    ),
                )
                effective.recompute(conn, uid)
                materialize_solutions(
                    conn,
                    uid,
                    allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
                )
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
                    (uid, current_hash, UNIFIED_PROMPT_VERSION, SUBAGENT_MODEL_NAME),
                )
                # 注意：用户决策要求 NEEDS_REVIEW 不阻断 Stage 2，直接落库 S2 结果并置 S2='OK'
                _write_stage2_db(conn, uid=uid, content_hash=current_hash, out=out_u)

            if m_status == "VERIFIED_CONSENSUS":
                stats.ingested_verified += 1
            else:
                stats.ingested_needs_review += 1

    if retry_items:
        retry_path.parent.mkdir(parents=True, exist_ok=True)
        with open(retry_path, "w", encoding="utf-8", newline="\n") as rf:
            for item in retry_items:
                rf.write(json.dumps(item, ensure_ascii=False) + "\n")
        stats.retry_file = retry_path.as_posix()

    return stats
