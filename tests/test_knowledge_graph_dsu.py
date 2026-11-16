"""P3-A：三层知识网 + 同域可逆并查集 Compaction + 双指标专属测试。

规格：docs/designs/adaptive_practice_harness.md §3.4 全节、CUJ 4、Target File #17、
Alternative 2A/2B；任务契约 docs/tasks/P3-A_knowledge_graph.md 测试要求逐条覆盖。

约束：USE_MOCK_LLM=True 全程零真实网络；题目/步骤行直接 SQL 构造（不依赖
ingest_service）；tmp_db 夹具隔离；Elo 账本零变化逐字段断言。

P10-C（docs/tasks/P10-C_graph_grade_scoping.md）预授权机械性期望演进：作用域类
函数调用补 grade 实参（默认 GRADE）；raw SQL 种子节点/边/审计/账本行补
grade_subject；resolver 构造补 grade。既有核心断言（跨 L1/L2 硬拒、两跳回滚
逐字段精确恢复、悲观半格、自环消除、冷启动无 NaN、Elo 正交）一律不动。
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

# P10-C：学段学科分区——默认学段与 _insert_question 的 target_grade_and_subject 一致；
# GRADE_OTHER 用于跨学段隔离测试（真实隐患：同名 L1/L2 在两学段各自长节点）
GRADE = "高中物理-高二"
GRADE_OTHER = "小学奥数"

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
    grade: str = GRADE,  # P10-C：种子节点补 grade_subject
    l1: str = L1,
    l2: str = L2,
    l3: str | None = None,
    aliases: tuple[str, ...] = (),
    redirect: str | None = None,
    created_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
        " l3_operator, aliases_json, redirect_to_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
        (
            node_id,
            grade,
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
    grade: str = GRADE,  # P10-C：学段参数化（原硬编码 '高中物理-高二' 即 GRADE）
    module: str = L1,
    origin: str = "USER_CANONICAL",
    mastery: str = "ACTIVE",
    created_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO questions (question_uid, origin_type, target_grade_and_subject,"
        " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
        " base_elo, tier, primary_l1_module, mastery_state, created_at)"
        " VALUES (?, ?, ?, '题干', ?, '42', 2, 2, 2, 2, 1500,"
        " 'L3_综合中档', ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
        (uid, origin, grade, f"hash-{uid}", module, mastery, created_at),
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
    # P10-C：种子边补 grade_subject，自 u 端点派生（与 P10-A 回填规则一致）
    u_row = conn.execute(
        "SELECT l1_module, grade_subject FROM knowledge_nodes WHERE node_id = ?", (u,)
    ).fetchone()
    mu, grade = u_row[0], u_row[1]
    mv = conn.execute(
        "SELECT l1_module FROM knowledge_nodes WHERE node_id = ?", (v,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO concept_co_errors (node_u_id, node_v_id, grade_subject,"
        " l1_module_u, l1_module_v, active_error_count, total_historical_count,"
        " source_question_uids_json, last_error_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (u, v, grade, mu, mv, active, historical, json.dumps(list(uids), ensure_ascii=False),
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
        "INSERT INTO elo_mutation_ledger (idempotency_key, grade_subject, l1_module,"
        " question_uid, mutation_trigger, eval_difficulty_elo, watermark_gamma,"
        " coverage_elo_before, coverage_elo_after, combat_elo_before, combat_elo_after,"
        " explanation_text)"
        " VALUES ('q1:s1:MASTERED_INIT', ?, ?, 'q1', 'MASTERED_INIT', 1500, 1.0,"
        " 1200.0, 1216.0, 1200.0, 1216.0, '测试账本行')",
        (GRADE, L1),  # P10-C：种子账本行补 grade_subject（零变化断言本身不动）
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
    assert CanonicalNodeResolver(conn, GRADE).resolve("A") == "A"


def test_resolver_follows_redirect_chain(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B", redirect="A")
    _insert_node(conn, "C", redirect="B")
    assert CanonicalNodeResolver(conn, GRADE).resolve("C") == "A"


def test_resolver_unknown_node_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        CanonicalNodeResolver(conn, GRADE).resolve("ghost")


def test_resolver_cycle_raises(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B")
    conn.execute("UPDATE knowledge_nodes SET redirect_to_id = 'B' WHERE node_id = 'A'")
    conn.execute("UPDATE knowledge_nodes SET redirect_to_id = 'A' WHERE node_id = 'B'")
    with pytest.raises(ValueError, match="环"):
        CanonicalNodeResolver(conn, GRADE).resolve("A")


# ---------------------------------------------------------------------------
# match_or_create_l3_node：召回 / 复用 / 新建
# ---------------------------------------------------------------------------


async def test_first_node_created_without_judge(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    node_id, reused = await match_or_create_l3_node(conn, GRADE, L1, L2, "全新算子", ["别名1"])
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
    node_id, reused = await match_or_create_l3_node(conn, GRADE, L1, L2, NAME_B, [])
    assert reused is False and node_id != "A"
    assert conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 2


async def test_reuse_merges_aliases_dedup_and_excludes_primary(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3="主算子", aliases=("旧别名",))
    judge.decide = lambda payload: "A"
    node_id, reused = await match_or_create_l3_node(
        conn, GRADE, L1, L2, "新名称", ["旧别名", "另一别名"]
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
    _, reused = await match_or_create_l3_node(conn, GRADE, L1, L2, "主算子", [])
    assert reused is True
    assert json.loads(
        conn.execute("SELECT aliases_json FROM knowledge_nodes WHERE node_id='A'").fetchone()[0]
    ) == []


async def test_reuse_alias_cap_8(conn: sqlite3.Connection, judge: JudgeSpy) -> None:
    full = tuple(f"别名{i}" for i in range(settings.MAX_ALIASES_PER_NODE))
    _insert_node(conn, "A", aliases=full)
    judge.decide = lambda payload: "A"
    _, reused = await match_or_create_l3_node(conn, GRADE, L1, L2, "溢出新名称", [])
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
    node_id, reused = await match_or_create_l3_node(conn, GRADE, L1, L2, "新算子", [])
    assert reused is False and node_id != "A"


async def test_judge_hit_cross_scope_creates_new(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """恶意裁判返回异 L2 节点：硬拒绝复用，绝不跨域锚定。"""
    _insert_node(conn, "A")
    _insert_node(conn, "E", l2=L2_OTHER, l3=NAME_A)
    judge.decide = lambda payload: "E"
    node_id, reused = await match_or_create_l3_node(conn, GRADE, L1, L2, NAME_A, [])
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
    node_id, reused = await match_or_create_l3_node(conn, GRADE, L1, L2, NAME_B, [])
    assert reused is False and node_id not in ("A", "R")


async def test_recall_25_nodes_all_sent(conn: sqlite3.Connection, judge: JudgeSpy) -> None:
    for i in range(settings.L3_LOCAL_RECALL_TRIGGER_COUNT):
        _insert_node(conn, f"n{i:02d}", l3=f"动量定理电荷量算子{i:02d}")
    _, reused = await match_or_create_l3_node(conn, GRADE, L1, L2, "动量定理电荷量", [])
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
    await match_or_create_l3_node(conn, GRADE, L1, L2, "动量定理电荷量计算", [])
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
    await match_or_create_l3_node(conn, GRADE, L1, L2, "动量定理电荷量", [])
    assert len(judge.match_calls[0]["existing_nodes"]) == 5


# ---------------------------------------------------------------------------
# 阈值触发自动 Compaction
# ---------------------------------------------------------------------------


async def test_auto_compaction_triggers_at_15(
    conn: sqlite3.Connection, judge: JudgeSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    original = kg.run_scoped_compaction

    def wrapped(c: sqlite3.Connection, grade: str, l1: str, l2: str) -> str | None:
        calls.append((l1, l2))
        return original(c, grade, l1, l2)

    monkeypatch.setattr(kg, "run_scoped_compaction", wrapped)
    for name in DISJOINT_NAMES[:14]:
        await match_or_create_l3_node(conn, GRADE, L1, L2, name, [])
    assert calls == []
    await match_or_create_l3_node(conn, GRADE, L1, L2, DISJOINT_NAMES[14], [])
    assert calls == [(L1, L2)]


async def test_threshold_counts_only_since_last_batch(
    conn: sqlite3.Connection, judge: JudgeSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    # P10-C：补 grade 形参；原 lambda 的 append-or-None 写法有 mypy func-returns-value
    # 存量告警（门禁仅查 backend/ 未暴露），改等价具名函数，行为不变
    def spy(c: sqlite3.Connection, grade: str, l1: str, l2: str) -> None:
        calls.append((l1, l2))

    monkeypatch.setattr(kg, "run_scoped_compaction", spy)
    for i in range(10):
        _insert_node(conn, f"old-{i}", l3=DISJOINT_NAMES[i], created_at="1999-01-01 00:00:00")
    conn.execute(
        "INSERT INTO compaction_audit_ledger (batch_id, grade_subject, l1_module,"
        " l2_entity, from_node_id, to_node_id, ai_judge_evidence,"
        " previous_aliases_to_json, affected_child_node_ids_json,"
        " previous_co_errors_snapshot_json, created_at)"
        " VALUES ('old-batch', ?, ?, ?, 'old-0', 'old-1', '证据', '[]', '[]', '[]',"
        " '2000-01-01 00:00:00')",
        (GRADE, L1, L2),  # P10-C：种子审计行补 grade_subject（水位线查询限同学段）
    )
    for name in DISJOINT_NAMES[:14]:
        await match_or_create_l3_node(conn, GRADE, L1, L2, f"新{name}", [])
    assert calls == []  # 旧批次前的 10 个节点不计入
    await match_or_create_l3_node(conn, GRADE, L1, L2, "新寅亥", [])
    assert calls == [(L1, L2)]


async def test_auto_compaction_merges_similar_pair(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """新建满 15 自动 compaction，Mock 裁判确认近义对 -> 真实合并 + 审计。"""
    judge.decide = _confirm_compaction
    for name in DISJOINT_NAMES[:13]:
        await match_or_create_l3_node(conn, GRADE, L1, L2, name, [])
    older_id, _ = await match_or_create_l3_node(conn, GRADE, L1, L2, NAME_A, [])
    assert conn.execute("SELECT COUNT(*) FROM compaction_audit_ledger").fetchone()[0] == 0
    newer_id, _ = await match_or_create_l3_node(conn, GRADE, L1, L2, NAME_B, [])
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
    assert run_scoped_compaction(conn, GRADE, L1, L2) is None
    _insert_node(conn, "A")
    assert run_scoped_compaction(conn, GRADE, L1, L2) is None
    assert judge.calls == []


def test_compaction_zero_jaccard_skips_judge(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3="甲子")
    _insert_node(conn, "B", l3="乙丑")
    assert run_scoped_compaction(conn, GRADE, L1, L2) is None
    assert judge.compaction_calls == []


def test_compaction_judge_reject_no_merge(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "B", l3=NAME_B)
    assert run_scoped_compaction(conn, GRADE, L1, L2) is None
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
    assert run_scoped_compaction(conn, GRADE, L1, L2) is None
    assert _redirects(conn) == {"A": None, "B": None, "E": None}
    assert conn.execute("SELECT COUNT(*) FROM compaction_audit_ledger").fetchone()[0] == 0


def test_compaction_cross_l2_judge_hard_rejected(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "B", l3=NAME_B)
    _insert_node(conn, "E", l2=L2_OTHER, l3=NAME_A)
    judge.decide = lambda payload: "E"
    assert run_scoped_compaction(conn, GRADE, L1, L2) is None
    assert _redirects(conn) == {"A": None, "B": None, "E": None}


def test_compaction_scope_isolation(conn: sqlite3.Connection, judge: JudgeSpy) -> None:
    """同名近义节点分属不同 L2：各自作用域内均无可合并对。"""
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "E", l2=L2_OTHER, l3=NAME_B)
    judge.decide = _confirm_compaction
    assert run_scoped_compaction(conn, GRADE, L1, L2) is None
    assert run_scoped_compaction(conn, GRADE, L1, L2_OTHER) is None
    assert _redirects(conn) == {"A": None, "E": None}


def test_scope_guard_raises_on_cross_scope(conn: sqlite3.Connection) -> None:
    """写入前最后一道硬闸：跨 L1/L2 合并请求直接 ValueError。"""
    _insert_node(conn, "A")
    _insert_node(conn, "E", l1=L1_OTHER)
    with pytest.raises(ValueError, match="严禁跨 L1/L2"):
        kg._assert_same_scope(conn, GRADE, ["A", "E"], L1, L2)


def test_merge_pair_rejects_inactive_nodes(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B", redirect="A")
    with pytest.raises(ValueError, match="活跃主节点"):
        kg._merge_pair(conn, GRADE, L1, L2, "B", "A", "batch-x", "证据")


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

    def boom(c: sqlite3.Connection, grade: str, snapshot: list[dict[str, Any]]) -> None:
        raise RuntimeError("边归并中途失败")

    monkeypatch.setattr(kg, "_rewrite_edges_after_merge", boom)
    with pytest.raises(RuntimeError, match="边归并中途失败"):
        run_scoped_compaction(conn, GRADE, L1, L2)
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
    batch_id = run_scoped_compaction(conn, GRADE, L1, L2)
    assert isinstance(batch_id, str) and batch_id
    assert _redirects(conn) == {"A": None, "B": "A", "C": "A"}
    _assert_hops_le_one(conn)
    resolver = CanonicalNodeResolver(conn, GRADE)
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
    batch_id = run_scoped_compaction(conn, GRADE, L1, L2)
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
    run_scoped_compaction(conn, GRADE, L1, L2)
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
    run_scoped_compaction(conn, GRADE, L1, L2)
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
    run_scoped_compaction(conn, GRADE, L1, L2)
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

    batch_id = run_scoped_compaction(conn, GRADE, L1, L2)
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
    batch_id = run_scoped_compaction(conn, GRADE, L1, L2)
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
    batch_id = run_scoped_compaction(conn, GRADE, L1, L2)
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
    batch_id = run_scoped_compaction(conn, GRADE, L1, L2)
    assert batch_id is not None
    revert_compaction_batch(conn, batch_id)
    assert _dump(conn, "elo_mutation_ledger", "event_id") == elo_before
    assert _dump(conn, "question_escrow_pool", "question_uid") == pool_before


# ---------------------------------------------------------------------------
# compute_graph_metrics：双指标与冷启动安全边界
# ---------------------------------------------------------------------------


def test_metrics_empty_module_no_nan(conn: sqlite3.Connection) -> None:
    dto = compute_graph_metrics(conn, GRADE, L1)
    assert isinstance(dto, GraphMetricsDTO)
    assert dto.mastery_rate == 0.0 and dto.convergence == 0.0 and dto.active_nodes == 0
    assert math.isfinite(dto.mastery_rate) and math.isfinite(dto.convergence)


def test_metrics_cold_start_n1_w2(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    for uid in ("q1", "q2"):
        _insert_question(conn, uid, mastery="ACTIVE")
        _insert_step(conn, f"{uid}-s1", uid, "A", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, GRADE, L1)
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
    dto = compute_graph_metrics(conn, GRADE, L1)
    assert dto.active_nodes == 6
    assert dto.mastery_rate == pytest.approx(50.0)  # (2 + 0.5*1) / 5 * 100
    assert dto.convergence == 0.0  # k_reuse=0


def test_metrics_uniform_distribution_eta_1(conn: sqlite3.Connection) -> None:
    for nid in ("n1", "n2", "n3"):
        _insert_node(conn, nid)
    for i in range(6):
        _insert_question(conn, f"q{i}")
        _insert_step(conn, f"s{i}", f"q{i}", f"n{i % 3 + 1}", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, GRADE, L1)
    assert dto.convergence == pytest.approx(100.0)  # rho=1, eta=1
    assert dto.mastery_rate == 0.0


def test_metrics_single_point_collapse_eta_0(conn: sqlite3.Connection) -> None:
    for nid in ("n1", "n2", "n3"):
        _insert_node(conn, nid)
    for i in range(6):
        _insert_question(conn, f"q{i}")
        _insert_step(conn, f"s{i}", f"q{i}", "n1", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, GRADE, L1)
    assert dto.convergence == pytest.approx(40.0)  # rho=1, eta=0 -> 0.4*100
    assert dto.active_nodes == 3


def test_metrics_window_only_recent_20(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    for i in range(25):
        _insert_question(conn, f"q{i:02d}", created_at=f"2026-01-01 00:{i:02d}:00")
        _insert_step(
            conn, f"s{i:02d}", f"q{i:02d}", "A", blocked=1, reused=1 if i < 5 else 0
        )
    dto = compute_graph_metrics(conn, GRADE, L1)
    assert dto.convergence == 0.0  # 最近 20 道全部未复用，最早 5 道复用不计入


def test_metrics_min_sample_boundary(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    for i in range(4):
        _insert_question(conn, f"q{i}")
        _insert_step(conn, f"s{i}", f"q{i}", "A", blocked=1, reused=1)
    assert compute_graph_metrics(conn, GRADE, L1).convergence == 0.0  # w=4 < 5
    _insert_question(conn, "q4")
    _insert_step(conn, "s4", "q4", "A", blocked=1, reused=1)
    # w=5, rho=1；N=1 < 3 -> eta=0 -> 40.0
    assert compute_graph_metrics(conn, GRADE, L1).convergence == pytest.approx(40.0)


def test_metrics_redirected_steps_count_to_parent(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    _insert_node(conn, "B")
    _insert_node(conn, "D")
    _insert_node(conn, "C", redirect="B")
    mounts = ["A", "A", "C", "C", "D", "D"]
    for i, nid in enumerate(mounts):
        _insert_question(conn, f"q{i}")
        _insert_step(conn, f"s{i}", f"q{i}", nid, blocked=1, reused=1)
    dto = compute_graph_metrics(conn, GRADE, L1)
    assert dto.active_nodes == 3  # C 已重定向，不计活跃主节点
    assert dto.convergence == pytest.approx(100.0)  # A/B/D 各 2 步均匀分布


def test_metrics_cross_module_steps_excluded(conn: sqlite3.Connection) -> None:
    """跨模块挂载的步骤不计入本模块分母，归属到节点所在模块。"""
    _insert_node(conn, "A")
    _insert_node(conn, "D", l1=L1_OTHER, l2=L2_OTHER)
    _insert_question(conn, "q1", mastery="MASTERED")
    _insert_step(conn, "s1", "q1", "D", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, GRADE, L1)
    assert dto.mastery_rate == 0.0  # A 无挂载，D 的步骤归属其他模块
    assert dto.active_nodes == 1
    dto_other = compute_graph_metrics(conn, GRADE, L1_OTHER)
    assert dto_other.mastery_rate == 100.0
    assert dto_other.active_nodes == 1


def test_metrics_synthetic_excluded_from_window(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "A")
    for i in range(6):
        _insert_question(conn, f"q{i}", origin="AI_SYNTHETIC_SCAFFOLD")
        _insert_step(conn, f"s{i}", f"q{i}", "A", blocked=1, reused=1)
    dto = compute_graph_metrics(conn, GRADE, L1)
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


def test_all_node_states_matches_node_state(conn: sqlite3.Connection) -> None:
    _insert_node(conn, "M0")  # 无阻塞挂载 -> MASTERED
    _insert_node(conn, "A1")
    _insert_node(conn, "A1-sub", redirect="A1")
    _insert_node(conn, "P1")
    _insert_question(conn, "q-act", mastery="ACTIVE")
    _insert_question(conn, "q-pen", mastery="PENDING_RETEST")
    _insert_step(conn, "s1", "q-act", "A1-sub", blocked=1)
    _insert_step(conn, "s2", "q-pen", "P1", blocked=1)
    bulk = kg.all_node_states(conn, GRADE)
    for nid in ("M0", "A1", "P1"):
        assert bulk[nid] == node_state(conn, nid)
    assert bulk["M0"] == "MASTERED"
    assert bulk["A1"] == "ACTIVE"
    assert bulk["P1"] == "PENDING_RETEST"


async def test_compaction_zero_merge_watermark_skips_rescan(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    kg._NO_MERGE_SCAN_WATERMARKS.clear()
    names = [
        "甲子", "乙丑", "丙寅", "丁卯", "戊辰",
        "己巳", "庚午", "辛未", "壬申", "癸酉",
        "戌亥", "子戌", "丑亥", "寅亥", "卯戌",
        "辰巳", "午未", "申酉", "乾坤", "坎离",
        "震巽", "艮兑", "风雷", "水火", "山泽",
        "日月", "星辰", "天地", "玄黄", "宇宙",
    ]
    for name in names[:15]:
        await match_or_create_l3_node(conn, GRADE, "天文", "星象", f"定理{name}", [])
    compactions_at_15 = len(judge.compaction_calls)
    assert compactions_at_15 > 0

    for name in names[15:29]:
        await match_or_create_l3_node(conn, GRADE, "天文", "星象", f"定理{name}", [])
    assert len(judge.compaction_calls) == compactions_at_15

    await match_or_create_l3_node(conn, GRADE, "天文", "星象", f"定理{names[29]}", [])
    assert len(judge.compaction_calls) > compactions_at_15


# ---------------------------------------------------------------------------
# P10-C：学段学科分区（docs/tasks/P10-C_graph_grade_scoping.md 新增测试 1~5）
# ---------------------------------------------------------------------------


async def test_cross_grade_same_path_nodes_independent(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """P10-C 新增 1：两学段同名 (L1, L2, L3) 各自独立长节点，学段落库正确。"""
    id_a, reused_a = await match_or_create_l3_node(conn, GRADE, L1, L2, NAME_A, [])
    id_b, reused_b = await match_or_create_l3_node(conn, GRADE_OTHER, L1, L2, NAME_A, [])
    assert reused_a is False and reused_b is False
    assert id_a != id_b
    assert judge.calls == []  # 各自作用域内首个节点：无候选不触发裁判，更不跨学段召回
    grades = {
        str(r["node_id"]): r["grade_subject"]
        for r in conn.execute("SELECT node_id, grade_subject FROM knowledge_nodes")
    }
    assert grades == {id_a: GRADE, id_b: GRADE_OTHER}


async def test_recall_never_reuses_cross_grade(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """P10-C 新增 1：召回限同学段——异学段同名节点既不进候选集也不可复用。"""
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "E", grade=GRADE_OTHER, l3=NAME_B)  # 同 L1/L2 异学段
    judge.decide = lambda payload: "A"  # 恶意裁判返回异学段节点 ID
    node_id, reused = await match_or_create_l3_node(conn, GRADE_OTHER, L1, L2, NAME_A, [])
    assert reused is False and node_id not in ("A", "E")
    # 候选集仅含同学段 E；跨学段 A 从未被召回
    assert len(judge.match_calls) == 1
    assert {n["node_id"] for n in judge.match_calls[0]["existing_nodes"]} == {"E"}
    row = conn.execute(
        "SELECT grade_subject, redirect_to_id FROM knowledge_nodes WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    assert row["grade_subject"] == GRADE_OTHER and row["redirect_to_id"] is None
    assert _redirects(conn) == {"A": None, "E": None, node_id: None}


def test_compaction_scan_never_pairs_cross_grade(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """P10-C 新增 1：高 Jaccard 近义对分属两学段 -> 各作用域零配对零裁判零合并。"""
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "B", grade=GRADE_OTHER, l3=NAME_B)
    judge.decide = _confirm_compaction
    assert run_scoped_compaction(conn, GRADE, L1, L2) is None
    assert run_scoped_compaction(conn, GRADE_OTHER, L1, L2) is None
    assert judge.compaction_calls == []
    assert _redirects(conn) == {"A": None, "B": None}
    assert conn.execute("SELECT COUNT(*) FROM compaction_audit_ledger").fetchone()[0] == 0


def test_scope_guard_raises_on_cross_grade(conn: sqlite3.Connection) -> None:
    """P10-C 新增 1：跨学段合并与跨 L1/L2 同级红线——写入前硬闸 ValueError。"""
    _insert_node(conn, "A")
    _insert_node(conn, "E", grade=GRADE_OTHER)  # 同 L1/L2，仅学段不同
    with pytest.raises(ValueError, match="严禁跨学段"):
        kg._assert_same_scope(conn, GRADE, ["A", "E"], L1, L2)
    with pytest.raises(ValueError, match="严禁跨学段"):
        kg._merge_pair(conn, GRADE, L1, L2, "E", "A", "batch-x", "证据")
    assert _redirects(conn) == {"A": None, "E": None}
    assert conn.execute("SELECT COUNT(*) FROM compaction_audit_ledger").fetchone()[0] == 0


def test_build_pairs_rejects_cross_grade_input(conn: sqlite3.Connection) -> None:
    """P10-C 新增 1：配对预筛防御断言——混入异学段节点立即 ValueError。"""
    _insert_node(conn, "A", l3=NAME_A)
    _insert_node(conn, "B", grade=GRADE_OTHER, l3=NAME_B)
    rows = conn.execute("SELECT * FROM knowledge_nodes ORDER BY node_id").fetchall()
    with pytest.raises(ValueError, match="严禁跨学段配对"):
        kg._build_compaction_pairs(GRADE, rows)


def test_resolver_cross_grade_node_is_unknown(conn: sqlite3.Connection) -> None:
    """P10-C 新增 1：解析器 SQL grade 过滤防御——异学段节点视为未知（KeyError）。"""
    _insert_node(conn, "A")
    _insert_node(conn, "E", grade=GRADE_OTHER)
    assert CanonicalNodeResolver(conn, GRADE).resolve("A") == "A"
    with pytest.raises(KeyError):
        CanonicalNodeResolver(conn, GRADE).resolve("E")


def test_grade_scoped_compaction_audit_and_exact_revert(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """P10-C 新增 2：同学段近义合并全链——审计/边 grade 正确；回滚逐字段恢复（含 grade 列）。"""
    judge.decide = _confirm_compaction
    _insert_node(conn, "A", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "B", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(conn, "C", redirect="B", created_at="2026-01-01 00:00:03")
    _insert_node(conn, "X", l2=L2_OTHER)
    _insert_node(conn, "A-decoy", grade=GRADE_OTHER, l3=NAME_B)  # 异学段诱饵：绝不被触碰
    _insert_edge(conn, "B", "X", active=2, historical=3, uids=("q1",))
    before = _snapshot_all(conn)

    batch_id = run_scoped_compaction(conn, GRADE, L1, L2)
    assert batch_id is not None
    assert _redirects(conn)["B"] == "A" and _redirects(conn)["C"] == "A"
    _assert_hops_le_one(conn)
    assert _redirects(conn)["A-decoy"] is None
    ledger = conn.execute(
        "SELECT * FROM compaction_audit_ledger WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    assert len(ledger) == 1
    assert ledger[0]["grade_subject"] == GRADE  # 审计行学段正确
    # 迁移新键边 (B,X)->(A,X) 的 grade_subject 落库正确
    edge = conn.execute(
        "SELECT * FROM concept_co_errors WHERE node_u_id = 'A' AND node_v_id = 'X'"
    ).fetchone()
    assert edge["grade_subject"] == GRADE
    assert edge["active_error_count"] == 2 and edge["total_historical_count"] == 3

    revert_compaction_batch(conn, batch_id)
    after = _snapshot_all(conn)
    for table, rows in before.items():
        assert after[table] == rows, f"{table} 未逐字段精确恢复（含 grade_subject）"
    restored = conn.execute(
        "SELECT grade_subject FROM concept_co_errors WHERE node_u_id = 'B' AND node_v_id = 'X'"
    ).fetchone()
    assert restored[0] == GRADE  # 快照含 grade 列自然恢复


def test_record_edges_grade_from_question_and_lifecycle(conn: sqlite3.Connection) -> None:
    """P10-C 新增 3：标红边学段自题目行派生（uid 签名不变）；同学段边生长/销账正常。"""
    _insert_node(conn, "A")
    _insert_node(conn, "B")
    _insert_node(conn, "A2", grade=GRADE_OTHER)
    _insert_node(conn, "B2", grade=GRADE_OTHER)
    _insert_question(conn, "q1")
    _insert_step(conn, "q1-s1", "q1", "A")
    _insert_step(conn, "q1-s2", "q1", "B", index=2)
    _insert_question(conn, "q2", grade=GRADE_OTHER)
    _insert_step(conn, "q2-s1", "q2", "A2")
    _insert_step(conn, "q2-s2", "q2", "B2", index=2)

    record_co_error_edges(conn, "q1")
    record_co_error_edges(conn, "q2")
    edges = {
        (e["node_u_id"], e["node_v_id"]): e
        for e in _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    }
    assert set(edges) == {("A", "B"), ("A2", "B2")}  # 无跨学段边
    assert edges[("A", "B")]["grade_subject"] == GRADE
    assert edges[("A2", "B2")]["grade_subject"] == GRADE_OTHER

    record_co_error_edges(conn, "q1")  # 同学段生长：计数递增，学段不变
    edge = conn.execute(
        "SELECT * FROM concept_co_errors WHERE node_u_id = 'A' AND node_v_id = 'B'"
    ).fetchone()
    assert edge["active_error_count"] == 2 and edge["total_historical_count"] == 2
    assert edge["grade_subject"] == GRADE
    decrement_co_error_edges_on_mastery(conn, "q1")  # 按 uid 销账：签名不变
    edge = conn.execute(
        "SELECT * FROM concept_co_errors WHERE node_u_id = 'A' AND node_v_id = 'B'"
    ).fetchone()
    assert edge["active_error_count"] == 1 and edge["total_historical_count"] == 2
    assert edge["grade_subject"] == GRADE
    other = conn.execute(
        "SELECT * FROM concept_co_errors WHERE node_u_id = 'A2' AND node_v_id = 'B2'"
    ).fetchone()
    assert other["active_error_count"] == 1 and other["grade_subject"] == GRADE_OTHER


def test_metrics_grade_partition_independent(conn: sqlite3.Connection) -> None:
    """P10-C 新增 4：两学段同名 L1 双指标各自独立；最近窗口不被跨学段稀释。"""
    for i in (1, 2, 3):
        _insert_node(conn, f"g{i}")
        _insert_node(conn, f"o{i}", grade=GRADE_OTHER)
    for i in range(6):
        _insert_question(
            conn, f"qg{i}", mastery="MASTERED", created_at=f"2026-01-01 00:0{i}:00"
        )
        _insert_step(conn, f"sg{i}", f"qg{i}", f"g{i % 3 + 1}", blocked=1, reused=1)
    for i in range(6):
        _insert_question(
            conn, f"qo{i}", grade=GRADE_OTHER, mastery="ACTIVE",
            created_at=f"2026-01-02 00:0{i}:00",
        )
        _insert_step(conn, f"so{i}", f"qo{i}", f"o{i % 3 + 1}", blocked=1, reused=0)

    dto = compute_graph_metrics(conn, GRADE, L1)
    assert dto.active_nodes == 3
    assert dto.mastery_rate == pytest.approx(100.0)
    # 若窗口未按学段分区，异学段 6 道更新的未复用题会把 rho 稀释到 6/12
    assert dto.convergence == pytest.approx(100.0)
    dto_other = compute_graph_metrics(conn, GRADE_OTHER, L1)
    assert dto_other.active_nodes == 3
    assert dto_other.mastery_rate == 0.0
    assert dto_other.convergence == 0.0  # rho=0（全部未复用）


def test_metrics_cold_start_grade_no_nan(conn: sqlite3.Connection) -> None:
    """P10-C 新增 4：零节点零题目的学段返回全 0，无除零 NaN。"""
    _insert_node(conn, "A")  # 仅 GRADE 有节点
    dto = compute_graph_metrics(conn, GRADE_OTHER, L1)
    assert isinstance(dto, GraphMetricsDTO)
    assert dto.mastery_rate == 0.0 and dto.convergence == 0.0 and dto.active_nodes == 0
    assert math.isfinite(dto.mastery_rate) and math.isfinite(dto.convergence)


def test_all_node_states_grade_partition(conn: sqlite3.Connection) -> None:
    """P10-C 新增 4：批量状态聚合只返回同学段活跃主节点；node_state 学段自派生。"""
    _insert_node(conn, "A")
    _insert_node(conn, "A2", grade=GRADE_OTHER)
    _insert_question(conn, "q1", mastery="ACTIVE")
    _insert_step(conn, "s1", "q1", "A", blocked=1)
    _insert_question(conn, "q2", grade=GRADE_OTHER, mastery="PENDING_RETEST")
    _insert_step(conn, "s2", "q2", "A2", blocked=1)
    assert kg.all_node_states(conn, GRADE) == {"A": "ACTIVE"}
    assert kg.all_node_states(conn, GRADE_OTHER) == {"A2": "PENDING_RETEST"}
    assert node_state(conn, "A") == "ACTIVE"  # 按 uid 签名不变，学段自节点行派生
    assert node_state(conn, "A2") == "PENDING_RETEST"


async def test_watermark_grade_partition(
    conn: sqlite3.Connection, judge: JudgeSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-C 新增 5：(db, gradeA, l1, l2) 0 合并水位不影响 gradeB 同 (l1, l2) 触发计数。"""
    kg._NO_MERGE_SCAN_WATERMARKS.clear()
    calls: list[tuple[str, str, str]] = []
    original = kg.run_scoped_compaction

    def wrapped(c: sqlite3.Connection, grade: str, l1: str, l2: str) -> str | None:
        calls.append((grade, l1, l2))
        return original(c, grade, l1, l2)

    monkeypatch.setattr(kg, "run_scoped_compaction", wrapped)
    names = DISJOINT_NAMES  # 15 个互零 Jaccard 名称
    for name in names[:14]:
        await match_or_create_l3_node(conn, GRADE, "天文", "星象", f"定理{name}", [])
    assert calls == []
    await match_or_create_l3_node(conn, GRADE, "天文", "星象", f"定理{names[14]}", [])
    assert calls == [(GRADE, "天文", "星象")]  # gradeA 第 15 个触发 0 合并扫描
    # 水位线 key 升级为 (db_key, grade, l1, l2) 四元组
    assert (kg._db_cache_key(conn), GRADE, "天文", "星象") in kg._NO_MERGE_SCAN_WATERMARKS

    # gradeB 同 (l1, l2) 独立计数：不被 gradeA 水位压制
    for name in names[:14]:
        await match_or_create_l3_node(conn, GRADE_OTHER, "天文", "星象", f"定理{name}", [])
    assert calls == [(GRADE, "天文", "星象")]
    await match_or_create_l3_node(conn, GRADE_OTHER, "天文", "星象", f"定理{names[14]}", [])
    assert calls == [(GRADE, "天文", "星象"), (GRADE_OTHER, "天文", "星象")]

    # gradeA 水位仍有效：再增 14 不触发，第 15 个再触发（与 gradeB 互不干扰）
    round2 = [
        "乾坤", "坎离", "震巽", "艮兑", "风雷", "水火", "日月", "星辰",
        "天地", "玄黄", "宇宙", "阴阳", "山河", "草木", "金石",
    ]
    for name in round2[:14]:
        await match_or_create_l3_node(conn, GRADE, "天文", "星象", name, [])
    assert len(calls) == 2
    await match_or_create_l3_node(conn, GRADE, "天文", "星象", round2[14], [])
    assert len(calls) == 3 and calls[-1] == (GRADE, "天文", "星象")


# ---------------------------------------------------------------------------
# P12-B：Delta-Preserving Revert & LIFO 依赖守卫（Risk 2）
# ---------------------------------------------------------------------------


def test_revert_preserves_post_merge_edges_and_deltas(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """P12-B：合并 B1 (A->B) 后新增边 (B,Y) 与增量 (B,X)+q_post，回滚 B1 保留增量且不丢 (B,Y)。"""
    judge.decide = _confirm_compaction
    # B 更早创建（older 主节点），A 较新（newer 合并入 B）
    _insert_node(conn, "B", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "A", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(conn, "X", l2=L2_OTHER)
    _insert_node(conn, "Y", l2=L2_OTHER)
    _insert_edge(
        conn, "A", "X", active=1, historical=1, uids=("q1",),
        last_error_at="2026-01-02 00:00:00",
    )
    _insert_edge(
        conn, "B", "X", active=2, historical=3, uids=("q2", "q3"),
        last_error_at="2026-01-03 00:00:00",
    )

    batch_b1 = run_scoped_compaction(conn, GRADE, L1, L2)
    assert batch_b1 is not None
    assert _redirects(conn)["A"] == "B"

    # 合并后新增标红边 (B, Y) 以及对既有 (B, X) 增量写入 q_post
    _insert_question(conn, "q_post")
    _insert_step(conn, "sp1", "q_post", "B", index=1)
    _insert_step(conn, "sp2", "q_post", "X", index=2)
    record_co_error_edges(conn, "q_post")

    _insert_question(conn, "q_y")
    _insert_step(conn, "sy1", "q_y", "B", index=1)
    _insert_step(conn, "sy2", "q_y", "Y", index=2)
    record_co_error_edges(conn, "q_y")

    # 回滚 B1
    revert_compaction_batch(conn, batch_b1)
    assert _redirects(conn)["A"] is None
    assert _redirects(conn)["B"] is None

    edges = {
        (e["node_u_id"], e["node_v_id"]): e
        for e in _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    }
    assert set(edges) == {("A", "X"), ("B", "X"), ("B", "Y")}

    # (A, X) 原样恢复
    ax = edges[("A", "X")]
    assert ax["active_error_count"] == 1
    assert ax["total_historical_count"] == 1
    assert json.loads(ax["source_question_uids_json"]) == ["q1"]
    assert ax["last_error_at"] == "2026-01-02 00:00:00"

    # (B, X) 保留合并前 (2,3) + 合并后增量 (1,1) = (3,4) 与 q_post，且未丢弃任何后续写入
    bx = edges[("B", "X")]
    assert bx["active_error_count"] == 3
    assert bx["total_historical_count"] == 4
    assert json.loads(bx["source_question_uids_json"]) == ["q2", "q3", "q_post"]

    # (B, Y) 合并后新增边完好无损
    by = edges[("B", "Y")]
    assert by["active_error_count"] == 1
    assert by["total_historical_count"] == 1
    assert json.loads(by["source_question_uids_json"]) == ["q_y"]


def test_revert_preserves_shared_uid_and_migrated_edge_post_increment(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """P12-B：合并双方共享同一 q_shared 时回滚不误删 pre_to 的 q_shared；纯迁移边后增量亦保留。"""
    judge.decide = _confirm_compaction
    _insert_node(conn, "B", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "A", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(conn, "X", l2=L2_OTHER)
    _insert_node(conn, "Z", l2=L2_OTHER)
    _insert_edge(
        conn, "A", "X", active=1, historical=1, uids=("q_shared",),
        last_error_at="2026-01-02 00:00:00",
    )
    _insert_edge(
        conn, "B", "X", active=1, historical=1, uids=("q_shared",),
        last_error_at="2026-01-03 00:00:00",
    )
    _insert_edge(
        conn, "A", "Z", active=1, historical=1, uids=("q_az",),
        last_error_at="2026-01-01 00:00:00",
    )

    batch_id = run_scoped_compaction(conn, GRADE, L1, L2)
    assert batch_id is not None

    # 仅在 (A, Z)->(B, Z) 上发生合并后增量 q_bz_post
    _insert_question(conn, "q_bz_post")
    _insert_step(conn, "s1", "q_bz_post", "B", index=1)
    _insert_step(conn, "s2", "q_bz_post", "Z", index=2)
    record_co_error_edges(conn, "q_bz_post")

    revert_compaction_batch(conn, batch_id)
    edges = {
        (e["node_u_id"], e["node_v_id"]): e
        for e in _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    }
    assert set(edges) == {("A", "X"), ("A", "Z"), ("B", "X"), ("B", "Z")}
    assert json.loads(edges[("A", "X")]["source_question_uids_json"]) == ["q_shared"]
    assert json.loads(edges[("B", "X")]["source_question_uids_json"]) == ["q_shared"]
    assert edges[("B", "X")]["last_error_at"] == "2026-01-03 00:00:00"
    assert json.loads(edges[("A", "Z")]["source_question_uids_json"]) == ["q_az"]
    assert edges[("B", "Z")]["active_error_count"] == 1
    assert json.loads(edges[("B", "Z")]["source_question_uids_json"]) == ["q_bz_post"]


def test_revert_lifo_guard_blocks_out_of_order_direct_dependency(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """P12-B：B1(B->A) 之后又发生 B2(C->A)，先回滚 B1 抛 ValueError；先 B2 后 B1 成功。"""
    judge.decide = _confirm_compaction
    _insert_node(conn, "A", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "B", l3=NAME_B, created_at="2026-01-01 00:00:02")
    batch_b1 = run_scoped_compaction(conn, GRADE, L1, L2)
    assert batch_b1 is not None

    _insert_node(conn, "C", l3=NAME_C, created_at="2026-01-01 00:00:03")
    batch_b2 = run_scoped_compaction(conn, GRADE, L1, L2)
    assert batch_b2 is not None and batch_b2 != batch_b1
    assert _redirects(conn) == {"A": None, "B": "A", "C": "A"}

    snapshot_before_err = _snapshot_all(conn)
    with pytest.raises(ValueError, match="先回滚"):
        revert_compaction_batch(conn, batch_b1)
    assert _snapshot_all(conn) == snapshot_before_err

    revert_compaction_batch(conn, batch_b2)
    revert_compaction_batch(conn, batch_b1)
    assert _redirects(conn) == {"A": None, "B": None, "C": None}


def test_revert_lifo_guard_blocks_cross_scope_snapshot_neighbor_dependency(
    conn: sqlite3.Connection, judge: JudgeSpy
) -> None:
    """P12-B：B1(A->B) 快照含跨 L1/L2 边 (A, C)，后续 B2(D->C) 跨作用域合并后先回滚 B1 抛错。"""
    judge.decide = _confirm_compaction
    _insert_node(conn, "B", l3=NAME_A, created_at="2026-01-01 00:00:01")
    _insert_node(conn, "A", l3=NAME_B, created_at="2026-01-01 00:00:02")
    _insert_node(
        conn, "C", l1=L1_OTHER, l2=L2_OTHER, l3="动量定理求电荷量", created_at="2026-01-01 00:00:01"
    )
    _insert_edge(conn, "A", "C", uids=("q_cross",))

    batch_b1 = run_scoped_compaction(conn, GRADE, L1, L2)
    assert batch_b1 is not None

    _insert_node(
        conn,
        "D",
        l1=L1_OTHER,
        l2=L2_OTHER,
        l3="动量定理求电荷量大小",
        created_at="2026-01-01 00:00:02",
    )
    batch_b2 = run_scoped_compaction(conn, GRADE, L1_OTHER, L2_OTHER)
    assert batch_b2 is not None
    assert _redirects(conn) == {"B": None, "A": "B", "C": None, "D": "C"}

    with pytest.raises(ValueError, match="先回滚"):
        revert_compaction_batch(conn, batch_b1)

    revert_compaction_batch(conn, batch_b2)
    revert_compaction_batch(conn, batch_b1)
    assert _redirects(conn) == {"B": None, "A": None, "C": None, "D": None}
    edges = _dump(conn, "concept_co_errors", "node_u_id, node_v_id")
    assert [(e["node_u_id"], e["node_v_id"]) for e in edges] == [("A", "C")]
