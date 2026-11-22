---
name: shuati-bank-compile
description: >-
  Project-specific runbook for running fresh-DB full re-compiles or incremental
  data compilation iterations in shuati-harness v2. Covers collections mapping
  checks, non-md ingestion, dynamic anchor calibration, configurable smoke
  gates, unified export/import locking, NEEDS_REVIEW non-blocking S2
  convergence, and Windows sandbox rules. Use when starting a new
  prompt/algorithm re-compile, adding new question chapters, or rebuilding
  shuati.db from zero. Do not use for generic non-shuati tasks.
---

# Shuati Bank Compile

Standard operating procedure for running full-bank or incremental knowledge-compilation iterations in `shuati-harness` v2.

## Hard Project Invariants (Never Violate)
1. **Read-Only Source & `md/` Exclusion**: Never modify `backup/v1/` or `题库/`. When scanning `题库/`, read ONLY `problems.jsonl` and referenced `images/`; **NEVER read any file under `题库/*/md/`**.
2. **Subagent Isolation**: Compiled workers (`run_workflow` / `swarm`) must never read `题库/`, `docs/`, `src/`, `human/`, `data/shuati.db`, or `data/unified/task_map.json`.
3. **No Auto Git Commits**: Never run `git commit` or `git push` unless the user explicitly instructs.
4. **Sequential Export/Import Safety**: Never run `shuati unified export` (which renumbers `T0001..`) until all existing flushed `results_*.jsonl` files have been committed via `shuati unified import`.

---

## End-to-End Iteration Workflow

### Step 1: Pre-Flight & Cold Backup
1. Inspect `题库/` chapter folders and verify EVERY directory prefix/path matches `config/collections.yaml` (e.g., `ch*`, `xsc_ch*` -> `小学奥数`). Add missing rules before running Stage 0.
2. Check which static bad/excluded UIDs (such as `ch28_025`, `ch28_031`, `ch28_091`) should remain excluded in this iteration, and inspect the active anchor file `config/anchors/anchors_<slug>.yaml`.
3. Create an online SQLite cold backup to `data/backups/` before modifying or rebuilding `data/shuati.db` (see [cli_commands_and_prompts.md](./references/cli_commands_and_prompts.md)).

### Step 2: Fresh DB Reset (If Full Re-Compile) & Stage 0 Ingest
1. If rebuilding from zero, back up and remove/rotate `data/shuati.db` (confirm with the user whether `human/overrides.jsonl` or static excluded UIDs should be preserved/re-applied).
2. Run Stage 0 with near-dedup enabled (and **DO NOT** pass `--apply-retire` if old chapters remain on disk):
   `.venv\Scripts\python.exe -m shuati.cli.main ingest --dedup-near --json`
3. Verify `unmapped_collections == []` and `rejected_questions == []` in the generated `data/reports/ingest_*.json`.

### Step 3: Dynamic Anchor Bootstrap & Calibration (Stage 1.5)
- Anchor count and exact UIDs are **parameterized** by `config/anchors/anchors_<slug>.yaml` (code minimum is `>= 4` anchors; never hardcode a fixed count like `5`).
- In a brand-new empty DB, every anchor UID in `anchors_<slug>.yaml` must first reach `VERIFIED_CONSENSUS` or `HUMAN_VERIFIED` (Stage 1 bootstrap) before running:
  `.venv\Scripts\python.exe -m shuati.cli.main unified calibrate-anchors --json`

### Step 4: Sanitized Export & Configurable Smoke Gate
1. Export sanitized batches (recommended `--batch-size 50` for wave runs):
   `.venv\Scripts\python.exe -m shuati.cli.main unified export --batch-size 50 --json`
2. **Dynamic Smoke Gate (Before Full Scale)**: Do NOT hardcode smoke size or selection rules. Confirm or choose per iteration:
   - `smoke_count`: number of items needed to verify the target change;
   - `smoke_strategy`: `stratified` (by chapter/format/`has_images`), `targeted` (prioritizing changed formats or prior `NEEDS_REVIEW`/failed UIDs), or `fixed_uids` (strict A/B baseline comparison).
3. Run the smoke slice -> `unified import` -> inspect SymPy pass rate, zero-spoiler hints, `raw_l3` digit check, and anchor-consistent `eff_base_elo` before scaling.

### Step 5: Full Wave Compilation, Import & Swarm Recovery
1. Execute wave compilation (sliding concurrency + per-item/chunk flush, isolating per-item errors).
2. Commit each completed wave with `shuati unified import` (requires `BypassSandbox: true` on Windows due to `data/.compile.lock`).
3. Note non-blocking policy: `NEEDS_REVIEW` (visual ambiguity or post-retry SymPy failure) still writes Stage 2 (`S2='OK'`, `eff_is_servable=0`).
4. Aggregate wave `retry_*.jsonl` files, shard failures (`5–8` items/worker) to `swarm` workers pinned to `gemini-4-neon-high` for 1 targeted repair round, import with `--final --worker-tag gemini-4-neon-high`, then kill all retry workers.

### Step 6: Review Convergence, Verification & Records
1. Verify final DB distribution via read-only `sqlite3('file:data/shuati.db?immutable=1', uri=True)`.
2. Hand off `NEEDS_REVIEW` tail items to the user for interactive resolution via `.venv\Scripts\python.exe -m shuati.cli.main review`, then run `reindex` if needed.
3. Update `docs/HANDOFF.md` and write the `docs/records/` triplet (`decisions`, `execution`, `report`) + `docs/records/INDEX.md` per `AGENTS.md` §5.

---

## Verification Checklist
- [ ] All `题库/` directories matched `config/collections.yaml`; zero `md/` files read.
- [ ] Cold backup created under `data/backups/` before DB reset/ingest.
- [ ] `anchors_<slug>.yaml` (`>= 4` anchors) calibrated cleanly before full `unified export`.
- [ ] Smoke gate executed with iteration-appropriate `smoke_count` and `smoke_strategy`.
- [ ] Every wave imported before any re-export; all retry workers killed after `--final`.
- [ ] Final DB counts verified read-only; `docs/records/` updated on milestone close.
