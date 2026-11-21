"""Repro: submit_manual_review / POST /api/edit 接受空 final_answers 并直接定稿为 HUMAN_VERIFIED + servable。

期望：无任何答案小问的人工定稿应被拒绝（PreconditionError），否则无答案题以 HUMAN_VERIFIED 流入 Stage 2/组卷池。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _m1_common as c  # noqa: E402

from shuati.db.connection import PreconditionError  # noqa: E402
from shuati.services import facade  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    tmp = Path(td)
    conn = c.make_db(tmp / "t.db")
    uid = "primary_olympiad:ch_test:q_empty"
    c.seed_question(conn, uid=uid)
    try:
        facade.submit_manual_review(
            conn,
            uid,
            visual_facts="",
            final_answers=[],
            solution_markdown="",
            human_dir=tmp / "human",
        )
        rejected = False
    except PreconditionError:
        rejected = True
    row = conn.execute(
        "SELECT eff_verification_status, eff_final_answers_json, eff_is_servable "
        "FROM questions WHERE question_uid = ?",
        (uid,),
    ).fetchone()
    print("rejected =", rejected, "| row =", dict(row))
    assert rejected, (
        "BUG: 空 final_answers 被接受，题目被定稿: "
        f"eff_status={row['eff_verification_status']} answers={row['eff_final_answers_json']} "
        f"servable={row['eff_is_servable']}"
    )
print("PASS")
