"""Repro: 复核页 POST 接口无 Origin/Host/Content-Type 校验 -> 任意网页可跨站写（CSRF / DNS rebinding）。

127.0.0.1 绑定只防外部主机直连，不防浏览器内恶意网页向 http://127.0.0.1:8765/api/* 发
"simple request"（Content-Type: text/plain + JSON 体，无需预检）。期望：带跨站 Origin 或非 JSON Content-Type 的
写请求被拒（403/415），且不产生覆写。需非沙盒运行（回环 TCP）。
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
    uid = "primary_olympiad:ch_test:q_csrf"
    c.seed_question(conn, uid=uid)
    conn.close()
    server = c.start_review_server(db, tmp / "assets", tmp / "human")
    port = server.server_address[1]
    try:
        status, body = c.http_request(
            port,
            "POST",
            "/api/exclude",
            body=json.dumps({"question_uid": uid}).encode(),
            headers={
                "Content-Type": "text/plain",
                "Origin": "http://evil.example",
                "Host": "evil.example:8765",
            },
        )
        conn = c.get_connection(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM human_overrides WHERE question_uid=? AND field_name='is_excluded' AND is_active=1",
            (uid,),
        ).fetchone()[0]
        conn.close()
        print("status =", status, "| active is_excluded overrides =", n, "|", body[:120])
        assert status in (403, 415) and n == 0, (
            f"BUG: 跨站 Origin=evil.example + text/plain 的 POST 被接受 (status={status}, 覆写数={n})"
        )
    finally:
        server.shutdown()
        server.server_close()
print("PASS")
