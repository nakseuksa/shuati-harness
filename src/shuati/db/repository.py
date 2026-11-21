"""题库基础仓储查询、UID 解析与 FTS5 检索（DESIGN.md §3.2-13 & §3.2-2）。"""

from __future__ import annotations

import sqlite3
from typing import Any


class AmbiguousUidError(Exception):
    """简写 ID 匹配到多个题目候选。"""

    def __init__(self, candidates: list[str]) -> None:
        super().__init__(f"简写 ID 匹配到多道题，请使用完整 question_uid: {candidates}")
        self.candidates = candidates


class NotFoundError(Exception):
    """题目未找到。"""
    pass


def _follow_duplicates(conn: sqlite3.Connection, initial_uid: str) -> str:
    """循环追踪 duplicate_of_uid 至最终主题（带 visited 防环保护）。"""
    curr = initial_uid
    visited = set()
    while curr and curr not in visited:
        visited.add(curr)
        row = conn.execute(
            "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid = ?",
            (curr,),
        ).fetchone()
        if not row or not row["duplicate_of_uid"]:
            return curr
        curr = row["duplicate_of_uid"]
    return curr


def resolve_question_uid(conn: sqlite3.Connection, id_or_uid: str) -> str:
    """支持完整 question_uid 与 source_id 简写解析。

    - 唯一匹配时返回 uid；
    - 歧义时抛 AmbiguousUidError(candidates)；
    - 匹配到同题副本 (duplicate_of_uid 非空) 时自动重定向至主题 uid；
    - 未匹配抛 NotFoundError。
    """
    id_or_uid = id_or_uid.strip()

    # 1. 完整 UID 精确匹配
    if ":" in id_or_uid:
        row = conn.execute(
            "SELECT question_uid FROM raw_questions WHERE question_uid = ?",
            (id_or_uid,),
        ).fetchone()
        if row:
            return _follow_duplicates(conn, row["question_uid"])

    # 2. source_id 简写匹配
    rows = conn.execute(
        "SELECT question_uid FROM raw_questions WHERE source_id = ?",
        (id_or_uid,),
    ).fetchall()

    if len(rows) == 1:
        return _follow_duplicates(conn, rows[0]["question_uid"])
    elif len(rows) > 1:
        candidates = [r["question_uid"] for r in rows]
        raise AmbiguousUidError(candidates)

    # 3. 兜底检查（如传入的不带冒号但等于 question_uid）
    row = conn.execute(
        "SELECT question_uid FROM raw_questions WHERE question_uid = ?",
        (id_or_uid,),
    ).fetchone()
    if row:
        return _follow_duplicates(conn, row["question_uid"])

    raise NotFoundError(f"题目 '{id_or_uid}' 未找到")


def refresh_search_index(conn: sqlite3.Connection, uid: str) -> None:
    """刷新指定题目的 FTS5 索引。"""
    row = conn.execute(
        """
        SELECT question_uid, grade_subject, eff_question_text, eff_visual_facts, eff_abstract_skeleton
        FROM questions
        WHERE question_uid = ?
        """,
        (uid,),
    ).fetchone()

    conn.execute("DELETE FROM questions_fts WHERE question_uid = ?", (uid,))
    if row:
        conn.execute(
            """
            INSERT INTO questions_fts(question_uid, grade_subject, question_text, visual_facts, abstract_skeleton)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["question_uid"],
                row["grade_subject"],
                row["eff_question_text"],
                row["eff_visual_facts"],
                row["eff_abstract_skeleton"],
            ),
        )


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """全量重建 questions_fts 虚表。"""
    conn.execute("DELETE FROM questions_fts;")
    rows = conn.execute(
        """
        SELECT question_uid, grade_subject, eff_question_text, eff_visual_facts, eff_abstract_skeleton
        FROM questions
        """
    ).fetchall()

    for r in rows:
        conn.execute(
            """
            INSERT INTO questions_fts(question_uid, grade_subject, question_text, visual_facts, abstract_skeleton)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                r["question_uid"],
                r["grade_subject"],
                r["eff_question_text"],
                r["eff_visual_facts"],
                r["eff_abstract_skeleton"],
            ),
        )
    return len(rows)


def search_fts(
    conn: sqlite3.Connection,
    query_text: str,
    grade_subject: str | None = None,
    servable_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """全文检索（FTS5 trigram + <3字短词 LIKE 回退，DESIGN.md §3.2-13）。"""
    from shuati.core.text_norm import _FULLWIDTH_TO_HALFWIDTH

    query_text = query_text.translate(_FULLWIDTH_TO_HALFWIDTH)
    tokens = [t.strip() for t in query_text.split() if t.strip()]
    if not tokens:
        return []

    # 分离 >= 3 字的 token（走 FTS5 MATCH）与 < 3 字的 token（走 LIKE）
    match_tokens = []
    like_tokens = []

    for t in tokens:
        if len(t) >= 3:
            # 双引号转义并用双引号包裹
            escaped = t.replace('"', '""')
            match_tokens.append(f'"{escaped}"')
        else:
            like_tokens.append(t)

    where_clauses: list[str] = []
    params: list[Any] = []

    if servable_only:
        where_clauses.append("q.eff_is_servable = 1")
    if grade_subject:
        where_clauses.append("q.grade_subject = ?")
        params.append(grade_subject)

    # 拼接 FTS MATCH
    if match_tokens:
        fts_query = " AND ".join(match_tokens)
        where_clauses.append(
            "q.question_uid IN (SELECT question_uid FROM questions_fts WHERE questions_fts MATCH ?)"
        )
        params.append(fts_query)

    # 拼接短词 LIKE
    for lt in like_tokens:
        escaped_lt = lt.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_clauses.append(
            "(q.eff_question_text LIKE ? ESCAPE '\\' OR q.eff_visual_facts LIKE ? ESCAPE '\\' OR q.eff_abstract_skeleton LIKE ? ESCAPE '\\')"
        )
        pattern = f"%{escaped_lt}%"
        params.extend([pattern, pattern, pattern])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = f"""
        SELECT
            q.question_uid, q.grade_subject, q.eff_format,
            q.eff_question_text, q.eff_base_elo, q.eff_tier,
            q.eff_primary_l1_module, q.eff_verification_status,
            q.eff_is_servable
        FROM questions q
        {where_sql}
        ORDER BY q.eff_base_elo ASC
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def add_grade_subject(
    conn: sqlite3.Connection,
    name: str,
    slug: str,
    family: str = "MATH",
    display_name: str | None = None,
) -> None:
    """注册新的学段学科。"""
    disp = display_name or name
    conn.execute(
        """
        INSERT INTO grade_subjects(grade_subject, slug, display_name, subject_family)
        VALUES (?, ?, ?, ?);
        """,
        (name, slug, disp, family),
    )
    conn.commit()


def list_grade_subjects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """列出全部已注册学段学科。"""
    rows = conn.execute("SELECT * FROM grade_subjects ORDER BY created_at ASC;").fetchall()
    return [dict(r) for r in rows]


def get_active_overrides(conn: sqlite3.Connection, question_uid: str) -> list[dict[str, Any]]:
    """获取指定题目的全部活跃人工覆写。"""
    rows = conn.execute(
        """
        SELECT * FROM human_overrides
        WHERE question_uid = ? AND is_active = 1
        ORDER BY created_at DESC;
        """,
        (question_uid,),
    ).fetchall()
    return [dict(r) for r in rows]
