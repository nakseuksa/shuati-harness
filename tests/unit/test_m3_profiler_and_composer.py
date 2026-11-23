"""M3 单元测试：profiler 在线水位重放、8 档阶梯、雷达 与 composer 梯度组卷、护栏及互斥。"""

from __future__ import annotations

import json

from shuati.config import Settings
from shuati.core import composer, effective, profiler
from shuati.db.connection import get_connection
from shuati.db.schema import init_db


def _insert_q(
    conn,
    uid: str,
    *,
    l1: str,
    l2: str,
    l3_op: str,
    c: int,
    s: int,
    t: int,
    l_score: int,
    traps: list[str] | None = None,
    sec_l1: list[str] | None = None,
    elo_override: int | None = None,
    tier_override: tuple[str, int] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'c', ?, ?, ?);",
        (uid, uid, f"题干_{uid}", f"h_{uid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format, m_verification_status,
            m_primary_l1_module, m_secondary_l1_modules_json,
            m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json
        )
        VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', ?, ?, ?, ?, ?, ?, '[{"label":"主问","answer_type":"NUMERIC","verify_mode":"SYMBOLIC","collection_mode":"SINGLE","answer":"10","sympy_expr":"10"}]');
        """,
        (uid, f"题干_{uid}", l1, json.dumps(sec_l1 or [], ensure_ascii=False), c, s, t, l_score),
    )
    if elo_override is not None:
        conn.execute(
            "INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint) VALUES (?, 'difficulty_override_json', ?, 'ANCHOR_CALIBRATION', ?);",
            (uid, json.dumps({"C": c, "S": s, "T": t, "L": l_score}), f"h_{uid}"),
        )
    nid = f"kn_{l2}_{l3_op}"
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, '小学奥数', '1.0', 'L3', ?, ?, ?);",
        (nid, l1, l2, l3_op),
    )
    conn.execute(
        "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解');",
        (f"{uid}::branch_1", uid),
    )
    conn.execute(
        "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, hint, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, 'branch_1', 1, '目标步', '提示步', ?, ?, ?);",
        (f"{uid}::branch_1::1", uid, l1, l2, nid),
    )
    for tr in traps or []:
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
            (uid, tr),
        )
    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK'), (?, 'S4', 'OK');",
        (uid, uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)


def test_profiler_dual_stream_void_repeat_and_diagnosis(tmp_path) -> None:
    conn = get_connection(tmp_path / "prof.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject='小学奥数';")

    # 3 道 L4-A（C=2,S=3,T=3,L=3 -> 1600+）全部 WRONG；3 道 L4-B（C=3,S=1,T=3,L=1 -> 1965）全部 SOLVED_SOLO
    for i in range(1, 4):
        _insert_q(conn, f"q_a{i}", l1="平面几何", l2="等高模型", l3_op="面积计算", c=2, s=3, t=3, l_score=3, traps=["TRAP:TRAP_DOMAIN_BOUNDARY"])
        _insert_q(conn, f"q_b{i}", l1="平面几何", l2="等高模型", l3_op="巧作辅助线", c=3, s=1, t=3, l_score=1, sec_l1=["组合与计数"])

    # 插入日志：包含 1 条 VOIDED 冲销与 7 天内重刷
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome, stuck_node_id) VALUES ('default', ?, ?, 'WRONG', 'kn_等高模型_面积计算');",
            (f"q_a{i}", f"2026-11-10 0{i}:00:00"),
        )
        conn.execute(
            "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', ?, ?, 'SOLVED_SOLO');",
            (f"q_b{i}", f"2026-11-11 0{i}:00:00"),
        )
    # q_b1 在 3 天内重刷且使用 score_ratio=0.5 与 hint_level=1
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome, score_ratio, hint_level_used) VALUES ('default', 'q_b1', '2026-11-14 01:00:00', 'SOLVED_WITH_HINT', 0.8, 1);"
    )
    # 插入一条将被 VOIDED 冲销的错误记录
    cur = conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', 'q_b2', '2026-11-15 00:00:00', 'WRONG');"
    )
    bad_id = cur.lastrowid
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome, voids_log_id) VALUES ('default', 'q_b2', '2026-11-15 01:00:00', 'VOIDED', ?);",
        (bad_id,),
    )
    conn.commit()

    prof = profiler.compute_learner_profile(
        conn, learner_id="default", grade_subject="小学奥数", elo_cfg=Settings().learner_elo
    )
    assert prof["overall"]["valid_attempts"] == 7
    assert "平面几何" in prof["modules"]
    assert "组合与计数" in prof["modules"]
    assert prof["modules"]["组合与计数"]["rating"] != 1300.0
    assert "偏怕繁琐计算与分类讨论" in prof["ability_diagnosis"]
    assert prof["weak_radar"]["l2"][0]["l2_entity"] == "等高模型"
    assert prof["weak_radar"]["trap"][0]["name"] == "TRAP_DOMAIN_BOUNDARY"
    assert prof["weak_radar"]["stuck_points"][0]["node_id"] == "kn_等高模型_面积计算"
    conn.close()


def test_composer_mix_guardrails_clone_mutex_and_recent_batches(tmp_path) -> None:
    conn = get_connection(tmp_path / "comp.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject='小学奥数';")

    # 构建从低分到高阶的丰富题目，含 CLONE 对 (g1, g2)
    _insert_q(conn, "g1", l1="平面几何", l2="等高模型", l3_op="基础面积", c=1, s=1, t=1, l_score=1, elo_override=1150, tier_override=("L1_基础直达", 1))
    _insert_q(conn, "g2", l1="平面几何", l2="等高模型", l3_op="基础面积", c=1, s=1, t=1, l_score=1, elo_override=1160, tier_override=("L1_基础直达", 1))
    _insert_q(conn, "g3", l1="平面几何", l2="等高模型", l3_op="比例转换", c=1, s=1, t=1, l_score=2, elo_override=1185, tier_override=("L1_基础直达", 1))
    _insert_q(conn, "g4", l1="平面几何", l2="等高模型", l3_op="综合转化", c=2, s=2, t=2, l_score=1, elo_override=1450, tier_override=("L3_综合中档", 3))
    _insert_q(conn, "g5", l1="平面几何", l2="等高模型", l3_op="陷阱辨析", c=2, s=2, t=2, l_score=2, traps=["TRAP:TRAP_EXTRANEOUS_ROOT"], elo_override=1520, tier_override=("L3_综合中档", 3))
    _insert_q(conn, "g6", l1="平面几何", l2="等高模型", l3_op="压轴巧思", c=3, s=2, t=3, l_score=2, elo_override=2050, tier_override=("L4-B_进阶拔高(巧)", 5))
    _insert_q(conn, "g7", l1="平面几何", l2="等高模型", l3_op="竞赛压轴", c=4, s=2, t=2, l_score=2, elo_override=2480, tier_override=("L5-2_竞赛超难", 7))

    # g1 与 g2 为 CLONE 孪生对；g1 -> g4 为 SCAFFOLD 升维
    conn.execute(
        "INSERT INTO question_relations(source_question_uid, target_question_uid, relation_type, is_directed, is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct, containment_score, trap_overlap, confidence_source) VALUES ('g1', 'g2', 'CLONE', 0, 0, 0.99, 0.99, 0.99, 0.99, 1.0, 0.0, 'RULE_MATRIX');"
    )
    conn.execute(
        "INSERT INTO question_relations(source_question_uid, target_question_uid, relation_type, is_directed, is_cross_scope, surface_sim, skeleton_sim, surface_pct, skeleton_pct, containment_score, trap_overlap, confidence_source) VALUES ('g1', 'g4', 'SCAFFOLD', 1, 0, 0.50, 0.85, 0.50, 0.92, 0.7, 0.0, 'RULE_MATRIX');"
    )
    conn.commit()

    settings = Settings()

    # 1. 低水位 ZPD 钳位测试 (target_elo=900 -> R+200 < 1200)
    res_low = composer.compose_quiz(
        conn,
        learner_id="default",
        grade_subject="小学奥数",
        elo_cfg=settings.learner_elo,
        composer_cfg=settings.composer,
        l1="平面几何",
        count=4,
        mix="1:2:1",
        target_elo=900,
    )
    assert res_low["actual_count"] == 4
    selected_uids_1 = {x["question_uid"] for x in res_low["items"]}
    # 同卷 CLONE 绝不同时选中 g1 与 g2
    assert not ({"g1", "g2"} <= selected_uids_1)
    assert any("低水位 ZPD 钳位" in w for w in res_low["warnings"])

    # 2. 再次组卷应受 exclude_recent_quiz_batches 避重排除掉上一批未登记题
    res_high = composer.compose_quiz(
        conn,
        learner_id="default",
        grade_subject="小学奥数",
        elo_cfg=settings.learner_elo,
        composer_cfg=settings.composer,
        l1="平面几何",
        count=3,
        mix="1:2:0",
        target_elo=1850,
    )
    selected_uids_2 = {x["question_uid"] for x in res_high["items"]}
    assert selected_uids_1.isdisjoint(selected_uids_2)
    # 高水位至少挑中高阶题 g6 / g7
    assert ("g6" in selected_uids_2 or "g7" in selected_uids_2)
    conn.close()
