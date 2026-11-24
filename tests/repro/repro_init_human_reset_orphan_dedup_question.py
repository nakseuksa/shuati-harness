"""Repro: `shuati human reset` leaves `BATCH_JSONL` questions orphaned in `raw_questions` when deduplicated against an `EXTERNAL_MISTAKE`.

In `src/shuati/services/compiler_tools.py::reset_human_workspace`:
When deleting `EXTERNAL_MISTAKE` questions, it executes:
  `UPDATE raw_questions SET duplicate_of_uid = NULL WHERE duplicate_of_uid IN (...)`
without promoting those orphaned `BATCH_JSONL` rows into `questions` + `compile_state` (unlike `stage0_ingest._promote_orphan`).
Even re-running `shuati ingest` afterwards sees `content_hash` unchanged and skips them (`skipped_unchanged`), permanently black-holing those questions from `questions`.
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
from shuati.core.text_norm import compute_content_hash, compute_plain_fingerprint, normalize_text  # noqa: E402
from shuati.db.connection import get_connection  # noqa: E402
from shuati.db.schema import init_db  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    runner = CliRunner()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg_dir = tmp / "config"
        shutil.copytree(REPO_ROOT / "config", cfg_dir)
        db_path = tmp / "data" / "shuati.db"
        human_dir = tmp / "human"
        arch_dir = tmp / "backups"
        bank_dir = tmp / "bank"
        (bank_dir / "ch01_arith").mkdir(parents=True)

        q_text = "计算 12 + 30 等于多少？"
        norm = normalize_text(q_text, num_images=0)
        c_hash = compute_content_hash(norm.normalized_text, [], [])
        p_fp = compute_plain_fingerprint(norm.normalized_text, 0)

        # 1. Existing DB has an EXTERNAL_MISTAKE with identical text/content_hash
        with closing(get_connection(db_path)) as conn:
            init_db(conn, config_dir=cfg_dir)
            conn.execute(
                """
                INSERT INTO raw_questions(
                    question_uid, grade_subject, source_type, source_collection,
                    source_file, source_id, raw_question, content_hash, plain_fingerprint
                ) VALUES (
                    'primary_olympiad:ext:e1', '小学奥数', 'EXTERNAL_MISTAKE',
                    'external_mistake', '', 'e1', ?, ?, ?
                )
                """,
                (q_text, c_hash, p_fp),
            )
            conn.execute(
                """
                INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format)
                VALUES ('primary_olympiad:ext:e1', '小学奥数', ?, '解答题')
                """,
                (norm.normalized_text,),
            )
            conn.commit()

        # 2. Ingest external BATCH_JSONL containing the same problem
        (bank_dir / "ch01_arith" / "problems.jsonl").write_text(
            json.dumps({"id": "a_1", "question": q_text}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        r_ing = runner.invoke(
            app,
            [
                "ingest",
                "--root",
                str(bank_dir),
                "--dedup-near",
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--json",
            ],
        )
        assert r_ing.exit_code == 0, r_ing.output

        # Confirm a_1 was recorded as duplicate_of_uid -> e1
        with closing(get_connection(db_path, readonly=True)) as conn:
            rq = conn.execute(
                "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid = 'primary_olympiad:ch01_arith:a_1'"
            ).fetchone()
            assert rq is not None and rq["duplicate_of_uid"] == "primary_olympiad:ext:e1"

        # 3. Run `shuati human reset` (wipes EXTERNAL_MISTAKE e1)
        r_reset = runner.invoke(
            app,
            [
                "human",
                "reset",
                "--learner-id",
                "alice",
                "--human-dir",
                str(human_dir),
                "--archive-dir",
                str(arch_dir),
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--json",
            ],
        )
        assert r_reset.exit_code == 0, r_reset.output

        # 4. Even after re-running `shuati ingest`, check if a_1 exists in `questions`
        r_ing2 = runner.invoke(
            app,
            [
                "ingest",
                "--root",
                str(bank_dir),
                "--dedup-near",
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--json",
            ],
        )
        assert r_ing2.exit_code == 0, r_ing2.output

        with closing(get_connection(db_path, readonly=True)) as conn:
            rq_after = conn.execute(
                "SELECT duplicate_of_uid, is_missing_from_source FROM raw_questions WHERE question_uid = 'primary_olympiad:ch01_arith:a_1'"
            ).fetchone()
            q_after = conn.execute(
                "SELECT question_uid FROM questions WHERE question_uid = 'primary_olympiad:ch01_arith:a_1'"
            ).fetchone()
            cs_cnt = conn.execute(
                "SELECT COUNT(*) FROM compile_state WHERE question_uid = 'primary_olympiad:ch01_arith:a_1'"
            ).fetchone()[0]

        if q_after is None or cs_cnt == 0:
            print(
                "FAIL: `primary_olympiad:ch01_arith:a_1` in `raw_questions` has "
                f"duplicate_of_uid={rq_after['duplicate_of_uid']!r}, is_missing={rq_after['is_missing_from_source']}, "
                f"but `questions` row is {q_after!r} and `compile_state` count is {cs_cnt} (permanently black-holed orphan!).",
                file=sys.stderr,
            )
            return 1

    print("PASS: Orphaned duplicate of EXTERNAL_MISTAKE was properly promoted on human reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
