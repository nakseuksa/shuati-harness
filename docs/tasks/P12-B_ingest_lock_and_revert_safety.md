# Task Contract: P12-B — Non-Blocking Lock-Free Ingest Judge/Compaction & Delta-Safe DSU Revert (Risk 1 & Risk 2)

## Context & Goal
1. **Risk 1 (SQLite Write-Lock Held Across Async LLM Calls during Ingest)**:
   - In `backend/services/ingest_service.py` (`_ingest_new_question` and `_persist_reimport_draft_session`), `get_or_create_grade_subject` (`INSERT OR IGNORE`) and `_insert_question_row` run inside `with get_db_connection(db_path) as conn:` **before** looping over steps to call `await match_or_create_l3_node(...)`, AND step 1's `INSERT INTO knowledge_nodes` holds an uncommitted SQLite write transaction while step 2+ await `knowledge_graph._judge_nodes(...)` (and `run_scoped_compaction`), keeping SQLite's write lock occupied for 10–300s under real LLM gateways.
2. **Risk 2 (`revert_compaction_batch` Blind Edge Wipe & Non-LIFO Batch Corruption)**:
   - In `backend/services/knowledge_graph.py`, `_revert_single_merge` deletes **all** `concept_co_errors` rows touching `to_id` (`WHERE node_u_id IN affected OR node_v_id IN affected`) before reinserting `previous_co_errors_snapshot_json`. Any co-error edge added to `to_id` (e.g., `(to_id, Y)`) or incremented on `(to_id, X)` **after** the merge is silently destroyed.
   - Furthermore, reverting an older batch (`B1`) when a newer un-reverted merge (`B2`, `is_reverted = 0 AND merge_id > B1.merge_id`) already merged/redirected one of `B1`'s nodes or snapshot neighbor nodes breaks DSU/co-error root invariants.

## File Whitelist (Strict)
- `backend/services/ingest_service.py`
- `backend/services/knowledge_graph.py`
- `tests/test_ingest_service.py`
- `tests/test_knowledge_graph_dsu.py`

## Requirements

### Part 1: Risk 1 — Zero SQLite Write Lock During LLM Network Calls (`ingest_service.py`, `knowledge_graph.py`)
1. Preserve all existing invariants & test contracts:
   - `test_functions_never_commit` (`tests/test_knowledge_graph_dsu.py`): `knowledge_graph` functions accepting `conn` NEVER call `conn.commit()`.
   - `test_transaction_atomic_on_step_insert_failure` (`tests/test_ingest_service.py`): if `_insert_step_row` fails inside the write tx, BOTH `questions` AND newly inserted `knowledge_nodes` roll back completely.
   - `test_auto_compaction_triggered_at_threshold_during_ingest` (`tests/test_ingest_service.py`): monkeypatching `knowledge_graph.run_scoped_compaction` still detects compaction triggered at threshold during ingest.
   - Monkeypatching `ingest_service.match_or_create_l3_node` (`tests/test_ingest_service.py:769,840`) must continue to work cleanly.
2. Split node judging cleanly from SQLite write lock in **both** `_ingest_new_question` and `_persist_reimport_draft_session` (when `match_or_create_l3_node is _ORIG_MATCH_OR_CREATE` — if a test monkeypatched `ingest_service.match_or_create_l3_node`, fall back to calling that patched function inside the write tx as before):
   - **Read-Only Query + Close BEFORE LLM await**: Resolve normalized `tree_grade` via read-only `SELECT` on `grade_subjects` (no `INSERT` yet!) and load current active `knowledge_nodes` for every distinct `(tree_grade, l1_module, l2_entity)` present in the question's branches, then **exit/close the DB connection** before making any `await` LLM calls.
   - **Lock-Free L3 Resolution (`knowledge_graph.plan_l3_node_resolutions` or similar helper)**: Run candidate recall, alias matching, and `await _judge_nodes(...)` purely in memory (tracking newly planned intra-question candidates so step 2 can match a newly planned step 1 node without DB writes!). Zero DB connection or lock is held while `await _judge_nodes(...)` is in flight.
   - **Atomic Write Phase**: Open ONE `with get_db_connection(db_path) as conn:`, ensure `get_or_create_grade_subject`, insert question/rubrics (or update reimport fields), apply the planned node creations/alias merges inside `conn` (re-checking active nodes inside the tx in case a concurrent ingest already created/activated an exact/alias match, inserting new nodes + bumping `compaction_watermarks` using shared helper with `match_or_create_l3_node`), insert `solution_steps`, record co-error edges, and collect any `(tree_grade, l1, l2)` scopes whose watermark reached `COMPACTION_NODE_THRESHOLD`. Commit & exit `conn`!
   - **Post-Commit Non-Blocking Compaction**: After the write transaction closes:
     - If `knowledge_graph.run_scoped_compaction` is monkeypatched (`is not _ORIG_RUN_SCOPED_COMPACTION`), open a short `with get_db_connection(db_path) as conn:` and call `knowledge_graph.run_scoped_compaction(conn, grade, l1, l2)` so existing spy tests observe the call.
     - Otherwise run an async lock-free compaction helper `await knowledge_graph.run_scoped_compaction_unlocked(db_path, grade, l1, l2)` which:
       1) reads active nodes & edge snapshot via a short read-only connection (closed immediately),
       2) runs `await _judge_nodes(...)` outside ANY SQLite connection/transaction,
       3) opens a short `with get_db_connection(db_path) as conn:` write transaction, re-validates that candidate pairs are still `is_active = 1` roots (skipping stale pairs merged concurrently), and executes the DSU merges & watermark reset atomically!

### Part 2: Risk 2 — Dependency/LIFO Guard & Delta-Preserving `concept_co_errors` Revert (`knowledge_graph.py`)
1. **Dependency / LIFO Guard in `revert_compaction_batch(conn, batch_id)`**:
   - After loading `rows` for `batch_id` (`ORDER BY merge_id DESC`):
     - Preserve existing idempotency (`if all(r["is_reverted"] for r in rows): return`) and missing batch behavior (`if not rows: return`).
   - Collect all nodes touched by `batch_id`: `{from_node_id, to_node_id, *previous_children_json}` PLUS all node endpoints appearing in `previous_co_errors_snapshot_json` of every row in `batch_id`.
   - Query `compaction_audit_ledger` for any newer un-reverted merge (`is_reverted = 0 AND batch_id != ? AND merge_id > max_batch_merge_id`): if any such newer row has `{from_node_id, to_node_id, *previous_children_json}` intersecting `batch_id`'s touched nodes/snapshot endpoints (even across `(l1, l2)` via cross-scope edges!), raise `ValueError` clearly instructing to revert the newer dependent batch first.
2. **Delta-Preserving Co-Error Restoration in `_revert_single_merge`**:
   - Do NOT unconditionally delete all `concept_co_errors` rows touching `to_id`.
   - Instead, partition `previous_co_errors_snapshot_json` into:
     - `src_rows`: snapshot rows where `node_u_id in {from_id, *children}` or `node_v_id in {from_id, *children}`.
     - `to_rows`: snapshot rows only touching `to_id` (neither endpoint in `{from_id, *children}`), indexed by canonical `(node_u_id, node_v_id)`.
   - Group `src_rows` by their post-merge target key `_canonical_edge_key(map_to(u), map_to(v))` (where `from_id` and `*children` map to `to_id`, while outside nodes `z` map to `z`):
     - If mapped endpoints collide (`nu == nv`, i.e., internal edge between `from_id`/`*children` and `to_id` that was dropped at merge time): simply restore those `src_rows` into `concept_co_errors`.
     - For each external mapped key `(mu, mv)` (always involving `to_id` and `z`):
       - Compute the aggregate contribution from `src_rows` mapping to `(mu, mv)`: `contrib_active`, `contrib_hist`, `contrib_uids`.
       - Check current row at `(mu, mv)` in `concept_co_errors` (`pre_to = to_rows.get((mu, mv))`):
         - Subtract `contrib_hist` / `contrib_active` (`max(0, ...)`), remove `contrib_uids` EXCEPT keep any uids present in `pre_to`'s `source_question_uids_json` (in case both `from` and `to` shared a `question_uid` pre-merge!) and keep all post-merge uids not in `contrib_uids`.
         - Timestamp check: let `pre_merged_max_ts` be `max(r["last_error_at"] for r in src_rows_for_key + ([pre_to] if pre_to else []))`. If `pre_to is not None` and current `last_error_at == pre_merged_max_ts`, restore `last_error_at = pre_to["last_error_at"]` (exact field-by-field parity with pre-merge!); otherwise preserve the newer post-merge `last_error_at`.
         - If remaining `total_historical_count <= 0` AND `not remaining_uids`: delete row `(mu, mv)`.
         - Otherwise update row `(mu, mv)` with remaining `active_error_count`, `total_historical_count`, `source_question_uids_json`, and `last_error_at`.
     - Delete/replace any stale rows still referencing `from_id` or `*children` only, and re-insert all `src_rows` verbatim (`INSERT OR REPLACE`).
     - Any post-merge brand-new edge `(to_id, Y)` NOT referenced by `src_rows` or `to_rows` stays 100% untouched!
   - Also check `nodes` restoration & `is_reverted = 1` + `reverted_at` marking preserve all existing tests (`test_revert_exact_field_by_field_restoration`, `test_revert_restores_two_hop_child_pointers`, `test_revert_idempotent`, etc.).

### Part 3: Tests (`tests/test_ingest_service.py`, `tests/test_knowledge_graph_dsu.py`)
- In `tests/test_ingest_service.py`:
  - Add concurrency test proving another SQLite connection can perform writes while `node_judge` LLM call is awaiting in both `_ingest_new_question` and reimport flow (and also during compaction LLM call!).
  - Confirm `test_transaction_atomic_on_step_insert_failure` and `test_auto_compaction_triggered_at_threshold_during_ingest` continue to pass.
- In `tests/test_knowledge_graph_dsu.py`:
  - Add test: merge `B1` (`A -> B`), then add post-merge co-error edge `(B, Y)` and post-merge increment on existing `(B, X)` with new `q_post`, then `revert_compaction_batch(conn, B1)`: verify `(A, X)` is restored, `(B, X)` retains its pre-merge + post-merge counts and `q_post` without losing `(B, Y)`, and `(B, Y)` stays intact!
  - Add test: merge `B1` (`A -> B`), then merge `B2` (`B -> C` or `(B, C)` neighbor `C -> D`), verify `revert_compaction_batch(conn, B1)` before `B2` raises `ValueError`, and reverting `B2` first then `B1` succeeds cleanly!

## Constraints
- Do NOT edit files outside the whitelist.
- Do NOT run `git commit` or `git push`.
- Targeted verification after edits:
  `.venv/bin/python -m pytest tests/test_ingest_service.py tests/test_knowledge_graph_dsu.py -q && .venv/bin/ruff check backend/services/ingest_service.py backend/services/knowledge_graph.py tests/test_ingest_service.py tests/test_knowledge_graph_dsu.py && .venv/bin/mypy backend/services/ingest_service.py backend/services/knowledge_graph.py`
