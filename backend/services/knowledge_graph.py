"""三层知识网引擎：路径锚定、同域可逆并查集 Compaction 与双指标。

唯一权威规格：docs/designs/adaptive_practice_harness.md §3.4（模块 4）、CUJ 4、
Target File #10/#17、Alternative 2A/2B（为什么同域 + 可逆）。
API 签名为 PM 锁定契约（docs/tasks/P3-A_knowledge_graph.md，conn 由调用方管理，
同 elo_engine 模式：本模块不开连接、不 commit）。

核心不变量：
- 严禁跨 L1/L2 重定向（Alternative 2A 被否决原因：撕裂分模块 Elo 账本）：
  函数签名即作用域，合并写入前再断言节点归属；AI 裁判返回作用域外 / 非活跃
  节点 ID 一律视为未命中（硬拒绝，绝不产生跨域 redirect）。
- 一次性路径压缩：from 及其全部子节点直接指向 to，任意节点跳数 <= 1。
- 每对合并单 SAVEPOINT 小事务（Risks 表：避开前台答题高峰的单对小事务）。
- 回滚按 merge_id 逆序逐字段精确恢复（父指针 / aliases / 标红边快照），
  Elo 账本（elo_mutation_ledger / question_escrow_pool）零变化。
- 节点展示状态为派生值（悲观半格 ACTIVE > PENDING_RETEST > MASTERED），不落库。
- 双指标冷启动安全：total=0 / w<5 / N<3 时无除零、无 NaN。

说明：设计 Target File #10 的签名草稿（``-> list[str]`` / ``-> str``）与 PM 契约
（``-> str | None`` 批次 UUID / ``-> tuple[str, bool]``）不一致；权威行为规格 §3.4
与 CUJ 4 均以 batch_id 为回滚单元且未锁定返回值形状，故按 PM 契约实现。
``question_steps.reused_existing_node`` 的落库由入库方（ingest）依据本模块返回的
reused 标记完成，本模块不直接写步骤表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import uuid
from collections import Counter
from collections.abc import Coroutine, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import combinations
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from backend.config import settings
from backend.observability import log_event
from backend.services import llm_gateway

__all__ = [
    "CanonicalNodeResolver",
    "GraphMetricsDTO",
    "NodeJudgeResult",
    "compute_graph_metrics",
    "decrement_co_error_edges_on_mastery",
    "match_or_create_l3_node",
    "node_state",
    "record_co_error_edges",
    "revert_compaction_batch",
    "run_scoped_compaction",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 设计 §3.4 公式常量（设计文档锁定、config 未托管的数学定义参数）
# ---------------------------------------------------------------------------

_RECENT_WINDOW = 20  # 收敛度统计窗口 W：最近 20 道 USER_CANONICAL 录入题
_MIN_SAMPLE = 5  # 最小样本门槛：w < 5 时 rho = 0
_MIN_ENTROPY_NODES = 3  # N < 3 时 eta = 0（防单点垃圾桶坍缩误判）
_CONVERGENCE_BASE = 0.4  # Convergence = rho * (0.4 + 0.6 * eta) * 100
_CONVERGENCE_ENTROPY_WEIGHT = 0.6
_PENDING_CREDIT = 0.5  # MasteryRate 中 PENDING_RETEST 记半分

# 悲观半格优先级：ACTIVE > PENDING_RETEST > MASTERED（设计 §3.4 第 2.2 条）
_STATE_RANK: dict[str, int] = {"MASTERED": 0, "PENDING_RETEST": 1, "ACTIVE": 2}

_JUDGE_SYSTEM_PROMPT = (
    "你是三层知识网的 L3 原子转化算子对齐裁判。判断候选算子与既有节点是否语义同一"
    "（同一原子转化操作，而非仅表面相似或形似神异）。只输出 JSON 对象："
    '{"match_node_id": "<命中的既有节点ID，未命中为null>", "evidence": "<简短判定证据>"}。'
)


class NodeJudgeResult(BaseModel):
    """AI 节点裁判结构化输出（purpose='node_judge'）。"""

    match_node_id: str | None
    evidence: str


class GraphMetricsDTO(BaseModel):
    """单个 L1 模块的知识网双指标（版图攻克率 + 分布熵校准收敛度，均为百分数）。"""

    mastery_rate: float
    convergence: float
    active_nodes: int


# ---------------------------------------------------------------------------
# 并查集解析器
# ---------------------------------------------------------------------------


class CanonicalNodeResolver:
    """穿透 redirect_to_id 解析活跃主节点 ID（跳数 <= 1 由合并侧保证）。

    防御性实现：允许穿透任意长链，但检测到环立即抛 ValueError（图谱损坏）；
    未知 node_id 抛 KeyError。不做缓存（conn 由调用方管理，合并/回滚随时发生）。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def resolve(self, node_id: str) -> str:
        seen: set[str] = set()
        current = node_id
        while True:
            row = self._conn.execute(
                "SELECT redirect_to_id FROM knowledge_nodes WHERE node_id = ?", (current,)
            ).fetchone()
            if row is None:
                raise KeyError(f"knowledge_nodes 中不存在节点: {node_id!r}")
            target = row[0]
            if target is None:
                return current
            if current in seen:
                raise ValueError(f"redirect_to_id 出现环（图谱损坏）: {node_id!r}")
            seen.add(current)
            current = str(target)


# ---------------------------------------------------------------------------
# 内部工具：作用域断言 / 事务 / 字符 N-gram / 别名 / 裁判调用
# ---------------------------------------------------------------------------


def _node_row(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM knowledge_nodes WHERE node_id = ?", (node_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"knowledge_nodes 中不存在节点: {node_id!r}")
    return row


def _assert_same_scope(
    conn: sqlite3.Connection, node_ids: Iterable[str], l1: str, l2: str
) -> None:
    """合并/回滚写入前的最后一道硬闸：任何节点跨 L1/L2 立即抛 ValueError。"""
    for node_id in node_ids:
        row = _node_row(conn, node_id)
        if row["l1_module"] != l1 or row["l2_entity"] != l2:
            raise ValueError(
                f"严禁跨 L1/L2 重定向: 节点 {node_id!r} 属于 "
                f"({row['l1_module']!r}, {row['l2_entity']!r})，作用域为 ({l1!r}, {l2!r})"
            )


@contextmanager
def _savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """单对合并/回滚的小事务（调用方管理外层事务，本模块不 commit）。"""
    name = f"kg_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def _char_ngrams(names: Iterable[str]) -> set[str]:
    """标准库字符 2/3-gram 集合（casefold + 去空白；设计 §3.4 第 1 条）。"""
    grams: set[str] = set()
    for name in names:
        text = "".join(name.casefold().split())
        for n in (2, 3):
            for i in range(len(text) - n + 1):
                grams.add(text[i : i + n])
    return grams


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _surface_names(row: sqlite3.Row) -> list[str]:
    return [str(row["l3_operator"]), *json.loads(row["aliases_json"])]


def _row_grams(row: sqlite3.Row) -> set[str]:
    return _char_ngrams(_surface_names(row))


def _merge_aliases(existing: list[str], primary: str, incoming: Iterable[str]) -> list[str]:
    """新名称并入 aliases：保序去重、排除主名称、上限 settings.MAX_ALIASES_PER_NODE。"""
    merged = list(existing)
    for name in incoming:
        if name and name != primary and name not in merged:
            merged.append(name)
    return merged[: settings.MAX_ALIASES_PER_NODE]


def _active_nodes(conn: sqlite3.Connection, l1: str, l2: str) -> list[sqlite3.Row]:
    """同父 L2 活跃节点（redirect_to_id IS NULL），按创建时间 + ID 稳定排序。"""
    return conn.execute(
        "SELECT * FROM knowledge_nodes WHERE l1_module = ? AND l2_entity = ?"
        " AND redirect_to_id IS NULL ORDER BY created_at, node_id",
        (l1, l2),
    ).fetchall()


def _judge_payload(
    task: str, l1: str, l2: str, candidate_names: list[str], existing: list[sqlite3.Row]
) -> dict[str, Any]:
    return {
        "task": task,
        "l1_module": l1,
        "l2_entity": l2,
        "candidate": {"names": candidate_names},
        "existing_nodes": [
            {"node_id": row["node_id"], "names": _surface_names(row)} for row in existing
        ],
    }


async def _judge_nodes(payload: dict[str, Any]) -> NodeJudgeResult:
    """AI 裁判调用（纯网关，不触 DB，便于同步侧跨线程执行）。"""
    result = await llm_gateway.chat_json(
        [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        NodeJudgeResult,
        model=settings.SOLVER_LLM_MODEL_NAME,
        purpose="node_judge",
    )
    return cast(NodeJudgeResult, result)


_T = TypeVar("_T")


def _run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """同步执行协程：无运行中事件循环用 asyncio.run；否则投递到独立线程的新循环。

    仅承载纯网关协程（不触 sqlite conn），规避 check_same_thread 限制。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _pessimistic(current: str, incoming: str) -> str:
    return incoming if _STATE_RANK[incoming] > _STATE_RANK[current] else current


# ---------------------------------------------------------------------------
# 同父 L2 邻域召回与节点复用 / 新建
# ---------------------------------------------------------------------------


async def match_or_create_l3_node(
    conn: sqlite3.Connection,
    l1: str,
    l2: str,
    candidate_l3: str,
    candidate_aliases: list[str],
) -> tuple[str, bool]:
    """同父 L2 邻域 AI 裁判对齐：命中复用主 ID，未命中 uuid4 新建。

    返回 (node_id, reused)。reused=True 时由入库方标记
    question_steps.reused_existing_node = 1。新建累计满
    settings.COMPACTION_BATCH_THRESHOLD（自上次该 L2 compaction 批次以来）
    自动触发 run_scoped_compaction。
    """
    nodes = _active_nodes(conn, l1, l2)
    match_id: str | None = None
    if nodes:
        if len(nodes) > settings.L3_LOCAL_RECALL_TRIGGER_COUNT:
            cand_grams = _char_ngrams([candidate_l3, *candidate_aliases])
            ranked = sorted(
                nodes,
                key=lambda row: (-_jaccard(cand_grams, _row_grams(row)), str(row["node_id"])),
            )
            sent = ranked[: settings.L3_TOP_K_CANDIDATES]
        else:
            sent = nodes
        payload = _judge_payload(
            "match_or_create", l1, l2, [candidate_l3, *candidate_aliases], sent
        )
        verdict = await _judge_nodes(payload)
        sent_ids = {str(row["node_id"]) for row in sent}
        if verdict.match_node_id is not None and verdict.match_node_id in sent_ids:
            match_id = verdict.match_node_id

    if match_id is not None:
        row = _node_row(conn, match_id)
        # 硬拒绝：裁判命中必须仍是同作用域活跃主节点，否则降级为新建。
        if row["redirect_to_id"] is None and row["l1_module"] == l1 and row["l2_entity"] == l2:
            aliases = _merge_aliases(
                json.loads(row["aliases_json"]),
                str(row["l3_operator"]),
                [candidate_l3, *candidate_aliases],
            )
            conn.execute(
                "UPDATE knowledge_nodes SET aliases_json = ? WHERE node_id = ?",
                (json.dumps(aliases, ensure_ascii=False), match_id),
            )
            log_event(
                logger,
                "graph.node_reused",
                l1=l1,
                l2=l2,
                l3=candidate_l3,
                node_id=match_id,
            )
            return match_id, True

    new_id = str(uuid.uuid4())
    aliases = _merge_aliases([], candidate_l3, candidate_aliases)
    conn.execute(
        "INSERT INTO knowledge_nodes (node_id, l1_module, l2_entity, l3_operator, aliases_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (new_id, l1, l2, candidate_l3, json.dumps(aliases, ensure_ascii=False)),
    )
    log_event(
        logger,
        "graph.node_created",
        l1=l1,
        l2=l2,
        l3=candidate_l3,
        node_id=new_id,
    )
    if _new_nodes_since_last_compaction(conn, l1, l2) >= settings.COMPACTION_BATCH_THRESHOLD:
        run_scoped_compaction(conn, l1, l2)
    return new_id, False


def _new_nodes_since_last_compaction(conn: sqlite3.Connection, l1: str, l2: str) -> int:
    """自上次该 L2 compaction 批次以来的新建节点数（无批次则全部计数）。"""
    last_batch_at = conn.execute(
        "SELECT MAX(created_at) FROM compaction_audit_ledger WHERE l1_module = ? AND l2_entity = ?",
        (l1, l2),
    ).fetchone()[0]
    if last_batch_at is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE l1_module = ? AND l2_entity = ?",
            (l1, l2),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE l1_module = ? AND l2_entity = ?"
            " AND created_at > ?",
            (l1, l2, last_batch_at),
        ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# 同域可逆并查集 Compaction
# ---------------------------------------------------------------------------


def run_scoped_compaction(conn: sqlite3.Connection, l1: str, l2: str) -> str | None:
    """同 (l1, l2) 作用域内扫描近义 L3 节点并合并，返回 batch_id（无合并返回 None）。

    近义对 = 字符 2/3-gram Jaccard 预筛（>0）+ AI 裁判确认；合并方向固定为
    较新节点并入较旧主节点（CUJ 4：node_27 -> node_12）。每对合并单 SAVEPOINT
    小事务：路径压缩 + 标红边去自环/归并 + 审计快照。严禁跨 L1/L2 重定向。
    """
    nodes = _active_nodes(conn, l1, l2)
    if len(nodes) < 2:
        return None
    grams = {str(row["node_id"]): _row_grams(row) for row in nodes}
    pairs: list[tuple[float, str, str]] = []
    for older, newer in combinations(nodes, 2):  # nodes 已按 (created_at, node_id) 排序
        older_id, newer_id = str(older["node_id"]), str(newer["node_id"])
        sim = _jaccard(grams[older_id], grams[newer_id])
        if sim > 0.0:
            pairs.append((-sim, older_id, newer_id))
    pairs.sort()

    batch_id: str | None = None
    for _neg_sim, to_id, from_id in pairs:
        if _is_redirected(conn, to_id) or _is_redirected(conn, from_id):
            continue  # 先序合并已使该节点失活（路径压缩保持跳数 <= 1）
        to_row, from_row = _node_row(conn, to_id), _node_row(conn, from_id)
        payload = _judge_payload(
            "scoped_compaction", l1, l2, _surface_names(from_row), [to_row]
        )
        verdict = _run_sync(_judge_nodes(payload))
        # 硬拒绝：裁判必须确认并入同作用域主节点；作用域外 ID 一律视为未命中。
        if verdict.match_node_id != to_id:
            continue
        if batch_id is None:
            batch_id = str(uuid.uuid4())
        _merge_pair(conn, l1, l2, from_id, to_id, batch_id, verdict.evidence)
    return batch_id


def _is_redirected(conn: sqlite3.Connection, node_id: str) -> bool:
    return _node_row(conn, node_id)["redirect_to_id"] is not None


def _merge_pair(
    conn: sqlite3.Connection,
    l1: str,
    l2: str,
    from_id: str,
    to_id: str,
    batch_id: str,
    evidence: str,
) -> None:
    """单对合并（单 SAVEPOINT 事务）：路径压缩 -> 标红边归并 -> 审计快照。"""
    _assert_same_scope(conn, [from_id, to_id], l1, l2)
    from_row, to_row = _node_row(conn, from_id), _node_row(conn, to_id)
    if from_row["redirect_to_id"] is not None or to_row["redirect_to_id"] is not None:
        raise ValueError(f"合并双方必须均为活跃主节点: {from_id!r} -> {to_id!r}")

    # ---- 合并前快照（逐字段精确回滚的依据） ----
    previous_aliases_to = str(to_row["aliases_json"])
    children = sorted(
        str(row[0])
        for row in conn.execute(
            "SELECT node_id FROM knowledge_nodes WHERE redirect_to_id = ?", (from_id,)
        )
    )
    affected_endpoints = {from_id, to_id, *children}
    edge_snapshot = [
        dict(row) for row in _edges_touching(conn, affected_endpoints)
    ]

    with _savepoint(conn):
        # 1) 一次性路径压缩：from 与全部子节点直接指向 to（跳数 <= 1）
        conn.execute(
            "UPDATE knowledge_nodes SET redirect_to_id = ? WHERE redirect_to_id = ?",
            (to_id, from_id),
        )
        conn.execute(
            "UPDATE knowledge_nodes SET redirect_to_id = ? WHERE node_id = ?",
            (to_id, from_id),
        )
        # 2) 别名并入主节点（上限 settings.MAX_ALIASES_PER_NODE）
        merged_aliases = _merge_aliases(
            json.loads(previous_aliases_to),
            str(to_row["l3_operator"]),
            _surface_names(from_row),
        )
        conn.execute(
            "UPDATE knowledge_nodes SET aliases_json = ? WHERE node_id = ?",
            (json.dumps(merged_aliases, ensure_ascii=False), to_id),
        )
        # 3) 标红边：resolve 端点 -> 删自环 / 重合累加并 uids / 迁移新键
        _rewrite_edges_after_merge(conn, edge_snapshot)
        # 4) 审计快照（batch_id 同批共用 UUID）
        conn.execute(
            "INSERT INTO compaction_audit_ledger (batch_id, l1_module, l2_entity,"
            " from_node_id, to_node_id, ai_judge_evidence, previous_aliases_to_json,"
            " affected_child_node_ids_json, previous_co_errors_snapshot_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                l1,
                l2,
                from_id,
                to_id,
                evidence,
                previous_aliases_to,
                json.dumps(children, ensure_ascii=False),
                json.dumps(edge_snapshot, ensure_ascii=False),
            ),
        )
    log_event(
        logger,
        "graph.compaction_merged",
        batch_id=batch_id,
        from_node=from_id,
        to_node=to_id,
        l1=l1,
        l2=l2,
    )


def _edges_touching(conn: sqlite3.Connection, node_ids: set[str]) -> list[sqlite3.Row]:
    ids = sorted(node_ids)
    placeholders = ", ".join("?" for _ in ids)
    return conn.execute(
        f"SELECT * FROM concept_co_errors WHERE node_u_id IN ({placeholders})"
        f" OR node_v_id IN ({placeholders}) ORDER BY node_u_id, node_v_id",
        [*ids, *ids],
    ).fetchall()


def _rewrite_edges_after_merge(
    conn: sqlite3.Connection, edge_snapshot: list[dict[str, Any]]
) -> None:
    """按合并前快照逐边重算规范化端点 (u', v') = (resolve(u), resolve(v))。"""
    resolver = CanonicalNodeResolver(conn)
    for edge in edge_snapshot:
        old_u, old_v = str(edge["node_u_id"]), str(edge["node_v_id"])
        new_u, new_v = resolver.resolve(old_u), resolver.resolve(old_v)
        if new_u == new_v:
            # 自环消除（DB CHECK 约束级兜底之外的主动删除）
            conn.execute(
                "DELETE FROM concept_co_errors WHERE node_u_id = ? AND node_v_id = ?",
                (old_u, old_v),
            )
            continue
        key = (min(new_u, new_v), max(new_u, new_v))
        if key == (old_u, old_v):
            continue  # 端点未变（本就挂在主节点上的边），计数保持
        uids = json.loads(edge["source_question_uids_json"])
        target = conn.execute(
            "SELECT * FROM concept_co_errors WHERE node_u_id = ? AND node_v_id = ?",
            key,
        ).fetchone()
        conn.execute(
            "DELETE FROM concept_co_errors WHERE node_u_id = ? AND node_v_id = ?",
            (old_u, old_v),
        )
        if target is None:
            conn.execute(
                "INSERT INTO concept_co_errors (node_u_id, node_v_id, l1_module_u,"
                " l1_module_v, active_error_count, total_historical_count,"
                " source_question_uids_json, last_error_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key[0],
                    key[1],
                    str(_node_row(conn, key[0])["l1_module"]),
                    str(_node_row(conn, key[1])["l1_module"]),
                    edge["active_error_count"],
                    edge["total_historical_count"],
                    json.dumps(uids, ensure_ascii=False),
                    edge["last_error_at"],
                ),
            )
        else:
            merged_uids = list(json.loads(target["source_question_uids_json"]))
            for uid in uids:
                if uid not in merged_uids:
                    merged_uids.append(uid)
            conn.execute(
                "UPDATE concept_co_errors SET active_error_count = ?,"
                " total_historical_count = ?, source_question_uids_json = ?,"
                " last_error_at = ? WHERE node_u_id = ? AND node_v_id = ?",
                (
                    int(target["active_error_count"]) + int(edge["active_error_count"]),
                    int(target["total_historical_count"]) + int(edge["total_historical_count"]),
                    json.dumps(merged_uids, ensure_ascii=False),
                    max(str(target["last_error_at"]), str(edge["last_error_at"])),
                    key[0],
                    key[1],
                ),
            )


# ---------------------------------------------------------------------------
# 一键解绑回滚（Un-merge）
# ---------------------------------------------------------------------------


def revert_compaction_batch(conn: sqlite3.Connection, batch_id: str) -> None:
    """按 merge_id 逆序精确恢复批次内全部合并；已回滚批次幂等无操作。

    恢复内容：from.redirect=NULL、affected 子节点指回 from、to.aliases 还原、
    标红边按快照逐字段还原（删新增 / 恢复计数与 uids）。Elo 账本完全不动。
    未知 batch_id 亦幂等无操作。语义为标准 undo：假定批次后无冲突性后续变更。
    """
    rows = conn.execute(
        "SELECT * FROM compaction_audit_ledger WHERE batch_id = ? ORDER BY merge_id DESC",
        (batch_id,),
    ).fetchall()
    reverted = 0
    for row in rows:
        if row["is_reverted"]:
            continue
        with _savepoint(conn):
            _revert_single_merge(conn, row)
        reverted += 1
    log_event(logger, "graph.reverted", batch_id=batch_id, merges=reverted)


def _revert_single_merge(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    l1, l2 = str(row["l1_module"]), str(row["l2_entity"])
    from_id, to_id = str(row["from_node_id"]), str(row["to_node_id"])
    _assert_same_scope(conn, [from_id, to_id], l1, l2)
    children = [str(c) for c in json.loads(row["affected_child_node_ids_json"])]

    # 标红边：删除当前受影响边（含合并新增），按快照逐字段原样恢复
    affected_endpoints = {from_id, to_id, *children}
    ids = sorted(affected_endpoints)
    placeholders = ", ".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM concept_co_errors WHERE node_u_id IN ({placeholders})"
        f" OR node_v_id IN ({placeholders})",
        [*ids, *ids],
    )
    for edge in json.loads(row["previous_co_errors_snapshot_json"]):
        conn.execute(
            "INSERT INTO concept_co_errors (node_u_id, node_v_id, l1_module_u,"
            " l1_module_v, active_error_count, total_historical_count,"
            " source_question_uids_json, last_error_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edge["node_u_id"],
                edge["node_v_id"],
                edge["l1_module_u"],
                edge["l1_module_v"],
                edge["active_error_count"],
                edge["total_historical_count"],
                edge["source_question_uids_json"],
                edge["last_error_at"],
            ),
        )
    # 主节点 aliases 精确还原
    conn.execute(
        "UPDATE knowledge_nodes SET aliases_json = ? WHERE node_id = ?",
        (row["previous_aliases_to_json"], to_id),
    )
    # 两跳指针回滚：二级子节点指回 from，from 恢复活跃主节点
    for child in children:
        conn.execute(
            "UPDATE knowledge_nodes SET redirect_to_id = ? WHERE node_id = ?",
            (from_id, child),
        )
    conn.execute(
        "UPDATE knowledge_nodes SET redirect_to_id = NULL WHERE node_id = ?", (from_id,)
    )
    conn.execute(
        "UPDATE compaction_audit_ledger SET is_reverted = 1,"
        " reverted_at = CURRENT_TIMESTAMP WHERE merge_id = ?",
        (row["merge_id"],),
    )


# ---------------------------------------------------------------------------
# 节点展示状态（派生值，不落库）
# ---------------------------------------------------------------------------


def node_state(conn: sqlite3.Connection, node_id: str) -> str:
    """悲观半格聚合：仅 USER_CANONICAL 且 is_blocked_step=1 步骤所属题目状态。

    含重定向子节点挂载的步骤；无此类步骤 -> MASTERED（仅已掌握前缀步骤的节点
    保持绿色）。优先级 ACTIVE > PENDING_RETEST > MASTERED。
    """
    resolver = CanonicalNodeResolver(conn)
    root = resolver.resolve(node_id)
    rows = conn.execute(
        "SELECT s.knowledge_node_id, q.mastery_state FROM question_steps s"
        " JOIN questions q ON q.question_uid = s.question_uid"
        " WHERE q.origin_type = 'USER_CANONICAL' AND s.is_blocked_step = 1"
    ).fetchall()
    state = "MASTERED"
    for row in rows:
        if resolver.resolve(str(row["knowledge_node_id"])) == root:
            state = _pessimistic(state, str(row["mastery_state"]))
    return state


# ---------------------------------------------------------------------------
# 跨考点标红边生命周期
# ---------------------------------------------------------------------------


def _question_canonical_nodes(conn: sqlite3.Connection, question_uid: str) -> list[str]:
    resolver = CanonicalNodeResolver(conn)
    rows = conn.execute(
        "SELECT DISTINCT knowledge_node_id FROM question_steps WHERE question_uid = ?",
        (question_uid,),
    ).fetchall()
    return sorted({resolver.resolve(str(row[0])) for row in rows})


def record_co_error_edges(conn: sqlite3.Connection, question_uid: str) -> None:
    """该题 >=2 个不同 resolve 后节点 -> 两两边 (u<v)：active+1、historical+1、uids 并入。"""
    nodes = _question_canonical_nodes(conn, question_uid)
    if len(nodes) < 2:
        return
    for u, v in combinations(nodes, 2):  # nodes 已排序 -> 天然 u < v，DB CHECK 兜底
        existing = conn.execute(
            "SELECT * FROM concept_co_errors WHERE node_u_id = ? AND node_v_id = ?",
            (u, v),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO concept_co_errors (node_u_id, node_v_id, l1_module_u,"
                " l1_module_v, active_error_count, total_historical_count,"
                " source_question_uids_json) VALUES (?, ?, ?, ?, 1, 1, ?)",
                (
                    u,
                    v,
                    str(_node_row(conn, u)["l1_module"]),
                    str(_node_row(conn, v)["l1_module"]),
                    json.dumps([question_uid], ensure_ascii=False),
                ),
            )
        else:
            uids = json.loads(existing["source_question_uids_json"])
            if question_uid not in uids:
                uids.append(question_uid)
            conn.execute(
                "UPDATE concept_co_errors SET active_error_count = active_error_count + 1,"
                " total_historical_count = total_historical_count + 1,"
                " source_question_uids_json = ?, last_error_at = CURRENT_TIMESTAMP"
                " WHERE node_u_id = ? AND node_v_id = ?",
                (json.dumps(uids, ensure_ascii=False), u, v),
            )
    log_event(
        logger,
        "graph.co_error",
        action="record",
        question_uid=question_uid,
        nodes=nodes,
        edges=len(nodes) * (len(nodes) - 1) // 2,
    )


def decrement_co_error_edges_on_mastery(conn: sqlite3.Connection, question_uid: str) -> None:
    """销账时对应边 active-1（下限 0）；historical 与 uids 溯源不动。"""
    nodes = _question_canonical_nodes(conn, question_uid)
    for u, v in combinations(nodes, 2):
        conn.execute(
            "UPDATE concept_co_errors SET active_error_count = MAX(0, active_error_count - 1)"
            " WHERE node_u_id = ? AND node_v_id = ?",
            (u, v),
        )
    log_event(
        logger,
        "graph.co_error",
        action="decrement",
        question_uid=question_uid,
        nodes=nodes,
        edges=len(nodes) * (len(nodes) - 1) // 2,
    )


# ---------------------------------------------------------------------------
# 双指标：版图攻克率 + 分布熵校准收敛度
# ---------------------------------------------------------------------------


def compute_graph_metrics(conn: sqlite3.Connection, l1_module: str) -> GraphMetricsDTO:
    """按 L1 模块独立核算双指标（设计 §3.4 第 3 条，冷启动无除零 NaN）。

    - MasteryRate = (mastered + 0.5 * pending) / total * 100，仅统计挂载
      USER_CANONICAL 题目的活跃主节点（含经重定向子节点挂载）。
    - Convergence = rho * (0.4 + 0.6 * eta) * 100：rho 取最近 W=20 道题的复用
      比例（w<5 -> 0）；eta 为节点步骤分布归一化熵（N<3 -> 0，0 ln 0 = 0）。
    """
    active_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT node_id FROM knowledge_nodes WHERE l1_module = ?"
            " AND redirect_to_id IS NULL ORDER BY node_id",
            (l1_module,),
        )
    ]
    n = len(active_ids)
    resolver = CanonicalNodeResolver(conn)
    module_by_node = {
        str(row["node_id"]): str(row["l1_module"])
        for row in conn.execute("SELECT node_id, l1_module FROM knowledge_nodes")
    }

    mounted: set[str] = set()
    blocked_states: dict[str, str] = {}
    step_counts: Counter[str] = Counter()
    step_rows = conn.execute(
        "SELECT s.knowledge_node_id, s.is_blocked_step, q.mastery_state"
        " FROM question_steps s JOIN questions q ON q.question_uid = s.question_uid"
        " WHERE q.origin_type = 'USER_CANONICAL'"
    ).fetchall()
    for row in step_rows:
        root = resolver.resolve(str(row["knowledge_node_id"]))
        if module_by_node.get(root) != l1_module:
            continue
        mounted.add(root)
        step_counts[root] += 1
        if row["is_blocked_step"] == 1:
            blocked_states[root] = _pessimistic(
                blocked_states.get(root, "MASTERED"), str(row["mastery_state"])
            )

    total_mounted = len(mounted)
    mastered = sum(
        1 for nid in mounted if blocked_states.get(nid, "MASTERED") == "MASTERED"
    )
    pending = sum(
        1 for nid in mounted if blocked_states.get(nid, "MASTERED") == "PENDING_RETEST"
    )
    mastery_rate = (
        0.0
        if total_mounted == 0
        else (mastered + _PENDING_CREDIT * pending) / total_mounted * 100.0
    )

    # rho：最近 W=20 道 USER_CANONICAL 题中含 reused_existing_node=1 步骤的题数占比
    recent = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM question_steps s WHERE s.question_uid = q.question_uid"
        " AND s.reused_existing_node = 1) AS has_reuse FROM questions q"
        " WHERE q.origin_type = 'USER_CANONICAL' AND q.primary_l1_module = ?"
        " ORDER BY q.created_at DESC, q.question_uid DESC LIMIT ?",
        (l1_module, _RECENT_WINDOW),
    ).fetchall()
    w = len(recent)
    k_reuse = sum(1 for row in recent if row["has_reuse"])
    rho = 0.0 if w < _MIN_SAMPLE else k_reuse / w

    # eta：p_i = 节点挂载 USER_CANONICAL 步骤数 / 模块总步骤数（0 ln 0 = 0）
    total_steps = sum(step_counts.values())
    eta = 0.0
    if n >= _MIN_ENTROPY_NODES and total_steps > 0:
        entropy = 0.0
        for count in step_counts.values():
            if count > 0:
                p = count / total_steps
                entropy -= p * math.log(p)
        eta = min(1.0, entropy / math.log(n))

    convergence = rho * (_CONVERGENCE_BASE + _CONVERGENCE_ENTROPY_WEIGHT * eta) * 100.0
    return GraphMetricsDTO(
        mastery_rate=mastery_rate, convergence=convergence, active_nodes=n
    )
