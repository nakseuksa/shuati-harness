"""Repro: POST /api/edit 的 sympy_code 非字符串（如数组）未校验 -> 事务内 sqlite3 绑定异常 -> 500 INTERNAL_ERROR。

期望：非法输入类型返回 400（PRECONDITION_FAILED）。同时 final_answers 里放合法答案以排除其他拒绝原因。
需非沙盒运行（回环 TCP）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _m1_common as c  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    db = tmp / "t.db"
    conn = c.make_db(db)
    uid = "primary_olympiad:ch_test:q_type"
    c.seed_question(conn, uid=uid)
    conn.close()
    server = c.start_review_server(db, tmp / "assets", tmp / "human")
    port = server.server_address[1]
    try:
        payload = {
            "question_uid": uid,
            "visual_facts": "",
            "final_answers": [c.num_part("12").model_dump()],
            "solution_markdown": "x",
            "sympy_code": ["def verify(): pass"],
        }
        status, body = c.http_request(
            port,
            "POST",
            "/api/edit",
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        print("status =", status, "|", body[:200])
        assert status == 400, f"BUG: 非法 sympy_code 类型返回 {status}（应 400），响应 {body[:160]!r}"
    finally:
        server.shutdown()
        server.server_close()
print("PASS")
