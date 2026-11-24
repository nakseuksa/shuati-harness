"""Repro: 单行畸形输入即可使 stdio MCP 服务端进程崩溃（拒绝服务）。

预期（JSON-RPC 2.0）：畸形请求应返回 -32700/-32600 错误响应，服务端继续处理后续请求。
实际：深层嵌套 JSON 触发 RecursionError（非 ValueError，未被 `except ValueError` 捕获），
      serve_stdio 主循环无 try/except，进程以非零码退出；后续合法 ping 无响应。

运行：.venv\\Scripts\\python.exe tests/repro/repro_seam_mcp_malformed_input_crashes_server.py
期望修复后 Exit Code == 0。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_server(stdin_bytes: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "shuati.mcp.server", "--role", "worker"],
        input=stdin_bytes,
        capture_output=True,
        cwd=str(ROOT),
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        timeout=60,
        check=False,
    )


def main() -> int:
    failures: list[str] = []
    ping = (json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n").encode("utf-8")

    # Case 1: 深层嵌套数组
    deep = ("[" * 100000 + "]" * 100000 + "\n").encode("utf-8")
    p1 = _run_server(deep + ping)
    out1 = p1.stdout.decode("utf-8", "replace")
    if p1.returncode != 0 or '"id": 7' not in out1:
        failures.append(
            f"[deep-nesting] returncode={p1.returncode}, ping_answered={'\"id\": 7' in out1}, "
            f"stderr_tail={p1.stderr.decode('utf-8', 'replace')[-200:]!r}"
        )

    # Case 2: 非法 UTF-8 字节
    bad = b"\xff\xfe\x80 not-utf8\n"
    p2 = _run_server(bad + ping)
    out2 = p2.stdout.decode("utf-8", "replace")
    if p2.returncode != 0 or '"id": 7' not in out2:
        failures.append(
            f"[invalid-utf8] returncode={p2.returncode}, ping_answered={'\"id\": 7' in out2}, "
            f"stderr_tail={p2.stderr.decode('utf-8', 'replace')[-200:]!r}"
        )

    if failures:
        print("REPRO FAILED (bug reproduced):")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: server survived malformed input")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
