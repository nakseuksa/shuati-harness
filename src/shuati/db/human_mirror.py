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
import sqlite3
import time
from pathlib import Path
from typing import Any

from shuati.core import effective


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
    backup_file = b_dir / f"{timestamp}_{command_name}.db"

    # SQLite VACUUM INTO 要求目标文件不存在
    if backup_file.exists():
        backup_file.unlink()

    # 转为正斜杠绝对路径
    posix_path = backup_file.resolve().as_posix()
    conn.execute(f"VACUUM INTO '{posix_path}';")

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
    """导出全量人工资产快照至 human/*.jsonl。"""
    h_dir = Path(human_dir)
    counts: dict[str, int] = {}

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


def restore_snapshot(
    conn: sqlite3.Connection,
    human_dir: Path | str = "human",
    backups_dir: Path | str | None = "data/backups",
    dry_run: bool = False,
) -> dict[str, int]:
    """从 human/*.jsonl 恢复人工资产。

    使用幂等 UPSERT (INSERT ... ON CONFLICT DO UPDATE SET)
    恢复后对涉及题目调用 effective.recompute。
    """
    h_dir = Path(human_dir)
    counts: dict[str, int] = {
        "learners": 0,
        "human_overrides": 0,
        "relation_overrides": 0,
        "practice_log": 0,
    }

    if dry_run:
        for tbl, fname in [
            ("learners", "learners.jsonl"),
            ("human_overrides", "overrides.jsonl"),
            ("relation_overrides", "relation_overrides.jsonl"),
            ("practice_log", "practice_log.jsonl"),
        ]:
            fpath = h_dir / fname
            if fpath.exists():
                with open(fpath, encoding="utf-8") as f:
                    counts[tbl] = sum(1 for line in f if line.strip())
        return counts

    # 若库内已有数据且提供了 backups_dir，恢复前先备份
    has_data = conn.execute("SELECT 1 FROM learners LIMIT 1;").fetchone() is not None
    if has_data and backups_dir:
        vacuum_backup(conn, backups_dir, "human_restore")

    affected_question_uids: set[str] = set()

    with conn:
        # 1. 恢复 learners
        learners_file = h_dir / "learners.jsonl"
        if learners_file.exists():
            # 恢复前重置库中已有当前学习者，避免恢复快照中的当前学习者时触发 uq_learners_current
            conn.execute("UPDATE learners SET is_current = 0 WHERE is_current = 1;")
            with open(learners_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
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
            with open(overrides_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
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
            with open(rel_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
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

        # 4. 恢复 practice_log (按 log_id 升序自然恢复，支持空库恢复)
        pl_file = h_dir / "practice_log.jsonl"
        if pl_file.exists():
            with open(pl_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
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

    # 恢复后对涉及题目调用 effective.recompute
    for uid in affected_question_uids:
        # 仅当题目在 questions 表中存在时才重算
        exists = conn.execute("SELECT 1 FROM questions WHERE question_uid = ?", (uid,)).fetchone()
        if exists:
            effective.recompute(conn, uid)
    conn.commit()

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

    return results
