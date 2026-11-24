"""v2.6 P1 集成测试：独立多模态 Stage 2 流水线 + subagent_bridge 条目完整性硬校验。"""

from __future__ import annotations

import base64
import io
import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import BaseModel, Field
from typer.testing import CliRunner

from shuati.cli.main import app
from shuati.config import Settings
from shuati.core import effective
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm.gateway import LLMCallResult, LLMGateway
from shuati.models.schemas import (
    PrerequisiteDagOutput,
    Stage2OnlyOutput,
    Stage3cBatchMappingOutput,
    Stage4RefineBatchOutput,
    TaxonomyProposeOutput,
)
from shuati.pipeline.stage2_decompose import run_stage2_decompose_async
from shuati.pipeline.subagent_bridge import export_task_batches, load_and_validate_results
from shuati.pipeline.unified_import import register_anchors_from_yaml


def _setup_cfg(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("settings.yaml", "subjects.yaml", "method_blacklist.yaml"):
        src = Path("config") / fname
        if src.exists():
            (cfg_dir / fname).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    prompts_src = Path("config/prompts")
    if prompts_src.is_dir():
        for fp in prompts_src.rglob("*"):
            if fp.is_file():
                rel = fp.relative_to(prompts_src)
                dst = cfg_dir / "prompts" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(fp.read_text(encoding="utf-8"), encoding="utf-8")
    return cfg_dir


def _seed_question(
    conn,
    *,
    uid: str,
    source_id: str,
    text: str,
    content_hash: str,
    ver_status: str = "VERIFIED_CONSENSUS",
    has_images: bool = False,
    cas_images: list[str] | None = None,
    visual_facts: str = "",
    final_answers: list[dict[str, Any]] | None = None,
    with_solution: bool = True,
) -> None:
    ans = final_answers if final_answers is not None else [
        {
            "label": "主问",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "SINGLE",
            "answer": "24",
            "sympy_expr": "24",
        }
    ]
    ans_json = json.dumps(ans, ensure_ascii=False)
    cas_json = json.dumps(cas_images or [], ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id,
            raw_chapter, raw_question, cas_images_json, legacy_tags_json, content_hash
        ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'secret_col', ?, 'secret_chapter_leak', ?, ?, '{"tag":"secret_legacy"}', ?)
        """,
        (uid, source_id, text, cas_json, content_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, cas_images_json, has_images,
            m_question_text, m_format, m_visual_facts, m_verification_status, m_final_answers_json
        ) VALUES (?, '小学奥数', ?, ?, ?, '解答题', ?, ?, ?)
        """,
        (uid, cas_json, 1 if (has_images or cas_images) else 0, text, visual_facts, ver_status, ans_json),
    )
    if with_solution:
        conn.execute(
            """
            INSERT INTO question_solutions(
                solution_id, question_uid, branch_id, is_optimal, source_origin,
                method_summary, solution_markdown, sympy_verified
            ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '等高面积转化', '解：面积为 24。', 1)
            """,
            (f"{uid}::branch_1", uid),
        )
    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status, input_hash) VALUES (?, 'S1', 'OK', ?)",
        (uid, content_hash),
    )
    effective.recompute(conn, uid)


def _seed_anchors(db_path: Path, cfg_dir: Path, conn) -> list[str]:
    anchors = [f"primary_olympiad:ch_anc:a{i}" for i in range(1, 5)]
    for i, uid in enumerate(anchors, 1):
        _seed_question(
            conn,
            uid=uid,
            source_id=f"a{i}",
            text=f"锚点题 {i}：求面积",
            content_hash=f"ha_{i}",
            ver_status="VERIFIED_CONSENSUS",
        )
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status, input_hash, model_name) VALUES (?, 'S2', 'OK', ?, 'subagent-unified')",
            (uid, f"ha_{i}"),
        )
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
    return anchors


def _sample_stage2_item(task_id: str, *, skeleton: str = "利用几何面积比建立方程求解目标面积") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "steps": [
            {
                "step_index": 1,
                "sub_goal": "确定底边比例并转化面积关系",
                "core_formula": "S_1 / S_2 = b_1 / b_2",
                "hint": "先观察共顶点三角形的高是否相同",
                "raw_l1": "平面几何",
                "raw_l2": "等高模型",
                "raw_l3": "等高三角形面积比",
            }
        ],
        "abstract_skeleton": skeleton,
        "primary_l1_module": "平面几何",
        "secondary_l1_modules": [],
        "thinking_methods": ["数形结合"],
        "classic_motifs": ["等高模型"],
        "trap_tags": [],
        "difficulty": {
            "C_concept_count": {"evidence": "单一等高模型转化", "score": 2},
            "S_chain_length": {"evidence": "两步比例换算", "score": 1},
            "T_trickiness": {"evidence": "图形条件明晰无陷阱", "score": 1},
            "L_computation_load": {"evidence": "基础整数比例运算", "score": 1},
        },
    }


def _make_exif_rotated_jpeg(path: Path) -> None:
    """生成 40x20 且带 EXIF Orientation=6（顺时针 90° 后应为 20x40）的测试图片。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (40, 20), color=(255, 0, 0))
    exif = Image.Exif()
    exif[274] = 6  # Orientation = Rotate 90 CW
    img.save(path, format="JPEG", exif=exif.tobytes())


def test_subagent_bridge_item_completeness_rejects_truncation(tmp_path: Path) -> None:
    # 1. Stage3cBatchMappingOutput (items[].task_item_id): 导出 3 项，仅回传 2 项 -> 拦截且不标 IMPORTED
    d_3c = tmp_path / "bridge_3c"
    batches_3c = [
        {
            "batch_id": "3c_b1",
            "messages": [{"role": "user", "content": "map 3 items"}],
            "task_item_ids": ["item_1", "item_2", "item_3"],
        }
    ]
    manifest = export_task_batches(
        "3c", batches_3c, {"items": {}}, d_3c, schema_cls=Stage3cBatchMappingOutput
    )
    assert manifest["batch_item_ids"] == {"3c_b1": ["item_1", "item_2", "item_3"]}

    res_partial = d_3c / "results_partial.jsonl"
    partial_payload = {
        "batch_id": "3c_b1",
        "result": {
            "items": [
                {
                    "task_item_id": "item_1",
                    "primary_l1_module": "平面几何",
                    "steps": [{"step_index": 1, "mapped_l1": "平面几何", "mapped_l2": "等高模型"}],
                },
                {
                    "task_item_id": "item_2",
                    "primary_l1_module": "平面几何",
                    "steps": [{"step_index": 1, "mapped_l1": "平面几何", "mapped_l2": "等高模型"}],
                },
            ]
        },
    }
    res_partial.write_text(json.dumps(partial_payload, ensure_ascii=False) + "\n", encoding="utf-8")

    valid_p, _, errs_p = load_and_validate_results(
        "3c", res_partial, d_3c / "3c_task_map.json", Stage3cBatchMappingOutput
    )
    assert valid_p == {}
    assert len(errs_p) == 1
    assert errs_p[0]["error_code"] == "INCOMPLETE_BATCH_ITEMS"
    assert errs_p[0]["missing_item_ids"] == ["item_3"]
    assert (d_3c / "retry_tasks.jsonl").is_file()
    assert "3c_b1" in (d_3c / "retry_tasks.jsonl").read_text(encoding="utf-8")
    assert json.loads((d_3c / "3c_manifest.json").read_text(encoding="utf-8"))["status"] == "PENDING_IMPORT"

    # 空 items=[] 同样不能绕过完整性检查
    res_empty_items = d_3c / "results_empty_items.jsonl"
    res_empty_items.write_text(
        json.dumps({"batch_id": "3c_b1", "result": {"items": []}}) + "\n", encoding="utf-8"
    )
    valid_e, _, errs_e = load_and_validate_results(
        "3c", res_empty_items, d_3c / "3c_task_map.json", Stage3cBatchMappingOutput
    )
    assert valid_e == {}
    assert errs_e[0]["error_code"] == "INCOMPLETE_BATCH_ITEMS"
    assert errs_e[0]["missing_item_ids"] == ["item_1", "item_2", "item_3"]

    # 传 verify_item_completeness=False 时可显式放行
    valid_skip, _, errs_skip = load_and_validate_results(
        "3c",
        res_partial,
        d_3c / "3c_task_map.json",
        Stage3cBatchMappingOutput,
        verify_item_completeness=False,
    )
    assert "3c_b1" in valid_skip and errs_skip == []

    # 重新导出后补齐 3 项应成功流转 IMPORTED
    d_3c_ok = tmp_path / "bridge_3c_ok"
    export_task_batches("3c", batches_3c, {"items": {}}, d_3c_ok, schema_cls=Stage3cBatchMappingOutput)
    full_payload = {
        "batch_id": "3c_b1",
        "result": {
            "items": [
                {
                    "task_item_id": tid,
                    "primary_l1_module": "平面几何",
                    "steps": [{"step_index": 1, "mapped_l1": "平面几何", "mapped_l2": "等高模型"}],
                }
                for tid in ("item_1", "item_2", "item_3")
            ]
        },
    }
    res_full = d_3c_ok / "results_full.jsonl"
    res_full.write_text(json.dumps(full_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    valid_f, _, errs_f = load_and_validate_results(
        "3c", res_full, d_3c_ok / "3c_task_map.json", Stage3cBatchMappingOutput
    )
    assert "3c_b1" in valid_f and errs_f == []
    assert json.loads((d_3c_ok / "3c_manifest.json").read_text(encoding="utf-8"))["status"] == "IMPORTED"

    # 2. Stage4RefineBatchOutput (decisions[].pair_id) 漏项拦截
    d_s4 = tmp_path / "bridge_s4"
    export_task_batches(
        "stage4",
        [{"batch_id": "s4_b1", "messages": [], "task_item_ids": ["p1", "p2"]}],
        {"pairs": {}},
        d_s4,
        schema_cls=Stage4RefineBatchOutput,
    )
    res_s4 = d_s4 / "results.jsonl"
    res_s4.write_text(
        json.dumps({"batch_id": "s4_b1", "result": {"decisions": [{"pair_id": "p1", "is_valid": True}]}})
        + "\n",
        encoding="utf-8",
    )
    v4, _, e4 = load_and_validate_results("stage4", res_s4, d_s4 / "stage4_task_map.json", Stage4RefineBatchOutput)
    assert v4 == {} and e4[0]["error_code"] == "INCOMPLETE_BATCH_ITEMS"
    assert json.loads((d_s4 / "stage4_manifest.json").read_text(encoding="utf-8"))["status"] == "PENDING_IMPORT"

    # 3. assignments[] 型 Schema 漏项拦截（模拟 Stage3dClusterAssignOutput 契约）
    class _AssignElem(BaseModel):
        item_id: str = ""
        raw_l3: str
        canonical_name: str

    class _AssignBatch(BaseModel):
        assignments: list[_AssignElem] = Field(default_factory=list)

    d_asg = tmp_path / "bridge_assign"
    export_task_batches(
        "3d_cluster",
        [{"batch_id": "asg_b1", "messages": [], "task_item_ids": ["算子甲", "算子乙"]}],
        {},
        d_asg,
        schema_cls=_AssignBatch,
    )
    res_asg = d_asg / "results.jsonl"
    res_asg.write_text(
        json.dumps(
            {
                "batch_id": "asg_b1",
                "result": {"assignments": [{"raw_l3": "算子甲", "canonical_name": "标准算子"}]},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    va, _, ea = load_and_validate_results(
        "3d_cluster", res_asg, d_asg / "3d_cluster_task_map.json", _AssignBatch
    )
    assert va == {} and ea[0]["error_code"] == "INCOMPLETE_BATCH_ITEMS" and ea[0]["missing_item_ids"] == ["算子乙"]

    # 4. 非条目型单对象 Schema (TaxonomyProposeOutput / PrerequisiteDagOutput) 自动跳过逐条比对
    d_dag = tmp_path / "bridge_dag"
    export_task_batches(
        "3e",
        [{"batch_id": "dag_b1", "messages": [], "task_item_ids": ["node_1", "node_2"]}],
        {},
        d_dag,
        schema_cls=PrerequisiteDagOutput,
    )
    res_dag = d_dag / "results.jsonl"
    res_dag.write_text(json.dumps({"batch_id": "dag_b1", "result": {"edges": []}}) + "\n", encoding="utf-8")
    vd, _, ed = load_and_validate_results("3e", res_dag, d_dag / "3e_task_map.json", PrerequisiteDagOutput)
    assert "dag_b1" in vd and ed == []
    assert json.loads((d_dag / "3e_manifest.json").read_text(encoding="utf-8"))["status"] == "IMPORTED"

    # TaxonomyProposeOutput 同理跳过
    d_prop = tmp_path / "bridge_prop"
    export_task_batches(
        "propose",
        [{"batch_id": "p_b1", "messages": [], "task_item_ids": ["cluster_1", "cluster_2"]}],
        {},
        d_prop,
        schema_cls=TaxonomyProposeOutput,
    )
    res_prop = d_prop / "results.jsonl"
    res_prop.write_text(
        json.dumps({"batch_id": "p_b1", "result": {"modules": []}}) + "\n", encoding="utf-8"
    )
    vp, _, ep = load_and_validate_results("propose", res_prop, d_prop / "propose_task_map.json", TaxonomyProposeOutput)
    assert "p_b1" in vp and ep == []


@pytest.mark.asyncio
async def test_stage2_api_multimodal_routing_exif_and_human_override_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = _setup_cfg(tmp_path)
    db_path = tmp_path / "s2_api.db"
    assets_dir = tmp_path / "assets" / "images"
    img_name = "cas_img_rot.jpg"
    _make_exif_rotated_jpeg(assets_dir / img_name)

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)

        # 未就绪 anchors_ready == 0 时前置门禁必须阻断
        with pytest.raises(PreconditionError, match="PRECONDITION_FAILED"):
            await run_stage2_decompose_async(
                conn, Settings(), grade_subject="小学奥数", mode="api", config_dir=cfg_dir
            )

        _seed_anchors(db_path, cfg_dir, conn)

        uid_img = "primary_olympiad:ch01:q_img"
        uid_txt = "primary_olympiad:ch01:q_txt"
        uid_nr = "primary_olympiad:ch01:q_nr"
        uid_excl = "primary_olympiad:ch01:q_excl"

        _seed_question(
            conn,
            uid=uid_img,
            source_id="q_img",
            text="如图，已知阴影部分为直角三角形，求面积。",
            content_hash="h_img",
            ver_status="NEEDS_REVIEW",
            has_images=True,
            cas_images=[img_name],
            visual_facts="机审视觉：三角形底 6 高 8",
        )
        _seed_question(
            conn,
            uid=uid_txt,
            source_id="q_txt",
            text="纯文字题：甲乙两车相向而行，求相遇时间。",
            content_hash="h_txt",
            ver_status="VERIFIED_CONSENSUS",
            has_images=False,
        )
        _seed_question(
            conn,
            uid=uid_nr,
            source_id="q_nr",
            text="待人工复核题目不应进入 Stage 2",
            content_hash="h_nr",
            ver_status="NEEDS_REVIEW",
        )
        _seed_question(
            conn,
            uid=uid_excl,
            source_id="q_excl",
            text="已排除坏题不应进入 Stage 2",
            content_hash="h_excl",
            ver_status="VERIFIED_CONSENSUS",
        )
        # 注入人工 verification_status='HUMAN_VERIFIED'、visual_facts 与 final_answers_json 激活覆写，以及 q_excl 排除覆写
        human_ans = json.dumps(
            [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "48",
                    "sympy_expr": "48",
                }
            ],
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT INTO human_overrides(
                question_uid, field_name, override_value, reason, source, base_fingerprint, is_active, is_suspended
            ) VALUES
                (?, 'verification_status', 'HUMAN_VERIFIED', '人工验真定稿', 'REVIEW_UI', 'h_img', 1, 0),
                (?, 'visual_facts', '人工核定：底为 12、高为 8（图上印刷模糊处已核实）', '人工看图修正', 'REVIEW_UI', 'h_img', 1, 0),
                (?, 'final_answers_json', ?, '人工终值核定', 'REVIEW_UI', 'h_img', 1, 0),
                (?, 'is_excluded', '1', '坏题排除', 'REVIEW_UI', 'h_excl', 1, 0)
            """,
            (uid_img, uid_img, uid_img, human_ans, uid_excl),
        )
        effective.recompute(conn, uid_img)
        effective.recompute(conn, uid_excl)
        conn.commit()

        captured_calls: dict[str, dict[str, Any]] = {}

        async def _fake_call_structured(
            self: LLMGateway,
            *,
            slot: str,
            stage: str,
            messages: list[dict[str, Any]],
            schema: Any,
            question_uid: str | None = None,
            prompt_version: str = "",
            mock_key: str | None = None,
        ) -> LLMCallResult[Stage2OnlyOutput]:
            assert question_uid is not None
            captured_calls[question_uid] = {
                "slot": slot,
                "stage": stage,
                "messages": messages,
                "prompt_version": prompt_version,
            }
            payload = _sample_stage2_item(question_uid)
            return LLMCallResult(
                parsed=Stage2OnlyOutput.model_validate(payload),
                model_name=f"mock-{slot}",
                raw_response=json.dumps(payload, ensure_ascii=False),
            )

        monkeypatch.setattr(LLMGateway, "call_structured", _fake_call_structured)

        res = await run_stage2_decompose_async(
            conn,
            Settings(),
            grade_subject="小学奥数",
            mode="api",
            only_uids=[uid_img, uid_txt, uid_nr, uid_excl],
            config_dir=cfg_dir,
            assets_dir=assets_dir,
        )
        assert res["total_candidates"] == 2
        assert res["processed"] == 2
        assert res["succeeded"] == 2
        assert res["failed"] == 0

        # 验证有图题强制路由 vlm_primary、传入 EXIF 矫正后原图、并注入 D-Audit-4 最高权威指令
        assert uid_img in captured_calls
        img_call = captured_calls[uid_img]
        assert img_call["slot"] == "vlm_primary"
        img_sys = img_call["messages"][0]["content"]
        assert "D-Audit-4 人工视觉事实最高权威" in img_sys
        assert "D-Audit-4 人工标准答案最高权威" in img_sys
        img_user_parts = img_call["messages"][1]["content"]
        assert isinstance(img_user_parts, list)
        text_joined = "\n".join(p["text"] for p in img_user_parts if p.get("type") == "text")
        assert "人工核定：底为 12、高为 8" in text_joined
        assert "【人工核定视觉事实 visual_facts（最高权威）】" in text_joined
        assert "【人工核定标准答案 final_answers（最高权威）】" in text_joined

        image_blocks = [p for p in img_user_parts if p.get("type") == "image_url"]
        assert len(image_blocks) == 1
        data_url = image_blocks[0]["image_url"]["url"]
        assert data_url.startswith("data:image/jpeg;base64,")
        raw_bytes = base64.b64decode(data_url.split(",", 1)[1])
        with Image.open(io.BytesIO(raw_bytes)) as decoded_img:
            # 原图 40x20 + Orientation=6 -> exif_transpose 矫正后应为 20x40
            assert decoded_img.size == (20, 40)

        # 验证无图题路由 text_llm、不传图且不注入人工覆写权威提示
        assert uid_txt in captured_calls
        txt_call = captured_calls[uid_txt]
        assert txt_call["slot"] == "text_llm"
        assert isinstance(txt_call["messages"][1]["content"], str)
        assert "D-Audit-4 人工视觉事实最高权威" not in txt_call["messages"][0]["content"]
        assert uid_nr not in captured_calls and uid_excl not in captured_calls

        # 验证落库与下游 STALE 传播
        for u in (uid_img, uid_txt):
            qrow = conn.execute(
                "SELECT eff_primary_l1_module, eff_base_elo, eff_tier FROM questions WHERE question_uid = ?",
                (u,),
            ).fetchone()
            assert qrow["eff_primary_l1_module"] == "平面几何"
            assert qrow["eff_base_elo"] > 0
            assert qrow["eff_tier"] is not None
            states = {
                r["stage"]: r["status"]
                for r in conn.execute(
                    "SELECT stage, status FROM compile_state WHERE question_uid = ?", (u,)
                ).fetchall()
            }
            assert states["S2"] == "OK"
            assert states["S3C"] == "STALE"
            assert states["S3D"] == "STALE"
            assert states["S4"] == "STALE"

        # 断点续跑跳过已 OK 且指纹未变题目
        res2 = await run_stage2_decompose_async(
            conn,
            Settings(),
            grade_subject="小学奥数",
            mode="api",
            only_uids=[uid_img, uid_txt],
            config_dir=cfg_dir,
            assets_dir=assets_dir,
        )
        assert res2["skipped_locked"] == 2
        assert res2["processed"] == 0

        # --mock 离线兜底也应正常通过
        res_mock = await run_stage2_decompose_async(
            conn,
            Settings(),
            grade_subject="小学奥数",
            mode="api",
            only_uids=[uid_txt],
            force=True,
            config_dir=cfg_dir,
            assets_dir=assets_dir,
            use_mock=True,
        )
        assert res_mock["succeeded"] == 1


def test_cli_stage2_export_import_e2e_and_truncation_guard(tmp_path: Path) -> None:
    cfg_dir = _setup_cfg(tmp_path)
    db_path = tmp_path / "cli_s2.db"
    assets_dir = tmp_path / "assets" / "images"
    _make_exif_rotated_jpeg(assets_dir / "q1.jpg")

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_anchors(db_path, cfg_dir, conn)
        uids = [
            "primary_olympiad:ch02:q1",
            "primary_olympiad:ch02:q2",
            "primary_olympiad:ch02:q3",
        ]
        for idx, uid in enumerate(uids, 1):
            _seed_question(
                conn,
                uid=uid,
                source_id=f"q{idx}",
                text=f"第 {idx} 题：求等高三角形面积（值 24）。",
                content_hash=f"hc_{idx}",
                ver_status="VERIFIED_CONSENSUS",
                has_images=(idx == 1),
                cas_images=["q1.jpg"] if idx == 1 else [],
            )
        conn.commit()

    tasks_dir = tmp_path / "stage2_tasks"
    runner = CliRunner()

    # 1. CLI export（batch_size=3 打包为 1 批，包含 3 题）
    r_exp = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "2",
            "--mode",
            "export",
            "--grade-subject",
            "小学奥数",
            "--batch-size",
            "3",
            "--tasks-dir",
            str(tasks_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--assets-dir",
            str(assets_dir),
            "--json",
        ],
    )
    assert r_exp.exit_code == 0, r_exp.output
    d_exp = json.loads(r_exp.stdout)["data"]
    assert d_exp["exported_questions"] == 3
    assert d_exp["batch_count"] == 1
    assert d_exp["subagent_model"] == "gemini-4-neon-medium"

    manifest = json.loads((tasks_dir / "stage2_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "PENDING_IMPORT"
    assert manifest["subagent_model"] == "gemini-4-neon-medium"
    assert manifest["batch_item_ids"] == {"stage2_batch_0001": ["s2_0001", "s2_0002", "s2_0003"]}

    batch_file = tasks_dir / "batches" / "batch_0001.jsonl"
    batch_txt = batch_file.read_text(encoding="utf-8")
    assert "primary_olympiad:ch02" not in batch_txt
    assert "secret_chapter_leak" not in batch_txt
    assert "secret_col" not in batch_txt
    assert "secret_legacy" not in batch_txt
    assert "q1.jpg" in batch_txt

    # 2. 截断回传（3 题仅回 2 题）-> CLI import 拦截并写入 retry_tasks.jsonl，保持 PENDING_IMPORT
    trunc_results = tmp_path / "s2_results_truncated.jsonl"
    trunc_results.write_text(
        json.dumps(
            {
                "batch_id": "stage2_batch_0001",
                "result": {
                    "items": [
                        _sample_stage2_item("s2_0001"),
                        _sample_stage2_item("s2_0002"),
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    r_imp_trunc = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "S2",
            "--mode",
            "import",
            "--grade-subject",
            "小学奥数",
            "--tasks-dir",
            str(tasks_dir),
            "--results",
            str(trunc_results),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_imp_trunc.exit_code == 0, r_imp_trunc.output
    d_trunc = json.loads(r_imp_trunc.stdout)["data"]
    assert d_trunc["ingested"] == 0
    assert d_trunc["batch_errors"] == 1
    assert d_trunc["errors"][0]["error_code"] == "INCOMPLETE_BATCH_ITEMS"
    assert d_trunc["manifest_status"] == "PENDING_IMPORT"
    assert (tasks_dir / "retry_tasks.jsonl").is_file()

    # 3. 完整回传 3 题 -> 全部校验入库，manifest 流转 IMPORTED
    full_results = tmp_path / "s2_results_full.jsonl"
    full_results.write_text(
        json.dumps(
            {
                "batch_id": "stage2_batch_0001",
                "result": {
                    "items": [
                        _sample_stage2_item("s2_0001"),
                        _sample_stage2_item("s2_0002"),
                        _sample_stage2_item("s2_0003"),
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    r_imp_ok = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "2",
            "--mode",
            "import",
            "--grade-subject",
            "小学奥数",
            "--tasks-dir",
            str(tasks_dir),
            "--results",
            str(full_results),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_imp_ok.exit_code == 0, r_imp_ok.output
    d_ok = json.loads(r_imp_ok.stdout)["data"]
    assert d_ok["ingested"] == 3
    assert d_ok["batch_errors"] == 0
    assert d_ok["failed"] == 0
    assert d_ok["manifest_status"] == "IMPORTED"

    with closing(get_connection(db_path, readonly=True)) as conn:
        for uid in ("primary_olympiad:ch02:q1", "primary_olympiad:ch02:q2", "primary_olympiad:ch02:q3"):
            cs2 = conn.execute(
                "SELECT status, model_name FROM compile_state WHERE question_uid = ? AND stage = 'S2'",
                (uid,),
            ).fetchone()
            assert cs2["status"] == "OK"
            assert cs2["model_name"] == "subagent:gemini-4-neon-medium"
            steps_cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM question_steps WHERE question_uid = ?", (uid,)
            ).fetchone()["cnt"]
            assert steps_cnt == 1

    # 4. 锁定后再次导出默认跳过全部已锁定题目（4 道锚点 + 3 道新题 = 7）
    r_reexp = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "2",
            "--mode",
            "export",
            "--grade-subject",
            "小学奥数",
            "--tasks-dir",
            str(tasks_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_reexp.exit_code == 0, r_reexp.output
    d_reexp = json.loads(r_reexp.stdout)["data"]
    assert d_reexp["exported_questions"] == 0
    assert d_reexp["skipped_locked"] == 7
