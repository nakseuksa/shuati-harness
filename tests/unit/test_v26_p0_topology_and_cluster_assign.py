"""v2.6 P0 单元测试：Cluster-then-Assign 归并、可服务隔离、向量拼接与 Stage 4 宽进精裁（D-Audit-2/3/5）。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from shuati.config import Settings
from shuati.core import effective
from shuati.core.knowledge_graph import (
    apply_cluster_assignments,
    apply_merge_pairs,
    expand_canonical_node_ids,
    revert_merge_batch,
)
from shuati.core.topology import (
    compute_idf_weights,
    generate_candidate_edges,
    load_question_features,
    weave_single_question,
)
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm import mocks
from shuati.llm.prompt_builder import flatten_json_schema
from shuati.models.schemas import (
    Stage2OnlyOutput,
    Stage3dCanonicalOperator,
    Stage3dClusterAssignment,
    Stage3dClusterAssignOutput,
)
from shuati.pipeline.stage3_taxonomy import (
    propose_taxonomy_async,
    run_compaction_async,
    run_stage3e_async,
)
from shuati.pipeline.stage4_weave import (
    reindex_vectors_and_quantiles_async,
    run_stage4_weave_async,
)
from shuati.vectors.store import VectorStore


def _seed_q(
    conn: Any,
    uid: str,
    *,
    text: str,
    visual_facts: str = "",
    skel: str = "骨架描述",
    elo_tier: str = "mid",
    primary_l1: str = "平面几何",
    ver_status: str = "VERIFIED_CONSENSUS",
    steps: list[tuple[str, str, str, str | None]] | None = None,
    motifs: list[str] | None = None,
    traps: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id,
            raw_chapter, raw_question, legacy_tags_json, content_hash
        )
        VALUES (?, '小学奥数', 'BATCH_JSONL', 'secret_col', ?, 'secret_chap', ?, '{"old":"tag"}', ?);
        """,
        (uid, uid, text, f"h_{uid}"),
    )
    if elo_tier == "high":
        c, s, t, l_val = 3, 2, 2, 1  # 1850
    elif elo_tier == "low":
        c, s, t, l_val = 1, 1, 1, 1  # 1120
    else:
        c, s, t, l_val = 2, 2, 2, 1  # 1510

    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_visual_facts, m_format,
            m_verification_status, m_abstract_skeleton, m_primary_l1_module,
            m_c_score, m_s_score, m_t_score, m_l_score
        )
        VALUES (?, '小学奥数', ?, ?, '解答题', ?, ?, ?, ?, ?, ?, ?);
        """,
        (uid, text, visual_facts, ver_status, skel, primary_l1, c, s, t, l_val),
    )
    conn.execute(
        "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解析');",
        (f"{uid}::b1", uid),
    )
    for idx, (l1, l2, l3, nid) in enumerate(steps or [], start=1):
        conn.execute(
            """
            INSERT INTO question_steps(
                step_id, question_uid, branch_id, step_index, sub_goal,
                raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id
            )
            VALUES (?, ?, 'branch_1', ?, '步骤', ?, ?, ?, ?, ?, ?);
            """,
            (f"{uid}::b1::{idx}", uid, idx, l1, l2, l3, l1, l2, nid),
        )
    for m in motifs or []:
        cid = f"MOTIF:{m}"
        conn.execute("INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed) VALUES (?, 'MOTIF', ?, 0);", (cid, m))
        conn.execute("INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'MOTIF', 'MACHINE');", (uid, cid))
    for tr in traps or []:
        conn.execute("INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');", (uid, tr))

    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK');",
        (uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)


def test_stage3d_schema_flatten_and_default_mocks() -> None:
    flat = flatten_json_schema(Stage3dClusterAssignOutput)
    flat_txt = json.dumps(flat, ensure_ascii=False)
    assert '"$defs"' not in flat_txt
    assert '"$ref"' not in flat_txt

    # 支持列表及兼容字典形式的 assignments 校验
    parsed = Stage3dClusterAssignOutput.model_validate(
        {
            "canonical_operators": [{"canonical_name": "等高面积转化", "description": "同底或等高比"}],
            "assignments": {"同底等高求面积": "等高面积转化"},
        }
    )
    assert len(parsed.assignments) == 1
    assert parsed.assignments[0].canonical_name == "等高面积转化"

    m_s3d, _ = mocks.resolve_mock(Stage3dClusterAssignOutput, slot="judge_llm", stage="STAGE3D_COMPACT")
    assert isinstance(m_s3d, Stage3dClusterAssignOutput)

    m_s2, _ = mocks.resolve_mock(Stage2OnlyOutput, slot="text_llm", stage="STAGE2_ONLY")
    assert isinstance(m_s2, Stage2OnlyOutput)
    assert m_s2.task_id == "MOCK_S2_TASK"


def test_apply_cluster_assignments_and_revert_and_canonical_merge(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "ca.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject='小学奥数';")

    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        VALUES
        ('l2_eq', '小学奥数', '1.0', 'L2', '平面几何', '等高模型', ''),
        ('n1', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '同底等高三角形面积比'),
        ('n2', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '等高三角形面积转化'),
        ('n3', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '共高线段比换面积'),
        ('n_other', '小学奥数', '1.0', 'L3', '平面几何', '鸟头模型', '共角三角形面积比');
        """
    )
    _seed_q(conn, "q1", text="t1", steps=[("平面几何", "等高模型", "同底等高三角形面积比", "n1")])
    _seed_q(conn, "q2", text="t2", steps=[("平面几何", "等高模型", "等高三角形面积转化", "n2")])
    _seed_q(conn, "q3", text="t3", steps=[("平面几何", "等高模型", "共高线段比换面积", "n3")])

    conn.execute(
        """
        INSERT INTO concepts(concept_id, kind, name, is_seed)
        VALUES ('c_m1', 'METHOD', '列表枚举法', 0), ('c_m2', 'METHOD', '逐项枚举', 0);
        """
    )
    conn.execute("INSERT INTO question_concepts(question_uid, concept_id, kind, origin) VALUES ('q1', 'c_m1', 'METHOD', 'HUMAN'), ('q1', 'c_m2', 'METHOD', 'MACHINE');")
    conn.commit()

    scope = {"grade_subject": "小学奥数", "taxonomy_version": "1.0", "l1_module": "平面几何", "l2_entity": "等高模型"}

    # 1. 严禁 L2 / 跨作用域 / TRAP / is_seed=1
    with pytest.raises(PreconditionError, match="L2"):
        apply_cluster_assignments(
            conn,
            target_kind="KNOWLEDGE_NODE",
            scope_dict=scope,
            canonical_operators=[],
            assignments=[{"item_id": "l2_eq", "raw_l3": "等高模型", "canonical_name": "标准算子"}],
            batch_id="bad_l2",
        )
    with pytest.raises(PreconditionError, match="跨作用域"):
        apply_cluster_assignments(
            conn,
            target_kind="KNOWLEDGE_NODE",
            scope_dict=scope,
            canonical_operators=[],
            assignments=[{"item_id": "n_other", "raw_l3": "共角三角形面积比", "canonical_name": "标准算子"}],
            batch_id="bad_scope",
        )
    with pytest.raises(PreconditionError, match="TRAP"):
        apply_cluster_assignments(
            conn,
            target_kind="CONCEPT",
            scope_dict={"kind": "TRAP"},
            canonical_operators=[],
            assignments=[{"item_id": "TRAP:TRAP_DOMAIN_BOUNDARY", "raw_l3": "边界", "canonical_name": "新陷阱"}],
            batch_id="bad_trap",
        )

    # 2. L3 Cluster-then-Assign：新建标准主节点“等高面积比转化”并归并 n1, n2；n3 单独归并到已有标准名
    res = apply_cluster_assignments(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict=scope,
        canonical_operators=[Stage3dCanonicalOperator(canonical_name="等高面积比转化", description="标准等高比例算子")],
        assignments=[
            Stage3dClusterAssignment(item_id="n1", raw_l3="同底等高三角形面积比", canonical_name="等高面积比转化", reason="同义归纳"),
            Stage3dClusterAssignment(raw_l3="等高三角形面积转化", canonical_name="等高面积比转化", reason="同义归纳"),
        ],
        batch_id="ca_batch_1",
        slug="primary_olympiad",
        grade_subject="小学奥数",
        taxonomy_version="1.0",
    )
    assert res["canonical_nodes_created"] == 1
    assert res["applied"] == 2
    assert res["affected_questions"] == 2

    master_row = conn.execute(
        "SELECT node_id, redirect_to_id FROM knowledge_nodes WHERE l3_operator = '等高面积比转化' AND redirect_to_id IS NULL;"
    ).fetchone()
    assert master_row is not None
    master_id = str(master_row["node_id"])
    assert expand_canonical_node_ids(conn, master_id) == {master_id, "n1", "n2"}
    assert json.loads(conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid = 'q1';").fetchone()["eff_node_ids_json"]) == [master_id]

    # 3. 路径压缩（跳数 <= 1）：再把 master_id 归并到 n3，n1/n2 必须直接指向 n3
    res2 = apply_merge_pairs(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict=scope,
        merges=[{"from_id": master_id, "to_id": "n3", "canonical_name": "共高线段比换面积", "evidence": "二次收敛"}],
        batch_id="ca_batch_2",
    )
    assert res2["applied"] == 1
    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = 'n1';").fetchone()["redirect_to_id"] == "n3"
    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = ?;", (master_id,)).fetchone()["redirect_to_id"] == "n3"

    # 4. 逆序回滚 ca_batch_2 -> ca_batch_1 无损恢复
    revert_merge_batch(conn, "ca_batch_2", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = 'n1';").fetchone()["redirect_to_id"] == master_id

    revert_merge_batch(conn, "ca_batch_1", backups_dir=tmp_path / "bk", lock_path=tmp_path / ".lock")
    assert conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = 'n1';").fetchone()["redirect_to_id"] is None
    assert json.loads(conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid = 'q1';").fetchone()["eff_node_ids_json"]) == ["n1"]

    # 5. CONCEPT Cluster-then-Assign：创建新标准 METHOD 并保留 HUMAN 来源
    c_res = apply_cluster_assignments(
        conn,
        target_kind="CONCEPT",
        scope_dict={"kind": "METHOD"},
        canonical_operators=[{"canonical_name": "标准枚举法", "description": "有序枚举"}],
        assignments=[
            {"raw_l3": "列表枚举法", "canonical_name": "标准枚举法"},
            {"raw_l3": "逐项枚举", "canonical_name": "标准枚举法"},
        ],
        batch_id="ca_c1",
    )
    assert c_res["applied"] == 2
    qc = conn.execute("SELECT concept_id, origin FROM question_concepts WHERE question_uid = 'q1' AND kind = 'METHOD';").fetchall()
    assert [(r["concept_id"], r["origin"]) for r in qc] == [("METHOD:标准枚举法", "HUMAN")]
    conn.close()


@pytest.mark.asyncio
async def test_stage3_servable_filter_and_cluster_compaction_over_80(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = get_connection(tmp_path / "s3.db")
    init_db(conn)
    cfg_dir = tmp_path / "config"
    # 初始化默认 taxonomy.draft
    conn.execute("UPDATE grade_subjects SET taxonomy_status='DRAFT', taxonomy_version='draft-1' WHERE grade_subject='小学奥数';")
    from shuati.core.taxonomy import (
        TaxonomyL1Entry,
        TaxonomyL2Entry,
        TaxonomySpec,
        default_taxonomy_path,
        save_taxonomy_spec,
    )

    spec = TaxonomySpec(
        grade_subject="小学奥数",
        slug="primary_olympiad",
        status="DRAFT",
        version="draft-1",
        modules=[
            TaxonomyL1Entry(
                name="平面几何",
                l2_entities=[TaxonomyL2Entry(name="等高模型")],
            )
        ],
    )
    t_path = save_taxonomy_spec(spec, default_taxonomy_path(cfg_dir, "primary_olympiad", "DRAFT"))
    conn.execute("UPDATE grade_subjects SET taxonomy_file = ? WHERE grade_subject = '小学奥数';", (str(t_path),))

    # 插入 1 道可服务题 + 1 道 NEEDS_REVIEW 题（其专属脏标签绝不可进入 Stage 3a / 3e）
    _seed_q(
        conn,
        "q_ok",
        text="三角形面积问题",
        steps=[("平面几何", "等高模型", "算子甲", None)],
        ver_status="VERIFIED_CONSENSUS",
    )
    _seed_q(
        conn,
        "q_bad",
        text="待复核污染题",
        steps=[("污染模块XYZ", "污染L2_ONLY_BAD", "算子乙", None), ("平面几何", "等高模型", "算子丙", None)],
        ver_status="NEEDS_REVIEW",
    )

    # 在同一 (平面几何, 等高模型) L2 域下插入 95 条去重 L3 节点以测试 >80 预聚拆批
    for i in range(1, 96):
        conn.execute(
            """
            INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
            VALUES (?, '小学奥数', 'draft-1', 'L3', '平面几何', '等高模型', ?);
            """,
            (f"n_bulk_{i:03d}", f"等高细分算子_{i:03d}"),
        )
    conn.commit()

    settings = Settings()
    settings.pipeline.use_mock_llm = True

    # 1. Stage 3a 过滤 eff_is_servable=1（导出模式检查不含“污染L2_ONLY_BAD”）
    exp_3a = await propose_taxonomy_async(
        conn, settings, grade_subject="小学奥数", mode="export", tasks_dir=tmp_path / "p_tasks", config_dir=cfg_dir, use_mock=True
    )
    assert exp_3a["distinct_pairs"] == 1
    p_batch = (tmp_path / "p_tasks" / "batches" / "batch_0001.jsonl").read_text(encoding="utf-8")
    assert "污染L2_ONLY_BAD" not in p_batch

    # 2. Stage 3e 过滤 eff_is_servable=1（q_bad 的步骤转移不计入）
    exp_3e = await run_stage3e_async(
        conn, settings, grade_subject="小学奥数", mode="export", tasks_dir=tmp_path / "e_tasks", config_dir=cfg_dir
    )
    assert exp_3e["transitions"] == 0

    # 3. Stage 3d Cluster-then-Assign export/import + >80 预聚分簇 + subagent_model 默认 gemini-4-neon-medium
    store = VectorStore(tmp_path / "vecs")
    export_dir = tmp_path / "cmp_tasks"
    exp_cmp = await run_compaction_async(
        conn,
        settings,
        store,
        grade_subject="小学奥数",
        target="l3",
        mode="export",
        tasks_dir=export_dir,
        config_dir=cfg_dir,
        use_mock=True,
    )
    assert exp_cmp["strategy"] == "cluster"
    assert exp_cmp["candidate_items"] == 95
    assert exp_cmp["batch_count"] >= 2

    manifest = exp_cmp["manifest"]
    assert manifest["subagent_model"] == "gemini-4-neon-medium"
    assert manifest["schema_name"] == "Stage3dClusterAssignOutput"

    all_exported_ids: list[str] = []
    for bf in sorted((export_dir / "batches").glob("batch_*.jsonl")):
        rec = json.loads(bf.read_text(encoding="utf-8").strip())
        assert len(rec["task_item_ids"]) <= 80
        assert "secret_col" not in json.dumps(rec, ensure_ascii=False)
        assert "secret_chap" not in json.dumps(rec, ensure_ascii=False)
        all_exported_ids.extend(rec["task_item_ids"])
    assert len(all_exported_ids) == 95
    assert len(set(all_exported_ids)) == 95

    # 回灌 import 模式验证写入并查集与账本
    res_lines = []
    for bf in sorted((export_dir / "batches").glob("batch_*.jsonl")):
        rec = json.loads(bf.read_text(encoding="utf-8").strip())
        assignments = [
            {"raw_l3": raw_op, "canonical_name": "等高比例标准算子", "reason": "同域归纳"}
            for raw_op in rec["task_item_ids"]
        ]
        res_lines.append(
            json.dumps(
                {
                    "batch_id": rec["batch_id"],
                    "result": {
                        "canonical_operators": [{"canonical_name": "等高比例标准算子", "description": "等高比"}],
                        "assignments": assignments,
                    },
                },
                ensure_ascii=False,
            )
        )
    res_path = tmp_path / "cmp_results.jsonl"
    res_path.write_text("\n".join(res_lines) + "\n", encoding="utf-8")

    imp_cmp = await run_compaction_async(
        conn,
        settings,
        store,
        grade_subject="小学奥数",
        target="l3",
        mode="import",
        tasks_dir=export_dir,
        results_path=res_path,
        config_dir=cfg_dir,
        use_mock=True,
    )
    assert imp_cmp["applied_merges"] == 95
    assert imp_cmp["batch_errors"] == 0
    active_l3_cnt = conn.execute(
        "SELECT COUNT(*) FROM knowledge_nodes WHERE grade_subject='小学奥数' AND taxonomy_version='draft-1' AND node_level='L3' AND redirect_to_id IS NULL;"
    ).fetchone()[0]
    assert active_l3_cnt == 1

    # 4. api 模式调用 MOCK_RESPONSES 走 Cluster-then-Assign
    conn.execute("INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES ('nx1', '小学奥数', 'draft-1', 'L3', '平面几何', '等高模型', '同底面积比法');")
    conn.commit()
    monkeypatch.setitem(
        mocks.MOCK_RESPONSES,
        "STAGE3D_COMPACT:judge_llm",
        {
            "canonical_operators": [{"canonical_name": "等高比例标准算子", "description": "归并"}],
            "assignments": [
                {"raw_l3": "同底面积比法", "canonical_name": "等高比例标准算子"},
                {"raw_l3": "等高比例标准算子", "canonical_name": "等高比例标准算子"},
            ],
        },
    )
    api_cmp = await run_compaction_async(
        conn, settings, store, grade_subject="小学奥数", target="l3", mode="api", config_dir=cfg_dir, use_mock=True
    )
    assert api_cmp["applied_merges"] == 1
    conn.close()


@pytest.mark.asyncio
async def test_stage4_vector_concat_servable_isolation_and_wide_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = get_connection(tmp_path / "s4.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject='小学奥数';")

    for nid, l1, l2, l3 in (
        ("n1", "平面几何", "等高模型", "底乘高除二"),
        ("n2", "平面几何", "等高模型", "等高面积比例"),
        ("n3", "平面几何", "等高模型", "复杂辅助线切割"),
        ("n4", "数论基础", "整除与同余", "余数性质转化"),
    ):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, '小学奥数', '1.0', 'L3', ?, ?, ?);",
            (nid, l1, l2, l3),
        )

    # q_child(low, 1120) 与 q_parent(high, 1850) 共享核心 L2 (平面几何/等高模型)，但 L3 不完全相同（n1 vs n3）-> 走 WIDE_RECALL_SCAFFOLD
    # q_iso_a & q_iso_b: 同为 mid(1510)，带有 df=1 伪 MOTIF（应被过滤）和 df=2 经典 MOTIF（应保留），surface_pct=0.75 (>0.60) 但非 CLONE -> 走 ISOMORPHIC 宽进精裁
    # q_nr: NEEDS_REVIEW -> 默认必须被 reindex 与 stage4_weave 隔离排除
    _seed_q(
        conn,
        "q_child",
        text="已知底和高求三角形面积",
        visual_facts="图中标注底边BC=6，高AD=4",
        skel="底乘高直接面积计算模型",
        elo_tier="low",
        steps=[("平面几何", "等高模型", "底乘高除二", "n1")],
        motifs=["基础面积公式", "仅出现一次的伪母题A"],
        traps=["TRAP:TRAP_ARITHMETIC_SLIP"],
    )
    _seed_q(
        conn,
        "q_parent",
        text="复杂多边形分割求阴影面积",
        visual_facts="",
        skel="底乘高面积比例拓展分割模型",
        elo_tier="high",
        steps=[("平面几何", "等高模型", "复杂辅助线切割", "n3")],
        motifs=["基础面积公式"],
        traps=["TRAP:TRAP_ARITHMETIC_SLIP", "TRAP:TRAP_DOMAIN_BOUNDARY"],
    )
    _seed_q(
        conn,
        "q_iso_a",
        text="农夫分田地几何面积比例",
        visual_facts="   ",
        skel="同余与面积双模型同构骨架",
        elo_tier="mid",
        steps=[("平面几何", "等高模型", "等高面积比例", "n2")],
        motifs=["经典同构母题"],
        traps=["TRAP:TRAP_DOMAIN_BOUNDARY", "TRAP:TRAP_SIGN_DIRECTION"],
    )
    _seed_q(
        conn,
        "q_iso_b",
        text="齿轮转速比与余数周期问题",
        visual_facts="",
        skel="同余与面积双模型同构骨架变体",
        elo_tier="mid",
        steps=[("数论基础", "整除与同余", "余数性质转化", "n4")],
        motifs=["经典同构母题", "仅出现一次的伪母题B"],
        traps=[],
    )
    _seed_q(
        conn,
        "q_nr",
        text="待人工复核的坏题",
        visual_facts="模糊读图",
        skel="模糊骨架",
        elo_tier="mid",
        ver_status="NEEDS_REVIEW",
        steps=[("平面几何", "等高模型", "底乘高除二", "n1")],
    )
    conn.commit()

    # 1. 验证 reindex_vectors_and_quantiles_async 拼接 surface 文本及隔离 eff_is_servable=0
    captured_surface: list[tuple[str, str]] = []
    orig_sync = VectorStore.sync_vectors

    async def _spy_sync(self, namespace, items, embed_client, **kwargs):
        if namespace == "surface":
            captured_surface.extend(items)
        return await orig_sync(self, namespace, items, embed_client, **kwargs)

    monkeypatch.setattr(VectorStore, "sync_vectors", _spy_sync)

    settings = Settings()
    settings.pipeline.use_mock_llm = True
    vec_dir = tmp_path / "vecs"
    await reindex_vectors_and_quantiles_async(
        conn, settings, grade_subject="小学奥数", vectors_dir=vec_dir, use_mock=True
    )

    surf_dict = dict(captured_surface)
    assert "q_nr" not in surf_dict
    assert surf_dict["q_child"] == "已知底和高求三角形面积\n图中标注底边BC=6，高AD=4"
    assert surf_dict["q_iso_a"] == "农夫分田地几何面积比例"  # 仅空白 eff_visual_facts 不拼接换行

    # 2. 验证 load_question_features: l2_entities 提取、df==1 伪 MOTIF 过滤、df>=2 保留、TRAP_ARITHMETIC_SLIP 排除、平滑 IDF (+1.0)
    feats = load_question_features(conn, grade_subject="小学奥数")
    assert "q_nr" not in feats
    assert "平面几何/等高模型" in feats["q_child"].l2_entities
    assert "M:MOTIF:仅出现一次的伪母题A" not in feats["q_child"].k_union
    assert "M:MOTIF:基础面积公式" in feats["q_child"].k_union
    assert "M:MOTIF:经典同构母题" in feats["q_iso_a"].k_union
    assert "M:MOTIF:经典同构母题" in feats["q_iso_b"].k_union
    assert "M:MOTIF:仅出现一次的伪母题B" not in feats["q_iso_b"].k_union
    assert "T:TRAP:TRAP_ARITHMETIC_SLIP" not in feats["q_child"].e_traps

    idf_k, idf_t = compute_idf_weights(feats)
    expected_idf = math.log((4 + 1.0) / (2 + 1.0)) + 1.0
    assert idf_k["M:MOTIF:经典同构母题"] == pytest.approx(expected_idf)
    assert all(v >= 1.0 for v in idf_t.values())

    # 3. 写入可控向量矩阵验证 ISOMORPHIC & SCAFFOLD 宽进召回标记
    store = VectorStore(vec_dir)
    uids = ["q_child", "q_iso_a", "q_iso_b", "q_parent"]
    surf_vecs = np.asarray(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.78, 0.625, 0.0],
            [0.0, 0.75, -0.66, 0.0],  # surface_sim ~ 0.17, 若用分位 0.75 测 surface_pct > 0.60
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )
    # 故意让 q_iso_a 与 q_iso_b 的 surface_sim=0.75 (s_pct=0.75 > 0.60, < clone 0.995) 且骨架相似 k_sim=0.88
    surf_vecs[1] = np.asarray([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    surf_vecs[2] = np.asarray([0.0, 0.75, 0.6614, 0.0], dtype=np.float32)
    skel_vecs = np.asarray(
        [
            [1.0, 0.0, 0.0, 0.0],      # q_child
            [0.0, 1.0, 0.0, 0.0],      # q_iso_a
            [0.0, 0.88, 0.475, 0.0],   # q_iso_b ~ q_iso_a (k_sim=0.88)
            [0.86, 0.0, 0.0, 0.51],    # q_parent ~ q_child (k_sim=0.86)
        ],
        dtype=np.float32,
    )
    store._save_npz(store.npz_path("surface", "primary_olympiad"), uids, ["h"] * 4, surf_vecs)
    store._save_npz(store.npz_path("skeleton", "primary_olympiad"), uids, ["h"] * 4, skel_vecs)
    store._save_meta(
        {
            "embedding_model": settings.llm.embedding.model,
            "dimension": 4,
            "meta_version": 1,
            "quantiles": {
                "primary_olympiad::surface": [i / 100.0 for i in range(101)],
                "primary_olympiad::skeleton": [i / 100.0 for i in range(101)],
            },
        }
    )

    cands = generate_candidate_edges(
        conn, store, grade_subject="小学奥数", scope_slug="primary_olympiad", topology_cfg=settings.topology, features=feats
    )
    triples = {(e.source_uid, e.target_uid, e.relation_type): e for e in cands}

    # (q_iso_a, q_iso_b): s_pct=0.75 (>0.60) 不被封杀，且共享经典 MOTIF / 骨架近邻 -> ISOMORPHIC 宽进精裁
    iso_edge = triples.get(("q_iso_a", "q_iso_b", "ISOMORPHIC"))
    assert iso_edge is not None
    assert iso_edge.needs_llm_refine is True
    assert iso_edge.refine_reason == "WIDE_RECALL_ISOMORPHIC"

    # (q_child, q_parent): 共享 L2 + skeleton 近邻，严格锁死 q_child -> q_parent 且绝无反向
    scaf_edge = triples.get(("q_child", "q_parent", "SCAFFOLD"))
    assert scaf_edge is not None
    assert scaf_edge.needs_llm_refine is True
    assert scaf_edge.refine_reason == "WIDE_RECALL_SCAFFOLD"
    assert ("q_parent", "q_child", "SCAFFOLD") not in triples

    # 4. 验证 run_stage4_weave_async 经 LLM 精裁后落库（同时验证 sync_vectors 仅传入 eff_is_servable=1 与拼接文本）
    stage4_surface_captured: list[tuple[str, str]] = []

    async def _noop_sync(self, namespace, items, embed_client, **kwargs):
        if namespace == "surface":
            stage4_surface_captured.extend(items)
        return {"synced": 0}

    monkeypatch.setattr(VectorStore, "sync_vectors", _noop_sync)
    exp_s4 = await run_stage4_weave_async(
        conn,
        settings,
        grade_subject="小学奥数",
        mode="export",
        tasks_dir=tmp_path / "s4_tasks",
        vectors_dir=vec_dir,
        use_mock=True,
    )
    assert exp_s4["refine_candidates"] >= 2
    s4_surf_dict = dict(stage4_surface_captured)
    assert "q_nr" not in s4_surf_dict
    assert s4_surf_dict["q_child"] == "已知底和高求三角形面积\n图中标注底边BC=6，高AD=4"

    # 走 import 确认宽进边经 LLM 精裁落库
    s4_batch = json.loads((tmp_path / "s4_tasks" / "batches" / "batch_0001.jsonl").read_text(encoding="utf-8").strip())
    s4_res_file = tmp_path / "s4_results.jsonl"
    s4_res_file.write_text(
        json.dumps(
            {
                "batch_id": s4_batch["batch_id"],
                "result": {
                    "decisions": [
                        {"pair_id": pid, "is_valid": True, "pedagogical_note": "LLM精裁确认"}
                        for pid in s4_batch["task_item_ids"]
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    imp_s4 = await run_stage4_weave_async(
        conn,
        settings,
        grade_subject="小学奥数",
        mode="import",
        tasks_dir=tmp_path / "s4_tasks",
        results_path=s4_res_file,
        vectors_dir=vec_dir,
        use_mock=True,
    )
    assert imp_s4["blocked_unservable"] == 1
    assert conn.execute("SELECT confidence_source FROM question_relations WHERE source_question_uid='q_child' AND target_question_uid='q_parent' AND relation_type='SCAFFOLD';").fetchone()["confidence_source"] == "LLM_VERIFIED"
    assert conn.execute("SELECT confidence_source FROM question_relations WHERE source_question_uid='q_iso_a' AND target_question_uid='q_iso_b' AND relation_type='ISOMORPHIC';").fetchone()["confidence_source"] == "LLM_VERIFIED"

    # 5. 验证 weave_single_question 的 SCAFFOLD 方向锁死（D-Audit-5）与不直接写入未精裁 WIDE_RECALL_*
    conn.execute("DELETE FROM question_relations;")
    # 插入一道具备 core SCAFFOLD 条件的更低分题 q_base(elo=1120) -> q_iso_a(elo=1510, k_union >= 2)
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES ('n5', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '拓展综合');"
    )
    _seed_q(
        conn,
        "q_base",
        text="基础前置题",
        skel="基础前置骨架",
        elo_tier="low",
        steps=[("平面几何", "等高模型", "等高面积比例", "n2")],
        motifs=["经典同构母题"],
    )
    conn.execute("INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id) VALUES ('q_iso_a::b1::2', 'q_iso_a', 'branch_1', 2, '拓展', '平面几何', '等高模型', '拓展综合', '平面几何', '等高模型', 'n5');")
    effective.recompute(conn, "q_iso_a")
    conn.commit()

    w_res = weave_single_question(conn, store, question_uid="q_base", topology_cfg=settings.topology)
    assert w_res["written"] >= 1
    assert conn.execute("SELECT 1 FROM question_relations WHERE source_question_uid='q_base' AND target_question_uid='q_iso_a' AND relation_type='SCAFFOLD';").fetchone() is not None
    assert conn.execute("SELECT 1 FROM question_relations WHERE source_question_uid='q_iso_a' AND target_question_uid='q_base' AND relation_type='SCAFFOLD';").fetchone() is None
    assert conn.execute("SELECT 1 FROM question_relations WHERE source_question_uid='q_child' AND target_question_uid='q_parent' AND relation_type='SCAFFOLD';").fetchone() is None
    conn.close()
