"""Repro: 6A 修复路径产生 duplicate_of_uid 自环并使题目整体从 questions 消失（M0.1 Round 1）。

场景（全部在 tempfile 临时目录内）：
 1. 第 1 轮 dedup_near=True：q1 = "求 1+1 的值"，q2 = "求 $1+1$ 的值"（近重，hash 不同，plain_fingerprint 相同）
    -> q2.duplicate_of_uid = q1。
 2. 第 2 轮：源端把 q1 的题干修正为与 q2 完全相同的 "求 $1+1$ 的值"。
    new_dup 命中 q2 (hash 相同)，target_dup_uid = q2.duplicate_of_uid = q1 (即 q1 自己)
    -> q1.duplicate_of_uid = q1（自环），q1 的 questions 行被 DELETE，q2 本就无 questions 行。
期望：整个题目簇至少保留一个 duplicate_of_uid IS NULL 且在 questions 中的主题，且无自环。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shuati.pipeline.stage0_ingest import run_stage0_ingest  # noqa: E402


def write(root: Path, rows: list[dict]) -> None:
    d = root / "chA"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "problems.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    cfg = tmp / "cfg"
    cfg.mkdir()
    (cfg / "collections.yaml").write_text(
        'rules:\n  - match: "ch*"\n    grade_subject: 小学奥数\n', encoding="utf-8"
    )
    root = tmp / "root"
    kw = dict(
        db_path=tmp / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp / "assets",
        reports_dir=tmp / "reports",
        lock_path=tmp / ".compile.lock",
        dedup_near=True,
    )
    write(root, [
        {"id": "q1", "question": "求 1+1 的值", "images": []},
        {"id": "q2", "question": "求 $1+1$ 的值", "images": []},
    ])
    run_stage0_ingest(**kw)
    c = sqlite3.connect(tmp / "s0.db")
    c.row_factory = sqlite3.Row
    rows1 = {r["question_uid"]: r["duplicate_of_uid"] for r in c.execute("SELECT question_uid, duplicate_of_uid FROM raw_questions")}
    c.close()
    print("after round1:", rows1)
    assert rows1["primary_olympiad:chA:q2"] == "primary_olympiad:chA:q1", "precondition: q2 must be near-dup kid of q1"

    write(root, [
        {"id": "q1", "question": "求 $1+1$ 的值", "images": []},
        {"id": "q2", "question": "求 $1+1$ 的值", "images": []},
    ])
    run_stage0_ingest(**kw)
    c = sqlite3.connect(tmp / "s0.db")
    c.row_factory = sqlite3.Row
    rows2 = {r["question_uid"]: r["duplicate_of_uid"] for r in c.execute("SELECT question_uid, duplicate_of_uid FROM raw_questions")}
    qs = [r[0] for r in c.execute("SELECT question_uid FROM questions")]
    c.close()
    print("after round2 raw dup map:", rows2)
    print("after round2 questions rows:", qs)
    problems = []
    for uid, dup in rows2.items():
        if dup == uid:
            problems.append(f"self-loop: {uid}")
    masters = [u for u, d in rows2.items() if d is None and u in qs]
    if not masters:
        problems.append("no servable master remains in questions for the cluster (question vanished)")
    if problems:
        print("REPRO FAILED (defect confirmed):", problems)
        sys.exit(1)
    print("OK")
