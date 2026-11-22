---
name: subagent-batch-compiler
description: >-
  Orchestrates high-volume, contamination-free batch compilation using isolated
  run_workflow subagents, pre-tightened inline JSON schemas, incremental disk
  flushing, deterministic single-writer Python validation, and 1-round sharded
  swarm error recovery. Use when processing hundreds or thousands of independent
  records (math/science problems, documents, multimodal assets) with strict
  anti-leakage isolation and database integrity guarantees. Do not use for
  single-item tasks or interactive multi-turn editing.
---

# Subagent Batch Compiler

A domain-agnostic architecture for compiling large batches (`100` to `5,000+` items) reliably while keeping LLM inference isolated from source metadata, local databases, and answer keys.

**Core Principle**: Subagents only transform sanitized single-item payloads (plus explicit media paths) into structured JSON. A single deterministic Python pipeline validates, verifies, locks, and commits to storage.

---

## Workflow

### Phase 1: Sanitized Export & Isolation Boundary
1. **Separate Identity Mapping**: Export sanitized task payloads (`task_id`, clean content, media paths/captions, mode) to `batches/batch_*.jsonl` and store sensitive mappings (`uid`, `source_id`, `content_hash`, ground-truth/legacy tags) strictly in a separate `task_map.json`.
2. **Enforce Hard Read Boundaries**: In every subagent prompt, explicitly forbid reading raw source folders, solution/doc folders (`md/`), database files, `src/`, or `task_map.json`.
3. **Content-Hash Locking**: Before exporting, skip records already stamped as locked with an unchanged `content_hash` unless `--force` is requested.

### Phase 2: Schema Inlining & Pre-Flight Tightening
1. Before passing a JSON Schema to `run_workflow`, inline all `$defs`/`$ref` definitions using [schema_inline.py](./scripts/schema_inline.py) so structured validators do not stumble on nested references.
2. Tighten business bounds directly in the exported schema (for example, narrowing a generic `1..4` integer field to `maximum: 3` where domain rules require it) rather than relying solely on post-hoc rejection.

### Phase 3: Configurable Smoke Gate & Wave Execution
1. **Run a Configurable Smoke Gate First**: Before scaling to the full corpus after any prompt, rubric, or schema change, run a small smoke batch whose **size (`smoke_count`) and selection strategy (`smoke_strategy`: stratified, targeted failures, or fixed UIDs)** are chosen for the iteration. Verify pass rate and schema compliance before launching full waves.
2. **Manage Main-Session Context via Waves**: Split large corpora into waves of `250–500` items per `run_workflow` call so the coordinator conversation wakes only a few times without blowing context limits.
3. **Sliding-Window Concurrency & Real-Time Flush**:
   - Use [workflow_template.py](./scripts/workflow_template.py) (`asyncio.Semaphore` inside `parallel`, or bounded chunks) so slow items do not stall fast items.
   - Wrap every item call in `try/except` so one failure never cancels sibling tasks.
   - Read existing `task_id`s from `out_file` on startup to resume seamlessly, and `append + flush()` each completed item immediately to disk.
4. **Never Re-Export Over Unimported Results**: Never re-run `export` (which renumbers `task_id`s) until any previously flushed `results_*.jsonl` files have been committed via `import`.

### Phase 4: Deterministic Single-Writer Verification & Import
1. Feed wave outputs into a single deterministic Python importer holding a write lock (on Windows, ensure required lock/DB write permissions outside restricted sandboxes).
2. Run programmatic checks **before** committing:
   - Schema validation & contiguous indices;
   - Domain verification (e.g., sandboxed SymPy/code execution, AST anti-tautology checks forbidding hardcoded literal returns, regex/blacklist checks);
   - Non-blocking quarantine: route ambiguous or post-retry unverified items to a `NEEDS_REVIEW` queue (`servable=0`) without discarding valid secondary outputs.
3. Stamp verified rows with a versioned/locked model marker + `input_hash` so restarts skip completed work.

### Phase 5: Aggregated 1-Round `swarm` Targeted Recovery
When Phase 4 emits `retry_*.jsonl` items, follow [swarm_retry_protocol.md](./references/swarm_retry_protocol.md):
1. Aggregate retries across a wave or milestone instead of spawning single-item workers.
2. Shard failures (`5–8` items per shard) to pinned high-reasoning `swarm` workers via `tasks=[...]`, passing only sanitized item payloads, `previous_output`, and deterministic `validation_errors`.
3. Run `--final` single-writer import, then `swarm(action="kill")` all retry workers.

### Phase 6: Read-Only Verification & Handoff
1. Inspect final counts via read-only DB queries (e.g., SQLite `?immutable=1` URI mode).
2. Log failure patterns to feed the next prompt/rubric refinement loop.

---

## Verification Checklist
- [ ] Zero forbidden paths accessed by subagents; `task_map.json` never exposed to workers.
- [ ] Smoke gate passed (`smoke_count` & `smoke_strategy` confirmed for current changes).
- [ ] All waves flushed and imported before any subsequent `export`.
- [ ] All Phase 5 `retry` workers killed after `--final` import.
- [ ] Storage state & pass/review/fail counts verified directly from DB output.
