"""Wave 2 集成测试：自包含批导出与防踩踏锁、存量/新锁双向兼容、CLI 原子命令族与角色隔离 stdio MCP。"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shuati.cli.main import app
from shuati.config import Settings
from shuati.core import effective
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.mcp.server import dispatch_jsonrpc_message, get_tools_for_role, serve_stdio
from shuati.models.schemas import TaxonomyProposeOutput
from shuati.pipeline.stage1_solve import run_stage1_compile
from shuati.pipeline.stage3_taxonomy import propose_taxonomy_async, run_stage3c_async
from shuati.pipeline.subagent_bridge import export_task_batches, load_and_validate_results
from shuati.pipeline.unified_import import (
    export_unified_tasks,
    import_unified_results,
    is_compile_locked,
    register_anchors_from_yaml,
)


def _setup_config_dir(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "config"
    shutil.copytree(Path("config"), cfg_dir)
    settings_txt = (cfg_dir / "settings.yaml").read_text(encoding="utf-8")
    settings_txt = settings_txt.replace('dir: "human/"', f'dir: "{(tmp_path / "human").as_posix()}"')
    (cfg_dir / "settings.yaml").write_text(settings_txt, encoding="utf-8")
    return cfg_dir


def _seed_question(
    conn: Any,
    *,
    uid: str,
    source_id: str,
    text: str,
    content_hash: str,
    verified: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_file, source_id, raw_question, cas_images_json,
            image_captions_json, legacy_tags_json, content_hash
        ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch_secret_collection', 'ch_secret/problems.jsonl', ?, ?, '[]', '[]', '["secret_legacy_tag"]', ?)
        """,
        (uid, source_id, text, content_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, cas_images_json, image_captions_json,
            has_images, m_question_text, m_options_json, m_format,
            m_verification_status, m_final_answers_json
        ) VALUES (?, '小学奥数', '[]', '[]', 0, ?, '{}', '解答题', ?, ?)
        """,
        (
            uid,
            text,
            "VERIFIED_CONSENSUS" if verified else "PENDING",
            json.dumps(
                [
                    {
                        "label": "主问",
                        "answer_type": "NUMERIC",
                        "verify_mode": "SYMBOLIC",
                        "collection_mode": "SINGLE",
                        "answer": "42",
                        "unit": None,
                        "sympy_expr": "42",
                        "items": None,
                        "choice_label": None,
                        "canonical": None,
                    }
                ],
                ensure_ascii=False,
            )
            if verified
            else "[]",
        ),
    )
    for stg in ("S1", "S2", "S3C", "S3D", "S4"):
        st = "OK" if (verified and stg == "S1") else "PENDING"
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, ?)",
            (uid, stg, st),
        )
    effective.recompute(conn, uid)


def _write_anchors(cfg_dir: Path, anchor_uids: list[str]) -> Path:
    anchors_path = cfg_dir / "anchors" / "anchors_primary_olympiad.yaml"
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    tiers = [
        ("L1_基础直达", 1, 1, 1, 1, 1120),
        ("L3_综合中档", 2, 2, 2, 1, 1510),
        ("L4-A_进阶拔高(繁)", 2, 3, 2, 3, 1745),
        ("L5-2_竞赛超难", 4, 2, 2, 1, 2530),
    ]
    lines = ["grade_subject: 小学奥数", 'version: "1.0"', "anchors:"]
    for uid, (tier, c, s, t, l, elo) in zip(anchor_uids, tiers, strict=True):
        lines.extend(
            [
                f'  - question_uid: "{uid}"',
                f'    target_tier: "{tier}"',
                "    scores:",
                f"      C: {c}",
                f"      S: {s}",
                f"      T: {t}",
                f"      L: {l}",
                f"    base_elo: {elo}",
                f'    rationale: "校准 {tier}"',
            ]
        )
    anchors_path.write_text("\n".join(lines), encoding="utf-8")
    return anchors_path


def _sample_unified_candidate(task_id: str = "T0001") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "visual_facts": "",
        "visual_ambiguity_flag": False,
        "visual_ambiguity_note": "",
        "method_summary": "直接整数加法",
        "solution_markdown": "12 + 30 = 42。",
        "final_answers": [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "42",
                "sympy_expr": "42",
            }
        ],
        "sympy_code": "def verify():\n    return {'主问': str(12 + 30)}\n",
        "steps": [
            {
                "step_index": 1,
                "sub_goal": "将两个加数相加",
                "core_formula": "a + b = c",
                "hint": "把十位和个位分别对齐相加",
                "raw_l1": "计算与数感",
                "raw_l2": "加减法巧算",
                "raw_l3": "整数加减凑整转化",
            }
        ],
        "abstract_skeleton": "两整数直接求和模型",
        "primary_l1_module": "计算与数感",
        "secondary_l1_modules": [],
        "thinking_methods": ["位值拆分法"],
        "classic_motifs": [],
        "trap_tags": [],
        "difficulty": {
            "C_concept_count": {"evidence": "仅 1 个核心概念", "score": 1},
            "S_chain_length": {"evidence": "1 步推演", "score": 1},
            "T_trickiness": {"evidence": "直接求解无弯绕", "score": 1},
            "L_computation_load": {"evidence": "两位数口算", "score": 1},
        },
    }


def _assert_no_refs(schema_obj: dict[str, Any]) -> None:
    text = json.dumps(schema_obj, ensure_ascii=False)
    assert '"$defs"' not in text
    assert '"$ref"' not in text


@pytest.mark.asyncio
async def test_self_contained_unified_export_lock_and_legacy_compat(tmp_path: Path) -> None:
    cfg_dir = _setup_config_dir(tmp_path)
    db_path = tmp_path / "test.db"

    anchor_uids = [f"primary_olympiad:ch_test:anc_{i}" for i in range(1, 5)]
    pending_new_uid = "primary_olympiad:ch_test:q_new"
    legacy_uid = "primary_olympiad:ch_test:q_legacy"

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        for idx, uid in enumerate(anchor_uids, 1):
            _seed_question(
                conn,
                uid=uid,
                source_id=f"anc_{idx}",
                text=f"已知数字 555，求基础结果 {idx}",
                content_hash=f"hash_anc_{idx}",
                verified=True,
            )
            conn.execute(
                """
                INSERT INTO question_solutions(
                    solution_id, question_uid, branch_id, is_optimal, source_origin,
                    method_summary, solution_markdown, sympy_verified
                ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '直算', '解析', 1)
                """,
                (f"{uid}::branch_1", uid),
            )
        _seed_question(
            conn,
            uid=pending_new_uid,
            source_id="q_new",
            text="计算 12 + 30 等于多少？",
            content_hash="hash_new",
        )
        _seed_question(
            conn,
            uid=legacy_uid,
            source_id="q_legacy",
            text="计算 100 + 200 等于多少？",
            content_hash="hash_legacy",
            verified=True,
        )
        # 模拟存量 1,725 题的旧锁 ("subagent-unified")
        conn.execute(
            """
            INSERT OR REPLACE INTO compile_state(
                question_uid, stage, status, input_hash, prompt_version, model_name, attempts, last_error
            ) VALUES
                (?, 'S1', 'OK', 'hash_legacy', 'unified-v1.0', 'subagent-unified', 1, ''),
                (?, 'S2', 'OK', 'hash_legacy', 'unified-v1.0', 'subagent-unified', 1, '')
            """,
            (legacy_uid, legacy_uid),
        )
        conn.commit()

    # 双向锁兼容性检查：同时识别 subagent-unified 与 subagent:*
    assert is_compile_locked("OK", "subagent-unified", "h1", "h1") is True
    assert is_compile_locked("OK", "subagent:gemini-4-neon-medium", "h1", "h1") is True
    assert is_compile_locked("OK", "subagent:gemini-4-neon-medium", "h1", "h2") is False
    assert is_compile_locked("OK", "subagent:gemini-4-neon-medium", "h1", "h1", force=True) is False

    anchors_yaml = _write_anchors(cfg_dir, anchor_uids)
    register_anchors_from_yaml(db_path=db_path, config_dir=cfg_dir, anchors_file=anchors_yaml)

    # 1. 自包含导出 + 拼写错误拦截
    out_dir = tmp_path / "unified_out"
    with pytest.raises(PreconditionError, match="gemini-4-neon-medium"):
        export_unified_tasks(
            db_path=db_path,
            out_dir=out_dir,
            config_dir=cfg_dir,
            subagent_model="gemini-4-neno-medium",
        )

    exp = export_unified_tasks(
        db_path=db_path,
        out_dir=out_dir,
        config_dir=cfg_dir,
        only_uids=[pending_new_uid],
    )
    assert exp.exported_unified == 1
    assert exp.subagent_model == "gemini-4-neon-medium"

    manifest = json.loads((out_dir / "unified_manifest.json").read_text(encoding="utf-8"))
    assert manifest["subagent_model"] == "gemini-4-neon-medium"
    assert manifest["dispatch_driver"] == "swarm"
    assert manifest["max_parallel_workers"] == 4
    assert manifest["status"] == "PENDING_IMPORT"
    assert manifest["prompt_version"] == "unified-v1.0"
    assert len(manifest["prompt_digest"]) == 16

    flat_u = json.loads((out_dir / "flat_schema_unified.json").read_text(encoding="utf-8"))
    flat_s2 = json.loads((out_dir / "flat_schema_stage2.json").read_text(encoding="utf-8"))
    _assert_no_refs(flat_u)
    _assert_no_refs(flat_s2)
    assert flat_u["properties"]["difficulty"]["properties"]["C_concept_count"]["properties"]["score"]["maximum"] == 4
    assert flat_u["properties"]["difficulty"]["properties"]["S_chain_length"]["properties"]["score"]["maximum"] == 3

    tasks_lines = [json.loads(line) for line in (out_dir / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(tasks_lines) == 1
    assert "messages" in tasks_lines[0]
    assert len(tasks_lines[0]["messages"]) == 2
    tasks_raw_text = (out_dir / "tasks.jsonl").read_text(encoding="utf-8")
    for forbidden in ("primary_olympiad:", "ch_secret_collection", "secret_legacy_tag", "q_new"):
        assert forbidden not in tasks_raw_text

    # 2. 防踩踏锁：PENDING_IMPORT 时禁止覆盖导出
    with pytest.raises(PreconditionError, match="PENDING_UNIMPORTED_BATCH"):
        export_unified_tasks(
            db_path=db_path,
            out_dir=out_dir,
            config_dir=cfg_dir,
            only_uids=[pending_new_uid],
            overwrite_pending=False,
        )

    # overwrite_pending=True 时放行
    exp_ov = export_unified_tasks(
        db_path=db_path,
        out_dir=out_dir,
        config_dir=cfg_dir,
        only_uids=[pending_new_uid],
        overwrite_pending=True,
    )
    assert exp_ov.exported_unified == 1

    tmap = json.loads((out_dir / "task_map.json").read_text(encoding="utf-8"))
    tid = next(iter(tmap))
    res_file = out_dir / "results.jsonl"
    res_file.write_text(json.dumps(_sample_unified_candidate(tid), ensure_ascii=False) + "\n", encoding="utf-8")

    imp = import_unified_results(
        db_path=db_path,
        results_file=res_file,
        task_map_file=out_dir / "task_map.json",
        config_dir=cfg_dir,
    )
    assert imp.ingested_verified == 1
    assert imp.retry_count == 0
    assert imp.failed_final_count == 0

    manifest_after = json.loads((out_dir / "unified_manifest.json").read_text(encoding="utf-8"))
    assert manifest_after["status"] == "IMPORTED"

    # 验证库内写入 subagent:gemini-4-neon-medium 且 stage1_solve 不重复覆盖新旧锁
    with closing(get_connection(db_path, readonly=True)) as conn:
        run_row = conn.execute(
            "SELECT model_name FROM stage1_vlm_runs WHERE question_uid = ? ORDER BY run_id DESC LIMIT 1",
            (pending_new_uid,),
        ).fetchone()
        assert run_row["model_name"] == "subagent:gemini-4-neon-medium"
        cs_s1 = conn.execute(
            "SELECT status, model_name FROM compile_state WHERE question_uid = ? AND stage = 'S1'",
            (pending_new_uid,),
        ).fetchone()
        assert cs_s1["model_name"] == "subagent:gemini-4-neon-medium"

    s1_check = await run_stage1_compile(
        db_path=db_path,
        config_dir=cfg_dir,
        only_uids=[pending_new_uid, legacy_uid],
        use_mock=True,
        force=False,
    )
    assert s1_check.skipped_ok == 2
    assert s1_check.processed == 0

    # IMPORTED 后允许再次安全导出，但若出现更新的未消费 results*.jsonl 仍会被防踩踏拦截
    time.sleep(0.02)
    extra_res = out_dir / "results_new.jsonl"
    extra_res.write_text('{"task_id": "T9999"}\n', encoding="utf-8")
    with pytest.raises(PreconditionError, match="PENDING_UNIMPORTED_BATCH"):
        export_unified_tasks(
            db_path=db_path,
            out_dir=out_dir,
            config_dir=cfg_dir,
            only_uids=anchor_uids,
        )

    # 无 manifest 且不传 subagent_model 时保持 "subagent-unified" 向后兼容
    no_mf_dir = tmp_path / "no_manifest"
    no_mf_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_dir / "task_map.json", no_mf_dir / "task_map.json")
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "UPDATE compile_state SET status='PENDING', model_name='' WHERE question_uid = ?",
            (pending_new_uid,),
        )
        conn.commit()
    imp_compat = import_unified_results(
        db_path=db_path,
        results_file=res_file,
        task_map_file=no_mf_dir / "task_map.json",
        config_dir=cfg_dir,
    )
    assert imp_compat.ingested_verified == 1
    with closing(get_connection(db_path, readonly=True)) as conn:
        cs_compat = conn.execute(
            "SELECT model_name FROM compile_state WHERE question_uid = ? AND stage = 'S2'",
            (pending_new_uid,),
        ).fetchone()
        assert cs_compat["model_name"] == "subagent-unified"


def test_subagent_bridge_manifest_flat_schema_and_lock(tmp_path: Path) -> None:
    out_dir = tmp_path / "bridge_propose"
    batches = [
        {
            "batch_id": "propose_batch_0001",
            "messages": [{"role": "user", "content": "hi"}],
            "task_item_ids": ["propose_0001"],
        }
    ]
    task_map = {"grade_subject": "小学奥数"}

    manifest = export_task_batches(
        "propose",
        batches,
        task_map,
        out_dir,
        schema_cls=TaxonomyProposeOutput,
        subagent_model="gemini-4-neon-medium",
        prompt_version="s3a_v1",
        prompt_digest="abcd1234ef567890",
        prompt_profile="strict",
        max_parallel_workers=4,
    )
    assert manifest["status"] == "PENDING_IMPORT"
    assert manifest["dispatch_driver"] == "swarm"
    assert manifest["subagent_model"] == "gemini-4-neon-medium"
    assert manifest["prompt_profile"] == "strict"

    flat_schema = json.loads((out_dir / "flat_schema.json").read_text(encoding="utf-8"))
    _assert_no_refs(flat_schema)

    with pytest.raises(PreconditionError, match="PENDING_UNIMPORTED_BATCH"):
        export_task_batches(
            "propose",
            batches,
            task_map,
            out_dir,
            schema_cls=TaxonomyProposeOutput,
            overwrite_pending=False,
        )

    res_file = out_dir / "results.jsonl"
    valid_payload = {
        "batch_id": "propose_batch_0001",
        "result": {
            "modules": [
                {
                    "name": "计算与数感",
                    "aliases": [],
                    "l2_entities": [{"name": "加减法巧算", "aliases": [], "description": "整数巧算"}],
                }
            ]
        },
    }
    res_file.write_text(json.dumps(valid_payload, ensure_ascii=False) + "\n", encoding="utf-8")

    valid_by_batch, _, errs = load_and_validate_results(
        "propose",
        res_file,
        out_dir / "propose_task_map.json",
        TaxonomyProposeOutput,
    )
    assert not errs
    assert "propose_batch_0001" in valid_by_batch

    manifest_after = json.loads((out_dir / "propose_manifest.json").read_text(encoding="utf-8"))
    assert manifest_after["status"] == "IMPORTED"

    # IMPORTED 后允许再导出
    manifest_re = export_task_batches(
        "propose",
        batches,
        task_map,
        out_dir,
        schema_cls=TaxonomyProposeOutput,
    )
    assert manifest_re["status"] == "PENDING_IMPORT"


@pytest.mark.asyncio
async def test_stage3_export_passes_subagent_options(tmp_path: Path) -> None:
    cfg_dir = _setup_config_dir(tmp_path)
    db_path = tmp_path / "s3.db"
    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)
    _seed_question(conn, uid="primary_olympiad:ch01:q1", source_id="q1", text="算一算 1+2", content_hash="h1", verified=True)
    conn.execute(
        "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES ('primary_olympiad:ch01:q1::branch_1', 'primary_olympiad:ch01:q1', 'branch_1', 1, 'PRIMARY_VLM', '3')"
    )
    conn.execute(
        "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, raw_l1, raw_l2, raw_l3) VALUES ('s1', 'primary_olympiad:ch01:q1', 'branch_1', 1, '求和', '计算与数感', '加减法巧算', '整数加法求和')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO compile_state(question_uid, stage, status) VALUES ('primary_olympiad:ch01:q1', 'S2', 'OK')"
    )
    conn.commit()

    propose_dir = tmp_path / "propose_out"
    p_res = await propose_taxonomy_async(
        conn,
        Settings(),
        grade_subject="小学奥数",
        mode="export",
        tasks_dir=propose_dir,
        config_dir=cfg_dir,
        use_mock=True,
        n_clusters=1,
        subagent_model="gemini-4-neon-high",
    )
    assert p_res["manifest"]["subagent_model"] == "gemini-4-neon-high"
    assert (propose_dir / "flat_schema.json").exists()

    # 造 DRAFT 词表并测 stage3c export 透传
    tax_file = tmp_path / "draft.yaml"
    tax_file.write_text(
        "schema_version: 1\ngrade_subject: 小学奥数\nslug: primary_olympiad\nstatus: DRAFT\nversion: draft-1\n"
        "modules:\n  - name: 平面几何\n    aliases: []\n    l2_entities:\n      - name: 等高模型\n        aliases: []\n        description: 等高\n",
        encoding="utf-8",
    )
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status='DRAFT', taxonomy_version='draft-1', taxonomy_file=? WHERE grade_subject='小学奥数'",
        (str(tax_file),),
    )
    conn.commit()

    c_dir = tmp_path / "3c_out"
    c_res = await run_stage3c_async(
        conn,
        Settings(),
        grade_subject="小学奥数",
        mode="export",
        tasks_dir=c_dir,
        lock_path=tmp_path / ".lock",
        config_dir=cfg_dir,
        subagent_model="gemini-4-neon-medium",
    )
    assert c_res["manifest"]["subagent_model"] == "gemini-4-neon-medium"
    conn.close()


def test_cli_atomic_compiler_commands_and_dry_run(tmp_path: Path) -> None:
    cfg_dir = _setup_config_dir(tmp_path)
    runner = CliRunner()

    # 1. prompt list / lint / render
    r_list = runner.invoke(app, ["prompt", "list", "--config-dir", str(cfg_dir), "--json"])
    assert r_list.exit_code == 0, r_list.output
    d_list = json.loads(r_list.stdout)
    assert d_list["ok"] is True
    assert d_list["data"]["default_exists"] is True

    r_lint = runner.invoke(app, ["prompt", "lint", "--config-dir", str(cfg_dir), "--json"])
    assert r_lint.exit_code == 0, r_lint.output
    d_lint = json.loads(r_lint.stdout)
    assert d_lint["data"]["ok"] is True

    r_render = runner.invoke(
        app,
        ["prompt", "render", "--stage", "unified", "--grade-subject", "小学奥数", "--config-dir", str(cfg_dir), "--json"],
    )
    assert r_render.exit_code == 0, r_render.output
    d_render = json.loads(r_render.stdout)
    assert d_render["data"]["prompt_version"] == "unified-v1.0"
    assert "flattened_schema" in d_render["data"]
    _assert_no_refs(d_render["data"]["flattened_schema"])

    # 2. tool verify-sympy & validate-draft (--input @file 与 --stdin)
    sym_payload = {
        "sympy_code": "def verify():\n    return {'主问': str(12 + 30)}\n",
        "final_answers": [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "42",
                "sympy_expr": "42",
            }
        ],
    }
    sym_file = tmp_path / "sym.json"
    sym_file.write_text(json.dumps(sym_payload, ensure_ascii=False), encoding="utf-8")

    r_sym = runner.invoke(
        app,
        ["tool", "verify-sympy", "--input", f"@{sym_file}", "--config-dir", str(cfg_dir), "--json"],
    )
    assert r_sym.exit_code == 0, r_sym.output
    assert json.loads(r_sym.stdout)["data"]["ok"] is True

    draft_payload = {
        "mode": "UNIFIED_S1_S2",
        "question_text": "计算 12 + 30 等于多少？",
        "grade_subject": "小学奥数",
        "candidate": _sample_unified_candidate("T0001"),
    }
    r_draft = runner.invoke(
        app,
        ["tool", "validate-draft", "--stdin", "--config-dir", str(cfg_dir), "--json"],
        input=json.dumps(draft_payload, ensure_ascii=False),
    )
    assert r_draft.exit_code == 0, r_draft.output
    d_draft = json.loads(r_draft.stdout)
    assert d_draft["data"]["ok"] is True
    assert d_draft["data"]["normalized_preview"]["base_elo"] > 0

    # 3. diagnose-preflight (源文件只读，修复 ##1. 漏空格与图片行)
    src_md = tmp_path / "raw_list.md"
    orig_md = "##1.第一题\n---\n图片: figs/q1.png\n"
    src_md.write_text(orig_md, encoding="utf-8")
    norm_md = tmp_path / "norm.md"
    r_pre = runner.invoke(
        app,
        ["diagnose-preflight", "--file", str(src_md), "--out-normalized", str(norm_md), "--json"],
    )
    assert r_pre.exit_code == 0, r_pre.output
    d_pre = json.loads(r_pre.stdout)
    assert d_pre["data"]["total_questions"] == 1
    assert src_md.read_text(encoding="utf-8") == orig_md
    assert norm_md.exists()

    # 4. ingest --dry-run (零写库) 与 status (空库/未建库)
    raw_root = tmp_path / "题库"
    col_dir = raw_root / "ch01_test"
    col_dir.mkdir(parents=True)
    (col_dir / "problems.jsonl").write_text(
        json.dumps({"id": "q_dry_1", "question": "测试 dry-run 题目：1+1=?"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    db_missing = tmp_path / "nonexistent.db"
    r_ing_dry = runner.invoke(
        app,
        [
            "ingest",
            "--root",
            str(raw_root),
            "--grade-subject",
            "小学奥数",
            "--db",
            str(db_missing),
            "--config-dir",
            str(cfg_dir),
            "--dry-run",
            "--json",
        ],
    )
    assert r_ing_dry.exit_code == 0, r_ing_dry.output
    d_ing = json.loads(r_ing_dry.stdout)
    assert d_ing["data"]["dry_run"] is True
    assert d_ing["data"]["total_scanned"] == 1
    assert not db_missing.exists()

    r_status = runner.invoke(
        app,
        [
            "status",
            "--db",
            str(db_missing),
            "--config-dir",
            str(cfg_dir),
            "--vectors-dir",
            str(tmp_path / "vecs"),
            "--unified-dir",
            str(tmp_path / "uni"),
            "--json",
        ],
    )
    assert r_status.exit_code == 0, r_status.output
    d_status = json.loads(r_status.stdout)
    assert d_status["data"]["db_exists"] is False
    assert d_status["data"]["subagent_routing"]["default_model"] == "gemini-4-neon-medium"


def test_mcp_roles_and_worker_physical_isolation(tmp_path: Path) -> None:
    cfg_dir = _setup_config_dir(tmp_path)
    db_path = tmp_path / "mcp_practice.db"

    # 1. 验证角色注册表精确分离
    worker_tools = [t["name"] for t in get_tools_for_role("worker")]
    compiler_tools = [t["name"] for t in get_tools_for_role("compiler")]
    practice_tools = [t["name"] for t in get_tools_for_role("practice")]
    all_tools = [t["name"] for t in get_tools_for_role("all")]

    assert worker_tools == ["verify_sympy", "validate_draft"]
    assert compiler_tools == [
        "verify_sympy",
        "validate_draft",
        "prompt_list",
        "prompt_render",
        "prompt_lint",
        "pipeline_status",
        "diagnose_preflight",
        "ingest_dry_run",
        "doctor",
        "anchors_sample",
    ]
    assert practice_tools == [
        "learner_list",
        "profile",
        "taxonomy",
        "search",
        "related",
        "detail",
        "check",
        "compose_quiz",
    ]
    assert len(all_tools) == len(compiler_tools) + len(practice_tools)

    # 2. 内存流 stdio JSON-RPC：initialize / notifications/initialized / ping /越权拦截
    assert (
        dispatch_jsonrpc_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            role="worker",
            config_dir=cfg_dir,
        )
        is None
    )
    unauth = dispatch_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "pipeline_status", "arguments": {}}},
        role="worker",
        config_dir=cfg_dir,
    )
    assert unauth is not None and "error" in unauth

    # 3. practice 角色防剧透验证（默认 reveal="quiz" 不泄露答案）
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_question(
            conn,
            uid="primary_olympiad:ch01:q_prac",
            source_id="q_prac",
            text="求 21 + 21 的值。",
            content_hash="h_prac",
            verified=True,
        )
        conn.execute("UPDATE questions SET m_c_score=1, m_s_score=1, m_t_score=1, m_l_score=1 WHERE question_uid='primary_olympiad:ch01:q_prac'")
        effective.recompute(conn, "primary_olympiad:ch01:q_prac")
        conn.commit()

    det_resp = dispatch_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "detail", "arguments": {"uid": "q_prac"}},
        },
        role="practice",
        db_path=db_path,
        config_dir=cfg_dir,
    )
    assert det_resp is not None and det_resp["result"]["isError"] is False
    det_data = det_resp["result"]["structuredContent"]
    assert "final_answers" not in det_data
    assert "solutions" not in det_data

    # 4. 子进程真实 stdio 启动 `--role worker`：验证工具列表、SymPy/草稿调用与 sqlite3 零加载铁律
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("src").resolve())

    # 4a. 直接通过 `python -m shuati.mcp.server --role worker` 跑真实 stdio 管道
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "validate_draft",
                "arguments": {
                    "mode": "UNIFIED_S1_S2",
                    "question_text": "计算 12 + 30 等于多少？",
                    "candidate": _sample_unified_candidate("T0001"),
                    "run_sympy": True,
                },
            },
        },
    ]
    stdin_blob = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "shuati.mcp.server", "--role", "worker", "--config-dir", str(cfg_dir)],
        input=stdin_blob,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        check=True,
    )
    out_lines = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
    assert [m["id"] for m in out_lines] == [1, 2, 3, 4]
    assert [t["name"] for t in out_lines[2]["result"]["tools"]] == ["verify_sympy", "validate_draft"]
    assert out_lines[3]["result"]["isError"] is False
    assert out_lines[3]["result"]["structuredContent"]["ok"] is True

    # 4b. 子进程断言：启动 worker MCP 并执行 tools/list + verify_sympy + validate_draft 后，
    #     sys.modules 中绝对不存在 sqlite3 / shuati.db / shuati.services.facade
    probe_script = (
        "import io, json, sys\n"
        "from pathlib import Path\n"
        "from shuati.mcp.server import serve_stdio\n"
        f"cfg = Path({str(cfg_dir)!r})\n"
        "req = [\n"
        "  {'jsonrpc':'2.0','id':1,'method':'tools/list'},\n"
        "  {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'verify_sympy','arguments':{"
        "'sympy_code':'def verify():\\n    return {\"主问\": str(6*7)}\\n',"
        "'final_answers':[{'label':'主问','answer_type':'NUMERIC','verify_mode':'SYMBOLIC','collection_mode':'SINGLE','answer':'42','sympy_expr':'42'}]"
        "}}},\n"
        "]\n"
        "buf_in = io.StringIO('\\n'.join(json.dumps(x, ensure_ascii=False) for x in req) + '\\n')\n"
        "buf_out = io.StringIO()\n"
        "serve_stdio(role='worker', config_dir=cfg, stdin=buf_in, stdout=buf_out)\n"
        "assert 'sqlite3' not in sys.modules, 'sqlite3 loaded in worker!'\n"
        "assert 'shuati.db' not in sys.modules, 'shuati.db loaded in worker!'\n"
        "assert 'shuati.services.facade' not in sys.modules, 'facade loaded in worker!'\n"
        "resps = [json.loads(l) for l in buf_out.getvalue().splitlines() if l.strip()]\n"
        "assert [t['name'] for t in resps[0]['result']['tools']] == ['verify_sympy', 'validate_draft']\n"
        "assert resps[1]['result']['structuredContent']['ok'] is True\n"
        "print('WORKER_ISOLATION_OK')\n"
    )
    probe_proc = subprocess.run(
        [sys.executable, "-c", probe_script],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        check=True,
    )
    assert "WORKER_ISOLATION_OK" in probe_proc.stdout

    # 5. serve_stdio 内存环回验证 compiler 角色
    c_in = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "prompt_lint", "arguments": {}}})
        + "\n"
    )
    c_out = io.StringIO()
    assert serve_stdio(role="compiler", config_dir=cfg_dir, stdin=c_in, stdout=c_out) == 0
    c_resp = json.loads(c_out.getvalue().strip())
    assert c_resp["result"]["structuredContent"]["ok"] is True
