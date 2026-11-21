"""Repro: 双盲路径答案归一化失败(ANSWER_PART_NORMALIZE_ERROR)耗尽转 NEEDS_REVIEW 时，S2 未置 BLOCKED。

对照：API 失败耗尽分支(stage1_solve.py L698-711)会 upsert S2=BLOCKED，归一化失败耗尽分支(L745-748)漏掉。
DESIGN §4.1：eff NEEDS_REVIEW 的题 S2 必须 BLOCKED。
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


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "t.db"
        conn = c.make_db(db)
        uid = "primary_olympiad:ch_test:q_norm"
        c.seed_question(conn, uid=uid)
        conn.close()
        # 辅路返回重复小问标签 -> normalize_answer_parts 抛 ValueError（pydantic 层不拦截）
        fake = c.make_fake_gateway(secondary_label="DUP")
        with patch.object(LLMGateway, "call_structured", new=fake):
            for _ in range(3):
                await run_stage1_compile(db_path=db, config_dir=c.CONFIG_DIR, assets_dir=tmp / "assets")
        conn = c.get_connection(db)
        eff = conn.execute(
            "SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid=?", (uid,)
        ).fetchone()
        s2 = conn.execute(
            "SELECT status FROM compile_state WHERE question_uid=? AND stage='S2'", (uid,)
        ).fetchone()[0]
        print(dict(eff), "| S2 =", s2)
        conn.close()
        assert eff["eff_verification_status"] == "NEEDS_REVIEW", "前置：应已转人工"
        assert s2 == "BLOCKED", f"BUG: eff=NEEDS_REVIEW 但 S2={s2}（应 BLOCKED）"


asyncio.run(main())
print("PASS")
