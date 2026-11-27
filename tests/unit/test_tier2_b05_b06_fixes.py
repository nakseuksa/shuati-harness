"""Tier 2 外部审查（B05 + B06）全量回归单元测试。

覆盖范围：
- B05-F1..F10 (`profiler.py` & `composer.py`)
- B06-F1..F11 (`practice.py` & `facade.reconfirm_stale_overrides`)
所有测试严格使用 `tmp_path` 隔离 `db` / `human_dir` / `vectors_dir` / `assets_dir`。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from shuati.config import Settings
from shuati.core import composer, effective, profiler
from shuati.db import human_mirror, repository
from shuati.db.connection import PreconditionError, SingleWriterExecutor, get_connection
from shuati.db.repository import NotFoundError
from shuati.db.schema import init_db
from shuati.models.domain import AnswerPart
from shuati.services import practice
from shuati.vectors.store import VectorStore


def _make_db(tmp_path: Path, name: str = "tier2.db") -> sqlite3.Connection:
    conn = get_connection(tmp_path / name)
    init_db(conn)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject='小学奥数';"
    )
    conn.commit()
    return conn


def _seed_question(
    conn: sqlite3.Connection,
    uid: str,
    *,
    grade_subject: str = "小学奥数",
    text: str | None = None,
    l1: str = "平面几何",
    l2: str = "等高模型",
    l3_op: str = "面积计算",
    node_id: str | None = None,
    c: int = 2,
    s: int = 2,
    t: int = 2,
    l_score: int = 1,
    elo_override: int | None = None,
    tier_override: tuple[str, int] | None = None,
    traps: list[str] | None = None,
    final_answers: list[dict[str, Any]] | None = None,
    branches: list[dict[str, Any]] | None = None,
) -> None:
    q_text = text or f"题干_{uid}"
    fa_list = final_answers or [
        {
            "label": "主问",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "SINGLE",
            "answer": "42",
            "sympy_expr": "42",
        }
    ]
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash
        )
        VALUES (?, ?, 'BATCH_JSONL', 'col', ?, ?, ?);
        """,
        (uid, grade_subject, uid, q_text, f"h_{uid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format, m_verification_status,
            m_primary_l1_module, m_secondary_l1_modules_json,
            m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json, m_abstract_skeleton
        )
        VALUES (?, ?, ?, '解答题', 'VERIFIED_CONSENSUS', ?, '[]', ?, ?, ?, ?, ?, ?);
        """,
        (
            uid,
            grade_subject,
            q_text,
            l1,
            c,
            s,
            t,
            l_score,
            json.dumps(fa_list, ensure_ascii=False),
            f"骨架_{uid}",
        ),
    )

    nid = node_id or f"kn_{l2}_{l3_op}"
    conn.execute(
        """
        INSERT OR IGNORE INTO knowledge_nodes(
            node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator
        )
        VALUES (?, ?, '1.0', 'L3', ?, ?, ?);
        """,
        (nid, grade_subject, l1, l2, l3_op),
    )

    if branches is None:
        branches = [
            {
                "branch_id": "branch_1",
                "is_optimal": 1,
                "steps": [(1, "目标步", "提示步", nid)],
            }
        ]
    for br in branches:
        bid = str(br["branch_id"])
        is_opt = int(br.get("is_optimal", 0))
        conn.execute(
            """
            INSERT INTO question_solutions(
                solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
            )
            VALUES (?, ?, ?, ?, 'PRIMARY_VLM', '解答');
            """,
            (f"{uid}::{bid}", uid, bid, is_opt),
        )
        for step_idx, sub_goal, hint, step_nid in br.get("steps", []):
            conn.execute(
                """
                INSERT INTO question_steps(
                    step_id, question_uid, branch_id, step_index, sub_goal, hint, m_l1, m_l2, m_knowledge_node_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (f"{uid}::{bid}::{step_idx}", uid, bid, step_idx, sub_goal, hint, l1, l2, step_nid),
            )

    for tr in traps or []:
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'TRAP', 'MACHINE');",
            (uid, tr),
        )

    conn.execute(
        """
        INSERT INTO compile_state(question_uid, stage, status)
        VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'OK'), (?, 'S3D', 'OK'), (?, 'S4', 'OK');
        """,
        (uid, uid, uid, uid, uid),
    )
    effective.recompute(conn, uid)
    if elo_override is not None or tier_override is not None:
        with effective.authorized_effective_writer():
            if elo_override is not None:
                conn.execute(
                    "UPDATE questions SET eff_base_elo = ? WHERE question_uid = ?;",
                    (elo_override, uid),
                )
            if tier_override is not None:
                conn.execute(
                    "UPDATE questions SET eff_tier = ?, eff_tier_rank = ? WHERE question_uid = ?;",
                    (tier_override[0], tier_override[1], uid),
                )
    repository.refresh_search_index(conn, uid)


def _add_clone(conn: sqlite3.Connection, u: str, v: str) -> None:
    lo, hi = (u, v) if u < v else (v, u)
    conn.execute(
        """
        INSERT INTO question_relations(
            source_question_uid, target_question_uid, relation_type, is_directed, is_cross_scope,
            surface_sim, skeleton_sim, surface_pct, skeleton_pct, containment_score, trap_overlap, confidence_source
        )
        VALUES (?, ?, 'CLONE', 0, 0, 0.99, 0.99, 0.99, 0.99, 1.0, 0.0, 'RULE_MATRIX');
        """,
        (lo, hi),
    )


# ---------------------------------------------------------------------------
# B05-F1..F10
# ---------------------------------------------------------------------------


def test_b05_f1_intra_quiz_clone_mutex_all_six_loops(tmp_path: Path) -> None:
    settings = Settings()

    # 1. _select_by_window (SOLID 2:0:0)
    conn = _make_db(tmp_path, "f1_win.db")
    _seed_question(conn, "q1", elo_override=1270, tier_override=("L2_常规巩固", 2))
    _seed_question(conn, "q2", elo_override=1270, tier_override=("L2_常规巩固", 2))
    _seed_question(conn, "q3", elo_override=1285, tier_override=("L2_常规巩固", 2))
    _add_clone(conn, "q1", "q2")
    conn.commit()
    res = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="2:0:0", target_elo=1300,
    )
    uids = {it["question_uid"] for it in res["items"]}
    assert not ({"q1", "q2"} <= uids) and "q3" in uids
    conn.close()

    # 2. high_band ({L4-B, L5-1} target_elo=1900, mix=0:2:0)
    conn = _make_db(tmp_path, "f1_high.db")
    _seed_question(conn, "q1", elo_override=1965, tier_override=("L4-B_进阶拔高(巧)", 5))
    _seed_question(conn, "q2", elo_override=1965, tier_override=("L4-B_进阶拔高(巧)", 5))
    _seed_question(conn, "q3", elo_override=2020, tier_override=("L5-1_压轴挑战", 6))
    _add_clone(conn, "q1", "q2")
    conn.commit()
    res = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="0:2:0", target_elo=1900,
    )
    uids = {it["question_uid"] for it in res["items"]}
    assert not ({"q1", "q2"} <= uids) and "q3" in uids
    conn.close()

    # 3. climb_cands (L5-2/L5-3 target_elo=2450, mix=0:2:0)
    conn = _make_db(tmp_path, "f1_climb.db")
    _seed_question(conn, "q1", elo_override=2520, tier_override=("L5-2_竞赛超难", 7))
    _seed_question(conn, "q2", elo_override=2520, tier_override=("L5-2_竞赛超难", 7))
    _seed_question(conn, "q3", elo_override=2620, tier_override=("L5-3_殿堂极限", 8))
    _add_clone(conn, "q1", "q2")
    conn.commit()
    res = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="0:2:0", target_elo=2450,
    )
    uids = {it["question_uid"] for it in res["items"]}
    assert not ({"q1", "q2"} <= uids) and "q3" in uids
    conn.close()

    # 4. fallback_high (target_elo=2450, 无 L5-2/L5-3)
    conn = _make_db(tmp_path, "f1_fb.db")
    _seed_question(conn, "q1", elo_override=1750, tier_override=("L4-A_进阶拔高(繁)", 4))
    _seed_question(conn, "q2", elo_override=1750, tier_override=("L4-A_进阶拔高(繁)", 4))
    _seed_question(conn, "q3", elo_override=1600, tier_override=("L3_综合中档", 3))
    _add_clone(conn, "q1", "q2")
    conn.commit()
    res = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="0:2:0", target_elo=2450,
    )
    uids = {it["question_uid"] for it in res["items"]}
    assert not ({"q1", "q2"} <= uids) and "q3" in uids
    conn.close()

    # 5 & 6. radar_cands (弱陷阱雷达) & non_slip_cands (冷启动兜底)
    conn = _make_db(tmp_path, "f1_trap.db")
    _seed_question(conn, "q1", elo_override=1300, traps=["TRAP:TRAP_DOMAIN_BOUNDARY"])
    _seed_question(conn, "q2", elo_override=1300, traps=["TRAP:TRAP_DOMAIN_BOUNDARY"])
    _seed_question(conn, "q3", elo_override=1340, traps=["TRAP:TRAP_DOMAIN_BOUNDARY"])
    _add_clone(conn, "q1", "q2")
    conn.commit()
    # 冷启动 non_slip_cands
    res_cold = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="0:0:2", exclude_recent_quiz_batches=0,
    )
    uids_cold = {it["question_uid"] for it in res_cold["items"]}
    assert not ({"q1", "q2"} <= uids_cold) and "q3" in uids_cold

    # 加入历史错题（5 天前）触发 weak_radar -> radar_cands，同时 q_hist 自身受 14 天冷却排除出候选
    _seed_question(conn, "q_hist", elo_override=1300, traps=["TRAP:TRAP_DOMAIN_BOUNDARY"])
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', 'q_hist', '2026-05-20 00:00:00', 'WRONG');"
    )
    conn.commit()
    res_radar = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="0:0:2", exclude_recent_quiz_batches=0,
        now_utc=datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC),
    )
    uids_radar = {it["question_uid"] for it in res_radar["items"]}
    assert not ({"q1", "q2"} <= uids_radar) and "q3" in uids_radar
    conn.close()


def test_b05_f2_match_node_uids_strict_eff_only(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "f2.db")
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES ('N_OLD', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '旧算子'), ('N_NEW', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '新算子');"
    )
    _seed_question(conn, "q1", node_id="N_OLD", elo_override=1300)
    conn.execute(
        "INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint, taxonomy_version) VALUES ('q1', 'taxonomy_override_json', ?, 'PI_CLI', 'h_q1', '1.0');",
        (json.dumps({"mode": "REPLACE", "nodes": [{"node_id": "N_NEW"}]}),),
    )
    effective.recompute(conn, "q1")
    conn.commit()

    settings = Settings()
    with pytest.raises(PreconditionError):
        composer.compose_quiz(
            conn, learner_id="default", grade_subject="小学奥数",
            elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
            l3="N_OLD", count=1, mix="1:0:0",
        )

    res_new = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        l3="N_NEW", count=1, mix="1:0:0",
    )
    assert [it["question_uid"] for it in res_new["items"]] == ["q1"]
    conn.close()


def test_b05_f3_cooldown_includes_original_with_single_retry_allowance(tmp_path: Path) -> None:
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    ten_days_ago = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    settings = Settings()

    # 场景 1：昨日 SKIPPED 的原题及其 CLONE 均被 14 天冷却排除
    conn = _make_db(tmp_path, "f3_s1.db")
    for u in ("q_orig", "q_clone", "q_a", "q_b"):
        _seed_question(conn, u, elo_override=1300)
    _add_clone(conn, "q_orig", "q_clone")
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', 'q_orig', ?, 'SKIPPED');",
        (yesterday,),
    )
    conn.commit()
    res1 = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="2:0:0", now_utc=now,
    )
    assert {it["question_uid"] for it in res1["items"]} == {"q_a", "q_b"}
    conn.close()

    # 场景 2：10 天前 SOLVED_SOLO，exclude_solved_days=7, exclude_clone_days=14，原题与 CLONE 均被排除
    conn = _make_db(tmp_path, "f3_s2.db")
    for u in ("q_orig", "q_clone", "q_a", "q_b"):
        _seed_question(conn, u, elo_override=1300)
    _add_clone(conn, "q_orig", "q_clone")
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', 'q_orig', ?, 'SOLVED_SOLO');",
        (ten_days_ago,),
    )
    conn.commit()
    res2 = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="2:0:0", exclude_solved_days=7, exclude_clone_days=14, now_utc=now,
    )
    assert {it["question_uid"] for it in res2["items"]} == {"q_a", "q_b"}
    conn.close()

    # 场景 3：昨日 3 题 WRONG，稳固槽仅放行最多 1 题回练，其余优先选新题
    conn = _make_db(tmp_path, "f3_s3.db")
    for u in ("q_w1", "q_w2", "q_w3", "q_f1", "q_f2"):
        _seed_question(conn, u, elo_override=1300)
    for wu in ("q_w1", "q_w2", "q_w3"):
        conn.execute(
            "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', ?, ?, 'WRONG');",
            (wu, yesterday),
        )
    conn.commit()
    res3 = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=3, mix="3:0:0", now_utc=now,
    )
    picked3 = {it["question_uid"] for it in res3["items"]}
    assert {"q_f1", "q_f2"} <= picked3
    assert len(picked3 & {"q_w1", "q_w2", "q_w3"}) == 1
    conn.close()


def test_b05_f4_high_elo_stretch_no_difficulty_inversion(tmp_path: Path) -> None:
    settings = Settings()
    conn = _make_db(tmp_path, "f4.db")
    _seed_question(conn, "q_l4b", elo_override=1965, tier_override=("L4-B_进阶拔高(巧)", 5))
    _seed_question(conn, "q_l51", elo_override=2020, tier_override=("L5-1_压轴挑战", 6))
    _seed_question(conn, "q_l52_solid", elo_override=2420, tier_override=("L5-2_竞赛超难", 7))
    _seed_question(conn, "q_l52_stretch", elo_override=2550, tier_override=("L5-2_竞赛超难", 7))
    _seed_question(conn, "q_l53", elo_override=2650, tier_override=("L5-3_殿堂极限", 8))
    conn.commit()

    # R=2450：STRETCH 必须直爬 L5-2/L5-3，严禁倒挂选 q_l4b (1965)
    res_2450 = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="1:1:0", target_elo=2450, exclude_recent_quiz_batches=0,
    )
    by_slot = {it["slot"]: it for it in res_2450["items"]}
    assert by_slot["SOLID"]["question_uid"] == "q_l52_solid"
    assert by_slot["STRETCH"]["question_uid"] == "q_l52_stretch"
    assert by_slot["STRETCH"]["base_elo"] > by_slot["SOLID"]["base_elo"]

    # R=2650：优先 L5-3 (2650) 而非较低分的 L5-2
    res_2650 = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=1, mix="0:1:0", target_elo=2650, exclude_recent_quiz_batches=0,
    )
    assert res_2650["items"][0]["question_uid"] == "q_l53"

    # R=1900：保留 {L4-B, L5-1} 合并选拔
    res_1900 = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=2, mix="0:2:0", target_elo=1900, exclude_recent_quiz_batches=0,
    )
    assert {it["question_uid"] for it in res_1900["items"]} == {"q_l4b", "q_l51"}
    conn.close()

    # 空 L5-2/L5-3 回退契约
    conn2 = _make_db(tmp_path, "f4_empty.db")
    _seed_question(conn2, "q_l4b", elo_override=1965, tier_override=("L4-B_进阶拔高(巧)", 5))
    _seed_question(conn2, "q_l51", elo_override=2300, tier_override=("L5-1_压轴挑战", 6))
    conn2.commit()
    res_fb = composer.compose_quiz(
        conn2, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=1, mix="0:1:0", target_elo=2450, exclude_recent_quiz_batches=0,
    )
    assert res_fb["items"][0]["question_uid"] == "q_l51"
    assert any("候选不足" in w for w in res_fb["warnings"])
    conn2.close()


def test_b05_f5_l2_empty_match_not_dropped_with_l3(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "f5.db")
    _seed_question(conn, "q1", l2="三角形", l3_op="面积", node_id="N1", elo_override=1300)
    conn.commit()
    settings = Settings()
    with pytest.raises(PreconditionError):
        composer.compose_quiz(
            conn, learner_id="default", grade_subject="小学奥数",
            elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
            l2="不存在的L2", l3="N1", count=1, mix="1:0:0",
        )
    conn.close()


def test_b05_f6_traps_redirect_and_arithmetic_slip_alias_filtered(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "f6.db")
    conn.execute(
        "INSERT OR IGNORE INTO concepts(concept_id, kind, name, aliases_json, redirect_to_id, is_seed) VALUES ('TRAP:MASTER', 'TRAP', '主陷阱', '[]', NULL, 0);"
    )
    conn.execute(
        "INSERT OR IGNORE INTO concepts(concept_id, kind, name, aliases_json, redirect_to_id, is_seed) VALUES ('TRAP:CHILD', 'TRAP', '子陷阱', '[]', 'TRAP:MASTER', 0);"
    )
    conn.execute(
        "INSERT OR IGNORE INTO concepts(concept_id, kind, name, aliases_json, redirect_to_id, is_seed) VALUES ('TRAP:SLIP_ALIAS', 'TRAP', '计算笔误', '[]', 'TRAP:TRAP_ARITHMETIC_SLIP', 0);"
    )
    _seed_question(conn, "q_hist", elo_override=1300, traps=["TRAP:CHILD"])
    _seed_question(conn, "q_target_child", elo_override=1500, traps=["TRAP:CHILD"])
    _seed_question(conn, "q_slip_alias", elo_override=1300, traps=["TRAP:SLIP_ALIAS"])
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', 'q_hist', '2026-05-20 00:00:00', 'WRONG');"
    )
    conn.commit()

    settings = Settings()
    res = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=1, mix="0:0:1", now_utc=datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC),
    )
    assert res["items"][0]["question_uid"] == "q_target_child"
    assert "TRAP:MASTER" in res["items"][0]["reason_full"]

    # 冷启动（无薄弱雷达）下，重定向至 TRAP_ARITHMETIC_SLIP 的 q_slip_alias 不得被当成非笔误高危陷阱选中
    conn2 = _make_db(tmp_path, "f6_cold.db")
    conn2.execute(
        "INSERT OR IGNORE INTO concepts(concept_id, kind, name, aliases_json, redirect_to_id, is_seed) VALUES ('TRAP:SLIP_ALIAS', 'TRAP', '计算笔误', '[]', 'TRAP:TRAP_ARITHMETIC_SLIP', 0);"
    )
    _seed_question(conn2, "q_slip_alias", elo_override=1300, traps=["TRAP:SLIP_ALIAS"])
    _seed_question(conn2, "q_real_trap", elo_override=1500, traps=["TRAP:TRAP_DOMAIN_BOUNDARY"])
    conn2.commit()
    res_cold = composer.compose_quiz(
        conn2, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=1, mix="0:0:1",
    )
    assert res_cold["items"][0]["question_uid"] == "q_real_trap"
    conn2.close()
    conn.close()


def test_b05_f7_profiler_solved_with_hint_score_ratio_without_hint_level(tmp_path: Path) -> None:
    elo_cfg = Settings().learner_elo
    assert profiler._compute_actual_score("SOLVED_WITH_HINT", 1.0, None, elo_cfg) == pytest.approx(0.5)
    assert profiler._compute_actual_score("SOLVED_WITH_HINT", 0.8, None, elo_cfg) == pytest.approx(0.4)
    assert profiler._compute_actual_score("SOLVED_SOLO", 1.0, None, elo_cfg) == pytest.approx(1.0)

    conn = _make_db(tmp_path, "f7.db")
    _seed_question(conn, "q1", elo_override=1300)
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome, score_ratio, hint_level_used) VALUES ('default', 'q1', '2026-05-10 00:00:00', 'SOLVED_WITH_HINT', 1.0, NULL);"
    )
    conn.commit()
    prof = profiler.compute_learner_profile(
        conn, learner_id="default", grade_subject="小学奥数", elo_cfg=elo_cfg
    )
    # E=0.5, A=0.5 -> rating 应保持 1300.0 而非涨至 1324.0
    assert prof["overall"]["rating"] == 1300.0
    conn.close()


def test_b05_f8_empty_quiz_batch_not_persisted_and_ignored_in_recent_limit(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "f8.db")
    _seed_question(conn, "q1", elo_override=1300)
    conn.commit()
    settings = Settings()

    counts: list[int] = []
    batch_ids: list[str | None] = []
    for _ in range(5):
        res = composer.compose_quiz(
            conn, learner_id="default", grade_subject="小学奥数",
            elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
            count=1, mix="1:0:0", exclude_recent_quiz_batches=3,
        )
        counts.append(res["actual_count"])
        batch_ids.append(res["quiz_batch_id"])

    assert counts == [1, 0, 0, 0, 0]
    assert batch_ids[0] is not None
    assert all(b is None for b in batch_ids[1:])
    assert conn.execute("SELECT COUNT(*) FROM quiz_batches;").fetchone()[0] == 1

    # 即使历史库中已存在 3 条空 items_json='[]' 记录，查询也必须忽略它们以保护真实未作答卷
    for i in range(3):
        conn.execute(
            "INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json, created_at) VALUES (?, 'default', '小学奥数', '{}', '[]', ?);",
            (f"qz_empty_{i}", f"2099-01-0{i + 1} 00:00:00"),
        )
    conn.commit()
    res_after_empty = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=1, mix="1:0:0", exclude_recent_quiz_batches=3,
    )
    assert res_after_empty["actual_count"] == 0
    conn.close()


def test_b05_f9_utc_datetime_normalization_iso_and_offset(tmp_path: Path) -> None:
    settings = Settings()
    now_utc = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    # 场景 1：14.5 天前的 ISO 'T'/'Z' 时间戳已过冷却窗，不应触发冷却兜底告警
    conn = _make_db(tmp_path, "f9_s1.db")
    _seed_question(conn, "q1", elo_override=1300)
    _seed_question(conn, "q2", elo_override=1300)
    _add_clone(conn, "q1", "q2")
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', 'q1', '2026-05-06T00:00:00Z', 'SOLVED_SOLO');"
    )
    conn.commit()
    res1 = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=1, mix="1:0:0", now_utc=now_utc,
    )
    assert res1["items"][0]["question_uid"] == "q2"
    assert not any("CLONE 冷却" in w for w in res1["warnings"])
    conn.close()

    # 场景 2：+08:00 偏移时间戳（20:00+08:00 == 12:00 UTC）早于 qb1 创建时间 15:00 UTC，q1 仍应被视为未作答在途题
    conn = _make_db(tmp_path, "f9_s2.db")
    _seed_question(conn, "q1", elo_override=1300)
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', 'q1', '2026-05-20T20:00:00+08:00', 'WRONG');"
    )
    conn.execute(
        "INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json, created_at) VALUES ('qb1', 'default', '小学奥数', '{}', ?, '2026-05-20 15:00:00');",
        (json.dumps([{"question_uid": "q1"}]),),
    )
    conn.commit()
    # 传入非 UTC 时区 now_utc（+08:00）也应正确归一到 UTC
    now_shanghai = datetime(2026, 5, 20, 23, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    res2 = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        count=1, mix="1:0:0", now_utc=now_shanghai,
    )
    assert res2["actual_count"] == 0
    conn.close()


def test_b05_f10_redirected_l3_resolves_canonical_l1_module_rating(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "f10.db")
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator, redirect_to_id)
        VALUES
          ('N_MASTER', '小学奥数', '1.0', 'L3', '数论', '整除', '主算子', NULL),
          ('N_CHILD', '小学奥数', '1.0', 'L3', '数论', '整除', '旧算子', 'N_MASTER');
        """
    )
    _seed_question(conn, "q_geo", l1="平面几何", elo_override=1300)
    _seed_question(conn, "q_nt", l1="数论", l2="整除", l3_op="主算子", node_id="N_MASTER", elo_override=1300)
    conn.execute(
        "INSERT INTO practice_log(learner_id, question_uid, practiced_at, outcome) VALUES ('default', 'q_geo', '2026-05-01 00:00:00', 'WRONG');"
    )
    conn.commit()

    settings = Settings()
    res = composer.compose_quiz(
        conn, learner_id="default", grade_subject="小学奥数",
        elo_cfg=settings.learner_elo, composer_cfg=settings.composer,
        l3="N_CHILD", count=1, mix="1:0:0",
    )
    assert res["rating_source"] == "module:数论"
    assert res["base_elo_used"] == 1300.0
    assert res["items"][0]["question_uid"] == "q_nt"
    conn.close()


# ---------------------------------------------------------------------------
# B06-F1..F11
# ---------------------------------------------------------------------------


def test_b06_f1_decimal_and_paren_expr_not_mis_parsed_as_labels(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f1.db")
    human_dir = tmp_path / "human"
    settings = Settings()
    settings.pipeline.use_mock_llm = True

    _seed_question(
        conn,
        "q_pi",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "3.14",
                "sympy_expr": "3.14",
            }
        ],
    )
    _seed_question(
        conn,
        "q_five",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "5",
                "sympy_expr": "5",
            }
        ],
    )
    _seed_question(
        conn,
        "q_two",
        final_answers=[
            {
                "label": "(1)",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "1.5",
                "sympy_expr": "1.5",
            },
            {
                "label": "(2)",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "2.5",
                "sympy_expr": "2.5",
            },
        ],
    )
    _seed_question(
        conn,
        "q_zero",
        final_answers=[
            {
                "label": "(1)",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "0.5",
                "sympy_expr": "0.5",
            },
            {
                "label": "(2)",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "1.5",
                "sympy_expr": "1.5",
            },
        ],
    )
    _seed_question(
        conn,
        "q_same_int",
        final_answers=[
            {
                "label": "(1)",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "1.5",
                "sympy_expr": "1.5",
            },
            {
                "label": "(2)",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "1.8",
                "sympy_expr": "1.8",
            },
        ],
    )
    conn.commit()

    r_pi = asyncio.run(practice.check_answer_async(conn, settings, "q_pi", answer_input="3.14", human_dir=human_dir))
    assert r_pi["all_correct"] is True

    r_pi_lbl = asyncio.run(practice.check_answer_async(conn, settings, "q_pi", answer_input="主问: 3.14", human_dir=human_dir))
    assert r_pi_lbl["all_correct"] is True

    r_paren1 = asyncio.run(practice.check_answer_async(conn, settings, "q_five", answer_input="(2)+3", human_dir=human_dir))
    assert r_paren1["all_correct"] is True

    r_paren2 = asyncio.run(practice.check_answer_async(conn, settings, "q_five", answer_input="(2) + 3", human_dir=human_dir))
    assert r_paren2["all_correct"] is True

    r_two = asyncio.run(practice.check_answer_async(conn, settings, "q_two", answer_input="1.5; 2.5", human_dir=human_dir))
    assert r_two["all_correct"] is True

    r_two_lbl = asyncio.run(practice.check_answer_async(conn, settings, "q_two", answer_input="1. 1.5; 2. 2.5", human_dir=human_dir))
    assert r_two_lbl["all_correct"] is True

    r_same = asyncio.run(practice.check_answer_async(conn, settings, "q_same_int", answer_input="1.5; 1.8", human_dir=human_dir))
    assert r_same["all_correct"] is True

    r_zero = asyncio.run(practice.check_answer_async(conn, settings, "q_zero", answer_input="0.5; 1.5", human_dir=human_dir))
    assert r_zero["all_correct"] is True

    # 非法零号标签前缀不崩溃，安全降级位置分配（无 ValueError）
    r_bad_prefix = asyncio.run(practice.check_answer_async(conn, settings, "q_zero", answer_input="0. 5; 2. 1.5", human_dir=human_dir))
    assert r_bad_prefix["all_correct"] is False
    assert r_bad_prefix["parts_correct"] == 1
    conn.close()


def test_b06_f2_symbol_fill_collections_and_empty_part_no_validation_error(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f2.db")
    human_dir = tmp_path / "human"
    settings = Settings()
    settings.pipeline.use_mock_llm = True

    _seed_question(
        conn,
        "q_sym_tuple",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "SYMBOL_FILL",
                "verify_mode": "EXACT",
                "collection_mode": "ORDERED_TUPLE",
                "answer": ">, <, =",
                "items": [">", "<", "="],
            }
        ],
    )
    _seed_question(
        conn,
        "q_sym_map",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "SYMBOL_FILL",
                "verify_mode": "EXACT",
                "collection_mode": "LABELED_MAP",
                "answer": "甲: >, 乙: =",
                "items": [
                    {"label": "甲", "item_type": "SYMBOL_FILL", "canonical": ">"},
                    {"label": "乙", "item_type": "SYMBOL_FILL", "canonical": "="},
                ],
            }
        ],
    )
    _seed_question(
        conn,
        "q_two",
        final_answers=[
            {
                "label": "(1)",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "1.5",
                "sympy_expr": "1.5",
            },
            {
                "label": "(2)",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "2.5",
                "sympy_expr": "2.5",
            },
        ],
    )
    _seed_question(
        conn,
        "q_sym_single",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "SYMBOL_FILL",
                "verify_mode": "EXACT",
                "collection_mode": "SINGLE",
                "answer": ">",
                "canonical": ">",
            }
        ],
    )
    conn.commit()

    r_ok = asyncio.run(practice.check_answer_async(conn, settings, "q_sym_tuple", answer_input=">, <, =", human_dir=human_dir))
    assert r_ok["all_correct"] is True

    r_cn = asyncio.run(practice.check_answer_async(conn, settings, "q_sym_tuple", answer_input="大于，小于，等于", human_dir=human_dir))
    assert r_cn["all_correct"] is True

    r_bad = asyncio.run(practice.check_answer_async(conn, settings, "q_sym_tuple", answer_input="<, >, =", human_dir=human_dir))
    assert r_bad["all_correct"] is False

    r_map_1 = asyncio.run(practice.check_answer_async(conn, settings, "q_sym_map", answer_input="甲: >, 乙: =", human_dir=human_dir))
    assert r_map_1["all_correct"] is True

    r_map_2 = asyncio.run(practice.check_answer_async(conn, settings, "q_sym_map", answer_input="乙==, 甲=>", human_dir=human_dir))
    assert r_map_2["all_correct"] is True

    r_map_pos = asyncio.run(practice.check_answer_async(conn, settings, "q_sym_map", answer_input=">, =", human_dir=human_dir))
    assert r_map_pos["all_correct"] is True

    # 空值不抛 Pydantic ValidationError，计为 missing/incorrect
    r_empty1 = asyncio.run(practice.check_answer_async(conn, settings, "q_two", answer_input="(1): ; (2): 2.5", human_dir=human_dir))
    assert r_empty1["all_correct"] is False
    assert r_empty1["missing_labels"] == ["(1)"]
    assert r_empty1["parts_correct"] == 1

    r_empty2 = asyncio.run(practice.check_answer_async(conn, settings, "q_sym_single", answer_input="主问: ", human_dir=human_dir))
    assert r_empty2["all_correct"] is False
    assert r_empty2["missing_labels"] == ["主问"]
    conn.close()


def test_b06_f3_search_questions_strict_eff_node_ids(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f3.db")
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES ('N_OLD', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '旧算子'), ('N_NEW', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '新算子');"
    )
    _seed_question(conn, "Q_TAX", node_id="N_OLD")
    conn.execute(
        "INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint, taxonomy_version) VALUES ('Q_TAX', 'taxonomy_override_json', ?, 'PI_CLI', 'h_Q_TAX', '1.0');",
        (json.dumps({"mode": "REPLACE", "nodes": [{"node_id": "N_NEW"}]}),),
    )
    effective.recompute(conn, "Q_TAX")
    conn.commit()

    settings = Settings()
    assets_dir = tmp_path / "assets"
    vectors_dir = tmp_path / "vectors"

    res_old = practice.search_questions(
        conn, settings, grade_subject="小学奥数", l3="N_OLD", assets_dir=assets_dir, vectors_dir=vectors_dir
    )
    assert res_old["returned"] == 0

    res_new = practice.search_questions(
        conn, settings, grade_subject="小学奥数", l3="N_NEW", assets_dir=assets_dir, vectors_dir=vectors_dir
    )
    assert [it["question_uid"] for it in res_new["items"]] == ["Q_TAX"]
    conn.close()


def test_b06_f4_log_practice_stuck_step_uses_optimal_branch(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f4.db")
    human_dir = tmp_path / "human"
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES ('N_B1', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', 'B1'), ('N_B2', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', 'B2'), ('N_H1', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', 'H1');"
    )
    _seed_question(
        conn,
        "Q1",
        branches=[
            {"branch_id": "branch_1", "is_optimal": 0, "steps": [(1, "sub_b1", "hint_b1", "N_B1")]},
            {"branch_id": "branch_2", "is_optimal": 1, "steps": [(1, "sub_b2", "hint_b2", "N_B2")]},
        ],
    )
    _seed_question(
        conn,
        "Q2",
        branches=[
            {"branch_id": "human_1", "is_optimal": 1, "steps": [(1, "sub_h1", "hint_h1", "N_H1")]},
        ],
    )
    conn.commit()

    r1 = practice.log_practice(conn, uid_or_id="Q1", outcome="WRONG", stuck_step_index=1, human_dir=human_dir)
    assert r1["stuck_node_id"] == "N_B2"
    assert r1["stuck_sub_goal"] == "sub_b2"

    r2 = practice.log_practice(conn, uid_or_id="Q2", outcome="WRONG", stuck_step_index=1, human_dir=human_dir)
    assert r2["stuck_node_id"] == "N_H1"
    assert r2["stuck_sub_goal"] == "sub_h1"
    conn.close()


def test_b06_f5_log_batch_practice_atomic_rollback_and_single_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _make_db(tmp_path, "b06_f5.db")
    human_dir = tmp_path / "human"
    _seed_question(conn, "Q1")
    _seed_question(conn, "Q2")
    conn.execute(
        "INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json) VALUES ('QB1', 'default', '小学奥数', '{}', '[]');"
    )
    conn.commit()

    export_calls = 0
    orig_export = human_mirror.export_snapshot

    def _spy_export(*args: Any, **kwargs: Any) -> Any:
        nonlocal export_calls
        export_calls += 1
        return orig_export(*args, **kwargs)

    monkeypatch.setattr(human_mirror, "export_snapshot", _spy_export)

    # 中途失败必须零落库、零镜像导出
    with pytest.raises(NotFoundError):
        practice.log_batch_practice(
            conn,
            quiz_batch_id="QB1",
            entries=[
                {"uid": "Q1", "outcome": "SOLVED_SOLO"},
                {"uid": "NONEXISTENT", "outcome": "WRONG"},
            ],
            human_dir=human_dir,
        )
    assert conn.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0] == 0
    assert export_calls == 0

    # 合法批量提交：支持自定义 writer，单事务原子写入，仅导出 1 次快照
    custom_writer = SingleWriterExecutor()
    res = practice.log_batch_practice(
        conn,
        quiz_batch_id="QB1",
        entries=[
            {"uid": "Q1", "outcome": "SOLVED_SOLO"},
            {"uid": "Q2", "outcome": "WRONG", "score_ratio": 0.0},
        ],
        request_id_prefix="batch_req",
        human_dir=human_dir,
        writer=custom_writer,
    )
    assert res["logged_count"] == 2
    assert conn.execute("SELECT COUNT(*) FROM practice_log;").fetchone()[0] == 2
    assert export_calls == 1
    conn.close()


def test_b06_f6_apply_single_override_fingerprints_stale_and_reconfirm_forwarding(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f6.db")
    human_dir = tmp_path / "human"
    _seed_question(conn, "Q_OV")
    conn.commit()

    def _stages() -> dict[str, str]:
        return {
            str(r["stage"]): str(r["status"])
            for r in conn.execute("SELECT stage, status FROM compile_state WHERE question_uid='Q_OV';").fetchall()
        }

    def _reset_ok() -> None:
        conn.execute("UPDATE compile_state SET status = 'OK' WHERE question_uid = 'Q_OV';")
        conn.commit()

    # 1. 标注类（abstract_skeleton / thinking_methods_json）：保留 base_fingerprint=content_hash，taxonomy_version=''，置 S4=STALE
    practice.apply_single_override(
        conn, "Q_OV", field_name="thinking_methods_json", value='["方程法"]', human_dir=human_dir
    )
    ov_ann = conn.execute(
        "SELECT base_fingerprint, taxonomy_version FROM human_overrides WHERE question_uid='Q_OV' AND field_name='thinking_methods_json' AND is_active=1;"
    ).fetchone()
    assert ov_ann["base_fingerprint"] == "h_Q_OV"
    assert ov_ann["taxonomy_version"] == ""
    assert _stages()["S4"] == "STALE" and _stages()["S3D"] == "OK"

    # revoke 标注类也必须传播 S4=STALE
    _reset_ok()
    practice.apply_single_override(
        conn, "Q_OV", field_name="thinking_methods_json", revoke=True, human_dir=human_dir
    )
    assert _stages()["S4"] == "STALE" and _stages()["S3D"] == "OK"

    # 2. 词表类（primary_l1_module）：base_fingerprint=content_hash，taxonomy_version='1.0'，set 与 revoke 均置 S3D/S4=STALE
    _reset_ok()
    practice.apply_single_override(
        conn, "Q_OV", field_name="primary_l1_module", value="数论", human_dir=human_dir
    )
    ov_tax = conn.execute(
        "SELECT base_fingerprint, taxonomy_version FROM human_overrides WHERE question_uid='Q_OV' AND field_name='primary_l1_module' AND is_active=1;"
    ).fetchone()
    assert ov_tax["base_fingerprint"] == "h_Q_OV"
    assert ov_tax["taxonomy_version"] == "1.0"
    assert _stages()["S3D"] == "STALE" and _stages()["S4"] == "STALE" and _stages()["S3C"] == "OK"

    _reset_ok()
    practice.apply_single_override(
        conn, "Q_OV", field_name="primary_l1_module", revoke=True, human_dir=human_dir
    )
    assert _stages()["S3D"] == "STALE" and _stages()["S4"] == "STALE" and _stages()["S3C"] == "OK"

    # 3. 验真类（verification_status / is_excluded）：taxonomy_version=''；verification_status -> S2..S4 STALE，is_excluded -> S4 STALE
    _reset_ok()
    practice.apply_single_override(
        conn, "Q_OV", field_name="verification_status", value="HUMAN_VERIFIED", human_dir=human_dir
    )
    ov_ver = conn.execute(
        "SELECT base_fingerprint, taxonomy_version FROM human_overrides WHERE question_uid='Q_OV' AND field_name='verification_status' AND is_active=1;"
    ).fetchone()
    assert ov_ver["base_fingerprint"] == "h_Q_OV"
    assert ov_ver["taxonomy_version"] == ""
    assert _stages()["S2"] == "STALE" and _stages()["S3C"] == "STALE" and _stages()["S3D"] == "STALE" and _stages()["S4"] == "STALE"

    _reset_ok()
    practice.apply_single_override(
        conn, "Q_OV", field_name="is_excluded", value="1", human_dir=human_dir
    )
    assert _stages()["S4"] == "STALE" and _stages()["S2"] == "OK"

    # 4. reconfirm 必须透传 reason、source 与 include_practice_log=False
    conn.execute("UPDATE human_overrides SET is_suspended = 1, suspended_reason = 'SOURCE_CHANGED' WHERE question_uid = 'Q_OV' AND field_name = 'verification_status' AND is_active = 1;")
    conn.commit()
    reconfirm_dir = tmp_path / "reconfirm_human"
    practice.apply_single_override(
        conn,
        "Q_OV",
        field_name="verification_status",
        reconfirm=True,
        reason="MY_REASON",
        source="PI_CLI",
        human_dir=reconfirm_dir,
        include_practice_log=False,
    )
    re_row = conn.execute(
        "SELECT reason, source, is_suspended FROM human_overrides WHERE question_uid='Q_OV' AND field_name='verification_status' AND is_active=1;"
    ).fetchone()
    assert re_row["reason"] == "MY_REASON"
    assert re_row["source"] == "PI_CLI"
    assert re_row["is_suspended"] == 0
    assert not (reconfirm_dir / "practice_log.jsonl").exists()
    conn.close()


def test_b06_f7_interval_parentheses_and_ratio_labeled_map(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f7.db")
    human_dir = tmp_path / "human"
    settings = Settings()
    settings.pipeline.use_mock_llm = True

    _seed_question(
        conn,
        "q_interval",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "SET_OR_INTERVAL",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "UNORDERED_MULTISET",
                "answer": "(1, 3)",
                "items": ["(1, 3)"],
            }
        ],
    )
    _seed_question(
        conn,
        "q_ratio_map",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "RATIO",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "LABELED_MAP",
                "answer": "甲=3:4, 乙=1:2",
                "items": [
                    {"label": "甲", "expr": "3/4"},
                    {"label": "乙", "expr": "1/2"},
                ],
            }
        ],
    )
    conn.commit()

    # 单个开区间 "(1, 3)" 不得剥掉圆括号
    r_int = asyncio.run(practice.check_answer_async(conn, settings, "q_interval", answer_input="(1, 3)", human_dir=human_dir))
    assert r_int["all_correct"] is True

    # 花括号包裹的多区间仍正常剥除花括号
    stu_braces, _, _ = practice._parse_student_answer_parts(
        "{(1, 3), [5, 7]}",
        [
            AnswerPart(
                label="主问",
                answer_type="SET_OR_INTERVAL",
                verify_mode="SYMBOLIC",
                collection_mode="UNORDERED_MULTISET",
                answer="(1, 3), [5, 7]",
                items=["(1, 3)", "[5, 7]"],
            )
        ],
    )
    assert stu_braces[0].items == [{"expr": "(1, 3)"}, {"expr": "[5, 7]"}]

    # RATIO 位置作答 "3:4, 1:2" 与显式键值均应判对
    r_rat_pos = asyncio.run(practice.check_answer_async(conn, settings, "q_ratio_map", answer_input="3:4, 1:2", human_dir=human_dir))
    assert r_rat_pos["all_correct"] is True

    r_rat_eq = asyncio.run(practice.check_answer_async(conn, settings, "q_ratio_map", answer_input="甲=3:4, 乙=1:2", human_dir=human_dir))
    assert r_rat_eq["all_correct"] is True

    r_rat_col = asyncio.run(practice.check_answer_async(conn, settings, "q_ratio_map", answer_input="甲:3:4, 乙:1:2", human_dir=human_dir))
    assert r_rat_col["all_correct"] is True

    r_rat_fullwidth_eq = asyncio.run(practice.check_answer_async(conn, settings, "q_ratio_map", answer_input="甲＝3:4，乙＝1:2", human_dir=human_dir))
    assert r_rat_fullwidth_eq["all_correct"] is True
    conn.close()


def test_b06_f8_log_practice_validation_blank_request_id_and_check_replay(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f8.db")
    human_dir = tmp_path / "human"
    settings = Settings()
    settings.pipeline.use_mock_llm = True
    _seed_question(conn, "Q1")
    conn.commit()

    # 空串 / 纯空白 request_id 归一为 NULL，连续两次不触发 UNIQUE 冲突
    l1 = practice.log_practice(conn, uid_or_id="Q1", outcome="WRONG", request_id="", human_dir=human_dir)
    l2 = practice.log_practice(conn, uid_or_id="Q1", outcome="WRONG", request_id="   ", human_dir=human_dir)
    assert l1["client_request_id"] is None and l2["client_request_id"] is None
    assert l1["log_id"] != l2["log_id"]

    # 负 hint_level_used 与越界 score_ratio 均前置抛 PreconditionError
    with pytest.raises(PreconditionError):
        practice.log_practice(conn, uid_or_id="Q1", outcome="SOLVED_SOLO", hint_level_used=-1, human_dir=human_dir)
    with pytest.raises(PreconditionError):
        practice.log_practice(conn, uid_or_id="Q1", outcome="WRONG", score_ratio=-0.1, human_dir=human_dir)
    with pytest.raises(PreconditionError):
        practice.log_practice(conn, uid_or_id="Q1", outcome="WRONG", score_ratio=1.2, human_dir=human_dir)

    # check_answer_async 幂等重放回填已落库的真实 outcome 与 score_ratio
    c1 = asyncio.run(
        practice.check_answer_async(
            conn, settings, "Q1", answer_input="42", log=True, request_id="req_dup", human_dir=human_dir
        )
    )
    c2 = asyncio.run(
        practice.check_answer_async(
            conn, settings, "Q1", answer_input="99", log=True, request_id="req_dup", human_dir=human_dir
        )
    )
    assert c1["log_id"] == c2["log_id"]
    assert c2["outcome"] == "SOLVED_SOLO"
    assert c2["score_ratio"] == 1.0
    conn.close()


def test_b06_f9_search_questions_semantic_multi_scope_grouping(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f9.db")
    conn.execute(
        """
        INSERT INTO grade_subjects(grade_subject, slug, display_name, subject_family, taxonomy_status, taxonomy_version)
        VALUES ('高中物理', 'high_physics', '高中物理', 'PHYSICS', 'LOCKED', '1.0');
        """
    )
    _seed_question(conn, "Q_MATH", grade_subject="小学奥数", text="三角形等高面积计算", elo_override=1120)
    _seed_question(conn, "Q_PHYS", grade_subject="高中物理", text="牛顿第二定律受力分析", l1="力学", l2="动力学", l3_op="牛顿第二定律", node_id="kn_phys_1", elo_override=1500)
    conn.commit()

    vec_dir = tmp_path / "vectors"
    assets_dir = tmp_path / "assets"
    store = VectorStore(vec_dir)
    settings = Settings()
    settings.pipeline.use_mock_llm = True

    # 写入两个学段的 skeleton 向量（维度与 mock embedding 一致：通过 EmbeddingClient 生成或直接用 mock 向量）
    from shuati.llm.embeddings import EmbeddingClient

    async def _mk_vecs() -> np.ndarray:
        client = EmbeddingClient(settings, conn=None, use_mock=True)
        try:
            return await client.embed_texts(["三角形等高面积计算", "牛顿第二定律受力分析"], stage="TEST")
        finally:
            await client.aclose()

    vecs = asyncio.run(_mk_vecs())
    store._save_npz(store.npz_path("skeleton", "primary_olympiad"), ["Q_MATH"], ["hm"], vecs[0:1])
    store._save_npz(store.npz_path("skeleton", "high_physics"), ["Q_PHYS"], ["hp"], vecs[1:2])

    # 不传 grade_subject，FTS 过滤后仅剩高中物理题，语义重排不得误查 primary_olympiad 导致 0 召回
    res_phys = practice.search_questions(
        conn,
        settings,
        query="牛顿第二定律",
        semantic="牛顿第二定律",
        vectors_dir=vec_dir,
        assets_dir=assets_dir,
        use_mock=True,
    )
    assert res_phys["returned"] == 1
    assert res_phys["items"][0]["question_uid"] == "Q_PHYS"
    assert "semantic_similarity" in res_phys["items"][0]

    # 不传 grade_subject 且无 FTS 过滤时，跨学段归并排序两个学段结果
    res_all = practice.search_questions(
        conn,
        settings,
        semantic="牛顿第二定律受力分析",
        vectors_dir=vec_dir,
        assets_dir=assets_dir,
        use_mock=True,
    )
    assert res_all["returned"] == 2
    assert res_all["items"][0]["question_uid"] == "Q_PHYS"
    conn.close()


def test_b06_f10_qualitative_judge_receives_grade_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _make_db(tmp_path, "b06_f10.db")
    human_dir = tmp_path / "human"
    conn.execute(
        """
        INSERT INTO grade_subjects(grade_subject, slug, display_name, subject_family, taxonomy_status, taxonomy_version)
        VALUES ('高中物理', 'high_physics', '高中物理', 'PHYSICS', 'LOCKED', '1.0');
        """
    )
    _seed_question(
        conn,
        "Q_PHYS_QUAL",
        grade_subject="高中物理",
        text="请解释失重现象",
        l1="力学",
        l2="运动学",
        l3_op="失重",
        node_id="kn_phys_q",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "TEXT",
                "verify_mode": "QUALITATIVE",
                "collection_mode": "SINGLE",
                "answer": "加速度向下导致视重小于实重",
            }
        ],
    )
    conn.commit()

    captured: dict[str, Any] = {}
    orig_builder = practice.build_qualitative_judge_messages

    def _spy_builder(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return orig_builder(*args, **kwargs)

    monkeypatch.setattr(practice, "build_qualitative_judge_messages", _spy_builder)

    settings = Settings()
    settings.pipeline.use_mock_llm = True
    asyncio.run(
        practice.check_answer_async(
            conn,
            settings,
            "Q_PHYS_QUAL",
            answer_input="物体具有向下的加速度时支持力小于重力",
            human_dir=human_dir,
            use_mock=True,
        )
    )
    assert captured.get("grade_subject") == "高中物理"
    conn.close()


def test_b06_f11_hydrate_concepts_and_browse_taxonomy_canonical_redirect(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "b06_f11.db")
    assets_dir = tmp_path / "assets"

    conn.execute(
        "INSERT OR IGNORE INTO concepts(concept_id, kind, name, aliases_json, redirect_to_id, is_seed) VALUES ('C_MASTER', 'TRAP', '主陷阱', '[]', NULL, 0);"
    )
    conn.execute(
        "INSERT OR IGNORE INTO concepts(concept_id, kind, name, aliases_json, redirect_to_id, is_seed) VALUES ('C_SUB', 'TRAP', '旧陷阱', '[]', 'C_MASTER', 0);"
    )
    conn.execute(
        """
        INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator, redirect_to_id)
        VALUES
          ('N_NEW', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '新算子', NULL),
          ('N_SUB', '小学奥数', '1.0', 'L3', '平面几何', '等高模型', '旧算子', 'N_NEW');
        """
    )

    _seed_question(conn, "Q1", node_id="N_NEW", traps=["C_SUB", "C_MASTER"])
    _seed_question(conn, "Q2", node_id="N_NEW")
    # 模拟重定向前残留的 eff_node_ids_json（含重定向子节点及主/子并存）
    with effective.authorized_effective_writer():
        conn.execute(
            "UPDATE questions SET eff_node_ids_json = '[\"N_SUB\"]' WHERE question_uid = 'Q1';"
        )
        conn.execute(
            "UPDATE questions SET eff_node_ids_json = '[\"N_SUB\", \"N_NEW\"]' WHERE question_uid = 'Q2';"
        )
    conn.commit()

    hydrated = practice.hydrate_question_dict(conn, "Q1", assets_dir=assets_dir)
    assert hydrated["trap_tags"] == ["主陷阱"]

    tree = practice.browse_taxonomy(conn, grade_subject="小学奥数")
    l3_ops = tree["modules"][0]["l2_entities"][0]["l3_operators"]
    by_id = {op["node_id"]: op for op in l3_ops}
    assert "N_NEW" in by_id
    assert by_id["N_NEW"]["question_count"] == 2
    conn.close()


def test_round2_repro_regressions_and_edge_cases(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, "r2_edge.db")
    human_dir = tmp_path / "human"
    settings = Settings()
    settings.pipeline.use_mock_llm = True

    # R1: 合法带标签符号填空 '(1) *; (2) -' 不被二元算术保护误杀，而 '(2) * (3); 5' 正常按数学式判对
    _seed_question(
        conn,
        "Q_SYM_2P",
        final_answers=[
            {
                "label": "(1)",
                "answer_type": "SYMBOL_FILL",
                "verify_mode": "EXACT",
                "collection_mode": "SINGLE",
                "answer": "*",
                "canonical": "*",
            },
            {
                "label": "(2)",
                "answer_type": "SYMBOL_FILL",
                "verify_mode": "EXACT",
                "collection_mode": "SINGLE",
                "answer": "-",
                "canonical": "-",
            },
        ],
    )
    # R2: SYMBOL_FILL UNORDERED_MULTISET + fullwidth '＝' in LABELED_MAP
    _seed_question(
        conn,
        "Q_SYM_MS",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "SYMBOL_FILL",
                "verify_mode": "EXACT",
                "collection_mode": "UNORDERED_MULTISET",
                "answer": ">, <",
                "items": [">", "<"],
            }
        ],
    )
    _seed_question(
        conn,
        "Q_SYM_MAP_FW",
        final_answers=[
            {
                "label": "主问",
                "answer_type": "SYMBOL_FILL",
                "verify_mode": "EXACT",
                "collection_mode": "LABELED_MAP",
                "answer": "甲: >, 乙: =",
                "items": [
                    {"label": "甲", "item_type": "SYMBOL_FILL", "canonical": ">"},
                    {"label": "乙", "item_type": "SYMBOL_FILL", "canonical": "="},
                ],
            }
        ],
    )
    conn.commit()

    r_sym2p = asyncio.run(
        practice.check_answer_async(conn, settings, "Q_SYM_2P", answer_input="(1) *; (2) -", human_dir=human_dir)
    )
    assert r_sym2p["all_correct"] is True

    r_ms_ok = asyncio.run(
        practice.check_answer_async(conn, settings, "Q_SYM_MS", answer_input="<, >", human_dir=human_dir)
    )
    assert r_ms_ok["all_correct"] is True

    r_ms_bad = asyncio.run(
        practice.check_answer_async(conn, settings, "Q_SYM_MS", answer_input="<, <", human_dir=human_dir)
    )
    assert r_ms_bad["all_correct"] is False

    r_fw = asyncio.run(
        practice.check_answer_async(conn, settings, "Q_SYM_MAP_FW", answer_input="甲＝>，乙＝＝", human_dir=human_dir)
    )
    assert r_fw["all_correct"] is True
    conn.close()
