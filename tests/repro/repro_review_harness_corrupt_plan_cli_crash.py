"""Repro #3: Corrupted YAML syntax and malformed plan fields crash run_cli instead of clean exit 1."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_harness as rh  # noqa: E402


def _setup_repo(tmp_path: Path, plan_text: str) -> Path:
    (tmp_path / ".agents/rules").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agents/rules/external_review_brief.md").write_text("# Brief\n", encoding="utf-8")
    (tmp_path / "src/shuati/db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/shuati/db/schema.py").write_text(
        'DDL_STATEMENTS = ["CREATE TABLE items (id INT PRIMARY KEY);"]\n',
        encoding="utf-8",
    )
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config/review_plan.yaml").write_text(plan_text, encoding="utf-8")
    return tmp_path


def test_repro_corrupted_yaml_syntax_exits_cleanly_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _setup_repo(tmp_path, "schema_version: 1\nbatches: [unclosed_yaml\n")

    for subcmd in (
        ["status"],
        ["prepare", "B01"],
        ["record", "B01", "--status", "REVIEWED"],
        ["mark-fixed", "B01"],
    ):
        rc = rh.run_cli(["--repo-root", str(repo), *subcmd])
        assert rc == 1
        err = capsys.readouterr().err
        assert "ERROR:" in err


def test_repro_malformed_plan_fields_do_not_crash_with_type_or_attr_error(tmp_path: Path) -> None:
    # 1. batches 为字符串或整数时，status 必须报错返回 1（不能静默当成 0 批返回 0，也不能抛 TypeError）
    repo_str_batches = _setup_repo(tmp_path / "case_str", "schema_version: 1\nbatches: invalid\n")
    assert rh.run_cli(["--repo-root", str(repo_str_batches), "status"]) == 1

    repo_int_batches = _setup_repo(tmp_path / "case_int", "schema_version: 1\nbatches: 123\n")
    assert rh.run_cli(["--repo-root", str(repo_int_batches), "status"]) == 1

    # 2. tier 为 null 或 bool True 时，status 与 prepare 必须返回 1（不能抛 TypeError 或把 True 当成 Tier 1）
    bad_tier_null = {
        "schema_version": 1,
        "batches": [
            {
                "id": "B01",
                "tier": None,
                "title": "Bad tier",
                "targets": [{"path": "src/shuati/db/schema.py"}],
                "tables": ["items"],
                "focus_points": ["fp"],
                "status": "PENDING",
            }
        ],
    }
    repo_tier_null = _setup_repo(tmp_path / "case_tier_null", yaml.safe_dump(bad_tier_null))
    assert rh.run_cli(["--repo-root", str(repo_tier_null), "status"]) == 1
    assert rh.run_cli(["--repo-root", str(repo_tier_null), "prepare", "B01"]) == 1

    bad_tier_bool = {
        "schema_version": 1,
        "batches": [
            {
                "id": "B01",
                "tier": True,
                "title": "Bool tier",
                "targets": [{"path": "src/shuati/db/schema.py"}],
                "tables": ["items"],
                "focus_points": ["fp"],
                "status": "PENDING",
            }
        ],
    }
    repo_tier_bool = _setup_repo(tmp_path / "case_tier_bool", yaml.safe_dump(bad_tier_bool))
    assert rh.run_cli(["--repo-root", str(repo_tier_bool), "status"]) == 1
    assert rh.run_cli(["--repo-root", str(repo_tier_bool), "prepare", "B01"]) == 1

    # 3. targets 元素非 dict，或 reviewed_by_models 为字符串/整数时，不得抛 AttributeError/TypeError 或拆成单字符
    bad_shapes = {
        "schema_version": 1,
        "batches": [
            {
                "id": "B01",
                "tier": 1,
                "title": "Bad target & models",
                "targets": ["src/shuati/db/schema.py"],
                "tables": ["items"],
                "focus_points": ["fp"],
                "status": "PENDING",
                "reviewed_by_models": "codex-cli",
            }
        ],
    }
    repo_shapes = _setup_repo(tmp_path / "case_shapes", yaml.safe_dump(bad_shapes))
    assert rh.run_cli(["--repo-root", str(repo_shapes), "prepare", "B01"]) == 1
    assert rh.run_cli(["--repo-root", str(repo_shapes), "status"]) == 1
    assert (
        rh.run_cli(["--repo-root", str(repo_shapes), "record", "B01", "--status", "REVIEWED"]) == 1
    )


def test_repro_extract_dependency_stubs_rejects_bool_in_ranges(tmp_path: Path) -> None:
    repo = _setup_repo(tmp_path / "case_bool_range", "schema_version: 1\nbatches: []\n")
    with pytest.raises(ValueError):
        rh.extract_dependency_stubs(
            repo,
            [{"path": "src/shuati/db/schema.py", "ranges": [[True, 1]]}],
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
