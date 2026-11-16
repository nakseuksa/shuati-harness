# Task Contract: P12-C — Non-Blocking Prewarm on Drill Failure & UTC Timezone Alignment (Risk 4 & Risk 5)

## Context & Goal
1. **Risk 4 (Synchronous Prewarm Blocking `POST /api/v1/drills/daily`)**:
   - In `backend/main.py:submit_drill`, when a blind drill fails and returns `outcome.retrain_session_id`, the route currently `await prewarm_session_questions(...)` synchronously on the HTTP request, blocking for 15–90s under real LLM gateways instead of launching background prewarm via `in_flight_prewarm_tasks` and letting the client poll `GET /api/v1/sessions/{session_id}/view`.
2. **Risk 5 (UTC vs Local Date Mismatch & Deferral Timestamp Truncation)**:
   - `backend/services/scaffold_engine.py`, SQLite `CURRENT_TIMESTAMP`, and `tests/test_e2e_cuj_flows.py` use **UTC** (`datetime.now(UTC)`), whereas `backend/main.py` (`date.today()`), `backend/services/blind_drill_scheduler.py` (`date.today()`), and `frontend/app.js` (`todayISO()` local JS date) use **local** timezone, causing day-boundary drift (e.g. 00:00–08:59 in East Asia) between frontend requests, blind drill windows, and scaffold cooldown checks.
   - Additionally, `backend/services/blind_drill_scheduler.py` defers excess cards using `date(blind_test_unlock_at, '+1 day')` / `date(blind_test_due_at, '+1 day')`, truncating `"YYYY-MM-DD HH:MM:SS"` timestamps to `"YYYY-MM-DD"`.

## File Whitelist (Strict)
- `backend/services/scaffold_engine.py`
- `backend/services/blind_drill_scheduler.py`
- `backend/main.py`
- `frontend/app.js`
- `tests/test_scaffold_engine.py`
- `tests/test_blind_drill_scheduler.py`
- `tests/test_api_routes.py`
- `tests/test_e2e_cuj_flows.py`

## Requirements

### Part 1: Risk 4 — Background Prewarm in `POST /api/v1/drills/daily` (`scaffold_engine.py`, `main.py`)
1. Export a clean public helper in `backend/services/scaffold_engine.py`, e.g. `schedule_session_prewarm(session_id: str, *, refresh_scaffold: bool = True, db_path: Path | None = None) -> asyncio.Task[None] | None` (check existing `_ensure_prewarm_task` signature & behavior first!):
   - Marks `diagnostic_sessions.prewarm_status = 'RUNNING'` when launching (unless already `RUNNING`/ready cleanly? Wait: on drill failure retrain, we DO want `prewarm_status = 'RUNNING'` immediately before returning `outcome`), clears/preserves fields appropriately, and calls `_ensure_prewarm_task(...)` so `in_flight_prewarm_tasks[session_id]` holds the running `asyncio.Task` and avoids duplicate concurrent tasks for the same `session_id`.
   - Ensure `_ensure_prewarm_task` forwards `refresh_scaffold` accurately to `prewarm_session_questions(session_id, db_path=db_path, refresh_scaffold=refresh_scaffold)`! (Inspect lines 425-470 of `scaffold_engine.py` to make sure `_ensure_prewarm_task` supports `refresh_scaffold: bool = True` without breaking existing callers like `enter_retrain_or_scaffold`.)
2. Update `backend/main.py:submit_drill`:
   - Replace blocking `await prewarm_session_questions(...)` with `schedule_session_prewarm(outcome.retrain_session_id, refresh_scaffold=outcome.retrain_stage != "STAGE_4_DECRUTCH_VARIANT")` (note `from backend.config import settings` -> pass `settings.SQLITE_PATH` or `None` consistently with `get_db_connection()`).
   - `submit_drill` must return `BlindDrillOutcomeDTO` immediately without waiting for LLM prewarm to finish!
   - Keep `test_drill_failure_returns_retrain_session_id_and_prewarms` (`tests/test_api_routes.py:1234`, which awaits `scaffold_engine.in_flight_prewarm_tasks.values()`) passing 100%, and add a test proving `submit_drill` returns BEFORE a gated/slow prewarm task finishes while registering the background task in `in_flight_prewarm_tasks`.

### Part 2: Risk 5 — Unified UTC Date Semantics & Format-Preserving Deferral (`main.py`, `blind_drill_scheduler.py`, `frontend/app.js`, `scaffold_engine.py`)
1. Unify server default `"today"` on **UTC** (`datetime.now(UTC).date()`, matching SQLite `CURRENT_TIMESTAMP` and `scaffold_engine.py` `_TS_FORMAT`):
   - `backend/main.py`: `get_daily_drills` (`active_date`) and `submit_drill` (`body.active_date`) default to `datetime.now(UTC).date()` instead of `date.today()`.
   - `backend/services/blind_drill_scheduler.py`: `get_daily_drills` (and any remaining `date.today()` defaults, plus docstring!) default `active_date` to `datetime.now(UTC).date()`.
   - Check `backend/services/scaffold_engine.py`: verify cooldown date comparison and `blind_test_unlock_at` / `blind_test_due_at` consistently use `datetime.now(UTC)` (`_TS_FORMAT`).
2. Unify frontend `todayISO()` in `frontend/app.js`:
   - Return UTC `YYYY-MM-DD` (`new Date().toISOString().slice(0, 10)`) instead of local `getFullYear()/getMonth()/getDate()`, matching backend UTC `CURRENT_TIMESTAMP` and default `active_date`. (Verify `node --check frontend/app.js` stays green.)
3. Preserve timestamp granularity during overload deferral in `backend/services/blind_drill_scheduler.py`:
   - When deferring excess due cards `+1 day`, preserve `"YYYY-MM-DD HH:MM:SS"` when time is present and `"YYYY-MM-DD"` when date-only:
     ```sql
     blind_test_unlock_at = CASE
         WHEN length(blind_test_unlock_at) > 10 THEN datetime(blind_test_unlock_at, '+1 day')
         ELSE date(blind_test_unlock_at, '+1 day')
     END
     ```
     and equivalently for `blind_test_due_at` when `blind_test_due_at IS NOT NULL` (`CASE WHEN length(blind_test_due_at) > 10 THEN datetime(blind_test_due_at, '+1 day') ELSE date(blind_test_due_at, '+1 day') END`).
4. Update & add tests:
   - Update `tests/test_api_routes.py:581` (`test_drills_daily_default_active_date_today`) from `date.today()` to `datetime.now(UTC).date()` so it is timezone-safe across midnight boundaries.
   - Add test in `tests/test_blind_drill_scheduler.py`: overload deferral preserves `%Y-%m-%d %H:%M:%S` timestamp on `blind_test_unlock_at` and `blind_test_due_at` while still advancing +1 day AND preserving `%Y-%m-%d` format for date-only values.
   - Verify `tests/test_scaffold_engine.py` and `tests/test_e2e_cuj_flows.py` pass without regression.

## Constraints
- Do NOT edit files outside the whitelist.
- Do NOT run `git commit` or `git push`.
- Targeted verification after edits:
  `.venv/bin/python -m pytest tests/test_scaffold_engine.py tests/test_blind_drill_scheduler.py tests/test_api_routes.py tests/test_e2e_cuj_flows.py -q && .venv/bin/ruff check backend/services/scaffold_engine.py backend/services/blind_drill_scheduler.py backend/main.py tests/test_scaffold_engine.py tests/test_blind_drill_scheduler.py tests/test_api_routes.py tests/test_e2e_cuj_flows.py && .venv/bin/mypy backend/services/scaffold_engine.py backend/services/blind_drill_scheduler.py backend/main.py && node --check frontend/app.js`
