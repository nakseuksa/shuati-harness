"""三层知识网引擎：路径锚定、同域可逆并查集 Compaction 与双指标。

唯一权威规格：docs/designs/adaptive_practice_harness.md §3.4（模块 4）、CUJ 4、
Target File #10/#17、Alternative 2A/2B（为什么同域 + 可逆）。
API 签名为 PM 锁定契约（docs/tasks/P3-A_knowledge_graph.md，conn 由调用方管理，
同 elo_engine 模式：本模块不开连接、不 commit）。

P10-C（docs/tasks/P10-C_graph_grade_scoping.md）学段学科分区：知识网全部操作
（召回/复用/Compaction/标红边/指标/水位线）缩到同学段作用域；Compaction 作用域
由 (L1, L2) 升级为 (grade_subject, L1, L2) 三元组，跨学段合并与跨 L1/L2 同级硬拒。
作用域类函数在 conn 后加 grade_subject 参数；按 uid 操作的函数（node_state /
record_co_error_edges / decrement_co_error_edges_on_mastery / revert_compaction_batch）
签名不变，学段自题目/节点/批次行自派生并在写入时落库。

核心不变量：
- 严禁跨学段 / 跨 L1/L2 重定向（Alternative 2A 被否决原因：撕裂分模块 Elo 账本）：
  函数签名即作用域，合并写入前再断言节点归属；AI 裁判返回作用域外 / 非活跃
  节点 ID 一律视为未命中（硬拒绝，绝不产生跨域 redirect）。
- 一次性路径压缩：from 及其全部子节点直接指向 to，任意节点跳数 <= 1。
- 每对合并单 SAVEPOINT 小事务（Risks 表：避开前台答题高峰的单对小事务）。
- 回滚按 merge_id 逆序逐字段精确恢复（父指针 / aliases / 标红边快照，含 grade_subject），
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
from collections.abc import Coroutine, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.observability import log_event
from backend.services import llm_gateway

__all__ = [
    "ActiveNodeRecord",
    "CanonicalNodeResolver",
    "GraphMetricsDTO",
    "L3ResolutionRequest",
    "NodeJudgeResult",
    "PlannedL3Resolution",
    "all_node_states",
    "apply_planned_l3_node_resolutions",
    "compute_graph_metrics",
    "decrement_co_error_edges_on_mastery",
    "load_active_nodes_snapshot",
    "match_or_create_l3_node",
    "node_state",
    "plan_l3_node_resolutions",
    "record_co_error_edges",
    "revert_compaction_batch",
    "run_scoped_compaction",
    "run_scoped_compaction_unlocked",
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
    """单个 (学段, L1 模块) 分区的知识网双指标（版图攻克率 + 分布熵校准收敛度，均为百分数）。"""

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
    P10-C：解析链限同学段（SQL grade 过滤为防御；迁移与新建/合并同学段保证链
    内同质），跨学段节点视为未知（KeyError）。
    """

    def __init__(self, conn: sqlite3.Connection, grade_subject: str) -> None:
        self._conn = conn
        self._grade_subject = grade_subject

    def resolve(self, node_id: str) -> str:
        seen: set[str] = set()
        current = node_id
        while True:
            row = self._conn.execute(
                "SELECT redirect_to_id FROM knowledge_nodes"
                " WHERE node_id = ? AND grade_subject = ?",
                (current, self._grade_subject),
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
    conn: sqlite3.Connection,
    grade_subject: str,
    node_ids: Iterable[str],
    l1: str,
    l2: str,
) -> None:
    """合并/回滚写入前的最后一道硬闸：任何节点跨学段或跨 L1/L2 立即抛 ValueError。

    P10-C：学段相等与 L1/L2 相等为同级红线（跨学段合并撕裂学段分区）。
    """
    for node_id in node_ids:
        row = _node_row(conn, node_id)
        if row["grade_subject"] != grade_subject:
            raise ValueError(
                f"严禁跨学段重定向: 节点 {node_id!r} 属于学段 {row['grade_subject']!r}，"
                f"作用域学段为 {grade_subject!r}"
            )
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


def _active_nodes(
    conn: sqlite3.Connection, grade_subject: str, l1: str, l2: str
) -> list[sqlite3.Row]:
    """同 (学段, 父 L2) 活跃节点（redirect_to_id IS NULL），按创建时间 + ID 稳定排序。

    P10-C：召回限同学段，绝不跨学段召回。
    """
    return conn.execute(
        "SELECT * FROM knowledge_nodes WHERE grade_subject = ? AND l1_module = ?"
        " AND l2_entity = ? AND redirect_to_id IS NULL ORDER BY created_at, node_id",
        (grade_subject, l1, l2),
    ).fetchall()


@dataclass
class ActiveNodeRecord:
    """无锁规划阶段的轻量活跃节点内存快照。"""

    node_id: str
    grade_subject: str
    l1_module: str
    l2_entity: str
    l3_operator: str
    aliases: list[str]


@dataclass(frozen=True)
class L3ResolutionRequest:
    """无锁 L3 节点对齐请求项。"""

    grade_subject: str
    l1_module: str
    l2_entity: str
    candidate_l3: str
    candidate_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedL3Resolution:
    """无锁 L3 节点对齐规划结果（写库事务内应用）。"""

    grade_subject: str
    l1_module: str
    l2_entity: str
    candidate_l3: str
    candidate_aliases: tuple[str, ...]
    action: Literal["reuse", "create"]
    target_node_id: str


def _row_to_active_record(row: sqlite3.Row) -> ActiveNodeRecord:
    return ActiveNodeRecord(
        node_id=str(row["node_id"]),
        grade_subject=str(row["grade_subject"]),
        l1_module=str(row["l1_module"]),
        l2_entity=str(row["l2_entity"]),
        l3_operator=str(row["l3_operator"]),
        aliases=list(json.loads(row["aliases_json"])),
    )


def _select_judge_candidates(
    nodes: Sequence[ActiveNodeRecord], candidate_names: Sequence[str]
) -> list[ActiveNodeRecord]:
    if len(nodes) > settings.L3_LOCAL_RECALL_TRIGGER_COUNT:
        cand_grams = _char_ngrams(candidate_names)
        ranked = sorted(
            nodes,
            key=lambda rec: (
                -_jaccard(cand_grams, _char_ngrams([rec.l3_operator, *rec.aliases])),
                rec.node_id,
            ),
        )
        return ranked[: settings.L3_TOP_K_CANDIDATES]
    return list(nodes)


def _judge_payload_from_records(
    task: str,
    l1: str,
    l2: str,
    candidate_names: list[str],
    existing: Sequence[ActiveNodeRecord],
) -> dict[str, Any]:
    return {
        "task": task,
        "l1_module": l1,
        "l2_entity": l2,
        "candidate": {"names": candidate_names},
        "existing_nodes": [
            {"node_id": rec.node_id, "names": [rec.l3_operator, *rec.aliases]}
            for rec in existing
        ],
    }


def _judge_payload(
    task: str, l1: str, l2: str, candidate_names: list[str], existing: list[sqlite3.Row]
) -> dict[str, Any]:
    return _judge_payload_from_records(
        task, l1, l2, candidate_names, [_row_to_active_record(row) for row in existing]
    )


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
# 同父 L2 邻域召回与节点复用 / 新建（含无锁规划/落库拆分）
# ---------------------------------------------------------------------------


def _reuse_existing_l3_node(
    conn: sqlite3.Connection,
    grade_subject: str,
    l1: str,
    l2: str,
    match_id: str,
    candidate_l3: str,
    candidate_aliases: Sequence[str],
) -> bool:
    """同作用域活跃主节点复核与别名扩充；非活跃或跨域则返回 False。"""
    row = conn.execute(
        "SELECT * FROM knowledge_nodes WHERE node_id = ?", (match_id,)
    ).fetchone()
    if row is None:
        return False
    if not (
        row["redirect_to_id"] is None
        and row["grade_subject"] == grade_subject
        and row["l1_module"] == l1
        and row["l2_entity"] == l2
    ):
        return False
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
        grade=grade_subject,
        l1=l1,
        l2=l2,
        l3=candidate_l3,
        node_id=match_id,
    )
    return True


def _insert_new_l3_node(
    conn: sqlite3.Connection,
    grade_subject: str,
    l1: str,
    l2: str,
    candidate_l3: str,
    candidate_aliases: Sequence[str],
    *,
    node_id: str | None = None,
) -> tuple[str, bool]:
    """新建 L3 节点并返回 (node_id, 是否达到自动 Compaction 水位线阈值)。"""
    new_id = node_id or str(uuid.uuid4())
    aliases = _merge_aliases([], candidate_l3, candidate_aliases)
    conn.execute(
        "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
        " l3_operator, aliases_json) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id, grade_subject, l1, l2, candidate_l3, json.dumps(aliases, ensure_ascii=False)),
    )
    log_event(
        logger,
        "graph.node_created",
        grade=grade_subject,
        l1=l1,
        l2=l2,
        l3=candidate_l3,
        node_id=new_id,
    )
    needs_compaction = (
        _new_nodes_since_last_compaction(conn, grade_subject, l1, l2)
        >= settings.COMPACTION_BATCH_THRESHOLD
    )
    return new_id, needs_compaction


def load_active_nodes_snapshot(
    conn: sqlite3.Connection, scopes: Iterable[tuple[str, str, str]]
) -> dict[tuple[str, str, str], list[ActiveNodeRecord]]:
    """只读加载各 (grade_subject, l1, l2) 作用域活跃节点快照（供无锁规划使用）。"""
    snapshot: dict[tuple[str, str, str], list[ActiveNodeRecord]] = {}
    for grade_subject, l1, l2 in dict.fromkeys(scopes):
        snapshot[(grade_subject, l1, l2)] = [
            _row_to_active_record(row) for row in _active_nodes(conn, grade_subject, l1, l2)
        ]
    return snapshot


async def plan_l3_node_resolutions(
    requests: Sequence[L3ResolutionRequest],
    snapshot: dict[tuple[str, str, str], list[ActiveNodeRecord]],
) -> list[PlannedL3Resolution]:
    """纯内存无锁 L3 节点对齐规划：召回 + 别名传播 + AI 裁判，绝不持 DB 连接。"""
    working: dict[tuple[str, str, str], list[ActiveNodeRecord]] = {
        scope: [
            ActiveNodeRecord(
                node_id=rec.node_id,
                grade_subject=rec.grade_subject,
                l1_module=rec.l1_module,
                l2_entity=rec.l2_entity,
                l3_operator=rec.l3_operator,
                aliases=list(rec.aliases),
            )
            for rec in recs
        ]
        for scope, recs in snapshot.items()
    }
    plans: list[PlannedL3Resolution] = []
    for req in requests:
        scope = (req.grade_subject, req.l1_module, req.l2_entity)
        scope_nodes = working.setdefault(scope, [])
        cand_names = [req.candidate_l3, *req.candidate_aliases]
        match_id: str | None = None
        if scope_nodes:
            sent = _select_judge_candidates(scope_nodes, cand_names)
            payload = _judge_payload_from_records(
                "match_or_create", req.l1_module, req.l2_entity, cand_names, sent
            )
            verdict = await _judge_nodes(payload)
            sent_ids = {rec.node_id for rec in sent}
            if verdict.match_node_id is not None and verdict.match_node_id in sent_ids:
                match_id = verdict.match_node_id

        if match_id is not None:
            matched_rec = next(rec for rec in scope_nodes if rec.node_id == match_id)
            matched_rec.aliases = _merge_aliases(
                matched_rec.aliases, matched_rec.l3_operator, cand_names
            )
            plans.append(
                PlannedL3Resolution(
                    grade_subject=req.grade_subject,
                    l1_module=req.l1_module,
                    l2_entity=req.l2_entity,
                    candidate_l3=req.candidate_l3,
                    candidate_aliases=req.candidate_aliases,
                    action="reuse",
                    target_node_id=match_id,
                )
            )
        else:
            planned_id = str(uuid.uuid4())
            scope_nodes.append(
                ActiveNodeRecord(
                    node_id=planned_id,
                    grade_subject=req.grade_subject,
                    l1_module=req.l1_module,
                    l2_entity=req.l2_entity,
                    l3_operator=req.candidate_l3,
                    aliases=_merge_aliases([], req.candidate_l3, req.candidate_aliases),
                )
            )
            plans.append(
                PlannedL3Resolution(
                    grade_subject=req.grade_subject,
                    l1_module=req.l1_module,
                    l2_entity=req.l2_entity,
                    candidate_l3=req.candidate_l3,
                    candidate_aliases=req.candidate_aliases,
                    action="create",
                    target_node_id=planned_id,
                )
            )
    return plans


def _find_concurrent_surface_match(
    conn: sqlite3.Connection,
    grade_subject: str,
    l1: str,
    l2: str,
    candidate_names: Sequence[str],
    snapshot_surfaces: dict[str, set[str]],
    created_in_tx: set[str],
) -> str | None:
    """写事务内复核并发写入：若无锁阶段后出现新活跃节点或新别名精确命中则复用。"""
    cand_set = {name for name in candidate_names if name}
    if not cand_set:
        return None
    for row in _active_nodes(conn, grade_subject, l1, l2):
        nid = str(row["node_id"])
        if nid in created_in_tx:
            continue
        surfaces = set(_surface_names(row))
        if nid not in snapshot_surfaces:
            if cand_set & surfaces:
                return nid
        elif cand_set & (surfaces - snapshot_surfaces[nid]):
            return nid
    return None


def apply_planned_l3_node_resolutions(
    conn: sqlite3.Connection,
    plans: Sequence[PlannedL3Resolution],
    snapshot: dict[tuple[str, str, str], list[ActiveNodeRecord]],
) -> tuple[list[tuple[str, bool]], list[tuple[str, str, str]]]:
    """在单事务内原子落地规划好的 L3 节点复用/新建与水位线检查（不 commit）。"""
    snapshot_surfaces_by_scope: dict[tuple[str, str, str], dict[str, set[str]]] = {
        scope: {rec.node_id: {rec.l3_operator, *rec.aliases} for rec in recs}
        for scope, recs in snapshot.items()
    }
    planned_id_map: dict[str, str] = {}
    created_in_tx: set[str] = set()
    results: list[tuple[str, bool]] = []
    scopes_to_compact: list[tuple[str, str, str]] = []
    seen_compact_scopes: set[tuple[str, str, str]] = set()

    for plan in plans:
        scope = (plan.grade_subject, plan.l1_module, plan.l2_entity)
        if plan.action == "reuse":
            resolved_target = planned_id_map.get(plan.target_node_id, plan.target_node_id)
            if _reuse_existing_l3_node(
                conn,
                plan.grade_subject,
                plan.l1_module,
                plan.l2_entity,
                resolved_target,
                plan.candidate_l3,
                plan.candidate_aliases,
            ):
                results.append((resolved_target, True))
                continue

        concurrent_id = _find_concurrent_surface_match(
            conn,
            plan.grade_subject,
            plan.l1_module,
            plan.l2_entity,
            [plan.candidate_l3, *plan.candidate_aliases],
            snapshot_surfaces_by_scope.get(scope, {}),
            created_in_tx,
        )
        if concurrent_id is not None and _reuse_existing_l3_node(
            conn,
            plan.grade_subject,
            plan.l1_module,
            plan.l2_entity,
            concurrent_id,
            plan.candidate_l3,
            plan.candidate_aliases,
        ):
            if plan.action == "create":
                planned_id_map[plan.target_node_id] = concurrent_id
            results.append((concurrent_id, True))
            continue

        new_id, needs_compaction = _insert_new_l3_node(
            conn,
            plan.grade_subject,
            plan.l1_module,
            plan.l2_entity,
            plan.candidate_l3,
            plan.candidate_aliases,
            node_id=plan.target_node_id if plan.action == "create" else None,
        )
        if plan.action == "create":
            planned_id_map[plan.target_node_id] = new_id
        created_in_tx.add(new_id)
        if needs_compaction and scope not in seen_compact_scopes:
            seen_compact_scopes.add(scope)
            scopes_to_compact.append(scope)
        results.append((new_id, False))

    return results, scopes_to_compact


async def match_or_create_l3_node(
    conn: sqlite3.Connection,
    grade_subject: str,
    l1: str,
    l2: str,
    candidate_l3: str,
    candidate_aliases: list[str],
) -> tuple[str, bool]:
    """同 (学段, 父 L2) 邻域 AI 裁判对齐：命中复用主 ID，未命中 uuid4 新建。

    返回 (node_id, reused)。reused=True 时由入库方标记
    question_steps.reused_existing_node = 1。新建累计满
    settings.COMPACTION_BATCH_THRESHOLD（自上次该 (学段, L2) compaction 批次以来）
    自动触发 run_scoped_compaction。P10-C：召回/复用/新建均限同学段，
    新建节点 INSERT 落 grade_subject。
    """
    records = [_row_to_active_record(row) for row in _active_nodes(conn, grade_subject, l1, l2)]
    match_id: str | None = None
    if records:
        cand_names = [candidate_l3, *candidate_aliases]
        sent = _select_judge_candidates(records, cand_names)
        payload = _judge_payload_from_records("match_or_create", l1, l2, cand_names, sent)
        verdict = await _judge_nodes(payload)
        sent_ids = {rec.node_id for rec in sent}
        if verdict.match_node_id is not None and verdict.match_node_id in sent_ids:
            match_id = verdict.match_node_id

    if match_id is not None and _reuse_existing_l3_node(
        conn, grade_subject, l1, l2, match_id, candidate_l3, candidate_aliases
    ):
        return match_id, True

    new_id, needs_compaction = _insert_new_l3_node(
        conn, grade_subject, l1, l2, candidate_l3, candidate_aliases
    )
    if needs_compaction:
        if run_scoped_compaction is _ORIG_RUN_SCOPED_COMPACTION:
            await _run_scoped_compaction_async(conn, grade_subject, l1, l2)
        else:
            run_scoped_compaction(conn, grade_subject, l1, l2)
    return new_id, False


# (db_key, grade, l1, l2) -> (anchor_node_id, scanned_raw_count)：0 合并扫描后推进水位，防重复全扫
# P10-C：水位线 key 升级含 grade，跨学段 0 合并水位互不影响
_NO_MERGE_SCAN_WATERMARKS: dict[tuple[str, str, str, str], tuple[str, int]] = {}


def _db_cache_key(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    db_file = str(row[2]) if row is not None and row[2] else ""
    return db_file or f"mem:{id(conn)}"


def _raw_nodes_since_last_compaction(
    conn: sqlite3.Connection, grade_subject: str, l1: str, l2: str
) -> int:
    """自上次该 (学段, L2) compaction 批次以来的原始新建节点数（无批次则全部计数）。"""
    last_batch_at = conn.execute(
        "SELECT MAX(created_at) FROM compaction_audit_ledger"
        " WHERE grade_subject = ? AND l1_module = ? AND l2_entity = ?",
        (grade_subject, l1, l2),
    ).fetchone()[0]
    if last_batch_at is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes"
            " WHERE grade_subject = ? AND l1_module = ? AND l2_entity = ?",
            (grade_subject, l1, l2),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM knowledge_nodes"
            " WHERE grade_subject = ? AND l1_module = ? AND l2_entity = ?"
            " AND created_at > ?",
            (grade_subject, l1, l2, last_batch_at),
        ).fetchone()
    return int(row[0])


def _new_nodes_since_last_compaction(
    conn: sqlite3.Connection, grade_subject: str, l1: str, l2: str
) -> int:
    """自上次该 (学段, L2) compaction（含 0 合并扫描水位）以来的新建节点数。

    P10-C：计数限同学段；水位线 key 为 (db_key, grade, l1, l2)。
    """
    raw_count = _raw_nodes_since_last_compaction(conn, grade_subject, l1, l2)
    wm_key = (_db_cache_key(conn), grade_subject, l1, l2)
    wm = _NO_MERGE_SCAN_WATERMARKS.get(wm_key)
    if wm is None:
        return raw_count
    anchor_id, scanned_count = wm
    exists = (
        conn.execute(
            "SELECT 1 FROM knowledge_nodes WHERE node_id = ? AND grade_subject = ?",
            (anchor_id, grade_subject),
        ).fetchone()
        is not None
    )
    if not exists or raw_count < scanned_count:
        _NO_MERGE_SCAN_WATERMARKS.pop(wm_key, None)
        return raw_count
    return raw_count - scanned_count


# ---------------------------------------------------------------------------
# 同域可逆并查集 Compaction
# ---------------------------------------------------------------------------


def _build_compaction_pairs(
    grade_subject: str, nodes: list[sqlite3.Row]
) -> list[tuple[float, str, str]]:
    """近义对预筛：输入必须已限定同一 (学段, L1, L2) 作用域（P10-C 防御断言）。"""
    for row in nodes:
        if row["grade_subject"] != grade_subject:
            raise ValueError(
                f"严禁跨学段配对: 节点 {row['node_id']!r} 属于学段 {row['grade_subject']!r}，"
                f"作用域学段为 {grade_subject!r}"
            )
    grams = {str(row["node_id"]): _row_grams(row) for row in nodes}
    pairs: list[tuple[float, str, str]] = []
    for older, newer in combinations(nodes, 2):  # nodes 已按 (created_at, node_id) 排序
        older_id, newer_id = str(older["node_id"]), str(newer["node_id"])
        sim = _jaccard(grams[older_id], grams[newer_id])
        if sim > 0.0:
            pairs.append((-sim, older_id, newer_id))
    pairs.sort()
    return pairs


def _record_compaction_watermark(
    conn: sqlite3.Connection,
    grade_subject: str,
    l1: str,
    l2: str,
    nodes: list[sqlite3.Row],
    batch_id: str | None,
) -> None:
    wm_key = (_db_cache_key(conn), grade_subject, l1, l2)
    if batch_id is not None:
        _NO_MERGE_SCAN_WATERMARKS.pop(wm_key, None)
    elif nodes:
        _NO_MERGE_SCAN_WATERMARKS[wm_key] = (
            str(nodes[-1]["node_id"]),
            _raw_nodes_since_last_compaction(conn, grade_subject, l1, l2),
        )


def run_scoped_compaction(
    conn: sqlite3.Connection, grade_subject: str, l1: str, l2: str
) -> str | None:
    """同 (grade_subject, l1, l2) 作用域内扫描近义 L3 节点并合并，返回 batch_id（无合并返回 None）。

    近义对 = 字符 2/3-gram Jaccard 预筛（>0）+ AI 裁判确认；合并方向固定为
    较新节点并入较旧主节点（CUJ 4：node_27 -> node_12）。每对合并单 SAVEPOINT
    小事务：路径压缩 + 标红边去自环/归并 + 审计快照。
    P10-C：严禁跨学段 / 跨 L1/L2 重定向（三元组作用域红线）。
    """
    nodes = _active_nodes(conn, grade_subject, l1, l2)
    if len(nodes) < 2:
        _record_compaction_watermark(conn, grade_subject, l1, l2, nodes, None)
        return None
    pairs = _build_compaction_pairs(grade_subject, nodes)

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
        _merge_pair(conn, grade_subject, l1, l2, from_id, to_id, batch_id, verdict.evidence)
    _record_compaction_watermark(conn, grade_subject, l1, l2, nodes, batch_id)
    return batch_id


_ORIG_RUN_SCOPED_COMPACTION = run_scoped_compaction


async def _run_scoped_compaction_async(
    conn: sqlite3.Connection, grade_subject: str, l1: str, l2: str
) -> str | None:
    """run_scoped_compaction 的原生协程版：直接 await _judge_nodes，避免阻塞事件循环。"""
    nodes = _active_nodes(conn, grade_subject, l1, l2)
    if len(nodes) < 2:
        _record_compaction_watermark(conn, grade_subject, l1, l2, nodes, None)
        return None
    pairs = _build_compaction_pairs(grade_subject, nodes)

    batch_id: str | None = None
    for _neg_sim, to_id, from_id in pairs:
        if _is_redirected(conn, to_id) or _is_redirected(conn, from_id):
            continue
        to_row, from_row = _node_row(conn, to_id), _node_row(conn, from_id)
        payload = _judge_payload(
            "scoped_compaction", l1, l2, _surface_names(from_row), [to_row]
        )
        verdict = await _judge_nodes(payload)
        if verdict.match_node_id != to_id:
            continue
        if batch_id is None:
            batch_id = str(uuid.uuid4())
        _merge_pair(conn, grade_subject, l1, l2, from_id, to_id, batch_id, verdict.evidence)
    _record_compaction_watermark(conn, grade_subject, l1, l2, nodes, batch_id)
    return batch_id


async def run_scoped_compaction_unlocked(
    db_path: Path | None, grade_subject: str, l1: str, l2: str
) -> str | None:
    """无锁 Compaction：短只读取快照 → 释放连接 await _judge_nodes → 短写事务复核合并。"""
    with get_db_connection(db_path) as conn:
        nodes = _active_nodes(conn, grade_subject, l1, l2)
        raw_count = _raw_nodes_since_last_compaction(conn, grade_subject, l1, l2)
        db_key = _db_cache_key(conn)
        node_records = {
            str(row["node_id"]): _row_to_active_record(row) for row in nodes
        }
        last_node_id = str(nodes[-1]["node_id"]) if nodes else None
        pairs = _build_compaction_pairs(grade_subject, nodes) if len(nodes) >= 2 else []

    wm_key = (db_key, grade_subject, l1, l2)
    if len(node_records) < 2 or not pairs:
        if last_node_id is not None:
            _NO_MERGE_SCAN_WATERMARKS[wm_key] = (last_node_id, raw_count)
        return None

    redirected_in_mem: set[str] = set()
    confirmed_merges: list[tuple[str, str, str]] = []
    for _neg_sim, to_id, from_id in pairs:
        if to_id in redirected_in_mem or from_id in redirected_in_mem:
            continue
        to_rec = node_records[to_id]
        from_rec = node_records[from_id]
        payload = _judge_payload_from_records(
            "scoped_compaction",
            l1,
            l2,
            [from_rec.l3_operator, *from_rec.aliases],
            [to_rec],
        )
        verdict = await _judge_nodes(payload)
        if verdict.match_node_id != to_id:
            continue
        confirmed_merges.append((from_id, to_id, verdict.evidence))
        redirected_in_mem.add(from_id)
        to_rec.aliases = _merge_aliases(
            to_rec.aliases,
            to_rec.l3_operator,
            [from_rec.l3_operator, *from_rec.aliases],
        )

    if not confirmed_merges:
        if last_node_id is not None:
            _NO_MERGE_SCAN_WATERMARKS[wm_key] = (last_node_id, raw_count)
        return None

    batch_id: str | None = None
    with get_db_connection(db_path) as conn:
        for from_id, to_id, evidence in confirmed_merges:
            from_row = conn.execute(
                "SELECT * FROM knowledge_nodes WHERE node_id = ?", (from_id,)
            ).fetchone()
            to_row = conn.execute(
                "SELECT * FROM knowledge_nodes WHERE node_id = ?", (to_id,)
            ).fetchone()
            if (
                from_row is None
                or to_row is None
                or from_row["redirect_to_id"] is not None
                or to_row["redirect_to_id"] is not None
                or from_row["grade_subject"] != grade_subject
                or to_row["grade_subject"] != grade_subject
                or from_row["l1_module"] != l1
                or to_row["l1_module"] != l1
                or from_row["l2_entity"] != l2
                or to_row["l2_entity"] != l2
            ):
                continue
            if batch_id is None:
                batch_id = str(uuid.uuid4())
            _merge_pair(conn, grade_subject, l1, l2, from_id, to_id, batch_id, evidence)

        if batch_id is not None:
            _NO_MERGE_SCAN_WATERMARKS.pop(wm_key, None)
        elif last_node_id is not None:
            _NO_MERGE_SCAN_WATERMARKS[wm_key] = (last_node_id, raw_count)
    return batch_id


def _is_redirected(conn: sqlite3.Connection, node_id: str) -> bool:
    return _node_row(conn, node_id)["redirect_to_id"] is not None


def _merge_pair(
    conn: sqlite3.Connection,
    grade_subject: str,
    l1: str,
    l2: str,
    from_id: str,
    to_id: str,
    batch_id: str,
    evidence: str,
) -> None:
    """单对合并（单 SAVEPOINT 事务）：路径压缩 -> 标红边归并 -> 审计快照。"""
    _assert_same_scope(conn, grade_subject, [from_id, to_id], l1, l2)
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
        _rewrite_edges_after_merge(conn, grade_subject, edge_snapshot)
        # 4) 审计快照（batch_id 同批共用 UUID；P10-C 落 grade_subject）
        conn.execute(
            "INSERT INTO compaction_audit_ledger (batch_id, grade_subject, l1_module,"
            " l2_entity, from_node_id, to_node_id, ai_judge_evidence,"
            " previous_aliases_to_json, affected_child_node_ids_json,"
            " previous_co_errors_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                grade_subject,
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
        grade=grade_subject,
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
    conn: sqlite3.Connection, grade_subject: str, edge_snapshot: list[dict[str, Any]]
) -> None:
    """按合并前快照逐边重算规范化端点 (u', v') = (resolve(u), resolve(v))。

    P10-C：解析限同学段；迁移新键边 INSERT 落 grade_subject（两端点同学段由构造保证）。
    """
    resolver = CanonicalNodeResolver(conn, grade_subject)
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
                "INSERT INTO concept_co_errors (node_u_id, node_v_id, grade_subject,"
                " l1_module_u, l1_module_v, active_error_count, total_historical_count,"
                " source_question_uids_json, last_error_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key[0],
                    key[1],
                    grade_subject,
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


def _canonical_edge_key(u: str, v: str) -> tuple[str, str]:
    return (u, v) if u <= v else (v, u)


def _assert_no_newer_dependent_batches(
    conn: sqlite3.Connection, batch_id: str, rows: Sequence[sqlite3.Row]
) -> None:
    """LIFO 依赖守卫：若存在触及本批次节点或快照边端点的较新未回滚合并，拒绝回滚。"""
    max_batch_merge_id = max(int(row["merge_id"]) for row in rows)
    batch_touched_nodes: set[str] = set()
    for row in rows:
        batch_touched_nodes.add(str(row["from_node_id"]))
        batch_touched_nodes.add(str(row["to_node_id"]))
        for child in json.loads(row["affected_child_node_ids_json"]):
            batch_touched_nodes.add(str(child))
        for edge in json.loads(row["previous_co_errors_snapshot_json"]):
            batch_touched_nodes.add(str(edge["node_u_id"]))
            batch_touched_nodes.add(str(edge["node_v_id"]))

    newer_rows = conn.execute(
        "SELECT batch_id, from_node_id, to_node_id, affected_child_node_ids_json"
        " FROM compaction_audit_ledger"
        " WHERE is_reverted = 0 AND batch_id != ? AND merge_id > ?"
        " ORDER BY merge_id ASC",
        (batch_id, max_batch_merge_id),
    ).fetchall()
    for newer in newer_rows:
        newer_nodes = {
            str(newer["from_node_id"]),
            str(newer["to_node_id"]),
            *(str(c) for c in json.loads(newer["affected_child_node_ids_json"])),
        }
        if batch_touched_nodes & newer_nodes:
            raise ValueError(
                f"无法回滚批次 {batch_id!r}：存在依赖相关节点的较新未回滚批次"
                f" {newer['batch_id']!r}，请先回滚较新依赖批次"
            )


def _insert_or_replace_co_error(
    conn: sqlite3.Connection, grade: str, edge: dict[str, Any]
) -> None:
    # P10-C：快照含 grade_subject 列自然逐字段恢复；P10-C 前的旧快照无此键时
    # 回退批次行学段（P10-A 已回填），绝不丢失分区归属。
    edge_grade = str(edge.get("grade_subject", grade))
    conn.execute(
        "INSERT OR REPLACE INTO concept_co_errors (node_u_id, node_v_id, grade_subject,"
        " l1_module_u, l1_module_v, active_error_count, total_historical_count,"
        " source_question_uids_json, last_error_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge["node_u_id"],
            edge["node_v_id"],
            edge_grade,
            edge["l1_module_u"],
            edge["l1_module_v"],
            edge["active_error_count"],
            edge["total_historical_count"],
            edge["source_question_uids_json"],
            edge["last_error_at"],
        ),
    )


def revert_compaction_batch(conn: sqlite3.Connection, batch_id: str) -> None:
    """按 merge_id 逆序精确恢复批次内全部合并；已回滚批次幂等无操作。

    恢复内容：from.redirect=NULL、affected 子节点指回 from、to.aliases 还原、
    标红边增量差值还原（保留合并后新增边与后增量计数/时间戳）。Elo 账本完全不动。
    未知/已回滚 batch_id 幂等无操作；存在依赖本批次节点/边端点的较新未回滚批次时抛 ValueError。
    """
    rows = conn.execute(
        "SELECT * FROM compaction_audit_ledger WHERE batch_id = ? ORDER BY merge_id DESC",
        (batch_id,),
    ).fetchall()
    if not rows or all(row["is_reverted"] for row in rows):
        log_event(logger, "graph.reverted", batch_id=batch_id, merges=0)
        return
    _assert_no_newer_dependent_batches(conn, batch_id, rows)
    reverted = 0
    for row in rows:
        if row["is_reverted"]:
            continue
        with _savepoint(conn):
            _revert_single_merge(conn, row)
        reverted += 1
    log_event(logger, "graph.reverted", batch_id=batch_id, merges=reverted)


def _revert_single_merge(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    grade = str(row["grade_subject"])  # P10-C：学段自批次行自派生（按 batch 操作签名不变）
    l1, l2 = str(row["l1_module"]), str(row["l2_entity"])
    from_id, to_id = str(row["from_node_id"]), str(row["to_node_id"])
    _assert_same_scope(conn, grade, [from_id, to_id], l1, l2)
    children = [str(c) for c in json.loads(row["affected_child_node_ids_json"])]

    # 标红边增量恢复：仅扣除 src_rows 对 to_id 外部边的贡献，保留合并后新边与后增量
    src_endpoints = {from_id, *children}
    snapshot_edges: list[dict[str, Any]] = json.loads(row["previous_co_errors_snapshot_json"])
    src_rows: list[dict[str, Any]] = []
    to_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in snapshot_edges:
        u, v = str(edge["node_u_id"]), str(edge["node_v_id"])
        if u in src_endpoints or v in src_endpoints:
            src_rows.append(edge)
        else:
            to_rows[_canonical_edge_key(u, v)] = edge

    def _map_to(node_id: str) -> str:
        return to_id if node_id in src_endpoints else node_id

    grouped_external: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in src_rows:
        mu, mv = _canonical_edge_key(
            _map_to(str(edge["node_u_id"])), _map_to(str(edge["node_v_id"]))
        )
        if mu == mv:
            continue  # 合并时被消除的自环内边，稍后直接原样恢复
        grouped_external.setdefault((mu, mv), []).append(edge)

    for (mu, mv), src_group in grouped_external.items():
        current = conn.execute(
            "SELECT * FROM concept_co_errors WHERE node_u_id = ? AND node_v_id = ?",
            (mu, mv),
        ).fetchone()
        if current is None:
            continue
        contrib_active = sum(int(e["active_error_count"]) for e in src_group)
        contrib_hist = sum(int(e["total_historical_count"]) for e in src_group)
        contrib_uids: set[str] = set()
        for e in src_group:
            contrib_uids.update(str(u) for u in json.loads(e["source_question_uids_json"]))

        pre_to = to_rows.get((mu, mv))
        pre_to_uids = (
            {str(u) for u in json.loads(pre_to["source_question_uids_json"])}
            if pre_to is not None
            else set()
        )
        uids_to_remove = contrib_uids - pre_to_uids
        current_uids = [str(u) for u in json.loads(current["source_question_uids_json"])]
        remaining_uids = [u for u in current_uids if u not in uids_to_remove]
        remaining_active = max(0, int(current["active_error_count"]) - contrib_active)
        remaining_hist = max(0, int(current["total_historical_count"]) - contrib_hist)

        if remaining_hist <= 0 and not remaining_uids:
            conn.execute(
                "DELETE FROM concept_co_errors WHERE node_u_id = ? AND node_v_id = ?",
                (mu, mv),
            )
            continue

        pre_merged_max_ts = max(
            str(r["last_error_at"]) for r in [*src_group, *([pre_to] if pre_to is not None else [])]
        )
        if pre_to is not None and str(current["last_error_at"]) == pre_merged_max_ts:
            restored_ts = str(pre_to["last_error_at"])
        else:
            restored_ts = str(current["last_error_at"])

        conn.execute(
            "UPDATE concept_co_errors SET active_error_count = ?,"
            " total_historical_count = ?, source_question_uids_json = ?,"
            " last_error_at = ? WHERE node_u_id = ? AND node_v_id = ?",
            (
                remaining_active,
                remaining_hist,
                json.dumps(remaining_uids, ensure_ascii=False),
                restored_ts,
                mu,
                mv,
            ),
        )

    src_ids = sorted(src_endpoints)
    placeholders = ", ".join("?" for _ in src_ids)
    conn.execute(
        f"DELETE FROM concept_co_errors WHERE node_u_id IN ({placeholders})"
        f" OR node_v_id IN ({placeholders})",
        [*src_ids, *src_ids],
    )
    for edge in src_rows:
        _insert_or_replace_co_error(conn, grade, edge)

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


def all_node_states(conn: sqlite3.Connection, grade_subject: str) -> dict[str, str]:
    """批量聚合学段内全部活跃主节点展示状态（O(N+M) 单查询，消除接口 N+1 全表扫描）。

    P10-C：活跃节点与挂载步骤均限同学段（JOIN 过滤）。
    """
    resolver = CanonicalNodeResolver(conn, grade_subject)
    states: dict[str, str] = {
        str(row[0]): "MASTERED"
        for row in conn.execute(
            "SELECT node_id FROM knowledge_nodes"
            " WHERE redirect_to_id IS NULL AND grade_subject = ?",
            (grade_subject,),
        )
    }
    rows = conn.execute(
        "SELECT s.knowledge_node_id, q.mastery_state FROM question_steps s"
        " JOIN questions q ON q.question_uid = s.question_uid"
        " JOIN knowledge_nodes kn ON kn.node_id = s.knowledge_node_id"
        " WHERE q.origin_type = 'USER_CANONICAL' AND s.is_blocked_step = 1"
        " AND kn.grade_subject = ?",
        (grade_subject,),
    ).fetchall()
    for row in rows:
        root = resolver.resolve(str(row["knowledge_node_id"]))
        states[root] = _pessimistic(states.get(root, "MASTERED"), str(row["mastery_state"]))
    return states


def node_state(conn: sqlite3.Connection, node_id: str) -> str:
    """悲观半格聚合：仅 USER_CANONICAL 且 is_blocked_step=1 步骤所属题目状态。

    含重定向子节点挂载的步骤；无此类步骤 -> MASTERED（仅已掌握前缀步骤的节点
    保持绿色）。优先级 ACTIVE > PENDING_RETEST > MASTERED。
    P10-C：签名不变，学段自节点行自派生，聚合限同学段。
    """
    grade = str(_node_row(conn, node_id)["grade_subject"])
    resolver = CanonicalNodeResolver(conn, grade)
    root = resolver.resolve(node_id)
    rows = conn.execute(
        "SELECT s.knowledge_node_id, q.mastery_state FROM question_steps s"
        " JOIN questions q ON q.question_uid = s.question_uid"
        " JOIN knowledge_nodes kn ON kn.node_id = s.knowledge_node_id"
        " WHERE q.origin_type = 'USER_CANONICAL' AND s.is_blocked_step = 1"
        " AND kn.grade_subject = ?",
        (grade,),
    ).fetchall()
    state = "MASTERED"
    for row in rows:
        if resolver.resolve(str(row["knowledge_node_id"])) == root:
            state = _pessimistic(state, str(row["mastery_state"]))
    return state


# ---------------------------------------------------------------------------
# 跨考点标红边生命周期
# ---------------------------------------------------------------------------


def _question_grade(conn: sqlite3.Connection, question_uid: str) -> str:
    """P10-C：题目学段（questions.target_grade_and_subject，uid 操作函数的自派生源）。"""
    row = conn.execute(
        "SELECT target_grade_and_subject FROM questions WHERE question_uid = ?",
        (question_uid,),
    ).fetchone()
    if row is None:
        raise KeyError(f"questions 中不存在题目: {question_uid!r}")
    return str(row[0])


def _question_canonical_nodes(conn: sqlite3.Connection, question_uid: str) -> list[str]:
    """P10-C：学段自题目行自派生，解析限同学段。"""
    resolver = CanonicalNodeResolver(conn, _question_grade(conn, question_uid))
    rows = conn.execute(
        "SELECT DISTINCT knowledge_node_id FROM question_steps WHERE question_uid = ?",
        (question_uid,),
    ).fetchall()
    return sorted({resolver.resolve(str(row[0])) for row in rows})


def record_co_error_edges(conn: sqlite3.Connection, question_uid: str) -> None:
    """该题 >=2 个不同 resolve 后节点 -> 两两边 (u<v)：active+1、historical+1、uids 并入。

    P10-C：签名不变，学段自题目行自派生并落边（INSERT/UPDATE 均写 grade_subject；
    两端点同学段由构造保证：步骤挂载同学段节点，解析器限同学段）。
    """
    nodes = _question_canonical_nodes(conn, question_uid)
    if len(nodes) < 2:
        return
    grade = _question_grade(conn, question_uid)
    for u, v in combinations(nodes, 2):  # nodes 已排序 -> 天然 u < v，DB CHECK 兜底
        existing = conn.execute(
            "SELECT * FROM concept_co_errors WHERE node_u_id = ? AND node_v_id = ?",
            (u, v),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO concept_co_errors (node_u_id, node_v_id, grade_subject,"
                " l1_module_u, l1_module_v, active_error_count, total_historical_count,"
                " source_question_uids_json) VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
                (
                    u,
                    v,
                    grade,
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
                " source_question_uids_json = ?, grade_subject = ?,"
                " last_error_at = CURRENT_TIMESTAMP"
                " WHERE node_u_id = ? AND node_v_id = ?",
                (json.dumps(uids, ensure_ascii=False), grade, u, v),
            )
    log_event(
        logger,
        "graph.co_error",
        action="record",
        question_uid=question_uid,
        grade=grade,
        nodes=nodes,
        edges=len(nodes) * (len(nodes) - 1) // 2,
    )


def decrement_co_error_edges_on_mastery(conn: sqlite3.Connection, question_uid: str) -> None:
    """销账时对应边 active-1（下限 0）；historical 与 uids 溯源不动。

    P10-C：按 uid 操作签名不变，学段经 _question_canonical_nodes 自题目行自派生。
    """
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


def compute_graph_metrics(
    conn: sqlite3.Connection, grade_subject: str, l1_module: str
) -> GraphMetricsDTO:
    """按 (学段, L1 模块) 独立核算双指标（设计 §3.4 第 3 条，冷启动无除零 NaN）。

    - MasteryRate = (mastered + 0.5 * pending) / total * 100，仅统计挂载
      USER_CANONICAL 题目的活跃主节点（含经重定向子节点挂载）。
    - Convergence = rho * (0.4 + 0.6 * eta) * 100：rho 取最近 W=20 道题的复用
      比例（w<5 -> 0）；eta 为节点步骤分布归一化熵（N<3 -> 0，0 ln 0 = 0）。
    P10-C：节点/步骤/最近 W=20 题统计全部限 (grade_subject, l1_module) 分区。
    """
    active_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT node_id FROM knowledge_nodes WHERE grade_subject = ? AND l1_module = ?"
            " AND redirect_to_id IS NULL ORDER BY node_id",
            (grade_subject, l1_module),
        )
    ]
    n = len(active_ids)
    resolver = CanonicalNodeResolver(conn, grade_subject)
    module_by_node = {
        str(row["node_id"]): str(row["l1_module"])
        for row in conn.execute(
            "SELECT node_id, l1_module FROM knowledge_nodes WHERE grade_subject = ?",
            (grade_subject,),
        )
    }

    mounted: set[str] = set()
    blocked_states: dict[str, str] = {}
    step_counts: Counter[str] = Counter()
    step_rows = conn.execute(
        "SELECT s.knowledge_node_id, s.is_blocked_step, q.mastery_state"
        " FROM question_steps s JOIN questions q ON q.question_uid = s.question_uid"
        " JOIN knowledge_nodes kn ON kn.node_id = s.knowledge_node_id"
        " WHERE q.origin_type = 'USER_CANONICAL' AND kn.grade_subject = ?",
        (grade_subject,),
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
        " WHERE q.origin_type = 'USER_CANONICAL' AND q.target_grade_and_subject = ?"
        " AND q.primary_l1_module = ?"
        " ORDER BY q.created_at DESC, q.question_uid DESC LIMIT ?",
        (grade_subject, l1_module, _RECENT_WINDOW),
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
