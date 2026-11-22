"""主会话 Subagent 合一初始化编译器（Stage 1.5 + Stage 1+2 Unified）端到端测试。"""

from __future__ import annotations

import json
import shutil
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from shuati.core import effective
from shuati.db import human_mirror
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.pipeline.stage1_solve import run_stage1_compile
from shuati.pipeline.unified_import import (
    SUBAGENT_MODEL_NAME,
    export_unified_tasks,
    import_unified_results,
    register_anchors_from_yaml,
)


def _setup_config_dir(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "config"
    shutil.copytree(Path("config"), cfg_dir)
    # 指向临时 human/ 目录，避免污染仓库
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
    fmt: str = "解答题",
    options: dict[str, str] | None = None,
    verified: bool = False,
    excluded: bool = False,
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
        ) VALUES (?, '小学奥数', '[]', '[]', 0, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            text,
            json.dumps(options or {}, ensure_ascii=False),
            fmt,
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
    if excluded:
        conn.execute(
            """
            INSERT INTO human_overrides(
                question_uid, field_name, override_value, reason, source, base_fingerprint, is_active
            ) VALUES (?, 'is_excluded', '1', 'excluded test', 'REVIEW_UI', ?, 1)
            """,
            (uid, content_hash),
        )
    effective.recompute(conn, uid)


def _write_test_anchors(cfg_dir: Path, anchor_uids: list[str]) -> Path:
    anchors_path = cfg_dir / "anchors" / "anchors_primary_olympiad.yaml"
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    tiers = [
        ("L1_基础直达", 1, 1, 1, 1, 1120),
        ("L3_综合中档", 2, 2, 2, 1, 1510),
        ("L4-A_进阶拔高(繁)", 2, 3, 2, 3, 1745),
        ("L5-2_竞赛超难", 4, 2, 2, 1, 2530),
    ]
    lines = ['grade_subject: 小学奥数', 'version: "1.0"', "anchors:"]
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


def _base_diff() -> dict[str, Any]:
    return {
        "C_concept_count": {"evidence": "用到 2 个核心概念", "score": 2},
        "S_chain_length": {"evidence": "共 2 步推演", "score": 2},
        "T_trickiness": {"evidence": "需要构造辅助转化", "score": 2},
        "L_computation_load": {"evidence": "整数运算简洁", "score": 1},
    }


def _base_steps() -> list[dict[str, Any]]:
    return [
        {
            "step_index": 1,
            "sub_goal": "建立核心等量关系",
            "core_formula": "a + b = c",
            "hint": "先观察已知量之间的和差关系",
            "raw_l1": "计算与数感",
            "raw_l2": "加减法巧算",
            "raw_l3": "整数加减凑整转化",
        }
    ]


@pytest.mark.asyncio
async def test_unified_pipeline_end_to_end_and_lock(tmp_path: Path) -> None:
    cfg_dir = _setup_config_dir(tmp_path)
    db_path = tmp_path / "test.db"

    anchor_uids = [f"primary_olympiad:ch_test:anc_{i}" for i in range(1, 5)]
    pending_sym_uid = "primary_olympiad:ch_test:q_sym"
    pending_choice_uid = "primary_olympiad:ch_test:q_choice"
    pending_ambig_uid = "primary_olympiad:ch_test:q_ambig"
    pending_qual_uid = "primary_olympiad:ch_test:q_qual"
    excluded_uid = "primary_olympiad:ch_test:q_excl"

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        for idx, uid in enumerate(anchor_uids, 1):
            _seed_question(
                conn,
                uid=uid,
                source_id=f"anc_{idx}",
                text=f"已知数字 678，求基础结果 {idx}",
                content_hash=f"hash_anc_{idx}",
                verified=True,
            )
            conn.execute(
                """
                INSERT INTO question_solutions(
                    solution_id, question_uid, branch_id, is_optimal, source_origin,
                    method_summary, solution_markdown, sympy_verified
                ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '直接求解', '详细解析过程', 1)
                """,
                (f"{uid}::branch_1", uid),
            )
        _seed_question(
            conn,
            uid=pending_sym_uid,
            source_id="q_sym",
            text="计算 356 + 187 等于多少？",
            content_hash="hash_sym",
        )
        _seed_question(
            conn,
            uid=pending_choice_uid,
            source_id="q_choice",
            text="下列哪个选项正确？\nA. 10\nB. 20\nC. 30\nD. 40",
            content_hash="hash_choice",
            fmt="选择题",
            options={"A": "10", "B": "20", "C": "30", "D": "40"},
        )
        _seed_question(
            conn,
            uid=pending_ambig_uid,
            source_id="q_ambig",
            text="如图，求角度之和。",
            content_hash="hash_ambig",
        )
        _seed_question(
            conn,
            uid=pending_qual_uid,
            source_id="q_qual",
            text="请画出满足条件的辅助线并说明理由。",
            content_hash="hash_qual",
        )
        _seed_question(
            conn,
            uid=excluded_uid,
            source_id="q_excl",
            text="此题已作废。",
            content_hash="hash_excl",
            excluded=True,
        )
        conn.commit()

    anchors_yaml = _write_test_anchors(cfg_dir, anchor_uids)
    # 1. Stage 1.5 黄金锚点标定前导出应直接拒绝
    with pytest.raises(Exception, match="PRECONDITION_FAILED"):
        export_unified_tasks(db_path=db_path, out_dir=tmp_path / "out", config_dir=cfg_dir)

    cal_res = register_anchors_from_yaml(
        db_path=db_path,
        config_dir=cfg_dir,
        anchors_file=anchors_yaml,
    )
    assert cal_res.anchor_count == 4

    with closing(get_connection(db_path, readonly=True)) as conn:
        gs = conn.execute("SELECT anchors_ready, anchors_version FROM grade_subjects WHERE grade_subject='小学奥数'").fetchone()
        assert gs["anchors_ready"] == 1
        assert gs["anchors_version"] == "1.0"
        snap = human_mirror.verify_snapshot(conn, human_dir=tmp_path / "human")
        assert snap["is_consistent"] is True

    # 2. 导出脱敏任务，验证零泄漏与批量切片
    out_dir = tmp_path / "unified_out"
    exp = export_unified_tasks(
        db_path=db_path,
        out_dir=out_dir,
        config_dir=cfg_dir,
        batch_size=3,
    )
    assert exp.exported_stage2_only == 4
    assert exp.exported_unified == 4
    assert len(exp.batch_files) == 3

    tasks_text = (out_dir / "tasks.jsonl").read_text(encoding="utf-8")
    anchors_text = (out_dir / "anchors_prompt.txt").read_text(encoding="utf-8")
    for forbidden in ("primary_olympiad:", "ch_secret_collection", "secret_legacy_tag", "anc_1", "q_sym"):
        assert forbidden not in tasks_text
        assert forbidden not in anchors_text

    task_map = json.loads((out_dir / "task_map.json").read_text(encoding="utf-8"))
    uid_to_tid = {v["question_uid"]: k for k, v in task_map.items()}

    # 3. 第一轮回灌：构造 1 条成功 STAGE2_ONLY、1 条失败 STAGE2_ONLY（raw_l3 含数字）、
    #    1 条成功 SYMBOLIC、1 条失败 SYMBOLIC（SymPy 错）、1 条 CHOICE EXACT、
    #    1 条 VISUAL_AMBIGUITY、1 条 QUALITATIVE
    results_round1 = [
        {
            "task_id": uid_to_tid[anchor_uids[0]],
            "steps": _base_steps(),
            "abstract_skeleton": "基础数字等量关系直达模型",
            "primary_l1_module": "计算与数感",
            "secondary_l1_modules": [],
            "thinking_methods": ["倒推转化法"],
            "classic_motifs": ["和差基本模型"],
            "trap_tags": ["TRAP_ARITHMETIC_SLIP"],
            "difficulty": _base_diff(),
        },
        {
            # raw_l3 含数字 -> 应打回 retry
            "task_id": uid_to_tid[anchor_uids[1]],
            "steps": [
                {
                    "step_index": 1,
                    "sub_goal": "拆分步骤",
                    "core_formula": "x = 1",
                    "hint": "思考整数拆分",
                    "raw_l1": "计算与数感",
                    "raw_l2": "加减法巧算",
                    "raw_l3": "算出678之和",
                }
            ],
            "abstract_skeleton": "基础加法恒等模型",
            "primary_l1_module": "计算与数感",
            "secondary_l1_modules": [],
            "thinking_methods": ["凑整法"],
            "classic_motifs": [],
            "trap_tags": [],
            "difficulty": _base_diff(),
        },
        {
            "task_id": uid_to_tid[pending_sym_uid],
            "visual_facts": "",
            "visual_ambiguity_flag": False,
            "visual_ambiguity_note": "",
            "method_summary": "凑整相加",
            "solution_markdown": "356 + 187 = 543",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "543",
                    "sympy_expr": "543",
                }
            ],
            "sympy_code": "def verify():\n    a = 356\n    b = 187\n    return {'主问': str(a + b)}\n",
            "steps": _base_steps(),
            "abstract_skeleton": "三位数进位加法模型",
            "primary_l1_module": "计算与数感",
            "secondary_l1_modules": [],
            "thinking_methods": ["位值拆分法"],
            "classic_motifs": ["凑整运算模型"],
            "trap_tags": ["TRAP_ARITHMETIC_SLIP"],
            "difficulty": _base_diff(),
        },
        {
            "task_id": uid_to_tid[pending_choice_uid],
            "visual_facts": "",
            "visual_ambiguity_flag": False,
            "visual_ambiguity_note": "",
            "method_summary": "直接选 B",
            "solution_markdown": "20 正确，故选 B",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "CHOICE",
                    "verify_mode": "EXACT",
                    "collection_mode": "SINGLE",
                    "answer": "B",
                    "choice_label": "B",
                    "sympy_expr": "20",
                }
            ],
            "sympy_code": None,
            "steps": _base_steps(),
            "abstract_skeleton": "数值筛选模型",
            "primary_l1_module": "计算与数感",
            "secondary_l1_modules": [],
            "thinking_methods": ["代入检验法"],
            "classic_motifs": [],
            "trap_tags": [],
            "difficulty": _base_diff(),
        },
        {
            "task_id": uid_to_tid[pending_ambig_uid],
            "visual_facts": "图中有两个角标注模糊",
            "visual_ambiguity_flag": True,
            "visual_ambiguity_note": "角标不清晰",
            "method_summary": "估算角度",
            "solution_markdown": "总角度为 90 度",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "90",
                    "sympy_expr": "90",
                }
            ],
            "sympy_code": "def verify():\n    x = 45 + 45\n    return {'主问': str(x)}\n",
            "steps": _base_steps(),
            "abstract_skeleton": "平面多角求和模型",
            "primary_l1_module": "平面几何",
            "secondary_l1_modules": [],
            "thinking_methods": ["局部和差法"],
            "classic_motifs": [],
            "trap_tags": [],
            "difficulty": _base_diff(),
        },
        {
            "task_id": uid_to_tid[pending_qual_uid],
            "visual_facts": "",
            "visual_ambiguity_flag": False,
            "visual_ambiguity_note": "",
            "method_summary": "过顶点作平行线",
            "solution_markdown": "过点 A 作底边平行线即可证明。",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "PROOF",
                    "verify_mode": "QUALITATIVE",
                    "collection_mode": "SINGLE",
                    "answer": "过顶点作平行线得等角",
                }
            ],
            "sympy_code": None,
            "steps": _base_steps(),
            "abstract_skeleton": "辅助线平行构造证明模型",
            "primary_l1_module": "平面几何",
            "secondary_l1_modules": [],
            "thinking_methods": ["平行线转移角法"],
            "classic_motifs": ["一线三等角模型"],
            "trap_tags": [],
            "difficulty": _base_diff(),
        },
    ]
    r1_path = tmp_path / "results_r1.jsonl"
    r1_path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in results_round1), encoding="utf-8")

    imp1 = import_unified_results(
        db_path=db_path,
        results_file=r1_path,
        task_map_file=out_dir / "task_map.json",
        config_dir=cfg_dir,
    )
    assert imp1.ingested_stage2_only == 1
    assert imp1.ingested_verified == 2  # q_sym + q_choice 均无 visual_ambiguity，直接放行 VERIFIED_CONSENSUS
    assert imp1.ingested_needs_review == 2  # q_ambig + q_qual
    assert imp1.retry_count == 1  # anc_2 raw_l3 含数字被打回

    retry_lines = [json.loads(l) for l in Path(imp1.retry_file or "").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(retry_lines) == 1
    assert retry_lines[0]["task_id"] == uid_to_tid[anchor_uids[1]]
    assert any("RAW_L3_CONTAINS_DIGIT" in e for e in retry_lines[0]["validation_errors"])

    # 验证 NEEDS_REVIEW 不阻断 Stage 2（S2='OK'，eff_is_servable=0，steps 与 concepts 已入库）
    with closing(get_connection(db_path, readonly=True)) as conn:
        q_ambig = conn.execute(
            "SELECT eff_verification_status, eff_is_servable, review_reason, eff_primary_l1_module FROM questions WHERE question_uid = ?",
            (pending_ambig_uid,),
        ).fetchone()
        assert q_ambig["eff_verification_status"] == "NEEDS_REVIEW"
        assert q_ambig["eff_is_servable"] == 0
        assert "VISUAL_AMBIGUITY" in q_ambig["review_reason"]
        assert q_ambig["eff_primary_l1_module"] == "平面几何"
        cs_ambig_s2 = conn.execute(
            "SELECT status, model_name FROM compile_state WHERE question_uid = ? AND stage = 'S2'",
            (pending_ambig_uid,),
        ).fetchone()
        assert cs_ambig_s2["status"] == "OK"
        assert cs_ambig_s2["model_name"] == SUBAGENT_MODEL_NAME

        q_qual = conn.execute(
            "SELECT eff_verification_status, eff_is_servable, review_reason FROM questions WHERE question_uid = ?",
            (pending_qual_uid,),
        ).fetchone()
        assert q_qual["eff_verification_status"] == "NEEDS_REVIEW"
        assert q_qual["eff_is_servable"] == 0
        assert "SINGLE_RUN_ONLY" in q_qual["review_reason"]

        q_sym = conn.execute(
            "SELECT eff_verification_status, eff_is_servable, eff_base_elo FROM questions WHERE question_uid = ?",
            (pending_sym_uid,),
        ).fetchone()
        assert q_sym["eff_verification_status"] == "VERIFIED_CONSENSUS"
        assert q_sym["eff_is_servable"] == 1
        assert q_sym["eff_base_elo"] > 0

        steps_cnt = conn.execute("SELECT COUNT(*) FROM question_steps WHERE question_uid = ?", (pending_sym_uid,)).fetchone()[0]
        assert steps_cnt == 1

    # 4. 验证 anti-spoiler 与黑名单在校验中被拦截
    bad_spoiler_rec = dict(results_round1[0])
    bad_spoiler_rec["task_id"] = uid_to_tid[anchor_uids[2]]
    bad_spoiler_rec["steps"] = [
        {
            "step_index": 1,
            "sub_goal": "直接写出答案",
            "core_formula": "x = 42",
            "hint": "直接解得结果 42",
            "raw_l1": "计算与数感",
            "raw_l2": "加减法巧算",
            "raw_l3": "整数和差性质",
        }
    ]
    bad_spoiler_rec["thinking_methods"] = ["转化与化归"]
    bad_spoiler_rec["trap_tags"] = ["TRAP_NONEXISTENT"]
    bad_spoiler_rec["abstract_skeleton"] = "关于 678 的加法模型"
    bad_path = tmp_path / "bad.jsonl"
    bad_path.write_text(json.dumps(bad_spoiler_rec, ensure_ascii=False), encoding="utf-8")
    imp_bad = import_unified_results(
        db_path=db_path,
        results_file=bad_path,
        task_map_file=out_dir / "task_map.json",
        config_dir=cfg_dir,
        retry_out=tmp_path / "retry_bad.jsonl",
    )
    assert imp_bad.retry_count == 1
    bad_retry = json.loads((tmp_path / "retry_bad.jsonl").read_text(encoding="utf-8").splitlines()[0])
    err_blob = "\n".join(bad_retry["validation_errors"])
    assert "HINT_SPOILER_PHRASE" in err_blob
    assert "HINT_LEAKS_ANSWER" in err_blob
    assert "BLACKLIST_TERM_HIT" in err_blob
    assert "INVALID_TRAP_TAG" in err_blob
    assert "SKELETON_LEAKS_NUMBER" in err_blob

    # 4b. 验证终轮 final_round=True 在 SymPy 仍败时降级 NEEDS_REVIEW 但仍保留 Stage 2 结果
    sympy_fail_rec = {
        "task_id": uid_to_tid[anchor_uids[3]],
        "steps": _base_steps(),
        "abstract_skeleton": "基础算术恒等转化模型",
        "primary_l1_module": "计算与数感",
        "secondary_l1_modules": [],
        "thinking_methods": ["分步直算"],
        "classic_motifs": [],
        "trap_tags": [],
        "difficulty": _base_diff(),
    }
    sympy_fail_path = tmp_path / "s2_ok.jsonl"
    sympy_fail_path.write_text(json.dumps(sympy_fail_rec, ensure_ascii=False), encoding="utf-8")
    imp_s2 = import_unified_results(
        db_path=db_path,
        results_file=sympy_fail_path,
        task_map_file=out_dir / "task_map.json",
        config_dir=cfg_dir,
        final_round=True,
    )
    assert imp_s2.ingested_stage2_only == 1

    # 另造一题新 pending 测 final_round SymPy 失败降级
    pending_sfail_uid = "primary_olympiad:ch_test:q_sfail"
    with closing(get_connection(db_path)) as conn:
        _seed_question(
            conn,
            uid=pending_sfail_uid,
            source_id="q_sfail",
            text="求 100 + 200 的值。",
            content_hash="hash_sfail",
        )
        conn.commit()
    out_dir_sf = tmp_path / "out_sf"
    exp_sf = export_unified_tasks(db_path=db_path, out_dir=out_dir_sf, config_dir=cfg_dir, only_uids=[pending_sfail_uid])
    assert exp_sf.exported_unified == 1
    sf_map = json.loads((out_dir_sf / "task_map.json").read_text(encoding="utf-8"))
    sf_tid = next(iter(sf_map))
    sf_rec = dict(results_round1[2])
    sf_rec["task_id"] = sf_tid
    sf_rec["final_answers"] = [
        {
            "label": "主问",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "SINGLE",
            "answer": "999",
            "sympy_expr": "999",
        }
    ]
    sf_rec["sympy_code"] = "def verify():\n    return {'主问': str(100 + 200)}\n"
    sf_res_file = tmp_path / "sf_res.jsonl"
    sf_res_file.write_text(json.dumps(sf_rec, ensure_ascii=False), encoding="utf-8")
    imp_sf = import_unified_results(
        db_path=db_path,
        results_file=sf_res_file,
        task_map_file=out_dir_sf / "task_map.json",
        config_dir=cfg_dir,
        final_round=True,
    )
    assert imp_sf.ingested_needs_review == 1
    assert imp_sf.failed_final_count == 0
    with closing(get_connection(db_path, readonly=True)) as conn:
        q_sf = conn.execute(
            "SELECT eff_verification_status, eff_is_servable, review_reason, eff_base_elo FROM questions WHERE question_uid = ?",
            (pending_sfail_uid,),
        ).fetchone()
        assert q_sf["eff_verification_status"] == "NEEDS_REVIEW"
        assert q_sf["eff_is_servable"] == 0
        assert "SYMPY_FAILED" in q_sf["review_reason"]
        assert q_sf["eff_base_elo"] > 0
        s2_sf = conn.execute(
            "SELECT status, model_name FROM compile_state WHERE question_uid = ? AND stage = 'S2'",
            (pending_sfail_uid,),
        ).fetchone()
        assert s2_sf["status"] == "OK"
        assert s2_sf["model_name"] == SUBAGENT_MODEL_NAME

    # 5. 验证防覆盖锁：即使 .env 模型改变，stage1_solve 默认跳过 subagent-unified
    s1_res = await run_stage1_compile(
        db_path=db_path,
        config_dir=cfg_dir,
        only_uids=[pending_sym_uid],
        use_mock=True,
        force=False,
    )
    assert s1_res.skipped_ok == 1
    assert s1_res.processed == 0

    # 再次导出时，已完成的应全部跳过
    exp2 = export_unified_tasks(db_path=db_path, out_dir=tmp_path / "out2", config_dir=cfg_dir)
    assert exp2.exported_unified == 0
    assert exp2.exported_stage2_only == 2  # 仅剩 anc_2/3 尚未入库，anc_1/anc_4 与全部 pending 均已锁定跳过
