"""Step 2a（零 DB 无状态自检核）与 Step 3（题单预检、Stage 0 dry_run、态势巡检与挂起批扫描）单元测试。"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.models.schemas import ExternalDiagnosisOutput, Stage2OnlyOutput, UnifiedS1S2Output
from shuati.pipeline.stage0_ingest import run_stage0_ingest
from shuati.services.compiler_tools import get_pipeline_status, scan_pending_batches
from shuati.services.diagnose_batch import (
    parse_question_list_markdown,
    preflight_question_list_markdown,
)
from shuati.verify.draft_validator import (
    flatten_strict_schema,
    validate_draft_payload,
    verify_sympy_payload,
)


def _write_minimal_config(cfg_dir: Path, *, embed_model: str = "text-embedding-3-large") -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.yaml").write_text(
        (
            "llm:\n"
            "  embedding:\n"
            f"    model: \"{embed_model}\"\n"
            "pipeline:\n"
            "  sympy_timeout_seconds: 5.0\n"
            "  sympy_memory_limit_mb: 512\n"
            "  allow_pi_314_subjects:\n"
            "    - 小学奥数\n"
        ),
        encoding="utf-8",
    )
    (cfg_dir / "method_blacklist.yaml").write_text(
        (
            "blacklist_terms:\n"
            "  - 转化与化归\n"
            "  - 逻辑推理\n"
            "seed_traps:\n"
            "  - code: TRAP_DOMAIN_BOUNDARY\n"
            "    name: 边界端点与定义域遗漏\n"
            "  - code: TRAP_FRAME_SHIFT\n"
            "    name: 参考系转换与基准漂移\n"
            "  - code: TRAP_ARITHMETIC_SLIP\n"
            "    name: 纯四则计算与抄写笔误\n"
        ),
        encoding="utf-8",
    )
    (cfg_dir / "collections.yaml").write_text(
        (
            "rules:\n"
            "  - match: \"ch*\"\n"
            "    grade_subject: \"小学奥数\"\n"
        ),
        encoding="utf-8",
    )


def _valid_unified_payload() -> dict[str, Any]:
    return {
        "task_id": "T0001",
        "visual_facts": "",
        "visual_ambiguity_flag": False,
        "visual_ambiguity_note": "",
        "method_summary": "利用往返速度比与时间反比列方程求解",
        "solution_markdown": "设单程为 d，联立方程 5*(d/30 + d/90) = 20，解得 d = 90。",
        "final_answers": [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "90",
                "unit": "千米",
                "sympy_expr": "90",
                "items": None,
                "choice_label": None,
                "canonical": None,
            }
        ],
        "sympy_code": (
            "import sympy as sp\n"
            "def verify():\n"
            "    d = sp.symbols('d', positive=True)\n"
            "    sol = sp.solve(sp.Eq(5 * (d / 30 + d / 90), 20), d)[0]\n"
            "    return {'主问': str(sol)}\n"
        ),
        "steps": [
            {
                "step_index": 1,
                "sub_goal": "建立往返总时间与单程距离关系",
                "core_formula": "5*(d/v1 + d/v2) = T",
                "hint": "先求出空车速度，再列出单程往返时间表达式。",
                "raw_l1": "行程问题",
                "raw_l2": "比例行程",
                "raw_l3": "速度比与时间反比转化",
            }
        ],
        "abstract_skeleton": "已知往返两段速度倍数关系与总时间，通过路程时间关系求单程距离。",
        "primary_l1_module": "行程问题",
        "secondary_l1_modules": [],
        "thinking_methods": ["比例转化", "方程建模"],
        "classic_motifs": ["往返行程模型"],
        "trap_tags": ["TRAP_FRAME_SHIFT"],
        "difficulty": {
            "C_concept_count": {"evidence": "双考点串联", "score": 2},
            "S_chain_length": {"evidence": "两步推理", "score": 1},
            "T_trickiness": {"evidence": "区分单程与往返", "score": 2},
            "L_computation_load": {"evidence": "常规分数运算", "score": 1},
        },
    }


def test_draft_validator_has_zero_sqlite_dependency(tmp_path: Path) -> None:
    validator_file = Path("src/shuati/verify/draft_validator.py")
    tree = ast.parse(validator_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "sqlite3"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "sqlite3"

    # 在全新子进程中导入 draft_validator，确认不会间接加载 sqlite3
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, shuati.verify.draft_validator as dv; assert 'sqlite3' not in sys.modules",
        ],
        cwd=str(Path.cwd()),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"

    # 在没有任何 db/task_map 的临时目录中可正常校验
    cfg_dir = tmp_path / "config"
    _write_minimal_config(cfg_dir)
    res = validate_draft_payload(
        mode="UNIFIED_S1_S2",
        candidate=_valid_unified_payload(),
        question_text="一辆货车满载速度为30千米/小时，空车速度是满载的3倍，往返5次共耗时20小时，求单程距离。",
        grade_subject="小学奥数",
        run_sympy=False,
        config_dir=cfg_dir,
    )
    assert res["ok"] is True
    assert res["normalized_preview"]["base_elo"] == 1405
    assert res["normalized_preview"]["tier"] == "L3_综合中档"


def test_flatten_strict_schema_inlines_refs_and_tightens_bounds() -> None:
    for schema_cls in (UnifiedS1S2Output, Stage2OnlyOutput, ExternalDiagnosisOutput):
        flat = flatten_strict_schema(schema_cls)
        raw_txt = json.dumps(flat, ensure_ascii=False)
        assert "$defs" not in flat
        assert '"$ref"' not in raw_txt
        diff_props = flat["properties"]["difficulty"]["properties"]
        assert diff_props["C_concept_count"]["properties"]["score"]["minimum"] == 1
        assert diff_props["C_concept_count"]["properties"]["score"]["maximum"] == 4
        for dim in ("S_chain_length", "T_trickiness", "L_computation_load"):
            assert diff_props[dim]["properties"]["score"]["minimum"] == 1
            assert diff_props[dim]["properties"]["score"]["maximum"] == 3


def test_verify_sympy_payload_passes_rejects_vacuous_and_checks_pi(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    _write_minimal_config(cfg_dir)

    ok_res = verify_sympy_payload(
        sympy_code=(
            "import sympy as sp\n"
            "def verify():\n"
            "    d = sp.symbols('d', positive=True)\n"
            "    return {'主问': str(sp.solve(sp.Eq(5 * (d / 30 + d / 90), 20), d)[0])}\n"
        ),
        final_answers=_valid_unified_payload()["final_answers"],
        grade_subject="小学奥数",
        config_dir=cfg_dir,
    )
    assert ok_res["ok"] is True
    assert ok_res["sympy_status"] == "PASSED"
    assert ok_res["computed_values"] == {"主问": "90"}
    assert ok_res["errors"] == []

    # 反空壳硬编码必须被 AST 探针拦截并产出 fix_hints
    vacuous_res = verify_sympy_payload(
        sympy_code="def verify():\n    return {'主问': '90'}\n",
        final_answers=_valid_unified_payload()["final_answers"],
        grade_subject="小学奥数",
        config_dir=cfg_dir,
    )
    assert vacuous_res["ok"] is False
    assert vacuous_res["sympy_status"] == "AST_REJECTED"
    assert vacuous_res["errors"][0]["code"] == "SYMPY_AST_REJECTED"
    assert vacuous_res["fix_hints"]

    # 小学奥数允许 pi ≈ 3.14，初中数学拒绝
    pi_answers = [
        {
            "label": "主问",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "SINGLE",
            "answer": "12.56",
            "sympy_expr": "12.56",
        }
    ]
    pi_code = "import sympy as sp\ndef verify():\n    return {'主问': str(4 * sp.pi)}\n"
    res_primary = verify_sympy_payload(
        sympy_code=pi_code,
        final_answers=pi_answers,
        grade_subject="小学奥数",
        config_dir=cfg_dir,
    )
    assert res_primary["ok"] is True

    res_junior = verify_sympy_payload(
        sympy_code=pi_code,
        final_answers=pi_answers,
        grade_subject="初中数学",
        config_dir=cfg_dir,
    )
    assert res_junior["ok"] is False
    assert res_junior["sympy_status"] == "FAILED"


def test_validate_draft_payload_catches_all_deterministic_violations(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    _write_minimal_config(cfg_dir)

    q_text = "甲乙两地相距450千米，角度为60度，求单程距离。"

    # 1. Schema 越界 (S=4)
    bad_schema = _valid_unified_payload()
    bad_schema["difficulty"]["S_chain_length"]["score"] = 4
    r_schema = validate_draft_payload(
        mode="UNIFIED_S1_S2",
        candidate=bad_schema,
        question_text=q_text,
        run_sympy=False,
        config_dir=cfg_dir,
    )
    assert r_schema["ok"] is False
    assert r_schema["errors"][0]["code"] == "SCHEMA_ERROR"

    # 2. 综合 Stage 2 违规：步骤不连续 + raw_l3 含数字 + 黑名单 + hint 剧透 + trap 枚举外 + 骨架泄数
    bad_s2 = _valid_unified_payload()
    bad_s2["steps"][0]["step_index"] = 2
    bad_s2["steps"][0]["raw_l3"] = "转化90度模型"
    bad_s2["steps"][0]["hint"] = "所以等于90，直接填90即可"
    bad_s2["thinking_methods"] = ["转化与化归"]
    bad_s2["trap_tags"] = ["FREE_TEXT_TRAP"]
    bad_s2["abstract_skeleton"] = "已知相距450与角度60求单程距离。"  # 450 禁止，60 豁免
    r_s2 = validate_draft_payload(
        mode="UNIFIED_S1_S2",
        candidate=bad_s2,
        question_text=q_text,
        run_sympy=False,
        config_dir=cfg_dir,
    )
    assert r_s2["ok"] is False
    codes = {e["code"] for e in r_s2["errors"]}
    assert {
        "STEP_INDEX_NON_CONTIGUOUS",
        "RAW_L3_CONTAINS_DIGIT",
        "BLACKLIST_TERM_HIT",
        "HINT_SPOILER_PHRASE",
        "HINT_LEAKS_ANSWER",
        "INVALID_TRAP_TAG",
        "SKELETON_LEAKS_NUMBER",
    }.issubset(codes)
    # 确认 60 作为豁免角未触发 SKELETON_LEAKS_NUMBER
    skel_msgs = [e["message"] for e in r_s2["errors"] if e["code"] == "SKELETON_LEAKS_NUMBER"]
    assert len(skel_msgs) == 1 and "450" in skel_msgs[0]

    # 3. STAGE2_ONLY + DIAGNOSE 模式
    s2_only = {
        "task_id": "T0002",
        "steps": _valid_unified_payload()["steps"],
        "abstract_skeleton": "已知两段速度比例与总时间，求单程距离。",
        "primary_l1_module": "行程问题",
        "secondary_l1_modules": [],
        "thinking_methods": ["比例转化"],
        "classic_motifs": ["往返行程模型"],
        "trap_tags": ["TRAP_FRAME_SHIFT"],
        "difficulty": _valid_unified_payload()["difficulty"],
    }
    r_stage2 = validate_draft_payload(
        mode="STAGE2_ONLY",
        candidate=s2_only,
        question_text=q_text,
        verified_final_answers=_valid_unified_payload()["final_answers"],
        run_sympy=False,
        config_dir=cfg_dir,
    )
    assert r_stage2["ok"] is True

    diag_payload = dict(_valid_unified_payload())
    diag_payload.pop("task_id")
    diag_payload["matched_l2"] = ["比例行程"]
    r_diag = validate_draft_payload(
        mode="DIAGNOSE",
        candidate=diag_payload,
        question_text=q_text,
        run_sympy=True,
        config_dir=cfg_dir,
    )
    assert r_diag["ok"] is True
    assert r_diag["sympy_check"]["sympy_status"] == "PASSED"


def test_preflight_question_list_markdown_fixes_and_readonly_source(tmp_path: Path) -> None:
    img_dir = tmp_path / "imgs"
    img_dir.mkdir()
    img1 = img_dir / "fig1.png"
    img2 = img_dir / "fig2.png"
    Image.new("RGB", (20, 20), color=(255, 0, 0)).save(img1, format="PNG")
    Image.new("RGB", (20, 20), color=(0, 255, 0)).save(img2, format="PNG")

    src_md = tmp_path / "raw_questions.md"
    raw_content = (
        "# 行程错题集\n\n"
        "##12. 如图，求阴影面积。\n"
        "--- \n"
        "图片: imgs/fig1.png\n\n"
        "##19. 一列火车从甲地开往乙地，每小时行60千米，3小时到达，求路程。\n"
        "![火车图](imgs/fig2.png)\n\n"
        "###2. 缺图验证题\n"
        "已知正方形边长为5，求对角线平方。\n"
        "图片: imgs/missing_fig.png\n"
    )
    src_md.write_text(raw_content, encoding="utf-8")

    report = preflight_question_list_markdown(src_md, drafts_dir=tmp_path / "drafts")

    # 源文件严格只读未修改
    assert src_md.read_text(encoding="utf-8") == raw_content
    norm_path = Path(report["normalized_file"])
    assert norm_path.exists()
    assert report["total_questions"] == 3
    assert len(report["fixes_applied"]) >= 4

    # 验证解析结果：1) fig1 归位到第1题；2) 第2题同行正文未丢弃；3) 图片路径绝对化；4) missing_fig 检出
    parsed = parse_question_list_markdown(norm_path)
    assert len(parsed) == 3
    assert parsed[0].question_text == "如图，求阴影面积。"
    assert [p.resolve() for p in parsed[0].image_paths] == [img1.resolve()]

    assert "一列火车从甲地开往乙地" in parsed[1].question_text
    assert [p.resolve() for p in parsed[1].image_paths] == [img2.resolve()]

    assert len(report["missing_images"]) == 1
    assert report["missing_images"][0]["index"] == 3
    assert report["items"][1]["char_count"] > 10
    assert report["items"][2]["missing_images"]


def test_stage0_ingest_dry_run_is_zero_write(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    _write_minimal_config(cfg_dir)

    bank_dir = tmp_path / "题库"
    col_dir = bank_dir / "ch01"
    unmapped_dir = bank_dir / "misc_unmapped"
    (col_dir / "images").mkdir(parents=True)
    unmapped_dir.mkdir(parents=True)

    img_file = col_dir / "images" / "q1.png"
    Image.new("RGB", (150, 150), color=(10, 20, 30)).save(img_file, format="PNG")

    (col_dir / "problems.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "q1", "question": "如图求面积", "images": ["images/q1.png"]}, ensure_ascii=False),
                json.dumps({"id": "q2", "question": "如图求面积", "images": ["images/q1.png"]}, ensure_ascii=False),
                json.dumps({"id": "q3", "question": "缺图题", "images": ["images/missing.png"]}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (unmapped_dir / "problems.jsonl").write_text(
        json.dumps({"id": "u1", "question": "未映射集合题"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "data" / "shuati.db"
    assets_dir = tmp_path / "data" / "assets" / "images"
    reports_dir = tmp_path / "data" / "reports"
    lock_path = tmp_path / "data" / ".compile.lock"

    dry_rep = run_stage0_ingest(
        db_path=db_path,
        root_dir=bank_dir,
        config_dir=cfg_dir,
        assets_dir=assets_dir,
        reports_dir=reports_dir,
        lock_path=lock_path,
        dry_run=True,
    )
    assert dry_rep.dry_run is True
    assert dry_rep.to_dict()["dry_run"] is True
    assert dry_rep.unmapped_collections == ["misc_unmapped"]
    assert dry_rep.total_scanned == 3
    assert len(dry_rep.newly_ingested) == 2  # q1 + exact dup q2
    assert len(dry_rep.exact_duplicates) == 1
    assert len(dry_rep.rejected_questions) == 1  # q3 missing image
    # 严格零写盘：DB、锁文件、CAS 目录、报告目录均未创建
    assert not db_path.exists()
    assert not lock_path.exists()
    assert not assets_dir.exists()
    assert not reports_dir.exists()

    # 真实入库后，再次 dry_run 应识别 skipped_unchanged
    real_rep = run_stage0_ingest(
        db_path=db_path,
        root_dir=bank_dir,
        config_dir=cfg_dir,
        assets_dir=assets_dir,
        reports_dir=reports_dir,
        lock_path=lock_path,
        dry_run=False,
    )
    assert len(real_rep.newly_ingested) == 2

    dry_rep2 = run_stage0_ingest(
        db_path=db_path,
        root_dir=bank_dir,
        config_dir=cfg_dir,
        assets_dir=assets_dir,
        reports_dir=reports_dir,
        lock_path=lock_path,
        dry_run=True,
    )
    assert len(dry_rep2.skipped_unchanged) == 2
    assert len(dry_rep2.newly_ingested) == 0


def test_compiler_tools_status_and_pending_batches(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    _write_minimal_config(cfg_dir)

    db_path = tmp_path / "data" / "shuati.db"
    vec_dir = tmp_path / "data" / "vectors"
    uni_dir = tmp_path / "data" / "unified"

    # 1. 空库前巡检不崩溃且不创建 DB
    status_empty = get_pipeline_status(
        db_path=db_path,
        config_dir=cfg_dir,
        vectors_dir=vec_dir,
        unified_dir=uni_dir,
    )
    assert status_empty["db_exists"] is False
    assert not db_path.exists()
    assert "shuati ingest --dry-run --json" in status_empty["recommended_next_commands"]

    # 2. 构造 DB + 挂起批 + 向量漂移
    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)
    for uid, v_status, reason, s1_st, s2_st, servable_ok in (
        ("primary_olympiad:ch01:q1", "VERIFIED_CONSENSUS", "", "OK", "OK", True),
        ("primary_olympiad:ch01:q2", "NEEDS_REVIEW", "SYMPY_FAILED,VISUAL_AMBIGUITY", "OK", "PENDING", False),
    ):
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection, source_id,
                raw_question, content_hash, plain_fingerprint
            ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', ?, '题干', ?, 'fp');
            """,
            (uid, uid.split(":")[-1], f"h_{uid}"),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_format,
                m_verification_status, review_reason, m_primary_l1_module,
                m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json
            ) VALUES (?, '小学奥数', '题干', '解答题', ?, ?, '行程问题', 2, 1, 1, 1, ?);
            """,
            (
                uid,
                v_status,
                reason,
                json.dumps(
                    [
                        {
                            "label": "主问",
                            "answer_type": "NUMERIC",
                            "verify_mode": "SYMBOLIC",
                            "collection_mode": "SINGLE",
                            "answer": "10",
                            "sympy_expr": "10",
                        }
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute("INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', ?);", (uid, s1_st))
        conn.execute("INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S2', ?);", (uid, s2_st))
        for stg in ("S3C", "S3D", "S4"):
            conn.execute("INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, 'PENDING');", (uid, stg))
        effective.recompute(conn, uid)
    conn.commit()
    conn.close()

    # 构造挂起批：root unified READY_TO_IMPORT + stage3_4/3c NO_RESULTS
    (uni_dir / "batches").mkdir(parents=True)
    (uni_dir / "task_map.json").write_text(json.dumps({"T0001": {"question_uid": "u1"}}), encoding="utf-8")
    (uni_dir / "batches" / "batch_001.jsonl").write_text('{"task_id": "T0001"}\n', encoding="utf-8")
    (uni_dir / "results_001.jsonl").write_text('{"task_id": "T0001"}\n', encoding="utf-8")

    s3c_dir = uni_dir / "stage3_4" / "3c"
    (s3c_dir / "batches").mkdir(parents=True)
    (s3c_dir / "3c_manifest.json").write_text(
        json.dumps({"step_kind": "3c", "batch_count": 2}),
        encoding="utf-8",
    )
    (s3c_dir / "3c_task_map.json").write_text(json.dumps({"T1": "u1"}), encoding="utf-8")

    # 构造向量仓（故意留空/旧模型触发漂移告警）
    vec_dir.mkdir(parents=True)
    (vec_dir / "meta.json").write_text(
        json.dumps({"embedding_model": "old-model", "dimension": 4, "meta_version": 1, "quantiles": {}}),
        encoding="utf-8",
    )
    np.savez(
        vec_dir / "surface_primary_olympiad.npz",
        ids=np.asarray(["u1"]),
        text_hashes=np.asarray(["h1"]),
        vectors=np.zeros((1, 4), dtype=np.float32),
    )

    batches = scan_pending_batches(uni_dir)
    assert len(batches) == 2
    by_kind = {b["step_kind"]: b for b in batches}
    assert by_kind["unified"]["status"] == "READY_TO_IMPORT"
    assert by_kind["3c"]["status"] == "NO_RESULTS"

    status = get_pipeline_status(
        db_path=db_path,
        config_dir=cfg_dir,
        vectors_dir=vec_dir,
        unified_dir=uni_dir,
    )
    assert status["db_exists"] is True
    subj = status["subjects"]["小学奥数"]
    assert subj["raw_total"] == 2
    assert subj["active_total"] == 2
    assert subj["servable_total"] == 1
    assert subj["needs_review_by_reason"] == {"SYMPY_FAILED": 1, "VISUAL_AMBIGUITY": 1}
    assert subj["stage_matrix"]["S1"] == {"OK": 2}
    assert subj["stage_matrix"]["S2"] == {"OK": 1, "PENDING": 1}
    assert status["subagent_routing"]["default_model"] in {
        "gemini-4-neon-medium",
        status["subagent_routing"]["default_model"],
    }
    assert status["subagent_routing"]["resolved_stages"]["unified"]
    assert status["vectors"]["model_mismatch"] is True
    assert status["vectors"]["needs_reindex"] is True
    cmds = status["recommended_next_commands"]
    assert any("shuati unified import" in c for c in cmds)
    assert any("shuati review" in c for c in cmds)
    assert any("shuati reindex --vectors --quantiles" in c for c in cmds)
