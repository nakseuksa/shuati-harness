"""P2 单元测试（Worker A）：向量分位漂移基准、单侧边界带与精裁池预截断、Stage 4 差量织网、挂起批过滤与 Stage Q 扩展审计。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from shuati.config import Settings, TopologyConfig
from shuati.core import effective
from shuati.core.topology import (
    CandidateEdge,
    _near_threshold,
    generate_candidate_edges,
    load_question_features,
    pretruncate_refine_candidates,
)
from shuati.db import human_mirror
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm.embeddings import EmbeddingClient
from shuati.pipeline.audit import run_stage_q_audit
from shuati.pipeline.stage4_weave import (
    reindex_vectors_and_quantiles_async,
    run_stage4_weave_async,
)
from shuati.services.compiler_tools import get_pipeline_status, scan_pending_batches
from shuati.vectors.store import VectorStore, evaluate_scope_drift

GS = "小学奥数"
SLUG = "primary_olympiad"


def _write_temp_config(cfg_dir: Path, *, embed_model: str = "text-embedding-3-large") -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.yaml").write_text(
        (
            "llm:\n"
            "  embedding:\n"
            f'    model: "{embed_model}"\n'
            "pipeline:\n"
            "  use_mock_llm: true\n"
            "  consensus_audit_ratio: 0.05\n"
        ),
        encoding="utf-8",
    )
    (cfg_dir / "method_blacklist.yaml").write_text(
        (
            "blacklist_terms:\n"
            "  - 转化与化归\n"
            "  - 逻辑推理\n"
            "  - 数形结合\n"
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


def _seed_question(
    conn: Any,
    uid: str,
    *,
    source_id: str | None = None,
    text: str = "求面积",
    skel: str = "几何面积比例骨架",
    primary_l1: str = "平面几何",
    m_format: str = "解答题",
    ver_status: str = "VERIFIED_CONSENSUS",
    has_images: int = 0,
    cstl: tuple[int, int, int, int] = (2, 2, 2, 1),
    nids: list[str] | None = None,
    methods: list[str] | None = None,
    motifs: list[str] | None = None,
    traps: list[str] | None = None,
    legacy_tags: dict[str, Any] | None = None,
    content_hash: str | None = None,
    s3d_status: str = "OK",
    s4_status: str = "PENDING",
    answer_val: str = "12",
) -> None:
    sid = source_id or uid.split(":")[-1]
    chash = content_hash or f"hash_{uid}"
    ans_json = json.dumps(
        [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": answer_val,
                "sympy_expr": answer_val,
            }
        ],
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id,
            raw_question, legacy_tags_json, content_hash
        ) VALUES (?, ?, 'BATCH_JSONL', 'ch01', ?, ?, ?, ?);
        """,
        (uid, GS, sid, text, json.dumps(legacy_tags or {}, ensure_ascii=False), chash),
    )
    c, s, t, l_val = cstl
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, has_images, m_question_text, m_format,
            m_verification_status, m_final_answers_json, m_abstract_skeleton,
            m_primary_l1_module, m_c_score, m_s_score, m_t_score, m_l_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (uid, GS, has_images, text, m_format, ver_status, ans_json, skel, primary_l1, c, s, t, l_val),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '标准解析');
        """,
        (f"{uid}::b1", uid),
    )
    for idx, nid in enumerate(nids or [], start=1):
        conn.execute(
            """
            INSERT INTO question_steps(
                step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id
            ) VALUES (?, ?, 'branch_1', ?, '子目标', ?, '等高模型', ?);
            """,
            (f"{uid}::b1::{idx}", uid, idx, primary_l1, nid),
        )
    for m_name in methods or []:
        cid = f"METHOD:{m_name}"
        conn.execute(
            "INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed) VALUES (?, 'METHOD', ?, 0);",
            (cid, m_name),
        )
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'METHOD', 'MACHINE');",
            (uid, cid),
        )
    for mo_name in motifs or []:
        cid = f"MOTIF:{mo_name}"
        conn.execute(
            "INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed) VALUES (?, 'MOTIF', ?, 0);",
            (cid, mo_name),
        )
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'MOTIF', 'MACHINE');",
            (uid, cid),
        )
    for tr_id in traps or []:
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
            (uid, tr_id),
        )
    for stg, st_val in (("S1", "OK"), ("S2", "OK"), ("S3C", "OK"), ("S3D", s3d_status), ("S4", s4_status)):
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, ?);",
            (uid, stg, st_val),
        )
    effective.recompute(conn, uid)


def test_config_refine_pool_multiplier_bounds() -> None:
    cfg = TopologyConfig()
    assert cfg.refine_pool_multiplier == 3
    assert TopologyConfig(refine_pool_multiplier=1).refine_pool_multiplier == 1
    with pytest.raises(ValidationError):
        TopologyConfig(refine_pool_multiplier=0)
    with pytest.raises(ValidationError):
        TopologyConfig(refine_pool_multiplier=-2)


@pytest.mark.asyncio
async def test_vector_store_scopes_baseline_drift_and_model_rebuild_reset(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "vectors")
    meta0 = store.load_meta()
    assert meta0["scopes"] == {}

    # 1. 有可服务题但尚无 scopes 基准
    no_base = evaluate_scope_drift(meta0, SLUG, 10, {"平面几何": 0.6, "行程问题": 0.4})
    assert no_base["has_baseline"] is False
    assert no_base["drifted"] is False
    assert any("reindex --quantiles" in r for r in no_base["reasons"])

    # 空题集无基准时不报漂移
    empty_base = evaluate_scope_drift(meta0, SLUG, 0, {})
    assert empty_base["has_baseline"] is False
    assert empty_base["drifted"] is False
    assert empty_base["reasons"] == []

    # 2. 写入向量并冻结带 scope_baseline 的分位表
    settings = Settings()
    settings.llm.embedding.model = "embed-v1"
    client1 = EmbeddingClient(settings, use_mock=True)
    items = [("q1", "长方形面积"), ("q2", "三角形面积"), ("q3", "相遇问题")]
    await store.sync_vectors("surface", items, client1, scope_slug=SLUG)
    await store.sync_vectors("skeleton", items, client1, scope_slug=SLUG)
    await client1.aclose()

    base_l1_dist: dict[str, float] = {
        "平面几何": 0.25,
        "行程问题": 0.25,
        "数论基础": 0.25,
        "组合计数": 0.25,
    }
    baseline: dict[str, Any] = {
        "n0": 20,
        "l1_dist": base_l1_dist,
    }
    store.rebuild_quantiles(SLUG, ("surface", "skeleton"), scope_baseline=baseline)
    meta1 = store.load_meta()
    assert SLUG in meta1["scopes"]
    assert meta1["scopes"][SLUG]["n0"] == 20
    assert meta1["scopes"][SLUG]["l1_dist"]["平面几何"] == pytest.approx(0.25)
    assert meta1["scopes"][SLUG]["updated_at"]

    # 2a. 轻微波动未触发漂移（+10% < 20%, 单 L1 +0.05 < 0.10, TVD = 0.05 < 0.15）
    ok_res = store.evaluate_scope_drift(
        SLUG,
        22,
        {"平面几何": 0.30, "行程问题": 0.20, "数论基础": 0.25, "组合计数": 0.25},
    )
    assert ok_res["has_baseline"] is True
    assert ok_res["drifted"] is False
    assert ok_res["reasons"] == []

    # 2b. 题量变化 >= 20% (20 -> 24, 恰 20%)
    growth_res = store.evaluate_scope_drift(SLUG, 24, base_l1_dist)
    assert growth_res["drifted"] is True
    assert growth_res["rel_growth"] == pytest.approx(0.20)
    assert any("20%" in r for r in growth_res["reasons"])

    # 2c. 单 L1 偏移 >= 0.10（0.25 -> 0.35，精确边界 0.10，TVD = 0.10 < 0.15）
    l1_res = store.evaluate_scope_drift(
        SLUG,
        20,
        {"平面几何": 0.35, "行程问题": 0.15, "数论基础": 0.25, "组合计数": 0.25},
    )
    assert l1_res["drifted"] is True
    assert l1_res["max_l1_delta"] == pytest.approx(0.10)
    assert l1_res["tvd"] == pytest.approx(0.10)
    assert any("0.10" in r for r in l1_res["reasons"])

    # 2d. TVD >= 0.15（各单模块仅偏移 0.08 < 0.10，但 TVD = 0.5 * 0.32 = 0.16 >= 0.15）
    tvd_res = store.evaluate_scope_drift(
        SLUG,
        20,
        {"平面几何": 0.33, "行程问题": 0.33, "数论基础": 0.17, "组合计数": 0.17},
    )
    assert tvd_res["drifted"] is True
    assert tvd_res["max_l1_delta"] == pytest.approx(0.08)
    assert tvd_res["tvd"] == pytest.approx(0.16)
    assert any("TVD" in r for r in tvd_res["reasons"])

    # 3. 模型切换 force_rebuild=True 自动清空 quantiles 与 scopes
    settings2 = Settings()
    settings2.llm.embedding.model = "embed-v2"
    client2 = EmbeddingClient(settings2, use_mock=True)
    await store.sync_vectors("surface", items, client2, scope_slug=SLUG, force_rebuild=True)
    await client2.aclose()
    meta_after_switch = store.load_meta()
    assert meta_after_switch["quantiles"] == {}
    assert meta_after_switch["scopes"] == {}

    # 4. clear_model_binding_for_rebuild 同样清空 scopes
    store.rebuild_quantiles(SLUG, ("surface",), scope_baseline=baseline)
    assert store.load_meta()["scopes"]
    store.clear_model_binding_for_rebuild()
    meta_cleared = store.load_meta()
    assert meta_cleared["quantiles"] == {}
    assert meta_cleared["scopes"] == {}


def test_topology_onesided_near_threshold_and_pretruncate_refine_candidates(tmp_path: Path) -> None:
    # 1. 单侧下探 _near_threshold 验证
    assert _near_threshold(0.71, 0.80, 0.10) is False
    assert _near_threshold(0.72, 0.80, 0.10) is True
    assert _near_threshold(0.799, 0.80, 0.10) is True
    assert _near_threshold(0.80, 0.80, 0.10) is False
    assert _near_threshold(0.85, 0.80, 0.10) is False  # 过硬阈值后绝不反向落入 BOUNDARY_BAND

    # 2. 端到端验证 iso_core_hit 与 sc_core 刚过线时免审直通 RULE_MATRIX
    conn = get_connection(tmp_path / "topo_onesided.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;", (GS,))
    for nid in ("n1", "n2", "n3", "n4"):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, ?, '1.0', 'L3', '平面几何', '等高模型', ?);",
            (nid, GS, f"算子{nid}"),
        )

    # q_iso1 & q_iso2: 同分 1510, k_union={'N:n1','N:n2'} -> k_jacc=1.0; k_sim 设为 0.78 (刚过 0.76 < 0.76*1.1=0.836)
    # q_low (C=2,S=1,T=1,L=1 -> elo=1290) -> q_iso1 (C=2,S=2,T=2,L=1 -> elo=1510): elo_gap=220 (刚过 200 < 200*1.1=220!)
    _seed_question(conn, "p:c:q_iso1", text="甲题", skel="骨架1", cstl=(2, 2, 2, 1), nids=["n1", "n2"])
    _seed_question(conn, "p:c:q_iso2", text="乙题", skel="骨架2", cstl=(2, 2, 2, 1), nids=["n1", "n2"])
    _seed_question(conn, "p:c:q_high", text="高阶母题", skel="母题骨架", cstl=(3, 2, 2, 1), nids=["n1", "n2", "n3", "n4"])  # elo=1850
    _seed_question(conn, "p:c:q_mid2", text="子题恰过线", skel="子题骨架", cstl=(2, 3, 1, 3), nids=["n1", "n2"])  # elo=1630, 1850-1630=220 (恰在旧版 +10% 误判带!)
    conn.commit()

    store = VectorStore(tmp_path / "vecs")
    uids = ["p:c:q_high", "p:c:q_iso1", "p:c:q_iso2", "p:c:q_mid2"]
    surf_vecs = np.eye(4, dtype=np.float32)  # 表皮正交避免触发 CLONE
    skel_vecs = np.asarray(
        [
            [0.0, 0.0, 1.0, 0.0],          # q_high
            [1.0, 0.0, 0.0, 0.0],          # q_iso1
            [0.78, 0.6258, 0.0, 0.0],      # q_iso2 (与 q_iso1 k_sim=0.78，刚过 0.76 硬阈值)
            [0.0, 0.0, 0.0, 1.0],          # q_mid2
        ],
        dtype=np.float32,
    )
    store._save_npz(store.npz_path("surface", SLUG), uids, ["h"] * 4, surf_vecs)
    store._save_npz(store.npz_path("skeleton", SLUG), uids, ["h"] * 4, skel_vecs)
    store._save_meta(
        {
            "embedding_model": "mock",
            "dimension": 4,
            "meta_version": 1,
            "quantiles": {
                f"{SLUG}::surface": [i / 100.0 for i in range(101)],
                f"{SLUG}::skeleton": [i / 100.0 for i in range(101)],
            },
        }
    )

    cfg = TopologyConfig()
    cfg.thresholds.isomorphic.skeleton_pct = 0.75  # k_pct=0.78 刚过 0.75 硬阈值
    feats = load_question_features(conn, grade_subject=GS)
    edges = generate_candidate_edges(
        conn, store, grade_subject=GS, scope_slug=SLUG, topology_cfg=cfg, features=feats
    )
    emap = {(e.source_uid, e.target_uid, e.relation_type): e for e in edges}

    iso_e = emap[("p:c:q_iso1", "p:c:q_iso2", "ISOMORPHIC")]
    assert iso_e.needs_llm_refine is False
    assert iso_e.refine_reason == ""
    assert iso_e.confidence_source == "RULE_MATRIX"

    sc_e = emap[("p:c:q_mid2", "p:c:q_high", "SCAFFOLD")]
    assert sc_e.needs_llm_refine is False
    assert sc_e.refine_reason == ""
    assert sc_e.confidence_source == "RULE_MATRIX"
    conn.close()

    # 3. 精裁池预截断 pretruncate_refine_candidates 验证
    trunc_cfg = TopologyConfig(refine_pool_multiplier=2)
    trunc_cfg.thresholds.isomorphic.degree_cap = 2  # 预截断度上限 = 2 * 2 = 4
    trunc_cfg.thresholds.scaffold.out_degree_cap = 1  # 预截断出度上限 = 2
    trunc_cfg.thresholds.scaffold.in_degree_cap = 2  # 预截断入度上限 = 4

    raw_refines: list[CandidateEdge] = []
    for idx in range(1, 8):
        raw_refines.append(
            CandidateEdge(
                source_uid="hub",
                target_uid=f"peer_{idx:02d}",
                relation_type="ISOMORPHIC",
                is_directed=0,
                is_cross_scope=0,
                surface_sim=0.3,
                skeleton_sim=0.8,
                surface_pct=0.3,
                skeleton_pct=0.8,
                containment_score=0.5,
                trap_overlap=0.0,
                rank_score=round(0.1 * idx, 3),
                needs_llm_refine=True,
                refine_reason="WIDE_RECALL_ISOMORPHIC",
            )
        )
    for idx in range(1, 6):
        raw_refines.append(
            CandidateEdge(
                source_uid="sub_q",
                target_uid=f"parent_{idx:02d}",
                relation_type="SCAFFOLD",
                is_directed=1,
                is_cross_scope=0,
                surface_sim=0.2,
                skeleton_sim=0.7,
                surface_pct=0.2,
                skeleton_pct=0.7,
                containment_score=0.75,
                trap_overlap=0.0,
                rank_score=round(0.1 * idx, 3),
                needs_llm_refine=True,
                refine_reason="WIDE_RECALL_SCAFFOLD",
            )
        )

    kept = pretruncate_refine_candidates(raw_refines, trunc_cfg)
    iso_kept = [e for e in kept if e.relation_type == "ISOMORPHIC"]
    scaf_kept = [e for e in kept if e.relation_type == "SCAFFOLD"]
    assert [e.target_uid for e in iso_kept] == ["peer_07", "peer_06", "peer_05", "peer_04"]
    assert [e.target_uid for e in scaf_kept] == ["parent_05", "parent_04"]


@pytest.mark.asyncio
async def test_stage4_reindex_stale_drift_auto_rebuild_and_only_uids_incremental(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = get_connection(tmp_path / "s4_inc.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;", (GS,))
    for nid, l1_name, op in (
        ("n1", "平面几何", "等高转化"),
        ("n2", "平面几何", "燕尾比例"),
        ("n3", "行程问题", "相遇速度和"),
        ("n4", "行程问题", "追及速度差"),
    ):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, ?, '1.0', 'L3', ?, '核心模型', ?);",
            (nid, GS, l1_name, op),
        )

    _seed_question(conn, "primary_olympiad:ch01:q1", source_id="q1_short", text="几何题1", skel="几何同构骨架", primary_l1="平面几何", nids=["n1", "n2"], motifs=["共边模型"], s4_status="OK")
    _seed_question(conn, "primary_olympiad:ch01:q2", source_id="q2_short", text="几何题2", skel="几何同构骨架", primary_l1="平面几何", nids=["n1", "n2"], motifs=["共边模型"], s4_status="OK")
    _seed_question(conn, "primary_olympiad:ch01:q3", source_id="q3_short", text="行程题3", skel="行程同构骨架", primary_l1="行程问题", nids=["n3", "n4"], motifs=["相遇模型"], s4_status="OK")
    _seed_question(conn, "primary_olympiad:ch01:q4", source_id="q4_short", text="行程题4", skel="行程同构骨架", primary_l1="行程问题", nids=["n3", "n4"], motifs=["相遇模型"], s4_status="OK")
    _seed_question(conn, "primary_olympiad:ch01:q_nr", source_id="q_nr", text="待复核题", ver_status="NEEDS_REVIEW", nids=["n1"], s3d_status="PENDING", s4_status="PENDING")
    conn.commit()

    settings = Settings()
    settings.pipeline.use_mock_llm = True
    vec_dir = tmp_path / "vecs"
    lock_path = tmp_path / ".compile.lock"

    # 1. reindex_vectors_and_quantiles_async 写入 scopes 基准并将 S3D='OK' 可服务题 S4 置为 STALE
    re_res = await reindex_vectors_and_quantiles_async(
        conn, settings, grade_subject=GS, vectors_dir=vec_dir, use_mock=True
    )
    assert re_res["meta_version"] == 1
    store = VectorStore(vec_dir)
    meta = store.load_meta()
    assert meta["scopes"][SLUG]["n0"] == 4
    assert meta["scopes"][SLUG]["l1_dist"] == {"平面几何": 0.5, "行程问题": 0.5}

    s4_map = {
        r["question_uid"]: r["status"]
        for r in conn.execute("SELECT question_uid, status FROM compile_state WHERE stage='S4';").fetchall()
    }
    for k in ("q1", "q2", "q3", "q4"):
        assert s4_map[f"primary_olympiad:ch01:{k}"] == "STALE"
    assert s4_map["primary_olympiad:ch01:q_nr"] == "PENDING"

    # 2. 前置漂移防呆：人为篡改 meta["scopes"] 制造题量漂移 (n0=2 -> N=4, +100%)
    meta["scopes"][SLUG]["n0"] = 2
    store._save_meta(meta)

    # ignore_drift=True 跳过重算分位表
    res_ignored = await run_stage4_weave_async(
        conn,
        settings,
        grade_subject=GS,
        mode="api",
        vectors_dir=vec_dir,
        lock_path=lock_path,
        use_mock=True,
        ignore_drift=True,
    )
    assert res_ignored["quantiles_rebuilt"] is False
    assert store.load_meta()["meta_version"] == 1

    # 默认 ignore_drift=False 自动重算分位表并修复 scopes 基准
    res_rebuilt = await run_stage4_weave_async(
        conn,
        settings,
        grade_subject=GS,
        mode="api",
        vectors_dir=vec_dir,
        lock_path=lock_path,
        use_mock=True,
        ignore_drift=False,
    )
    assert res_rebuilt["quantiles_rebuilt"] is True
    assert store.load_meta()["meta_version"] == 2
    assert store.load_meta()["scopes"][SLUG]["n0"] == 4

    # 3. 验证 only_uids 增量织网（export -> import 与 api）：绝不抹除无关题既有边与状态
    uids = [
        "primary_olympiad:ch01:q1",
        "primary_olympiad:ch01:q2",
        "primary_olympiad:ch01:q3",
        "primary_olympiad:ch01:q4",
    ]
    surf_vecs = np.eye(4, dtype=np.float32)
    skel_vecs = np.asarray(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.72, 0.6939, 0.0, 0.0],  # q1-q2 落入 WIDE_RECALL_ISOMORPHIC 精裁带
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.72, 0.6939],  # q3-q4 同样潜在可匹配，但 only_uids=['q1_short'] 时不得重跑
        ],
        dtype=np.float32,
    )
    store._save_npz(store.npz_path("surface", SLUG), uids, ["h"] * 4, surf_vecs)
    store._save_npz(store.npz_path("skeleton", SLUG), uids, ["h"] * 4, skel_vecs)
    store._save_meta(
        {
            "embedding_model": settings.llm.embedding.model,
            "dimension": 4,
            "meta_version": 3,
            "quantiles": {
                f"{SLUG}::surface": [i / 100.0 for i in range(101)],
                f"{SLUG}::skeleton": [i / 100.0 for i in range(101)],
            },
            "scopes": {
                SLUG: {"n0": 4, "l1_dist": {"平面几何": 0.5, "行程问题": 0.5}, "updated_at": "2026-11-25T00:00:00Z"}
            },
        }
    )

    async def _noop_sync(self, namespace, items, embed_client, **kwargs):
        return {"total": len(items), "reused": len(items), "embedded": 0, "removed": 0, "dimension": 4}

    monkeypatch.setattr(VectorStore, "sync_vectors", _noop_sync)

    # 预置无关边 (q3, q4) 并将 q3, q4 的 S4 设为 STALE 哨兵，检验 only_uids 不会误改
    conn.execute("DELETE FROM question_relations;")
    conn.execute(
        """
        INSERT INTO question_relations(
            source_question_uid, target_question_uid, relation_type, is_directed,
            is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
            containment_score, trap_overlap, confidence_source, pedagogical_note, rule_version
        ) VALUES (
            'primary_olympiad:ch01:q3', 'primary_olympiad:ch01:q4', 'ISOMORPHIC', 0,
            0, 0.1, 0.9, 0.1, 0.9, 1.0, 0.0, 'LLM_VERIFIED', '哨兵既有边', 'sentinel'
        );
        """
    )
    conn.execute("UPDATE compile_state SET status='STALE', input_hash='keep_me' WHERE stage='S4';")
    conn.commit()

    tasks_dir = tmp_path / "s4_inc_tasks"
    exp_inc = await run_stage4_weave_async(
        conn,
        settings,
        grade_subject=GS,
        mode="export",
        tasks_dir=tasks_dir,
        vectors_dir=vec_dir,
        lock_path=lock_path,
        use_mock=True,
        only_uids=["q1_short"],  # 测试简写 source_id 解析
    )
    assert exp_inc["target_uids"] == ["primary_olympiad:ch01:q1"]
    saved_map = json.loads((tasks_dir / "stage4_task_map.json").read_text(encoding="utf-8"))
    assert saved_map["target_uids"] == ["primary_olympiad:ch01:q1"]
    assert saved_map["pairs"]
    for pmeta in saved_map["pairs"].values():
        assert "primary_olympiad:ch01:q1" in (pmeta["source_uid"], pmeta["target_uid"])

    # 构造回传结果并在不传 only_uids 的情况下执行 import（自动从 task_map 恢复 target_uids）
    batch_rec = json.loads((tasks_dir / "batches" / "batch_0001.jsonl").read_text(encoding="utf-8").strip())
    results_file = tmp_path / "s4_inc_results.jsonl"
    results_file.write_text(
        json.dumps(
            {
                "batch_id": batch_rec["batch_id"],
                "result": {
                    "decisions": [
                        {"pair_id": pid, "is_valid": True, "pedagogical_note": "增量精裁通过"}
                        for pid in batch_rec["task_item_ids"]
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    imp_inc = await run_stage4_weave_async(
        conn,
        settings,
        grade_subject=GS,
        mode="import",
        tasks_dir=tasks_dir,
        results_path=results_file,
        vectors_dir=vec_dir,
        lock_path=lock_path,
        use_mock=True,
    )
    assert imp_inc["target_uids"] == ["primary_olympiad:ch01:q1"]
    assert imp_inc["eligible_questions"] == 1

    # 验证既有无关边 (q3, q4) 毫发无损，且新增了 (q1, q2) 边
    sentinel_edge = conn.execute(
        "SELECT pedagogical_note, rule_version FROM question_relations WHERE source_question_uid='primary_olympiad:ch01:q3' AND target_question_uid='primary_olympiad:ch01:q4';"
    ).fetchone()
    assert sentinel_edge is not None
    assert sentinel_edge["pedagogical_note"] == "哨兵既有边"
    assert sentinel_edge["rule_version"] == "sentinel"

    q1_q2_edge = conn.execute(
        "SELECT confidence_source, pedagogical_note FROM question_relations WHERE source_question_uid='primary_olympiad:ch01:q1' AND target_question_uid='primary_olympiad:ch01:q2';"
    ).fetchone()
    assert q1_q2_edge is not None
    assert q1_q2_edge["confidence_source"] == "LLM_VERIFIED"

    # 验证仅 q1 的 S4 被置为 OK，q2/q3/q4 保持原 STALE 状态不变
    s4_after = {
        r["question_uid"]: r["status"]
        for r in conn.execute("SELECT question_uid, status FROM compile_state WHERE stage='S4';").fetchall()
    }
    assert s4_after["primary_olympiad:ch01:q1"] == "OK"
    assert s4_after["primary_olympiad:ch01:q2"] == "STALE"
    assert s4_after["primary_olympiad:ch01:q3"] == "STALE"
    assert s4_after["primary_olympiad:ch01:q4"] == "STALE"

    # 4. 回归防退化（P2-Bug-2）：
    # (a) 当 batch_count > 0 但回传空/漏批结果时，import 必须抛 PreconditionError 且严禁静默改写 S4
    tasks_missing_dir = tmp_path / "s4_missing_batch_tasks"
    exp_mb = await run_stage4_weave_async(
        conn,
        settings,
        grade_subject=GS,
        mode="export",
        tasks_dir=tasks_missing_dir,
        vectors_dir=vec_dir,
        lock_path=lock_path,
        use_mock=True,
        only_uids=["q3_short"],
    )
    assert exp_mb["refine_candidates"] >= 1
    empty_res_file = tasks_missing_dir / "results.jsonl"
    empty_res_file.write_text("", encoding="utf-8")
    with pytest.raises(PreconditionError, match="回传批次不完整"):
        await run_stage4_weave_async(
            conn,
            settings,
            grade_subject=GS,
            mode="import",
            tasks_dir=tasks_missing_dir,
            results_path=empty_res_file,
            vectors_dir=vec_dir,
            lock_path=lock_path,
            use_mock=True,
        )
    assert (
        conn.execute(
            "SELECT status FROM compile_state WHERE question_uid='primary_olympiad:ch01:q3' AND stage='S4';"
        ).fetchone()[0]
        == "STALE"
    )

    # (b) 当 refine_candidates == 0 (batch_count == 0) 时，import 落库后必须将 manifest 置为 IMPORTED 且不卡挂起批
    settings_no_ref = Settings()
    settings_no_ref.pipeline.use_mock_llm = True
    settings_no_ref.pipeline.enable_stage4_llm_refine = False
    uni_zero = tmp_path / "uni_zero"
    tasks_zero_dir = uni_zero / "stage3_4" / "stage4"
    exp_zero = await run_stage4_weave_async(
        conn,
        settings_no_ref,
        grade_subject=GS,
        mode="export",
        tasks_dir=tasks_zero_dir,
        vectors_dir=vec_dir,
        lock_path=lock_path,
        use_mock=True,
        only_uids=["q3_short"],
    )
    assert exp_zero["refine_candidates"] == 0
    zero_res_file = tasks_zero_dir / "results.jsonl"
    zero_res_file.write_text("", encoding="utf-8")
    await run_stage4_weave_async(
        conn,
        settings_no_ref,
        grade_subject=GS,
        mode="import",
        tasks_dir=tasks_zero_dir,
        results_path=zero_res_file,
        vectors_dir=vec_dir,
        lock_path=lock_path,
        use_mock=True,
    )
    zero_manifest = json.loads((tasks_zero_dir / "stage4_manifest.json").read_text(encoding="utf-8"))
    assert zero_manifest["status"] == "IMPORTED"
    assert zero_manifest.get("imported_at")
    assert scan_pending_batches(uni_zero) == []
    conn.close()


def test_compiler_tools_skip_imported_batches_and_vector_drift_warnings(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    _write_temp_config(cfg_dir, embed_model="text-embedding-3-large")

    uni_dir = tmp_path / "unified"
    (uni_dir / "batches").mkdir(parents=True)
    (uni_dir / "task_map.json").write_text(json.dumps({"T0001": {"question_uid": "u1"}}), encoding="utf-8")
    (uni_dir / "batches" / "batch_001.jsonl").write_text('{"task_id": "T0001"}\n', encoding="utf-8")
    (uni_dir / "results.jsonl").write_text('{"task_id": "T0001"}\n', encoding="utf-8")
    (uni_dir / "unified_manifest.json").write_text(
        json.dumps({"step_kind": "unified", "status": "IMPORTED"}),
        encoding="utf-8",
    )

    # stage3_4/3c: IMPORTED 且无 retry -> 必须跳过
    s3c_dir = uni_dir / "stage3_4" / "3c"
    (s3c_dir / "batches").mkdir(parents=True)
    (s3c_dir / "3c_manifest.json").write_text(
        json.dumps({"step_kind": "3c", "batch_count": 1, "status": "IMPORTED"}),
        encoding="utf-8",
    )
    (s3c_dir / "3c_task_map.json").write_text(json.dumps({"T1": "u1"}), encoding="utf-8")
    (s3c_dir / "results.jsonl").write_text('{"batch_id": "b1"}\n', encoding="utf-8")

    # stage3_4/stage4: IMPORTED 但存在非空 retry_tasks.jsonl (retry_count > 0) -> 不可跳过！
    s4_dir = uni_dir / "stage3_4" / "stage4"
    (s4_dir / "batches").mkdir(parents=True)
    (s4_dir / "stage4_manifest.json").write_text(
        json.dumps({"step_kind": "stage4", "batch_count": 1, "status": "IMPORTED"}),
        encoding="utf-8",
    )
    (s4_dir / "stage4_task_map.json").write_text(json.dumps({"pairs": {}}), encoding="utf-8")
    (s4_dir / "retry_tasks.jsonl").write_text('{"batch_id": "b_err"}\n', encoding="utf-8")

    pending = scan_pending_batches(uni_dir)
    assert len(pending) == 1
    assert pending[0]["step_kind"] == "stage4"
    assert pending[0]["retry_count"] == 1

    # 测试 _check_vectors_status 接入 evaluate_scope_drift（缺基准告警 -> 漂移告警 -> 对齐消警）
    db_path = tmp_path / "status.db"
    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)
    _seed_question(conn, "primary_olympiad:ch01:q1", primary_l1="平面几何")
    _seed_question(conn, "primary_olympiad:ch01:q2", primary_l1="行程问题")
    conn.commit()
    conn.close()

    vec_dir = tmp_path / "vectors"
    store = VectorStore(vec_dir)
    vecs = np.eye(2, 4, dtype=np.float32)
    store._save_npz(store.npz_path("surface", SLUG), ["q1", "q2"], ["h1", "h2"], vecs)
    store._save_npz(store.npz_path("skeleton", SLUG), ["q1", "q2"], ["h1", "h2"], vecs)
    store._save_meta(
        {
            "embedding_model": "text-embedding-3-large",
            "dimension": 4,
            "meta_version": 1,
            "quantiles": {
                f"{SLUG}::surface": [0.0, 1.0],
                f"{SLUG}::skeleton": [0.0, 1.0],
            },
            # 故意不写 scopes
        }
    )

    st_missing_base = get_pipeline_status(
        db_path=db_path, config_dir=cfg_dir, vectors_dir=vec_dir, unified_dir=uni_dir
    )
    assert st_missing_base["vectors"]["needs_reindex"] is True
    assert any("[VECTOR_DRIFT]" in w for w in st_missing_base["vectors"]["drift_warnings"])

    # 写入偏移基准 (平面几何 1.0 vs 当前 0.5 -> delta=0.50 >= 0.10)
    store._save_meta(
        {
            "embedding_model": "text-embedding-3-large",
            "dimension": 4,
            "meta_version": 2,
            "quantiles": {
                f"{SLUG}::surface": [0.0, 1.0],
                f"{SLUG}::skeleton": [0.0, 1.0],
            },
            "scopes": {
                SLUG: {"n0": 2, "l1_dist": {"平面几何": 1.0}, "updated_at": "2026-11-25T00:00:00Z"}
            },
        }
    )
    st_drifted = get_pipeline_status(
        db_path=db_path, config_dir=cfg_dir, vectors_dir=vec_dir, unified_dir=uni_dir
    )
    assert st_drifted["vectors"]["needs_reindex"] is True
    assert any("[VECTOR_DRIFT]" in w and "偏移" in w for w in st_drifted["vectors"]["drift_warnings"])

    # 写入完全匹配基准 -> needs_reindex=False 且零漂移告警
    store._save_meta(
        {
            "embedding_model": "text-embedding-3-large",
            "dimension": 4,
            "meta_version": 3,
            "quantiles": {
                f"{SLUG}::surface": [0.0, 1.0],
                f"{SLUG}::skeleton": [0.0, 1.0],
            },
            "scopes": {
                SLUG: {
                    "n0": 2,
                    "l1_dist": {"平面几何": 0.5, "行程问题": 0.5},
                    "updated_at": "2026-11-25T00:00:00Z",
                }
            },
        }
    )
    st_clean = get_pipeline_status(
        db_path=db_path, config_dir=cfg_dir, vectors_dir=vec_dir, unified_dir=uni_dir
    )
    assert st_clean["vectors"]["needs_reindex"] is False
    assert st_clean["vectors"]["drift_warnings"] == []


def test_stage_q_audit_readonly_extensions_wilson_difficulty_drift_and_legacy_tags(
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "config"
    _write_temp_config(cfg_dir)
    db_path = tmp_path / "audit.db"
    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;", (GS,))

    # 构造 5 道题覆盖：
    # q1: VERIFIED_CONSENSUS, 单路有图, 旧标签 format="解答题" 纠偏为 eff_format="选择题", 旧 method 含黑名单 "转化与化归" 已清除
    _seed_question(
        conn,
        "p:c:q1",
        text="选择题：下列阴影面积是多少？ A. 10 B. 12 C. 14 D. 16",
        m_format="选择题",
        has_images=1,
        primary_l1="平面几何",
        cstl=(1, 1, 1, 1),  # L1_基础直达
        methods=["割补法"],
        legacy_tags={"domain": "几何", "format": "解答题", "method": ["转化与化归", "割补法"]},
        content_hash="h_old_q1",
    )
    # 给 q1 添加源题变更后依然激活的标注类覆写 (base_fingerprint='h_v0' != 'h_old_q1')
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, base_fingerprint, is_active, is_suspended
        ) VALUES ('p:c:q1', 'abstract_skeleton', '人工黄金几何骨架', 'PI_CLI', 'h_v0', 1, 0);
        """
    )
    effective.recompute(conn, "p:c:q1")

    # q2: VERIFIED_CONSENSUS, 双路共识 (含 SECONDARY OK run)
    _seed_question(
        conn,
        "p:c:q2",
        has_images=0,
        primary_l1="行程问题",
        cstl=(3, 2, 2, 1),  # L5-1_压轴挑战
        methods=["比例法"],
        legacy_tags={"domain": "应用题", "format": "解答题", "method": ["逻辑推理", "比例法"]},
    )
    conn.execute(
        """
        INSERT INTO stage1_vlm_runs(
            question_uid, role, model_name, prompt_version, input_hash, run_status, sympy_status
        ) VALUES ('p:c:q2', 'SECONDARY', 'm2', 'v1', 'h', 'OK', 'PASSED');
        """
    )

    # q3: 已审真共识 (m=VERIFIED_CONSENSUS, 人工复核答案一致 -> HUMAN_VERIFIED, 非假共识)
    _seed_question(
        conn,
        "p:c:q3",
        primary_l1="平面几何",
        cstl=(2, 2, 2, 1),  # L3_综合中档
        answer_val="12",
    )
    same_ans = conn.execute("SELECT m_final_answers_json FROM questions WHERE question_uid='p:c:q3';").fetchone()[0]
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, base_fingerprint, is_active
        ) VALUES ('p:c:q3', 'final_answers_json', ?, 'REVIEW_UI', 'hash_p:c:q3', 1);
        """,
        (same_ans,),
    )
    effective.recompute(conn, "p:c:q3")

    # q4: 已审假共识 #1 (m=VERIFIED_CONSENSUS, 人工改了答案 12 -> 99)
    _seed_question(
        conn,
        "p:c:q4",
        primary_l1="数论基础",
        cstl=(4, 3, 3, 3),  # L5-3_殿堂极限
        answer_val="12",
    )
    changed_ans = json.dumps(
        [{"label": "主问", "answer_type": "NUMERIC", "verify_mode": "SYMBOLIC", "collection_mode": "SINGLE", "answer": "99", "sympy_expr": "99"}],
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, base_fingerprint, is_active
        ) VALUES ('p:c:q4', 'final_answers_json', ?, 'REVIEW_UI', 'hash_p:c:q4', 1);
        """,
        (changed_ans,),
    )
    effective.recompute(conn, "p:c:q4")

    # q5: 已审假共识 #2 (m=VERIFIED_CONSENSUS, 人工标记错题排除 is_excluded=1)
    _seed_question(conn, "p:c:q5", primary_l1="平面几何", answer_val="12")
    conn.execute(
        """
        INSERT INTO human_overrides(
            question_uid, field_name, override_value, source, base_fingerprint, is_active
        ) VALUES ('p:c:q5', 'is_excluded', '1', 'REVIEW_UI', 'hash_p:c:q5', 1);
        """
    )
    effective.recompute(conn, "p:c:q5")

    h_dir = tmp_path / "human"
    human_mirror.export_snapshot(conn, human_dir=h_dir)
    conn.commit()
    conn.close()

    # 向量仓故意留空 scopes 以触发 scope_drift 告警
    vec_dir = tmp_path / "vectors"
    store = VectorStore(vec_dir)
    store._save_meta({"embedding_model": "mock-embed", "dimension": 4, "meta_version": 1, "quantiles": {}})

    # 使用 readonly=True 连接运行 Stage Q 体检，100% 证明纯只读零写库
    ro_conn = get_connection(db_path, readonly=True)
    try:
        settings = Settings()
        reports_dir = tmp_path / "reports"
        res = run_stage_q_audit(
            ro_conn,
            settings,
            grade_subject=GS,
            human_dir=h_dir,
            vectors_dir=vec_dir,
            reports_dir=reports_dir,
            config_dir=cfg_dir,
            prune_runs=False,
        )
    finally:
        ro_conn.close()

    rep = res["report"]
    # 1. 向后兼容既有 7 大板块键名
    for sec in (
        "section_1_compile_coverage",
        "section_2_verification_sympy",
        "section_2b_difficulty_distribution",
        "section_3_knowledge_graph",
        "section_4_topology_degrees",
        "section_5_integrity_and_cost",
        "section_6_vectors",
        "section_7_risks_and_actions",
        "section_8_legacy_tags_comparison",
    ):
        assert sec in rep

    # 2. 验证 consensus_audit 与 Wilson 95% 置信区间上界 (audited_n=3, false_consensus_k=2)
    ca = rep["section_2_verification_sympy"]["consensus_audit"]
    assert ca["single_run_with_images"] == 1
    assert ca["single_run_no_images"] == 0
    assert ca["dual_run"] == 1
    assert ca["recommended_sample_size"] == 5  # max(5, ceil(0.10 * 1))
    assert ca["audited_n"] == 3
    assert ca["false_consensus_k"] == 2
    assert ca["false_consensus_rate"] == pytest.approx(2 / 3, abs=1e-4)

    p = 2.0 / 3.0
    z = 1.959963984540054
    z2 = z * z
    expected_wilson = (p + z2 / 6.0 + z * math.sqrt(p * (1.0 - p) / 3.0 + z2 / 36.0)) / (1.0 + z2 / 3.0)
    assert ca["wilson_95_upper"] == pytest.approx(round(expected_wilson, 4))

    # 3. 验证 8 档与 [C, S, T, L] 分布 (q1..q4 4 道可服务题，q5 已排除)
    dd = rep["section_2b_difficulty_distribution"]
    assert dd["servable_count"] == 4
    assert dd["tier_histogram"]["L1_基础直达"] == 1
    assert dd["tier_histogram"]["L3_综合中档"] == 1
    assert dd["tier_histogram"]["L5-1_压轴挑战"] == 1
    assert dd["tier_histogram"]["L5-3_殿堂极限"] == 1
    assert dd["cstl_marginals"]["C"] == {"1": 1, "2": 1, "3": 1, "4": 1}

    # 4. 验证 scope_drift 与 VECTOR_DRIFT 风险告警
    sd = rep["section_6_vectors"]["scope_drift"][GS]
    assert sd["has_baseline"] is False
    risks = rep["section_7_risks_and_actions"]["risks"]
    assert any(r.startswith("VECTOR_DRIFT:") for r in risks)

    # 5. 验证源题变更标注覆写提示
    sc_ann = rep["section_7_risks_and_actions"]["source_changed_annotation_overrides"]
    assert sc_ann["count"] == 1
    assert sc_ann["question_uids"] == ["p:c:q1"]
    assert any("标注类覆写" in r for r in risks)

    # 6. 验证 section_8_legacy_tags_comparison 与 Markdown 末尾第 8 节渲染
    s8 = rep["section_8_legacy_tags_comparison"]
    assert s8["format_changed_count"] == 1
    assert s8["format_changed_samples"] == [
        {"question_uid": "p:c:q1", "legacy_format": "解答题", "eff_format": "选择题"}
    ]
    assert s8["domain_to_primary_l1_matrix"] == {
        "几何": {"平面几何": 1},
        "应用题": {"行程问题": 1},
    }
    mb = s8["method_blacklist_purge"]
    assert mb["legacy_hit_question_count"] == 2
    assert mb["purged_question_count"] == 2
    assert mb["purged_term_counts"] == {"转化与化归": 1, "逻辑推理": 1}

    md_text = Path(res["md_path"]).read_text(encoding="utf-8")
    assert "## 8. 新旧标签与题型纠偏对比" in md_text
