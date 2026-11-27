"""外部错题诊断推荐引擎 rec_v3 与 Stage 4 拓扑改进回归测试（审计闭环 P0+P1）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from shuati.config import Settings, load_settings
from shuati.core import effective
from shuati.core.text_norm import compute_plain_fingerprint, normalize_text
from shuati.core.topology import (
    CandidateEdge,
    apply_degree_caps,
    generate_candidate_edges,
    load_question_features,
    prune_transitive_scaffolds,
)
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm.mocks import MOCK_RESPONSES
from shuati.models.schemas import ExternalDiagnosisOutput
from shuati.services.diagnose_batch import render_batch_markdown_report
from shuati.services.mistake import (
    RECOMMENDATION_ENGINE_VERSION,
    _scaffold_elo_ok,
    diagnose_external_mistake_async,
    ingest_external_mistake_async,
)
from shuati.vectors.store import VectorStore

GS = "小学奥数"
SCOPE = "primary_olympiad"


def _setup_v3_env(tmp_path: Path) -> tuple[Path, Path, Path, Path, Settings]:
    db_path = tmp_path / "data" / "shuati.db"
    cfg_dir = tmp_path / "config"
    vec_dir = tmp_path / "data" / "vectors"
    drafts_dir = tmp_path / "data" / "drafts"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    repo_cfg = Path("config")
    for fname in ("settings.yaml", "method_blacklist.yaml"):
        src = repo_cfg / fname
        txt = src.read_text(encoding="utf-8")
        if fname == "settings.yaml":
            txt = txt.replace("use_mock_llm: false", "use_mock_llm: true")
        (cfg_dir / fname).write_text(txt, encoding="utf-8")

    prompts_dst = cfg_dir / "prompts"
    prompts_dst.mkdir(parents=True, exist_ok=True)
    for pf in (repo_cfg / "prompts").glob("*.yaml"):
        (prompts_dst / pf.name).write_text(pf.read_text(encoding="utf-8"), encoding="utf-8")
    for sub_dir in ("subjects", "snippets"):
        if (repo_cfg / "prompts" / sub_dir).exists():
            dst_sub = prompts_dst / sub_dir
            dst_sub.mkdir(parents=True, exist_ok=True)
            for sf in (repo_cfg / "prompts" / sub_dir).glob("*"):
                if sf.is_file():
                    (dst_sub / sf.name).write_text(sf.read_text(encoding="utf-8"), encoding="utf-8")

    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0' WHERE grade_subject = ?;",
        (GS,),
    )
    for nid, lvl, l1, l2, l3 in (
        ("n_l2_meet", "L2", "行程问题", "多次相遇与往返行程", ""),
        ("n_l2_coord", "L2", "行程问题", "接送与多人统筹行程", ""),
        ("n_l3_3s", "L3", "行程问题", "多次相遇与往返行程", "两岸往返全程倍数转化"),
        ("n_l3_bridge", "L3", "行程问题", "火车过桥与错车", "火车过桥车身长度补偿"),
        ("n_l3_tunnel", "L3", "行程问题", "火车过桥与错车", "变速过隧道折算同速时间"),
        ("n_l3_coord", "L3", "行程问题", "接送与多人统筹行程", "瓶颈两车相向相遇求时下界"),
    ):
        conn.execute(
            """
            INSERT INTO knowledge_nodes(
                node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator
            ) VALUES (?, ?, '1.0', ?, ?, ?, ?);
            """,
            (nid, GS, lvl, l1, l2, l3),
        )
    conn.commit()
    conn.close()

    settings = load_settings(cfg_dir / "settings.yaml", env_path=tmp_path / ".env")
    settings.pipeline.use_mock_llm = True
    return db_path, cfg_dir, vec_dir, drafts_dir, settings


def _seed_q(
    conn: Any,
    uid: str,
    source_id: str,
    text: str,
    *,
    l1: str = "行程问题",
    l2: str = "多次相遇与往返行程",
    nids: list[str] | None = None,
    scores: tuple[int, int, int, int] = (2, 1, 2, 1),
    answer: str = "100",
    traps: list[str] | None = None,
) -> None:
    norm_t = normalize_text(text).normalized_text
    fp = compute_plain_fingerprint(norm_t, 0)
    fa = [
        {
            "label": "主问",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "SINGLE",
            "answer": answer,
            "sympy_expr": answer,
        }
    ]
    c, s, t, l_sc = scores
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id,
            raw_question, content_hash, plain_fingerprint
        ) VALUES (?, ?, 'BATCH_JSONL', 'ch17', ?, ?, ?, ?);
        """,
        (uid, GS, source_id, norm_t, f"h_{uid}", fp),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format, m_verification_status,
            m_abstract_skeleton, m_primary_l1_module,
            m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json
        ) VALUES (?, ?, ?, '解答题', 'VERIFIED_CONSENSUS', '行程抽象骨架', ?, ?, ?, ?, ?, ?);
        """,
        (uid, GS, norm_t, l1, c, s, t, l_sc, json.dumps(fa, ensure_ascii=False)),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解析');
        """,
        (f"{uid}::b1", uid),
    )
    eff_nids = nids or [None]  # type: ignore[list-item]
    for idx, nid in enumerate(eff_nids, start=1):
        conn.execute(
            """
            INSERT INTO question_steps(
                step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id
            ) VALUES (?, ?, 'branch_1', ?, '子目标', ?, ?, ?);
            """,
            (f"{uid}::b1::{idx}", uid, idx, l1, l2, nid),
        )
    for tr in traps or []:
        conn.execute(
            "INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed) VALUES (?, 'TRAP', ?, 1);",
            (f"TRAP:{tr}", tr),
        )
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
            (uid, f"TRAP:{tr}"),
        )
    for stg in ("S1", "S2", "S3C", "S3D", "S4"):
        conn.execute("INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, 'OK');", (uid, stg))
    effective.recompute(conn, uid)


def test_scaffold_elo_window_blocks_inversion_and_boss_elementary_drop() -> None:
    # 1. L1 1120 地板题绝不允许推 1120 或 1185 同级/倒挂题（修复 Q6 ch18_001 倒挂）
    assert _scaffold_elo_ok(1120, 1120) is False
    assert _scaffold_elo_ok(1185, 1120) is False
    # 2. L2 1235 母题允许推 1120 基础题（保留 Q11 铺垫）
    assert _scaffold_elo_ok(1120, 1235) is True
    # 3. 1915 / 2030 高分段压轴题禁止推 1120 低幼凑数题（修复 Q2/Q12/Q13）
    assert _scaffold_elo_ok(1120, 1915) is False
    assert _scaffold_elo_ok(1120, 2030) is False
    assert _scaffold_elo_ok(1355, 1915) is True
    assert _scaffold_elo_ok(1405, 2030) is True


def test_new_medium_granularity_seed_traps_schema_and_markdown(tmp_path: Path) -> None:
    _, cfg_dir, _, _, settings = _setup_v3_env(tmp_path)
    assert settings.llm.judge_llm.timeout_seconds >= 150.0
    assert settings.topology.thresholds.scaffold.max_elo_diff == 750

    payload = {
        "visual_facts": "",
        "visual_ambiguity_flag": False,
        "visual_ambiguity_note": "",
        "method_summary": "调和平均求往返速度",
        "solution_markdown": "总路程除以总时间得平均速度。",
        "final_answers": [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "45",
                "sympy_expr": "45",
            }
        ],
        "sympy_code": "def verify():\n    return {'主问': '45'}",
        "steps": [
            {
                "step_index": 1,
                "sub_goal": "求总耗时",
                "core_formula": "t = s/v1 + s/v2",
                "hint": "分别表达去回时间",
                "raw_l1": "行程问题",
                "raw_l2": "比例与变速行程",
                "raw_l3": "调和平均速度求解",
            }
        ],
        "abstract_skeleton": "已知去返速度求往返平均速度",
        "primary_l1_module": "行程问题",
        "matched_l2": ["比例与变速行程"],
        "thinking_methods": ["设而不求"],
        "classic_motifs": [],
        "trap_tags": [
            "TRAP_AVERAGE_HARMONIC",
            "TRAP_EXTENDED_BODY_LENGTH",
            "TRAP_ASYNC_SYNC_CONFUSION",
            "TRAP_DIRECTION_REF_POINT",
        ],
        "difficulty": {
            "C_concept_count": {"evidence": "双步", "score": 2},
            "S_chain_length": {"evidence": "两步", "score": 1},
            "T_trickiness": {"evidence": "有坑", "score": 2},
            "L_computation_load": {"evidence": "口算", "score": 1},
        },
    }
    validated = ExternalDiagnosisOutput.model_validate(payload)
    assert len(validated.trap_tags) == 4

    md = render_batch_markdown_report(
        {
            "batch_id": "b_v3",
            "source_file": "x.md",
            "grade_subject": GS,
            "created_at": "2026-11-27",
            "total": 1,
            "succeeded": 1,
            "reused": 0,
            "failed": 0,
            "items": [
                {
                    "index": 1,
                    "title": "2",
                    "status": "OK",
                    "result": {
                        "draft_id": "drf_1",
                        "tier": "L3_综合中档",
                        "base_elo": 1405,
                        "primary_l1_module": "行程问题",
                        "matched_l2": ["比例与变速行程"],
                        "final_answers": [{"label": "主问", "answer": "1", "unit": "小时"}],
                        "matched_db_question": {
                            "source_id": "ch17_058",
                            "verification_status": "VERIFIED_CONSENSUS",
                            "answer_conflict": True,
                            "db_answer_display": "2小时",
                        },
                        "trap_tags": ["TRAP_AVERAGE_HARMONIC"],
                        "recommendations": {},
                        "warnings": ["DIAGNOSE_RERANK_DEGRADED: 模拟超时告警"],
                    },
                }
            ],
        },
        config_dir=cfg_dir,
    )
    assert "⚠️ 1小时 (库内验真: 2小时)" in md
    assert "调和平均与算术平均混淆" in md
    assert "DIAGNOSE_RERANK_DEGRADED: 模拟超时告警" in md


def test_topology_same_nums_diff_ans_routes_to_contrastive_and_transitive_pruning(tmp_path: Path) -> None:
    db_path, _, vec_dir, _, settings = _setup_v3_env(tmp_path)
    conn = get_connection(db_path)
    try:
        # ch17_031 vs ch17_032：同数字 [800, 500]，异标准答案 1900 vs 1450（离B地 vs 离A地）
        _seed_q(
            conn,
            "u_031",
            "ch17_031",
            "甲、乙同时从A、B相向出发，第一次相遇离A地800米，折返后第二次相遇离B地500米，求全程。",
            nids=["n_l3_3s"],
            scores=(2, 1, 2, 1),
            answer="1900",
        )
        _seed_q(
            conn,
            "u_032",
            "ch17_032",
            "甲、乙同时从A、B相向出发，第一次相遇离A地800米，折返后第二次相遇离A地500米，求全程。",
            nids=["n_l3_3s"],
            scores=(2, 1, 2, 1),
            answer="1450",
        )
        conn.commit()

        store = VectorStore(vec_dir)
        uids = ["u_031", "u_032"]
        mat = np.asarray([[1.0, 0.0, 0.0, 0.0], [0.9995, 0.0316, 0.0, 0.0]], dtype=np.float32)
        store._save_npz(store.npz_path("surface", SCOPE), uids, ["h", "h"], mat)
        store._save_npz(store.npz_path("skeleton", SCOPE), uids, ["h", "h"], mat)
        store._save_meta(
            {
                "embedding_model": "mock",
                "dimension": 4,
                "meta_version": 1,
                "quantiles": {
                    f"{SCOPE}::surface": [i / 100.0 for i in range(101)],
                    f"{SCOPE}::skeleton": [i / 100.0 for i in range(101)],
                },
            }
        )

        feats = load_question_features(conn, grade_subject=GS)
        cands = generate_candidate_edges(
            conn, store, grade_subject=GS, scope_slug=SCOPE, topology_cfg=settings.topology, features=feats
        )
        rtypes = {e.relation_type for e in cands}
        assert "CLONE" not in rtypes
        assert "CONTRASTIVE" in rtypes

        # 测试传递约简：A -> B, B -> C, A -> C 中自动剪除跳跃边 A -> C
        sc_edges = [
            CandidateEdge("uA", "uB", "SCAFFOLD", 1, 0, 0.8, 0.9, 0.8, 0.9, 0.9, 0.0, 0.9),
            CandidateEdge("uB", "uC", "SCAFFOLD", 1, 0, 0.8, 0.9, 0.8, 0.9, 0.9, 0.0, 0.9),
            CandidateEdge("uA", "uC", "SCAFFOLD", 1, 0, 0.8, 0.95, 0.8, 0.95, 0.95, 0.0, 0.98),
        ]
        pruned = prune_transitive_scaffolds(sc_edges)
        assert {(e.source_uid, e.target_uid) for e in pruned} == {("uA", "uB"), ("uB", "uC")}
        capped = apply_degree_caps(sc_edges, settings.topology)
        assert ("uA", "uC") not in {(e.source_uid, e.target_uid) for e in capped}
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_db_verified_l1_l2_sync_transitive_pruning_and_conflict_ingest_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, cfg_dir, vec_dir, drafts_dir, settings = _setup_v3_env(tmp_path)
    conn = get_connection(db_path)
    try:
        q7_text = "4辆汽车停在十字路口的4条岔道上，距路口均为18千米，时速40、50、60、70，最少几分钟相聚？"
        _seed_q(
            conn,
            "u_q7_orig",
            "ch17_072",
            q7_text,
            l1="行程问题",
            l2="接送与多人统筹行程",
            nids=["n_l3_coord"],
            scores=(3, 1, 3, 1),  # 1860 Elo
            answer="24",
        )
        _seed_q(
            conn,
            "u_q7_scaf_mid",
            "ch17_030",
            "甲乙从A地、丙从B地同时相向出发，求多人统筹相遇时间差。",
            l1="行程问题",
            l2="接送与多人统筹行程",
            nids=["n_l3_coord"],
            scores=(2, 2, 2, 1),  # 1510 Elo
            answer="15",
        )
        _seed_q(
            conn,
            "u_q7_scaf_low",
            "ch17_005",
            "两车从相距36千米的两地相向而行，时速40与50，多久相遇？",
            l1="行程问题",
            l2="接送与多人统筹行程",
            nids=["n_l3_coord"],
            scores=(1, 1, 1, 1),  # 1120 Elo，且为 u_q7_scaf_mid 的下位子题
            answer="24",
        )
        conn.execute(
            """
            INSERT INTO question_relations(
                source_question_uid, target_question_uid, relation_type,
                is_directed, is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
                containment_score, trap_overlap, confidence_source, pedagogical_note, rule_version
            ) VALUES ('u_q7_scaf_low', 'u_q7_scaf_mid', 'SCAFFOLD', 1, 0, 0.85, 0.92, 0.9, 0.95, 1.0, 0.0, 'LLM_VERIFIED', '下位基础', '2.4-init');
            """
        )
        conn.commit()

        monkeypatch.setitem(
            MOCK_RESPONSES,
            "DIAGNOSE_RERANK:judge_llm",
            {
                "decisions": [
                    {"candidate_id": "c01", "verdict": "SCAFFOLD", "pedagogical_reason": "铺垫多人相向统筹时间计算"},
                ]
            },
        )

        # 单路草稿故意错把 L1 标为“逻辑推理与杂题”，且给错答案“27”触发冲突
        bad_diag = {
            "visual_facts": "",
            "visual_ambiguity_flag": False,
            "visual_ambiguity_note": "",
            "method_summary": "极端原理分析",
            "solution_markdown": "错误以为在十字路口中心会合得27分钟。",
            "final_answers": [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "27",
                    "sympy_expr": "27",
                }
            ],
            "sympy_code": "def verify():\n    return {'主问': '27'}",
            "steps": [
                {
                    "step_index": 1,
                    "sub_goal": "求时间",
                    "core_formula": "18/40*60",
                    "hint": "算最慢车到路口时间",
                    "raw_l1": "逻辑推理与杂题",
                    "raw_l2": "极端原理与最值构造",
                    "raw_l3": "瓶颈时间下界",
                }
            ],
            "abstract_skeleton": "多车岔道相聚最短时间",
            "primary_l1_module": "逻辑推理与杂题",
            "matched_l2": ["极端原理与最值构造"],
            "thinking_methods": ["极端原理"],
            "classic_motifs": [],
            "trap_tags": [],
            "difficulty": {
                "C_concept_count": {"evidence": "构造", "score": 3},
                "S_chain_length": {"evidence": "一步", "score": 1},
                "T_trickiness": {"evidence": "极值", "score": 3},
                "L_computation_load": {"evidence": "口算", "score": 1},
            },
        }
        res = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text=q7_text,
            grade_subject=GS,
            diagnosis_json=bad_diag,
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )
        assert res["recommendation_engine_version"] == RECOMMENDATION_ENGINE_VERSION
        # 1. 成功继承 ch17_072 的行程问题 L1/L2，召回 u_q7_scaf_mid，且自动传递剪除冗余祖先 u_q7_scaf_low
        sc_uids = [x["question_uid"] for x in res["recommendations"]["scaffold_foundations"]]
        assert sc_uids == ["u_q7_scaf_mid"]

        # 2. 携带答案冲突的草稿默认拦截入库，显式传 --answer 或 allow_conflict_ingest=True 才放行
        with pytest.raises(PreconditionError, match="ANSWER_CONFLICT_BLOCKED"):
            await ingest_external_mistake_async(
                conn,
                settings,
                draft_id=res["draft_id"],
                config_dir=cfg_dir,
                drafts_dir=drafts_dir,
                dry_run=True,
                use_mock=True,
            )

        dry_ok = await ingest_external_mistake_async(
            conn,
            settings,
            draft_id=res["draft_id"],
            allow_conflict_ingest=True,
            config_dir=cfg_dir,
            drafts_dir=drafts_dir,
            dry_run=True,
            use_mock=True,
        )
        assert dry_ok["dry_run"] is True

        # 3. 验证 shuati override --field trap_tags_json 同步物化至 question_concepts(origin='HUMAN')
        from shuati.services.practice import apply_single_override, hydrate_question_dict

        apply_single_override(
            conn,
            "u_q7_scaf_mid",
            field_name="trap_tags_json",
            value='["TRAP_AVERAGE_HARMONIC"]',
            reason="测试新增陷阱覆写物化",
            human_dir=tmp_path / "human",
        )
        hyd = hydrate_question_dict(conn, "u_q7_scaf_mid")
        assert "TRAP_AVERAGE_HARMONIC" in hyd["trap_tags"]
    finally:
        conn.close()
