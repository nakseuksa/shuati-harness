"""P2 单元测试：materialize_solutions 防级联误删、复核采纳免误置 STALE、定向抽检排序、词表智能无损晋升与 CLI 接线。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shuati.cli.main import app
from shuati.config import Settings
from shuati.core import effective
from shuati.core.knowledge_graph import apply_cluster_assignments, revert_merge_batch
from shuati.core.taxonomy import (
    TaxonomyChangeAction,
    TaxonomyChangelogEntry,
    TaxonomyL1Entry,
    TaxonomyL2Entry,
    TaxonomySpec,
    make_node_id,
    save_taxonomy_spec,
    transition_taxonomy,
)
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.llm.embeddings import EmbeddingClient
from shuati.models.domain import AnswerPart
from shuati.pipeline import stage4_weave
from shuati.pipeline.stage1_solve import materialize_solutions
from shuati.pipeline.stage3_taxonomy import run_stage3c_async, run_stage3d_mount
from shuati.services import facade
from shuati.vectors.store import VectorStore

GS = "小学奥数"
SLUG = "primary_olympiad"


def _make_conn(tmp_path: Path, name: str = "p2_test.db") -> sqlite3.Connection:
    conn = get_connection(tmp_path / name)
    init_db(conn)
    assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1
    return conn


def _ans_json(val: str) -> str:
    part = AnswerPart(
        label="主问",
        answer_type="NUMERIC",
        verify_mode="SYMBOLIC",
        answer=val,
        sympy_expr=val,
    )
    return json.dumps([part.model_dump()], ensure_ascii=False)


# ===========================================================================
# 1. stage1_solve.materialize_solutions 防级联误删 question_steps
# ===========================================================================


def test_materialize_solutions_upserts_in_place_without_cascading_steps(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    uid = "primary_olympiad:ch01:q_mat"
    ans12 = _ans_json("12")

    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        ) VALUES (?, ?, 'BATCH_JSONL', 'ch01', 'q_mat', '长3宽4求面积', 'h_mat');
        """,
        (uid, GS),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_final_answers_json
        ) VALUES (?, ?, '长3宽4求面积', '解答题', 'VERIFIED_CONSENSUS', ?);
        """,
        (uid, GS, ans12),
    )
    conn.execute(
        """
        INSERT INTO stage1_vlm_runs(
            run_id, question_uid, role, model_name, prompt_version, input_hash,
            run_status, visual_facts, method_summary, solution_markdown,
            final_answers_json, sympy_code, sympy_status
        ) VALUES (10, ?, 'PRIMARY', 'm1', 's1_v1', 'ih', 'OK', '', '面积公式', '旧主路解：3*4=12', ?, 'x', 'PASSED');
        """,
        (uid, ans12),
    )
    # 预置自定义 solution_id 的 branch_1 与多余 branch_2，各自挂载步骤
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
        ) VALUES
            ('custom_pk_b1', ?, 'branch_1', 1, 'PRIMARY_VLM', '初始解1'),
            ('custom_pk_b2', ?, 'branch_2', 0, 'SECONDARY_VLM', '初始解2');
        """,
        (uid, uid),
    )
    conn.execute(
        """
        INSERT INTO question_steps(
            step_id, question_uid, branch_id, step_index, sub_goal, raw_l1, raw_l2, raw_l3
        ) VALUES
            ('st_b1_1', ?, 'branch_1', 1, '代公式', '平面几何', '长方形', '长方形面积公式'),
            ('st_b1_2', ?, 'branch_1', 2, '算乘积', '计算', '乘法', '整数乘法'),
            ('st_b2_1', ?, 'branch_2', 1, '数格子', '平面几何', '长方形', '网格计数');
        """,
        (uid, uid, uid),
    )
    effective.recompute(conn, uid)
    conn.commit()

    # 1) 机器路径重建（仅保留 branch_1）：branch_2 及其步骤清理，branch_1 主键与 2 条步骤完好保留！
    materialize_solutions(conn, uid)
    conn.commit()

    sols = conn.execute(
        "SELECT solution_id, branch_id, source_origin, solution_markdown FROM question_solutions WHERE question_uid = ? ORDER BY branch_id;",
        (uid,),
    ).fetchall()
    assert len(sols) == 1
    assert sols[0]["solution_id"] == "custom_pk_b1"
    assert sols[0]["branch_id"] == "branch_1"
    assert sols[0]["source_origin"] == "PRIMARY_VLM"
    assert sols[0]["solution_markdown"] == "旧主路解：3*4=12"

    steps = [
        r["step_id"]
        for r in conn.execute(
            "SELECT step_id FROM question_steps WHERE question_uid = ? ORDER BY step_id;",
            (uid,),
        ).fetchall()
    ]
    assert steps == ["st_b1_1", "st_b1_2"]

    # 2) 写入 solutions_override_json 人工覆写再次物化：branch_1 的步骤依然丝毫无损！
    ov_payload = json.dumps(
        {
            "branches": [
                {
                    "branch_id": "branch_1",
                    "is_optimal": 1,
                    "method_summary": "人工精修公式法",
                    "solution_markdown": "人工精修解：3*4=12",
                    "sympy_verified": 1,
                }
            ]
        },
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, base_fingerprint, is_active
        ) VALUES (?, 'solutions_override_json', ?, 'REVIEW_UI', 'h_mat', 1);
        """,
        (uid, ov_payload),
    )
    materialize_solutions(conn, uid)
    conn.commit()

    sol_h = conn.execute(
        "SELECT solution_id, source_origin, solution_markdown FROM question_solutions WHERE question_uid = ?;",
        (uid,),
    ).fetchone()
    assert sol_h["solution_id"] == "custom_pk_b1"
    assert sol_h["source_origin"] == "HUMAN"
    assert sol_h["solution_markdown"] == "人工精修解：3*4=12"
    assert (
        conn.execute("SELECT COUNT(*) FROM question_steps WHERE question_uid = ?;", (uid,)).fetchone()[0]
        == 2
    )

    # 3) 当无激活覆写且无 PRIMARY OK 运行时，清空全部 question_solutions
    conn.execute("UPDATE human_overrides SET is_active = 0 WHERE question_uid = ?;", (uid,))
    conn.execute("DELETE FROM stage1_vlm_runs WHERE question_uid = ?;", (uid,))
    materialize_solutions(conn, uid)
    conn.commit()
    assert (
        conn.execute("SELECT COUNT(*) FROM question_solutions WHERE question_uid = ?;", (uid,)).fetchone()[0]
        == 0
    )
    conn.close()


# ===========================================================================
# 2. facade.adopt_run / reconfirm_stale_overrides 免误置 S2..S3D STALE + 定向排序
# ===========================================================================


def test_adopt_run_and_reconfirm_preserve_s2_to_s3d_when_upstream_unchanged(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    human_dir = tmp_path / "human"
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'DRAFT', taxonomy_version = 'draft-1' WHERE grade_subject = ?;",
        (GS,),
    )
    l3_nid = make_node_id(SLUG, "draft-1", "L3", "平面几何", "等高模型", "同底等高面积比")
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        VALUES (?, ?, 'draft-1', 'L3', '平面几何', '等高模型', '同底等高面积比');
        """,
        (l3_nid, GS),
    )

    uid = "primary_olympiad:ch01:q_single_adopt"
    ans8 = _ans_json("8")
    ans10 = _ans_json("10")

    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_id, raw_question, content_hash
        ) VALUES (?, ?, 'BATCH_JSONL', 'ch01', 'q_single_adopt', '如图求阴影面积', 'h_v1');
        """,
        (uid, GS),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, has_images, m_question_text, m_format,
            m_visual_facts, m_verification_status, m_final_answers_json,
            m_abstract_skeleton, m_primary_l1_module, m_c_score, m_s_score, m_t_score, m_l_score,
            review_reason
        ) VALUES (?, ?, 1, '如图求阴影面积', '解答题', '正方形边长4', 'NEEDS_REVIEW', ?, '几何面积骨架', '平面几何', 2, 2, 1, 1, 'VISUAL_AMBIGUITY');
        """,
        (uid, GS, ans8),
    )
    conn.execute(
        """
        INSERT INTO stage1_vlm_runs(
            run_id, question_uid, role, model_name, prompt_version, input_hash,
            run_status, visual_facts, method_summary, solution_markdown,
            final_answers_json, sympy_code, sympy_status
        ) VALUES
            (1, ?, 'PRIMARY', 'm1', 's1_v1', 'ih', 'OK', '正方形边长4', '割补法', '阴影面积=4*4/2=8', ?, 'def verify(): return {\"主问\": \"8\"}', 'PASSED'),
            (2, ?, 'SECONDARY', 'm2', 's1_v1', 'ih', 'OK', '长方形5*4', '直接减', '阴影面积=10', ?, NULL, 'NOT_RUN');
        """,
        (uid, ans8, uid, ans10),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, origin_run_id,
            method_summary, solution_markdown, sympy_code, sympy_verified
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', 1, '割补法', '阴影面积=4*4/2=8', 'def verify(): return {\"主问\": \"8\"}', 1);
        """,
        (f"{uid}::branch_1", uid),
    )
    conn.execute(
        """
        INSERT INTO question_steps(
            step_id, question_uid, branch_id, step_index, sub_goal,
            raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id
        ) VALUES (?, ?, 'branch_1', 1, '半正方形转化', '平面几何', '等高模型', '同底等高面积比', '平面几何', '等高模型', ?);
        """,
        (f"{uid}::b1::1", uid, l3_nid),
    )
    conn.execute(
        """
        INSERT INTO compile_state(question_uid, stage, status)
        VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK'), (?, 'S4', 'BLOCKED');
        """,
        (uid, uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)
    conn.commit()

    # 2.1 按 [1] 原样采纳 PRIMARY：原本 S2='OK' + 有步骤 + 上游四要素完全未变
    # -> S2/S3C/S3D 保持 OK，步骤与 eff_node_ids_json 丝毫不丢，仅 S4 置 STALE（因 eff_is_servable: 0 -> 1）
    facade.adopt_run(conn, uid, role="PRIMARY", human_dir=human_dir)
    q_after_p = conn.execute(
        "SELECT eff_verification_status, eff_is_servable, eff_node_ids_json FROM questions WHERE question_uid = ?;",
        (uid,),
    ).fetchone()
    assert q_after_p["eff_verification_status"] == "HUMAN_VERIFIED"
    assert q_after_p["eff_is_servable"] == 1
    assert json.loads(q_after_p["eff_node_ids_json"]) == [l3_nid]
    assert (
        conn.execute("SELECT COUNT(*) FROM question_steps WHERE question_uid = ?;", (uid,)).fetchone()[0]
        == 1
    )

    cs_after_p = {
        r["stage"]: r["status"]
        for r in conn.execute("SELECT stage, status FROM compile_state WHERE question_uid = ?;", (uid,)).fetchall()
    }
    assert cs_after_p == {"S1": "OK", "S2": "OK", "S3C": "OK", "S3D": "OK", "S4": "STALE"}

    # 2.2 若 S4 也已是 OK 且 eff_is_servable 已是 1（典型 audit 抽检确认无误按 [1]），连 S4 都保持 OK！
    conn.execute("UPDATE compile_state SET status = 'OK' WHERE question_uid = ? AND stage = 'S4';", (uid,))
    conn.commit()
    facade.adopt_run(conn, uid, role="PRIMARY", human_dir=human_dir)
    cs_audit_ok = {
        r["stage"]: r["status"]
        for r in conn.execute("SELECT stage, status FROM compile_state WHERE question_uid = ?;", (uid,)).fetchall()
    }
    assert cs_audit_ok == {"S1": "OK", "S2": "OK", "S3C": "OK", "S3D": "OK", "S4": "OK"}

    # 2.3 reconfirm_stale_overrides ([K] 键)：当挂起覆写与机器四要素完全一致且 S2=='OK' 时，同样保住 S2..S3D！
    conn.execute("UPDATE raw_questions SET content_hash = 'h_v2' WHERE question_uid = ?;", (uid,))
    conn.execute(
        "UPDATE human_overrides SET is_suspended = 1, suspended_reason = 'SOURCE_CHANGED' WHERE question_uid = ? AND is_active = 1;",
        (uid,),
    )
    effective.recompute(conn, uid)
    conn.commit()
    assert (
        conn.execute("SELECT eff_is_servable FROM questions WHERE question_uid = ?;", (uid,)).fetchone()[0]
        == 0
    )

    facade.reconfirm_stale_overrides(conn, uid, human_dir=human_dir)
    cs_after_k = {
        r["stage"]: r["status"]
        for r in conn.execute("SELECT stage, status FROM compile_state WHERE question_uid = ?;", (uid,)).fetchall()
    }
    assert cs_after_k == {"S1": "OK", "S2": "OK", "S3C": "OK", "S3D": "OK", "S4": "STALE"}

    # 2.4 采纳 SECONDARY（读图/答案/解法改变）：必须照常置 S2/S3C/S3D/S4 为 STALE！
    facade.adopt_run(conn, uid, role="SECONDARY", human_dir=human_dir)
    cs_after_s = {
        r["stage"]: r["status"]
        for r in conn.execute("SELECT stage, status FROM compile_state WHERE question_uid = ?;", (uid,)).fetchall()
    }
    assert cs_after_s == {"S1": "OK", "S2": "STALE", "S3C": "STALE", "S3D": "STALE", "S4": "STALE"}
    conn.close()


def test_list_review_queue_audit_priority_tiers_and_sha256_shuffle(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)

    # 构造不同桶样本（故意让 Tier 0 的 UID 字典序排在最后 'z_*'，检验不会在排序前被 SQL LIMIT 截掉）
    specs = [
        ("primary_olympiad:ch01:a_dual_1", 1, True, "VERIFIED_CONSENSUS"),
        ("primary_olympiad:ch01:b_dual_2", 0, True, "VERIFIED_CONSENSUS"),
        ("primary_olympiad:ch01:m_single_noimg_1", 0, False, "VERIFIED_CONSENSUS"),
        ("primary_olympiad:ch01:n_single_noimg_2", 0, False, "VERIFIED_CONSENSUS"),
        ("primary_olympiad:ch01:y_single_img_1", 1, False, "VERIFIED_CONSENSUS"),
        ("primary_olympiad:ch01:z_single_img_2", 1, "FAILED_SEC", "VERIFIED_CONSENSUS"),
        ("primary_olympiad:ch01:0_needs_rev", 1, False, "NEEDS_REVIEW"),
    ]
    for uid, has_img, sec_mode, ver_st in specs:
        sid = uid.split(":")[-1]
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection,
                source_id, raw_question, content_hash
            ) VALUES (?, ?, 'BATCH_JSONL', 'ch01', ?, '题目', ?);
            """,
            (uid, GS, sid, f"h_{sid}"),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, has_images, m_question_text, m_format,
                m_verification_status
            ) VALUES (?, ?, ?, '题目', '解答题', ?);
            """,
            (uid, GS, has_img, ver_st),
        )
        conn.execute(
            """
            INSERT INTO stage1_vlm_runs(
                question_uid, role, model_name, prompt_version, input_hash, run_status
            ) VALUES (?, 'PRIMARY', 'm1', 'v1', 'ih', 'OK');
            """,
            (uid,),
        )
        if sec_mode is True:
            conn.execute(
                """
                INSERT INTO stage1_vlm_runs(
                    question_uid, role, model_name, prompt_version, input_hash, run_status
                ) VALUES (?, 'SECONDARY', 'm2', 'v1', 'ih', 'OK');
                """,
                (uid,),
            )
        elif sec_mode == "FAILED_SEC":
            conn.execute(
                """
                INSERT INTO stage1_vlm_runs(
                    question_uid, role, model_name, prompt_version, input_hash, run_status
                ) VALUES (?, 'SECONDARY', 'm2', 'v1', 'ih', 'FAILED');
                """,
                (uid,),
            )
        effective.recompute(conn, uid)
    conn.commit()

    # 常规 NEEDS_REVIEW 队列保持不变
    normal_q = facade.list_review_queue(conn, audit_mode=False)
    assert [it["question_uid"] for it in normal_q] == ["primary_olympiad:ch01:0_needs_rev"]

    # Audit 模式：全量排序校验
    seed = "shuati-audit-v1"
    audit_q = facade.list_review_queue(conn, audit_mode=True, limit=10, seed=seed)
    assert len(audit_q) == 6
    assert [it["audit_priority_tier"] for it in audit_q] == [0, 0, 1, 1, 2, 2]
    assert [it["is_single_run"] for it in audit_q] == [True, True, True, True, False, False]

    # 同桶内顺序严格匹配 sha256(f"{seed}:{uid}")
    expected_tier0 = sorted(
        ["primary_olympiad:ch01:y_single_img_1", "primary_olympiad:ch01:z_single_img_2"],
        key=lambda u: hashlib.sha256(f"{seed}:{u}".encode()).hexdigest(),
    )
    assert [audit_q[0]["question_uid"], audit_q[1]["question_uid"]] == expected_tier0

    # limit=2 截断时必须拿到最高优先级的 Tier 0 两道题（尽管它们 UID 字典序在最后！）
    top2 = facade.list_review_queue(conn, audit_mode=True, limit=2, seed=seed)
    assert [it["question_uid"] for it in top2] == expected_tier0
    conn.close()


# ===========================================================================
# 3. taxonomy.transition_taxonomy 智能无损晋升、外键安全、并查集可逆与 S3 指纹对齐
# ===========================================================================


@pytest.mark.asyncio
async def test_taxonomy_smart_promotion_preserves_graph_ledger_vectors_and_s3_hashes(
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "config"
    vec_dir = tmp_path / "vectors"
    human_dir = tmp_path / "human"
    conn = _make_conn(tmp_path, "smart_lock.db")

    # 1. 初始化 draft-1 词表与学段状态
    draft_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="DRAFT",
        version="draft-1",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                l2_entities=[TaxonomyL2Entry(name="等高模型")],
            ),
            TaxonomyL1Entry(
                name="数论基础",
                l2_entities=[TaxonomyL2Entry(name="旧余数问题")],
            ),
            TaxonomyL1Entry(
                name="杂题模块",
                l2_entities=[TaxonomyL2Entry(name="即将裁撤类目")],
            ),
        ],
    )
    draft_yaml = cfg_dir / "taxonomies" / f"taxonomy_{SLUG}.draft.yaml"
    save_taxonomy_spec(draft_spec, draft_yaml)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'DRAFT', taxonomy_version = 'draft-1', taxonomy_file = ? WHERE grade_subject = ?;",
        (str(draft_yaml), GS),
    )

    # 2. 构造 3 道题目：
    # - q_keep：命中保留类目 (平面几何 / 等高模型)，挂载在并查集归并后的重定向子节点上
    # - q_rename：命中重命名类目 (数论基础 / 旧余数问题 -> 同余与余数)，并带有可迁移 taxonomy_override_json
    # - q_drop：命中即将删除类目 (杂题模块 / 即将裁撤类目)
    q_defs = [
        ("primary_olympiad:ch01:q_keep", "平面几何", "等高模型", "底乘高折半算"),
        ("primary_olympiad:ch01:q_rename", "数论基础", "旧余数问题", "同余可加性"),
        ("primary_olympiad:ch01:q_drop", "杂题模块", "即将裁撤类目", "旧偏怪算子"),
    ]
    for uid, rl1, rl2, rl3 in q_defs:
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection,
                source_id, raw_question, content_hash
            ) VALUES (?, ?, 'BATCH_JSONL', 'ch01', ?, '测试题', ?);
            """,
            (uid, GS, uid.split(":")[-1], f"hash_{uid}"),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_format,
                m_verification_status, m_primary_l1_module, m_c_score, m_s_score, m_t_score, m_l_score
            ) VALUES (?, ?, '测试题', '解答题', 'VERIFIED_CONSENSUS', ?, 1, 1, 1, 1);
            """,
            (uid, GS, rl1),
        )
        conn.execute(
            """
            INSERT INTO question_solutions(
                solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
            ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解答');
            """,
            (f"{uid}::branch_1", uid),
        )
        conn.execute(
            """
            INSERT INTO question_steps(
                step_id, question_uid, branch_id, step_index, sub_goal, raw_l1, raw_l2, raw_l3
            ) VALUES (?, ?, 'branch_1', 1, '核心步骤', ?, ?, ?);
            """,
            (f"{uid}::b1::1", uid, rl1, rl2, rl3),
        )
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK');",
            (uid, uid),
        )
        effective.recompute(conn, uid)
    conn.commit()

    settings = Settings()
    settings.pipeline.use_mock_llm = True

    # 跑真实 S3C + S3D（确保 draft-1 节点、步骤挂载与 S3C/S3D 状态生成）
    s3c_init = await run_stage3c_async(
        conn, settings, grade_subject=GS, mode="api", config_dir=cfg_dir, lock_path=tmp_path / ".lock"
    )
    assert s3c_init["exact_hits"] == 3
    s3d_init = run_stage3d_mount(conn, grade_subject=GS, config_dir=cfg_dir, lock_path=tmp_path / ".lock")
    assert s3d_init["mounted_questions"] == 3
    conn.execute("UPDATE compile_state SET status = 'OK' WHERE stage = 'S4';")

    # 在 draft-1 下对 (平面几何, 等高模型) 增加另一个活跃 L3 并执行 Cluster-then-Assign 并查集归并（产生新建主节点 + 账本行）
    extra_l3 = make_node_id(SLUG, "draft-1", "L3", "平面几何", "等高模型", "等底等高面积转化")
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        VALUES (?, ?, 'draft-1', 'L3', '平面几何', '等高模型', '等底等高面积转化');
        """,
        (extra_l3, GS),
    )
    old_child_nid = conn.execute(
        "SELECT m_knowledge_node_id FROM question_steps WHERE question_uid = 'primary_olympiad:ch01:q_keep';"
    ).fetchone()[0]

    batch_id = "compact_draft_b1"
    ca_stat = apply_cluster_assignments(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict={
            "grade_subject": GS,
            "taxonomy_version": "draft-1",
            "l1_module": "平面几何",
            "l2_entity": "等高模型",
        },
        canonical_operators=[{"canonical_name": "三角形等高面积模型", "description": "标准算子"}],
        assignments=[
            {"item_id": old_child_nid, "raw_l3": "底乘高折半算", "canonical_name": "三角形等高面积模型", "reason": "同义"},
            {"item_id": extra_l3, "raw_l3": "等底等高面积转化", "canonical_name": "三角形等高面积模型", "reason": "同义"},
        ],
        batch_id=batch_id,
        slug=SLUG,
        grade_subject=GS,
        taxonomy_version="draft-1",
    )
    assert ca_stat["applied"] == 2
    assert ca_stat["canonical_nodes_created"] == 1

    old_master_geom = make_node_id(SLUG, "draft-1", "L3", "平面几何", "等高模型", "三角形等高面积模型")
    old_num_l3 = conn.execute(
        "SELECT m_knowledge_node_id FROM question_steps WHERE question_uid = 'primary_olympiad:ch01:q_rename';"
    ).fetchone()[0]
    old_drop_l3 = conn.execute(
        "SELECT m_knowledge_node_id FROM question_steps WHERE question_uid = 'primary_olympiad:ch01:q_drop';"
    ).fetchone()[0]

    # 插入 knowledge_edges（一条跨保留节点应保住，一条连向被删节点应清理）
    conn.execute(
        """
        INSERT INTO knowledge_edges(source_node_id, target_node_id, grade_subject, edge_type, weight)
        VALUES
            (?, ?, ?, 'PREREQUISITE', 0.9),
            (?, ?, ?, 'CO_OCCURRENCE', 0.5);
        """,
        (old_master_geom, old_num_l3, GS, old_master_geom, old_drop_l3, GS),
    )

    # 插入 practice_log（log 1 指向保留 L3 主节点应精确保住 L3 卡点；log 2 指向被删节点应置 NULL）
    conn.execute(
        """
        INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, stuck_step_index, stuck_node_id, stuck_sub_goal)
        VALUES
            (1, 'default', 'primary_olympiad:ch01:q_keep', 'WRONG', 1, ?, '等高卡点'),
            (2, 'default', 'primary_olympiad:ch01:q_drop', 'WRONG', 1, ?, '废弃卡点');
        """,
        (old_master_geom, old_drop_l3),
    )

    # 为 q_rename 添加 ADD 模式 taxonomy_override_json
    conn.execute(
        """
        INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint, taxonomy_version, is_active)
        VALUES ('primary_olympiad:ch01:q_rename', 'taxonomy_override_json', ?, 'PI_CLI', 'hash_primary_olympiad:ch01:q_rename', 'draft-1', 1);
        """,
        (
            json.dumps(
                {
                    "taxonomy_version": "draft-1",
                    "mode": "ADD",
                    "nodes": [{"l1": "数论基础", "l2": "旧余数问题", "l3": "同余可加性", "node_id": old_num_l3}],
                },
                ensure_ascii=False,
            ),
        ),
    )
    effective.recompute_all(conn, GS)
    conn.commit()
    # 同步刷新一次 S3D 使 q_rename 在 draft-1 的 s3d_hash 包含该 override
    run_stage3d_mount(conn, grade_subject=GS, config_dir=cfg_dir, lock_path=tmp_path / ".lock")
    conn.execute("UPDATE compile_state SET status = 'OK' WHERE stage = 'S4';")
    conn.commit()

    # 预构建 draft-1 的 l3_nodes_<slug>.npz 向量矩阵
    vstore = VectorStore(vec_dir)
    emb_client = EmbeddingClient(settings, conn=conn, use_mock=True)
    try:
        active_draft_l3s = conn.execute(
            """
            SELECT node_id, l1_module, l2_entity, l3_operator
            FROM knowledge_nodes
            WHERE grade_subject = ? AND taxonomy_version = 'draft-1' AND node_level = 'L3' AND redirect_to_id IS NULL
            ORDER BY node_id;
            """,
            (GS,),
        ).fetchall()
        sync_init = await vstore.sync_vectors(
            "l3_nodes",
            [(str(r["node_id"]), f"{r['l1_module']}/{r['l2_entity']}/{r['l3_operator']}") for r in active_draft_l3s],
            emb_client,
            scope_slug=SLUG,
        )
        assert sync_init["embedded"] == 3
    finally:
        await emb_client.aclose()

    # 3. 准备 1.0 目标词表 YAML（保留 平面几何/等高模型；changelog 重命名 旧余数问题 -> 同余与余数；删除 杂题模块）
    lock_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="DRAFT",
        version="draft-1",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                l2_entities=[TaxonomyL2Entry(name="等高模型")],
            ),
            TaxonomyL1Entry(
                name="数论基础",
                l2_entities=[TaxonomyL2Entry(name="同余与余数")],
            ),
        ],
        changelog=[
            TaxonomyChangelogEntry(
                from_version="draft-1",
                to_version="1.0",
                actions=[
                    TaxonomyChangeAction(
                        op="rename",
                        level="L2",
                        parent_l1="数论基础",
                        target="旧余数问题",
                        new_name="同余与余数",
                    )
                ],
            )
        ],
    )
    save_taxonomy_spec(lock_spec, draft_yaml)

    # 3.1 Dry-run 检查新增预检指标
    dry = transition_taxonomy(
        conn,
        grade_subject=GS,
        yaml_path=draft_yaml,
        target_version="1.0",
        target_status="LOCKED",
        dry_run=True,
        vectors_dir=vec_dir,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "backups",
        human_dir=human_dir,
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".lock",
    )
    assert dry["promotion_mode"] == "smart_preserve"
    assert dry["preserved_l2_pairs"] == 2
    assert dry["dropped_l2_pairs"] == 1

    # 3.2 正式执行默认智能无损晋升
    res = transition_taxonomy(
        conn,
        grade_subject=GS,
        yaml_path=draft_yaml,
        target_version="1.0",
        target_status="LOCKED",
        vectors_dir=vec_dir,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "backups",
        human_dir=human_dir,
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".lock",
    )
    assert res["promotion_mode"] == "smart_preserve"
    assert res["preserved_questions"] == 2
    assert res["stale_questions"] == 1
    assert res["migrated_practice_logs"] == 1
    assert res["nulled_practice_logs"] == 1
    assert res["migrated_l3_vectors"] == 2

    # 外键完整性 0 违规且无任何旧版本节点残留
    assert conn.execute("PRAGMA foreign_key_check;").fetchall() == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE grade_subject = ? AND taxonomy_version != '1.0';",
            (GS,),
        ).fetchone()[0]
        == 0
    )

    new_master_geom = make_node_id(SLUG, "1.0", "L3", "平面几何", "等高模型", "三角形等高面积模型")
    new_child_geom = make_node_id(SLUG, "1.0", "L3", "平面几何", "等高模型", "底乘高折半算")
    new_num_l3 = make_node_id(SLUG, "1.0", "L3", "数论基础", "同余与余数", "同余可加性")

    # 验证 practice_log 精确保住 L3 级卡点（而非退化到 L2）
    pl1 = conn.execute("SELECT stuck_node_id FROM practice_log WHERE log_id = 1;").fetchone()
    pl2 = conn.execute("SELECT stuck_node_id FROM practice_log WHERE log_id = 2;").fetchone()
    assert pl1["stuck_node_id"] == new_master_geom
    assert pl2["stuck_node_id"] is None

    # 验证 knowledge_edges 保留跨保留节点边、清理废弃边
    edges = conn.execute(
        "SELECT source_node_id, target_node_id, edge_type FROM knowledge_edges WHERE grade_subject = ?;",
        (GS,),
    ).fetchall()
    assert [(r["source_node_id"], r["target_node_id"], r["edge_type"]) for r in edges] == [
        (new_master_geom, new_num_l3, "PREREQUISITE")
    ]

    # 验证 compile_state：q_keep 与 q_rename 保持 S3C/S3D/S4 全 OK；仅 q_drop 为 STALE！
    cs_keep = {
        r["stage"]: r["status"]
        for r in conn.execute(
            "SELECT stage, status FROM compile_state WHERE question_uid = 'primary_olympiad:ch01:q_keep';"
        ).fetchall()
    }
    cs_drop = {
        r["stage"]: r["status"]
        for r in conn.execute(
            "SELECT stage, status FROM compile_state WHERE question_uid = 'primary_olympiad:ch01:q_drop';"
        ).fetchall()
    }
    assert cs_keep == {"S1": "OK", "S2": "OK", "S3C": "OK", "S3D": "OK", "S4": "OK"}
    assert cs_drop == {"S1": "OK", "S2": "OK", "S3C": "STALE", "S3D": "STALE", "S4": "STALE"}

    # 3.3 验证 l3_nodes_<slug>.npz 原子替换为 1.0 新 ID，且未改名节点 100% 零损耗复用向量！
    vec_ids, _ = vstore.load_vectors("l3_nodes", scope_slug=SLUG)
    assert set(vec_ids) == {new_master_geom, new_num_l3}
    emb_client2 = EmbeddingClient(settings, conn=conn, use_mock=True)
    try:
        active_10_l3s = conn.execute(
            """
            SELECT node_id, l1_module, l2_entity, l3_operator
            FROM knowledge_nodes
            WHERE grade_subject = ? AND taxonomy_version = '1.0' AND node_level = 'L3' AND redirect_to_id IS NULL
            ORDER BY node_id;
            """,
            (GS,),
        ).fetchall()
        sync_after = await vstore.sync_vectors(
            "l3_nodes",
            [(str(r["node_id"]), f"{r['l1_module']}/{r['l2_entity']}/{r['l3_operator']}") for r in active_10_l3s],
            emb_client2,
            scope_slug=SLUG,
        )
        # new_master_geom 文本 "平面几何/等高模型/三角形等高面积模型" 完全未变 -> reused == 1；
        # new_num_l3 因 L2 改名 -> embedded == 1！
        assert sync_after["reused"] == 1
        assert sync_after["embedded"] == 1
    finally:
        await emb_client2.aclose()

    # 3.4 关键验证：紧接着执行 Stage 3c 与 Stage 3d，q_keep 与 q_rename 因 input_hash 严格对齐必须 100% skipped！
    s3c_after = await run_stage3c_async(
        conn, settings, grade_subject=GS, mode="api", config_dir=cfg_dir, lock_path=tmp_path / ".lock"
    )
    assert s3c_after["skipped"] == 2
    assert s3c_after["updated"] == 1  # 仅重编 q_drop

    s3d_after = run_stage3d_mount(conn, grade_subject=GS, config_dir=cfg_dir, lock_path=tmp_path / ".lock")
    assert s3d_after["skipped"] == 2
    assert s3d_after["mounted_questions"] == 1  # 仅挂载 q_drop

    # 3.5 关键验证：在 1.0 新版本下执行 revert_merge_batch 依然 100% 无损可逆！
    # 先移除绑定在 new_master_geom 上的边与日志引用，检验孤立 created_master_id 也能一并干净回收
    conn.execute("DELETE FROM knowledge_edges WHERE source_node_id = ?;", (new_master_geom,))
    conn.execute("UPDATE practice_log SET stuck_node_id = NULL WHERE log_id = 1;")
    conn.commit()

    rev_res = revert_merge_batch(
        conn,
        batch_id,
        backups_dir=tmp_path / "backups",
        lock_path=tmp_path / ".lock",
    )
    assert rev_res["reverted_count"] == 2
    # 回滚后 q_keep 的 eff_node_ids_json 自动恢复为原重定向子节点 new_child_geom！
    q_keep_nodes = json.loads(
        conn.execute(
            "SELECT eff_node_ids_json FROM questions WHERE question_uid = 'primary_olympiad:ch01:q_keep';"
        ).fetchone()[0]
    )
    assert q_keep_nodes == [new_child_geom]
    assert (
        conn.execute("SELECT 1 FROM knowledge_nodes WHERE node_id = ?;", (new_master_geom,)).fetchone()
        is None
    )
    conn.close()


# ===========================================================================
# 4. --force-recompile 兼容路径与 CLI 接线测试
# ===========================================================================


def test_cli_taxonomy_lock_force_recompile_and_compile_stage4_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "cli_p2.db"
    cfg_dir = tmp_path / "config"
    vec_dir = tmp_path / "vectors"
    human_dir = tmp_path / "human"

    # 写入最小 settings.yaml 供 CLI 读取
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.yaml").write_text("pipeline:\n  use_mock_llm: true\n", encoding="utf-8")

    conn = get_connection(db_path)
    init_db(conn, cfg_dir)
    spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="DRAFT",
        version="draft-1",
        modules=[TaxonomyL1Entry(name="平面几何", l2_entities=[TaxonomyL2Entry(name="等高模型")])],
    )
    draft_yaml = cfg_dir / "taxonomies" / f"taxonomy_{SLUG}.draft.yaml"
    save_taxonomy_spec(spec, draft_yaml)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'DRAFT', taxonomy_version = 'draft-1', taxonomy_file = ? WHERE grade_subject = ?;",
        (str(draft_yaml), GS),
    )
    l3_id = make_node_id(SLUG, "draft-1", "L3", "平面几何", "等高模型", "底乘高")
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        VALUES (?, ?, 'draft-1', 'L3', '平面几何', '等高模型', '底乘高');
        """,
        (l3_id, GS),
    )
    uid = "primary_olympiad:ch01:q_cli"
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, ?, 'BATCH_JSONL', 'ch01', 'q_cli', '题', 'h1');",
        (uid, GS),
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status, m_primary_l1_module) VALUES (?, ?, '题', '解答题', 'VERIFIED_CONSENSUS', '平面几何');",
        (uid, GS),
    )
    conn.execute(
        "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解');",
        (f"{uid}::b1", uid),
    )
    conn.execute(
        "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, 'branch_1', 1, '步', '平面几何', '等高模型', '底乘高', '平面几何', '等高模型', ?);",
        (f"{uid}::b1::1", uid, l3_id),
    )
    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK'), (?, 'S4', 'OK');",
        (uid, uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)
    conn.commit()
    conn.close()

    runner = CliRunner()

    # 4.1 taxonomy lock --force-recompile --vectors-dir
    res_lock = runner.invoke(
        app,
        [
            "taxonomy",
            "lock",
            "--grade-subject",
            GS,
            "--to-version",
            "1.0",
            "--force-recompile",
            "--vectors-dir",
            str(vec_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--human-dir",
            str(human_dir),
            "--json",
        ],
    )
    assert res_lock.exit_code == 0, res_lock.output
    payload = json.loads(res_lock.stdout)
    assert payload["ok"] is True
    assert payload["data"]["promotion_mode"] == "force_recompile"
    assert payload["data"]["stale_questions"] == 1

    # 4.2 compile --stage 4 --only-uids --ignore-drift 参数传递验证
    captured_s4: dict[str, Any] = {}

    async def _fake_s4(conn_arg: Any, settings_arg: Any, **kwargs: Any) -> dict[str, Any]:
        captured_s4.update(kwargs)
        return {"mode": kwargs.get("mode", "api"), "ok": True}

    monkeypatch.setattr(stage4_weave, "run_stage4_weave_async", _fake_s4)

    res_s4 = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "4",
            "--only-uids",
            "q_cli,q_other",
            "--ignore-drift",
            "--vectors-dir",
            str(vec_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert res_s4.exit_code == 0, res_s4.output
    assert captured_s4["only_uids"] == ["q_cli", "q_other"]
    assert captured_s4["ignore_drift"] is True

    # 4.3 audit --config-dir 透传验证
    from shuati.pipeline import audit as audit_mod

    captured_audit: dict[str, Any] = {}

    def _fake_audit(conn_arg: Any, settings_arg: Any, **kwargs: Any) -> dict[str, Any]:
        captured_audit.update(kwargs)
        return {"report": {}}

    monkeypatch.setattr(audit_mod, "run_stage_q_audit", _fake_audit)
    res_audit = runner.invoke(
        app,
        [
            "audit",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--human-dir",
            str(human_dir),
            "--vectors-dir",
            str(vec_dir),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--json",
        ],
    )
    assert res_audit.exit_code == 0, res_audit.output
    assert Path(str(captured_audit["config_dir"])) == cfg_dir

