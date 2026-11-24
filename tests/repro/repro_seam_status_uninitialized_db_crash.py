"""Repro: `shuati status` / get_pipeline_status 对“已存在但未初始化”的 SQLite 库（0 字节 / 空 schema）崩溃。

契约 §1.5 与任务要求“对空库的处理”：巡检应只读、不崩溃、给出 db_exists=True 与下一步建议
（如先 init/ingest）。实际：OperationalError: no such table: grade_subjects，
CLI 返回 INTERNAL_ERROR 信封（exit 1）。

运行：.venv\\Scripts\\python.exe tests/repro/repro_seam_status_uninitialized_db_crash.py
期望修复后 Exit Code == 0。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from shuati.services.compiler_tools import get_pipeline_status  # noqa: E402


def main() -> int:
    failures: list[str] = []
    for label, make in (
        ("zero-byte db", lambda p: p.write_bytes(b"")),
        ("empty sqlite (no tables)", lambda p: sqlite3.connect(p).close()),
    ):
        d = Path(tempfile.mkdtemp(prefix="repro_status_"))
        db = d / "shuati.db"
        make(db)
        try:
            res = get_pipeline_status(
                db_path=db, config_dir=ROOT / "config", vectors_dir=d / "v", unified_dir=d / "u"
            )
            assert res.get("db_exists") is True
            print(f"[OK ] {label}: recommended={res.get('recommended_next_commands')}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"[BUG] {label}: {type(exc).__name__}: {exc}")

        # CLI 层
        proc = subprocess.run(
            [sys.executable, "-m", "shuati.cli.main", "status", "--db", str(db), "--json",
             "--config-dir", str(ROOT / "config"), "--vectors-dir", str(d / "v"), "--unified-dir", str(d / "u")],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), check=False,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        )
        try:
            env = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            env = {}
        if proc.returncode != 0 or env.get("ok") is not True:
            failures.append(f"CLI {label}: exit={proc.returncode} error={env.get('error')}")
            print(f"[BUG] CLI {label}: exit={proc.returncode} error={env.get('error')}")

    if failures:
        print("REPRO FAILED:", len(failures), "failure(s)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
