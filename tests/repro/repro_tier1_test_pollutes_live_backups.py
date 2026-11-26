"""Repro for Tier 1 test hygiene bug: test_tier1_review_fixes pollutes CWD data/backups/.

tests/unit/test_tier1_review_fixes.py :: test_b03_restore_snapshot_atomic_rollback_on_recompute_error
calls human_mirror.restore_snapshot(conn, human_dir=human_dir) without passing
backups_dir=..., so it defaults to relative Path("data/backups") in the current
working directory. Because init_db() seeds the default learner, vacuum_backup()
executes against the live workspace data/backups/ directory, writes an untracked
dummy *_human_restore.db file, and prunes real backups once count > 10!

This repro executes the exact test function inside an isolated temporary CWD
seeded with 10 mock production backup files so it proves the defect deterministically
without touching the real workspace data/backups/.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.unit.test_tier1_review_fixes import (  # noqa: E402
    test_b03_restore_snapshot_atomic_rollback_on_recompute_error,
)


def main() -> None:
    orig_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fake_cwd = tmp / "workspace"
        fake_backups = fake_cwd / "data" / "backups"
        fake_backups.mkdir(parents=True, exist_ok=True)

        # Seed 10 existing "production" backups in CWD/data/backups
        seeded_names: set[str] = set()
        base_mtime = time.time() - 3600
        for idx in range(10):
            f = fake_backups / f"20261120_00000{idx}_prod_backup.db"
            f.write_bytes(b"prod-backup-placeholder")
            os.utime(f, (base_mtime + idx, base_mtime + idx))
            seeded_names.add(f.name)

        test_tmp_path = tmp / "pytest_tmp_path"
        test_tmp_path.mkdir(parents=True, exist_ok=True)

        os.chdir(fake_cwd)
        try:
            mp = pytest.MonkeyPatch()
            try:
                test_b03_restore_snapshot_atomic_rollback_on_recompute_error(
                    test_tmp_path, mp
                )
            finally:
                mp.undo()
        finally:
            os.chdir(orig_cwd)

        after_names = {p.name for p in fake_backups.glob("*.db")}
        added = sorted(after_names - seeded_names)
        deleted = sorted(seeded_names - after_names)

        assert not added and not deleted, (
            "BUG REPRODUCED: test_b03_restore_snapshot_atomic_rollback_on_recompute_error "
            f"polluted CWD data/backups/! added={added}, deleted_prod_backups={deleted}"
        )


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"ASSERTION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print("PASS: test_b03_restore_snapshot_atomic_rollback_on_recompute_error is isolated from CWD data/backups/.")
