"""Repro: `shuati compile --stage 1 --mode import` retains stale `retry_tasks.jsonl` after clean import and accumulates fixed item errors across runs.

In `src/shuati/pipeline/stage1_solve.py::_run_stage1_import` & `subagent_bridge.py`:
1. When consecutive `import` runs encounter item-level errors (e.g. `INELIGIBLE_POST_EXPORT` / `STALE_CONTENT_HASH`, where `bridge_errs == []`), `_run_stage1_import` reads `existing_lines` from previous runs' `retry_tasks.jsonl` and appends onto them instead of writing only the current run's unresolved errors.
2. When an earlier `import` failed (creating `retry_tasks.jsonl`) and a subsequent `import` succeeds with 0 errors (`manifest_status == 'IMPORTED'`), `retry_tasks.jsonl` is NOT unlinked, causing `data['retry_file']` to remain non-null (`.../retry_tasks.jsonl`) and leaving stale retry tasks on disk.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from shuati.cli.main import app  # noqa: E402
from shuati.db.connection import get_connection  # noqa: E402
from shuati.pipeline.stage0_ingest import run_stage0_ingest  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    runner = CliRunner()
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg_dir = tmp / "config"
        shutil.copytree(REPO_ROOT / "config", cfg_dir)
        bank = tmp / "bank"
        (bank / "ch01").mkdir(parents=True)
        (bank / "ch01" / "problems.jsonl").write_text(
            json.dumps({"id": "q1", "question": "计算 5 + 7 等于多少？"}, ensure_ascii=False) + "\n"
            + json.dumps({"id": "q2", "question": "计算 8 + 9 等于多少？"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        db_path = tmp / "data" / "shuati.db"
        s1_dir = tmp / "s1"
        run_stage0_ingest(db_path=db_path, root_dir=bank, config_dir=cfg_dir)

        r_exp = runner.invoke(
            app,
            [
                "compile",
                "--stage",
                "1",
                "--mode",
                "export",
                "--tasks-dir",
                str(s1_dir),
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--json",
            ],
        )
        assert r_exp.exit_code == 0, r_exp.output
        tmap = json.loads((s1_dir / "stage1_task_map.json").read_text(encoding="utf-8"))
        tids = sorted(tmap["items"].keys())
        assert len(tids) == 2

        def _make_item(tid: str, ans: str, expr: str) -> dict[str, object]:
            return {
                "visual_facts": "",
                "visual_ambiguity_flag": False,
                "visual_ambiguity_note": "",
                "method_summary": "加法",
                "solution_markdown": f"结果为 {ans}。",
                "final_answers": [
                    {
                        "label": "主问",
                        "answer_type": "NUMERIC",
                        "verify_mode": "SYMBOLIC",
                        "collection_mode": "SINGLE",
                        "answer": ans,
                        "sympy_expr": expr,
                    }
                ],
                "sympy_code": f"def verify():\n    return {{'主问': str({expr})}}\n",
                "task_item_id": tid,
            }

        results_file = s1_dir / "results.jsonl"
        results_file.write_text(
            json.dumps(
                {
                    "batch_id": "stage1_batch_0001",
                    "result": {
                        "items": [
                            _make_item(tids[0], "12", "5 + 7"),
                            _make_item(tids[1], "17", "8 + 9"),
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        # Temporarily retire both q1 and q2 to trigger 2 item_errors (INELIGIBLE_POST_EXPORT) with bridge_errs == []
        with closing(get_connection(db_path)) as conn:
            conn.execute("UPDATE questions SET is_retired = 1;")
            conn.commit()

        r1 = runner.invoke(
            app,
            [
                "compile",
                "--stage",
                "1",
                "--mode",
                "import",
                "--tasks-dir",
                str(s1_dir),
                "--results",
                str(results_file),
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--json",
            ],
        )
        assert r1.exit_code == 0, r1.output

        # Unretire q1 so only q2 fails in Run 2
        with closing(get_connection(db_path)) as conn:
            conn.execute("UPDATE questions SET is_retired = 0 WHERE question_uid = 'primary_olympiad:ch01:q1';")
            conn.commit()

        r2 = runner.invoke(
            app,
            [
                "compile",
                "--stage",
                "1",
                "--mode",
                "import",
                "--tasks-dir",
                str(s1_dir),
                "--results",
                str(results_file),
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--json",
            ],
        )
        assert r2.exit_code == 0, r2.output
        retry_lines_r2 = [
            ln for ln in (s1_dir / "retry_tasks.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        if len(retry_lines_r2) != 1:
            failures.append(
                f"FAIL 1: After resolving q1 in Run 2 (only q2 still failing), `retry_tasks.jsonl` should "
                f"contain 1 error line, but accumulated {len(retry_lines_r2)} lines (retaining stale Run 1 errors)!"
            )

        # Unretire q2 as well -> Run 3 is 100% clean (`errors == []`, `manifest_status == 'IMPORTED'`)
        with closing(get_connection(db_path)) as conn:
            conn.execute("UPDATE questions SET is_retired = 0;")
            conn.commit()

        r3 = runner.invoke(
            app,
            [
                "compile",
                "--stage",
                "1",
                "--mode",
                "import",
                "--tasks-dir",
                str(s1_dir),
                "--results",
                str(results_file),
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--json",
            ],
        )
        assert r3.exit_code == 0, r3.output
        d3 = json.loads(r3.stdout)["data"]
        assert d3["manifest_status"] == "IMPORTED", d3
        assert d3["errors"] == [], d3

        if d3["retry_file"] is not None or (s1_dir / "retry_tasks.jsonl").exists():
            failures.append(
                f"FAIL 2: Clean Stage 1 import (`manifest_status='IMPORTED'`, `errors=[]`) left stale "
                f"`retry_tasks.jsonl` on disk and returned `retry_file={d3['retry_file']!r}`!"
            )

    if failures:
        for f in failures:
            print(f, file=sys.stderr)
        return 1

    print("PASS: Stage 1 import refreshes and clears retry_tasks.jsonl cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
