"""P3-C：每日盲测调度器测试（配额熔断 / 幂等锁 / 防剧透 / 双重核验 / 连带销账 / 衰减）。

规格：docs/designs/adaptive_practice_harness.md §3.3 第 2 条、CUJ 3、Target File #12；
契约：docs/tasks/P3-C_blind_drill.md。

约束：USE_MOCK_LLM=True 全程零真实网络；题目/步骤/会话/托管池直接 SQL 构造，
Elo 兑现与标红边走真实 elo_engine / knowledge_graph；答案等价判定走真实
sympy_verifier.verify_equation_equivalence（隔离子进程，无网络）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.services import elo_engine, knowledge_graph
from backend.services.blind_drill_scheduler import (
    BlindDrillItemDTO,
    BlindDrillOutcomeDTO,
    evaluate_blind_drill_submission,
    get_daily_blind_drill_queue,
)

MODULE = "电磁学"
DAY = date(2026, 11, 12)
UNLOCK_DEFAULT = DAY - timedelta(days=1)
DUE_DEFAULT = DAY + timedelta(days=3)

PROBE_MD = "仅根据入射角与磁场宽度 d，轨迹半径 R 与 d 满足什么几何等量关系？"
INTERMEDIATE_STD = "R = 2*d"
INTERMEDIATE_RIGHT = "R/2 = d"  # 常数比率等价（e1/e2 = 1/2）
INTERMEDIATE_WRONG = "R = d"
FINAL_RIGHT = "x = 2"
FINAL_WRONG = "x = 9"


# ---------------------------------------------------------------------------
# 夹具与 SQL 构造工具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "USE_MOCK_LLM", True)


@pytest.fixture()
def conn(tmp_db: Path) -> Iterator[sqlite3.Connection]:
    with get_db_connection(tmp_db) as c:
        yield c


def _insert_question(
    conn: sqlite3.Connection,
    uid: str,
    *,
    module: str = MODULE,
    l_score: int = 2,
    base_elo: int = 2085,
    stem: str | None = None,
    answer: str = FINAL_RIGHT,
    origin: str = "USER_CANONICAL",
    mastery: str = "PENDING_RETEST",
    unlock: date | None = UNLOCK_DEFAULT,
    due: date | None = DUE_DEFAULT,
    exposure: int = 0,
    parent: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO questions (question_uid, origin_type, parent_question_uid,"
        " target_grade_and_subject, stem_md, stem_hash, standard_answer_latex,"
        " c_score, s_score, t_score, l_score, base_elo, tier, primary_l1_module,"
        " mastery_state, blind_test_unlock_at, blind_test_due_at, slated_exposure_count)"
        " VALUES (?, ?, ?, '高中物理-高二', ?, ?, ?, 2, 2, 2, ?, ?, 'L3_综合中档', ?, ?, ?, ?, ?)",
        (
            uid, origin, parent,
            stem if stem is not None else f"题干 {uid}：求 x。",
            f"hash-{uid}", answer, l_score, base_elo, module, mastery,
            unlock.isoformat() if unlock is not None else None,
            due.isoformat() if due is not None else None,
            exposure,
        ),
    )


def _insert_node(conn: sqlite3.Connection, nid: str) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (node_id, l1_module, l2_entity, l3_operator)"
        " VALUES (?, ?, ?, ?)",
        (nid, MODULE, f"实体-{nid}", f"算子-{nid}"),
    )


def _insert_step(
    conn: sqlite3.Connection,
    uid: str,
    step_id: str,
    *,
    index: int,
    formula: str,
    probe: str,
    node_id: str,
    blocked: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO question_steps (step_id, question_uid, step_index, step_goal_desc,"
        " step_formula_latex, knowledge_node_id, masked_conditions_json, probe_question_md,"
        " is_blocked_step, c_sub, s_sub, t_sub, l_sub, sub_elo)"
        " VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, 1, 1, 1, 1, 900)",
        (step_id, uid, index, f"步骤{index}子目标", formula, node_id, probe, blocked),
    )


def _insert_session(
    conn: sqlite3.Connection,
    uid: str,
    sid: str,
    *,
    stalled: int = 2,
    category: str = "CONCEPT_BOTTLENECK",
) -> None:
    conn.execute(
        "INSERT INTO diagnostic_sessions (session_id, question_uid,"
        " draft_image_cas_uris_json, draft_steps_latex_json, matched_branch_id,"
        " last_valid_step_index, stalled_step_index, error_category,"
        " user_confirmed_gate, micro_stage)"
        " VALUES (?, ?, '[]', '[]', 'canonical', ?, ?, ?, 1, 'SESSION_COMPLETED')",
        (sid, uid, stalled - 1, stalled, category),
    )


def _insert_pool(
    conn: sqlite3.Connection,
    uid: str,
    *,
    escrow: float = 20.0,
    decay: float = 1.0,
    module: str = MODULE,
) -> None:
    conn.execute(
        "INSERT INTO question_escrow_pool (question_uid, l1_module,"
        " initial_combat_elo_snapshot, floor_elo_applied, escrow_combat_delta,"
        " is_cov_settled, decay_factor, escrow_status)"
        " VALUES (?, ?, 1200.0, 0.0, ?, 1, ?, 'FROZEN_PENDING_BLIND')",
        (uid, module, escrow, decay),
    )


def _insert_relation(
    conn: sqlite3.Connection, src: str, tgt: str, rtype: str, *, containment: float = 0.9
) -> None:
    conn.execute(
        "INSERT INTO question_relations (source_question_uid, target_question_uid,"
        " relation_type, is_directed, surface_sim, skeleton_sim, containment_score,"
        " trap_overlap, confidence_source)"
        " VALUES (?, ?, ?, ?, 0.5, 0.8, ?, 0.2, 'HUMAN_VERIFIED')",
        (src, tgt, rtype, 1 if rtype == "SCAFFOLD_PRE" else 0, containment),
    )


def _prepare_original(
    conn: sqlite3.Connection,
    uid: str,
    *,
    l_score: int = 2,
    answer: str = FINAL_RIGHT,
    category: str = "CONCEPT_BOTTLENECK",
    stem: str | None = None,
    exposure: int = 0,
    unlock: date | None = UNLOCK_DEFAULT,
    escrow: float = 20.0,
    decay: float = 1.0,
) -> None:
    """完整可盲测原题：题目 + 双节点双步骤（Step2 卡壳）+ 原会话 + 冻结托管池。"""
    _insert_question(
        conn, uid, l_score=l_score, answer=answer, stem=stem, exposure=exposure, unlock=unlock
    )
    _insert_node(conn, f"n-{uid}-1")
    _insert_node(conn, f"n-{uid}-2")
    _insert_step(
        conn, uid, f"s-{uid}-1", index=1, formula="q*v*B = m*v^2/R",
        probe="步骤1反问句", node_id=f"n-{uid}-1", blocked=0,
    )
    _insert_step(
        conn, uid, f"s-{uid}-2", index=2, formula=INTERMEDIATE_STD,
        probe=PROBE_MD, node_id=f"n-{uid}-2", blocked=1,
    )
    _insert_session(conn, uid, f"sess-{uid}", stalled=2, category=category)
    _insert_pool(conn, uid, escrow=escrow, decay=decay)


def _insert_variant(
    conn: sqlite3.Connection, vid: str, orig_uid: str, *, answer: str = "x = 5"
) -> None:
    _insert_question(
        conn, vid, origin="AI_SYNTHETIC_VARIANT", parent=orig_uid,
        stem=f"换数同构变式 {vid}：求 x。", answer=answer, mastery="ACTIVE",
        unlock=None, due=None,
    )


def _row(conn: sqlite3.Connection, sql: str, *params: object) -> sqlite3.Row:
    cur = conn.execute(sql, params)
    cur.row_factory = sqlite3.Row
    row = cur.fetchone()
    assert row is not None
    return row


def _question(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    return _row(conn, "SELECT * FROM questions WHERE question_uid = ?", uid)


def _pool(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    return _row(conn, "SELECT * FROM question_escrow_pool WHERE question_uid = ?", uid)


def _attempt(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    return _row(
        conn,
        "SELECT * FROM blind_drill_attempts WHERE question_uid = ? ORDER BY attempt_id DESC",
        uid,
    )


def _combat(conn: sqlite3.Connection, module: str = MODULE) -> float:
    return elo_engine.get_module_elo(conn, module).combat_elo


def _submit(
    conn: sqlite3.Connection,
    uid: str,
    final: str = FINAL_RIGHT,
    inter: str | None = INTERMEDIATE_RIGHT,
) -> BlindDrillOutcomeDTO:
    """默认双正确提交的短包装（active_date 固定 DAY）。"""
    return evaluate_blind_drill_submission(conn, uid, final, inter, DAY)


def _expose_variant(conn: sqlite3.Connection, orig_uid: str, vid: str) -> None:
    """手工作答日志：标记某变式已曝光过（未曝光判据的数据来源）。"""
    conn.execute(
        "INSERT INTO blind_drill_attempts (question_uid, drill_question_uid, active_date,"
        " intermediate_correct, final_correct, passed)"
        " VALUES (?, ?, ?, NULL, 1, 1)",
        (orig_uid, vid, (DAY - timedelta(days=1)).isoformat()),
    )


# ---------------------------------------------------------------------------
# Schema 扩展（PM 批准）：blind_drill_attempts 表 + 索引 + 外键
# ---------------------------------------------------------------------------


def test_blind_drill_attempts_table_and_index_exist(tmp_db: Path) -> None:
    with get_db_connection(tmp_db) as c:
        tables = {
            r["name"]
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        indexes = {
            r["name"]
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "blind_drill_attempts" in tables
        assert "idx_blind_drill_attempts_question_date" in indexes
        _insert_question(c, "q-fk")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO blind_drill_attempts (question_uid, drill_question_uid,"
                " active_date, final_correct, passed) VALUES ('no-such', 'q-fk', ?, 1, 1)",
                (DAY.isoformat(),),
            )


# ---------------------------------------------------------------------------
# 幂等锁与跨日重新锁定
# ---------------------------------------------------------------------------


def test_same_day_idempotent_queue_and_exposure(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q1", unlock=DAY - timedelta(days=2))
    _prepare_original(conn, "q2", unlock=DAY - timedelta(days=1))
    first = get_daily_blind_drill_queue(conn, DAY)
    assert [i.question_uid for i in first] == ["q1", "q2"]
    for uid in ("q1", "q2"):
        row = _question(conn, uid)
        assert row["slated_drill_date"] == DAY.isoformat()
        assert row["slated_exposure_count"] == 1
    second = get_daily_blind_drill_queue(conn, DAY)
    assert second == first  # 幂等返回锁定题单
    for uid in ("q1", "q2"):
        assert _question(conn, uid)["slated_exposure_count"] == 1  # 曝光不重复递增


def test_cross_day_relock_increments_exposure(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q1")
    get_daily_blind_drill_queue(conn, DAY)
    next_day = DAY + timedelta(days=1)
    queue = get_daily_blind_drill_queue(conn, next_day)
    assert [i.question_uid for i in queue] == ["q1"]
    row = _question(conn, "q1")
    assert row["slated_drill_date"] == next_day.isoformat()
    assert row["slated_exposure_count"] == 2


def test_answered_items_leave_queue_without_relock(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q1", unlock=DAY - timedelta(days=3))
    _prepare_original(conn, "q2", unlock=DAY - timedelta(days=2))
    _prepare_original(conn, "q3", unlock=DAY - timedelta(days=1))
    assert len(get_daily_blind_drill_queue(conn, DAY)) == 3
    outcome = evaluate_blind_drill_submission(conn, "q1", FINAL_RIGHT, INTERMEDIATE_RIGHT, DAY)
    assert outcome.passed
    queue = get_daily_blind_drill_queue(conn, DAY)
    assert [i.question_uid for i in queue] == ["q2", "q3"]  # 已销账离队，不拉新题补位
    assert _question(conn, "q2")["slated_exposure_count"] == 1
    assert _question(conn, "q3")["slated_drill_date"] == DAY.isoformat()


def test_empty_queue_when_nothing_due(conn: sqlite3.Connection) -> None:
    assert get_daily_blind_drill_queue(conn, DAY) == []
    _prepare_original(conn, "q-future", unlock=DAY + timedelta(days=1))
    assert get_daily_blind_drill_queue(conn, DAY) == []
    assert _question(conn, "q-future")["slated_drill_date"] is None


# ---------------------------------------------------------------------------
# 配额熔断：溢出顺延单日 + unlock ASC 排序
# ---------------------------------------------------------------------------


def test_quota_overflow_defers_single_day(conn: sqlite3.Connection) -> None:
    for i in range(4):
        _prepare_original(conn, f"q{i + 1}", unlock=DAY - timedelta(days=4 - i))
    queue = get_daily_blind_drill_queue(conn, DAY)  # 缺省 cap=3
    assert [i.question_uid for i in queue] == ["q1", "q2", "q3"]  # unlock ASC
    deferred = _question(conn, "q4")
    assert deferred["slated_drill_date"] is None
    assert deferred["slated_exposure_count"] == 0
    assert deferred["blind_test_unlock_at"] == DAY.isoformat()  # 原 unlock=DAY-1，顺延 +1 天
    assert deferred["blind_test_due_at"] == (DUE_DEFAULT + timedelta(days=1)).isoformat()
    assert deferred["system_deferred_days"] == 1
    # 次日：前三题已销账（模拟），顺延题 unlock 到期入单
    for uid in ("q1", "q2", "q3"):
        conn.execute(
            "UPDATE questions SET mastery_state='MASTERED' WHERE question_uid=?", (uid,)
        )
    next_queue = get_daily_blind_drill_queue(conn, DAY + timedelta(days=1))
    assert [i.question_uid for i in next_queue] == ["q4"]
    assert _question(conn, "q4")["system_deferred_days"] == 1  # 未重复顺延


def test_limit_override(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q1", unlock=DAY - timedelta(days=2))
    _prepare_original(conn, "q2", unlock=DAY - timedelta(days=1))
    queue = get_daily_blind_drill_queue(conn, DAY, limit=1)
    assert [i.question_uid for i in queue] == ["q1"]
    assert _question(conn, "q2")["system_deferred_days"] == 1
    with pytest.raises(ValueError, match="配额"):
        get_daily_blind_drill_queue(conn, DAY, limit=-1)


# ---------------------------------------------------------------------------
# 防剧透：DTO 无泄漏字段 + 题干剥标签
# ---------------------------------------------------------------------------


def test_dto_strips_spoilers_and_has_no_leaky_fields(conn: sqlite3.Connection) -> None:
    spoiler_stem = (
        "---\nquestion_uid: q-spoiler\ntarget_grade_and_subject: 高中物理-高二\n---\n\n"
        "[L1] 电磁学 / [L2] 带电粒子在有界磁场中的运动 / [L3] 放缩圆与临界相切构造\n"
        "【来源】2026 届某校月考压轴题\n"
        "带电粒子以速度 v 垂直射入宽度为 d 的条形磁场，求轨迹半径 R。"
    )
    _prepare_original(conn, "q-spoiler", stem=spoiler_stem)
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert set(BlindDrillItemDTO.model_fields) == {
        "question_uid", "drill_question_uid", "stem_md", "verification_mode",
        "allowed_symbols", "probe_question_md", "requires_intermediate",
    }
    for name in BlindDrillItemDTO.model_fields:
        assert not any(
            tok in name.lower() for tok in ("module", "parent", "path", "l1", "l2", "l3")
        )
    assert "电磁学" not in item.stem_md
    assert "[L1]" not in item.stem_md and "[L2]" not in item.stem_md and "[L3]" not in item.stem_md
    assert "来源" not in item.stem_md and "月考" not in item.stem_md
    assert "q-spoiler" not in item.stem_md and "question_uid" not in item.stem_md
    assert "条形磁场" in item.stem_md  # 正常题干内容保留


def test_equation_skeleton_mode_and_symbols(conn: sqlite3.Connection) -> None:
    stem = (
        "粒子电荷 q、质量 m、速度 v 垂直射入磁感应强度 B 的磁场，"
        "宽度 d，入射角 \\theta，求半径 R。"
    )
    _prepare_original(conn, "q-skel", l_score=2, stem=stem)
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.verification_mode == "equation_skeleton"
    assert item.allowed_symbols  # 非空
    for sym in ("R", "d", "q", "m", "v", "B", "theta"):
        assert sym in item.allowed_symbols
    assert item.allowed_symbols == sorted(set(item.allowed_symbols))


def test_final_answer_mode_for_l1(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-l1", l_score=1)
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.verification_mode == "final_answer"
    assert item.allowed_symbols == []


# ---------------------------------------------------------------------------
# 变式优先 / 未曝光判据 / 原题回退携带 probe
# ---------------------------------------------------------------------------


def test_variant_preferred_and_unexposed_criterion(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-orig")
    _insert_variant(conn, "v-a", "q-orig")
    _insert_variant(conn, "v-b", "q-orig")
    _insert_relation(conn, "q-orig", "v-a", "CLONE")
    _insert_relation(conn, "v-b", "q-orig", "ISOMORPHIC")  # 反向边亦可检索
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.drill_question_uid == "v-a"  # CLONE 优先且未曝光
    assert not item.requires_intermediate and item.probe_question_md is None
    assert "换数同构变式 v-a" in item.stem_md
    _expose_variant(conn, "q-orig", "v-a")
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.drill_question_uid == "v-b"  # 已曝光变式被跳过
    _expose_variant(conn, "q-orig", "v-b")
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.drill_question_uid == "q-orig"  # 全部曝光 → 回退原题
    assert item.requires_intermediate and item.probe_question_md == PROBE_MD


def test_fallback_original_carries_probe(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-solo")
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.drill_question_uid == "q-solo"
    assert item.requires_intermediate is True
    assert item.probe_question_md == PROBE_MD


# ---------------------------------------------------------------------------
# 双重核验：原题中间量 + 最终答案；变式只核最终
# ---------------------------------------------------------------------------


def test_original_both_correct_passes(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-pass")
    get_daily_blind_drill_queue(conn, DAY)
    outcome = evaluate_blind_drill_submission(conn, "q-pass", FINAL_RIGHT, INTERMEDIATE_RIGHT, DAY)
    assert isinstance(outcome, BlindDrillOutcomeDTO)
    assert outcome.passed and outcome.mastery_state == "MASTERED"
    assert outcome.retrain_stage is None and outcome.cascaded_question_uids == []
    assert outcome.explanation_text
    assert _question(conn, "q-pass")["mastery_state"] == "MASTERED"
    log = _attempt(conn, "q-pass")
    assert log["drill_question_uid"] == "q-pass"
    assert log["intermediate_correct"] == 1 and log["final_correct"] == 1 and log["passed"] == 1
    assert log["active_date"] == DAY.isoformat()


def test_original_intermediate_wrong_final_right_fails(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-bad-inter")
    outcome = _submit(conn, "q-bad-inter", inter=INTERMEDIATE_WRONG)
    assert not outcome.passed and outcome.mastery_state == "ACTIVE"
    assert outcome.retrain_stage == "STAGE_1_BLIND_PROBE"
    log = _attempt(conn, "q-bad-inter")
    assert log["intermediate_correct"] == 0 and log["final_correct"] == 1 and log["passed"] == 0
    assert _pool(conn, "q-bad-inter")["escrow_status"] == "FORFEITED_RESET"
    assert _combat(conn) == pytest.approx(settings.MODULE_ELO_INITIAL)  # 基线分文不动


def test_original_intermediate_missing_fails(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-missing")
    outcome = evaluate_blind_drill_submission(conn, "q-missing", FINAL_RIGHT, None, DAY)
    assert not outcome.passed and outcome.retrain_stage == "STAGE_1_BLIND_PROBE"
    assert _attempt(conn, "q-missing")["intermediate_correct"] == 0


def test_original_intermediate_right_final_wrong_stage4(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-stage4", category="TRAP_OR_CALC_ERROR")
    outcome = _submit(conn, "q-stage4", final=FINAL_WRONG)
    assert not outcome.passed and outcome.mastery_state == "ACTIVE"
    assert outcome.retrain_stage == "STAGE_4_DECRUTCH_VARIANT"
    assert _attempt(conn, "q-stage4")["intermediate_correct"] == 1
    retrain = _row(
        conn,
        "SELECT * FROM diagnostic_sessions WHERE question_uid = ? AND session_id != ?",
        "q-stage4", "sess-q-stage4",
    )
    assert retrain["micro_stage"] == "STAGE_4_DECRUTCH_VARIANT"
    assert retrain["error_category"] == "TRAP_OR_CALC_ERROR"  # 沿用原会话
    assert retrain["stalled_step_index"] == 2
    count = conn.execute(
        "SELECT COUNT(*) FROM diagnostic_sessions WHERE question_uid = ?", ("q-stage4",)
    ).fetchone()[0]
    assert count == 2  # 恰好新建一条重训会话


def test_variant_pass_final_only_and_replay_matches_queue(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-var")
    _insert_variant(conn, "v-1", "q-var", answer="x = 5")
    _insert_relation(conn, "q-var", "v-1", "CLONE")
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.drill_question_uid == "v-1"
    outcome = evaluate_blind_drill_submission(conn, "q-var", "x = 5", None, DAY)
    assert outcome.passed and outcome.mastery_state == "MASTERED"
    log = _attempt(conn, "q-var")
    assert log["drill_question_uid"] == "v-1"  # 提交重放与排单选择一致
    assert log["intermediate_correct"] is None  # 变式不要求中间量
    assert _pool(conn, "q-var")["escrow_status"] == "REALIZED_MASTERED"
    assert _combat(conn) == pytest.approx(settings.MODULE_ELO_INITIAL + 20.0)


def test_variant_wrong_final_fails_stage1(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-var-fail")
    _insert_variant(conn, "v-1", "q-var-fail", answer="x = 5")
    _insert_relation(conn, "q-var-fail", "v-1", "ISOMORPHIC")
    get_daily_blind_drill_queue(conn, DAY)
    # 背原题答案无效：呈现题是变式，核验对象为变式标准答案
    outcome = evaluate_blind_drill_submission(conn, "q-var-fail", FINAL_RIGHT, None, DAY)
    assert not outcome.passed and outcome.retrain_stage == "STAGE_1_BLIND_PROBE"
    assert _question(conn, "q-var-fail")["mastery_state"] == "ACTIVE"
    assert _pool(conn, "q-var-fail")["escrow_combat_delta"] == 0.0


# ---------------------------------------------------------------------------
# pass 销账：escrow 兑现（含衰减系数）+ 标红边递减 + 全流程守恒
# ---------------------------------------------------------------------------


def test_pass_realizes_escrow_with_decay_and_decrements_edges(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-decay-pass", escrow=20.0, decay=0.85)
    knowledge_graph.record_co_error_edges(conn, "q-decay-pass")
    edge = _row(conn, "SELECT * FROM concept_co_errors")
    assert edge["active_error_count"] == 1
    outcome = evaluate_blind_drill_submission(
        conn, "q-decay-pass", FINAL_RIGHT, INTERMEDIATE_RIGHT, DAY
    )
    assert outcome.passed
    assert _combat(conn) == pytest.approx(settings.MODULE_ELO_INITIAL + 20.0 * 0.85)
    state = elo_engine.get_module_elo(conn, MODULE)
    assert state.combat_elo <= state.coverage_elo
    pool = _pool(conn, "q-decay-pass")
    assert pool["escrow_status"] == "REALIZED_MASTERED" and pool["escrow_combat_delta"] == 0.0
    edge = _row(conn, "SELECT * FROM concept_co_errors")
    assert edge["active_error_count"] == 0  # 标红边 active 递减
    assert edge["total_historical_count"] == 1  # 历史溯源不动


def test_full_flow_conservation_floor_plus_escrow(conn: sqlite3.Connection) -> None:
    """草稿保底 → 拆拐杖冻结 → 盲测兑现：累计实战分增量与直接独立做对严格守恒。"""
    uid, sid = "q-conserve", "sess-q-conserve"
    _insert_question(conn, uid, mastery="ACTIVE", unlock=None, due=None)
    _insert_node(conn, "n-c1")
    _insert_node(conn, "n-c2")
    _insert_step(conn, uid, "s-c1", index=1, formula="q*v*B = m*v^2/R",
                 probe="步骤1反问句", node_id="n-c1", blocked=0)
    _insert_step(conn, uid, "s-c2", index=2, formula=INTERMEDIATE_STD,
                 probe=PROBE_MD, node_id="n-c2", blocked=1)
    _insert_session(conn, uid, sid, stalled=2)
    floor = elo_engine.apply_draft_subgoal_floor(conn, uid, sid, 2085 - 150)
    assert floor.applied
    freeze = elo_engine.freeze_decrutch_escrow(conn, uid, sid)
    assert freeze.applied
    conn.execute(
        "UPDATE questions SET mastery_state='PENDING_RETEST', blind_test_unlock_at=?,"
        " blind_test_due_at=? WHERE question_uid=?",
        (UNLOCK_DEFAULT.isoformat(), DUE_DEFAULT.isoformat(), uid),
    )
    assert [i.question_uid for i in get_daily_blind_drill_queue(conn, DAY)] == [uid]
    outcome = evaluate_blind_drill_submission(conn, uid, FINAL_RIGHT, INTERMEDIATE_RIGHT, DAY)
    assert outcome.passed
    target = settings.K_COMBAT * (1.0 - elo_engine.expected_pass(settings.MODULE_ELO_INITIAL, 2085))
    assert _combat(conn) - settings.MODULE_ELO_INITIAL == pytest.approx(target)
    elo_engine.replay_module_elo_from_ledger(conn, MODULE)  # 账本链完整可重放


# ---------------------------------------------------------------------------
# 上位母题连带销账
# ---------------------------------------------------------------------------


def test_cascade_high_containment_pending_child(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-parent", escrow=20.0)
    _prepare_original(conn, "q-child", escrow=10.0, decay=0.9)
    _insert_relation(conn, "q-child", "q-parent", "SCAFFOLD_PRE", containment=0.9)
    outcome = _submit(conn, "q-parent")
    assert outcome.passed
    assert outcome.cascaded_question_uids == ["q-child"]
    assert _question(conn, "q-child")["mastery_state"] == "MASTERED"
    child_pool = _pool(conn, "q-child")
    assert child_pool["escrow_status"] == "REALIZED_MASTERED"  # 各自 escrow 独立兑现
    assert _combat(conn) == pytest.approx(settings.MODULE_ELO_INITIAL + 20.0 + 10.0 * 0.9)
    assert "连带销账" in (outcome.explanation_text or "")


def test_no_cascade_low_containment(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-parent")
    _prepare_original(conn, "q-child")
    _insert_relation(conn, "q-child", "q-parent", "SCAFFOLD_PRE", containment=0.7)
    outcome = _submit(conn, "q-parent")
    assert outcome.passed and outcome.cascaded_question_uids == []
    assert _question(conn, "q-child")["mastery_state"] == "PENDING_RETEST"
    assert _pool(conn, "q-child")["escrow_status"] == "FROZEN_PENDING_BLIND"


@pytest.mark.parametrize("child_state", ["ACTIVE", "MASTERED"])
def test_no_cascade_non_pending_child(conn: sqlite3.Connection, child_state: str) -> None:
    _prepare_original(conn, "q-parent")
    _prepare_original(conn, "q-child")
    conn.execute(
        "UPDATE questions SET mastery_state=? WHERE question_uid=?", (child_state, "q-child")
    )
    _insert_relation(conn, "q-child", "q-parent", "SCAFFOLD_PRE", containment=0.95)
    outcome = _submit(conn, "q-parent")
    assert outcome.passed and outcome.cascaded_question_uids == []
    assert _question(conn, "q-child")["mastery_state"] == child_state


def test_cascade_boundary_containment_exact_085(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-parent")
    _prepare_original(conn, "q-child")
    _insert_relation(conn, "q-child", "q-parent", "SCAFFOLD_PRE", containment=0.85)
    outcome = _submit(conn, "q-parent")
    assert outcome.cascaded_question_uids == ["q-child"]  # I >= 0.85 含等号


# ---------------------------------------------------------------------------
# 衰减接线：当日 slated 且曝光 > 3 才衰减；顺延题冻结；同日幂等
# ---------------------------------------------------------------------------


def test_decay_applies_to_overexposed_slated_only(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-hot", exposure=3, unlock=DAY - timedelta(days=5))
    _prepare_original(conn, "q-cold", exposure=0, unlock=DAY - timedelta(days=4))
    _prepare_original(conn, "q-warm", exposure=3, unlock=DAY - timedelta(days=3))
    _prepare_original(conn, "q-deferred", exposure=3, unlock=DAY - timedelta(days=2))
    queue = get_daily_blind_drill_queue(conn, DAY)
    assert [i.question_uid for i in queue] == ["q-hot", "q-cold", "q-warm"]
    assert _pool(conn, "q-hot")["decay_factor"] == pytest.approx(0.85)  # 曝光 4 > 3
    assert _pool(conn, "q-warm")["decay_factor"] == pytest.approx(0.85)
    assert _pool(conn, "q-cold")["decay_factor"] == pytest.approx(1.0)  # 曝光 1 不衰减
    assert _pool(conn, "q-deferred")["decay_factor"] == pytest.approx(1.0)  # 顺延冻结
    assert _question(conn, "q-deferred")["system_deferred_days"] == 1
    assert _combat(conn) == pytest.approx(settings.MODULE_ELO_INITIAL)  # 衰减不动基线
    get_daily_blind_drill_queue(conn, DAY)  # 同日二次刷新
    assert _pool(conn, "q-hot")["decay_factor"] == pytest.approx(0.85)  # 不重复衰减
    events = conn.execute(
        "SELECT COUNT(*) FROM elo_mutation_ledger WHERE mutation_trigger='ESCROW_TIMEOUT_DECAY'"
    ).fetchone()[0]
    assert events == 2


# ---------------------------------------------------------------------------
# 入口守卫与单事务原子性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["ACTIVE", "MASTERED"])
def test_evaluate_rejects_non_pending(conn: sqlite3.Connection, state: str) -> None:
    _prepare_original(conn, "q-guard")
    conn.execute("UPDATE questions SET mastery_state=? WHERE question_uid=?", (state, "q-guard"))
    with pytest.raises(ValueError, match="PENDING_RETEST"):
        evaluate_blind_drill_submission(conn, "q-guard", FINAL_RIGHT, INTERMEDIATE_RIGHT, DAY)


def test_evaluate_unknown_question_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="不存在"):
        evaluate_blind_drill_submission(conn, "q-ghost", FINAL_RIGHT, None, DAY)


def test_submission_atomic_rollback_on_error(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_original(conn, "q-atomic")

    def _boom(conn: sqlite3.Connection, question_uid: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(knowledge_graph, "decrement_co_error_edges_on_mastery", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        evaluate_blind_drill_submission(conn, "q-atomic", FINAL_RIGHT, INTERMEDIATE_RIGHT, DAY)
    # 全程单事务：作答日志 / mastery / 托管池 / 账本全部回滚
    assert _question(conn, "q-atomic")["mastery_state"] == "PENDING_RETEST"
    assert conn.execute(
        "SELECT COUNT(*) FROM blind_drill_attempts WHERE question_uid='q-atomic'"
    ).fetchone()[0] == 0
    pool = _pool(conn, "q-atomic")
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND" and pool["escrow_combat_delta"] == 20.0
    assert conn.execute("SELECT COUNT(*) FROM elo_mutation_ledger").fetchone()[0] == 0


def test_dto_models_are_pydantic() -> None:
    assert issubclass(BlindDrillItemDTO, BaseModel)
    assert issubclass(BlindDrillOutcomeDTO, BaseModel)


# ---------------------------------------------------------------------------
# 防御路径：会话分支不匹配回退 / 无原会话的 SQL 构造题
# ---------------------------------------------------------------------------


def test_stalled_step_branch_fallback(conn: sqlite3.Connection) -> None:
    _prepare_original(conn, "q-branch")
    conn.execute(
        "UPDATE diagnostic_sessions SET matched_branch_id='alt-branch'"
        " WHERE question_uid='q-branch'"
    )
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.probe_question_md == PROBE_MD  # 回退 canonical 分支取卡壳步
    assert _submit(conn, "q-branch").passed


def test_missing_session_defaults(conn: sqlite3.Connection) -> None:
    _insert_question(conn, "q-nosess")  # 无会话/无步骤的 SQL 构造题
    _insert_pool(conn, "q-nosess")
    item = get_daily_blind_drill_queue(conn, DAY)[0]
    assert item.drill_question_uid == "q-nosess"
    assert item.requires_intermediate is True and item.probe_question_md is None
    outcome = evaluate_blind_drill_submission(conn, "q-nosess", FINAL_WRONG, None, DAY)
    assert not outcome.passed and outcome.retrain_stage == "STAGE_1_BLIND_PROBE"
    retrain = _row(conn, "SELECT * FROM diagnostic_sessions WHERE question_uid='q-nosess'")
    assert retrain["error_category"] == "CONCEPT_BOTTLENECK"  # 无原会话时的默认诊断
    assert retrain["micro_stage"] == "STAGE_1_BLIND_PROBE"
