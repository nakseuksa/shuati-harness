"""Prompt Specs v2.0 回归测试：吸收 Round 2 Repro（CLI `compile --stage 3d` / `--stage all` 透传 `config_dir` 与 `prompt_profile`）。"""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shuati.cli.main import app
from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.pipeline import stage3_taxonomy, stage4_weave


def _setup_custom_config(tmp_path: Path, *, override_stage3d: str | None = None) -> Path:
    cfg = tmp_path / "config"
    shutil.copytree(Path("config"), cfg)
    st = (cfg / "settings.yaml").read_text(encoding="utf-8")
    st = st.replace('dir: "human/"', f'dir: "{(tmp_path / "human").as_posix()}"')
    (cfg / "settings.yaml").write_text(st, encoding="utf-8")
    if override_stage3d is not None:
        dy = cfg / "prompts" / "default.yaml"
        txt = dy.read_text(encoding="utf-8")
        assert 'stage3d: "s3d_v2"' in txt
        dy.write_text(
            txt.replace('stage3d: "s3d_v2"', f'stage3d: "{override_stage3d}"'),
            encoding="utf-8",
        )
    return cfg


def test_compile_stage_3d_passes_config_dir_and_prompt_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup_custom_config(tmp_path, override_stage3d="s3d_custom_99")
    seen: dict[str, Any] = {}

    def _fake_mount(conn: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"mounted": 0}

    monkeypatch.setattr(stage3_taxonomy, "run_stage3d_mount", _fake_mount)
    res = CliRunner().invoke(
        app,
        [
            "compile",
            "--stage",
            "3d",
            "--db",
            str(tmp_path / "t3d.db"),
            "--config-dir",
            str(cfg),
            "--prompt-profile",
            "strict_prof",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    assert Path(str(seen.get("config_dir", ""))) == cfg
    assert seen.get("prompt_profile") == "strict_prof"


def test_compile_stage_all_passes_config_dir_and_prompt_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup_custom_config(tmp_path)
    seen: dict[str, dict[str, Any]] = {}

    def _make_sync(name: str) -> Any:
        def _fn(conn: Any, *args: Any, **kw: Any) -> dict[str, Any]:
            seen[name] = kw
            return {}

        return _fn

    def _make_async(name: str) -> Any:
        async def _fn(conn: Any, *args: Any, **kw: Any) -> dict[str, Any]:
            seen[name] = kw
            return {}

        return _fn

    monkeypatch.setattr(stage3_taxonomy, "run_stage3c_async", _make_async("3c"))
    monkeypatch.setattr(stage3_taxonomy, "run_stage3d_mount", _make_sync("3d"))
    monkeypatch.setattr(stage3_taxonomy, "run_compaction_async", _make_async("compact"))
    monkeypatch.setattr(stage3_taxonomy, "run_stage3e_async", _make_async("3e"))
    monkeypatch.setattr(stage4_weave, "run_stage4_weave_async", _make_async("4"))

    res = CliRunner().invoke(
        app,
        [
            "compile",
            "--stage",
            "all",
            "--mode",
            "api",
            "--mock",
            "--db",
            str(tmp_path / "tall.db"),
            "--config-dir",
            str(cfg),
            "--prompt-profile",
            "custom_prof",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    for stage_key in ("3c", "3d", "compact", "3e", "4"):
        assert stage_key in seen
        assert Path(str(seen[stage_key].get("config_dir", ""))) == cfg
        assert seen[stage_key].get("prompt_profile") == "custom_prof"


def test_compile_stage_3d_persists_custom_prompt_version_e2e(tmp_path: Path) -> None:
    cfg = _setup_custom_config(tmp_path, override_stage3d="s3d_custom_99")
    db_path = tmp_path / "e2e_3d.db"
    tax_file = tmp_path / "tax.yaml"
    tax_file.write_text(
        "schema_version: 1\ngrade_subject: 小学奥数\nslug: primary_olympiad\nstatus: DRAFT\nversion: draft-1\n"
        "modules:\n  - name: 平面几何\n    aliases: []\n    l2_entities:\n      - name: 等高模型\n        aliases: []\n        description: 等高\n",
        encoding="utf-8",
    )

    uid = "primary_olympiad:ch_test:q1"
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg)
        conn.execute(
            "UPDATE grade_subjects SET taxonomy_status='DRAFT', taxonomy_version='draft-1', taxonomy_file=? WHERE grade_subject='小学奥数'",
            (str(tax_file),),
        )
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection,
                source_file, source_id, raw_question, cas_images_json,
                image_captions_json, legacy_tags_json, content_hash
            ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'c', 'f', 'q1', '求面积', '[]', '[]', '[]', 'h1')
            """,
            (uid,),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, cas_images_json, image_captions_json,
                has_images, m_question_text, m_options_json, m_format,
                m_verification_status, m_primary_l1_module
            ) VALUES (?, '小学奥数', '[]', '[]', 0, '求面积', '{}', '解答题', 'VERIFIED_CONSENSUS', '平面几何')
            """,
            (uid,),
        )
        conn.execute(
            "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解')",
            (f"{uid}::branch_1", uid),
        )
        conn.execute(
            "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, raw_l1, raw_l2, raw_l3, m_l1, m_l2) VALUES ('s1', ?, 'branch_1', 1, '求面积', '平面几何', '等高模型', '等高模型面积比转化', '平面几何', '等高模型')",
            (uid,),
        )
        conn.execute("INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S3C', 'OK')", (uid,))
        effective.recompute(conn, uid)
        conn.commit()

    res = CliRunner().invoke(
        app,
        [
            "compile",
            "--stage",
            "3d",
            "--grade-subject",
            "小学奥数",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg),
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    with closing(get_connection(db_path, readonly=True)) as conn:
        row = conn.execute(
            "SELECT status, prompt_version FROM compile_state WHERE question_uid = ? AND stage = 'S3D'",
            (uid,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "OK"
        assert row["prompt_version"] == "s3d_custom_99"
