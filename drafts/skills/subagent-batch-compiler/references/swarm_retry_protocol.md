# Phase 5: Aggregated 1-Round `swarm` Targeted Retry Protocol

Use this protocol only after deterministic Python import produces non-empty `retry_*.jsonl` items.

## 1. Aggregation & Sharding
- Merge `retry_*.jsonl` across completed batches/waves once a wave boundary completes (avoid spinning up workers for 1-2 isolated items when more batches are pending).
- Split failed tasks into shards of **`5–8` items per worker** (`shard_01.jsonl`, `shard_02.jsonl`, ...) so no retry worker overflows its context window.

## 2. Launching Pinned Retry Workers
Spawn a sharded `swarm` group pinned to the designated high-reasoning repair model (for example `gemini-4-neon-high`):

```text
swarm(
  action="add",
  name="retry",
  model="gemini-4-neon-high",
  workspace="inherit",
  tasks=["shard_01", "shard_02", ...],
  brief="""
  Fix ONLY the assigned retry shard JSONL (`<shard_file>`) and write repaired
  one-JSON-per-line results to `<retry_out_file>`.
  STRICT RULES:
  - Read ONLY `<shard_file>` (plus any explicitly listed CAS image paths in each item).
  - Do NOT read raw source dirs, md/ docs, src/, DBs, or task_map.json.
  - Address every error listed in `validation_errors` relative to `previous_output`.
  - Reply on the board when finished with item count and output path.
  """
)
```

## 3. Final Import & Teardown
1. Merge repaired shard outputs into `results_retry.jsonl`.
2. Run deterministic final import with `--final --worker-tag <model>` (marking remaining domain verification failures as `NEEDS_REVIEW` without blocking non-blocked downstream stages).
3. Terminate all retry workers immediately once landed:
   `swarm(action="kill", name="retry-1")`, `swarm(action="kill", name="retry-2")`, ...
