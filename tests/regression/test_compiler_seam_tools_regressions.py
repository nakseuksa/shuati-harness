"""Wave 2 红军审查 Repro 回归测试集（防退化永久资产）。

吸收自：
1. tests/repro/repro_seam_bridge_empty_results_marks_imported.py
2. tests/repro/repro_seam_unified_partial_import_marks_imported.py
3. tests/repro/repro_seam_mcp_malformed_input_crashes_server.py
4. tests/repro/repro_seam_corrupt_manifest_lock_bypass.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from shuati.core import effective
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.models.schemas import TaxonomyProposeOutput
from shuati.pipeline.subagent_bridge import export_task_batches, load_and_validate_results
from shuati.pipeline.unified_import import (
    export_unified_tasks,
    import_unified_results,
    register_anchors_from_yaml,
)


def _setup_cfg(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "config"
    shutil.copytree(Path("config"), cfg_dir)
    settings_txt = (cfg_dir / "settings.yaml").read_text(encoding="utf-8")
    settings_txt = settings_txt.replace('dir: "human/"', f'dir: "{(tmp_path / "human").as_posix()}"')
    (cfg_dir / "settings.yaml").write_text(settings_txt, encoding="utf-8")
    return cfg_dir


def _seed_q(
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
        ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch_secret', 'ch_secret/p.jsonl', ?, ?, '[]', '[]', '[]', ?)
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


def _sample_candidate(task_id: str) -> dict[str, Any]:
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


def test_bridge_empty_and_partial_results_do_not_mark_imported(tmp_path: Path) -> None:
    d = tmp_path / "bridge"
    batches = [
        {"batch_id": "b1", "messages": [{"role": "user", "content": "hello"}], "task_item_ids": ["t1"]},
        {"batch_id": "b2", "messages": [{"role": "user", "content": "world"}], "task_item_ids": ["t2"]},
    ]
    task_map = {"b1": {"x": 1}, "b2": {"x": 2}}
    export_task_batches("stage3a", batches, task_map, d, schema_cls=TaxonomyProposeOutput)

    mf = d / "stage3a_manifest.json"
    assert json.loads(mf.read_text(encoding="utf-8"))["status"] == "PENDING_IMPORT"

    # 1. 空结果文件不能置 IMPORTED，且继续阻止重导出
    res_empty = tmp_path / "empty.jsonl"
    res_empty.write_text("", encoding="utf-8")
    valid0, _, errs0 = load_and_validate_results(
        "stage3a", res_empty, d / "stage3a_task_map.json", TaxonomyProposeOutput
    )
    assert valid0 == {} and errs0 == []
    assert json.loads(mf.read_text(encoding="utf-8"))["status"] == "PENDING_IMPORT"

    with pytest.raises(PreconditionError, match="PENDING_UNIMPORTED_BATCH"):
        export_task_batches("stage3a", batches, task_map, d, schema_cls=TaxonomyProposeOutput)

    # 2. 仅回灌 b1 (1/2 批) 仍应保持 PENDING_IMPORT；补齐 b2 后才流转 IMPORTED
    mod_payload = {
        "modules": [
            {
                "name": "计算与数感",
                "aliases": [],
                "l2_entities": [{"name": "加减法巧算", "aliases": [], "description": "整数巧算"}],
            }
        ]
    }
    res_b1 = tmp_path / "b1.jsonl"
    res_b1.write_text(json.dumps({"batch_id": "b1", "result": mod_payload}, ensure_ascii=False) + "\n", encoding="utf-8")
    load_and_validate_results("stage3a", res_b1, d / "stage3a_task_map.json", TaxonomyProposeOutput)
    assert json.loads(mf.read_text(encoding="utf-8"))["status"] == "PENDING_IMPORT"

    res_b2 = tmp_path / "b2.jsonl"
    res_b2.write_text(json.dumps({"batch_id": "b2", "result": mod_payload}, ensure_ascii=False) + "\n", encoding="utf-8")
    load_and_validate_results("stage3a", res_b2, d / "stage3a_task_map.json", TaxonomyProposeOutput)
    assert json.loads(mf.read_text(encoding="utf-8"))["status"] == "IMPORTED"


def test_unified_partial_import_keeps_pending_until_all_tasks_resolved(tmp_path: Path) -> None:
    cfg_dir = _setup_cfg(tmp_path)
    db_path = tmp_path / "t.db"
    anchors = [f"primary_olympiad:ch_t:anc_{i}" for i in range(1, 5)]
    qa, qb = "primary_olympiad:ch_t:qa", "primary_olympiad:ch_t:qb"

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        for i, uid in enumerate(anchors, 1):
            _seed_q(conn, uid=uid, source_id=f"anc_{i}", text=f"锚点 {i}", content_hash=f"ha{i}", verified=True)
            conn.execute(
                "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin,"
                " method_summary, solution_markdown, sympy_verified) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '直算', '解析', 1)",
                (f"{uid}::branch_1", uid),
            )
        _seed_q(conn, uid=qa, source_id="qa", text="计算 12 + 30 等于多少？", content_hash="hqa")
        _seed_q(conn, uid=qb, source_id="qb", text="计算 12 + 30 等于多少？", content_hash="hqb")
        conn.commit()

    anchors_path = cfg_dir / "anchors" / "anchors_primary_olympiad.yaml"
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    tiers = [
        ("L1_基础直达", 1, 1, 1, 1, 1120),
        ("L3_综合中档", 2, 2, 2, 1, 1510),
        ("L4-A_进阶拔高(繁)", 2, 3, 2, 3, 1745),
        ("L5-2_竞赛超难", 4, 2, 2, 1, 2530),
    ]
    lines = ["grade_subject: 小学奥数", 'version: "1.0"', "anchors:"]
    for uid, (tier, c, s, t, l, elo) in zip(anchors, tiers, strict=True):
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
    register_anchors_from_yaml(db_path=db_path, config_dir=cfg_dir, anchors_file=anchors_path)

    out = tmp_path / "unified"
    exp = export_unified_tasks(db_path=db_path, out_dir=out, config_dir=cfg_dir, only_uids=[qa, qb])
    assert exp.exported_unified == 2

    # Worker B 先写盘未导入，仅导入 Worker A 的 T0001
    res_b = out / "results_b.jsonl"
    res_b.write_text(json.dumps(_sample_candidate("T0002"), ensure_ascii=False) + "\n", encoding="utf-8")
    res_a = out / "results_a.jsonl"
    res_a.write_text(json.dumps(_sample_candidate("T0001"), ensure_ascii=False) + "\n", encoding="utf-8")

    imp_a = import_unified_results(
        db_path=db_path, results_file=res_a, task_map_file=out / "task_map.json", config_dir=cfg_dir
    )
    assert imp_a.ingested_verified == 1
    mf_a = json.loads((out / "unified_manifest.json").read_text(encoding="utf-8"))
    assert mf_a["status"] == "PENDING_IMPORT"
    assert mf_a["imported_task_ids"] == ["T0001"]

    with pytest.raises(PreconditionError, match="PENDING_UNIMPORTED_BATCH"):
        export_unified_tasks(db_path=db_path, out_dir=out, config_dir=cfg_dir, only_uids=[qa, qb])

    # 再导入 Worker B 后全部完成 -> IMPORTED
    imp_b = import_unified_results(
        db_path=db_path, results_file=res_b, task_map_file=out / "task_map.json", config_dir=cfg_dir
    )
    assert imp_b.ingested_verified == 1
    mf_b = json.loads((out / "unified_manifest.json").read_text(encoding="utf-8"))
    assert mf_b["status"] == "IMPORTED"
    assert sorted(mf_b["imported_task_ids"]) == ["T0001", "T0002"]


def test_mcp_survives_deep_nesting_and_invalid_utf8(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
    ping = (json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n").encode("utf-8")

    deep = ("[" * 100000 + "]" * 100000 + "\n").encode("utf-8")
    p1 = subprocess.run(
        [sys.executable, "-m", "shuati.mcp.server", "--role", "worker"],
        input=deep + ping,
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert p1.returncode == 0
    lines1 = [json.loads(x) for x in p1.stdout.decode("utf-8").splitlines() if x.strip()]
    assert any(m.get("error", {}).get("code") == -32700 for m in lines1)
    assert any(m.get("id") == 7 and m.get("result") == {} for m in lines1)

    bad_utf8 = b"\xff\xfe\x80 not-utf8\n"
    p2 = subprocess.run(
        [sys.executable, "-m", "shuati.mcp.server", "--role", "worker"],
        input=bad_utf8 + ping,
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert p2.returncode == 0
    lines2 = [json.loads(x) for x in p2.stdout.decode("utf-8").splitlines() if x.strip()]
    assert any(m.get("error", {}).get("code") == -32700 for m in lines2)
    assert any(m.get("id") == 7 and m.get("result") == {} for m in lines2)


def test_corrupt_manifest_or_task_map_fail_closed(tmp_path: Path) -> None:
    valid_tm = json.dumps({f"T{i:04d}": {"question_uid": f"u{i}"} for i in range(1, 4)})
    cases = [
        ("truncated_manifest", "", valid_tm),
        ("bad_json_manifest", "{not json", valid_tm),
        ("corrupt_task_map", json.dumps({"status": "PENDING_IMPORT"}), "{bad"),
        ("corrupt_imported_manifest", json.dumps({"status": "IMPORTED"}), "{bad"),
    ]
    for idx, (_name, mf, tm) in enumerate(cases):
        d = tmp_path / f"unified_corrupt_{idx}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "unified_manifest.json").write_text(mf, encoding="utf-8")
        (d / "task_map.json").write_text(tm, encoding="utf-8")
        with pytest.raises(PreconditionError, match="PENDING_UNIMPORTED_BATCH"):
            export_unified_tasks(db_path=d / "nope.db", out_dir=d, config_dir=Path("config"))

    # subagent_bridge 同样 fail-closed
    bd = tmp_path / "bridge_corrupt"
    bd.mkdir(parents=True, exist_ok=True)
    (bd / "stage3a_manifest.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(PreconditionError, match="PENDING_UNIMPORTED_BATCH"):
        export_task_batches(
            "stage3a",
            [{"batch_id": "b1", "messages": [], "task_item_ids": ["t1"]}],
            {"b1": 1},
            bd,
            schema_cls=TaxonomyProposeOutput,
        )
