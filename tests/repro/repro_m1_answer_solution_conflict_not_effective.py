"""Repro: ANSWER_SOLUTION_CONFLICT 仅进入 review_reason，生效状态仍为 HUMAN_VERIFIED + servable。

DESIGN §3.2-6：机器 branch_1 声称答案与 eff_final_answers 不等价时，须"将题目置 NEEDS_REVIEW + ANSWER_SOLUTION_CONFLICT，
杜绝新机器解法 + 旧人工答案混搭流入 Stage 2"。当前 effective.recompute 中 final_answers_json 覆写直接推出 HUMAN_VERIFIED，
使冲突题仍 servable，S2 不会 BLOCKED。
需非沙盒运行（materialize_solutions 内部 asyncio.run）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _m1_common as c  # noqa: E402

from shuati.core import effective  # noqa: E402
from shuati.pipeline.stage1_solve import materialize_solutions  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    tmp = Path(td)
    conn = c.make_db(tmp / "t.db")
    uid = "primary_olympiad:ch_test:q_conf"
    c.seed_question(conn, uid=uid, content_hash="hc")
    # 机器主路 run：答案 12
    conn.execute(
        """
        INSERT INTO stage1_vlm_runs(question_uid, role, model_name, prompt_version, input_hash,
            run_status, visual_facts, method_summary, solution_markdown, final_answers_json,
            sympy_code, sympy_status)
        VALUES (?, 'PRIMARY', 'm', 's1_v1', 'ih', 'OK', '', '法', '解 12', ?, 'x', 'PASSED')
        """,
        (uid, json.dumps([c.num_part("12").model_dump()], ensure_ascii=False)),
    )
    # 人工（PI_CLI）仅覆写答案为 99，未覆写 solutions_override_json
    conn.execute(
        """
        INSERT INTO human_overrides(question_uid, field_name, override_value, reason, source,
            base_fingerprint, is_active, is_suspended, suspended_reason)
        VALUES (?, 'final_answers_json', ?, 'r', 'PI_CLI', 'hc', 1, 0, '')
        """,
        (uid, json.dumps([c.num_part("99").model_dump()], ensure_ascii=False)),
    )
    conn.commit()
    effective.recompute(conn, uid)
    materialize_solutions(conn, uid)
    effective.recompute(conn, uid)
    conn.commit()
    row = conn.execute(
        "SELECT eff_verification_status, eff_review_reason, eff_is_servable FROM questions WHERE question_uid=?",
        (uid,),
    ).fetchone()
    print(dict(row))
    assert "ANSWER_SOLUTION_CONFLICT" in row["eff_review_reason"], "前置：冲突应被检出"
    assert row["eff_verification_status"] == "NEEDS_REVIEW" and row["eff_is_servable"] == 0, (
        "BUG: 检出 ANSWER_SOLUTION_CONFLICT 后生效状态仍为 "
        f"{row['eff_verification_status']} servable={row['eff_is_servable']}（应 NEEDS_REVIEW 且不可服务）"
    )
print("PASS")
