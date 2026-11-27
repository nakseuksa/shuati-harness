"""外部错题诊断推荐引擎 v2（P0 确定性护栏 + P1 提示词与 judge_llm 二级精裁 + P2.1 离线骨架补检）单测。"""

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
    generate_candidate_edges,
    load_question_features,
    weave_single_question,
)
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.llm.mocks import MOCK_RESPONSES
from shuati.llm.prompt_builder import (
    build_diagnose_rerank_messages,
    build_external_diagnose_messages,
)
from shuati.services.diagnose_batch import (
    diagnose_external_file_async,
    render_batch_markdown_report,
)
from shuati.services.mistake import (
    RECOMMENDATION_ENGINE_VERSION,
    _is_same_original_question,
    diagnose_external_mistake_async,
)
from shuati.vectors.store import VectorStore

GS = "小学奥数"
SCOPE = "primary_olympiad"

_NODE_META: dict[str, tuple[str, str, str, str]] = {
    "n_l2_meet": ("L2", "行程问题", "相遇与追及", ""),
    "n_l2_water": ("L2", "行程问题", "流水行船与扶梯", ""),
    "n_l2_train": ("L2", "行程问题", "火车过桥与错车", ""),
    "n_l3_meet_multi": ("L3", "行程问题", "相遇与追及", "多次往返相遇倍比关系"),
    "n_l3_hex_path": ("L3", "行程问题", "相遇与追及", "规则多边形边界与对角线行程"),
    "n_l3_esc_rel": ("L3", "行程问题", "流水行船与扶梯", "可见级数与相对速度"),
    "n_l2_poly": ("L2", "平面几何", "多边形周长与对角线", ""),
    "n_l3_hex_diag": ("L3", "平面几何", "多边形周长与对角线", "正六角星外边与内对角线倍比"),
}


def _setup_env(tmp_path: Path) -> tuple[Path, Path, Path, Path, Settings]:
    db_path = tmp_path / "data" / "shuati.db"
    cfg_dir = tmp_path / "config"
    vec_dir = tmp_path / "data" / "vectors"
    drafts_dir = tmp_path / "data" / "drafts"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    repo_cfg = Path("config")
    for fname in ("settings.yaml", "method_blacklist.yaml"):
        src = repo_cfg / fname
        if src.exists():
            txt = src.read_text(encoding="utf-8")
            if fname == "settings.yaml":
                txt = txt.replace("use_mock_llm: false", "use_mock_llm: true")
            (cfg_dir / fname).write_text(txt, encoding="utf-8")

    prompts_dst = cfg_dir / "prompts"
    prompts_dst.mkdir(parents=True, exist_ok=True)
    for pf in (repo_cfg / "prompts").glob("*.yaml"):
        (prompts_dst / pf.name).write_text(pf.read_text(encoding="utf-8"), encoding="utf-8")

    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0' WHERE grade_subject = ?;",
        (GS,),
    )
    for nid, (lvl, l1, l2, l3) in _NODE_META.items():
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

    settings = load_settings(cfg_dir / "settings.yaml")
    settings.pipeline.use_mock_llm = True
    return db_path, cfg_dir, vec_dir, drafts_dir, settings


def _seed_question(
    conn: Any,
    *,
    uid: str,
    source_id: str,
    text: str,
    skeleton: str,
    l1: str,
    l3_nids: list[str],
    step_l2_fallback: str = "相遇与追及",
    scores: tuple[int, int, int, int] = (2, 2, 1, 1),
    answer: str = "10",
    traps: list[str] | None = None,
    methods: list[str] | None = None,
    has_images: int = 0,
    ver_status: str = "VERIFIED_CONSENSUS",
) -> None:
    norm_t = normalize_text(text).normalized_text
    fp = compute_plain_fingerprint(norm_t, has_images)
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
            m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json, has_images
        ) VALUES (?, ?, ?, '解答题', ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (uid, GS, norm_t, ver_status, skeleton, l1, c, s, t, l_sc, json.dumps(fa, ensure_ascii=False), has_images),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '标准解析');
        """,
        (f"{uid}::b1", uid),
    )
    if l3_nids:
        for idx, nid in enumerate(l3_nids, start=1):
            _, step_l1, step_l2, _ = _NODE_META[nid]
            kn_id = nid if _NODE_META[nid][0] == "L3" else None
            conn.execute(
                """
                INSERT INTO question_steps(
                    step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id
                ) VALUES (?, ?, 'branch_1', ?, '子目标', ?, ?, ?);
                """,
                (f"{uid}::b1::{idx}", uid, idx, step_l1, step_l2, kn_id),
            )
    else:
        conn.execute(
            """
            INSERT INTO question_steps(
                step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id
            ) VALUES (?, ?, 'branch_1', 1, '子目标', ?, ?, NULL);
            """,
            (f"{uid}::b1::1", uid, l1, step_l2_fallback),
        )
    for stg in ("S1", "S2", "S3C", "S3D", "S4"):
        conn.execute("INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, 'OK');", (uid, stg))
    for tr in traps or []:
        row_c = conn.execute(
            "SELECT concept_id FROM concepts WHERE kind = 'TRAP' AND name = ? AND redirect_to_id IS NULL;",
            (tr,),
        ).fetchone()
        if row_c:
            cid = str(row_c["concept_id"])
        else:
            cid = f"TRAP:{tr}"
            conn.execute("INSERT OR IGNORE INTO concepts(concept_id, kind, name) VALUES (?, 'TRAP', ?);", (cid, tr))
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
            (uid, cid),
        )
    for mt in methods or []:
        cid = f"METHOD:{mt}"
        conn.execute("INSERT OR IGNORE INTO concepts(concept_id, kind, name) VALUES (?, 'METHOD', ?);", (cid, mt))
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'METHOD', 'MACHINE');",
            (uid, cid),
        )
    effective.recompute(conn, uid)


def _diag_payload(
    *,
    answer: str = "2",
    l1: str = "行程问题",
    sec_l1: list[str] | None = None,
    matched_l2: list[str] | None = None,
    steps: list[dict[str, Any]] | None = None,
    scores: tuple[int, int, int, int] = (2, 2, 2, 1),
    traps: list[str] | None = None,
    skeleton: str = "两运动体在固定路径上按速度比运动求时间",
) -> dict[str, Any]:
    c, s, t, l_sc = scores
    return {
        "visual_facts": "",
        "visual_ambiguity_flag": False,
        "visual_ambiguity_note": "",
        "method_summary": "按往返相遇路程倍比建立等量关系求解。",
        "solution_markdown": "设全程为单位份数，根据相遇路程倍比列式求解。",
        "final_answers": [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": answer,
                "sympy_expr": answer,
            }
        ],
        "sympy_code": f"def verify():\n    return {{'主问': '{answer}'}}",
        "steps": steps
        or [
            {
                "step_index": 1,
                "sub_goal": "建立相遇倍比方程",
                "core_formula": "S = 3a - b",
                "hint": "分析两次相遇合走的总路程倍数",
                "raw_l1": l1,
                "raw_l2": "相遇与追及",
                "raw_l3": "多次往返相遇倍比关系",
            }
        ],
        "abstract_skeleton": skeleton,
        "primary_l1_module": l1,
        "secondary_l1_modules": sec_l1 or [],
        "matched_l2": matched_l2 or ["相遇与追及"],
        "thinking_methods": ["比例法"],
        "classic_motifs": [],
        "trap_tags": traps if traps is not None else ["TRAP_SIGN_DIRECTION"],
        "difficulty": {
            "C_concept_count": {"evidence": "概念证据", "score": c},
            "S_chain_length": {"evidence": "链长证据", "score": s},
            "T_trickiness": {"evidence": "技巧证据", "score": t},
            "L_computation_load": {"evidence": "计算证据", "score": l_sc},
        },
    }


def _unit_vec(dim: int, base_angle: float) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[0] = float(np.cos(base_angle))
    v[1] = float(np.sin(base_angle))
    return v / np.linalg.norm(v)


def test_same_original_question_distinguishes_exact_from_number_swapped_twin() -> None:
    q_orig = (
        "甲、乙两人同时从山脚开始爬山，到达山顶后就立即下山，他们两人的下山速度都是各自上山速度的2倍。"
        "甲到山顶时，乙距山顶还有400米；甲回到山脚时，乙刚好下到半山腰。求从山脚到山顶的距离。（第8题）"
    )
    db_orig = (
        "甲、乙两人同时从山脚开始爬山，到达山顶后就立即下山，他们两人的下山速度都是各自上山速度的2倍。"
        "甲到山顶时，乙距山顶还有400米；甲回到山脚时，乙刚好下到半山腰。求山脚到山顶的距离。"
    )
    db_twin = (
        "甲、乙两人同时从山脚开始爬山，到达山顶后就立即下山，他们两人的下山速度都是各自上山速度的1.5倍。"
        "甲到山顶时，乙距山顶还有400米；甲回到山脚时，乙刚好下到半山腰。求从山脚到山顶的距离。"
    )
    assert _is_same_original_question(q_orig, db_orig, surf_sim=0.985) is True
    assert _is_same_original_question(q_orig, db_twin, surf_sim=0.962) is False


@pytest.mark.asyncio
async def test_p0_original_excluded_answer_conflict_warned_elo_borrowed_and_twin_retained(
    tmp_path: Path,
) -> None:
    db_path, cfg_dir, vec_dir, drafts_dir, settings = _setup_env(tmp_path)
    conn = get_connection(db_path)
    try:
        orig_text = (
            "湖中有A、B两岛，甲、乙二人都要在两岛间游一个来回。两人分别从A、B两岛同时出发，"
            "他们第一次相遇时距A岛700米，第二次相遇时距B岛400米。两岛相距多少米？"
        )
        twin_text = (
            "湖中有A、B两岛，甲、乙二人都要在两岛间游一个来回。两人分别从A、B两岛同时出发，"
            "他们第一次相遇时距A岛600米，第二次相遇时距B岛300米。两岛相距多少米？"
        )
        scaf_text = "甲、乙两人从相距1000米的两地相向而行，甲每分钟走60米，乙每分钟走40米，几分钟相遇？"

        # 库内真原题：验真答案 1700，真实难度为 L1 (scores 1,1,1,1 -> 1120)
        _seed_question(
            conn,
            uid="u_orig",
            source_id="ch17_062",
            text=orig_text,
            skeleton="两岸往返二次相遇求总长",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(1, 1, 1, 1),
            answer="1700",
        )
        # 换数孪生题（不同数值 600/300，允许并应当作为同构变式保留）
        _seed_question(
            conn,
            uid="u_twin",
            source_id="ch17_012",
            text=twin_text,
            skeleton="两岸往返二次相遇求总长",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(1, 1, 1, 1),
            answer="1500",
        )
        # Stage 4 已验证关联边子题
        _seed_question(
            conn,
            uid="u_scaf_verified",
            source_id="ch17_001",
            text=scaf_text,
            skeleton="两地相向一次相遇求时间",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(1, 1, 1, 1),
            answer="10",
        )
        conn.execute(
            """
            INSERT INTO question_relations(
                source_question_uid, target_question_uid, relation_type,
                is_directed, is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
                containment_score, trap_overlap, confidence_source, pedagogical_note, rule_version
            ) VALUES ('u_scaf_verified', 'u_orig', 'SCAFFOLD', 1, 0, 0.82, 0.91, 0.82, 0.91, 0.90, 0.0, 'LLM_VERIFIED', '先掌握单次相遇路程和', '2.4-init');
            """
        )
        conn.execute(
            """
            INSERT INTO question_relations(
                source_question_uid, target_question_uid, relation_type,
                is_directed, is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
                containment_score, trap_overlap, confidence_source, pedagogical_note, rule_version
            ) VALUES ('u_orig', 'u_twin', 'ISOMORPHIC', 0, 0, 0.95, 0.97, 0.95, 0.97, 1.0, 0.0, 'LLM_VERIFIED', '同构换数巩固两岸二次相遇公式', '2.4-init');
            """
        )
        conn.commit()

        # 单路草稿故意给错答案 1500 且虚高打分为 L3 (2,2,2,1)
        res = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text=orig_text + "（第2题）",
            grade_subject=GS,
            diagnosis_json=_diag_payload(answer="1500", scores=(2, 2, 2, 1)),
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )

        # 1. 真原题 u_orig 彻底从全部三栏排除
        all_rec_uids = [
            it["question_uid"]
            for grp in res["recommendations"].values()
            for it in grp
        ]
        assert "u_orig" not in all_rec_uids

        # 2. 换数孪生题 u_twin 被保留在同构变式栏，且复用 Stage 4 教学说明
        iso_items = res["recommendations"]["isomorphic_variants"]
        assert [it["question_uid"] for it in iso_items] == ["u_twin"]
        assert "二次相遇" in iso_items[0]["match_reason"]

        # 3. 借用库内已验真原题的 1120 Elo 覆盖单路虚高打分，且显式产出答案冲突告警
        assert res["elo_calibration_source"] == "DB_VERIFIED_MATCH"
        assert res["raw_draft_base_elo"] > 1120
        assert res["base_elo"] == 1120
        assert res["matched_db_question"]["answer_conflict"] is True
        assert any("DUPLICATE_ANSWER_CONFLICT" in w for w in res["warnings"])

        # 4. 批量报告清晰渲染冲突告警与精裁推荐理由
        md = render_batch_markdown_report(
            {
                "batch_id": "batch_test",
                "source_file": "test.md",
                "grade_subject": GS,
                "created_at": "2026-11-27 12:00:00 UTC",
                "total": 1,
                "succeeded": 1,
                "reused": 0,
                "failed": 0,
                "items": [
                    {
                        "index": 1,
                        "title": "题号 2",
                        "status": "OK",
                        "result": res,
                    }
                ],
            }
        )
        assert "⚠️ 库内验真答案冲突告警" in md
        assert "1700" in md
        assert "二次相遇" in md
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_p0_clone_dedup_trap_no_truncation_and_boss_elo_ceiling(tmp_path: Path) -> None:
    db_path, cfg_dir, vec_dir, drafts_dir, settings = _setup_env(tmp_path)
    conn = get_connection(db_path)
    try:
        # 两道 CLONE 孪生题（用词微差：有 vs 又、A vs B，且存在 CLONE 边）
        _seed_question(
            conn,
            uid="u_clone_a",
            source_id="ch17_026",
            text="甲、乙两车同时从A、B两地相向而行，在距B地54千米处相遇，折返后途中有在距A地42千米处相遇，求全程。",
            skeleton="往返二次相遇折返倍比",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(2, 2, 1, 1),
        )
        _seed_question(
            conn,
            uid="u_clone_b",
            source_id="ch17_067",
            text="甲、乙两车同时从A、B两地相向而行，在距A地52千米处相遇，折返后途中又在距B地44千米处相遇，求全程。",
            skeleton="往返二次相遇折返倍比",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(2, 2, 1, 1),
        )
        conn.execute(
            """
            INSERT INTO question_relations(
                source_question_uid, target_question_uid, relation_type,
                is_directed, is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct,
                containment_score, trap_overlap, confidence_source, pedagogical_note, rule_version
            ) VALUES ('u_clone_a', 'u_clone_b', 'CLONE', 0, 0, 0.97, 0.98, 0.99, 0.99, 1.0, 0.0, 'RULE_MATRIX', '', '2.4-init');
            """
        )
        # 一道同坑排雷好题（适宜 Elo） vs 一道 L5 跨级难题（c=4, s=3, t=3, l=3 -> >2000）
        _seed_question(
            conn,
            uid="u_trap_good",
            source_id="ch17_031",
            text="一列火车与步行者相向而行，注意参照方向转换，求错车时间。",
            skeleton="错车相对方向参照端点转换辨析",
            l1="行程问题",
            l3_nids=[],
            step_l2_fallback="火车过桥与错车",
            scores=(2, 2, 1, 1),
            traps=["TRAP_SIGN_DIRECTION"],
        )
        _seed_question(
            conn,
            uid="u_boss_l5",
            source_id="ch17_073",
            text="甲、乙、丙三车在多段变速折返中多次迎面与追上，求第十次相遇位置。",
            skeleton="三体变速周期状态机压轴",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(4, 3, 3, 3),
            traps=["TRAP_SIGN_DIRECTION"],
        )
        conn.commit()

        res = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text="A、B两地相向出发往返行走，第一次距A地70千米相遇，第二次距B地30千米相遇，求全程。",
            grade_subject=GS,
            diagnosis_json=_diag_payload(scores=(2, 1, 1, 1), traps=["TRAP_SIGN_DIRECTION"]),
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )

        iso_uids = [it["question_uid"] for it in res["recommendations"]["isomorphic_variants"]]
        trap_uids = [it["question_uid"] for it in res["recommendations"]["contrastive_and_traps"]]
        all_uids = [
            it["question_uid"]
            for grp in res["recommendations"].values()
            for it in grp
        ]

        # CLONE 簇最多仅出 1 道代表题，绝不同时塞入 ch17_026 与 ch17_067
        assert sum(1 for u in all_uids if u in {"u_clone_a", "u_clone_b"}) == 1
        assert len(iso_uids) == 1
        # 同坑好题进入排雷栏，而 L5 压轴难题被硬性 Elo 护栏拦截
        assert "u_trap_good" in trap_uids
        assert "u_boss_l5" not in all_uids
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_p0_confidence_floor_sparse_submodel_returns_fewer_than_three_with_inventory_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, cfg_dir, vec_dir, drafts_dir, settings = _setup_env(tmp_path)
    conn = get_connection(db_path)
    try:
        # 扶梯细分模型仅收录 1 道同模型题，其余 4 道为低骨架相似度的无关流水题
        _seed_question(
            conn,
            uid="u_esc_peer",
            source_id="ch18_003",
            text="自动扶梯匀速向上运行，甲向上走数得20级，乙以2倍速度向上走数得30级，求扶梯可见级数。",
            skeleton="同向双人走自动扶梯可见级数与梯速比",
            l1="行程问题",
            l3_nids=["n_l3_esc_rel"],
            scores=(3, 2, 2, 1),
        )
        for i in range(4):
            _seed_question(
                conn,
                uid=f"u_noise_{i}",
                source_id=f"ch17_90{i}",
                text=f"轮船在静水中时速{20 + i}千米，水流时速4千米，顺流航行3小时行多少千米？",
                skeleton="基础顺水航行路程公式计算",
                l1="行程问题",
                l3_nids=[],
                step_l2_fallback="流水行船与扶梯",
                scores=(1, 1, 1, 1),
            )
        conn.commit()

        # 构造真实向量：u_esc_peer 与查询夹角极小(cos≈0.99)，4 道噪音题夹角大(cos≈0.50)
        q_vec = _unit_vec(8, 0.0)
        peer_vec = _unit_vec(8, 0.08)
        noise_vec = _unit_vec(8, 1.05)

        store = VectorStore(vec_dir)
        uids_v = ["u_esc_peer", *[f"u_noise_{i}" for i in range(4)]]
        mat = np.vstack([peer_vec, *[noise_vec for _ in range(4)]])
        store._save_npz(store.npz_path("surface", SCOPE), uids_v, ["h"] * 5, mat)
        store._save_npz(store.npz_path("skeleton", SCOPE), uids_v, ["h"] * 5, mat)

        from shuati.llm.embeddings import EmbeddingClient

        async def _fake_embed(self: Any, texts: list[str], stage: str = "EMBED") -> list[list[float]]:
            return [q_vec.tolist() for _ in texts]

        monkeypatch.setattr(EmbeddingClient, "embed_texts", _fake_embed)

        res = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text="商场的自动扶梯匀速由下往上行驶，兄妹两人从扶梯上楼，哥哥每分走20级，妹妹每分走15级，求可见级数。",
            grade_subject=GS,
            diagnosis_json=_diag_payload(
                matched_l2=["流水行船与扶梯"],
                steps=[
                    {
                        "step_index": 1,
                        "sub_goal": "利用可见级数差求梯速",
                        "core_formula": "L = (v + v0)*t",
                        "hint": "比较两人走的级数与时间",
                        "raw_l1": "行程问题",
                        "raw_l2": "流水行船与扶梯",
                        "raw_l3": "可见级数与相对速度",
                    }
                ],
                scores=(3, 2, 2, 1),
                traps=[],
                skeleton="同向双人走自动扶梯可见级数与梯速比",
            ),
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )

        # 不强凑 3 题：噪音船题全部被置信下限拦截，铺垫栏 0 题、同构栏 1 题、辨析栏 0 题
        assert len(res["recommendations"]["scaffold_foundations"]) == 0
        assert [it["question_uid"] for it in res["recommendations"]["isomorphic_variants"]] == ["u_esc_peer"]
        assert len(res["recommendations"]["contrastive_and_traps"]) == 0
        assert res["submodel_inventory"]["total_in_db"] == 1
        assert "仅收录 1 道匹配题" in res["recommendation_notes"]["scaffold_foundations"]
        assert "仅收录 1 道匹配题" in res["recommendation_notes"]["isomorphic_variants"]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_p1_judge_llm_reranker_filters_reassigns_sanitizes_and_enforces_elo_guardrails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, cfg_dir, vec_dir, drafts_dir, settings = _setup_env(tmp_path)
    conn = get_connection(db_path)
    try:
        _seed_question(
            conn,
            uid="u_cand_iso",
            source_id="ch17_050",
            text="甲乙从两端相向出发往返，第一次距A地90米，第二次距B地40米，求全长。",
            skeleton="两岸往返二次相遇",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(2, 2, 1, 1),
            traps=["TRAP_SIGN_DIRECTION"],
        )
        _seed_question(
            conn,
            uid="u_cand_reject",
            source_id="ch17_051",
            text="某列车通过一座大桥用时45秒，求车速。",
            skeleton="单车过桥路程和",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(1, 1, 1, 1),
        )
        # 一道难度远高于错题的候选（scores 3,2,2,1 -> 1850），测试即使 judge_llm 误判为 SCAFFOLD 也会被二次 Elo 护栏拦截
        _seed_question(
            conn,
            uid="u_cand_too_hard_for_scaf",
            source_id="ch17_052",
            text="甲乙丙三人在环形跑道多次往返变速相遇。",
            skeleton="往返变速相遇",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(3, 2, 2, 1),
            traps=["TRAP_SIGN_DIRECTION"],
        )
        conn.commit()

        # 校验精裁消息零泄露：严禁出现 question_uid / source_id / chapter
        sample_msgs = build_diagnose_rerank_messages(
            grade_subject=GS,
            mistake_summary={"question_text": "测试错题", "ref_elo": 1400},
            candidates=[
                {
                    "candidate_id": "c01",
                    "uid": "primary_olympiad:ch17:secret_uid",
                    "source_id": "ch17_999",
                    "proposed_slot": "SCAFFOLD",
                    "base_elo": 1200,
                    "tier": "L2",
                    "primary_l1_module": "行程问题",
                    "l2_entities": ["行程问题/相遇与追及"],
                    "l3_operators": ["多次往返相遇"],
                    "thinking_methods": ["比例法"],
                    "trap_tags": [],
                    "surface_sim": 0.85,
                    "skeleton_sim": 0.90,
                    "question_text": "候选题干正文",
                    "abstract_skeleton": "候选骨架",
                }
            ],
        )
        serialized_msgs = json.dumps(sample_msgs, ensure_ascii=False)
        assert "secret_uid" not in serialized_msgs
        assert "ch17_999" not in serialized_msgs

        # 同时校验外部诊断提示词包含反 [2,2,2,1] 趋中坍缩与几何倍比核验约束
        ext_msgs = build_external_diagnose_messages(
            grade_subject=GS,
            question_text="正六角星测试",
            taxonomy_enum_text="- 行程问题\n  - 相遇与追及",
            anchors_prompt_text="无",
            config_dir=cfg_dir,
        )
        ext_prompt_str = json.dumps(ext_msgs, ensure_ascii=False)
        assert "[2,2,2,1]" in ext_prompt_str
        assert "正六角星" in ext_prompt_str

        # c01=u_cand_reject (SCAFFOLD), c02=u_cand_iso (ISOMORPHIC), c03=u_cand_too_hard_for_scaf (CONTRASTIVE_TRAP)
        monkeypatch.setitem(
            MOCK_RESPONSES,
            "DIAGNOSE_RERANK:judge_llm",
            {
                "decisions": [
                    {"candidate_id": "c01", "verdict": "REJECT", "pedagogical_reason": "火车过桥与往返相遇无关"},
                    {
                        "candidate_id": "c02",
                        "verdict": "ISOMORPHIC",
                        "pedagogical_reason": "同为两岸相向往返二次相遇3S-S2等量模型",
                    },
                    {
                        "candidate_id": "c03",
                        "verdict": "SCAFFOLD",
                        "pedagogical_reason": "试图越界塞入高难度题做铺垫",
                    },
                ]
            },
        )
        res = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text="两船从东西两岸同时相向开出往返，第一次距东岸85米，第二次距西岸35米，求两岸距离。",
            grade_subject=GS,
            diagnosis_json=_diag_payload(scores=(2, 2, 2, 1), traps=["TRAP_SIGN_DIRECTION"]),
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )

        assert res["rerank_applied"] is True
        assert res["recommendations"]["scaffold_foundations"] == []
        iso = res["recommendations"]["isomorphic_variants"]
        assert [it["question_uid"] for it in iso] == ["u_cand_iso"]
        assert iso[0]["match_reason"] == "同为两岸相向往返二次相遇3S-S2等量模型"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_p1_batch_reuse_upgrades_legacy_draft_recommendations_in_place(tmp_path: Path) -> None:
    db_path, cfg_dir, vec_dir, drafts_dir, settings = _setup_env(tmp_path)
    conn = get_connection(db_path)
    try:
        q_text = "甲、乙两人从相距1200米的两地相向而行，甲每分走70米，乙每分走50米，几分钟相遇？"
        _seed_question(
            conn,
            uid="u_orig_batch",
            source_id="ch17_088",
            text=q_text,
            skeleton="相向一次相遇",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            scores=(1, 1, 1, 1),
            answer="10",
        )
        conn.commit()

        # 先生成一版草稿，再手工篡改为旧版（移除 recommendation_engine_version 并把原题自己塞进推荐栏）
        first = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text=q_text,
            grade_subject=GS,
            diagnosis_json=_diag_payload(answer="10", scores=(1, 1, 1, 1)),
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )
        draft_file = drafts_dir / f"{first['draft_id']}.json"
        legacy_obj = json.loads(draft_file.read_text(encoding="utf-8"))
        legacy_obj.pop("recommendation_engine_version", None)
        legacy_obj["recommendations"]["isomorphic_variants"] = [
            {"question_uid": "u_orig_batch", "source_id": "ch17_088", "question_text": q_text, "base_elo": 1120}
        ]
        draft_file.write_text(json.dumps(legacy_obj, ensure_ascii=False), encoding="utf-8")
    finally:
        conn.close()

    md_input = tmp_path / "batch_in.md"
    md_input.write_text(f"## 第1题\n{q_text}\n", encoding="utf-8")

    conn2 = get_connection(db_path)
    try:
        batch_res = await diagnose_external_file_async(
            conn2,
            settings,
            file_path=md_input,
            grade_subject=GS,
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
            force=False,
        )
    finally:
        conn2.close()
    assert batch_res["reused"] == 1
    upgraded_item = batch_res["items"][0]["result"]
    assert upgraded_item["recommendation_engine_version"] == RECOMMENDATION_ENGINE_VERSION
    iso_uids = [it["question_uid"] for it in upgraded_item["recommendations"]["isomorphic_variants"]]
    assert "u_orig_batch" not in iso_uids


def test_p2_1_topology_sc_core_requires_skeleton_percentile_when_vectors_exist(tmp_path: Path) -> None:
    db_path, cfg_dir, vec_dir, _, settings = _setup_env(tmp_path)
    conn = get_connection(db_path)
    try:
        # q_easy (1 L3 + 1 METHOD -> k_union=2, elo=1120) 与 q_hard (3 L3 + 1 METHOD, elo=1850) 共享同一宽泛 L3 算子与方法，但骨架向量近似正交
        _seed_question(
            conn,
            uid="q_easy",
            source_id="ch17_e1",
            text="基础单步相遇题",
            skeleton="单步速度和乘时间",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi"],
            methods=["比例法"],
            scores=(1, 1, 1, 1),
        )
        _seed_question(
            conn,
            uid="q_hard",
            source_id="ch17_h1",
            text="多步综合复杂图形折返题",
            skeleton="多边形状态转移与周期不动点",
            l1="行程问题",
            l3_nids=["n_l3_meet_multi", "n_l3_hex_path", "n_l3_hex_diag"],
            methods=["比例法"],
            scores=(3, 2, 2, 1),
        )
        bg_uids: list[str] = []
        for idx in range(12):
            b_uid = f"q_bg_{idx:02d}"
            bg_uids.append(b_uid)
            _seed_question(
                conn,
                uid=b_uid,
                source_id=f"ch17_b{idx:02d}",
                text=f"背景相遇题{idx}",
                skeleton="单步速度和乘时间",
                l1="行程问题",
                l3_nids=["n_l3_meet_multi"],
                scores=(2, 1, 1, 1),
            )
        conn.commit()

        v_easy = _unit_vec(8, 0.0)
        v_hard = _unit_vec(8, 1.35)  # cos ≈ 0.22，远低于 0.90 分位
        all_uids = ["q_easy", "q_hard", *bg_uids]
        skel_rows = [v_easy, v_hard, *[_unit_vec(8, 0.02 * (i + 1)) for i in range(12)]]
        store = VectorStore(vec_dir)
        mat = np.vstack(skel_rows)
        store._save_npz(store.npz_path("surface", SCOPE), all_uids, ["h"] * len(all_uids), mat)
        store._save_npz(store.npz_path("skeleton", SCOPE), all_uids, ["h"] * len(all_uids), mat)
        store._save_meta(
            {
                "embedding_model": "mock",
                "dimension": 8,
                "meta_version": 1,
                "quantiles": {
                    f"{SCOPE}::surface": [i / 100.0 for i in range(101)],
                    f"{SCOPE}::skeleton": [i / 100.0 for i in range(101)],
                },
            }
        )

        feats = load_question_features(conn, grade_subject=GS)
        cands = generate_candidate_edges(
            conn,
            store,
            grade_subject=GS,
            scope_slug=SCOPE,
            topology_cfg=settings.topology,
            features=feats,
        )
        pair_scaffolds = [
            c
            for c in cands
            if c.relation_type == "SCAFFOLD"
            and {c.source_uid, c.target_uid} == {"q_easy", "q_hard"}
        ]
        assert len(pair_scaffolds) == 1
        # P2.1 核心断言：低骨架分位不得免审直通 RULE_MATRIX，必须进入 WIDE_RECALL_SCAFFOLD 待精裁
        assert pair_scaffolds[0].needs_llm_refine is True
        assert pair_scaffolds[0].refine_reason == "WIDE_RECALL_SCAFFOLD"

        # weave_single_question 不写入未经 LLM 精裁的 WIDE_RECALL_* 边
        weave_single_question(
            conn,
            store,
            question_uid="q_hard",
            topology_cfg=settings.topology,
        )
        direct_sc = conn.execute(
            """
            SELECT COUNT(*) FROM question_relations
            WHERE source_question_uid = 'q_easy' AND target_question_uid = 'q_hard' AND relation_type = 'SCAFFOLD';
            """
        ).fetchone()[0]
        assert direct_sc == 0
    finally:
        conn.close()
