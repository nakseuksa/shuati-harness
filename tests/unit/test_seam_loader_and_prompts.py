"""Subagent 模型路由配置、三层提示词接缝引擎与 Prompt 构造器单元测试。"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from shuati.config import (
    KNOWN_SUBAGENT_MODELS,
    Settings,
    SubagentConfig,
    load_settings,
    resolve_subagent_model,
    validate_subagent_model_id,
)
from shuati.llm.prompt_builder import (
    STAGE1_PROMPT_VERSION,
    build_external_diagnose_messages,
    build_merge_judge_messages,
    build_multi_solution_judge_messages,
    build_prerequisite_dag_messages,
    build_qualitative_judge_messages,
    build_review_resolve_messages,
    build_stage1_messages,
    build_stage2_messages,
    build_stage3c_mapping_messages,
    build_stage3d_cluster_assign_messages,
    build_stage4_refine_messages,
    build_taxonomy_propose_messages,
    build_unified_s1_s2_messages,
    flatten_json_schema,
    render_prompt_preview,
)
from shuati.llm.seam_loader import (
    REQUIRED_BLOCKS,
    REQUIRED_PROMPT_VERSIONS,
    REQUIRED_STAGES,
    lint_prompt_seams,
    list_prompt_seams,
    load_prompt_seams,
    resolve_slug_for_subject,
)
from shuati.models.schemas import (
    DifficultyScoresOutput,
    ExternalDiagnosisOutput,
    Stage1Input,
    Stage2OnlyOutput,
    UnifiedS1S2Output,
)


def _make_jpeg_with_exif_orientation_6(path: Path) -> None:
    img = Image.new("RGB", (40, 20), color=(0, 128, 255))
    exif = img.getexif()
    exif[0x0112] = 6
    img.save(path, format="JPEG", exif=exif.tobytes())


def _extract_all_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
                    elif block.get("type") == "image_path":
                        parts.append(str(block.get("image_path", "")))
                    elif block.get("type") == "image_url":
                        parts.append(str(block.get("image_url", {}).get("url", ""))[:40])
    return "\n".join(parts)


def test_subagent_config_defaults_validation_and_resolution(tmp_path: Path) -> None:
    settings = load_settings(Path("config/settings.yaml"), env_path=tmp_path / ".env")
    assert settings.subagent.default_model == "gemini-4-neon-medium"
    assert settings.subagent.max_parallel_workers == 4
    assert settings.subagent.stage_models == {}
    assert "gemini-4-neon-medium" in KNOWN_SUBAGENT_MODELS

    # 拼写错误拦截与标准建议
    with pytest.raises(ValueError, match="gemini-4-neon-medium"):
        validate_subagent_model_id("gemini-4-neno-medium")

    with pytest.raises(ValueError, match="claude-sonnet-5-5-high"):
        validate_subagent_model_id("claude-sonet-5-5-high")

    with pytest.raises(ValueError):
        validate_subagent_model_id("")

    with pytest.raises(ValueError):
        validate_subagent_model_id("bad model id!")

    with pytest.raises(ValidationError):
        SubagentConfig(default_model="gemini-4-neno-medium")

    # 允许合法自定义 slug（非已知拼写近似）
    assert validate_subagent_model_id("  deepseek-reasoner  ") == "deepseek-reasoner"

    # 解析优先级：cli_override > stage_models[stage] > default_model
    custom_settings = Settings(
        subagent=SubagentConfig(
            default_model="gemini-4-neon-medium",
            max_parallel_workers=6,
            stage_models={"stage3d": "gemini-4-carbon-high", "stage4": "claude-sonnet-5-5-medium"},
        )
    )
    assert resolve_subagent_model(custom_settings, "stage2") == "gemini-4-neon-medium"
    assert resolve_subagent_model(custom_settings, "stage3d") == "gemini-4-carbon-high"
    assert resolve_subagent_model(custom_settings, "STAGE4") == "claude-sonnet-5-5-medium"
    assert (
        resolve_subagent_model(custom_settings, "stage3d", cli_override="claude-opus-5-5-high")
        == "claude-opus-5-5-high"
    )
    with pytest.raises(ValueError, match="gemini-4-neon-medium"):
        resolve_subagent_model(custom_settings, "stage2", cli_override="gemini-4-neno-medium")


def test_repo_prompt_seams_lint_list_and_load() -> None:
    lint_res = lint_prompt_seams(Path("config"))
    assert lint_res["ok"] is True, f"lint errors={lint_res['errors']} warnings={lint_res['warnings']}"
    assert lint_res["errors"] == []
    assert lint_res["warnings"] == []
    assert "default.yaml" in lint_res["checked_files"]
    assert "subjects/primary_olympiad.yaml" in lint_res["checked_files"]
    assert "snippets/primary_olympiad_l3_examples.md" in lint_res["checked_files"]

    listing = list_prompt_seams(Path("config"))
    assert listing["default_exists"] is True
    assert any(s["slug"] == "primary_olympiad" for s in listing["subjects"])
    assert any(sn["path"] == "snippets/primary_olympiad_l3_examples.md" for sn in listing["snippets"])

    assert resolve_slug_for_subject("小学奥数", Path("config")) == "primary_olympiad"
    bundle = load_prompt_seams(config_dir=Path("config"), grade_subject="小学奥数")
    assert bundle.slug == "primary_olympiad"
    assert bundle.chain == ["default.yaml", "subjects/primary_olympiad.yaml"]
    assert set(REQUIRED_BLOCKS).issubset(bundle.blocks.keys())
    assert set(REQUIRED_STAGES).issubset(bundle.stages.keys())
    assert set(REQUIRED_PROMPT_VERSIONS).issubset(bundle.prompt_versions.keys())
    assert len(bundle.prompt_digest) == 16
    assert "等高模型面积比转化" in bundle.blocks["steps_and_socratic_hint"]
    assert bundle.sources["blocks.steps_and_socratic_hint"] == [
        "default.yaml",
        "subjects/primary_olympiad.yaml",
    ]
    # 严禁在内置 seam 中出现防泄漏测试拦截词
    all_seam_text = "\n".join(list(bundle.blocks.values()) + list(bundle.stages.values()))
    assert "source_collection" not in all_seam_text
    assert "chapter" not in all_seam_text.lower()
    assert re.search(r"ch\d+_", all_seam_text) is None


def test_seam_loader_inheritance_directives_and_path_traversal_guard(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    prompts_dir = cfg_dir / "prompts"
    (prompts_dir / "subjects").mkdir(parents=True)
    (prompts_dir / "profiles").mkdir(parents=True)
    (prompts_dir / "snippets").mkdir(parents=True)
    (cfg_dir / "secret.md").write_text("SECRET_OUTSIDE_PROMPTS", encoding="utf-8")

    (prompts_dir / "snippets" / "base_persona.md").write_text("BASE_FILE_PERSONA", encoding="utf-8")
    (prompts_dir / "snippets" / "extra_hint.md").write_text("SNIPPET_APPEND_HINT", encoding="utf-8")
    (prompts_dir / "snippets" / "profile_stage4.md").write_text("PROFILE_FILE_STAGE4", encoding="utf-8")

    default_yaml = """
schema_version: 1
prompt_versions:
  stage1: "s1_v2"
  stage2: "s2_v1"
  unified: "unified-v1.0"
  stage3a: "s3a_v1"
  stage3c: "s3c_v1"
  stage3d: "s3d_v1"
  stage3e: "s3e_v1"
  stage4: "s4_v1"
  diagnose: "diag_v2"
blocks:
  persona:
    file: "snippets/base_persona.md"
  visual_extraction: "BASE_VISUAL"
  solving_conventions: "BASE_SOLVING"
  steps_and_socratic_hint: "BASE_STEPS"
  abstraction_and_tagging: "BASE_ABS"
  difficulty_rubric_notes: "BASE_DIFF"
stages:
  stage1_judge: "BASE_S1J"
  stage3_propose: "BASE_S3P"
  stage3c_mapping: "BASE_S3C"
  stage3d_cluster_assign: "BASE_S3D"
  stage3e_prerequisite: "BASE_S3E"
  stage4_refine: "BASE_S4R"
"""
    (prompts_dir / "default.yaml").write_text(default_yaml, encoding="utf-8")

    (prompts_dir / "subjects" / "junior_physics.yaml").write_text(
        """
grade_subject: "初中物理"
slug: "junior_physics"
blocks:
  visual_extraction:
    replace: "PHYS_VISUAL"
  steps_and_socratic_hint:
    append_file: "snippets/extra_hint.md"
""",
        encoding="utf-8",
    )

    (prompts_dir / "profiles" / "strict.yaml").write_text(
        """
description: "严格模式"
prompt_versions:
  unified: "unified-v2.0"
blocks:
  solving_conventions:
    append: "STRICT_SOLVING_APPENDIX"
stages:
  stage4_refine:
    file: "snippets/profile_stage4.md"
""",
        encoding="utf-8",
    )

    assert resolve_slug_for_subject("初中物理", cfg_dir) == "junior_physics"
    b1 = load_prompt_seams(config_dir=cfg_dir, grade_subject="初中物理")
    assert b1.blocks["persona"] == "BASE_FILE_PERSONA"
    assert b1.blocks["visual_extraction"] == "PHYS_VISUAL"
    assert b1.blocks["steps_and_socratic_hint"] == "BASE_STEPS\n\nSNIPPET_APPEND_HINT"

    b2 = load_prompt_seams(config_dir=cfg_dir, grade_subject="初中物理", profile="strict")
    assert b2.chain == ["default.yaml", "subjects/junior_physics.yaml", "profiles/strict.yaml"]
    assert b2.prompt_versions["unified"] == "unified-v2.0"
    assert b2.blocks["solving_conventions"] == "BASE_SOLVING\n\nSTRICT_SOLVING_APPENDIX"
    assert b2.stages["stage4_refine"] == "PROFILE_FILE_STAGE4"
    assert b2.prompt_digest != b1.prompt_digest
    assert b2.sources["blocks.solving_conventions"] == ["default.yaml", "profiles/strict.yaml"]

    # 越界路径拦截（../secret.md）
    (prompts_dir / "profiles" / "evil.yaml").write_text(
        'blocks:\n  persona:\n    file: "../secret.md"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="越界"):
        load_prompt_seams(config_dir=cfg_dir, grade_subject="初中物理", profile="evil")

    lint_evil = lint_prompt_seams(cfg_dir)
    assert lint_evil["ok"] is False
    assert any("evil.yaml" in e["file"] for e in lint_evil["errors"])

    # 未声明的 profile 报错
    with pytest.raises(FileNotFoundError):
        load_prompt_seams(config_dir=cfg_dir, grade_subject="初中物理", profile="not_exists")


def test_flatten_json_schema_inlines_refs_and_locks_bounds() -> None:
    for cls in (DifficultyScoresOutput, Stage2OnlyOutput, UnifiedS1S2Output, ExternalDiagnosisOutput):
        flat = flatten_json_schema(cls)
        assert "$defs" not in flat
        assert "definitions" not in flat
        dumped = json.dumps(flat, ensure_ascii=False)
        assert '"$ref"' not in dumped
        assert '"$defs"' not in dumped

        if "difficulty" in flat.get("properties", {}):
            diff_props = flat["properties"]["difficulty"]["properties"]
        else:
            diff_props = flat["properties"]

        c_dim = diff_props["C_concept_count"]
        assert c_dim["properties"]["score"]["minimum"] == 1
        assert c_dim["properties"]["score"]["maximum"] == 4
        assert list(c_dim["properties"].keys())[0] == "evidence"

        for s_key in ("S_chain_length", "T_trickiness", "L_computation_load"):
            dim = diff_props[s_key]
            assert dim["properties"]["score"]["minimum"] == 1
            assert dim["properties"]["score"]["maximum"] == 3
            assert list(dim["properties"].keys())[0] == "evidence"

    u_flat = flatten_json_schema(UnifiedS1S2Output)
    assert list(u_flat["properties"].keys())[0] == "visual_facts"


def test_stage2_unified_stage3d_and_preview_builders(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir(parents=True)
    img_path = assets_dir / "cas_0001.jpg"
    _make_jpeg_with_exif_orientation_6(img_path)

    # 1. build_stage2_messages: 有图 + D-Audit-4 人工视觉/答案最高权威
    s2_msgs = build_stage2_messages(
        grade_subject="小学奥数",
        question_text="如图，求正方形内阴影面积。",
        visual_facts="正方形 ABCD 边长 4，连接 AC，阴影为三角形 ABC。",
        verified_final_answers=[
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "8",
                "sympy_expr": "8",
            }
        ],
        anchors_prompt_text="Anchor-1 C=1, S=1, T=1, L=1",
        has_images=True,
        image_paths=["cas_0001.jpg"],
        image_captions=["图1"],
        assets_dir=assets_dir,
        embed_images=True,
        has_human_visual_override=True,
        has_human_answer_override=True,
        task_id="T0009",
    )
    assert "D-Audit-4 人工视觉事实最高权威" in s2_msgs[0]["content"]
    assert "D-Audit-4 人工标准答案最高权威" in s2_msgs[0]["content"]
    assert isinstance(s2_msgs[1]["content"], list)
    img_blocks = [b for b in s2_msgs[1]["content"] if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    raw_bytes = base64.b64decode(img_blocks[0]["image_url"]["url"].split(",", 1)[1])
    with Image.open(io.BytesIO(raw_bytes)) as decoded:
        assert decoded.size == (20, 40)

    # 2. build_unified_s1_s2_messages: embed_images=False 轻量描述 + 防泄漏白名单
    decoy_payload = {
        "task_id": "T0042",
        "mode": "UNIFIED_S1_S2",
        "grade_subject": "小学奥数",
        "allow_pi_314": True,
        "question_text": "求半径为 2 的圆周长。",
        "has_images": True,
        "image_paths": ["data/assets/images/cas_0001.jpg"],
        "image_captions": ["示意图"],
        # 故意注入禁止泄漏字段，验证白名单过滤
        "question_uid": "primary_olympiad:ch01_秘密章节:ch01_042",
        "source_collection": "ch01_秘密章节",
        "raw_chapter": "秘密章节",
        "legacy_tags": ["禁止标签"],
    }
    u_msgs, u_bundle = build_unified_s1_s2_messages(
        task_payload=decoy_payload,
        anchors_prompt_text="Anchor-1 C=1, S=1, T=1, L=1",
        config_dir=Path("config"),
        embed_images=False,
    )
    assert u_bundle.slug == "primary_olympiad"
    assert len(u_bundle.prompt_digest) == 16
    assert isinstance(u_msgs[1]["content"], list)
    path_blocks = [b for b in u_msgs[1]["content"] if b.get("type") == "image_path"]
    assert len(path_blocks) == 1
    assert path_blocks[0]["image_path"] == "data/assets/images/cas_0001.jpg"

    all_u_text = _extract_all_text(u_msgs)
    assert "秘密章节" not in all_u_text
    assert "禁止标签" not in all_u_text
    assert "primary_olympiad:ch01" not in all_u_text
    assert "def verify():" in u_msgs[0]["content"]
    assert "TRAP_DOMAIN_BOUNDARY" in u_msgs[0]["content"]

    # 3. build_stage3d_cluster_assign_messages
    s3d_msgs = build_stage3d_cluster_assign_messages(
        grade_subject="小学奥数",
        l1_module="平面几何",
        l2_entity="等高模型",
        raw_l3_items=[
            {"raw_l3": "等高三角形面积比转化", "freq": 12, "question_uid": "LEAK_UID"},
            {"raw_l3": "同高三角形底边比例转化", "freq": 4},
        ],
        taxonomy_version="1.0",
    )
    assert "Cluster-then-Assign" in s3d_msgs[0]["content"]
    assert "LEAK_UID" not in s3d_msgs[1]["content"]
    assert "等高三角形面积比转化" in s3d_msgs[1]["content"]

    # 4. render_prompt_preview
    for st in ("stage1", "stage2", "unified", "diagnose", "stage3a", "stage3c", "stage3d", "stage3e", "stage4"):
        preview = render_prompt_preview(stage=st, grade_subject="小学奥数", config_dir=Path("config"))
        assert preview["prompt_digest"] == u_bundle.prompt_digest
        assert preview["prompt_version"]
        assert len(preview["messages"]) == 2
        if st in ("stage2", "unified"):
            assert "flattened_schema" in preview
            assert "$defs" not in preview["flattened_schema"]

    with pytest.raises(ValueError, match="未知"):
        render_prompt_preview(stage="unknown_stage", grade_subject="小学奥数", config_dir=Path("config"))


def test_existing_builders_backward_compatible_and_profile_aware(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    prompts_dir = cfg_dir / "prompts"
    (prompts_dir / "profiles").mkdir(parents=True)
    # 拷贝真实 default.yaml 后增加一个测试 profile
    (prompts_dir / "default.yaml").write_text(
        Path("config/prompts/default.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (prompts_dir / "profiles" / "custom_prof.yaml").write_text(
        'blocks:\n  persona:\n    append: "CUSTOM_PROFILE_MARKER_XYZ"\nstages:\n  stage4_refine:\n    append: "CUSTOM_S4_MARKER"\n',
        encoding="utf-8",
    )

    assert STAGE1_PROMPT_VERSION == "s1_v2"
    inp = Stage1Input(
        question_uid="primary_olympiad:ch01:ch01_001",
        grade_subject="小学奥数",
        question_text="求 1+2。",
        has_images=False,
    )
    s1_msgs = build_stage1_messages(
        inp,
        role="PRIMARY",
        assets_dir=tmp_path,
        config_dir=cfg_dir,
        prompt_profile="custom_prof",
    )
    assert "CUSTOM_PROFILE_MARKER_XYZ" in s1_msgs[0]["content"]

    assert len(build_qualitative_judge_messages("(1)", "A", "B", "Q", config_dir=cfg_dir)) == 2
    assert len(build_multi_solution_judge_messages("A", "sa", "B", "sb", config_dir=cfg_dir)) == 2
    assert len(build_review_resolve_messages("Q", "", "小学奥数", config_dir=cfg_dir)) == 2
    assert (
        len(
            build_taxonomy_propose_messages(
                grade_subject="小学奥数",
                clusters_summary=[],
                top_pairs=[],
                config_dir=cfg_dir,
            )
        )
        == 2
    )
    assert (
        len(
            build_stage3c_mapping_messages(
                grade_subject="小学奥数",
                taxonomy_enum_text="- 几何: 等高",
                question_items=[],
                config_dir=cfg_dir,
            )
        )
        == 2
    )
    assert (
        len(
            build_merge_judge_messages(
                target_kind="KNOWLEDGE_NODE",
                scope_desc="几何/等高",
                candidate_pairs=[],
                config_dir=cfg_dir,
            )
        )
        == 2
    )
    assert (
        len(
            build_prerequisite_dag_messages(
                grade_subject="小学奥数",
                taxonomy_enum_text="- 几何: 等高",
                ordered_transitions=[],
                config_dir=cfg_dir,
            )
        )
        == 2
    )
    s4_msgs = build_stage4_refine_messages(
        candidate_pairs=[],
        config_dir=cfg_dir,
        prompt_profile="custom_prof",
    )
    assert "CUSTOM_S4_MARKER" in s4_msgs[0]["content"]
    diag_msgs = build_external_diagnose_messages(
        grade_subject="小学奥数",
        question_text="求面积。",
        taxonomy_enum_text="- 几何: 等高",
        config_dir=cfg_dir,
        prompt_profile="custom_prof",
    )
    assert "CUSTOM_PROFILE_MARKER_XYZ" in diag_msgs[0]["content"]
