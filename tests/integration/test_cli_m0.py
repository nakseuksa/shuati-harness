"""测试 M0 CLI 命令、退出码规范与 Windows UTF-8 子进程管道契约。"""

import json
import subprocess
import sys
from pathlib import Path

from shuati.db.connection import get_connection
from shuati.db.human_mirror import export_snapshot
from shuati.db.schema import init_db
from shuati.models.domain import (
    EXIT_OK,
    EXIT_PRECONDITION,
)


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """通过子进程调用 CLI，验证真实控制台/管道输出与编码。"""
    cmd = [sys.executable, "-m", "shuati.cli.main"] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cli_version_utf8_json_envelope():
    # 验证标准 JSON 信封与 Windows UTF-8 管道解析
    proc = _run_cli(["version", "--json"])
    assert proc.returncode == EXIT_OK
    assert proc.stderr == ""

    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["command"] == "version"
    assert "version" in data["data"]
    assert data["meta"]["schema_version"] == 1


def test_cli_grade_subject_lifecycle(tmp_path: Path):
    db_path = tmp_path / "test_cli_gs.db"

    # 1. 添加初中物理
    proc_add = _run_cli(
        [
            "grade-subject",
            "add",
            "--name",
            "初中物理",
            "--slug",
            "junior_physics",
            "--family",
            "PHYSICS",
            "--db",
            str(db_path),
            "--json",
        ]
    )
    assert proc_add.returncode == EXIT_OK
    res_add = json.loads(proc_add.stdout)
    assert res_add["ok"] is True
    assert res_add["data"]["grade_subject"] == "初中物理"

    # 2. 列出学段
    proc_list = _run_cli(
        [
            "grade-subject",
            "list",
            "--db",
            str(db_path),
            "--json",
        ]
    )
    assert proc_list.returncode == EXIT_OK
    res_list = json.loads(proc_list.stdout)
    assert res_list["ok"] is True
    names = [item["grade_subject"] for item in res_list["data"]["items"]]
    assert "小学奥数" in names
    assert "初中物理" in names

    # 3. 重复添加相同 slug 或 name，预期退出码 2 (PRECONDITION_FAILED)
    proc_dup = _run_cli(
        [
            "grade-subject",
            "add",
            "--name",
            "初中物理",
            "--slug",
            "junior_physics",
            "--family",
            "PHYSICS",
            "--db",
            str(db_path),
            "--json",
        ]
    )
    assert proc_dup.returncode == EXIT_PRECONDITION
    res_dup = json.loads(proc_dup.stdout)
    assert res_dup["ok"] is False
    assert res_dup["error"]["code"] == "PRECONDITION_FAILED"


def test_cli_ingest_single_collection_constraint(tmp_path: Path):
    db_path = tmp_path / "test_cli_ingest.db"
    reports_dir = tmp_path / "reports"

    # 题库根目录下有两个集合，显式传 --grade-subject 必须拦截并返回退出码 2
    proc = _run_cli(
        [
            "ingest",
            "--root",
            "题库",
            "--grade-subject",
            "小学奥数",
            "--db",
            str(db_path),
            "--reports-dir",
            str(reports_dir),
            "--json",
        ]
    )
    assert proc.returncode == EXIT_PRECONDITION
    res = json.loads(proc.stdout)
    assert res["ok"] is False
    assert res["error"]["code"] == "PRECONDITION_FAILED"
    assert "显式覆盖仅限单集合导入" in res["error"]["message"]


def test_cli_human_and_reindex(tmp_path: Path):
    db_path = tmp_path / "test_cli_human.db"
    human_dir = tmp_path / "human"
    backups_dir = tmp_path / "backups"

    conn = get_connection(db_path)
    init_db(conn)

    # 导出基准 snapshot
    export_snapshot(conn, human_dir=human_dir)

    # 1. 验证 human verify --json
    proc_verify = _run_cli(
        [
            "human",
            "verify",
            "--from",
            str(human_dir),
            "--db",
            str(db_path),
            "--json",
        ]
    )
    assert proc_verify.returncode == EXIT_OK
    res_verify = json.loads(proc_verify.stdout)
    assert res_verify["ok"] is True
    assert res_verify["data"]["is_consistent"] is True

    # 2. 验证 human restore --dry-run --json
    proc_restore = _run_cli(
        [
            "human",
            "restore",
            "--from",
            str(human_dir),
            "--dry-run",
            "--backups-dir",
            str(backups_dir),
            "--db",
            str(db_path),
            "--json",
        ]
    )
    assert proc_restore.returncode == EXIT_OK
    res_restore = json.loads(proc_restore.stdout)
    assert res_restore["ok"] is True
    assert res_restore["data"]["dry_run"] is True

    # 3. 验证 reindex --json
    proc_reindex = _run_cli(
        [
            "reindex",
            "--fts",
            "--effective",
            "--db",
            str(db_path),
            "--json",
        ]
    )
    assert proc_reindex.returncode == EXIT_OK
    res_reindex = json.loads(proc_reindex.stdout)
    assert res_reindex["ok"] is True
    assert "fts_rebuilt" in res_reindex["data"]
    assert "effective_recomputed" in res_reindex["data"]
