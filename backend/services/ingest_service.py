"""录入落库服务：题干+草稿 → 三表持久化 + 步骤对齐确认卡（GateCardDTO）。

唯一权威规格：docs/designs/adaptive_practice_harness.md CUJ 1、§3.1 第 1/4 条、
Target File #5/#8；API 签名与落库流程按 PM 契约 docs/tasks/P2-D_ingest.md。

关键不变量：
- 单事务原子落库：knowledge_nodes / questions / question_steps / diagnostic_sessions
  的全部写入在同一连接事务内提交，任一步失败整体回滚，绝不残留半截题目；
- 幂等重入库：stem_hash 与草稿 CAS URI 均无增量 → 直接返回既有最新会话确认卡，
  不新建任何行、不调用 VLM；题干变更仅更新 stem_md/stem_hash/updated_at；
  携带新草稿截图 → 新建一条 diagnostic_sessions（设计 §3.1 第 1 条，旧会话保留；
  契约「相同 stem_hash 一律短路」与设计「补充新草稿即新建会话」冲突时以设计为准，
  故 stem_hash 相同但草稿有增量时仍新建会话）；
- 重入库绝不触碰 mastery_state / 盲测字段 / Elo 相关数据；
- 未掌握题录入阶段不结算 Elo（保底结算发生在 confirm-gate，P2-E 负责）；
  已掌握题（P7-A：mastered_claim 勾选 / 作答「已掌握」类短文本 / 正确答案代数等价 /
  initial_state='已掌握' 旧开关）在录入单事务内即结算：elo_engine.record_mastered_init
  写 MASTERED_INIT 账本事件（水位线门控/幂等键等既有 Elo 规则不变），同事务置
  mastery_state='MASTERED' + SESSION_COMPLETED + user_confirmed_gate=1 + 步骤全不阻断，
  不启动预热、不进入门禁练习流；本模块不写入 question_escrow_pool；
- 知识点节点经 knowledge_graph.match_or_create_l3_node 解析（同父 L2 邻域 AI 裁判
  复用/新建，别名扩充与满阈值自动 Compaction 由该服务负责），
  question_steps.reused_existing_node 按其返回的 reused 标记落库；
- 新错题（initial_state='错题' 且 error_category != 'ALREADY_MASTERED'）步骤落库后
  同事务内调 knowledge_graph.record_co_error_edges 生长跨考点标红边（CUJ 4；
  ≥2 不同 resolve 节点才建边由该服务内部判定）；重入库路径绝不重复调用；
- 确认卡 steps 取会话 matched_branch_id 对应分支（一题多解时对齐索引针对该分支），
  该分支无步骤行时回退 canonical。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from backend.db.schema import get_db_connection
from backend.observability import log_event
from backend.sandbox.sympy_verifier import answers_equivalent
from backend.services import elo_engine
from backend.services.cas_store import (
    ingest_image_bytes,
    normalize_markdown_with_frontmatter,
)
from backend.services.difficulty_rubric import (
    calculate_base_elo,
    clamp_substep_difficulty,
)
from backend.services.knowledge_graph import match_or_create_l3_node, record_co_error_edges
from backend.services.vlm_diagnosis import (
    DiagnosisPayload,
    DraftAlignmentResult,
    StepSkeletonItem,
    analyze_question_and_draft,
)

__all__ = ["GateCardDTO", "GateStepDTO", "ingest_question"]

logger = logging.getLogger(__name__)

GateStatus = Literal["completed", "blocked", "untouched"]
InitialState = Literal["错题", "已掌握"]

# P7-A：「已掌握」类声明作答集合（strip 后长度 ≤12 且命中集合 → 直接判掌握）
_MASTERED_TEXTS = frozenset({"已掌握", "我已掌握", "掌握了", "我会做"})


def _is_mastered_text(text: str) -> bool:
    """作答是否为「已掌握」类声明文本（strip 后长度 ≤12 且属于模块级常量集合）。"""
    stripped = text.strip()
    return len(stripped) <= 12 and stripped in _MASTERED_TEXTS


class GateStepDTO(BaseModel):
    """确认卡单步：步骤序号 / 子目标 / 对齐状态 / 前缀保底 Elo。"""

    step_index: int
    step_goal_desc: str
    status: GateStatus
    sub_elo: int


class GateCardDTO(BaseModel):
    """步骤对齐微确认卡（Step-Skeleton Diff Gate）数据。"""

    session_id: str
    question_uid: str
    steps: list[GateStepDTO]
    error_category: str
    trap_taxonomy_tag: str | None
    # 已完成前缀的保底水位（last_valid>=1 时取 steps[last_valid-1].sub_elo，否则 None）
    floor_preview_elo: int | None
    base_elo: int
    tier: str
    is_reimport: bool
    # P7-A 录入即掌握：单事务结算标记 + record_mastered_init 账本解释条（向后兼容默认值）
    auto_mastered: bool = False
    mastery_explanation: str | None = None


async def ingest_question(
    stem_md: str,
    stem_images: list[Path],
    draft_images: list[Path],
    target_grade: str,
    initial_state: InitialState = "错题",
    base_dir: Path | None = None,
    db_path: Path | None = None,
    user_answer: str = "",
    mastered_claim: bool = False,
) -> GateCardDTO:
    """录入一道错题/已掌握题：CAS 归档 → 幂等检查 → 诊断 → 单事务落库 → 确认卡。

    - 题干经 ``normalize_markdown_with_frontmatter`` 提取/注入不可变 question_uid，
      本地配图改写为 cas:// URI；题干与草稿图片全部归档 CAS（设计 §3.1 第 1 条）；
    - uid 已存在走幂等重入库分支（见模块 docstring 不变量）；
    - 新题调用 ``analyze_question_and_draft`` 单次 VLM 诊断，确定性 rubric 定级，
      经 knowledge_graph AI 裁判解析知识点节点后原子写入三表；新错题同事务内
      生长跨考点标红边（record_co_error_edges，CUJ 4）；
    - 掌握判定（P7-A，诊断产出标准答案后）：``mastered_claim`` 勾选 /
      ``initial_state='已掌握'`` / ``user_answer`` 为「已掌握」类短文本 /
      ``user_answer`` 与标准答案代数等价（asyncio.to_thread 沙盒判定，
      判定异常按未掌握处理并 logger.warning，绝不击穿录入）任一命中 →
      error_category='ALREADY_MASTERED'，录入单事务内 record_mastered_init 结算，
      返回 auto_mastered=True + mastery_explanation 账本解释条，跳过门禁练习流；
    - 未掌握路径行为与既有完全一致；重入库幂等短路/增量逻辑不变，不重复结算。

    Raises:
        LLMGatewayError: VLM 诊断失败（事务回滚，无残留）。
        ValueError: 诊断结果非法（空骨架等，事务回滚，无残留）。
    """
    normalized_md, uid, stem_hash = normalize_markdown_with_frontmatter(
        stem_md, None, target_grade, base_dir
    )
    # 题干配图同样归档 CAS（幂等写盘；Markdown 内引用改写由 normalize 负责）
    for image_path in stem_images:
        ingest_image_bytes(image_path.read_bytes())
    draft_uris = [ingest_image_bytes(p.read_bytes()) for p in draft_images]

    log_event(
        logger,
        "ingest.received",
        question_uid=uid,
        stem_hash=stem_hash,
        initial_state=initial_state,
        target_grade=target_grade,
        stem_excerpt=normalized_md,  # 内容字段经观测层统一截断 <= 120 字符
        stem_image_count=len(stem_images),
        draft_image_count=len(draft_images),
        mastered_claim=mastered_claim,
        user_answer=user_answer,
    )

    with get_db_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM questions WHERE question_uid = ?", (uid,)
        ).fetchone()
        if existing is not None:
            latest = _latest_session(conn, uid)
            stem_changed = existing["stem_hash"] != stem_hash
            has_new_drafts = _has_new_drafts(draft_uris, latest)
            if not stem_changed and not has_new_drafts:
                log_event(
                    logger,
                    "ingest.reimport",
                    question_uid=uid,
                    session_id=str(latest["session_id"]),
                    mode="short_circuit",
                    stem_changed=False,
                )
                return _gate_card_from_db(conn, existing, latest, is_reimport=True)
            if stem_changed and not has_new_drafts:
                conn.execute(
                    "UPDATE questions SET stem_md = ?, stem_hash = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
                    (normalized_md, stem_hash, uid),
                )
                log_event(
                    logger,
                    "ingest.reimport",
                    question_uid=uid,
                    session_id=str(latest["session_id"]),
                    mode="stem_updated",
                    stem_changed=True,
                )
                log_event(
                    logger,
                    "ingest.persisted",
                    question_uid=uid,
                    session_id=str(latest["session_id"]),
                    base_elo=int(existing["base_elo"]),
                    tier=str(existing["tier"]),
                    is_reimport=True,
                )
                return _gate_card_from_db(conn, existing, latest, is_reimport=True)
        tree = _build_l1_l2_tree(conn)

    # 释放数据库连接与 SQLite 锁后，再跨网络执行耗时 VLM 诊断与沙盒判定
    payload = await analyze_question_and_draft(
        normalized_md, stem_images, draft_images, target_grade, tree
    )

    if existing is not None:
        with get_db_connection(db_path) as conn:
            fresh_existing = conn.execute(
                "SELECT * FROM questions WHERE question_uid = ?", (uid,)
            ).fetchone()
            assert fresh_existing is not None
            return _persist_reimport_draft_session(
                conn,
                fresh_existing,
                payload=payload,
                normalized_md=normalized_md,
                stem_hash=stem_hash,
                draft_uris=draft_uris,
                initial_state=initial_state,
            )

    return await _ingest_new_question(
        db_path,
        uid=uid,
        payload=payload,
        normalized_md=normalized_md,
        stem_hash=stem_hash,
        draft_uris=draft_uris,
        target_grade=target_grade,
        initial_state=initial_state,
        user_answer=user_answer,
        mastered_claim=mastered_claim,
    )


# ---------------------------------------------------------------------------
# 新题落库
# ---------------------------------------------------------------------------


async def _ingest_new_question(
    db_path: Path | None,
    *,
    uid: str,
    payload: DiagnosisPayload,
    normalized_md: str,
    stem_hash: str,
    draft_uris: list[str],
    target_grade: str,
    initial_state: InitialState,
    user_answer: str,
    mastered_claim: bool,
) -> GateCardDTO:
    """新题全链路：校验诊断结论与掌握判定（无锁） → rubric 定级 → 三表单事务原子写入。

    P7-A 掌握判定命中时，落库后同事务内 record_mastered_init 结算（录入即掌握）。
    """
    trace = payload.trace
    alignment = payload.alignment
    if not trace.steps:
        raise ValueError("诊断 trace.steps 不能为空")

    # P7-A 掌握判定：勾选 / 旧开关 / 「已掌握」类短文本 → 直接掌握；
    # 否则非空作答走沙盒代数等价判定（to_thread 避免阻塞事件循环；
    # 判定异常不得击穿录入 → 按未掌握处理并 warning）。
    mastered = mastered_claim or initial_state == "已掌握" or _is_mastered_text(user_answer)
    if not mastered and user_answer.strip():
        try:
            mastered = await asyncio.to_thread(
                answers_equivalent, user_answer, trace.standard_answer_latex
            )
        except Exception:
            logger.warning(
                "答案等价判定异常，按未掌握处理: question_uid=%s", uid, exc_info=True
            )
            mastered = False

    rubric = calculate_base_elo(trace.c_score, trace.s_score, trace.t_score, trace.l_score)
    base_elo = int(rubric["base_elo"])
    tier = str(rubric["tier"])
    parent_cstl = (trace.c_score, trace.s_score, trace.t_score, trace.l_score)

    log_event(
        logger,
        "ingest.diagnosis",
        question_uid=uid,
        base_elo=base_elo,
        tier=tier,
        error_category=alignment.error_category,
        stalled_step=alignment.stalled_step_index,
        step_count=len(trace.steps),
    )

    with get_db_connection(db_path) as conn:
        branches: list[tuple[str, list[StepSkeletonItem]]] = [("canonical", trace.steps)]
        if alignment.alt_branch_steps:
            branches.append(("user_alt_1", alignment.alt_branch_steps))
        resolved_steps: list[
            tuple[str, StepSkeletonItem, str, bool, tuple[int, int, int, int], int]
        ] = []
        for branch_id, steps in branches:
            for step in steps:
                sub_cstl, sub_elo = clamp_substep_difficulty(
                    parent_cstl, base_elo, (step.c_sub, step.s_sub, step.t_sub, step.l_sub)
                )
                # 诊断 Schema 暂无别名字段，candidate_aliases 传 []
                node_id, reused = await match_or_create_l3_node(
                    conn, step.l1_module, step.l2_entity, step.l3_operator, []
                )
                resolved_steps.append((branch_id, step, node_id, reused, sub_cstl, sub_elo))

        conn.execute(
            """INSERT INTO questions (
                question_uid, origin_type, target_grade_and_subject, stem_md, stem_hash,
                standard_answer_latex, c_score, s_score, t_score, l_score, base_elo, tier,
                trap_tags_json, primary_l1_module, mastery_state, verification_status
            ) VALUES (?, 'USER_CANONICAL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'ACTIVE', 'HUMAN_OR_CANONICAL')""",
            (
                uid,
                target_grade,
                normalized_md,
                stem_hash,
                trace.standard_answer_latex,
                trace.c_score,
                trace.s_score,
                trace.t_score,
                trace.l_score,
                base_elo,
                tier,
                json.dumps(trace.trap_tags, ensure_ascii=False),
                trace.steps[0].l1_module,
            ),
        )
        for branch_id, step, node_id, reused, sub_cstl, sub_elo in resolved_steps:
            _insert_step_row(
                conn,
                uid,
                branch_id,
                step,
                node_id,
                reused,
                sub_cstl,
                sub_elo,
                alignment.stalled_step_index,
            )

        session_id = _insert_session(
            conn, uid, draft_uris, alignment, initial_state, mastered=mastered
        )
        mastery_explanation: str | None = None
        if mastered:
            # 录入即结算（单事务）：参照 scaffold_engine.confirm_diagnostic_gate
            # ALREADY_MASTERED 分支；水位线门控/幂等键等既有 Elo 规则不变。
            result = elo_engine.record_mastered_init(conn, uid, session_id)
            mastery_explanation = result.explanation_text
            conn.execute(
                "UPDATE questions SET mastery_state = 'MASTERED', "
                "updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
                (uid,),
            )
            conn.execute(
                "UPDATE diagnostic_sessions SET micro_stage = 'SESSION_COMPLETED', "
                "user_confirmed_gate = 1, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                (session_id,),
            )
            conn.execute(
                "UPDATE question_steps SET is_blocked_step = 0 WHERE question_uid = ?",
                (uid,),
            )
            log_event(
                logger,
                "ingest.auto_mastered",
                question_uid=uid,
                session_id=session_id,
                mastered_claim=mastered_claim,
                initial_state=initial_state,
                applied=result.applied,
            )
        question = conn.execute(
            "SELECT * FROM questions WHERE question_uid = ?", (uid,)
        ).fetchone()
        assert question is not None  # 刚写入，同事务内必然可见
        session = _latest_session(conn, uid)
        # CUJ 4：综合错题跨考点标红边生长（步骤落库后、同一事务内；
        # ≥2 不同 resolve 节点才建边由 record_co_error_edges 内部判定）。
        # 重入库路径不经过此处，天然不重复递增。
        if initial_state == "错题" and session["error_category"] != "ALREADY_MASTERED":
            record_co_error_edges(conn, uid)
        log_event(
            logger,
            "ingest.persisted",
            question_uid=uid,
            session_id=str(session["session_id"]),
            base_elo=base_elo,
            tier=tier,
            is_reimport=False,
            auto_mastered=mastered,
        )
        return _gate_card_from_db(
            conn,
            question,
            session,
            is_reimport=False,
            auto_mastered=mastered,
            mastery_explanation=mastery_explanation,
        )


# ---------------------------------------------------------------------------
# 幂等重入库
# ---------------------------------------------------------------------------


def _persist_reimport_draft_session(
    conn: sqlite3.Connection,
    existing: sqlite3.Row,
    *,
    payload: DiagnosisPayload,
    normalized_md: str,
    stem_hash: str,
    draft_uris: list[str],
    initial_state: InitialState,
) -> GateCardDTO:
    """携带新草稿重入库写库事务：题干更新（若变更）+ 新建会话（绝不跨 VLM 持锁）。"""
    uid = str(existing["question_uid"])
    stem_changed = existing["stem_hash"] != stem_hash
    if stem_changed:
        conn.execute(
            "UPDATE questions SET stem_md = ?, stem_hash = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
            (normalized_md, stem_hash, uid),
        )
    log_event(
        logger,
        "ingest.diagnosis",
        question_uid=uid,
        base_elo=int(existing["base_elo"]),
        tier=str(existing["tier"]),
        error_category=payload.alignment.error_category,
        stalled_step=payload.alignment.stalled_step_index,
        step_count=len(payload.trace.steps),
    )
    _insert_session(conn, uid, draft_uris, payload.alignment, initial_state)
    new_session = _latest_session(conn, uid)
    log_event(
        logger,
        "ingest.reimport",
        question_uid=uid,
        session_id=str(new_session["session_id"]),
        mode="new_draft_session",
        stem_changed=stem_changed,
    )
    log_event(
        logger,
        "ingest.persisted",
        question_uid=uid,
        session_id=str(new_session["session_id"]),
        base_elo=int(existing["base_elo"]),
        tier=str(existing["tier"]),
        is_reimport=True,
    )
    return _gate_card_from_db(conn, existing, new_session, is_reimport=True)


def _has_new_drafts(draft_uris: list[str], latest_session: sqlite3.Row) -> bool:
    """本次草稿是否含最新会话未见过的 CAS URI（设计 §3.1 第 1 条「补充了新草稿截图」）。"""
    if not draft_uris:
        return False
    old_uris: set[str] = set(json.loads(latest_session["draft_image_cas_uris_json"]))
    return any(uri not in old_uris for uri in draft_uris)


# ---------------------------------------------------------------------------
# 知识点骨架树（节点解析复用 knowledge_graph.match_or_create_l3_node）
# ---------------------------------------------------------------------------


def _build_l1_l2_tree(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """由活跃节点（redirect_to_id IS NULL）构造既有 L1/L2 骨架树注入 Prompt。"""
    tree: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT DISTINCT l1_module, l2_entity FROM knowledge_nodes "
        "WHERE redirect_to_id IS NULL ORDER BY l1_module, l2_entity"
    ).fetchall()
    for row in rows:
        tree.setdefault(row["l1_module"], []).append(row["l2_entity"])
    return tree


# ---------------------------------------------------------------------------
# 行写入
# ---------------------------------------------------------------------------


def _insert_step_row(
    conn: sqlite3.Connection,
    question_uid: str,
    branch_id: str,
    step: StepSkeletonItem,
    knowledge_node_id: str,
    reused_existing_node: bool,
    sub_cstl: tuple[int, int, int, int],
    sub_elo: int,
    stalled_step_index: int,
) -> None:
    """写入单步骨架行：is_blocked_step = step_index >= stalled_step_index。"""
    conn.execute(
        """INSERT INTO question_steps (
            step_id, question_uid, solution_branch_id, step_index, step_goal_desc,
            step_formula_latex, knowledge_node_id, masked_conditions_json, probe_question_md,
            is_blocked_step, reused_existing_node, c_sub, s_sub, t_sub, l_sub, sub_elo
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            question_uid,
            branch_id,
            step.step_index,
            step.step_goal_desc,
            step.step_formula_latex,
            knowledge_node_id,
            json.dumps(step.masked_conditions, ensure_ascii=False),
            step.probe_question_md,
            1 if step.step_index >= stalled_step_index else 0,
            1 if reused_existing_node else 0,
            sub_cstl[0],
            sub_cstl[1],
            sub_cstl[2],
            sub_cstl[3],
            sub_elo,
        ),
    )


def _insert_session(
    conn: sqlite3.Connection,
    question_uid: str,
    draft_uris: list[str],
    alignment: DraftAlignmentResult,
    initial_state: InitialState,
    *,
    mastered: bool = False,
) -> str:
    """新建诊断会话：STAGE_0_CONFIRM_GATE / prewarm IDLE / 门禁未确认。

    initial_state='已掌握' 或 P7-A 掌握判定命中（mastered）时强制
    error_category='ALREADY_MASTERED'（标签置空），否则取 alignment 结论。
    掌握会话的 SESSION_COMPLETED / user_confirmed_gate=1 由调用方同事务结算写入。
    返回 session_id。
    """
    session_id = str(uuid.uuid4())
    if initial_state == "已掌握" or mastered:
        error_category = "ALREADY_MASTERED"
        trap_tag: str | None = None
    else:
        error_category = alignment.error_category
        trap_tag = alignment.trap_taxonomy_tag
    conn.execute(
        """INSERT INTO diagnostic_sessions (
            session_id, question_uid, draft_image_cas_uris_json, draft_steps_latex_json,
            matched_branch_id, last_valid_step_index, stalled_step_index,
            error_category, trap_taxonomy_tag, micro_stage, prewarm_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'STAGE_0_CONFIRM_GATE', 'IDLE')""",
        (
            session_id,
            question_uid,
            json.dumps(draft_uris, ensure_ascii=False),
            json.dumps(alignment.draft_steps_latex, ensure_ascii=False),
            alignment.matched_branch_id,
            alignment.last_valid_step_index,
            alignment.stalled_step_index,
            error_category,
            trap_tag,
        ),
    )
    return session_id


# ---------------------------------------------------------------------------
# 确认卡组装
# ---------------------------------------------------------------------------


def _latest_session(conn: sqlite3.Connection, question_uid: str) -> sqlite3.Row:
    """取题目最新会话（created_at 秒级精度，rowid 作同秒兜底排序）。"""
    row = conn.execute(
        "SELECT * FROM diagnostic_sessions WHERE question_uid = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (question_uid,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"题目 {question_uid} 缺少诊断会话（原子落库下不应发生）")
    return row


def _fetch_branch_steps(
    conn: sqlite3.Connection, question_uid: str, branch_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT step_index, step_goal_desc, sub_elo FROM question_steps "
        "WHERE question_uid = ? AND solution_branch_id = ? ORDER BY step_index",
        (question_uid, branch_id),
    ).fetchall()


def _derive_status(step_index: int, last_valid: int, stalled: int) -> GateStatus:
    """status 推导：<=last_valid → completed；==stalled → blocked；其余 untouched。"""
    if step_index <= last_valid:
        return "completed"
    if step_index == stalled:
        return "blocked"
    return "untouched"


def _gate_card_from_db(
    conn: sqlite3.Connection,
    question: sqlite3.Row,
    session: sqlite3.Row,
    *,
    is_reimport: bool,
    auto_mastered: bool = False,
    mastery_explanation: str | None = None,
) -> GateCardDTO:
    """由落库行组装确认卡：steps 取 matched 分支（缺失回退 canonical）。

    auto_mastered（P7-A 录入即结算）时步骤全 completed、floor_preview_elo=None；
    重入库路径不传该标记，组装行为与既有完全一致（零漂移）。
    """
    uid = question["question_uid"]
    branch_id = session["matched_branch_id"]
    step_rows = _fetch_branch_steps(conn, uid, branch_id)
    if not step_rows and branch_id != "canonical":
        step_rows = _fetch_branch_steps(conn, uid, "canonical")

    last_valid = session["last_valid_step_index"]
    stalled = session["stalled_step_index"]
    steps = [
        GateStepDTO(
            step_index=row["step_index"],
            step_goal_desc=row["step_goal_desc"],
            status=(
                "completed"
                if auto_mastered
                else _derive_status(row["step_index"], last_valid, stalled)
            ),
            sub_elo=row["sub_elo"],
        )
        for row in step_rows
    ]
    floor_preview: int | None = None
    if not auto_mastered and last_valid >= 1:
        floor_preview = next((s.sub_elo for s in steps if s.step_index == last_valid), None)

    return GateCardDTO(
        session_id=session["session_id"],
        question_uid=uid,
        steps=steps,
        error_category=session["error_category"],
        trap_taxonomy_tag=session["trap_taxonomy_tag"],
        floor_preview_elo=floor_preview,
        base_elo=question["base_elo"],
        tier=question["tier"],
        is_reimport=is_reimport,
        auto_mastered=auto_mastered,
        mastery_explanation=mastery_explanation,
    )
