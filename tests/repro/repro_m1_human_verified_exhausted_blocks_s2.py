"""Repro: --include-human-verified 重跑时 API 连续失败达上限，对 HUMAN_VERIFIED 题无条件写 S2=BLOCKED。

DESIGN §4.1：S2 仅在 eff_verification_status=NEEDS_REVIEW 时 BLOCKED；HUMAN_VERIFIED 题应保持 S2 可运行（STALE）。
stage1 成功路径会检查 eff 状态，失败耗尽路径(stage1_solve.py L698-711)却无条件 BLOCKED，并误计 needs_review。
需非沙盒运行（asyncio）。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _m1_common as c  # noqa: E402

from shuati.llm.gateway import LLMGateway  # noqa: E402
from shuati.pipeline.stage1_solve import run_stage1_compile  # noqa: E402
from shuati.services import facade  # noqa: E402


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "t.db"
        conn = c.make_db(db)
        uid = "primary_olympiad:ch_test:q_hv"
        c.seed_question(conn, uid=uid)
        facade.submit_manual_review(
            conn,
            uid,
            visual_facts="",
            final_answers=[c.num_part("12")],
            solution_markdown="人工定稿 12",
            human_dir=tmp / "human",
        )
        conn.close()

        fake = c.make_fake_gateway(fail_uids={uid})
        with patch.object(LLMGateway, "call_structured", new=fake):
            for _ in range(3):  # max_stage_attempts=3
                await run_stage1_compile(
                    db_path=db,
                    config_dir=c.CONFIG_DIR,
                    assets_dir=tmp / "assets",
                    include_human_verified=True,
                )

        conn = c.get_connection(db)
        eff = conn.execute(
            "SELECT eff_verification_status FROM questions WHERE question_uid=?", (uid,)
        ).fetchone()[0]
        s2 = conn.execute(
            "SELECT status FROM compile_state WHERE question_uid=? AND stage='S2'", (uid,)
        ).fetchone()[0]
        print("eff_verification_status =", eff, "| S2 =", s2)
        conn.close()
        assert eff == "HUMAN_VERIFIED"
        assert s2 != "BLOCKED", "BUG: HUMAN_VERIFIED 题因机器重跑失败被置 S2=BLOCKED"


asyncio.run(main())
print("PASS")
