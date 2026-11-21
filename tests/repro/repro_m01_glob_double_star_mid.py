"""Repro: _posix_glob_match 对中间 `/**/` 的处理错误（M0.1 Round 1）。

标准 POSIX/globstar 语义：`a/**/b` 匹配 `a/b`（零层）、`a/x/b`、`a/x/y/b`，
且绝不匹配 `ab` 或 `a/xb`。当前实现把 `/**/` 编译为 `(?:/.+)?`（缺少尾部 `/`），
导致零层情形漏匹配、并错误匹配无分隔符的拼接路径。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shuati.pipeline.stage0_ingest import _posix_glob_match  # noqa: E402

PAT = "初中物理/**/ch*"
cases = [
    ("初中物理/ch01", True),  # 零层
    ("初中物理/八上/ch01", True),
    ("初中物理/八上/声现象/ch01", True),
    ("初中物理ch01", False),  # 无分隔符拼接，绝不应匹配
    ("初中物理/八上ch01", False),
]
bad = []
for path, expect in cases:
    got = _posix_glob_match(path, PAT)
    print(f"{PAT!r} vs {path!r}: got={got} expect={expect}")
    if got != expect:
        bad.append((path, expect, got))
if bad:
    print(f"REPRO FAILED (defect confirmed): {bad}")
    sys.exit(1)
print("OK")
