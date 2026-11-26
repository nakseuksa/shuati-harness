"""人工资产 git 快照镜像与数据库快照备份（不变量 9，DESIGN.md §3.6）。

核心原则：
1. export_snapshot：按主键升序导出全表原子快照至 human/*.jsonl；
2. restore_snapshot：单一事务内使用 UPSERT (INSERT ... ON CONFLICT DO UPDATE SET)
   而非触发 DELETE 的 INSERT OR REPLACE，避免外键 RESTRICT 冲突；恢复后自动调用 effective.recompute；
3. vacuum_backup：破坏性操作前自动执行 VACUUM INTO 并保留最近 N 份备份。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from shuati.core import effective
from shuati.db.connection import PreconditionError


def _export_external_questions_jsonl(conn: sqlite3.Connection, target_file: Path) -> int:
    """导出外部录入错题 (source_type='EXTERNAL_MISTAKE') 的 raw_questions + questions 全量信息。"""
    rows = conn.execute(
        """
        SELECT
            r.question_uid, r.grade_subject, r.source_type, r.source_collection,
            r.source_file, r.source_id, r.raw_chapter, r.raw_question,
            r.raw_images_json, r.cas_images_json AS r_cas_images,
            r.image_captions_json AS r_captions, r.legacy_tags_json,
            r.content_hash, r.plain_fingerprint, r.duplicate_of_uid,
            r.ingested_at, r.updated_at,
            q.m_question_text, q.m_options_json, q.m_format, q.m_visual_facts,
            q.m_verification_status, q.m_final_answers_json, q.m_abstract_skeleton,
            q.m_primary_l1_module, q.m_secondary_l1_modules_json,
            q.m_c_score, q.m_s_score, q.m_t_score, q.m_l_score,
            q.has_images, q.cas_images_json, q.image_captions_json AS q_captions,
            q.is_retired, q.review_reason
        FROM raw_questions r
        LEFT JOIN questions q ON q.question_uid = r.question_uid
        WHERE r.source_type = 'EXTERNAL_MISTAKE'
        ORDER BY r.question_uid ASC;
        """
    ).fetchall()
    target_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = target_file.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(dict(r), ensure_ascii=False, sort_keys=True) + "\n")
    if tmp_file.exists():
        os.replace(tmp_file, target_file)
    return len(rows)


def vacuum_backup(
    conn: sqlite3.Connection,
    backups_dir: Path | str,
    command_name: str,
    keep: int = 10,
) -> Path:
    """执行 VACUUM INTO 备份并清理超出 keep 份的最旧快照。"""
    # 确保当前连接无未提交事务（SQLite 要求 VACUUM 处于非事务状态）
    try:
        conn.commit()
    except Exception:
        pass

    b_dir = Path(backups_dir)
    b_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    safe_command = re.sub(r"[^A-Za-z0-9_-]", "_", command_name).strip("_") or "backup"
    backup_file = b_dir / f"{timestamp}_{safe_command}.db"

    # SQLite VACUUM INTO 要求目标文件不存在
    if backup_file.exists():
        backup_file.unlink()

    # 转为正斜杠绝对路径并转义 SQLite 字符串单引号
    posix_path = backup_file.resolve().as_posix()
    escaped_path = posix_path.replace("'", "''")
    conn.execute(f"VACUUM INTO '{escaped_path}';")

    # 清理超出 keep 的备份文件
    existing_backups = sorted(
        b_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if len(existing_backups) > keep:
        for old_file in existing_backups[keep:]:
            try:
                old_file.unlink()
            except OSError:
                pass

    return backup_file


def _export_table_to_jsonl(
    conn: sqlite3.Connection,
    table_name: str,
    pk_col: str,
    target_file: Path,
) -> int:
    """按主键升序将表导出至 JSONL（原子写入）。"""
    rows = conn.execute(f"SELECT * FROM {table_name} ORDER BY {pk_col} ASC").fetchall()
    target_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = target_file.with_suffix(".tmp")

    with open(tmp_file, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            line = json.dumps(dict(r), ensure_ascii=False, sort_keys=True)
            f.write(line + "\n")

    if tmp_file.exists():
        os.replace(tmp_file, target_file)

    return len(rows)


def export_snapshot(
    conn: sqlite3.Connection,
    human_dir: Path | str = "human",
    include_practice_log: bool = True,
) -> dict[str, int]:
    """导出全量人工资产与外部错题快照至 human/*.jsonl。"""
    h_dir = Path(human_dir)
    counts: dict[str, int] = {}

    counts["external_questions"] = _export_external_questions_jsonl(
        conn, h_dir / "external_questions.jsonl"
    )
    counts["learners"] = _export_table_to_jsonl(
        conn, "learners", "learner_id", h_dir / "learners.jsonl"
    )
    counts["human_overrides"] = _export_table_to_jsonl(
        conn, "human_overrides", "override_id", h_dir / "overrides.jsonl"
    )
    counts["relation_overrides"] = _export_table_to_jsonl(
        conn, "relation_overrides", "override_id", h_dir / "relation_overrides.jsonl"
    )

    if include_practice_log:
        counts["practice_log"] = _export_table_to_jsonl(
            conn, "practice_log", "log_id", h_dir / "practice_log.jsonl"
        )

    return counts


def _read_jsonl_items(fpath: Path) -> list[dict[str, Any]]:
    if not fpath.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(fpath, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                out.append(json.loads(s))
    return out


def _normalize_external_question_item(item: dict[str, Any]) -> dict[str, Any]:
    """为旧版或手工裁剪的 external_questions.jsonl 记录补齐安全默认值。"""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    uid = str(item["question_uid"])
    raw_q = item.get("raw_question")
    if raw_q is None:
        raw_q = item.get("m_question_text") or ""
    raw_q_str = str(raw_q)
    cas_imgs = item.get("cas_images_json")
    r_cas = item.get("r_cas_images")
    if r_cas is None:
        r_cas = cas_imgs if cas_imgs is not None else "[]"
    if cas_imgs is None:
        cas_imgs = r_cas

    r_cap = item.get("r_captions")
    q_cap = item.get("q_captions")
    fallback_cap = item.get("image_captions_json")
    if r_cap is None:
        r_cap = fallback_cap if fallback_cap is not None else (q_cap if q_cap is not None else "[]")
    if q_cap is None:
        q_cap = fallback_cap if fallback_cap is not None else r_cap

    ingested_at = item.get("ingested_at") or now_str
    updated_at = item.get("updated_at") or ingested_at

    m_q_text = item.get("m_question_text")
    if m_q_text is None and not item.get("duplicate_of_uid"):
        m_q_text = raw_q_str

    return {
        "question_uid": uid,
        "grade_subject": str(item.get("grade_subject") or "小学奥数"),
        "source_type": str(item.get("source_type") or "EXTERNAL_MISTAKE"),
        "source_collection": str(item.get("source_collection") or "external_mistake"),
        "source_file": item.get("source_file"),
        "source_id": str(item.get("source_id") or uid.split(":")[-1]),
        "raw_chapter": item.get("raw_chapter"),
        "raw_question": raw_q_str,
        "raw_images_json": str(item.get("raw_images_json") or "[]"),
        "r_cas_images": str(r_cas),
        "r_captions": str(r_cap),
        "legacy_tags_json": str(item.get("legacy_tags_json") or "{}"),
        "content_hash": str(
            item.get("content_hash") or hashlib.sha256(raw_q_str.encode("utf-8")).hexdigest()
        ),
        "plain_fingerprint": str(item.get("plain_fingerprint") or ""),
        "duplicate_of_uid": item.get("duplicate_of_uid"),
        "ingested_at": str(ingested_at),
        "updated_at": str(updated_at),
        "m_question_text": m_q_text,
        "m_options_json": str(item.get("m_options_json") or "{}"),
        "m_format": str(item.get("m_format") or "解答题"),
        "m_visual_facts": str(item.get("m_visual_facts") if item.get("m_visual_facts") is not None else ""),
        "m_verification_status": str(item.get("m_verification_status") or "PENDING"),
        "m_final_answers_json": str(item.get("m_final_answers_json") or "[]"),
        "m_abstract_skeleton": str(
            item.get("m_abstract_skeleton") if item.get("m_abstract_skeleton") is not None else ""
        ),
        "m_primary_l1_module": str(
            item.get("m_primary_l1_module") if item.get("m_primary_l1_module") is not None else ""
        ),
        "m_secondary_l1_modules_json": str(item.get("m_secondary_l1_modules_json") or "[]"),
        "m_c_score": item.get("m_c_score"),
        "m_s_score": item.get("m_s_score"),
        "m_t_score": item.get("m_t_score"),
        "m_l_score": item.get("m_l_score"),
        "has_images": int(item.get("has_images") or 0),
        "cas_images_json": str(cas_imgs),
        "q_captions": str(q_cap),
        "is_retired": int(item.get("is_retired") or 0),
        "review_reason": str(item.get("review_reason") if item.get("review_reason") is not None else ""),
    }


def restore_snapshot(
    conn: sqlite3.Connection,
    human_dir: Path | str = "human",
    backups_dir: Path | str | None = "data/backups",
    dry_run: bool = False,
    backups_keep: int = 10,
    include_practice_log: bool = True,
    force_wipe_logs: bool = False,
) -> dict[str, Any]:
    """从 human/*.jsonl 恢复人工资产与外部错题（D-Audit-6：前置外部错题恢复 + 外键预检 + 日志防误删保护）。

    使用幂等 UPSERT (INSERT ... ON CONFLICT DO UPDATE SET)
    恢复后对涉及题目调用 effective.recompute。
    """
    h_dir = Path(human_dir)
    counts: dict[str, Any] = {
        "external_questions": 0,
        "learners": 0,
        "human_overrides": 0,
        "relation_overrides": 0,
        "practice_log": 0,
    }

    ext_items = _read_jsonl_items(h_dir / "external_questions.jsonl")
    learner_items = _read_jsonl_items(h_dir / "learners.jsonl")
    override_items = _read_jsonl_items(h_dir / "overrides.jsonl")
    rel_items = _read_jsonl_items(h_dir / "relation_overrides.jsonl")
    pl_file = h_dir / "practice_log.jsonl"
    pl_items = _read_jsonl_items(pl_file) if include_practice_log else []

    # 外键完整性前置预检（dry_run 与实执行共用）：待恢复外键引用的 question_uid 必须已存在于库中或待恢复的 external_questions.jsonl 中
    ext_uids = {str(it["question_uid"]) for it in ext_items}
    referenced_uids: set[str] = set()
    for it in override_items:
        referenced_uids.add(str(it["question_uid"]))
    for it in rel_items:
        referenced_uids.add(str(it["source_question_uid"]))
        referenced_uids.add(str(it["target_question_uid"]))
    for it in pl_items:
        referenced_uids.add(str(it["question_uid"]))

    unresolved_candidates = sorted(referenced_uids - ext_uids)
    missing: list[str] = []
    if unresolved_candidates:
        ph = ",".join("?" for _ in unresolved_candidates)
        found = {
            str(r["question_uid"])
            for r in conn.execute(
                f"SELECT question_uid FROM questions WHERE question_uid IN ({ph});",
                unresolved_candidates,
            ).fetchall()
        }
        missing = [u for u in unresolved_candidates if u not in found]

    if dry_run:
        counts["external_questions"] = len(ext_items)
        counts["learners"] = len(learner_items)
        counts["human_overrides"] = len(override_items)
        counts["relation_overrides"] = len(rel_items)
        counts["practice_log"] = len(pl_items)
        counts["fk_preflight_ok"] = len(missing) == 0
        counts["missing_question_uids"] = missing
        return counts

    if missing:
        raise PreconditionError(
            f"PRECONDITION_FAILED: human restore 外键预检失败，缺少 {len(missing)} 道题目基础行"
            f"（请先执行 shuati ingest 或补全 external_questions.jsonl）: {missing[:10]}"
        )

    # 若库内已有数据且提供了 backups_dir，恢复前先备份
    has_data = conn.execute("SELECT 1 FROM learners LIMIT 1;").fetchone() is not None
    if has_data and backups_dir:
        vacuum_backup(conn, backups_dir, "human_restore", keep=backups_keep)

    affected_question_uids: set[str] = set(ext_uids)
    seen_learner_ids: set[str] = set()
    seen_override_ids: set[int] = set()
    seen_rel_ids: set[int] = set()
    seen_log_ids: set[int] = set()

    with conn:
        # 0. 先幂等恢复外部错题基础行（raw_questions + questions 机器列）
        for raw_item in ext_items:
            item = _normalize_external_question_item(raw_item)
            conn.execute(
                """
                INSERT INTO raw_questions(
                    question_uid, grade_subject, source_type, source_collection, source_file,
                    source_id, raw_chapter, raw_question, raw_images_json, cas_images_json,
                    image_captions_json, legacy_tags_json, content_hash, plain_fingerprint,
                    duplicate_of_uid, ingested_at, updated_at
                )
                VALUES (
                    :question_uid, :grade_subject, :source_type, :source_collection, :source_file,
                    :source_id, :raw_chapter, :raw_question, :raw_images_json, :r_cas_images,
                    :r_captions, :legacy_tags_json, :content_hash, :plain_fingerprint,
                    :duplicate_of_uid, :ingested_at, :updated_at
                )
                ON CONFLICT(question_uid) DO UPDATE SET
                    raw_question = excluded.raw_question,
                    raw_images_json = excluded.raw_images_json,
                    cas_images_json = excluded.cas_images_json,
                    image_captions_json = excluded.image_captions_json,
                    content_hash = excluded.content_hash,
                    plain_fingerprint = excluded.plain_fingerprint,
                    updated_at = excluded.updated_at;
                """,
                item,
            )
            if item.get("m_question_text") is not None:
                conn.execute(
                    """
                    INSERT INTO questions(
                        question_uid, grade_subject, m_question_text, m_options_json, m_format,
                        m_visual_facts, m_verification_status, m_final_answers_json, m_abstract_skeleton,
                        m_primary_l1_module, m_secondary_l1_modules_json,
                        m_c_score, m_s_score, m_t_score, m_l_score,
                        has_images, cas_images_json, image_captions_json, is_retired, review_reason
                    )
                    VALUES (
                        :question_uid, :grade_subject, :m_question_text, :m_options_json, :m_format,
                        :m_visual_facts, :m_verification_status, :m_final_answers_json, :m_abstract_skeleton,
                        :m_primary_l1_module, :m_secondary_l1_modules_json,
                        :m_c_score, :m_s_score, :m_t_score, :m_l_score,
                        :has_images, :cas_images_json, :q_captions, :is_retired, :review_reason
                    )
                    ON CONFLICT(question_uid) DO UPDATE SET
                        m_question_text = excluded.m_question_text,
                        m_options_json = excluded.m_options_json,
                        m_format = excluded.m_format,
                        m_visual_facts = excluded.m_visual_facts,
                        m_verification_status = excluded.m_verification_status,
                        m_final_answers_json = excluded.m_final_answers_json,
                        m_abstract_skeleton = excluded.m_abstract_skeleton,
                        m_primary_l1_module = excluded.m_primary_l1_module,
                        m_secondary_l1_modules_json = excluded.m_secondary_l1_modules_json,
                        m_c_score = excluded.m_c_score,
                        m_s_score = excluded.m_s_score,
                        m_t_score = excluded.m_t_score,
                        m_l_score = excluded.m_l_score,
                        has_images = excluded.has_images,
                        cas_images_json = excluded.cas_images_json,
                        image_captions_json = excluded.image_captions_json,
                        is_retired = excluded.is_retired,
                        review_reason = excluded.review_reason;
                    """,
                    item,
                )
            counts["external_questions"] += 1

        # 1. 恢复 learners
        learners_file = h_dir / "learners.jsonl"
        if learners_file.exists():
            # 恢复前重置库中已有当前学习者，避免恢复快照中的当前学习者时触发 uq_learners_current
            conn.execute("UPDATE learners SET is_current = 0 WHERE is_current = 1;")
            for item in learner_items:
                seen_learner_ids.add(str(item["learner_id"]))
                if item.get("is_current") == 1:
                    conn.execute(
                        "UPDATE learners SET is_current = 0 WHERE is_current = 1 AND learner_id != :learner_id;",
                        item,
                    )
                conn.execute(
                    """
                    INSERT INTO learners(learner_id, display_name, default_grade_subject, is_current, notes, created_at)
                    VALUES (:learner_id, :display_name, :default_grade_subject, :is_current, :notes, :created_at)
                    ON CONFLICT(learner_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        default_grade_subject = excluded.default_grade_subject,
                        is_current = excluded.is_current,
                        notes = excluded.notes,
                        created_at = excluded.created_at;
                    """,
                    item,
                )
                counts["learners"] += 1

        # 2. 恢复 human_overrides
        overrides_file = h_dir / "overrides.jsonl"
        if overrides_file.exists():
            for item in override_items:
                seen_override_ids.add(int(item["override_id"]))
                # 若本条为活跃覆写，先将同 (question_uid, field_name) 的其他活跃行置 0，杜绝撞 uq_overrides_active
                if item.get("is_active") == 1:
                    conn.execute(
                        """
                        UPDATE human_overrides
                        SET is_active = 0
                        WHERE question_uid = :question_uid
                          AND field_name = :field_name
                          AND is_active = 1
                          AND override_id != :override_id;
                        """,
                        item,
                    )
                conn.execute(
                    """
                    INSERT INTO human_overrides(
                        override_id, question_uid, field_name, override_value, reason,
                        source, bundle_id, base_fingerprint, taxonomy_version,
                        is_active, is_suspended, suspended_reason, revoked_at, created_at
                    )
                    VALUES (
                        :override_id, :question_uid, :field_name, :override_value, :reason,
                        :source, :bundle_id, :base_fingerprint, :taxonomy_version,
                        :is_active, :is_suspended, :suspended_reason, :revoked_at, :created_at
                    )
                    ON CONFLICT(override_id) DO UPDATE SET
                        question_uid = excluded.question_uid,
                        field_name = excluded.field_name,
                        override_value = excluded.override_value,
                        reason = excluded.reason,
                        source = excluded.source,
                        bundle_id = excluded.bundle_id,
                        base_fingerprint = excluded.base_fingerprint,
                        taxonomy_version = excluded.taxonomy_version,
                        is_active = excluded.is_active,
                        is_suspended = excluded.is_suspended,
                        suspended_reason = excluded.suspended_reason,
                        revoked_at = excluded.revoked_at,
                        created_at = excluded.created_at;
                    """,
                    item,
                )
                affected_question_uids.add(item["question_uid"])
                counts["human_overrides"] += 1

        # 3. 恢复 relation_overrides
        rel_file = h_dir / "relation_overrides.jsonl"
        if rel_file.exists():
            for item in rel_items:
                seen_rel_ids.add(int(item["override_id"]))
                # 若本条为活跃关系覆写，先将同 (source, target, type) 的其他活跃行置 0，杜绝撞 uq_rel_overrides_active
                if item.get("is_active") == 1:
                    conn.execute(
                        """
                        UPDATE relation_overrides
                        SET is_active = 0
                        WHERE source_question_uid = :source_question_uid
                          AND target_question_uid = :target_question_uid
                          AND relation_type = :relation_type
                          AND is_active = 1
                          AND override_id != :override_id;
                        """,
                        item,
                    )
                conn.execute(
                    """
                    INSERT INTO relation_overrides(
                        override_id, source_question_uid, target_question_uid,
                        relation_type, action, pedagogical_note, reason,
                        source, is_active, revoked_at, created_at
                    )
                    VALUES (
                        :override_id, :source_question_uid, :target_question_uid,
                        :relation_type, :action, :pedagogical_note, :reason,
                        :source, :is_active, :revoked_at, :created_at
                    )
                    ON CONFLICT(override_id) DO UPDATE SET
                        source_question_uid = excluded.source_question_uid,
                        target_question_uid = excluded.target_question_uid,
                        relation_type = excluded.relation_type,
                        action = excluded.action,
                        pedagogical_note = excluded.pedagogical_note,
                        reason = excluded.reason,
                        source = excluded.source,
                        is_active = excluded.is_active,
                        revoked_at = excluded.revoked_at,
                        created_at = excluded.created_at;
                    """,
                    item,
                )
                counts["relation_overrides"] += 1

        # 4. 恢复 practice_log (按 log_id 升序自然恢复，受 include_practice_log 开关保护)
        if include_practice_log and pl_file.exists():
            for item in pl_items:
                seen_log_ids.add(int(item["log_id"]))
                # 消除二级唯一约束冲突
                if item.get("client_request_id"):
                    conn.execute(
                        """
                        UPDATE practice_log
                        SET client_request_id = NULL
                        WHERE learner_id = :learner_id
                          AND client_request_id = :client_request_id
                          AND log_id != :log_id;
                        """,
                        item,
                    )
                if item.get("voids_log_id"):
                    conn.execute(
                        """
                        DELETE FROM practice_log
                        WHERE voids_log_id = :voids_log_id
                          AND log_id != :log_id;
                        """,
                        item,
                    )
                conn.execute(
                    """
                    INSERT INTO practice_log(
                        log_id, learner_id, question_uid, practiced_at, outcome,
                        score_ratio, voids_log_id, stuck_step_index, stuck_node_id,
                        stuck_sub_goal, hint_level_used, user_note, submitted_answer,
                        quiz_batch_id, client_request_id
                    )
                    VALUES (
                        :log_id, :learner_id, :question_uid, :practiced_at, :outcome,
                        :score_ratio, :voids_log_id, :stuck_step_index, :stuck_node_id,
                        :stuck_sub_goal, :hint_level_used, :user_note, :submitted_answer,
                        :quiz_batch_id, :client_request_id
                    )
                    ON CONFLICT(log_id) DO UPDATE SET
                        learner_id = excluded.learner_id,
                        question_uid = excluded.question_uid,
                        practiced_at = excluded.practiced_at,
                        outcome = excluded.outcome,
                        score_ratio = excluded.score_ratio,
                        voids_log_id = excluded.voids_log_id,
                        stuck_step_index = excluded.stuck_step_index,
                        stuck_node_id = excluded.stuck_node_id,
                        stuck_sub_goal = excluded.stuck_sub_goal,
                        hint_level_used = excluded.hint_level_used,
                        user_note = excluded.user_note,
                        submitted_answer = excluded.submitted_answer,
                        quiz_batch_id = excluded.quiz_batch_id,
                        client_request_id = excluded.client_request_id;
                    """,
                    item,
                )
                counts["practice_log"] += 1

        # 5. 清理快照外多余行（按逆外键依赖顺序；空 practice_log 快照受防误删保护）
        if include_practice_log and pl_file.exists():
            if seen_log_ids:
                ph = ",".join("?" for _ in seen_log_ids)
                conn.execute(
                    f"DELETE FROM practice_log WHERE voids_log_id IS NOT NULL AND log_id NOT IN ({ph});",
                    list(seen_log_ids),
                )
                conn.execute(
                    f"DELETE FROM practice_log WHERE log_id NOT IN ({ph});",
                    list(seen_log_ids),
                )
            elif force_wipe_logs:
                conn.execute("DELETE FROM practice_log WHERE voids_log_id IS NOT NULL;")
                conn.execute("DELETE FROM practice_log;")

        # (2) relation_overrides
        if rel_file.exists():
            if seen_rel_ids:
                ph = ",".join("?" for _ in seen_rel_ids)
                conn.execute(
                    f"DELETE FROM relation_overrides WHERE override_id NOT IN ({ph});",
                    list(seen_rel_ids),
                )
            else:
                conn.execute("DELETE FROM relation_overrides;")

        # (3) human_overrides
        if overrides_file.exists():
            if seen_override_ids:
                ph = ",".join("?" for _ in seen_override_ids)
                del_ovs = conn.execute(
                    f"SELECT DISTINCT question_uid FROM human_overrides WHERE override_id NOT IN ({ph});",
                    list(seen_override_ids),
                ).fetchall()
                for r in del_ovs:
                    affected_question_uids.add(r["question_uid"])
                conn.execute(
                    f"DELETE FROM human_overrides WHERE override_id NOT IN ({ph});",
                    list(seen_override_ids),
                )
            else:
                del_ovs = conn.execute("SELECT DISTINCT question_uid FROM human_overrides;").fetchall()
                for r in del_ovs:
                    affected_question_uids.add(r["question_uid"])
                conn.execute("DELETE FROM human_overrides;")

        # (4) learners
        if learners_file.exists():
            if seen_learner_ids:
                ph = ",".join("?" for _ in seen_learner_ids)
                excess_learners = conn.execute(
                    f"SELECT learner_id FROM learners WHERE learner_id NOT IN ({ph});",
                    list(seen_learner_ids),
                ).fetchall()
            else:
                excess_learners = conn.execute("SELECT learner_id FROM learners;").fetchall()

            for er in excess_learners:
                lid = er["learner_id"]
                has_pl = conn.execute("SELECT 1 FROM practice_log WHERE learner_id = ? LIMIT 1;", (lid,)).fetchone()
                if has_pl:
                    continue
                conn.execute("DELETE FROM quiz_batches WHERE learner_id = ?;", (lid,))
                conn.execute("DELETE FROM learners WHERE learner_id = ?;", (lid,))

        # 6. 在同一事务内对涉及题目调用 effective.recompute（恢复与 eff_* / FTS 重算原子同提交或同回滚）
        for uid in sorted(affected_question_uids):
            # 仅当题目在 questions 表中存在时才重算
            exists = conn.execute("SELECT 1 FROM questions WHERE question_uid = ?", (uid,)).fetchone()
            if exists:
                effective.recompute(conn, uid)

    return counts


def verify_snapshot(
    conn: sqlite3.Connection,
    human_dir: Path | str = "human",
    include_practice_log: bool = True,
) -> dict[str, Any]:
    """对比库内全表数据与磁盘 human/*.jsonl 文件内容哈希的一致性。"""
    h_dir = Path(human_dir)
    results: dict[str, Any] = {"is_consistent": True, "tables": {}}

    tables = [
        ("learners", "learner_id", "learners.jsonl"),
        ("human_overrides", "override_id", "overrides.jsonl"),
        ("relation_overrides", "override_id", "relation_overrides.jsonl"),
    ]
    if include_practice_log:
        tables.append(("practice_log", "log_id", "practice_log.jsonl"))

    for table, pk_col, fname in tables:
        fpath = h_dir / fname
        disk_hash = ""
        if fpath.exists():
            disk_hash = hashlib.sha256(fpath.read_bytes()).hexdigest()

        rows = conn.execute(f"SELECT * FROM {table} ORDER BY {pk_col} ASC").fetchall()
        mem_lines = [json.dumps(dict(r), ensure_ascii=False, sort_keys=True) + "\n" for r in rows]
        mem_hash = hashlib.sha256("".join(mem_lines).encode("utf-8")).hexdigest()

        match = (disk_hash == mem_hash)
        results["tables"][table] = {
            "match": match,
            "disk_hash": disk_hash,
            "db_hash": mem_hash,
            "rows": len(rows),
        }
        if not match:
            results["is_consistent"] = False

    # 校验 external_questions.jsonl 一致性（若文件存在或库内存在 EXTERNAL_MISTAKE）
    ext_rows = conn.execute(
        """
        SELECT
            r.question_uid, r.grade_subject, r.source_type, r.source_collection,
            r.source_file, r.source_id, r.raw_chapter, r.raw_question,
            r.raw_images_json, r.cas_images_json AS r_cas_images,
            r.image_captions_json AS r_captions, r.legacy_tags_json,
            r.content_hash, r.plain_fingerprint, r.duplicate_of_uid,
            r.ingested_at, r.updated_at,
            q.m_question_text, q.m_options_json, q.m_format, q.m_visual_facts,
            q.m_verification_status, q.m_final_answers_json, q.m_abstract_skeleton,
            q.m_primary_l1_module, q.m_secondary_l1_modules_json,
            q.m_c_score, q.m_s_score, q.m_t_score, q.m_l_score,
            q.has_images, q.cas_images_json, q.image_captions_json AS q_captions,
            q.is_retired, q.review_reason
        FROM raw_questions r
        LEFT JOIN questions q ON q.question_uid = r.question_uid
        WHERE r.source_type = 'EXTERNAL_MISTAKE'
        ORDER BY r.question_uid ASC;
        """
    ).fetchall()
    ext_fpath = h_dir / "external_questions.jsonl"
    if ext_fpath.exists() or ext_rows:
        ext_disk_hash = hashlib.sha256(ext_fpath.read_bytes()).hexdigest() if ext_fpath.exists() else ""
        ext_mem_lines = [json.dumps(dict(r), ensure_ascii=False, sort_keys=True) + "\n" for r in ext_rows]
        ext_mem_hash = hashlib.sha256("".join(ext_mem_lines).encode("utf-8")).hexdigest()
        ext_match = ext_disk_hash == ext_mem_hash
        results["tables"]["external_questions"] = {
            "match": ext_match,
            "disk_hash": ext_disk_hash,
            "db_hash": ext_mem_hash,
            "rows": len(ext_rows),
        }
        if not ext_match:
            results["is_consistent"] = False

    return results
