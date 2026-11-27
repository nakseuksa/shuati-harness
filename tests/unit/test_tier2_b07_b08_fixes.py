"""Tier 2 (B07 + B08) 外部审查修复单元测试集（全量 tmp_path 隔离，覆盖 B07-F1..F10 与 B08-F1..F7）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from shuati.config import Settings
from shuati.core import effective
from shuati.core.taxonomy import TaxonomyL1Entry, TaxonomyL2Entry, TaxonomySpec, save_taxonomy_spec
from shuati.db import human_mirror
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.models.domain import AnswerPart
from shuati.models.schemas import QualitativeJudgeOutput
from shuati.services import diagnose_batch, mistake
from shuati.services.diagnose_batch import (
    _escape_md_cell,
    _format_answers_inline,
    _load_reusable_draft_index,
    diagnose_external_file_async,
    parse_question_list_markdown,
    preflight_question_list_markdown,
    render_batch_markdown_report,
)
from shuati.services.mistake import (
    _build_taxonomy_enum_summary,
    _parse_single_known_answer,
    _prune_expired_drafts,
    compute_diagnose_content_hash,
    diagnose_external_mistake_async,
    ingest_external_mistake_async,
)
from shuati.vectors.store import VectorStore
from shuati.verify.equivalence import compare_answer_parts


def _make_isolated_env(tmp_path: Path) -> dict[str, Path]:
    """为单测构造完全隔离在 tmp_path 下的全部工作目录与配置副本。"""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    repo_cfg = Path("config")
    settings_txt = (repo_cfg / "settings.yaml").read_text(encoding="utf-8")
    settings_txt = settings_txt.replace("use_mock_llm: false", "use_mock_llm: true")
    (cfg_dir / "settings.yaml").write_text(settings_txt, encoding="utf-8")
    if (repo_cfg / "method_blacklist.yaml").exists():
        shutil.copy2(repo_cfg / "method_blacklist.yaml", cfg_dir / "method_blacklist.yaml")
    if (repo_cfg / "prompts").is_dir():
        shutil.copytree(repo_cfg / "prompts", cfg_dir / "prompts", dirs_exist_ok=True)

    dirs = {
        "db_path": tmp_path / "data" / "shuati.db",
        "config_dir": cfg_dir,
        "human_dir": tmp_path / "human",
        "backups_dir": tmp_path / "data" / "backups",
        "vectors_dir": tmp_path / "data" / "vectors",
        "drafts_dir": tmp_path / "data" / "drafts",
        "assets_dir": tmp_path / "data" / "assets" / "images",
        "reports_dir": tmp_path / "data" / "reports",
        "lock_path": tmp_path / "data" / ".compile.lock",
    }
    return dirs


def _mock_settings() -> Settings:
    settings = Settings()
    settings.pipeline.use_mock_llm = True
    return settings


def _make_diag_payload(
    *,
    primary_l1: str = "平面几何",
    secondary_l1s: list[str] | None = None,
    raw_l1: str = "平面几何",
    raw_l2: str = "三角形面积",
    raw_l3: str = "等底等高转化",
    final_answers: list[dict[str, Any]] | None = None,
    sympy_code: str | None = "def verify():\n    return {'主问': str(6 * 4)}\n",
    trap_tags: list[str] | None = None,
) -> dict[str, Any]:
    if final_answers is None:
        final_answers = [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "24",
                "unit": None,
                "sympy_expr": "24",
                "items": None,
                "choice_label": None,
                "canonical": None,
            }
        ]
    return {
        "visual_facts": "图中有矩形和阴影三角形。",
        "visual_ambiguity_flag": False,
        "visual_ambiguity_note": "",
        "method_summary": "利用底高关系求面积",
        "solution_markdown": "面积为 $6 \\times 4 = 24$。",
        "final_answers": final_answers,
        "sympy_code": sympy_code,
        "steps": [
            {
                "step_index": 1,
                "sub_goal": "求阴影面积",
                "core_formula": "S = a * h",
                "hint": "观察底与高的关系。",
                "raw_l1": raw_l1,
                "raw_l2": raw_l2,
                "raw_l3": raw_l3,
            }
        ],
        "abstract_skeleton": "已知几何图形底与高求面积。",
        "primary_l1_module": primary_l1,
        "secondary_l1_modules": secondary_l1s or [],
        "matched_l2": [raw_l2],
        "thinking_methods": ["等积变形"],
        "classic_motifs": ["三角形等高模型"],
        "trap_tags": trap_tags if trap_tags is not None else [],
        "difficulty": {
            "C_concept_count": {"evidence": "单一面积公式", "score": 2},
            "S_chain_length": {"evidence": "两步推导", "score": 1},
            "T_trickiness": {"evidence": "直接看图", "score": 1},
            "L_computation_load": {"evidence": "简单乘法", "score": 1},
        },
    }


def _make_secondary_payload(final_answers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if final_answers is None:
        final_answers = [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "24",
                "unit": None,
                "sympy_expr": "24",
                "items": None,
                "choice_label": None,
                "canonical": None,
            }
        ]
    return {
        "visual_facts": "图中有阴影三角形。",
        "visual_ambiguity_flag": False,
        "visual_ambiguity_note": "",
        "method_summary": "直接计算底高乘积",
        "solution_markdown": "计算得 24。",
        "final_answers": final_answers,
    }


def _save_png(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 24), color=color).save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# B07-F1: 内容字节哈希去重与同名异图隔离
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b07_f1_content_hash_dedup_and_no_same_filename_collision(tmp_path: Path) -> None:
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    settings = _mock_settings()

    img_a = _save_png(tmp_path / "dir_a" / "photo.jpg", (255, 0, 0))
    img_b = _save_png(tmp_path / "dir_b" / "photo.jpg", (0, 0, 255))
    img_a_copy = tmp_path / "dir_c" / "renamed_same_bytes.jpg"
    img_a_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(img_a, img_a_copy)

    q_text = "如图，求阴影部分面积。"
    diag_geo = _make_diag_payload(primary_l1="平面几何")
    diag_nt = _make_diag_payload(primary_l1="数论")
    sec_ok = _make_secondary_payload()

    try:
        # 1. 同一题干 + 同一图片，经两次 diagnose 生成不同随机 drf_<uuid>_1.jpg，再与直传/改名拷贝入库 -> UID 全部一致
        d1 = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text=q_text,
            grade_subject="小学奥数",
            image_paths=[img_a],
            diagnosis_json=diag_geo,
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            use_mock=True,
        )
        d2 = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text=q_text,
            grade_subject="小学奥数",
            image_paths=[img_a],
            diagnosis_json=diag_geo,
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            use_mock=True,
        )
        r_d1 = await ingest_external_mistake_async(
            conn,
            settings,
            draft_id=d1["draft_id"],
            secondary_json=sec_ok,
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        r_d2 = await ingest_external_mistake_async(
            conn,
            settings,
            draft_id=d2["draft_id"],
            secondary_json=sec_ok,
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        r_direct = await ingest_external_mistake_async(
            conn,
            settings,
            question_text=q_text,
            grade_subject="小学奥数",
            image_paths=[img_a],
            diagnosis_json=diag_geo,
            secondary_json=sec_ok,
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        r_renamed = await ingest_external_mistake_async(
            conn,
            settings,
            question_text=q_text,
            grade_subject="小学奥数",
            image_paths=[img_a_copy],
            diagnosis_json=diag_geo,
            secondary_json=sec_ok,
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )

        assert r_d1["question_uid"] == r_d2["question_uid"] == r_direct["question_uid"] == r_renamed["question_uid"]
        assert conn.execute("SELECT COUNT(*) FROM questions;").fetchone()[0] == 1

        # 2. 同名异图 dir_b/photo.jpg 不得与 dir_a/photo.jpg 碰撞或覆盖前题
        r_b = await ingest_external_mistake_async(
            conn,
            settings,
            question_text=q_text,
            grade_subject="小学奥数",
            image_paths=[img_b],
            diagnosis_json=diag_nt,
            secondary_json=sec_ok,
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert r_b["question_uid"] != r_d1["question_uid"]
        assert conn.execute("SELECT COUNT(*) FROM questions;").fetchone()[0] == 2
        l1_a = conn.execute(
            "SELECT eff_primary_l1_module FROM questions WHERE question_uid = ?;", (r_d1["question_uid"],)
        ).fetchone()["eff_primary_l1_module"]
        l1_b = conn.execute(
            "SELECT eff_primary_l1_module FROM questions WHERE question_uid = ?;", (r_b["question_uid"],)
        ).fetchone()["eff_primary_l1_module"]
        assert l1_a == "平面几何"
        assert l1_b == "数论"

        # 3. 覆写草稿题干时不再误复用旧 content_hash
        r_mod = await ingest_external_mistake_async(
            conn,
            settings,
            draft_id=d1["draft_id"],
            question_text="修改后的不同题干。",
            dry_run=True,
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert r_mod["question_uid"] != r_d1["question_uid"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B07-F2: 词表激活时题级主/次 L1 归一与过滤
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b07_f2_l1_and_secondary_l1_normalized_against_locked_taxonomy(tmp_path: Path) -> None:
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    settings = _mock_settings()

    tax_path = env["config_dir"] / "taxonomy_primary_olympiad_locked.yaml"
    spec = TaxonomySpec(
        schema_version=1,
        grade_subject="小学奥数",
        slug="primary_olympiad",
        status="LOCKED",
        version="1.0",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                aliases=["几何"],
                l2_entities=[TaxonomyL2Entry(name="等高模型", aliases=["三角形面积"])],
            ),
            TaxonomyL1Entry(
                name="数论",
                aliases=["数论基础"],
                l2_entities=[TaxonomyL2Entry(name="整除特征", aliases=[])],
            ),
        ],
    )
    save_taxonomy_spec(spec, tax_path)
    conn.execute(
        """
        UPDATE grade_subjects
        SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0', taxonomy_file = ?
        WHERE grade_subject = '小学奥数';
        """,
        (str(tax_path),),
    )
    conn.commit()

    try:
        # Case 1: primary 为别名 "几何"，secondary 含非法项、与主模块冲突项、别名重复项
        diag1 = _make_diag_payload(
            primary_l1="几何",
            secondary_l1s=["不存在模块", "几何", "数论基础", "数论"],
            raw_l1="几何",
            raw_l2="三角形面积",
        )
        res1 = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="已知三角形底为6高为8求面积。",
            grade_subject="小学奥数",
            diagnosis_json=diag1,
            secondary_json=_make_secondary_payload(),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        q1 = conn.execute(
            "SELECT m_primary_l1_module, eff_primary_l1_module, eff_secondary_l1_modules_json FROM questions WHERE question_uid = ?;",
            (res1["question_uid"],),
        ).fetchone()
        s1 = conn.execute(
            "SELECT m_l1, m_l2 FROM question_steps WHERE question_uid = ?;",
            (res1["question_uid"],),
        ).fetchone()
        assert q1["m_primary_l1_module"] == "平面几何"
        assert q1["eff_primary_l1_module"] == "平面几何"
        assert json.loads(q1["eff_secondary_l1_modules_json"]) == ["数论"]
        assert s1["m_l1"] == "平面几何"
        assert s1["m_l2"] == "等高模型"

        # Case 2: primary 非法 -> 回退至步骤已归一 L1（数论）
        diag2 = _make_diag_payload(
            primary_l1="胡编模块",
            secondary_l1s=["几何", "胡编次模块"],
            raw_l1="数论基础",
            raw_l2="整除特征",
        )
        res2 = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="求满足整除特征的最小正整数。",
            grade_subject="小学奥数",
            diagnosis_json=diag2,
            secondary_json=_make_secondary_payload(),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        q2 = conn.execute(
            "SELECT eff_primary_l1_module, eff_secondary_l1_modules_json FROM questions WHERE question_uid = ?;",
            (res2["question_uid"],),
        ).fetchone()
        assert q2["eff_primary_l1_module"] == "数论"
        assert json.loads(q2["eff_secondary_l1_modules_json"]) == ["平面几何"]

        # Case 3: primary 与步骤均非法 -> 回退至 tax_spec.modules[0].name
        diag3 = _make_diag_payload(
            primary_l1="完全未知A",
            secondary_l1s=["完全未知B"],
            raw_l1="完全未知C",
            raw_l2="完全未知L2",
        )
        res3 = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="求未知模块的数值结果。",
            grade_subject="小学奥数",
            diagnosis_json=diag3,
            secondary_json=_make_secondary_payload(),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        q3 = conn.execute(
            "SELECT eff_primary_l1_module, eff_secondary_l1_modules_json FROM questions WHERE question_uid = ?;",
            (res3["question_uid"],),
        ).fetchone()
        assert q3["eff_primary_l1_module"] == "平面几何"
        assert json.loads(q3["eff_secondary_l1_modules_json"]) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B07-F3: _parse_single_known_answer 支持多值集合与多小问 SYMBOL_FILL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b07_f3_parse_known_answer_collections_and_multipart_symbol_fill(tmp_path: Path) -> None:
    # 1. 单小问 UNORDERED_MULTISET
    ref_multi = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="UNORDERED_MULTISET",
            answer="",
            items=[{"canonical": "2", "sympy_expr": "2"}, {"canonical": "3", "sympy_expr": "3"}],
        )
    ]
    parsed_multi = _parse_single_known_answer("3, 2", ref_multi)
    assert parsed_multi[0].collection_mode == "UNORDERED_MULTISET"
    assert parsed_multi[0].verify_mode == "SYMBOLIC"
    eq_multi = await compare_answer_parts(
        ref_multi, parsed_multi, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
    )
    assert eq_multi.all_dual_equal is True
    assert eq_multi.review_reasons == []

    # 2. 单小问 ORDERED_TUPLE（含括号拆包与顺序敏感校验）
    ref_tuple = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="ORDERED_TUPLE",
            answer="",
            items=[{"expr": "2"}, {"expr": "3"}],
        )
    ]
    eq_tuple_ok = await compare_answer_parts(
        ref_tuple,
        _parse_single_known_answer("(2, 3)", ref_tuple),
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
    )
    eq_tuple_bad = await compare_answer_parts(
        ref_tuple,
        _parse_single_known_answer("(3, 2)", ref_tuple),
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
    )
    assert eq_tuple_ok.all_dual_equal is True
    assert eq_tuple_bad.all_dual_equal is False

    # 3. 单小问 LABELED_MAP（键值与比值位置模式）
    ref_map = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[{"label": "鸡", "expr": "12"}, {"label": "兔", "expr": "8"}],
        )
    ]
    eq_map = await compare_answer_parts(
        ref_map,
        _parse_single_known_answer("兔=8, 鸡=12", ref_map),
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
    )
    assert eq_map.all_dual_equal is True

    ref_ratio_map = [
        AnswerPart(
            label="主问",
            answer_type="RATIO",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[{"label": "甲乙比", "expr": "3:4"}, {"label": "乙丙比", "expr": "1:2"}],
        )
    ]
    eq_ratio_map = await compare_answer_parts(
        ref_ratio_map,
        _parse_single_known_answer("3:4, 1:2", ref_ratio_map),
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
    )
    assert eq_ratio_map.all_dual_equal is True

    # 4. 多小问 SYMBOL_FILL（支持半角与中文词）
    ref_syms = [
        AnswerPart(
            label="(1)",
            answer_type="SYMBOL_FILL",
            verify_mode="EXACT",
            collection_mode="SINGLE",
            answer=">",
            canonical=">",
        ),
        AnswerPart(
            label="(2)",
            answer_type="SYMBOL_FILL",
            verify_mode="EXACT",
            collection_mode="SINGLE",
            answer="<",
            canonical="<",
        ),
    ]
    for raw_ans in (">; <", "大于；小于"):
        parsed_syms = _parse_single_known_answer(raw_ans, ref_syms)
        assert [p.answer_type for p in parsed_syms] == ["SYMBOL_FILL", "SYMBOL_FILL"]
        assert [p.verify_mode for p in parsed_syms] == ["EXACT", "EXACT"]
        eq_syms = await compare_answer_parts(
            ref_syms, parsed_syms, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
        )
        assert eq_syms.all_dual_equal is True
        assert eq_syms.review_reasons == []

    # 5. 多值 SYMBOL_FILL (ORDERED_TUPLE)
    ref_sym_tuple = [
        AnswerPart(
            label="主问",
            answer_type="SYMBOL_FILL",
            verify_mode="EXACT",
            collection_mode="ORDERED_TUPLE",
            answer="",
            items=[
                {"item_type": "SYMBOL_FILL", "canonical": ">"},
                {"item_type": "SYMBOL_FILL", "canonical": "="},
            ],
        )
    ]
    eq_sym_tuple = await compare_answer_parts(
        ref_sym_tuple,
        _parse_single_known_answer("大于, 等于", ref_sym_tuple),
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
    )
    assert eq_sym_tuple.all_dual_equal is True

    # 6. 非法 CHOICE 输入不抛 ValueError，而是干净返回不匹配
    ref_choice = [
        AnswerPart(
            label="主问",
            answer_type="CHOICE",
            verify_mode="EXACT",
            collection_mode="SINGLE",
            answer="B",
            choice_label="B",
        )
    ]
    eq_bad_choice = await compare_answer_parts(
        ref_choice,
        _parse_single_known_answer("123", ref_choice),
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
    )
    assert eq_bad_choice.all_dual_equal is False

    # 7. 端到端 ingest 带多值 --answer 保持 HUMAN_VERIFIED 可服务
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    try:
        multi_fa = [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "UNORDERED_MULTISET",
                "answer": "2, 3",
                "items": [{"canonical": "2", "sympy_expr": "2"}, {"canonical": "3", "sympy_expr": "3"}],
            }
        ]
        diag_m = _make_diag_payload(
            final_answers=multi_fa,
            sympy_code=(
                "import sympy as sp\n"
                "def verify():\n"
                "    x = sp.symbols('x')\n"
                "    sols = sorted(sp.solve((x - 2) * (x - 3), x))\n"
                "    return {'主问': [str(s) for s in sols]}\n"
            ),
        )
        res = await ingest_external_mistake_async(
            conn,
            _mock_settings(),
            question_text="解方程 (x-2)(x-3)=0。",
            grade_subject="小学奥数",
            known_answer="2, 3",
            diagnosis_json=diag_m,
            secondary_json=_make_secondary_payload(multi_fa),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert res["verification_status"] == "HUMAN_VERIFIED"
        assert res["eff_is_servable"] is True
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_b07_f3_labeled_map_operators_reordering_and_colon_priority(tmp_path: Path) -> None:
    """Round 2 BC-1 回归：LABELED_MAP 含 =/>=/<=/!=、重排 SYMBOL_FILL 键与冒号优先。"""
    # 1. SYMBOL_FILL + LABELED_MAP 位置输入、乱序冒号键值与匹配键名的 `=` 键值
    ref_sym_map = [
        AnswerPart(
            label="主问",
            answer_type="SYMBOL_FILL",
            verify_mode="EXACT",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "空1", "item_type": "SYMBOL_FILL", "canonical": ">="},
                {"label": "空2", "item_type": "SYMBOL_FILL", "canonical": "="},
            ],
        )
    ]
    for raw_sym in (">=, =", "空2: =, 空1: >=", "空2：=, 空1：>=", "空2 = =, 空1 = >="):
        parsed = _parse_single_known_answer(raw_sym, ref_sym_map)
        eq = await compare_answer_parts(
            ref_sym_map, parsed, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
        )
        assert eq.all_dual_equal is True, f"raw={raw_sym!r}, items={parsed[0].items!r}"

    ref_sym_ineq = [
        AnswerPart(
            label="主问",
            answer_type="SYMBOL_FILL",
            verify_mode="EXACT",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "空1", "item_type": "SYMBOL_FILL", "canonical": "<="},
                {"label": "空2", "item_type": "SYMBOL_FILL", "canonical": "!="},
            ],
        )
    ]
    eq_sym_ineq = await compare_answer_parts(
        ref_sym_ineq,
        _parse_single_known_answer("<=, !=", ref_sym_ineq),
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
    )
    assert eq_sym_ineq.all_dual_equal is True

    # 2. SYMBOLIC + LABELED_MAP 混合 SYMBOL_FILL、不等式与等式值（冒号优先于 =）
    ref_mixed_map = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "关系", "item_type": "SYMBOL_FILL", "canonical": ">="},
                {"label": "差值", "expr": "5"},
            ],
        )
    ]
    for raw_mix in ("关系: >=, 差值: 5", "关系：>=, 差值：5", ">=, 5", "差值=5, 关系=>="):
        eq_mix = await compare_answer_parts(
            ref_mixed_map,
            _parse_single_known_answer(raw_mix, ref_mixed_map),
            grade_subject="小学奥数",
            allow_pi_314_subjects=["小学奥数"],
        )
        assert eq_mix.all_dual_equal is True, f"raw={raw_mix!r}"

    ref_ineq_map = [
        AnswerPart(
            label="主问",
            answer_type="SET_OR_INTERVAL",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[{"label": "范围", "expr": "x >= 2"}],
        )
    ]
    for raw_ineq in ("范围: x >= 2", "x >= 2"):
        eq_ineq = await compare_answer_parts(
            ref_ineq_map,
            _parse_single_known_answer(raw_ineq, ref_ineq_map),
            grade_subject="小学奥数",
            allow_pi_314_subjects=["小学奥数"],
        )
        assert eq_ineq.all_dual_equal is True, f"raw={raw_ineq!r}"

    ref_eq_map = [
        AnswerPart(
            label="主问",
            answer_type="EXPRESSION",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[{"label": "直线", "expr": "y = 2*x + 1"}],
        )
    ]
    for raw_eq in ("直线: y = 2*x + 1", "y = 2*x + 1"):
        eq_expr = await compare_answer_parts(
            ref_eq_map,
            _parse_single_known_answer(raw_eq, ref_eq_map),
            grade_subject="小学奥数",
            allow_pi_314_subjects=["小学奥数"],
        )
        assert eq_expr.all_dual_equal is True, f"raw={raw_eq!r}"

    # 3. 端到端 ingest 带 SYMBOL_FILL LABELED_MAP known_answer=">=, =" 保持 HUMAN_VERIFIED
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    try:
        sym_fa = [
            {
                "label": "主问",
                "answer_type": "SYMBOL_FILL",
                "verify_mode": "EXACT",
                "collection_mode": "LABELED_MAP",
                "answer": "",
                "items": [
                    {"label": "空1", "item_type": "SYMBOL_FILL", "canonical": ">="},
                    {"label": "空2", "item_type": "SYMBOL_FILL", "canonical": "="},
                ],
            }
        ]
        res = await ingest_external_mistake_async(
            conn,
            _mock_settings(),
            question_text="在空1、空2中依次填入比较符号。",
            grade_subject="小学奥数",
            known_answer=">=, =",
            diagnosis_json=_make_diag_payload(final_answers=sym_fa, sympy_code=None),
            secondary_json=_make_secondary_payload(sym_fa),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert res["verification_status"] == "HUMAN_VERIFIED"
        assert res["eff_is_servable"] is True
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_b07_f3_bc2_ratio_eq_separator_and_wrong_keys_rejected(tmp_path: Path) -> None:
    """Round 3 BC-2 回归：RATIO LABELED_MAP 的 `=` 键值、全角 `＝` 及 NUMERIC/RATIO 错键拒绝。"""
    ref_ratio = [
        AnswerPart(
            label="主问",
            answer_type="RATIO",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "甲", "expr": "3:4"},
                {"label": "乙", "expr": "1:2"},
            ],
        )
    ]
    for raw_ok in ("甲=3:4, 乙=1:2", "甲 = 3:4, 乙 = 1:2", "乙=1:2, 甲=3:4", "甲＝3:4, 乙＝1:2", "甲: 3:4, 乙: 1:2"):
        parsed = _parse_single_known_answer(raw_ok, ref_ratio)
        eq = await compare_answer_parts(
            ref_ratio, parsed, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
        )
        assert eq.all_dual_equal is True, f"raw={raw_ok!r}, items={parsed[0].items!r}"

    # 错键 RATIO 必须被拒绝
    eq_ratio_wrong = await compare_answer_parts(
        ref_ratio,
        _parse_single_known_answer("丙=3:4, 丁=1:2", ref_ratio),
        grade_subject="小学奥数",
        allow_pi_314_subjects=["小学奥数"],
    )
    assert eq_ratio_wrong.all_dual_equal is False

    # 错键 NUMERIC 必须被拒绝（不得因回落位置键并被 SymPy 剥离等号左侧而误判相等）
    ref_num = [
        AnswerPart(
            label="主问",
            answer_type="NUMERIC",
            verify_mode="SYMBOLIC",
            collection_mode="LABELED_MAP",
            answer="",
            items=[
                {"label": "鸡", "expr": "12"},
                {"label": "兔", "expr": "8"},
            ],
        )
    ]
    parsed_wrong = _parse_single_known_answer("丙=12, 丁=8", ref_num)
    assert [it["label"] for it in (parsed_wrong[0].items or [])] == ["丙", "丁"]
    eq_num_wrong = await compare_answer_parts(
        ref_num, parsed_wrong, grade_subject="小学奥数", allow_pi_314_subjects=["小学奥数"]
    )
    assert eq_num_wrong.all_dual_equal is False

    # 端到端 ingest 带 RATIO LABELED_MAP + known_answer="甲=3:4, 乙=1:2" + 合法非空壳 sympy_code -> HUMAN_VERIFIED
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    try:
        ratio_fa = [
            {
                "label": "主问",
                "answer_type": "RATIO",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "LABELED_MAP",
                "answer": "",
                "items": [
                    {"label": "甲", "expr": "3:4"},
                    {"label": "乙", "expr": "1:2"},
                ],
            }
        ]
        computed_sympy = (
            "from fractions import Fraction\n"
            "def verify():\n"
            "    a = Fraction(6, 8)\n"
            "    b = Fraction(2, 4)\n"
            "    return {'主问': [f'甲:{a.numerator}:{a.denominator}', f'乙:{b.numerator}:{b.denominator}']}\n"
        )
        res = await ingest_external_mistake_async(
            conn,
            _mock_settings(),
            question_text="分别求甲、乙两组的最简整数比。",
            grade_subject="小学奥数",
            known_answer="甲=3:4, 乙=1:2",
            diagnosis_json=_make_diag_payload(final_answers=ratio_fa, sympy_code=computed_sympy),
            secondary_json=_make_secondary_payload(ratio_fa),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert res["verification_status"] == "HUMAN_VERIFIED"
        assert res["eff_is_servable"] is True
        rr = conn.execute(
            "SELECT review_reason FROM questions WHERE question_uid = ?;", (res["question_uid"],)
        ).fetchone()["review_reason"]
        assert rr == ""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B07-F4, B07-F5, B07-F6: S4 状态机、--log-wrong 事务原子性与降级清边
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b07_f4_f5_f6_s4_state_learner_precheck_and_demotion_edge_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    settings = _mock_settings()

    try:
        # B07-F5: 非法 learner_id 必须在任何写库/向量/镜像前抛 PreconditionError
        with pytest.raises(PreconditionError, match="ghost_learner"):
            await ingest_external_mistake_async(
                conn,
                settings,
                question_text="测试非法学习者阻断。",
                grade_subject="小学奥数",
                diagnosis_json=_make_diag_payload(),
                secondary_json=_make_secondary_payload(),
                log_wrong=True,
                learner_id="ghost_learner",
                assets_dir=env["assets_dir"],
                config_dir=env["config_dir"],
                vectors_dir=env["vectors_dir"],
                drafts_dir=env["drafts_dir"],
                human_dir=env["human_dir"],
                lock_path=env["lock_path"],
                use_mock=True,
            )
        assert conn.execute("SELECT COUNT(*) FROM questions;").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0] == 0
        assert not (env["human_dir"] / "external_questions.jsonl").exists()

        # B07-F4 场景 1: 分位表未初始化时，暂缓织网且 S4 保持 PENDING（绝非 OK），同时 --log-wrong 原子同步镜像
        res_no_q = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="分位表缺失场景测试题。",
            grade_subject="小学奥数",
            diagnosis_json=_make_diag_payload(),
            secondary_json=_make_secondary_payload(),
            log_wrong=True,
            learner_id="default",
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        uid = res_no_q["question_uid"]
        assert res_no_q["eff_is_servable"] is True
        assert res_no_q["woven_edges"] == 0
        assert res_no_q["warning"] and "向量分位表尚未初始化" in res_no_q["warning"]
        assert res_no_q["log_id"] and res_no_q["log_id"] > 0
        s4_row = conn.execute(
            "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S4';", (uid,)
        ).fetchone()
        assert s4_row["status"] == "PENDING"
        assert human_mirror.verify_snapshot(conn, human_dir=env["human_dir"])["is_consistent"] is True

        # B07-F4 场景 3: 初始化分位表后重编成功织网 -> S4 更新为 OK
        store = VectorStore(env["vectors_dir"])
        cur_meta = store.load_meta()
        cur_meta["quantiles"] = {
            "primary_olympiad::surface": [i / 100.0 for i in range(101)],
            "primary_olympiad::skeleton": [i / 100.0 for i in range(101)],
        }
        store._save_meta(cur_meta)
        res_ok = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="分位表缺失场景测试题。",
            grade_subject="小学奥数",
            diagnosis_json=_make_diag_payload(),
            secondary_json=_make_secondary_payload(),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert res_ok["question_uid"] == uid
        assert (
            conn.execute(
                "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S4';", (uid,)
            ).fetchone()["status"]
            == "OK"
        )

        # B07-F4 场景 4: weave_single_question 异常时，S4 不得置为 OK（保持 PENDING）
        def _boom_weave(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("simulated weave failure")

        with monkeypatch.context() as mp:
            mp.setattr(mistake, "weave_single_question", _boom_weave)
            with pytest.raises(RuntimeError, match="simulated weave failure"):
                await ingest_external_mistake_async(
                    conn,
                    settings,
                    question_text="分位表缺失场景测试题。",
                    grade_subject="小学奥数",
                    diagnosis_json=_make_diag_payload(),
                    secondary_json=_make_secondary_payload(),
                    assets_dir=env["assets_dir"],
                    config_dir=env["config_dir"],
                    vectors_dir=env["vectors_dir"],
                    drafts_dir=env["drafts_dir"],
                    human_dir=env["human_dir"],
                    lock_path=env["lock_path"],
                    use_mock=True,
                )
        assert (
            conn.execute(
                "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S4';", (uid,)
            ).fetchone()["status"]
            == "PENDING"
        )

        # B07-F6: 植入机器边（RULE_MATRIX / LLM_VERIFIED）与人工边（HUMAN_VERIFIED），重编降级 NEEDS_REVIEW 后仅清理机器边
        peer_uid = "primary_olympiad:ch01:peer"
        conn.execute(
            "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', 'peer', '对端题', 'h_peer');",
            (peer_uid,),
        )
        conn.execute(
            "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status) VALUES (?, '小学奥数', '对端题', '解答题', 'VERIFIED_CONSENSUS');",
            (peer_uid,),
        )
        effective.recompute(conn, peer_uid)
        u_low, u_high = sorted((uid, peer_uid))
        conn.execute(
            """
            INSERT INTO question_relations(
                source_question_uid, target_question_uid, relation_type, is_directed, is_cross_scope,
                surface_sim, skeleton_sim, surface_pct, skeleton_pct, containment_score, trap_overlap, confidence_source
            ) VALUES
                (?, ?, 'ISOMORPHIC', 0, 0, 0.5, 0.9, 0.5, 0.9, 0.8, 0.0, 'RULE_MATRIX'),
                (?, ?, 'SCAFFOLD', 1, 0, 0.4, 0.85, 0.4, 0.85, 0.7, 0.0, 'LLM_VERIFIED'),
                (?, ?, 'SHARED_TRAP', 0, 0, 0.3, 0.3, 0.3, 0.3, 0.1, 0.9, 'HUMAN_VERIFIED');
            """,
            (u_low, u_high, peer_uid, uid, u_low, u_high),
        )
        conn.commit()

        bad_sec = _make_secondary_payload(
            [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "999",
                    "sympy_expr": "999",
                }
            ]
        )
        res_demoted = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="分位表缺失场景测试题。",
            grade_subject="小学奥数",
            diagnosis_json=_make_diag_payload(),
            secondary_json=bad_sec,
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert res_demoted["eff_is_servable"] is False
        assert res_demoted["verification_status"] == "NEEDS_REVIEW"
        assert (
            conn.execute(
                "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S4';", (uid,)
            ).fetchone()["status"]
            == "BLOCKED"
        )
        rem_edges = conn.execute(
            "SELECT relation_type, confidence_source FROM question_relations WHERE source_question_uid = ? OR target_question_uid = ?;",
            (uid, uid),
        ).fetchall()
        assert [(r["relation_type"], r["confidence_source"]) for r in rem_edges] == [
            ("SHARED_TRAP", "HUMAN_VERIFIED")
        ]

        # B07-F4 场景 2: 已软删 (is_retired=1) 的既有题目，即使双盲 VERIFIED_CONSENSUS，eff_is_servable=0 -> S4 仍为 BLOCKED
        conn.execute("UPDATE questions SET is_retired = 1 WHERE question_uid = ?;", (uid,))
        conn.commit()
        res_ret = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="分位表缺失场景测试题。",
            grade_subject="小学奥数",
            diagnosis_json=_make_diag_payload(),
            secondary_json=_make_secondary_payload(),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert res_ret["eff_is_servable"] is False
        assert (
            conn.execute(
                "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S4';", (uid,)
            ).fetchone()["status"]
            == "BLOCKED"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B07-F7: 诊断同 TRAP 排雷推荐（单跳 redirect + 学段/可出题过滤先于 LIMIT）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b07_f7_diagnose_trap_recommendation_redirect_and_no_cross_scope_starvation(
    tmp_path: Path,
) -> None:
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    settings = _mock_settings()

    try:
        conn.execute(
            "INSERT OR IGNORE INTO grade_subjects(grade_subject, slug, display_name, subject_family) VALUES ('初中数学', 'junior_math', '初中数学', 'MATH');"
        )
        # 构造主概念 + 重定向别名概念
        master_row = conn.execute(
            "SELECT concept_id FROM concepts WHERE kind = 'TRAP' AND name = 'TRAP_SIGN_DIRECTION' AND redirect_to_id IS NULL;"
        ).fetchone()
        if master_row:
            master_cid = str(master_row["concept_id"])
        else:
            master_cid = "c_master_sign"
            conn.execute(
                "INSERT INTO concepts(concept_id, kind, name, redirect_to_id) VALUES (?, 'TRAP', 'TRAP_SIGN_DIRECTION', NULL);",
                (master_cid,),
            )
        alias_cid = "c_alias_sign"
        conn.execute(
            "INSERT INTO concepts(concept_id, kind, name, redirect_to_id) VALUES (?, 'TRAP', '旧正负符号陷阱', ?);",
            (alias_cid, master_cid),
        )

        # 植入 15 道字典序靠前的跨学段（初中数学）题 + 15 道不可出题的同学段题，全部挂载到 master_cid
        for i in range(15):
            j_uid = f"a_junior:ch01:j{i:02d}"
            conn.execute(
                "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '初中数学', 'BATCH_JSONL', 'ch01', ?, '初中题', ?);",
                (j_uid, f"j{i:02d}", f"hj{i}"),
            )
            conn.execute(
                "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status, m_c_score, m_s_score, m_t_score, m_l_score) VALUES (?, '初中数学', '初中题', '解答题', 'VERIFIED_CONSENSUS', 2, 1, 1, 1);",
                (j_uid,),
            )
            effective.recompute(conn, j_uid)
            conn.execute(
                "INSERT INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
                (j_uid, master_cid),
            )

            unserv_uid = f"a_primary_retired:ch01:r{i:02d}"
            conn.execute(
                "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', ?, '已下架题', ?);",
                (unserv_uid, f"r{i:02d}", f"hr{i}"),
            )
            conn.execute(
                "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status, is_retired) VALUES (?, '小学奥数', '已下架题', '解答题', 'NEEDS_REVIEW', 1);",
                (unserv_uid,),
            )
            effective.recompute(conn, unserv_uid)
            conn.execute(
                "INSERT INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
                (unserv_uid, master_cid),
            )

        # 植入 1 道字典序靠后的同学段可出题，且挂载在重定向成员 alias_cid 上
        target_uid = "z_primary_olympiad:ch01:target_alias"
        conn.execute(
            "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', 'target_alias', '同坑目标题', 'h_tgt');",
            (target_uid,),
        )
        conn.execute(
            "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status, m_c_score, m_s_score, m_t_score, m_l_score) VALUES (?, '小学奥数', '同坑目标题', '解答题', 'VERIFIED_CONSENSUS', 2, 1, 1, 1);",
            (target_uid,),
        )
        effective.recompute(conn, target_uid)
        conn.execute(
            "INSERT INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
            (target_uid, alias_cid),
        )
        conn.commit()

        res = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text="运动方向容易搞反的行程问题。",
            grade_subject="小学奥数",
            diagnosis_json=_make_diag_payload(trap_tags=["TRAP_SIGN_DIRECTION"]),
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            use_mock=True,
        )
        trap_uids = [it["question_uid"] for it in res["recommendations"]["contrastive_and_traps"]]
        assert target_uid in trap_uids
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B07-F8: raw_questions.cas_images_json / plain_fingerprint / updated_at 落库与恢复一致
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b07_f8_raw_questions_cas_images_and_restore_roundtrip(tmp_path: Path) -> None:
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    settings = _mock_settings()
    img = _save_png(tmp_path / "pic.png", (10, 200, 100))

    try:
        res = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="带图外部错题测试。",
            grade_subject="小学奥数",
            image_paths=[img],
            diagnosis_json=_make_diag_payload(),
            secondary_json=_make_secondary_payload(),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        uid = res["question_uid"]
        raw_row = conn.execute(
            "SELECT cas_images_json, plain_fingerprint, updated_at FROM raw_questions WHERE question_uid = ?;",
            (uid,),
        ).fetchone()
        q_row = conn.execute(
            "SELECT cas_images_json FROM questions WHERE question_uid = ?;", (uid,)
        ).fetchone()
        cas_list = json.loads(raw_row["cas_images_json"])
        assert len(cas_list) == 1
        assert raw_row["cas_images_json"] == q_row["cas_images_json"]
        assert raw_row["plain_fingerprint"] != ""

        # 检查导出的 external_questions.jsonl 与净仓 restore 一致性
        ext_lines = [
            json.loads(ln)
            for ln in (env["human_dir"] / "external_questions.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(ext_lines) == 1
        assert ext_lines[0]["r_cas_images"] == raw_row["cas_images_json"]

        # 净仓恢复验证
        dst_conn = get_connection(tmp_path / "data" / "restored.db")
        try:
            init_db(dst_conn, config_dir=env["config_dir"])
            human_mirror.restore_snapshot(
                dst_conn, human_dir=env["human_dir"], backups_dir=env["backups_dir"]
            )
            restored_raw = dst_conn.execute(
                "SELECT cas_images_json, plain_fingerprint FROM raw_questions WHERE question_uid = ?;",
                (uid,),
            ).fetchone()
            assert restored_raw["cas_images_json"] == raw_row["cas_images_json"]
            assert restored_raw["plain_fingerprint"] == raw_row["plain_fingerprint"]
        finally:
            dst_conn.close()

        # 手动把 raw_questions 改脏后再次入库，验证 ON CONFLICT DO UPDATE 同步刷新 cas_images_json / plain_fingerprint / updated_at
        conn.execute(
            "UPDATE raw_questions SET cas_images_json = '[]', plain_fingerprint = 'stale', updated_at = '2000-01-01 00:00:00' WHERE question_uid = ?;",
            (uid,),
        )
        conn.commit()
        await ingest_external_mistake_async(
            conn,
            settings,
            question_text="带图外部错题测试。",
            grade_subject="小学奥数",
            image_paths=[img],
            diagnosis_json=_make_diag_payload(),
            secondary_json=_make_secondary_payload(),
            assets_dir=env["assets_dir"],
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        updated_raw = conn.execute(
            "SELECT cas_images_json, plain_fingerprint, updated_at FROM raw_questions WHERE question_uid = ?;",
            (uid,),
        ).fetchone()
        assert updated_raw["cas_images_json"] == q_row["cas_images_json"]
        assert updated_raw["plain_fingerprint"] == raw_row["plain_fingerprint"]
        assert updated_raw["updated_at"] != "2000-01-01 00:00:00"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B07-F9: _build_taxonomy_enum_summary 仅暴露当前 taxonomy_version
# ---------------------------------------------------------------------------


def test_b07_f9_taxonomy_enum_summary_filters_active_version(tmp_path: Path) -> None:
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    try:
        conn.execute(
            "UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0' WHERE grade_subject = '小学奥数';"
        )
        conn.execute(
            """
            INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
            VALUES
                ('kn_old_l2', '小学奥数', 'draft-1', 'L2', '废弃旧模块', '废弃旧L2', ''),
                ('kn_cur_l2', '小学奥数', '1.0', 'L2', '平面几何', '三角形面积', '');
            """
        )
        conn.commit()

        summary = _build_taxonomy_enum_summary(conn, "小学奥数")
        assert "平面几何" in summary and "三角形面积" in summary
        assert "废弃旧模块" not in summary and "废弃旧L2" not in summary

        # taxonomy_version 为空时兜底放开过滤
        conn.execute(
            "UPDATE grade_subjects SET taxonomy_status = 'NONE', taxonomy_version = '' WHERE grade_subject = '小学奥数';"
        )
        conn.commit()
        summary_empty_ver = _build_taxonomy_enum_summary(conn, "小学奥数")
        assert "废弃旧模块" in summary_empty_ver and "平面几何" in summary_empty_ver
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B07-F10: build_stage1_messages & build_qualitative_judge_messages 透传参数
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b07_f10_ingest_forwards_config_dir_and_grade_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make_isolated_env(tmp_path)
    custom_cfg = tmp_path / "custom_config_dir"
    shutil.copytree(env["config_dir"], custom_cfg)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=custom_cfg)
    settings = _mock_settings()

    s1_calls: list[dict[str, Any]] = []
    judge_calls: list[dict[str, Any]] = []

    orig_build_s1 = mistake.build_stage1_messages
    orig_build_judge = mistake.build_qualitative_judge_messages

    def _spy_build_s1(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        s1_calls.append({"args": args, "kwargs": kwargs})
        return orig_build_s1(*args, **kwargs)

    def _spy_build_judge(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        judge_calls.append({"args": args, "kwargs": kwargs})
        return orig_build_judge(*args, **kwargs)

    class _DummyResp:
        def __init__(self, parsed: Any) -> None:
            self.parsed = parsed

    class _FakeGateway:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def call_structured(self, *, slot: str, stage: str, schema: Any, **kwargs: Any) -> Any:
            if stage == "STAGE1_SECONDARY":
                return _DummyResp(
                    schema.model_validate(
                        _make_secondary_payload(
                            [
                                {
                                    "label": "主问",
                                    "answer_type": "PROOF",
                                    "verify_mode": "QUALITATIVE",
                                    "collection_mode": "SINGLE",
                                    "answer": "由三角形全等定理 SAS 可证边相等",
                                }
                            ]
                        )
                    )
                )
            return _DummyResp(
                QualitativeJudgeOutput(equivalent=True, confidence="HIGH", reason="核心结论一致")
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(mistake, "build_stage1_messages", _spy_build_s1)
    monkeypatch.setattr(mistake, "build_qualitative_judge_messages", _spy_build_judge)
    monkeypatch.setattr(mistake, "LLMGateway", _FakeGateway)

    proof_fa = [
        {
            "label": "主问",
            "answer_type": "PROOF",
            "verify_mode": "QUALITATIVE",
            "collection_mode": "SINGLE",
            "answer": "利用边角边判定全等即可得证",
        }
    ]
    diag_proof = _make_diag_payload(final_answers=proof_fa, sympy_code=None)

    try:
        conn.execute(
            "INSERT OR IGNORE INTO grade_subjects(grade_subject, slug, display_name, subject_family) VALUES ('初中数学', 'junior_math', '初中数学', 'MATH');"
        )
        conn.commit()
        res = await ingest_external_mistake_async(
            conn,
            settings,
            question_text="求证三角形 ABC 与三角形 DEF 全等。",
            grade_subject="初中数学",
            diagnosis_json=diag_proof,
            assets_dir=env["assets_dir"],
            config_dir=custom_cfg,
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            human_dir=env["human_dir"],
            lock_path=env["lock_path"],
            use_mock=True,
        )
        assert res["verification_status"] == "VERIFIED_CONSENSUS"
        assert len(s1_calls) == 1
        assert Path(s1_calls[0]["kwargs"]["config_dir"]) == custom_cfg
        assert len(judge_calls) == 1
        assert judge_calls[0]["kwargs"]["grade_subject"] == "初中数学"
        assert Path(judge_calls[0]["kwargs"]["config_dir"]) == custom_cfg
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B08-F1 & B08-F2: Markdown 图片 alt 含括号、CommonMark <...> 与路径含括号往返
# ---------------------------------------------------------------------------


def test_b08_f1_f2_markdown_images_alt_parens_angle_brackets_and_preflight_roundtrip(
    tmp_path: Path,
) -> None:
    # 含英文括号与空格的父目录（Windows 常见目录）
    work_dir = tmp_path / "math (2026)"
    img_dir = work_dir / "img"
    img_a = _save_png(img_dir / "a.png", (255, 0, 0))
    img_fig = _save_png(img_dir / "fig (1).png", (0, 255, 0))

    src_md = work_dir / "questions.md"
    src_md.write_text(
        (
            "## 1. 含括号 alt 与尖括号图片\n"
            "如图：\n"
            "![图(1)](img/a.png) (注：不计摩擦)\n"
            "![图(2)](<img/fig (1).png> \"示意图(2)\")\n"
            "求阴影面积。\n\n"
            "## 2. 裸括号相对路径\n"
            "![图形(A)](img/fig (1).png)\n"
            "求周长。\n"
        ),
        encoding="utf-8",
    )

    # 1. 直接解析
    parsed_raw = parse_question_list_markdown(src_md)
    assert len(parsed_raw) == 2
    assert parsed_raw[0].image_paths == [img_a.resolve(), img_fig.resolve()]
    assert "(注：不计摩擦)" in parsed_raw[0].question_text
    assert "fig (1).png" not in parsed_raw[0].question_text
    assert parsed_raw[1].image_paths == [img_fig.resolve()]
    assert parsed_raw[1].question_text == "求周长。"

    # 2. preflight 规范化（含括号绝对路径应包裹在 <...> 中且不双层 <<...>>，alt 含 ( 不被截断）
    drafts_dir = tmp_path / "drafts"
    rep = preflight_question_list_markdown(src_md, drafts_dir=drafts_dir)
    assert rep["total_questions"] == 2
    assert rep["missing_images"] == []
    assert rep["items"][0]["image_count"] == 2
    assert rep["items"][1]["image_count"] == 1

    norm_path = Path(rep["normalized_file"])
    norm_txt = norm_path.read_text(encoding="utf-8")
    assert "![图(1)](<" in norm_txt
    assert "![图(2)](<" in norm_txt
    assert "<<" not in norm_txt and ">>" not in norm_txt

    parsed_norm = parse_question_list_markdown(norm_path)
    assert parsed_norm[0].image_paths == [img_a.resolve(), img_fig.resolve()]
    assert parsed_norm[1].image_paths == [img_fig.resolve()]
    assert "math (2026)" not in parsed_norm[0].question_text
    assert "math (2026)" not in parsed_norm[1].question_text


# ---------------------------------------------------------------------------
# B08-F3: 目录路径按无效图片处理、残缺草稿过滤与 _run_one 全异常隔离
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b08_f3_dir_image_corrupt_draft_and_run_one_exception_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make_isolated_env(tmp_path)
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    settings = _mock_settings()

    img_dir = tmp_path / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    md_file = tmp_path / "batch_dir.md"
    md_file.write_text(
        (
            "## 1. 正常题\n"
            "求 1 + 2 的值。\n\n"
            "## 2. 目录冒充图片题\n"
            "如图求面积。\n"
            "图片: images/\n"
        ),
        encoding="utf-8",
    )

    # 1. preflight 必须将目录路径识别为缺失/无效图片
    pf = preflight_question_list_markdown(md_file, drafts_dir=env["drafts_dir"])
    assert len(pf["missing_images"]) == 1
    assert pf["missing_images"][0]["index"] == 2

    # 2. 在 drafts_dir 植入多份外层字段残缺草稿（命中 Q1 content_hash）
    env["drafts_dir"].mkdir(parents=True, exist_ok=True)
    q1_hash = compute_diagnose_content_hash("小学奥数", "求 1 + 2 的值。", [])
    corrupt_cases = [
        {"grade_subject": "小学奥数", "content_hash": q1_hash, "draft_id": "drf_no_elo", "tier": "L1_基础直达", "diagnosis": _make_diag_payload()},
        {"grade_subject": "小学奥数", "content_hash": q1_hash, "draft_id": "drf_bool_elo", "tier": "L1_基础直达", "base_elo": True, "diagnosis": _make_diag_payload()},
        {"grade_subject": "小学奥数", "content_hash": q1_hash, "draft_id": "", "tier": "L1_基础直达", "base_elo": 1200, "diagnosis": _make_diag_payload()},
        {"grade_subject": "小学奥数", "content_hash": q1_hash, "draft_id": "drf_no_tier", "tier": "", "base_elo": 1200, "diagnosis": _make_diag_payload()},
    ]
    for i, bad in enumerate(corrupt_cases):
        (env["drafts_dir"] / f"drf_bad_{i}.json").write_text(
            json.dumps(bad, ensure_ascii=False), encoding="utf-8"
        )
    idx_map = _load_reusable_draft_index(env["drafts_dir"], "小学奥数")
    assert q1_hash not in idx_map

    try:
        # 3. 批跑不得被 Q2 目录图片或残缺草稿打崩，Q1 成功且双汇总报告完整落盘
        res = await diagnose_external_file_async(
            conn,
            settings,
            file_path=md_file,
            grade_subject="小学奥数",
            concurrency=2,
            force=False,
            config_dir=env["config_dir"],
            vectors_dir=env["vectors_dir"],
            drafts_dir=env["drafts_dir"],
            use_mock=True,
        )
        assert res["succeeded"] == 1
        assert res["failed"] == 1
        assert res["items"][0]["status"] == "OK"
        assert res["items"][1]["status"] == "FAILED"
        assert res["items"][1]["error"]["code"] == "PRECONDITION_FAILED"
        assert Path(res["batch_json_file"]).is_file()
        assert Path(res["batch_markdown_file"]).is_file()

        # 4. 模拟 compute_diagnose_content_hash 在单题抛出 PermissionError，仍由 _run_one 隔离不打崩整批
        orig_hash = diagnose_batch.compute_diagnose_content_hash

        def _flaky_hash(gs: str, text: str, imgs: Any) -> str:
            if "特殊触发" in text:
                raise PermissionError("simulated read permission error")
            return orig_hash(gs, text, imgs)

        md_perm = tmp_path / "batch_perm.md"
        md_perm.write_text(
            "## 1. 正常题\n计算 3 + 4。\n\n## 2. 权限异常题\n特殊触发权限异常题干。\n",
            encoding="utf-8",
        )
        with monkeypatch.context() as mp:
            mp.setattr(diagnose_batch, "compute_diagnose_content_hash", _flaky_hash)
            res_perm = await diagnose_external_file_async(
                conn,
                settings,
                file_path=md_perm,
                grade_subject="小学奥数",
                concurrency=2,
                force=True,
                config_dir=env["config_dir"],
                vectors_dir=env["vectors_dir"],
                drafts_dir=env["drafts_dir"],
                use_mock=True,
            )
        assert res_perm["succeeded"] == 1
        assert res_perm["failed"] == 1
        assert res_perm["items"][1]["error"]["code"] == "INTERNAL_ERROR"
        assert Path(res_perm["batch_markdown_file"]).is_file()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B08-F4: _format_answers_inline 按 I4 兜底渲染空顶层 answer
# ---------------------------------------------------------------------------


def test_b08_f4_format_answers_inline_i4_fallbacks(tmp_path: Path) -> None:
    p_multiset = AnswerPart.model_validate(
        {
            "label": "(1)",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "UNORDERED_MULTISET",
            "answer": "",
            "items": [{"canonical": "1", "sympy_expr": "1"}, {"canonical": "2", "sympy_expr": "2"}],
        }
    ).model_dump()
    assert _format_answers_inline([p_multiset]) == "(1): 1, 2"

    p_choice = AnswerPart.model_validate(
        {
            "label": "主问",
            "answer_type": "CHOICE",
            "verify_mode": "EXACT",
            "collection_mode": "SINGLE",
            "answer": "",
            "choice_label": "B",
            "items": [{"canonical": "B", "choice_label": "B"}],
        }
    ).model_dump()
    assert _format_answers_inline([p_choice]) == "B"

    p_sym = AnswerPart.model_validate(
        {
            "label": "主问",
            "answer_type": "SYMBOL_FILL",
            "verify_mode": "EXACT",
            "collection_mode": "SINGLE",
            "answer": "",
            "canonical": ">=",
        }
    ).model_dump()
    assert _format_answers_inline([p_sym]) == ">="

    p_map = AnswerPart.model_validate(
        {
            "label": "主问",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "LABELED_MAP",
            "answer": "",
            "unit": "只",
            "items": [{"label": "鸡", "expr": "12"}, {"label": "兔", "expr": "8"}],
        }
    ).model_dump()
    assert _format_answers_inline([p_map]) == "鸡=12, 兔=8只"

    p_zero = {
        "label": "主问",
        "answer_type": "NUMERIC",
        "verify_mode": "SYMBOLIC",
        "collection_mode": "ORDERED_TUPLE",
        "answer": "",
        "items": [{"expr": 0}, {"expr": 5}],
    }
    assert _format_answers_inline([p_zero]) == "0, 5"


# ---------------------------------------------------------------------------
# B08-F5: preflight Pass 3 在 has_body=True 时不误拆短标题（年份数字/冒号）
# ---------------------------------------------------------------------------


def test_b08_f5_preflight_pass3_preserves_clean_short_titles(tmp_path: Path) -> None:
    src = tmp_path / "clean_titles.md"
    src.write_text(
        (
            "## 1. 2024年期中第5题\n"
            "已知 x + 1 = 2，求 x。\n\n"
            "## 2. 一元二次方程：韦达定理\n"
            "已知 x^2 - 3x + 1 = 0，求两根之和。\n\n"
            "## 3. 一列火车每小时行60千米，3小时行驶多少千米？\n\n"
            "## 4. 已知甲乙两地相距300千米，两车同时相向而行：\n"
            "(1) 求相遇时间；\n"
        ),
        encoding="utf-8",
    )

    rep = preflight_question_list_markdown(src, drafts_dir=tmp_path / "drafts")
    parsed = parse_question_list_markdown(rep["normalized_file"])
    assert len(parsed) == 4

    # Q1 / Q2 短标题不被误拆进题干
    assert parsed[0].title == "1. 2024年期中第5题"
    assert parsed[0].question_text == "已知 x + 1 = 2，求 x。"
    assert parsed[1].title == "2. 一元二次方程：韦达定理"
    assert parsed[1].question_text == "已知 x^2 - 3x + 1 = 0，求两根之和。"

    # Q3 无正文单行题仍正常拆分保留题干
    assert "一列火车每小时行60千米" in parsed[2].question_text

    # Q4 有强正文标点（，）且有后续小问仍正常拆分
    assert "甲乙两地相距300千米" in parsed[3].question_text
    assert "(1) 求相遇时间；" in parsed[3].question_text


# ---------------------------------------------------------------------------
# B08-F6: 总览表所有列折叠换行、先截断后转义 `|`
# ---------------------------------------------------------------------------


def test_b08_f6_escape_md_cell_and_overview_table_integrity(tmp_path: Path) -> None:
    env = _make_isolated_env(tmp_path)
    # 边界：第 60 个字符恰好为 `|`，先截断再转义绝不产生孤立尾部 `\`
    edge_val = "A" * 59 + "|tail\nline2"
    escaped = _escape_md_cell(edge_val, max_len=60)
    assert escaped == "A" * 59 + "\\|"
    assert not escaped.endswith("\\\\")

    summary = {
        "batch_id": "batch_escape_test",
        "source_file": "test.md",
        "grade_subject": "小学奥数",
        "created_at": "2026-11-27 08:00:00 CST",
        "total": 2,
        "succeeded": 1,
        "reused": 0,
        "failed": 1,
        "items": [
            {
                "index": 1,
                "title": "题1|副题\n换行",
                "question_text": "题干1",
                "status": "FAILED",
                "error": {"code": "INTERNAL_ERROR", "message": "Validation failed:\r\nline 2 | col " + "x" * 50},
            },
            {
                "index": 2,
                "title": "题2|正常",
                "question_text": "题干2",
                "status": "OK",
                "result": {
                    "draft_id": "drf_abc|1",
                    "tier": "L3_综合|中档",
                    "base_elo": 1500,
                    "primary_l1_module": "几何|代数\n跨模块",
                    "matched_l2": ["模型A"],
                    "final_answers": [{"label": "主问", "answer": "行一\n行二|值"}],
                    "duplicate_candidates": [
                        {"question_uid": "u|1", "source_id": "src|1\n2", "similarity": 0.99, "match_type": "EXACT", "tier": "L3", "base_elo": 1500}
                    ],
                    "recommendations": {},
                },
            },
        ],
    }

    md = render_batch_markdown_report(summary, conn=None, config_dir=env["config_dir"])
    # 提取总览表数据行（介于表头分隔线与 `## 二、` 之间）
    m_tbl = re.search(r"\| :---: \| :--- \|.*?\n(.*?)\n\n## 二、", md, flags=re.S)
    assert m_tbl is not None
    data_rows = [ln for ln in m_tbl.group(1).splitlines() if ln.strip()]
    assert len(data_rows) == 2
    for row in data_rows:
        # 去除转义后的 `\|` 后，8 列 GFM 表格必须恰好有 9 个未转义 `|`
        unescaped_pipes = row.replace("\\|", "").count("|")
        assert unescaped_pipes == 9, f"表格列数损坏: {row}"


# ---------------------------------------------------------------------------
# B08-F7: _prune_expired_drafts 仅删 drf_*.json + 先渲染后原子落盘
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b08_f7_prune_only_drf_and_render_before_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make_isolated_env(tmp_path)
    drafts_dir = env["drafts_dir"]
    drafts_dir.mkdir(parents=True, exist_ok=True)

    old_ts = time.time() - 30 * 86400
    drf_old = drafts_dir / "drf_expired001.json"
    drf_new = drafts_dir / "drf_fresh001.json"
    batch_old_json = drafts_dir / "batch_20260101_000000_abcdef.json"
    batch_old_md = drafts_dir / "batch_20260101_000000_abcdef.md"
    for p in (drf_old, drf_new, batch_old_json, batch_old_md):
        p.write_text("{}", encoding="utf-8")
    for p in (drf_old, batch_old_json, batch_old_md):
        os.utime(p, (old_ts, old_ts))

    _prune_expired_drafts(drafts_dir)
    assert not drf_old.exists()
    assert drf_new.exists()
    assert batch_old_json.exists()
    assert batch_old_md.exists()

    # 若 render_batch_markdown_report 抛异常，不得留下孤立的 batch_*.json 或 .tmp 残片
    conn = get_connection(env["db_path"])
    init_db(conn, config_dir=env["config_dir"])
    md_src = tmp_path / "single_ok.md"
    md_src.write_text("## 1. 简单题\n求 2 + 2。\n", encoding="utf-8")
    clean_drafts = tmp_path / "drafts_atomic"
    try:
        def _boom_render(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("render explosion")

        with monkeypatch.context() as mp:
            mp.setattr(diagnose_batch, "render_batch_markdown_report", _boom_render)
            with pytest.raises(RuntimeError, match="render explosion"):
                await diagnose_external_file_async(
                    conn,
                    _mock_settings(),
                    file_path=md_src,
                    grade_subject="小学奥数",
                    config_dir=env["config_dir"],
                    vectors_dir=env["vectors_dir"],
                    drafts_dir=clean_drafts,
                    use_mock=True,
                )
        assert list(clean_drafts.glob("batch_*.json")) == []
        assert list(clean_drafts.glob("batch_*.md")) == []
        assert list(clean_drafts.glob("*.tmp")) == []

        # preflight 覆盖写原子性：os.replace 异常时保留既有 out_normalized 且清理临时文件
        out_norm = tmp_path / "existing.normalized.md"
        out_norm.write_text("ORIGINAL_CONTENT\n", encoding="utf-8")
        with monkeypatch.context() as mp:
            def _boom_replace(src: Any, dst: Any) -> None:
                raise OSError("simulated atomic replace failure")

            mp.setattr(diagnose_batch.os, "replace", _boom_replace)
            with pytest.raises(OSError, match="simulated atomic replace failure"):
                preflight_question_list_markdown(md_src, out_normalized=out_norm, drafts_dir=clean_drafts)
        assert out_norm.read_text(encoding="utf-8") == "ORIGINAL_CONTENT\n"
        assert list(tmp_path.glob(".*.tmp")) == []
    finally:
        conn.close()
