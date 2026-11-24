"""冷启动底层 CLI 补强红军实证回归测试（吸收 Repro #1～#3）。"""

from __future__ import annotations

import json
import shutil
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shuati.cli.main import app
from shuati.core.text_norm import (
    compute_content_hash,
    compute_plain_fingerprint,
    normalize_text,
)
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.models.domain import EXIT_PRECONDITION
from shuati.pipeline.stage0_ingest import run_stage0_ingest


def test_repro1_cold_start_human_reset_preserves_learner_and_atomic_precondition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repro #1: human reset 省略 --human-dir 遵循 settings.human_mirror.dir、跨 Step 0->1->3 保真，且前置失败零残留。"""
    runner = CliRunner()
    ext_bank = tmp_path / "external_physics"
    (ext_bank / "初中物理" / "ch01_motion").mkdir(parents=True)
    rows = [
        {"id": f"p{i}", "question": f"某物体在 {i} 秒内匀速移动 {i * 10} 米，求速度是多少米每秒？"}
        for i in range(1, 6)
    ]
    (ext_bank / "初中物理" / "ch01_motion" / "problems.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )

    cfg_dir = tmp_path / "config"
    shutil.copytree(Path("config"), cfg_dir)
    custom_human = tmp_path / "custom_human"
    custom_human.mkdir(parents=True, exist_ok=True)
    (custom_human / "learners.jsonl").write_text(
        json.dumps(
            {
                "created_at": "2026-01-01 00:00:00",
                "default_grade_subject": "小学奥数",
                "display_name": "原仓作者",
                "is_current": 1,
                "learner_id": "repo_owner",
                "notes": "",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    settings_path = cfg_dir / "settings.yaml"
    txt = settings_path.read_text(encoding="utf-8")
    txt = txt.replace('dir: "human/"', f'dir: "{custom_human.as_posix()}"')
    txt = txt.replace('- "题库"', f'- "{ext_bank.as_posix()}"')
    settings_path.write_text(txt, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "data" / "shuati.db"
    arch_dir = tmp_path / "data" / "backups"

    # 1. 省略 --human-dir 时遵循 settings.human_mirror.dir
    r_reset = runner.invoke(
        app,
        [
            "human",
            "reset",
            "--learner-id",
            "alice",
            "--learner-name",
            "Alice",
            "--subject",
            "初中物理",
            "--archive-dir",
            str(arch_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_reset.exit_code == 0, r_reset.output
    assert '"alice"' in (custom_human / "learners.jsonl").read_text(encoding="utf-8")

    # 2. Step 1: grade-subject add + ingest 后 alice 同步入库且 human verify 一致
    r_gs = runner.invoke(
        app,
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
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_gs.exit_code == 0, r_gs.output

    r_ing = runner.invoke(
        app,
        [
            "ingest",
            "--dedup-near",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_ing.exit_code == 0, r_ing.output

    r_ll = runner.invoke(
        app,
        ["learner", "list", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
    )
    assert r_ll.exit_code == 0, r_ll.output
    assert "alice" in [it["learner_id"] for it in json.loads(r_ll.stdout)["data"]["learners"]]

    r_ver = runner.invoke(
        app,
        ["human", "verify", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
    )
    assert r_ver.exit_code == 0, r_ver.output
    assert json.loads(r_ver.stdout)["data"]["is_consistent"] is True

    # 3. Step 3: Stage 1 export/import + calibrate-anchors 后 learners.jsonl 仍保留 alice
    s1_dir = tmp_path / "data" / "unified" / "stage3_4" / "stage1"
    anchor_uids = [f"junior_physics:初中物理/ch01_motion:p{i}" for i in range(1, 5)]
    r_exp = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "export",
            "--grade-subject",
            "初中物理",
            "--only-uids",
            ",".join(anchor_uids),
            "--tasks-dir",
            str(s1_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_exp.exit_code == 0, r_exp.output
    tmap = json.loads((s1_dir / "stage1_task_map.json").read_text(encoding="utf-8"))
    s1_items = [
        {
            "visual_facts": "",
            "visual_ambiguity_flag": False,
            "visual_ambiguity_note": "",
            "method_summary": "速度公式",
            "solution_markdown": "v = s / t = 10。",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "10",
                    "sympy_expr": "10",
                }
            ],
            "sympy_code": "def verify():\n    return {'主问': str(20 // 2)}\n",
            "task_item_id": tid,
        }
        for tid in tmap["items"]
    ]
    res_f = s1_dir / "results.jsonl"
    res_f.write_text(
        json.dumps({"batch_id": "stage1_batch_0001", "result": {"items": s1_items}}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    r_imp = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "import",
            "--tasks-dir",
            str(s1_dir),
            "--results",
            str(res_f),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_imp.exit_code == 0, r_imp.output

    anchors_yaml = cfg_dir / "anchors" / "anchors_junior_physics.yaml"
    anchors_yaml.parent.mkdir(parents=True, exist_ok=True)
    tiers = [
        ("L1_基础直达", 1, 1, 1, 1, 1120),
        ("L2_常规巩固", 1, 2, 1, 1, 1225),
        ("L3_综合中档", 2, 2, 2, 1, 1510),
        ("L4-A_进阶拔高(繁)", 2, 3, 2, 3, 1745),
    ]
    lines = ["grade_subject: 初中物理", 'version: "1.0"', "anchors:"]
    for uid, (tier, c, s, t, l_val, elo) in zip(anchor_uids, tiers, strict=True):
        lines.extend(
            [
                f'  - question_uid: "{uid}"',
                f'    target_tier: "{tier}"',
                "    scores:",
                f"      C: {c}",
                f"      S: {s}",
                f"      T: {t}",
                f"      L: {l_val}",
                f"    base_elo: {elo}",
                f'    rationale: "{tier}"',
            ]
        )
    anchors_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")

    r_cal = runner.invoke(
        app,
        [
            "unified",
            "calibrate-anchors",
            "--grade-subject",
            "初中物理",
            "--anchors-file",
            str(anchors_yaml),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_cal.exit_code == 0, r_cal.output
    assert '"alice"' in (custom_human / "learners.jsonl").read_text(encoding="utf-8")

    # 4. 未注册 --subject 失败时绝不残留任何备份目录
    clean_arch = tmp_path / "clean_arch_on_fail"
    r_bad = runner.invoke(
        app,
        [
            "human",
            "reset",
            "--learner-id",
            "bob",
            "--subject",
            "未注册高中化学",
            "--human-dir",
            str(custom_human),
            "--archive-dir",
            str(clean_arch),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_bad.exit_code == EXIT_PRECONDITION
    assert not clean_arch.exists() or not any(clean_arch.iterdir())


def test_repro2_human_reset_promotes_orphaned_batch_duplicates_of_external_mistake(
    tmp_path: Path,
) -> None:
    """Repro #2: 删除 EXTERNAL_MISTAKE 时将其下挂的 BATCH_JSONL 副本晋升为独立主记录。"""
    runner = CliRunner()
    cfg_dir = tmp_path / "config"
    shutil.copytree(Path("config"), cfg_dir)
    db_path = tmp_path / "data" / "shuati.db"
    human_dir = tmp_path / "human"
    arch_dir = tmp_path / "backups"
    bank_dir = tmp_path / "bank"
    (bank_dir / "ch01_arith").mkdir(parents=True)

    q_text = "计算 12 + 30 等于多少？"
    norm = normalize_text(q_text, num_images=0)
    c_hash = compute_content_hash(norm.normalized_text, [], [])
    p_fp = compute_plain_fingerprint(norm.normalized_text, 0)

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection,
                source_file, source_id, raw_question, content_hash, plain_fingerprint
            ) VALUES (
                'primary_olympiad:ext:e1', '小学奥数', 'EXTERNAL_MISTAKE',
                'external_mistake', '', 'e1', ?, ?, ?
            )
            """,
            (q_text, c_hash, p_fp),
        )
        conn.execute(
            """
            INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format)
            VALUES ('primary_olympiad:ext:e1', '小学奥数', ?, '解答题')
            """,
            (norm.normalized_text,),
        )
        conn.commit()

    (bank_dir / "ch01_arith" / "problems.jsonl").write_text(
        json.dumps({"id": "a_1", "question": q_text}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    r_ing = runner.invoke(
        app,
        [
            "ingest",
            "--root",
            str(bank_dir),
            "--dedup-near",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_ing.exit_code == 0, r_ing.output

    r_reset = runner.invoke(
        app,
        [
            "human",
            "reset",
            "--learner-id",
            "alice",
            "--human-dir",
            str(human_dir),
            "--archive-dir",
            str(arch_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_reset.exit_code == 0, r_reset.output

    with closing(get_connection(db_path, readonly=True)) as conn:
        rq_after = conn.execute(
            "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid = 'primary_olympiad:ch01_arith:a_1'"
        ).fetchone()
        q_after = conn.execute(
            "SELECT question_uid, eff_question_text FROM questions WHERE question_uid = 'primary_olympiad:ch01_arith:a_1'"
        ).fetchone()
        cs_cnt = conn.execute(
            "SELECT COUNT(*) FROM compile_state WHERE question_uid = 'primary_olympiad:ch01_arith:a_1'"
        ).fetchone()[0]
        assert rq_after is not None and rq_after["duplicate_of_uid"] is None
        assert q_after is not None and "12 + 30" in q_after["eff_question_text"]
        assert cs_cnt == 5


def test_repro3_stage1_import_does_not_accumulate_or_retain_stale_retry_file(
    tmp_path: Path,
) -> None:
    """Repro #3: Stage 1 import 绝不跨轮累加已修复旧错，且全量回灌成功后清理陈旧 retry_tasks.jsonl。"""
    runner = CliRunner()
    cfg_dir = tmp_path / "config"
    shutil.copytree(Path("config"), cfg_dir)
    bank = tmp_path / "bank"
    (bank / "ch01").mkdir(parents=True)
    (bank / "ch01" / "problems.jsonl").write_text(
        json.dumps({"id": "q1", "question": "计算 5 + 7 等于多少？"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"id": "q2", "question": "计算 8 + 9 等于多少？"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "data" / "shuati.db"
    s1_dir = tmp_path / "s1"
    run_stage0_ingest(db_path=db_path, root_dir=bank, config_dir=cfg_dir)

    r_exp = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "export",
            "--tasks-dir",
            str(s1_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_exp.exit_code == 0, r_exp.output
    tmap = json.loads((s1_dir / "stage1_task_map.json").read_text(encoding="utf-8"))
    tids = sorted(tmap["items"].keys())

    def _make_item(tid: str, ans: str, expr: str) -> dict[str, Any]:
        return {
            "visual_facts": "",
            "visual_ambiguity_flag": False,
            "visual_ambiguity_note": "",
            "method_summary": "加法",
            "solution_markdown": f"结果为 {ans}。",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": ans,
                    "sympy_expr": expr,
                }
            ],
            "sympy_code": f"def verify():\n    return {{'主问': str({expr})}}\n",
            "task_item_id": tid,
        }

    results_file = s1_dir / "results.jsonl"
    results_file.write_text(
        json.dumps(
            {
                "batch_id": "stage1_batch_0001",
                "result": {
                    "items": [
                        _make_item(tids[0], "12", "5 + 7"),
                        _make_item(tids[1], "17", "8 + 9"),
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with closing(get_connection(db_path)) as conn:
        conn.execute("UPDATE questions SET is_retired = 1;")
        conn.commit()

    r1 = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "import",
            "--tasks-dir",
            str(s1_dir),
            "--results",
            str(results_file),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r1.exit_code == 0, r1.output

    with closing(get_connection(db_path)) as conn:
        conn.execute("UPDATE questions SET is_retired = 0 WHERE question_uid = 'primary_olympiad:ch01:q1';")
        conn.commit()

    r2 = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "import",
            "--tasks-dir",
            str(s1_dir),
            "--results",
            str(results_file),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r2.exit_code == 0, r2.output
    retry_lines_r2 = [
        ln for ln in (s1_dir / "retry_tasks.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(retry_lines_r2) == 1

    with closing(get_connection(db_path)) as conn:
        conn.execute("UPDATE questions SET is_retired = 0;")
        conn.commit()

    r3 = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "import",
            "--tasks-dir",
            str(s1_dir),
            "--results",
            str(results_file),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r3.exit_code == 0, r3.output
    d3 = json.loads(r3.stdout)["data"]
    assert d3["manifest_status"] == "IMPORTED"
    assert d3["errors"] == []
    assert d3["retry_file"] is None
    assert not (s1_dir / "retry_tasks.jsonl").exists()
