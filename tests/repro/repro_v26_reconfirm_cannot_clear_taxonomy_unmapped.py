"""Repro v26: reconfirm_stale_overrides 对 TAXONOMY_UNMAPPED 挂起覆写是"空转"——复制时沿用旧 taxonomy_version，
新行立刻被 effective.recompute 再次挂起为 TAXONOMY_UNMAPPED，人工 [K] 重签永远无法解挂（返回 reconfirmed_count=1 却无效）。
运行：.venv\\Scripts\\python.exe tests/repro/repro_v26_reconfirm_cannot_clear_taxonomy_unmapped.py
期望修复后 Exit 0。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v26_common import L1, make_conn, seed_q  # noqa: E402

from shuati.core import effective  # noqa: E402
from shuati.services import facade  # noqa: E402


def main() -> int:
    conn, d = make_conn("reconfirm")
    seed_q(conn, "q1")
    conn.execute(
        "INSERT INTO human_overrides(question_uid, field_name, override_value, reason, source, bundle_id, "
        "base_fingerprint, taxonomy_version, is_active, is_suspended, suspended_reason) "
        "VALUES ('q1', 'primary_l1_module', ?, 'r', 'REVIEW_UI', NULL, 'h_q1', '0.9', 1, 0, '');",
        (L1,),
    )
    conn.commit()
    effective.recompute(conn, "q1")
    conn.commit()
    r0 = conn.execute("SELECT is_suspended, suspended_reason FROM human_overrides WHERE is_active=1;").fetchone()
    assert (r0[0], r0[1]) == (1, "TAXONOMY_UNMAPPED"), tuple(r0)
    res = facade.reconfirm_stale_overrides(conn, "q1", human_dir=d / "human")
    print("reconfirm result:", {k: res[k] for k in ("reconfirmed_count", "eff_verification_status")})
    rows = conn.execute(
        "SELECT is_active, is_suspended, suspended_reason, taxonomy_version FROM human_overrides ORDER BY override_id;"
    ).fetchall()
    print("rows:", [tuple(r) for r in rows])
    active = [r for r in rows if r["is_active"] == 1]
    assert active and active[0]["is_suspended"] == 0, f"重签后仍被挂起: {tuple(active[0])}"
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
