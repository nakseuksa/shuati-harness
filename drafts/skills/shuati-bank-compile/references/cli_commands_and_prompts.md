# Shuati Bank Compile — CLI & Environment Reference

All commands assume workspace root `D:\0.default\shuati-harness` and Windows virtual env `.venv\Scripts\python.exe`.

## 1. Windows Sandbox vs Bypass Rules
- **Run inside sandbox**: `ruff check --no-cache`, `mypy --cache-dir "$env:TEMP\shuati_mypy_cache" src`, and read-only SQLite inspection using URI `file:data/shuati.db?immutable=1`.
- **Requires `BypassSandbox: true`**: Anything touching `data/.compile.lock` (`msvcrt.locking`), writing `data/shuati.db` / `data/unified/` / `data/backups/`, or running `pytest-asyncio` tests (`ProactorEventLoop` socketpair).
- **PowerShell tip**: Avoid nested-quote multiline strings inside `python -c "..."`; write short scratch `.py` scripts with `sys.stdout.reconfigure(encoding="utf-8")`.

## 2. Standard Command Sequence

```powershell
# A. Online SQLite cold backup (safe even if DB is open)
.venv\Scripts\python.exe -c "import pathlib, sqlite3; pathlib.Path('data/backups').mkdir(parents=True, exist_ok=True); s=sqlite3.connect('file:data/shuati.db?mode=ro', uri=True); d=sqlite3.connect('data/backups/shuati_backup.db'); s.backup(d); d.close(); s.close()"

# B. Stage 0 ingest & near-dedup (ensure collections.yaml covers all prefixes)
.venv\Scripts\python.exe -m shuati.cli.main ingest --dedup-near --json

# C. Stage 1.5 anchor calibration (uses config/anchors/anchors_<slug>.yaml)
.venv\Scripts\python.exe -m shuati.cli.main unified calibrate-anchors --json

# D. Export sanitized tasks (batch-size 50 for wave execution)
.venv\Scripts\python.exe -m shuati.cli.main unified export --batch-size 50 --json

# E. Smoke-only export example (subset UIDs or limit)
.venv\Scripts\python.exe -m shuati.cli.main unified export --only-uids <uid1> --only-uids <uid2> --batch-size 50 --json

# F. Round 1 deterministic validation & SQLite import
.venv\Scripts\python.exe -m shuati.cli.main unified import --results data/unified/results_wave_01.jsonl --task-map data/unified/task_map.json --retry-out data/unified/retry_wave_01.jsonl --json

# G. Round 2 final import after gemini-4-neon-high swarm repair
.venv\Scripts\python.exe -m shuati.cli.main unified import --results data/unified/results_retry_final.jsonl --task-map data/unified/task_map.json --final --worker-tag gemini-4-neon-high --json

# H. Interactive review & full-text index refresh
.venv\Scripts\python.exe -m shuati.cli.main review
.venv\Scripts\python.exe -m shuati.cli.main reindex
```

## 3. Read-Only DB State Audit Query
Inspect `compile_state` and `questions` counts inside sandbox:
```python
import sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8")
c = sqlite3.connect("file:data/shuati.db?immutable=1", uri=True)
print(list(c.execute("SELECT stage, status, model_name, COUNT(*) FROM compile_state GROUP BY stage, status, model_name")))
print(list(c.execute("SELECT eff_verification_status, eff_is_servable, COUNT(*) FROM questions GROUP BY eff_verification_status, eff_is_servable")))
```
