# Task Contract: P12-D — Question Foreign Key `ON DELETE` Actions & Legacy Table Upgrade (Risk 6)

## Context & Goal
In `backend/db/schema.py`, several foreign keys referencing `questions(question_uid)` omit `ON DELETE` clauses (defaulting to SQLite `NO ACTION` / restrict under `PRAGMA foreign_keys=ON`), which blocks safe cleanup/deletion of questions (especially synthetic `micro_scaffold` / `isomorphic_variant` questions or re-ingested questions):
- `questions.parent_question_uid`: missing `ON DELETE SET NULL`
- `diagnostic_sessions.prewarm_scaffold_uid`: missing `ON DELETE SET NULL`
- `diagnostic_sessions.prewarm_variant_uid`: missing `ON DELETE SET NULL`
- `elo_mutation_ledger.question_uid` (`NOT NULL`): missing `ON DELETE CASCADE`
- `blind_drill_attempts.drill_question_uid` (`NOT NULL`): missing `ON DELETE CASCADE`

Because `CREATE TABLE IF NOT EXISTS` does not update FK constraints on pre-existing SQLite databases, `init_db()` also needs an idempotent, transaction-safe schema migration when any of those tables lacks the expected `ON DELETE` action.

## File Whitelist (Strict)
- `backend/db/schema.py`
- `tests/test_schema.py`

## Requirements
1. Update `TABLE_DDLS` in `backend/db/schema.py`:
   - `questions`: `parent_question_uid TEXT REFERENCES questions(question_uid) ON DELETE SET NULL`
   - `diagnostic_sessions`:
     - `prewarm_scaffold_uid TEXT REFERENCES questions(question_uid) ON DELETE SET NULL`
     - `prewarm_variant_uid TEXT REFERENCES questions(question_uid) ON DELETE SET NULL`
   - `elo_mutation_ledger`: `question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE`
   - `blind_drill_attempts`: `drill_question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE`
2. Add idempotent FK upgrade migration in `backend/db/schema.py` invoked by `init_db`:
   - Inspect `PRAGMA foreign_key_list(table_name)` for `("questions", "diagnostic_sessions", "elo_mutation_ledger", "blind_drill_attempts")`.
   - Only when a table exists AND one of the above `from` columns references `questions` with an unexpected `on_delete` (e.g., `"NO ACTION"` instead of `"SET NULL"` / `"CASCADE"`), rebuild that table cleanly preserving ALL existing columns and data:
     - **Critical SQLite rule**: `PRAGMA foreign_keys = OFF` ONLY takes effect when `conn.in_transaction` is `False`! Commit/use clean non-transaction state before `PRAGMA foreign_keys = OFF`, rebuild via temporary table `_p12d_migrate_<tbl>` with explicit column list copied (`SELECT <cols> FROM <tbl>` — handle column order dynamically from `PRAGMA table_info(<tbl>)` in case legacy tables were altered in different order!), `DROP TABLE <tbl>`, `ALTER TABLE ... RENAME TO <tbl>`, restore `PRAGMA foreign_keys = ON`, verify `PRAGMA foreign_key_check(<tbl>)` returns empty, and recreate indexes via `INDEX_DDLS`!
   - When all FK actions already match (`SET NULL` / `CASCADE`), do zero table rebuilds (fast O(1) idempotent `init_db()`).
3. Add comprehensive tests in `tests/test_schema.py`:
   - Verify `PRAGMA foreign_key_list` on fresh `init_db` shows `"SET NULL"` for `questions.parent_question_uid`, `diagnostic_sessions.prewarm_scaffold_uid`, `diagnostic_sessions.prewarm_variant_uid` and `"CASCADE"` for `elo_mutation_ledger.question_uid` and `blind_drill_attempts.drill_question_uid`.
   - Verify runtime deletion behavior under `get_db_connection`: deleting a parent question sets child `questions.parent_question_uid = NULL`, deleting a synthetic scaffold/variant sets `diagnostic_sessions.prewarm_scaffold_uid` / `prewarm_variant_uid = NULL` without deleting the session, and deleting a `drill_question_uid` / `elo_mutation_ledger.question_uid` target cascades cleanly.
   - Verify legacy DB upgrade (`init_db(legacy_db)` twice): build a legacy DB using old FKs without `ON DELETE` clauses containing real rows across all 4 tables, call `init_db(legacy_db)` twice, verify all rows and columns survive intact AND `PRAGMA foreign_key_list` now enforces the updated `ON DELETE` actions.

## Constraints
- Do NOT edit files outside the whitelist.
- Do NOT run `git commit` or `git push`.
- Preserve `test_init_db_adds_prewarm_excluded_uids_column_idempotently` and all existing `tests/test_schema.py` tests.
- Targeted verification after edits:
  `.venv/bin/python -m pytest tests/test_schema.py -q && .venv/bin/ruff check backend/db/schema.py tests/test_schema.py && .venv/bin/mypy backend/db/schema.py`
