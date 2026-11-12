"""P3-A：三层知识网 + 同域可逆并查集 Compaction + 双指标专属测试。

规格：docs/designs/adaptive_practice_harness.md §3.4 全节、CUJ 4、Target File #17、
Alternative 2A/2B；任务契约 docs/tasks/P3-A_knowledge_graph.md 测试要求逐条覆盖。

约束：USE_MOCK_LLM=True 全程零真实网络；题目/步骤行直接 SQL 构造（不依赖
ingest_service）；tmp_db 夹具隔离；Elo 账本零变化逐字段断言。
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.services import knowledge_graph as kg
from backend.services import llm_gateway, llm_mocks
from backend.services.knowledge_graph import (
    CanonicalNodeResolver,
    GraphMetricsDTO,
    NodeJudgeResult,
    compute_graph_metrics,
    decrement_co_error_edges_on_mastery,
    match_or_create_l3_node,
    node_state,
    record_co_error_edges,
    revert_compaction_batch,
    run_scoped_compaction,
)

L1 = "电磁学"
L2 = "电磁感应单双杆模型"
L1_OTHER = "函数与导数"
L2_OTHER = "函数极值"

# 三个近义算子（Jaccard: sim(B,C) > sim(A,B) > sim(A,C) > 0，保证先合 C->B 再合 B->A）
NAME_A = "动量定理求电荷量"
NAME_B = "动量定理求电荷量大小与位移"
NAME_C = "动量定理求电荷量大小与位移法"

# 互不共享任何 2/3-gram 的孤立名称（Jaccard 恒 0）
DISJOINT_NAMES = [
    "甲子", "乙丑", "丙寅", "丁卯", "戊辰", "己巳", "庚午", "辛未",
    "壬申", "癸酉", "戌亥", "子戌", "丑亥", "寅亥", "卯戌",
]


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


class JudgeSpy:
    """记录 node_judge 调用载荷并按 decide(payload) 返回命中 ID（None=未命中）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.decide: Any = lambda payload: None

    async def __call__(
        self,
        messages: list[dict[str, Any]],
        schema: type[Any],
        *,
        model: str,
        purpose: str,
        temperature: float | None = None,
    ) -> NodeJudgeResult:
        assert purpose == "node_judge"
        assert schema is NodeJudgeResult
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        return NodeJudgeResult(match_node_id=self.decide(payload), evidence="spy 判定")

    @property
    def match_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["task"] == "match_or_create"]

    @property
    def compaction_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["task"] == "scoped_compaction"]


@pytest.fixture()
def judge(monkeypatch: pytest.MonkeyPatch) -> JudgeSpy:
    spy = JudgeSpy()
    monkeypatch.setattr(llm_gateway, "chat_json", spy)
    return spy


def _confirm_compaction(payload: dict[str, Any]) -> str | None:
    """compaction 对一律确认命中主节点；match 请求一律未命中。"""
    if payload["task"] == "scoped_compaction":
        return str(payload["existing_nodes"][0]["node_id"])
    return None


def _insert_node(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    l1: str = L1,
    l2: str = L2,
    l3: str | None = None,
    aliases: tuple[str, ...] = (),
    redirect: str | None = None,
    created_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (node_id, l1_module, l2_entity, l3_operator,"
        " aliases_json, redirect_to_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
        (
            node_id,
            l1,
            l2,
            l3 if l3 is not None else f"算子-{node_id}",
            json.dumps(list(aliases), ensure_ascii=False),
            redirect,
            created_at,
        ),
    )


def _insert_question(
    conn: sqlite3.Connection,
    uid: str,
    *,
    module: str = L1,
    origin: str = "USER_CANONICAL",
    mastery: str = "ACTIVE",
    created_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO questions (question_uid, origin_type, target_grade_and_subject,"
        " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
        " base_elo, tier, primary_l1_module, mastery_state, created_at)"
        " VALUES (?, ?, '高中物理-高二', '题干', ?, '42', 2, 2, 2, 2, 1500,"
        " 'L3_综合中档', ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
        (uid, origin, f"hash-{uid}", module, mastery, created_at),
    )


def _insert_step(
    conn: sqlite3.Connection,
    step_id: str,
    uid: str,
    node_id: str,
    *,
    blocked: int = 0,
    reused: int = 0,
    index: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO question_steps (step_id, question_uid, step_index, step_goal_desc,"
        " step_formula_latex, knowledge_node_id, masked_conditions_json, probe_question_md,"
        " is_blocked_step, reused_existing_node, c_sub, s_sub, t_sub, l_sub, sub_elo)"
        " VALUES (?, ?, ?, '子目标', 'x=1', ?, '[]', '探针?', ?, ?, 1, 1, 1, 1, 1000)",
        (step_id, uid, index, node_id, blocked, reused),
    )


def _insert_edge(
    conn: sqlite3.Connection,
    u: str,
    v: str,
    *,
    active: int = 1,
    historical: int = 1,
    uids: tuple[str, ...] = ("q-default",),
    last_error_at: str = "2026-01-01 00:00:00",
) -> None:
    assert u < v
    mu = conn.execute(
        "SELECT l1_module FROM knowledge_nodes WHERE node_id = ?", (u,)
    ).fetchone()[0]
    mv = conn.execute(
        "SELECT l1_module FROM knowledge_nodes WHERE node_id = ?", (v,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO concept_co_errors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (u, v, mu, mv, active, historical, json.dumps(list(uids), ensure_ascii=False),
         last_error_at),
    )


def _dump(conn: sqlite3.Connection, table: str, order: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def _snapshot_all(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    return {
        "knowledge_nodes": _dump(conn, "knowledge_nodes", "node_id"),
        "concept_co_errors": _dump(conn, "concept_co_errors", "node_u_id, node_v_id"),
        "question_steps": _dump(conn, "question_steps", "step_id"),
        "questions": _dump(conn, "questions", "question_uid"),
        "elo_mutation_ledger": _dump(conn, "elo_mutation_ledger", "event_id"),
        "question_escrow_pool": _dump(conn, "question_escrow_pool", "question_uid"),
    }


def _redirects(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {
        str(r["node_id"]): r["redirect_to_id"]
        for r in conn.execute("SELECT node_id, redirect_to_id FROM knowledge_nodes")
    }


def _assert_hops_le_one(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT node_id, redirect_to_id FROM knowledge_nodes WHERE redirect_to_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        target = conn.execute(
            "SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = ?",
            (row["redirect_to_id"],),
        ).fetchone()
        assert target is not None, f"{row['node_id']} 指向不存在节点"
        assert target[0] is None, f"{row['node_id']} 跳数 > 1"


def _seed_elo_rows(conn: sqlite3.Connection) -> None:
    """直接 SQL 构造 Elo 账本与托管池行（不依赖 elo_engine），用于零变化断言。"""
    conn.execute(
        "INSERT INTO elo_mutation_ledger (idempotency_key, l1_module, question_uid,"
        " mutation_trigger, eval_difficulty_elo, watermark_gamma, coverage_elo_before,"
        " coverage_elo_after, combat_elo_before, combat_elo_after, explanation_text)"
        " VALUES ('q1:s1:MASTERED_INIT', ?, 'q1', 'MASTERED_INIT', 1500, 1.0,"
        " 1200.0, 1216.0, 1200.0, 1216.0, '测试账本行')",
        (L1,),
    )
    conn.execute(
        "INSERT INTO question_escrow_pool (question_uid, l1_module,"
        " initial_combat_elo_snapshot, floor_elo_applied, escrow_combat_delta,"
        " is_cov_settled, escrow_status)"
        " VALUES ('q1', ?, 1200.0, 8.0, 16.0, 1, 'FROZEN_PENDING_BLIND')",
        (L1,),
    )


def _build_merge_scenario(conn: sqlite3.Connection) -> None:
    """CUJ 4 富场景：A/B/C 近义（先合 C->B 再合 B->A），X 同模块异 L2，D 跨模块。"""
    _insert_node(conn, "A", l3=NAME_A, aliases=("主别名",), created_at="2026-01-01 00:00:01")
    _insert_node(conn, "B", l3=NAME_B, aliases=("B别名",), created_at="2026-01-01 00:00:02")
    _insert_node(conn, "C", l3=NAME_C, created_at="2026-01-01 00:00:03")
    _insert_node(conn, "X", l2=L2_OTHER, l3="安培力冲量近似")
    _insert_node(conn, "D", l1=L1_OTHER, l2=L2_OTHER, l3="洛必达求极限")
    for uid in ("q1", "q2", "q3", "q4"):
        _insert_question(conn, uid)
    _insert_question(conn, "q5", module=L1_OTHER)
    _insert_step(conn, "s1", "q1", "A", blocked=1)
    _insert_step(conn, "s2", "q2", "B", blocked=1)
    _insert_step(conn, "s3", "q3", "X", blocked=1)
    _insert_step(conn, "s4", "q4", "C", blocked=1)
    _insert_step(conn, "s5", "q5", "D", blocked=1)
    _insert_edge(conn, "A", "B", uids=("q5",))
    _insert_edge(conn, "A", "X", active=2, historical=5, uids=("q1", "q2"),
                 last_error_at="2026-01-02 00:00:00")
    _insert_edge(conn, "B", "C", uids=("q2",))
    _insert_edge(conn, "B", "X", active=1, historical=2, uids=("q3",),
                 last_error_at="2026-01-03 00:00:00")
    _insert_edge(conn, "C", "D", uids=("q5",))
    _insert_edge(conn, "C", "X", uids=("q4",))
    _seed_elo_rows(conn)


# ---------------------------------------------------------------------------
# CanonicalNodeResolver
# ---------------------------------------------------------------------------


def test_resolver_active_returns_self(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    assert CanonicalNodeResolver(conn).resolve("A") == "A"


def test_resolver_follows_redirect_chain(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B", redirect="A")
    _insert_node(conn, "C", redirect="B")
    assert CanonicalNodeResolver(conn).resolve("C") == "A"


def test_resolver_unknown_node_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        CanonicalNodeResolver(conn).resolve("ghost")


def test_resolver_cycle_raises(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B")
    conn.execute("UPDATE knowledge_nodes SET redirect_to_id = 'B' WHERE node_id = 'A'")
    conn.execute("UPDATE knowledge_nodes SET redirect_to_id = 'A' WHERE node_id = 'B'")
    with pytest.raises(ValueError, match="环"):
        CanonicalNodeResolver(conn).resolve("A")


# ---------------------------------------------------------------------------
# match_or_create_l3_node：召回 / 复用 / 新建
# ---------------------------------------------------------------------------


async def test_first_node_created_without_judge(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    node_id, reused = await match_or_create_l3_node(conn, L1, L2, "全新算子", ["别名1"])
    assert reused is False
    assert judge.calls == []
    row = conn.execute(
        "SELECT * FROM knowledge_nodes WHERE node_id = ?", (node_id,)
    ).fetchone()
    assert row["l1_module"] == L1 and row["l2_entity"] == L2
    assert row["redirect_to_id"] is None
    assert json.loads(row["aliases_json"]) == ["别名1"]


async def test_default_mock_fixture_routes_and_creates_new(conn: sqlite3.Connection) -> None:
    """不打桩：走真实网关 Mock 路由，验证追加的 node_judge fixture 可用且保守未命中。"""
    _insert_node(conn, "A", l3=NAME_A)
    node_id, reused = await match_or_create_l3_node(conn, L1, L2, NAME_B, [])
    assert reused is False and node_id != "A"
    assert conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 2


async def test_reuse_merges_aliases_dedup_and_excludes_primary(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3="主算子", aliases=("旧别名",))
    judge.decide = lambda payload: "A"
    node_id, reused = await match_or_create_l3_node(
        conn, L1, L2, "新名称", ["旧别名", "另一别名"]
    )
    assert (node_id, reused) == ("A", True)
    aliases = json.loads(
        conn.execute("SELECT aliases_json FROM knowledge_nodes WHERE node_id='A'").fetchone()[0]
    )
    assert aliases == ["旧别名", "新名称", "另一别名"]
    assert conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 1


async def test_reuse_candidate_equal_primary_not_aliased(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3="主算子")
    judge.decide = lambda payload: "A"
    _, reused = await match_or_create_l3_node(conn, L1, L2, "主算子", [])
    assert reused is True
    assert json.loads(
        conn.execute("SELECT aliases_json FROM knowledge_nodes WHERE node_id='A'").fetchone()[0]
    ) == []


async def test_reuse_alias_cap_8(conn: sqlite3.Connection, judge: JudgeSpy) -> None:
    full = tuple(f"别名{i}" for i in range(settings.MAX_ALIASES_PER_NODE))
    _insert_node(conn, "A", aliases=full)
    judge.decide = lambda payload: "A"
    _, reused = await match_or_create_l3_node(conn, L1, L2, "溢出新名称", [])
    assert reused is True
    aliases = json.loads(
        conn.execute("SELECT aliases_json FROM knowledge_nodes WHERE node_id='A'").fetchone()[0]
    )
    assert aliases == list(full)  # 上限 8：原别名保全，不溢出


async def test_judge_hit_outside_candidates_creates_new(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A")
    judge.decide = lambda payload: "ghost-not-sent"
    node_id, reused = await match_or_create_l3_node(conn, L1, L2, "新算子", [])
    assert reused is False and node_id != "A"


async def test_judge_hit_cross_scope_creates_new(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """恶意裁判返回异 L2 节点：硬拒绝复用，绝不跨域锚定。"""
    _insert_node(conn, "A")
    _insert_node(conn, "E", l2=L2_OTHER, l3=NAME_A)
    judge.decide = lambda payload: "E"
    node_id, reused = await match_or_create_l3_node(conn, L1, L2, NAME_A, [])
    assert reused is False and node_id not in ("A", "E")
    assert _redirects(conn) == {"A": None, "E": None, node_id: None}


async def test_reuse_revalidates_match_still_active(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """裁判命中后节点被并发合并失活：防御性复核降级为新建。"""
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "R")

    def decide(payload: dict[str, Any]) -> str:
        conn.execute("UPDATE knowledge_nodes SET redirect_to_id='R' WHERE node_id='A'")
        return "A"

    judge.decide = decide
    node_id, reused = await match_or_create_l3_node(conn, L1, L2, NAME_B, [])
    assert reused is False and node_id not in ("A", "R")


async def test_recall_25_nodes_all_sent(conn: sqlite3.Connection, judge: JudgeSpy) -> None:
    for i in range(settings.L3_LOCAL_RECALL_TRIGGER_COUNT):
        _insert_node(conn, f"n{i:02d}", l3=f"动量定理电荷量算子{i:02d}")
    _, reused = await match_or_create_l3_node(conn, L1, L2, "动量定理电荷量", [])
    assert reused is False
    assert len(judge.match_calls) == 1
    assert len(judge.match_calls[0]["existing_nodes"]) == 25


async def test_recall_30_nodes_jaccard_top15(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    high_ids = {f"hi{i:02d}" for i in range(15)}
    for i in range(15):
        _insert_node(conn, f"hi{i:02d}", l3=f"动量定理电荷量计算变体{i:02d}")
    for i in range(15):
        _insert_node(conn, f"lo{i:02d}", l3=f"无关算子{i:02d}")
    await match_or_create_l3_node(conn, L1, L2, "动量定理电荷量计算", [])
    assert len(judge.match_calls) == 1
    sent = judge.match_calls[0]["existing_nodes"]
    assert len(sent) == settings.L3_TOP_K_CANDIDATES == 15
    assert {n["node_id"] for n in sent} == high_ids


async def test_recall_thresholds_respect_settings(
    conn: sqlite3.Connection, judge: JudgeSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "L3_LOCAL_RECALL_TRIGGER_COUNT", 10)
    monkeypatch.setattr(settings, "L3_TOP_K_CANDIDATES", 5)
    for i in range(12):
        _insert_node(conn, f"n{i:02d}", l3=f"动量定理电荷量变体{i:02d}")
    await match_or_create_l3_node(conn, L1, L2, "动量定理电荷量", [])
    assert len(judge.match_calls[0]["existing_nodes"]) == 5


# ---------------------------------------------------------------------------
# 阈值触发自动 Compaction
# ---------------------------------------------------------------------------


async def test_auto_compaction_triggers_at_15(
    conn: sqlite3.Connection, judge: JudgeSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    original = kg.run_scoped_compaction

    def wrapped(c: sqlite3.Connection, l1: str, l2: str) -> str | None:
        calls.append((l1, l2))
        return original(c, l1, l2)

    monkeypatch.setattr(kg, "run_scoped_compaction", wrapped)
    for name in DISJOINT_NAMES[:14]:
        await match_or_create_l3_node(conn, L1, L2, name, [])
    assert calls == []
    await match_or_create_l3_node(conn, L1, L2, DISJOINT_NAMES[14], [])
    assert calls == [(L1, L2)]


async def test_threshold_counts_only_since_last_batch(
    conn: sqlite3.Connection, judge: JudgeSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        kg, "run_scoped_compaction", lambda c, l1, l2: calls.append((l1, l2)) or None
    )
    for i in range(10):
        _insert_node(conn, f"old-{i}", l3=DISJOINT_NAMES[i], created_at="1999-01-01 00:00:00")
    conn.execute(
        "INSERT INTO compaction_audit_ledger (batch_id, l1_module, l2_entity,"
        " from_node_id, to_node_id, ai_judge_evidence, previous_aliases_to_json,"
        " affected_child_node_ids_json, previous_co_errors_snapshot_json, created_at)"
        " VALUES ('old-batch', ?, ?, 'old-0', 'old-1', '证据', '[]', '[]', '[]',"
        " '2000-01-01 00:00:00')",
        (L1, L2),
    )
    for name in DISJOINT_NAMES[:14]:
        await match_or_create_l3_node(conn, L1, L2, f"新{name}", [])
    assert calls == []  # 旧批次前的 10 个节点不计入
    await match_or_create_l3_node(conn, L1, L2, "新寅亥", [])
    assert calls == [(L1, L2)]


async def test_auto_compaction_merges_similar_pair(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """新建满 15 自动 compaction，Mock 裁判确认近义对 -> 真实合并 + 审计。"""
    judge.decide = _confirm_compaction
    for name in DISJOINT_NAMES[:13]:
        await match_or_create_l3_node(conn, L1, L2, name, [])
    older_id, _ = await match_or_create_l3_node(conn, L1, L2, NAME_A, [])
    assert conn.execute("SELECT COUNT(*) FROM compaction_audit_ledger").fetchone()[0] == 0
    newer_id, _ = await match_or_create_l3_node(conn, L1, L2, NAME_B, [])
    # 同秒创建时 (created_at, node_id) 排序决定主从方向，按 DB 排序断言
    ordered = conn.execute(
        "SELECT node_id FROM knowledge_nodes WHERE node_id IN (?, ?)"
        " ORDER BY created_at, node_id",
        (older_id, newer_id),
    ).fetchall()
    expected_to, expected_from = str(ordered[0][0]), str(ordered[1][0])
    assert _redirects(conn)[expected_from] == expected_to
    _assert_hops_le_one(conn)
    ledger = conn.execute("SELECT * FROM compaction_audit_ledger").fetchall()
    assert len(ledger) == 1
    assert ledger[0]["from_node_id"] == expected_from
    assert ledger[0]["to_node_id"] == expected_to
    assert ledger[0]["ai_judge_evidence"] == "spy 判定"


# ---------------------------------------------------------------------------
# run_scoped_compaction：作用域硬约束 / 路径压缩
# ---------------------------------------------------------------------------


def test_compaction_empty_or_single_returns_none(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    assert run_scoped_compaction(conn, L1, L2) is None
    _insert_node(conn, "A")
    assert run_scoped_compaction(conn, L1, L2) is None
    assert judge.calls == []


def test_compaction_zero_jaccard_skips_judge(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3="甲子")
    _insert_node(conn, "B", l3="乙丑")
    assert run_scoped_compaction(conn, L1, L2) is None
    assert judge.compaction_calls == []


def test_compaction_judge_reject_no_merge(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "B", l3=NAME_B)
    assert run_scoped_compaction(conn, L1, L2) is None
    assert len(judge.compaction_calls) == 1
    assert _redirects(conn) == {"A": None, "B": None}
    assert conn.execute("SELECT COUNT(*) FROM compaction_audit_ledger").fetchone()[0] == 0


def test_compaction_cross_l1_judge_hard_rejected(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """恶意裁判返回跨 L1 节点 ID：硬拒绝，零重定向、零审计。"""
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "B", l3=NAME_B)
    _insert_node(conn, "E", l1=L1_OTHER, l3=NAME_A)
    judge.decide = lambda payload: "E"
    assert run_scoped_compaction(conn, L1, L2) is None
    assert _redirects(conn) == {"A": None, "B": None, "E": None}
    assert conn.execute("SELECT COUNT(*) FROM compaction_audit_ledger").fetchone()[0] == 0


def test_compaction_cross_l2_judge_hard_rejected(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "B", l3=NAME_B)
    _insert_node(conn, "E", l2=L2_OTHER, l3=NAME_A)
    judge.decide = lambda payload: "E"
    assert run_scoped_compaction(conn, L1, L2) is None
    assert _redirects(conn) == {"A": None, "B": None, "E": None}


def test_compaction_scope_isolation(conn: sqlite3.Connection, judge: JudgeSpy) -> None:
    """同名近义节点分属不同 L2：各自作用域内均无可合并对。"""
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "E", l2=L2_OTHER, l3=NAME_B)
    judge.decide = _confirm_compaction
    assert run_scoped_compaction(conn, L1, L2) is None
    assert run_scoped_compaction(conn, L1, L2_OTHER) is None
    assert _redirects(conn) == {"A": None, "E": None}


def test_scope_guard_raises_on_cross_scope(conn: sqlite3.Connection) -> None:
    """写入前最后一道硬闸：跨 L1/L2 合并请求直接 ValueError。"""
    _insert_node(conn, "A")
    _insert_node(conn, "E", l1=L1_OTHER)
    with pytest.raises(ValueError, match="严禁跨 L1/L2"):
        kg._assert_same_scope(conn, ["A", "E"], L1, L2)


def test_merge_pair_rejects_inactive_nodes(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B", redirect="A")
    with pytest.raises(ValueError, match="活跃主节点"):
        kg._merge_pair(conn, L1, L2, "B", "A", "batch-x", "证据")


def test_merge_pair_savepoint_atomic_on_failure(
    conn: sqlite3.Connection, judge: JudgeSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单对合并中途失败：SAVEPOINT 回滚，指针/别名/边/审计全部回到合并前。"""
    judge.decide = _confirm_compaction
    _insert_node(conn, "A", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "B", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(conn, "X", l2=L2_OTHER)
    _insert_edge(conn, "B", "X", uids=("q1",))
    before_nodes = _dump(conn, "knowledge_nodes", "node_id")
    before_edges = _dump(conn, "concept_co_errors", "node_u_id, node_v_id")

    def boom(c: sqlite3.Connection, snapshot: list[dict[str, Any]]) -> None:
        raise RuntimeError("边归并中途失败")

    monkeypatch.setattr(kg, "_rewrite_edges_after_merge", boom)
    with pytest.raises(RuntimeError, match="边归并中途失败"):
        run_scoped_compaction(conn, L1, L2)
    assert _dump(conn, "knowledge_nodes", "node_id") == before_nodes
    assert _dump(conn, "concept_co_errors", "node_u_id, node_v_id") == before_edges
    assert conn.execute("SELECT COUNT(*) FROM compaction_audit_ledger").fetchone()[0] == 0


def test_path_compression_two_hop_pointers(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """两对合并 C->B、B->A：二级子节点 C 被路径压缩直接指向 A（跳数 <= 1）。"""
    judge.decide = _confirm_compaction
    _insert_node(conn, "A", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "B", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(conn, "C", l3=NAME_C, created_at="2026-01-01 00:00:03")
    batch_id = run_scoped_compaction(conn, L1, L2)
    assert isinstance(batch_id, str) and batch_id
    assert _redirects(conn) == {"A": None, "B": "A", "C": "A"}
    _assert_hops_le_one(conn)
    resolver = CanonicalNodeResolver(conn)
    assert resolver.resolve("C") == "A" and resolver.resolve("B") == "A"
    ledger = conn.execute(
        "SELECT * FROM compaction_audit_ledger ORDER BY merge_id"
    ).fetchall()
    assert len(ledger) == 2
    assert {r["batch_id"] for r in ledger} == {batch_id}
    assert json.loads(ledger[0]["affected_child_node_ids_json"]) == []
    assert ledger[0]["from_node_id"] == "C" and ledger[0]["to_node_id"] == "B"
    assert json.loads(ledger[1]["affected_child_node_ids_json"]) == ["C"]
    assert ledger[1]["from_node_id"] == "B" and ledger[1]["to_node_id"] == "A"


def test_merge_direction_newer_into_older(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    judge.decide = _confirm_compaction
    _insert_node(conn, "Z-older", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "A-newer", l3=NAME_B, created_at="2026-01-02 00:00:01")
    batch_id = run_scoped_compaction(conn, L1, L2)
    assert batch_id is not None
    assert _redirects(conn) == {"Z-older": None, "A-newer": "Z-older"}


# ---------------------------------------------------------------------------
# 标红边：自环消除 / 重合累加 / u<v / 生命周期
# ---------------------------------------------------------------------------


def test_merge_removes_self_loop(conn: sqlite3.Connection, judge: JudgeSpy) -> None:
    judge.decide = _confirm_compaction
    _insert_node(conn, "A", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "B", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_edge(conn, "A", "B")
    run_scoped_compaction(conn, L1, L2)
    assert conn.execute("SELECT COUNT(*) FROM concept_co_errors").fetchone()[0] == 0


def test_merge_accumulates_overlapping_edges(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    judge.decide = _confirm_compaction
    _insert_node(conn, "A", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "B", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(conn, "X", l2=L2_OTHER)
    _insert_edge(conn, "A", "X", active=2, historical=5, uids=("q1", "q2"),
                 last_error_at="2026-01-02 00:00:00")
    _insert_edge(conn, "B", "X", active=1, historical=2, uids=("q3",),
                 last_error_at="2026-01-03 00:00:00")
    run_scoped_compaction(conn, L1, L2)
    edges = _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    assert len(edges) == 1
    edge = edges[0]
    assert (edge["node_u_id"], edge["node_v_id"]) == ("A", "X")
    assert edge["active_error_count"] == 3
    assert edge["total_historical_count"] == 7
    assert json.loads(edge["source_question_uids_json"]) == ["q1", "q2", "q3"]
    assert edge["last_error_at"] == "2026-01-03 00:00:00"


def test_merge_migrates_edge_to_new_key_preserving_u_lt_v(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    judge.decide = _confirm_compaction
    _insert_node(conn, "a-main", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "z-sub", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(conn, "m-other", l2=L2_OTHER)
    _insert_edge(conn, "m-other", "z-sub", active=2, historical=4, uids=("q9",))
    run_scoped_compaction(conn, L1, L2)
    edges = _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    assert len(edges) == 1
    assert (edges[0]["node_u_id"], edges[0]["node_v_id"]) == ("a-main", "m-other")
    assert edges[0]["active_error_count"] == 2
    assert edges[0]["total_historical_count"] == 4
    assert json.loads(edges[0]["source_question_uids_json"]) == ["q9"]


def test_record_edges_pairwise_u_lt_v(conn: sqlite3.Connection) -> None:
    for nid in ("zeta", "alpha", "mid"):
        _insert_node(conn, nid)
    _insert_question(conn, "q1")
    for i, nid in enumerate(("zeta", "alpha", "mid"), start=1):
        _insert_step(conn, f"s{i}", "q1", nid, index=i)
    record_co_error_edges(conn, "q1")
    edges = _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    assert [(e["node_u_id"], e["node_v_id"]) for e in edges] == [
        ("alpha", "mid"), ("alpha", "zeta"), ("mid", "zeta")
    ]
    for edge in edges:
        assert edge["active_error_count"] == 1
        assert edge["total_historical_count"] == 1
        assert json.loads(edge["source_question_uids_json"]) == ["q1"]
        assert edge["l1_module_u"] == L1 and edge["l1_module_v"] == L1


def test_record_edges_single_node_noop(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_question(conn, "q1")
    _insert_step(conn, "s1", "q1", "A")
    _insert_step(conn, "s2", "q1", "A", index=2)
    record_co_error_edges(conn, "q1")
    assert conn.execute("SELECT COUNT(*) FROM concept_co_errors").fetchone()[0] == 0


def test_record_edges_increment_and_uid_union(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B")
    for uid in ("q1", "q2"):
        _insert_question(conn, uid)
        _insert_step(conn, f"{uid}-s1", uid, "A")
        _insert_step(conn, f"{uid}-s2", uid, "B", index=2)
    record_co_error_edges(conn, "q1")
    record_co_error_edges(conn, "q2")
    record_co_error_edges(conn, "q1")  # 同题重复 record：计数递增，uids 不重复
    edge = conn.execute("SELECT * FROM concept_co_errors").fetchone()
    assert edge["active_error_count"] == 3
    assert edge["total_historical_count"] == 3
    assert json.loads(edge["source_question_uids_json"]) == ["q1", "q2"]


def test_decrement_floor_zero_and_historical_immutable(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B")
    _insert_question(conn, "q1")
    _insert_step(conn, "s1", "q1", "A")
    _insert_step(conn, "s2", "q1", "B", index=2)
    record_co_error_edges(conn, "q1")
    decrement_co_error_edges_on_mastery(conn, "q1")
    decrement_co_error_edges_on_mastery(conn, "q1")  # 下限 0，historical 不减
    edge = conn.execute("SELECT * FROM concept_co_errors").fetchone()
    assert edge["active_error_count"] == 0
    assert edge["total_historical_count"] == 1
    assert json.loads(edge["source_question_uids_json"]) == ["q1"]


def test_record_edges_resolves_redirected_nodes(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B")
    _insert_node(conn, "C", redirect="A")
    _insert_question(conn, "q1")
    _insert_step(conn, "s1", "q1", "C")
    _insert_step(conn, "s2", "q1", "B", index=2)
    record_co_error_edges(conn, "q1")
    edges = _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    assert [(e["node_u_id"], e["node_v_id"]) for e in edges] == [("A", "B")]


def test_record_edges_cross_module_l1_fields(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "D", l1=L1_OTHER, l2=L2_OTHER)
    _insert_question(conn, "q1")
    _insert_step(conn, "s1", "q1", "A")
    _insert_step(conn, "s2", "q1", "D", index=2)
    record_co_error_edges(conn, "q1")
    edge = conn.execute("SELECT * FROM concept_co_errors").fetchone()
    assert (edge["node_u_id"], edge["node_v_id"]) == ("A", "D")
    assert edge["l1_module_u"] == L1 and edge["l1_module_v"] == L1_OTHER


# ---------------------------------------------------------------------------
# node_state：悲观半格派生状态
# ---------------------------------------------------------------------------


def test_state_no_steps_mastered(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    assert node_state(conn, "A") == "MASTERED"


def test_state_prefix_only_steps_stay_mastered(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_question(conn, "q1", mastery="ACTIVE")
    _insert_step(conn, "s1", "q1", "A", blocked=0)
    assert node_state(conn, "A") == "MASTERED"


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        pytest.param(["ACTIVE"], "ACTIVE", id="active"),
        pytest.param(["PENDING_RETEST"], "PENDING_RETEST", id="pending"),
        pytest.param(["MASTERED"], "MASTERED", id="mastered"),
        pytest.param(["PENDING_RETEST", "ACTIVE"], "ACTIVE", id="active-wins"),
        pytest.param(["MASTERED", "PENDING_RETEST"], "PENDING_RETEST", id="pending-wins"),
        pytest.param(["MASTERED", "MASTERED"], "MASTERED", id="all-mastered"),
    ],
)
def test_state_pessimistic_semilattice(
    conn: sqlite3.Connection, states: list[str], expected: str
) -> None:
    _insert_node(conn, "A")
    for i, state in enumerate(states):
        _insert_question(conn, f"q{i}", mastery=state)
        _insert_step(conn, f"s{i}", f"q{i}", "A", blocked=1)
    assert node_state(conn, "A") == expected


def test_state_includes_redirected_children_steps(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "C", redirect="A")
    _insert_question(conn, "q1", mastery="ACTIVE")
    _insert_step(conn, "s1", "q1", "C", blocked=1)
    assert node_state(conn, "A") == "ACTIVE"
    assert node_state(conn, "C") == "ACTIVE"


def test_state_ignores_synthetic_questions(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_question(conn, "q1", origin="AI_SYNTHETIC_VARIANT", mastery="ACTIVE")
    _insert_step(conn, "s1", "q1", "A", blocked=1)
    assert node_state(conn, "A") == "MASTERED"


# ---------------------------------------------------------------------------
# revert：逐字段精确恢复 / 幂等 / Elo 零变化
# ---------------------------------------------------------------------------


def test_revert_exact_field_by_field_restoration(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    judge.decide = _confirm_compaction
    _build_merge_scenario(conn)
    before = _snapshot_all(conn)

    batch_id = run_scoped_compaction(conn, L1, L2)
    assert batch_id is not None
    # 合并确实发生：两跳压缩、自环删除、边归并、别名扩充
    assert _redirects(conn)["B"] == "A" and _redirects(conn)["C"] == "A"
    _assert_hops_le_one(conn)
    merged_edges = {
        (e["node_u_id"], e["node_v_id"]): e
        for e in _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    }
    assert set(merged_edges) == {("A", "X"), ("A", "D")}
    assert merged_edges[("A", "X")]["active_error_count"] == 4
    assert merged_edges[("A", "X")]["total_historical_count"] == 8
    assert json.loads(merged_edges[("A", "X")]["source_question_uids_json"]) == [
        "q1", "q2", "q3", "q4"
    ]
    a_aliases = json.loads(
        conn.execute("SELECT aliases_json FROM knowledge_nodes WHERE node_id='A'").fetchone()[0]
    )
    assert NAME_B in a_aliases and "B别名" in a_aliases and NAME_C in a_aliases
    # 合并期间 Elo 账本即零变化
    assert _dump(conn, "elo_mutation_ledger", "event_id") == before["elo_mutation_ledger"]
    assert _dump(conn, "question_escrow_pool", "question_uid") == before["question_escrow_pool"]

    revert_compaction_batch(conn, batch_id)
    after = _snapshot_all(conn)
    for table, rows in before.items():
        assert after[table] == rows, f"{table} 未逐字段精确恢复"
    ledger = conn.execute(
        "SELECT * FROM compaction_audit_ledger WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    assert len(ledger) == 2
    for row in ledger:
        assert row["is_reverted"] == 1
        assert row["reverted_at"] is not None


def test_revert_idempotent_second_call_noop(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    judge.decide = _confirm_compaction
    _build_merge_scenario(conn)
    batch_id = run_scoped_compaction(conn, L1, L2)
    assert batch_id is not None
    revert_compaction_batch(conn, batch_id)
    snapshot = _snapshot_all(conn)
    audit = _dump(conn, "compaction_audit_ledger", "merge_id")
    revert_compaction_batch(conn, batch_id)  # 幂等：状态与 reverted_at 均不变
    assert _snapshot_all(conn) == snapshot
    assert _dump(conn, "compaction_audit_ledger", "merge_id") == audit


def test_revert_unknown_batch_noop(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    snapshot = _snapshot_all(conn)
    revert_compaction_batch(conn, "no-such-batch")
    assert _snapshot_all(conn) == snapshot


def test_revert_restores_two_hop_child_pointers(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    judge.decide = _confirm_compaction
    _insert_node(conn, "A", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "B", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(conn, "C", l3=NAME_C, redirect="B", created_at="2026-01-01 00:00:03")
    batch_id = run_scoped_compaction(conn, L1, L2)
    assert batch_id is not None
    assert _redirects(conn) == {"A": None, "B": "A", "C": "A"}
    revert_compaction_batch(conn, batch_id)
    # 逆序恢复：B 先解绑，C 指回 B（合并前的两跳形态）
    assert _redirects(conn) == {"A": None, "B": None, "C": "B"}


def test_revert_elo_ledger_zero_change_isolated(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """合并 + 回滚全程：elo_mutation_ledger 与 question_escrow_pool 逐字段零变化。"""
    judge.decide = _confirm_compaction
    _build_merge_scenario(conn)
    elo_before = _dump(conn, "elo_mutation_ledger", "event_id")
    pool_before = _dump(conn, "question_escrow_pool", "question_uid")
    batch_id = run_scoped_compaction(conn, L1, L2)
    assert batch_id is not None
    revert_compaction_batch(conn, batch_id)
    assert _dump(conn, "elo_mutation_ledger", "event_id") == elo_before
    assert _dump(conn, "question_escrow_pool", "question_uid") == pool_before


# ---------------------------------------------------------------------------
# compute_graph_metrics：双指标与冷启动安全边界
# ---------------------------------------------------------------------------


def test_metrics_empty_module_no_nan(conn: sqlite3.Connection) -> None:
    dto = compute_graph_metrics(conn, L1)
    assert isinstance(dto, GraphMetricsDTO)
    assert dto.mastery_rate == 0.0 and dto.convergence == 0.0 and dto.active_nodes == 0
    assert math.isfinite(dto.mastery_rate) and math.isfinite(dto.convergence)


def test_metrics_cold_start_n1_w2(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    for uid in ("q1", "q2"):
        _insert_question(conn, uid, mastery="ACTIVE")
        _insert_step(conn, f"{uid}-s1", uid, "A", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, L1)
    assert dto.active_nodes == 1
    assert dto.mastery_rate == 0.0
    assert dto.convergence == 0.0  # w=2 < 5 -> rho=0；N=1 < 3 -> eta=0
    assert math.isfinite(dto.mastery_rate) and math.isfinite(dto.convergence)


def test_metrics_mastery_rate_mixed_states(conn: sqlite3.Connection) -> None:
    for i in range(1, 7):
        _insert_node(conn, f"n{i}")
    _insert_question(conn, "qm", mastery="MASTERED")
    _insert_step(conn, "sm", "qm", "n1", blocked=1)
    _insert_question(conn, "qp", mastery="PENDING_RETEST")
    _insert_step(conn, "sp", "qp", "n2", blocked=1)
    _insert_question(conn, "qa1", mastery="ACTIVE")
    _insert_step(conn, "sa1", "qa1", "n3", blocked=1)
    _insert_question(conn, "qa2", mastery="ACTIVE")
    _insert_step(conn, "sa2", "qa2", "n4", blocked=1)
    _insert_question(conn, "qpre", mastery="ACTIVE")  # 仅前缀步骤 -> MASTERED
    _insert_step(conn, "spre", "qpre", "n5", blocked=0)
    _insert_question(conn, "qsyn", origin="AI_SYNTHETIC_VARIANT", mastery="ACTIVE")
    _insert_step(conn, "ssyn", "qsyn", "n6", blocked=1)  # 合成题不计入分母
    dto = compute_graph_metrics(conn, L1)
    assert dto.active_nodes == 6
    assert dto.mastery_rate == pytest.approx(50.0)  # (2 + 0.5*1) / 5 * 100
    assert dto.convergence == 0.0  # k_reuse=0


def test_metrics_uniform_distribution_eta_1(conn: sqlite3.Connection) -> None:
    for nid in ("n1", "n2", "n3"):
        _insert_node(conn, nid)
    for i in range(6):
        _insert_question(conn, f"q{i}")
        _insert_step(conn, f"s{i}", f"q{i}", f"n{i % 3 + 1}", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, L1)
    assert dto.convergence == pytest.approx(100.0)  # rho=1, eta=1
    assert dto.mastery_rate == 0.0


def test_metrics_single_point_collapse_eta_0(conn: sqlite3.Connection) -> None:
    for nid in ("n1", "n2", "n3"):
        _insert_node(conn, nid)
    for i in range(6):
        _insert_question(conn, f"q{i}")
        _insert_step(conn, f"s{i}", f"q{i}", "n1", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, L1)
    assert dto.convergence == pytest.approx(40.0)  # rho=1, eta=0 -> 0.4*100
    assert dto.active_nodes == 3


def test_metrics_window_only_recent_20(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    for i in range(25):
        _insert_question(conn, f"q{i:02d}", created_at=f"2026-01-01 00:{i:02d}:00")
        _insert_step(
            conn, f"s{i:02d}", f"q{i:02d}", "A", blocked=1, reused=1 if i < 5 else 0
        )
    dto = compute_graph_metrics(conn, L1)
    assert dto.convergence == 0.0  # 最近 20 道全部未复用，最早 5 道复用不计入


def test_metrics_min_sample_boundary(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    for i in range(4):
        _insert_question(conn, f"q{i}")
        _insert_step(conn, f"s{i}", f"q{i}", "A", blocked=1, reused=1)
    assert compute_graph_metrics(conn, L1).convergence == 0.0  # w=4 < 5
    _insert_question(conn, "q4")
    _insert_step(conn, "s4", "q4", "A", blocked=1, reused=1)
    # w=5, rho=1；N=1 < 3 -> eta=0 -> 40.0
    assert compute_graph_metrics(conn, L1).convergence == pytest.approx(40.0)


def test_metrics_redirected_steps_count_to_parent(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B")
    _insert_node(conn, "D")
    _insert_node(conn, "C", redirect="B")
    mounts = ["A", "A", "C", "C", "D", "D"]
    for i, nid in enumerate(mounts):
        _insert_question(conn, f"q{i}")
        _insert_step(conn, f"s{i}", f"q{i}", nid, blocked=1, reused=1)
    dto = compute_graph_metrics(conn, L1)
    assert dto.active_nodes == 3  # C 已重定向，不计活跃主节点
    assert dto.convergence == pytest.approx(100.0)  # A/B/D 各 2 步均匀分布


def test_metrics_cross_module_steps_excluded(conn: sqlite3.Connection) -> None:
    """跨模块挂载的步骤不计入本模块分母，归属到节点所在模块。"""
    _insert_node(conn, "A")
    _insert_node(conn, "D", l1=L1_OTHER, l2=L2_OTHER)
    _insert_question(conn, "q1", mastery="MASTERED")
    _insert_step(conn, "s1", "q1", "D", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, L1)
    assert dto.mastery_rate == 0.0  # A 无挂载，D 的步骤归属其他模块
    assert dto.active_nodes == 1
    dto_other = compute_graph_metrics(conn, L1_OTHER)
    assert dto_other.mastery_rate == 100.0
    assert dto_other.active_nodes == 1


def test_metrics_synthetic_excluded_from_window(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    for i in range(6):
        _insert_question(conn, f"q{i}", origin="AI_SYNTHETIC_SCAFFOLD")
        _insert_step(conn, f"s{i}", f"q{i}", "A", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, L1)
    assert dto.convergence == 0.0
    assert dto.mastery_rate == 0.0
    assert dto.active_nodes == 1


# ---------------------------------------------------------------------------
# 事务边界与 Mock fixture 完整性
# ---------------------------------------------------------------------------


def test_functions_never_commit(tmp_db: Path) -> None:
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _insert_node(conn, "A")
        _insert_node(conn, "B")
        _insert_question(conn, "q1")
        _insert_step(conn, "s1", "q1", "A")
        _insert_step(conn, "s2", "q1", "B", index=2)
        record_co_error_edges(conn, "q1")
        other = sqlite3.connect(tmp_db)
        try:
            assert other.execute("SELECT COUNT(*) FROM concept_co_errors").fetchone()[0] == 0
        finally:
            other.close()
    finally:
        conn.rollback()
        conn.close()


def test_mock_fixture_registered_and_legacy_purposes_intact() -> None:
    for purpose in ("diagnosis", "scaffold_kernel", "variant_kernel", "solver_blind"):
        assert purpose in llm_mocks.MOCK_RESPONSES
    verdict = NodeJudgeResult.model_validate(llm_mocks.MOCK_RESPONSES["node_judge"])
    assert verdict.match_node_id is None
    assert verdict.evidence
