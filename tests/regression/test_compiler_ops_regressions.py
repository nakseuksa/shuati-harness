"""Wave 1B 红军 Repro 回归测试（未初始化 SQLite 库巡检 & preflight 空题幻影题与只读守卫）。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from shuati.db.connection import PreconditionError
from shuati.services.compiler_tools import get_pipeline_status
from shuati.services.diagnose_batch import (
    parse_question_list_markdown,
    preflight_question_list_markdown,
)


def test_status_handles_uninitialized_db_without_crash(tmp_path: Path) -> None:
    """Repro #5：0 字节或空 schema SQLite 库执行巡检与 CLI status 不崩溃。"""
    repo_root = Path.cwd()
    cfg_dir = repo_root / "config"

    for label, make_db in (
        ("zero_byte", lambda p: p.write_bytes(b"")),
        ("empty_schema", lambda p: sqlite3.connect(p).close()),
    ):
        case_dir = tmp_path / label
        case_dir.mkdir(parents=True, exist_ok=True)
        db_path = case_dir / "shuati.db"
        make_db(db_path)

        res = get_pipeline_status(
            db_path=db_path,
            config_dir=cfg_dir,
            vectors_dir=case_dir / "vectors",
            unified_dir=case_dir / "unified",
        )
        assert res["db_exists"] is True
        assert res["db_initialized"] is False
        assert "shuati ingest --json" in res["recommended_next_commands"]

        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "shuati.cli.main",
                "status",
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--vectors-dir",
                str(case_dir / "vectors"),
                "--unified-dir",
                str(case_dir / "unified"),
                "--json",
            ],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert proc.returncode == 0, f"{label} CLI failed: stderr={proc.stderr}, stdout={proc.stdout}"
        envelope = json.loads(proc.stdout.strip().splitlines()[-1])
        assert envelope["ok"] is True
        assert envelope["data"]["db_exists"] is True
        assert envelope["data"]["db_initialized"] is False


def test_preflight_ignores_empty_numbered_headings_and_blocks_overwrite(tmp_path: Path) -> None:
    """Repro #6：仅含题号无正文的空题标题不伪造幻影题，且禁止 out_normalized 覆盖源文件。"""
    cases = {
        "mid_empty": "## 1. \n\n---\n## 2. 有题\n这里是真正的题干文字\n",
        "tail_empty": "## 1. 有题\n这里是真正的题干文字\n\n---\n## 2.\n",
        "cn_empty": "## 第1题\n\n---\n## 第 2 题：\n已知正方形周长为20，求面积。\n",
    }
    for name, raw in cases.items():
        case_dir = tmp_path / name
        case_dir.mkdir(parents=True, exist_ok=True)
        src = case_dir / "q.md"
        src.write_text(raw, encoding="utf-8")

        real_count = len(parse_question_list_markdown(src))
        rep = preflight_question_list_markdown(src, drafts_dir=case_dir / "drafts")
        assert rep["total_questions"] == real_count == 1
        assert src.read_text(encoding="utf-8") == raw

    # 禁止 out_normalized 指向源文件本身
    src_ro = tmp_path / "ro.md"
    src_ro.write_text("## 1. 题干内容测试，求结果。\n", encoding="utf-8")
    with pytest.raises(PreconditionError, match="out_normalized 不得指向源文件本身"):
        preflight_question_list_markdown(src_ro, out_normalized=src_ro)
