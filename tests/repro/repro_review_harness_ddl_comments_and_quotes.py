"""Repro #1: DDL AST extractor fails on SQL comments, quoted identifiers, and case-insensitive names."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_harness as rh  # noqa: E402


def test_repro_ddl_leading_sql_comments_not_dropped(tmp_path: Path) -> None:
    schema_py = tmp_path / "schema.py"
    schema_py.write_text(
        "DDL_STATEMENTS = [\n"
        '    """\n'
        "    -- 1. 核心题目主表\n"
        "    CREATE TABLE IF NOT EXISTS questions (\n"
        "        question_uid TEXT PRIMARY KEY\n"
        "    );\n"
        '    """,\n'
        '    """\n'
        "    -- 题目主索引（带前置行注释时不得被静默丢弃）\n"
        "    CREATE INDEX IF NOT EXISTS idx_q_uid ON questions(question_uid);\n"
        '    """,\n'
        '    """\n'
        "    /* 21. FTS5 虚表块注释 */\n"
        "    CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(question_uid);\n"
        '    """,\n'
        "]\n",
        encoding="utf-8",
    )

    known = rh.list_schema_tables(schema_py)
    assert {"questions", "questions_fts"}.issubset({t.lower() for t in known})

    ddl = rh.extract_ddl_for_tables(schema_py, ["questions", "questions_fts"])
    assert "CREATE TABLE IF NOT EXISTS questions" in ddl
    assert "CREATE INDEX IF NOT EXISTS idx_q_uid ON questions" in ddl
    assert "CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts" in ddl


def test_repro_ddl_quoted_identifiers_match_and_mismatched_quotes_rejected(tmp_path: Path) -> None:
    schema_py = tmp_path / "schema.py"
    schema_py.write_text(
        "DDL_STATEMENTS = [\n"
        '    \'CREATE TABLE IF NOT EXISTS "quoted_questions" (id TEXT PRIMARY KEY);\',\n'
        '    \'CREATE UNIQUE INDEX IF NOT EXISTS "uq_qq_id" ON "quoted_questions"(id);\',\n'
        "    'CREATE TABLE IF NOT EXISTS `backtick_tbl` (id INT);',\n"
        "    'CREATE TABLE IF NOT EXISTS [bracket_tbl] (id INT);',\n"
        "]\n",
        encoding="utf-8",
    )

    known = rh.list_schema_tables(schema_py)
    assert {"quoted_questions", "backtick_tbl", "bracket_tbl"} == {t.lower() for t in known}

    ddl = rh.extract_ddl_for_tables(schema_py, ["quoted_questions", "backtick_tbl", "bracket_tbl"])
    assert '"quoted_questions"' in ddl
    assert '"uq_qq_id"' in ddl
    assert "`backtick_tbl`" in ddl
    assert "[bracket_tbl]" in ddl

    bad_quote_schema = tmp_path / "bad_quote_schema.py"
    bad_quote_schema.write_text(
        "DDL_STATEMENTS = [\n"
        '    \'CREATE TABLE "mismatched_quote] (id INT);\',\n'
        "]\n",
        encoding="utf-8",
    )
    assert rh.list_schema_tables(bad_quote_schema) == set()


def test_repro_ddl_case_insensitive_sqlite_table_lookup(tmp_path: Path) -> None:
    schema_py = tmp_path / "schema.py"
    schema_py.write_text(
        "DDL_STATEMENTS = [\n"
        '    "CREATE TABLE IF NOT EXISTS MixedCaseItems (id INTEGER PRIMARY KEY);",\n'
        '    "CREATE INDEX IF NOT EXISTS idx_mc_id ON mixedcaseitems(id);", \n'
        "]\n",
        encoding="utf-8",
    )

    ddl = rh.extract_ddl_for_tables(schema_py, ["mixedcaseitems"])
    assert "CREATE TABLE IF NOT EXISTS MixedCaseItems" in ddl
    assert "CREATE INDEX IF NOT EXISTS idx_mc_id ON mixedcaseitems" in ddl


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
