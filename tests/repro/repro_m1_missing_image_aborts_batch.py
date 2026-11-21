"""Repro: 单题 CAS 图片缺失/损坏 -> build_stage1_messages 在 _process_one 的 try 之外抛异常，
asyncio.gather 传播后整个 Stage 1 批次崩溃，且该题永久卡在 RUNNING（attempts 不计失败、不转 RUN_FAILED_EXHAUSTED）。

期望：单题异常只应记为该题 FAILED（attempts 计数），其余题照常 OK；run_stage1_compile 不抛出。
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
        assets = tmp / "assets" / "images"
        assets.mkdir(parents=True)
        db = tmp / "t.db"
        conn = c.make_db(db)
        good = "primary_olympiad:ch_test:a_good"
        bad = "primary_olympiad:ch_test:b_bad"
        c.seed_question(conn, uid=good, content_hash="hg")
        # b_bad 引用了一张不存在的 CAS 图
        c.seed_question(
            conn, uid=bad, content_hash="hb", cas_images=["0123456789abcdef.png"], image_captions=[""]
        )
        conn.close()

        err: BaseException | None = None
        with patch.object(LLMGateway, "call_structured", new=c.make_fake_gateway()):
            try:
                await run_stage1_compile(db_path=db, config_dir=c.CONFIG_DIR, assets_dir=assets)
            except BaseException as exc:  # noqa: BLE001
                err = exc

        conn = c.get_connection(db)
        rows = {
            r["question_uid"]: (r["status"], r["attempts"])
            for r in conn.execute("SELECT question_uid, status, attempts FROM compile_state WHERE stage='S1'")
        }
        print("exception =", repr(err))
        print("compile_state S1 =", rows)
        conn.close()
        assert err is None, f"BUG: 单题缺图导致整批 run_stage1_compile 抛出 {err!r}"
        assert rows[good][0] == "OK", f"BUG: 好题未被处理: {rows[good]}"
        assert rows[bad][0] == "FAILED", f"BUG: 坏题状态应为 FAILED，实为 {rows[bad]}"


asyncio.run(main())
print("PASS")
