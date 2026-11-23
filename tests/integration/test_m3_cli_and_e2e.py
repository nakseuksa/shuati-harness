"""M3 端到端集成与 Windows 子进程 CLI JSON 契约测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db


def _run_cli(args: list[str], cwd: Path) -> tuple[int, dict]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(
        [sys.executable, "-m", "shuati.cli.main", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout.strip(), f"CLI stdout 为空, stderr={proc.stderr}"
    payload = json.loads(proc.stdout.strip())
    return proc.returncode, payload


def _build_demo_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    db_path = tmp_path / "data" / "shuati.db"
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # 拷贝默认 config 目录关键 YAML
    repo_cfg = Path("config")
    (cfg_dir / "settings.yaml").write_text(
        (repo_cfg / "settings.yaml").read_text(encoding="utf-8").replace("use_mock_llm: false", "use_mock_llm: true"),
        encoding="utf-8",
    )
    if (repo_cfg / "method_blacklist.yaml").exists():
        (cfg_dir / "method_blacklist.yaml").write_text(
            (repo_cfg / "method_blacklist.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject='小学奥数';")
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES ('kn_l2', '小学奥数', '1.0', 'L2', '平面几何', '等高模型', ''), ('kn_l3', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '同底等高');",
    )

    samples = [
        ("primary_olympiad:ch01:q1", "q1", "底为6高为4求三角形面积", "NUMERIC", "SYMBOLIC", "12", 2, 1, 1, 1),
        ("primary_olympiad:ch01:q2", "q2", "请简述三角形等高模型的证明理由", "PROOF", "QUALITATIVE", "同底等高面积相等", 2, 2, 2, 1),
        ("primary_olympiad:ch01:q3", "q3", "高阶压轴三角形阴影面积构造问题", "NUMERIC", "SYMBOLIC", "24", 3, 2, 3, 1),
    ]
    for uid, sid, txt, atype, vmode, ans, c, s, t, l_score in samples:
        fa = {
            "label": "主问",
            "answer_type": atype,
            "verify_mode": vmode,
            "collection_mode": "SINGLE",
            "answer": ans,
            "sympy_expr": ans if vmode == "SYMBOLIC" else None,
        }
        conn.execute(
            "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', ?, ?, ?);",
            (uid, sid, txt, f"h_{uid}"),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_format, m_verification_status,
                m_visual_facts, m_abstract_skeleton, m_primary_l1_module,
                m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json
            )
            VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', '视觉图例描述', '等高面积比例模型', '平面几何', ?, ?, ?, ?, ?);
            """,
            (uid, txt, c, s, t, l_score, json.dumps([fa], ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '标准解答');",
            (f"{uid}::branch_1", uid),
        )
        conn.execute(
            "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, core_formula, hint, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, 'branch_1', 1, '关键等高关系', 'S=ah/2', '试试比较底边比例', '平面几何', '等高模型', 'kn_l3');",
            (f"{uid}::branch_1::1", uid),
        )
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK'), (?, 'S4', 'OK');",
            (uid, uid, uid, uid, uid),
        )
        effective.recompute(conn, uid)

    # SCAFFOLD: q1(低) -> q3(高)
    conn.execute(
        "INSERT INTO question_relations(source_question_uid, target_question_uid, relation_type, is_directed, is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct, containment_score, trap_overlap, confidence_source, pedagogical_note) VALUES ('primary_olympiad:ch01:q1', 'primary_olympiad:ch01:q3', 'SCAFFOLD', 1, 0, 0.4, 0.88, 0.4, 0.92, 0.8, 0.0, 'RULE_MATRIX', '先会基础面积再做复杂阴影');"
    )
    conn.commit()
    conn.close()
    return db_path, cfg_dir, Path.cwd()


def test_m3_cli_full_e2e_and_utf8_json_envelope(tmp_path: Path) -> None:
    db_path, cfg_dir, repo_root = _build_demo_workspace(tmp_path)
    human_dir = tmp_path / "human"
    vec_dir = tmp_path / "vectors"
    drafts_dir = tmp_path / "drafts"
    sheets_dir = tmp_path / "sheets"
    assets_dir = tmp_path / "assets"

    # 1. 重建 FTS5 + Mock 向量分位表
    rc, res = _run_cli(
        ["reindex", "--fts", "--vectors", "--quantiles", "--mock", "--db", str(db_path), "--config-dir", str(cfg_dir), "--vectors-dir", str(vec_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and res["ok"] is True

    # 2. learner create + use + taxonomy
    rc, res = _run_cli(
        ["learner", "create", "xiaoming", "--name", "小明", "--current", "--db", str(db_path), "--config-dir", str(cfg_dir), "--human-dir", str(human_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and res["data"]["is_current"] == 1

    rc, res = _run_cli(
        ["taxonomy", "--grade-subject", "小学奥数", "--learner", "xiaoming", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and len(res["data"]["modules"]) == 1

    # 3. search + detail(quiz/hint) + related(pre/post)
    rc, res = _run_cli(
        ["search", "--query", "三角形", "--l1", "平面几何", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and res["data"]["returned"] == 3
    assert all("final_answers" not in it for it in res["data"]["items"])

    rc, res = _run_cli(
        ["detail", "q1", "--reveal", "hint", "--hint-level", "1", "--db", str(db_path), "--json"],
        repo_root,
    )
    assert rc == 0 and len(res["data"]["hints"]) == 1 and "final_answers" not in res["data"]

    rc, res = _run_cli(
        ["related", "q3", "--types", "SCAFFOLD", "--direction", "pre", "--db", str(db_path), "--json"],
        repo_root,
    )
    assert rc == 0 and res["data"]["count"] == 1
    assert res["data"]["items"][0]["question"]["question_uid"] == "primary_olympiad:ch01:q1"
    assert "pedagogical_note" not in res["data"]["items"][0]

    # 4. compose-quiz + render-sheet
    rc, res = _run_cli(
        ["compose-quiz", "--grade-subject", "小学奥数", "--l1", "平面几何", "--count", "3", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and res["data"]["actual_count"] == 3
    batch_id = res["data"]["quiz_batch_id"]

    rc, res = _run_cli(
        ["render-sheet", "--quiz-batch-id", batch_id, "--sheets-dir", str(sheets_dir), "--assets-dir", str(assets_dir), "--db", str(db_path), "--json"],
        repo_root,
    )
    assert rc == 0 and Path(res["data"]["sheet_path"]).exists()

    # 5. check (SYMBOLIC 正确 & QUALITATIVE + --log 幂等) + log-void + profile
    rc, res = _run_cli(
        ["check", "q1", "--answer", "12", "--learner", "xiaoming", "--log", "--request-id", "req_q1", "--db", str(db_path), "--config-dir", str(cfg_dir), "--human-dir", str(human_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and res["data"]["all_correct"] is True and res["data"]["outcome"] == "SOLVED_SOLO"
    assert "final_answers" not in res["data"] and "solutions" not in res["data"]

    rc, res_q2 = _run_cli(
        ["check", "q2", "--answer", "连接中线可证明等底等高面积相同", "--judge-verdict", '{"equivalent": false, "confidence": "HIGH", "reason": "标准答案是全等"}', "--learner", "xiaoming", "--log", "--db", str(db_path), "--config-dir", str(cfg_dir), "--human-dir", str(human_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and res_q2["data"]["all_correct"] is False and "judge_reason" not in json.dumps(res_q2["data"])
    log_id_q2 = res_q2["data"]["log_id"]

    rc, res = _run_cli(
        ["log-void", str(log_id_q2), "--reason", "误交", "--db", str(db_path), "--config-dir", str(cfg_dir), "--human-dir", str(human_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and res["data"]["voided_log_id"] == log_id_q2

    rc, res = _run_cli(
        ["profile", "--learner", "xiaoming", "--grade-subject", "小学奥数", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and res["data"]["overall"]["valid_attempts"] == 1

    # 6. diagnose -> ingest-mistake --log-wrong -> external_questions.jsonl + human verify
    rc, diag_res = _run_cli(
        ["diagnose", "--text", "已知矩形长8宽6求对角分割三角形面积", "--grade-subject", "小学奥数", "--answer", "24", "--mock", "--db", str(db_path), "--config-dir", str(cfg_dir), "--vectors-dir", str(vec_dir), "--drafts-dir", str(drafts_dir), "--json"],
        repo_root,
    )
    assert rc == 0, f"diagnose 失败: {diag_res}"
    assert diag_res["data"]["unverified_single_pass"] is True and diag_res["data"]["known_answer_match"] is True
    draft_id = diag_res["data"]["draft_id"]

    rc, ing_res = _run_cli(
        ["ingest-mistake", "--draft-id", draft_id, "--log-wrong", "--learner", "xiaoming", "--mock", "--db", str(db_path), "--config-dir", str(cfg_dir), "--assets-dir", str(assets_dir), "--vectors-dir", str(vec_dir), "--drafts-dir", str(drafts_dir), "--human-dir", str(human_dir), "--json"],
        repo_root,
    )
    assert rc == 0, f"ingest-mistake 失败: {ing_res}"
    assert ing_res["data"]["verification_status"] == "HUMAN_VERIFIED"
    assert (human_dir / "external_questions.jsonl").exists()

    rc, ver_res = _run_cli(
        ["human", "verify", "--from", str(human_dir), "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and ver_res["data"]["is_consistent"] is True

    # 7. override (--dry-run -> set -> revoke) + skill install + NOT_FOUND exit=3
    rc, ov_dry = _run_cli(
        ["override", "q1", "--field", "abstract_skeleton", "--value", "新骨架", "--dry-run", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and ov_dry["data"]["dry_run"] is True

    rc, ov_set = _run_cli(
        ["override", "q1", "--field", "abstract_skeleton", "--value", "新骨架", "--db", str(db_path), "--config-dir", str(cfg_dir), "--human-dir", str(human_dir), "--json"],
        repo_root,
    )
    assert rc == 0 and ov_set["data"]["action"] == "set"

    skill_dst = tmp_path / ".pi" / "skills" / "shuati"
    rc, sk_res = _run_cli(
        ["skill", "install", "--target", str(skill_dst), "--copy", "--json"],
        repo_root,
    )
    assert rc == 0 and sk_res["data"]["skill_md_exists"] is True

    rc_nf, err_nf = _run_cli(["detail", "non_existent_id", "--db", str(db_path), "--json"], repo_root)
    assert rc_nf == 3 and err_nf["ok"] is False and err_nf["error"]["code"] == "NOT_FOUND"
