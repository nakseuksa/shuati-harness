"""生效层唯一写入者（决策 D5，DESIGN.md §3.2-4 与 §4.8）。

写入铁律：
1. 本模块是全系统唯一允许写入 questions.eff_* 列的模块；
2. 基于 sqlite3.Connection.set_authorizer 安装写拦截守卫；未在授权上下文内对 questions.eff_*
   执行 UPDATE 将被底层确定性拒绝（SQLITE_DENY）；
3. recompute(conn, uid) 按契约解析全部 eff_* 列并刷新 questions_fts。
"""

from __future__ import annotations

import contextvars
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from shuati.core.difficulty import compute_difficulty
from shuati.core.text_norm import detect_and_extract_choice_options

_AUTHORIZED_WRITER = contextvars.ContextVar("shuati_authorized_effective_writer", default=False)


@contextmanager
def authorized_effective_writer() -> Iterator[None]:
    """仅供本模块内部使用的授权写入上下文。"""
    token = _AUTHORIZED_WRITER.set(True)
    try:
        yield
    finally:
        _AUTHORIZED_WRITER.reset(token)


def install_eff_write_guard(conn: sqlite3.Connection) -> None:
    """在 SQLite 连接上安装生效列安全守卫。

    拦截任何非授权上下文下对 questions 表 eff_* 列的 UPDATE 与 INSERT。
    """
    try:
        conn.create_function("_shuati_is_eff_auth", 0, lambda: 1 if _AUTHORIZED_WRITER.get() else 0)
    except Exception:
        pass

    def _authorizer_callback(action: int, arg1: Any, arg2: Any, db_name: Any, trigger_name: Any) -> int:
        # SQLITE_UPDATE = 23
        if action == sqlite3.SQLITE_UPDATE and arg1 == "questions":
            if isinstance(arg2, str) and arg2.startswith("eff_"):
                if not _AUTHORIZED_WRITER.get():
                    return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(_authorizer_callback)

    # 检查 questions 表是否存在；若存在则创建 TEMP TRIGGER（严禁用宽泛 except 吞掉异常）
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='questions';").fetchone()
    if row is not None:
        conn.execute("""
            CREATE TEMP TRIGGER IF NOT EXISTS _guard_questions_eff_insert
            BEFORE INSERT ON questions
            BEGIN
                SELECT CASE
                    WHEN _shuati_is_eff_auth() = 0 AND (
                        NEW.eff_question_text IS NOT '' OR
                        NEW.eff_options_json IS NOT '{}' OR
                        NEW.eff_format IS NOT '解答题' OR
                        NEW.eff_visual_facts IS NOT '' OR
                        NEW.eff_verification_status IS NOT 'PENDING' OR
                        NEW.eff_review_reason IS NOT '' OR
                        NEW.eff_verification_mode IS NOT 'DETERMINISTIC' OR
                        NEW.eff_final_answers_json IS NOT '[]' OR
                        NEW.eff_abstract_skeleton IS NOT '' OR
                        NEW.eff_primary_l1_module IS NOT '' OR
                        NEW.eff_secondary_l1_modules_json IS NOT '[]' OR
                        NEW.eff_node_ids_json IS NOT '[]' OR
                        NEW.eff_c_score IS NOT NULL OR
                        NEW.eff_s_score IS NOT NULL OR
                        NEW.eff_t_score IS NOT NULL OR
                        NEW.eff_l_score IS NOT NULL OR
                        NEW.eff_base_elo IS NOT NULL OR
                        NEW.eff_tier IS NOT NULL OR
                        NEW.eff_tier_rank IS NOT NULL OR
                        NEW.eff_is_excluded IS NOT 0 OR
                        NEW.eff_is_servable IS NOT 0 OR
                        NEW.eff_overridden_fields_json IS NOT '[]'
                    )
                    THEN RAISE(ABORT, 'unauthorized column write: questions.eff_*')
                END;
            END;
        """)


def _resolve_canonical_node_id(conn: sqlite3.Connection, node_id: str) -> str | None:
    """通过 redirect_to_id 并查集追踪活跃主节点 ID，核验其在 knowledge_nodes 中存在且为 L3 节点。"""
    curr = node_id
    visited = set()
    while curr and curr not in visited:
        visited.add(curr)
        row = conn.execute(
            "SELECT redirect_to_id, node_level FROM knowledge_nodes WHERE node_id = ?",
            (curr,),
        ).fetchone()
        if not row:
            return None
        if not row["redirect_to_id"]:
            return curr if row["node_level"] == "L3" else None
        curr = row["redirect_to_id"]
    if curr:
        row = conn.execute(
            "SELECT node_level FROM knowledge_nodes WHERE node_id = ?",
            (curr,),
        ).fetchone()
        if row and row["node_level"] == "L3":
            return curr
    return None


def recompute(conn: sqlite3.Connection, question_uid: str) -> None:
    """依据机器层 (m_*) 与有效人工覆写重算全部生效列 (eff_*)。"""
    # 1. 读取 questions 机器字段与基础元数据
    q_row = conn.execute(
        """
        SELECT
            question_uid, grade_subject, is_retired,
            m_question_text, m_options_json, m_format, m_visual_facts,
            m_verification_status, m_final_answers_json, m_abstract_skeleton,
            m_primary_l1_module, m_secondary_l1_modules_json,
            m_c_score, m_s_score, m_t_score, m_l_score,
            review_reason
        FROM questions
        WHERE question_uid = ?
        """,
        (question_uid,),
    ).fetchone()

    if not q_row:
        return

    # 2. 读取 raw_questions content_hash 用于校验覆写指纹
    raw_row = conn.execute(
        "SELECT content_hash FROM raw_questions WHERE question_uid = ?",
        (question_uid,),
    ).fetchone()
    current_content_hash = raw_row["content_hash"] if raw_row else ""

    # 3. 读取该题的人工覆写记录
    overrides = conn.execute(
        """
        SELECT
            override_id, field_name, override_value, base_fingerprint,
            is_active, is_suspended, suspended_reason, bundle_id
        FROM human_overrides
        WHERE question_uid = ? AND is_active = 1
        """,
        (question_uid,),
    ).fetchall()

    # 识别需要挂起的 bundle（同 bundle_id 整组挂起）与单独覆写，记录真实触发原因
    bundle_reasons: dict[str, str] = {}
    stale_override_reasons: dict[int, str] = {}

    for ov in overrides:
        bid = ov["bundle_id"]
        is_fingerprint_stale = ov["base_fingerprint"] != current_content_hash
        is_already_suspended = ov["is_suspended"] == 1

        if is_fingerprint_stale or is_already_suspended:
            if is_fingerprint_stale:
                reason = "SOURCE_CHANGED"
            else:
                reason = ov["suspended_reason"] or "SOURCE_CHANGED"

            if bid:
                # 若有成员指纹失配，优先 SOURCE_CHANGED
                if bid not in bundle_reasons or reason == "SOURCE_CHANGED":
                    bundle_reasons[bid] = reason
            else:
                stale_override_reasons[ov["override_id"]] = reason

    # 执行整组或单条挂起持久化（仅挂起原本未挂起 is_suspended == 0 的记录）
    for bid, reason in bundle_reasons.items():
        conn.execute(
            """
            UPDATE human_overrides
            SET is_suspended = 1, suspended_reason = ?
            WHERE question_uid = ? AND bundle_id = ? AND is_active = 1 AND is_suspended = 0
            """,
            (reason, question_uid, bid),
        )

    for oid, reason in stale_override_reasons.items():
        conn.execute(
            """
            UPDATE human_overrides
            SET is_suspended = 1, suspended_reason = ?
            WHERE override_id = ? AND is_suspended = 0
            """,
            (reason, oid),
        )

    active_overrides: dict[str, dict[str, Any]] = {}
    has_suspended_verification_override = False

    for ov in overrides:
        bid = ov["bundle_id"]
        is_stale = (
            (bid and bid in bundle_reasons)
            or (ov["override_id"] in stale_override_reasons)
            or (ov["base_fingerprint"] != current_content_hash)
            or (ov["is_suspended"] == 1)
        )
        if is_stale:
            if ov["field_name"] in (
                "question_text",
                "visual_facts",
                "final_answers_json",
                "solutions_override_json",
                "verification_status",
                "format",
                "is_excluded",
            ):
                has_suspended_verification_override = True
            continue

        active_overrides[ov["field_name"]] = dict(ov)

    # 4. 逐字段解析生效值（DESIGN.md §3.2-4 逐字段解析表）
    # (1) 题干与格式
    eff_question_text = (
        active_overrides["question_text"]["override_value"]
        if "question_text" in active_overrides
        else q_row["m_question_text"]
    )

    eff_format = (
        active_overrides["format"]["override_value"]
        if "format" in active_overrides
        else q_row["m_format"]
    )

    # (2) 选项表：若为选择题则从生效题干提取
    if eff_format == "选择题":
        if "question_text" in active_overrides or not q_row["m_options_json"] or q_row["m_options_json"] == "{}":
            is_ch, extracted_opts = detect_and_extract_choice_options(eff_question_text)
            eff_options_json = json.dumps(extracted_opts, ensure_ascii=False) if (is_ch and extracted_opts) else "{}"
        else:
            eff_options_json = q_row["m_options_json"] or "{}"
    else:
        eff_options_json = "{}"

    # (3) 视觉事实
    eff_visual_facts = (
        active_overrides["visual_facts"]["override_value"]
        if "visual_facts" in active_overrides
        else q_row["m_visual_facts"]
    )

    # (4) 最终答案与验真模式
    eff_final_answers_json = (
        active_overrides["final_answers_json"]["override_value"]
        if "final_answers_json" in active_overrides
        else q_row["m_final_answers_json"]
    )

    try:
        parts = json.loads(eff_final_answers_json) if eff_final_answers_json else []
    except Exception:
        parts = []

    if not parts:
        eff_verification_mode = "DETERMINISTIC"
    else:
        vmodes = {p.get("verify_mode", "SYMBOLIC") for p in parts if isinstance(p, dict)}
        if vmodes.issubset({"SYMBOLIC", "EXACT"}):
            eff_verification_mode = "DETERMINISTIC"
        elif vmodes == {"QUALITATIVE"}:
            eff_verification_mode = "QUALITATIVE"
        else:
            eff_verification_mode = "MIXED"

    # (5) 验真状态与复核原因
    machine_status = q_row["m_verification_status"]
    machine_reason = q_row["review_reason"] or ""
    machine_reasons = [r.strip() for r in machine_reason.split(",") if r.strip()]

    if has_suspended_verification_override:
        eff_verification_status = "NEEDS_REVIEW"
        eff_review_reason = (machine_reason + ",OVERRIDE_STALE").strip(",")
    elif (
        "ANSWER_SOLUTION_CONFLICT" in machine_reasons
        and "solutions_override_json" not in active_overrides
    ):
        eff_verification_status = "NEEDS_REVIEW"
        eff_review_reason = machine_reason
    elif "visual_facts" in active_overrides and "final_answers_json" not in active_overrides:
        eff_verification_status = "NEEDS_REVIEW"
        eff_review_reason = (machine_reason + ",VISUAL_FACTS_EDITED_PENDING_CONFIRM").strip(",")
    elif "verification_status" in active_overrides:
        eff_verification_status = active_overrides["verification_status"]["override_value"]
        eff_review_reason = machine_reason
    elif "final_answers_json" in active_overrides or "solutions_override_json" in active_overrides:
        eff_verification_status = "HUMAN_VERIFIED"
        eff_review_reason = machine_reason
    else:
        eff_verification_status = machine_status
        eff_review_reason = machine_reason

    # (6) 抽象骨架
    eff_abstract_skeleton = (
        active_overrides["abstract_skeleton"]["override_value"]
        if "abstract_skeleton" in active_overrides
        else q_row["m_abstract_skeleton"]
    )

    # (7) L1 模块归属
    eff_primary_l1_module = (
        active_overrides["primary_l1_module"]["override_value"]
        if "primary_l1_module" in active_overrides
        else q_row["m_primary_l1_module"]
    )

    eff_secondary_l1_modules_json = (
        active_overrides["secondary_l1_modules_json"]["override_value"]
        if "secondary_l1_modules_json" in active_overrides
        else (q_row["m_secondary_l1_modules_json"] or "[]")
    )

    # (8) 四维难度与 Elo / Tier
    if "difficulty_override_json" in active_overrides:
        try:
            d_val = json.loads(active_overrides["difficulty_override_json"]["override_value"])
            eff_c = int(d_val["C"])
            eff_s = int(d_val["S"])
            eff_t = int(d_val["T"])
            eff_l = int(d_val["L"])
        except Exception:
            eff_c, eff_s, eff_t, eff_l = (
                q_row["m_c_score"],
                q_row["m_s_score"],
                q_row["m_t_score"],
                q_row["m_l_score"],
            )
    else:
        eff_c, eff_s, eff_t, eff_l = (
            q_row["m_c_score"],
            q_row["m_s_score"],
            q_row["m_t_score"],
            q_row["m_l_score"],
        )

    if eff_c is not None and eff_s is not None and eff_t is not None and eff_l is not None:
        d_res = compute_difficulty(eff_c, eff_s, eff_t, eff_l)
        eff_base_elo: int | None = d_res.base_elo
        eff_tier: str | None = d_res.tier
        eff_tier_rank: int | None = d_res.tier_rank
    else:
        eff_base_elo = None
        eff_tier = None
        eff_tier_rank = None

    # (9) 下架与可用标记
    if "is_excluded" in active_overrides:
        try:
            eff_is_excluded = int(active_overrides["is_excluded"]["override_value"])
        except Exception:
            eff_is_excluded = 0
    else:
        eff_is_excluded = 0

    eff_is_servable = 1 if (
        q_row["is_retired"] == 0
        and eff_is_excluded == 0
        and eff_verification_status in ("VERIFIED_CONSENSUS", "HUMAN_VERIFIED")
    ) else 0

    # (10) 生效考点节点 (eff_node_ids_json)
    step_rows = conn.execute(
        "SELECT m_knowledge_node_id FROM question_steps WHERE question_uid = ? AND m_knowledge_node_id IS NOT NULL",
        (question_uid,),
    ).fetchall()
    step_node_ids = {r["m_knowledge_node_id"] for r in step_rows if r["m_knowledge_node_id"]}

    if "taxonomy_override_json" in active_overrides:
        try:
            tax_val = json.loads(active_overrides["taxonomy_override_json"]["override_value"])
            mode = tax_val.get("mode", "REPLACE")
            nodes_list = tax_val.get("nodes", [])
            override_node_ids = {n["node_id"] for n in nodes_list if isinstance(n, dict) and "node_id" in n}
            if mode == "REPLACE":
                active_nodes = override_node_ids
            else:
                active_nodes = step_node_ids | override_node_ids
        except Exception:
            active_nodes = step_node_ids
    else:
        active_nodes = step_node_ids

    # 并查集 find() 归一与 L3 节点过滤
    canonical_nodes = sorted({
        c
        for nid in active_nodes
        if nid and (c := _resolve_canonical_node_id(conn, nid)) is not None
    })
    eff_node_ids_json = json.dumps(canonical_nodes, ensure_ascii=False)

    # (11) 当前生效覆写字段列表
    eff_overridden_fields = sorted(list(active_overrides.keys()))
    eff_overridden_fields_json = json.dumps(eff_overridden_fields, ensure_ascii=False)

    # 5. 在授权上下文中更新 questions 表 eff_* 列
    with authorized_effective_writer():
        conn.execute(
            """
            UPDATE questions
            SET
                eff_question_text = ?,
                eff_options_json = ?,
                eff_format = ?,
                eff_visual_facts = ?,
                eff_verification_status = ?,
                eff_review_reason = ?,
                eff_verification_mode = ?,
                eff_final_answers_json = ?,
                eff_abstract_skeleton = ?,
                eff_primary_l1_module = ?,
                eff_secondary_l1_modules_json = ?,
                eff_node_ids_json = ?,
                eff_c_score = ?,
                eff_s_score = ?,
                eff_t_score = ?,
                eff_l_score = ?,
                eff_base_elo = ?,
                eff_tier = ?,
                eff_tier_rank = ?,
                eff_is_excluded = ?,
                eff_is_servable = ?,
                eff_overridden_fields_json = ?,
                eff_refreshed_at = datetime('now'),
                updated_at = datetime('now')
            WHERE question_uid = ?
            """,
            (
                eff_question_text,
                eff_options_json,
                eff_format,
                eff_visual_facts,
                eff_verification_status,
                eff_review_reason,
                eff_verification_mode,
                eff_final_answers_json,
                eff_abstract_skeleton,
                eff_primary_l1_module,
                eff_secondary_l1_modules_json,
                eff_node_ids_json,
                eff_c,
                eff_s,
                eff_t,
                eff_l,
                eff_base_elo,
                eff_tier,
                eff_tier_rank,
                eff_is_excluded,
                eff_is_servable,
                eff_overridden_fields_json,
                question_uid,
            ),
        )

        # 6. 同步刷新 questions_fts 虚表
        conn.execute("DELETE FROM questions_fts WHERE question_uid = ?", (question_uid,))
        conn.execute(
            """
            INSERT INTO questions_fts(question_uid, grade_subject, question_text, visual_facts, abstract_skeleton)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                question_uid,
                q_row["grade_subject"],
                eff_question_text,
                eff_visual_facts,
                eff_abstract_skeleton,
            ),
        )


def recompute_all(conn: sqlite3.Connection, grade_subject: str | None = None) -> int:
    """全量重算生效层（供 shuati reindex --effective 使用）。"""
    if grade_subject:
        rows = conn.execute(
            "SELECT question_uid FROM questions WHERE grade_subject = ?",
            (grade_subject,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT question_uid FROM questions").fetchall()

    count = 0
    for r in rows:
        recompute(conn, r["question_uid"])
        count += 1
    return count


def verify_effective_consistency(conn: sqlite3.Connection) -> dict[str, Any]:
    """验证库内 eff_* 列与重算结果的一致性（Stage Q 巡检）。"""
    rows = conn.execute("SELECT question_uid FROM questions").fetchall()
    diffs: list[dict[str, Any]] = []

    for r in rows:
        uid = r["question_uid"]
        # 保存当前库内值
        before = dict(
            conn.execute(
                "SELECT * FROM questions WHERE question_uid = ?", (uid,)
            ).fetchone()
        )
        recompute(conn, uid)
        after = dict(
            conn.execute(
                "SELECT * FROM questions WHERE question_uid = ?", (uid,)
            ).fetchone()
        )

        # 对比忽略时间戳差异
        for k in before:
            if k.endswith("_at"):
                continue
            if before[k] != after[k]:
                diffs.append(
                    {
                        "question_uid": uid,
                        "field": k,
                        "db_val": before[k],
                        "recomputed_val": after[k],
                    }
                )

    return {
        "total_checked": len(rows),
        "diff_count": len(diffs),
        "diffs": diffs,
    }
