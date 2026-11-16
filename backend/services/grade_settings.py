"""学段学科条目 CRUD 服务（P10-A 分区基座）。

契约：docs/tasks/P10-A_grade_schema_settings.md；为 P10-B（Elo 分区）/P10-C（知识网分区）/
P10-D（录入接线）/P10-E（API）提供条目管理地基。设计权威 §2/§3.4/§3.5 的分区扩展。

架构约定（同 elo_engine / knowledge_graph / blind_drill_scheduler）：
- 所有函数接收调用方管理的 sqlite3.Connection：不自行开连接、不 commit；
- rename / delete 以 SAVEPOINT 保证函数体全程单事务（异常回滚到保存点，无半截改标）；
- 名称一律 strip 规范化；空/纯空白拒绝（ValueError）；create / get_or_create 幂等；
- 本模块只做条目管理与级联改标，不改任何既有引擎行为（分区接线属 P10-B/C/D）。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import BaseModel

__all__ = [
    "GradeSubjectItem",
    "GradeSubjectInUseError",
    "list_grade_subjects",
    "create_grade_subject",
    "get_or_create_grade_subject",
    "rename_grade_subject",
    "delete_grade_subject",
]

# 级联改标的四张分区表（questions 用 target_grade_and_subject 列，单独处理）。
_SCOPED_TABLES: tuple[str, ...] = (
    "knowledge_nodes",
    "elo_mutation_ledger",
    "compaction_audit_ledger",
    "concept_co_errors",
)


class GradeSubjectItem(BaseModel):
    """学段学科条目：名称 / 挂载题目计数 / 创建时间。"""

    grade_subject: str
    question_count: int
    created_at: str


class GradeSubjectInUseError(Exception):
    """删除被拒专用：该学段下仍有题目（携带名称与计数；API 层后续映射 409）。"""

    def __init__(self, grade_subject: str, question_count: int) -> None:
        self.grade_subject = grade_subject
        self.question_count = question_count
        super().__init__(f"学段「{grade_subject}」下仍有 {question_count} 道题目，禁止删除")


@contextmanager
def _savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """条目改标/删除的原子小事务（调用方管理外层事务，本模块不 commit）。"""
    name = f"grade_settings_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def _normalize_name(name: str) -> str:
    """strip 规范化 + 非空校验（空/纯空白 → ValueError）。"""
    stripped = name.strip()
    if not stripped:
        raise ValueError("学段学科名称不能为空或纯空白")
    return stripped


def _question_count(conn: sqlite3.Connection, name: str) -> int:
    """该学段下的题目数（questions.target_grade_and_subject 精确匹配）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM questions WHERE target_grade_and_subject = ?", (name,)
    ).fetchone()
    return int(row["n"])


def list_grade_subjects(conn: sqlite3.Connection) -> list[GradeSubjectItem]:
    """全部条目 + LEFT JOIN questions 计数（含 0 题条目），按 created_at 升序。

    created_at 为秒级 CURRENT_TIMESTAMP，同秒条目以名称决序保证返回稳定。
    """
    rows = conn.execute(
        """
        SELECT g.grade_subject, g.created_at, COUNT(q.question_uid) AS question_count
        FROM grade_subjects g
        LEFT JOIN questions q ON q.target_grade_and_subject = g.grade_subject
        GROUP BY g.grade_subject, g.created_at
        ORDER BY g.created_at, g.grade_subject
        """
    ).fetchall()
    return [
        GradeSubjectItem(
            grade_subject=row["grade_subject"],
            question_count=int(row["question_count"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def create_grade_subject(conn: sqlite3.Connection, name: str) -> tuple[GradeSubjectItem, bool]:
    """新建条目；已存在 → 幂等返回 (既有项, False)，新建 → (新项, True)。

    名称 strip 规范化；空/纯空白 → ValueError。计数实时统计（新建条目也可能已有
    同学段题目先于条目落库，故不硬编码 0）。
    """
    normalized = _normalize_name(name)
    cur = conn.execute(
        "INSERT OR IGNORE INTO grade_subjects (grade_subject) VALUES (?)", (normalized,)
    )
    created = cur.rowcount > 0
    row = conn.execute(
        "SELECT created_at FROM grade_subjects WHERE grade_subject = ?", (normalized,)
    ).fetchone()
    assert row is not None  # 刚 INSERT OR IGNORE，同事务内必然可见
    item = GradeSubjectItem(
        grade_subject=normalized,
        question_count=_question_count(conn, normalized),
        created_at=row["created_at"],
    )
    return item, created


def get_or_create_grade_subject(conn: sqlite3.Connection, name: str) -> str:
    """供 P10-D 录入接线：strip 非空校验 + 幂等建条目，返回规范化名称。"""
    normalized = _normalize_name(name)
    conn.execute(
        "INSERT OR IGNORE INTO grade_subjects (grade_subject) VALUES (?)", (normalized,)
    )
    return normalized


def rename_grade_subject(conn: sqlite3.Connection, old: str, new: str) -> dict[str, int]:
    """单事务级联改标：条目主键 + questions + 四张分区表，返回各表受影响行数。

    old 不存在 / new 已存在 / new 为空 → ValueError（写入前拦截，零残留）；
    五处 UPDATE 在同一 SAVEPOINT 内执行，任一步失败整体回滚，绝无半截改标。
    """
    old_name = _normalize_name(old)
    new_name = _normalize_name(new)
    exists = conn.execute(
        "SELECT 1 FROM grade_subjects WHERE grade_subject = ?", (old_name,)
    ).fetchone()
    if exists is None:
        raise ValueError(f"学段条目不存在: {old_name!r}")
    conflict = conn.execute(
        "SELECT 1 FROM grade_subjects WHERE grade_subject = ?", (new_name,)
    ).fetchone()
    if conflict is not None:
        raise ValueError(f"目标学段条目已存在: {new_name!r}")
    counts: dict[str, int] = {}
    with _savepoint(conn):
        cur = conn.execute(
            "UPDATE grade_subjects SET grade_subject = ? WHERE grade_subject = ?",
            (new_name, old_name),
        )
        counts["grade_subjects"] = cur.rowcount
        cur = conn.execute(
            "UPDATE questions SET target_grade_and_subject = ? WHERE target_grade_and_subject = ?",
            (new_name, old_name),
        )
        counts["questions"] = cur.rowcount
        for table in _SCOPED_TABLES:
            cur = conn.execute(
                f"UPDATE {table} SET grade_subject = ? WHERE grade_subject = ?",
                (new_name, old_name),
            )
            counts[table] = cur.rowcount
    return counts


def delete_grade_subject(conn: sqlite3.Connection, name: str) -> None:
    """删除条目（单事务）；该学段下题目数 > 0 → GradeSubjectInUseError。

    条目不存在时 DELETE 为零行幂等操作，不报错。
    """
    normalized = _normalize_name(name)
    count = _question_count(conn, normalized)
    if count > 0:
        raise GradeSubjectInUseError(normalized, count)
    with _savepoint(conn):
        conn.execute("DELETE FROM grade_subjects WHERE grade_subject = ?", (normalized,))