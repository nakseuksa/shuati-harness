"""Repro v26: 同 bundle 内词表类覆写 TAXONOMY_UNMAPPED 挂起会连坐整组验真类覆写（违反契约 §1.3 / 攻防点 4：不影响验真类）。

契约 §1.3：bundle 传播仅在验真字段 content_hash 失配（或已挂起）时触发；词表版本漂移只应把词表类覆写挂起为
TAXONOMY_UNMAPPED，"不影响验真类"。实际 effective.recompute 把 TAXONOMY_UNMAPPED 也写入 bundle_reasons，
整组（含 final_answers_json）被挂起 => 题目被降为 NEEDS_REVIEW(OVERRIDE_STALE)。
运行：.venv\\Scripts\\python.exe tests/repro/repro_v26_taxonomy_stale_poisons_verification_bundle.py
期望修复后 Exit 0。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v26_common import L1, make_conn, seed_q  # noqa: E402

from shuati.core import effective  # noqa: E402


def main() -> int:
    conn, _ = make_conn("bundle")
    seed_q(conn, "q1")
    ans = json.dumps([{"verify_mode": "EXACT", "value": "1"}])
    for fname, val, tv in (
        ("final_answers_json", ans, ""),
        ("primary_l1_module", L1, "0.9"),  # 词表版本 0.9 != 当前 1.0
    ):
        conn.execute(
            "INSERT INTO human_overrides(question_uid, field_name, override_value, reason, source, bundle_id, "
            "base_fingerprint, taxonomy_version, is_active, is_suspended, suspended_reason) "
            "VALUES ('q1', ?, ?, 'r', 'REVIEW_UI', 'bnd_mixed', 'h_q1', ?, 1, 0, '');",
            (fname, val, tv),
        )
    conn.commit()
    effective.recompute(conn, "q1")
    conn.commit()
    rows = {
        r["field_name"]: (r["is_suspended"], r["suspended_reason"])
        for r in conn.execute("SELECT field_name,is_suspended,suspended_reason FROM human_overrides;")
    }
    st = conn.execute("SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid='q1';").fetchone()
    print("overrides:", rows)
    print("eff status:", st["eff_verification_status"], "|", st["eff_review_reason"])
    assert rows["primary_l1_module"][1] == "TAXONOMY_UNMAPPED", rows
    assert rows["final_answers_json"][0] == 0, f"验真类 final_answers_json 被词表漂移连坐挂起: {rows['final_answers_json']}"
    assert st["eff_verification_status"] == "HUMAN_VERIFIED", st["eff_verification_status"]
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
