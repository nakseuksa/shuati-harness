"""每日盲测调度器：配额熔断 + 幂等题单锁 + 防剧透 + 双重核验 + 连带销账（Phase 3 切片 C）。

唯一权威规格：docs/designs/adaptive_practice_harness.md §3.3 第 2 条、CUJ 3、
Target File #12；任务契约 docs/tasks/P3-C_blind_drill.md。

职责边界（与 elo_engine / knowledge_graph 的分工）：
- 本模块只负责「排单 / 呈现题选择 / 答案核验 / 宏观 mastery_state 流转 / 重训分流」；
  双轨 Elo 账本与托管池一律委托 ``elo_engine``，标红边生命周期委托 ``knowledge_graph``。
- 所有函数接收调用方管理的 sqlite3.Connection：不自行开连接、不 commit；
  函数体内部以 SAVEPOINT 保证「全程单事务」的原子性（异常整体回滚）。

核心不变量：
- 同日题单幂等：``slated_drill_date == active_date`` 已存在任何锁定记录（含当日已
  作答而离开 PENDING_RETEST 的题）即幂等返回仍在待测的锁定题单，不重复递增曝光、
  不重复顺延；跨日才重新锁定并 ``slated_exposure_count += 1``。
- 溢出顺延：超出当日配额的到期题 unlock_at / due_at 各 +1 天、system_deferred_days
  +1，当日不排入 → ``apply_overdue_escrow_decay`` 的 slated 判据天然冻结其衰减。
- 防剧透：DTO 结构上不含 primary_l1_module / 知识点路径 / parent 字段；题干文本
  剥离 frontmatter、[L1]/[L2]/[L3] 路径标签行与来源行。
- 防背答案：优先呈现 blind_drill_attempts 中从未曝光过的 CLONE/ISOMORPHIC 变式；
  回退原题时强制「曾卡壳步中间子目标 + 最终答案」双正确方可销账。
- 连带销账：仅 SCAFFOLD_PRE（source=子题 A, target=母题 B）且 containment_score
  >= 0.85 且 A 仍为 PENDING_RETEST 的子题，随母题一次通过逐题独立兑现 escrow。
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date
from typing import Literal

from pydantic import BaseModel

from backend.config import settings
from backend.sandbox.sympy_verifier import verify_equation_equivalence
from backend.services import elo_engine, knowledge_graph

__all__ = [
    "BlindDrillItemDTO",
    "BlindDrillOutcomeDTO",
    "get_daily_blind_drill_queue",
    "evaluate_blind_drill_submission",
]

# 连带销账包含度阈值（设计 §3.3 第 2 条：I >= 0.85；设计常量，settings 未收纳）
_CASCADE_CONTAINMENT_MIN = 0.85

# 变式关系类型（无向边，双向检索）；SCAFFOLD_PRE 不在此列（那是前置子题边）
_VARIANT_RELATION_TYPES = ("CLONE", "ISOMORPHIC")


# ---------------------------------------------------------------------------
# DTO（防剧透：结构上禁止携带模块 / 知识点路径 / parent 信息）
# ---------------------------------------------------------------------------


class BlindDrillItemDTO(BaseModel):
    """每日盲测题单条目（已剥离来源与 [L1]/[L2]/[L3] 标签）。"""

    question_uid: str  # 原题 uid（销账主键）
    drill_question_uid: str  # 实际呈现题（变式或原题）
    stem_md: str  # 已剥离来源/L1/L2/L3 标签的呈现题题干
    verification_mode: Literal["final_answer", "equation_skeleton"]
    allowed_symbols: list[str]
    probe_question_md: str | None  # 仅回退原题时携带（曾卡壳步的一阶盲探反问句）
    requires_intermediate: bool


class BlindDrillOutcomeDTO(BaseModel):
    """盲测提交销账结果。"""

    passed: bool
    mastery_state: str
    retrain_stage: str | None  # 失败时新重训会话的起点
    cascaded_question_uids: list[str]  # 连带销账的子题
    explanation_text: str | None


# ---------------------------------------------------------------------------
# 内部工具：查询 / 小事务（调用方管理外层事务，本模块不 commit）
# ---------------------------------------------------------------------------


def _query_one(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> sqlite3.Row | None:
    cur = conn.execute(sql, params)
    cur.row_factory = sqlite3.Row
    return cur.fetchone()


def _query_all(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[sqlite3.Row]:
    cur = conn.execute(sql, params)
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


@contextmanager
def _savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """排单 / 销账的原子小事务：异常回滚到保存点，正常释放（不 commit）。"""
    name = f"blind_drill_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def _load_question(conn: sqlite3.Connection, question_uid: str) -> sqlite3.Row:
    row = _query_one(conn, "SELECT * FROM questions WHERE question_uid = ?", (question_uid,))
    if row is None:
        raise ValueError(f"题目不存在：question_uid={question_uid!r}")
    return row


def _latest_session(conn: sqlite3.Connection, question_uid: str) -> sqlite3.Row | None:
    """原会话：该题最近一条 diagnostic_sessions（rowid 兜底同秒创建时序）。"""
    return _query_one(
        conn,
        "SELECT * FROM diagnostic_sessions WHERE question_uid = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (question_uid,),
    )


def _stalled_step(
    conn: sqlite3.Connection, question_uid: str, session: sqlite3.Row | None
) -> sqlite3.Row | None:
    """曾卡壳步：原会话 stalled_step_index 对应的步骤（优先匹配分支）。"""
    if session is None:
        return None
    branch = str(session["matched_branch_id"])
    index = int(session["stalled_step_index"])
    row = _query_one(
        conn,
        "SELECT * FROM question_steps WHERE question_uid = ?"
        " AND solution_branch_id = ? AND step_index = ?",
        (question_uid, branch, index),
    )
    if row is None:
        row = _query_one(
            conn,
            "SELECT * FROM question_steps WHERE question_uid = ? AND step_index = ?"
            " ORDER BY solution_branch_id ASC LIMIT 1",
            (question_uid, index),
        )
    return row


# ---------------------------------------------------------------------------
# 防剧透：题干剥标签 + 符号集提取
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n(?:---|\.\.\.)\n?", re.DOTALL)
_PATH_TAG_LINE_RE = re.compile(r"^.*\[(?:L1|L2|L3)\].*$", re.MULTILINE)
_INLINE_PATH_TAG_RE = re.compile(r"\[/?L[123]\]")
_SOURCE_LINE_RE = re.compile(
    r"^\s*(?:【来源】|【原题来源】|来源[:：]|原题来源[:：]|source[:：]).*$",
    re.MULTILINE | re.IGNORECASE,
)
_META_LINE_RE = re.compile(r"^.*(?:question_uid|parent_question_uid|primary_l1_module).*$",
                           re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _strip_spoiler_tags(stem_md: str) -> str:
    """剥离原题来源与 [L1]/[L2]/[L3] 标签（设计 §3.3 第 2 条，接口层防剧透）。

    移除：YAML frontmatter（含 question_uid 等元数据）、含路径标签的整行、
    来源行、含 uid/parent/模块字段的元数据行、残留的内联路径标签。
    """
    text = stem_md.replace("\r\n", "\n").replace("\r", "\n")
    text = _FRONTMATTER_RE.sub("", text)
    text = _PATH_TAG_LINE_RE.sub("", text)
    text = _SOURCE_LINE_RE.sub("", text)
    text = _META_LINE_RE.sub("", text)
    text = _INLINE_PATH_TAG_RE.sub("", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


# LaTeX 希腊字母等「符号命令」白名单；\frac/\cos 等排版/函数命令不是未知量符号
_GREEK_SYMBOL_COMMANDS = frozenset(
    {
        "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta",
        "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu", "xi", "pi",
        "rho", "sigma", "tau", "upsilon", "phi", "varphi", "chi", "psi", "omega",
        "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon",
        "Phi", "Psi", "Omega",
    }
)
_LATEX_CMD_RE = re.compile(r"\\([a-zA-Z]+)")
_SINGLE_LETTER_RE = re.compile(r"(?<![A-Za-z])([A-Za-z])(?![A-Za-z])")


def _extract_symbols(texts: Iterable[str]) -> list[str]:
    """从题干/步骤公式提取符号集：希腊字母命令名 + 独立拉丁字母（确定性排序）。"""
    symbols: set[str] = set()
    for text in texts:
        for cmd in _LATEX_CMD_RE.findall(text):
            if cmd in _GREEK_SYMBOL_COMMANDS:
                symbols.add(cmd)
        cleaned = _LATEX_CMD_RE.sub(" ", text)
        symbols.update(_SINGLE_LETTER_RE.findall(cleaned))
    return sorted(symbols)


# ---------------------------------------------------------------------------
# 呈现题选择（排单与提交重放共用同一确定性逻辑）
# ---------------------------------------------------------------------------


def _select_unexposed_variant(conn: sqlite3.Connection, question_uid: str) -> str | None:
    """优先选择 blind_drill_attempts 中从未曝光过的 CLONE/ISOMORPHIC 变式。

    变式边为无向（is_directed=0），双向检索；「未曝光过」判据 = 该变式 uid 从未
    作为本题的 drill_question_uid 出现在作答日志中。确定性排序：relation_type
    ASC（CLONE 先于 ISOMORPHIC）+ uid ASC，保证同日重放与排单选择一致。
    """
    row = _query_one(
        conn,
        "SELECT v.variant_uid FROM ("
        " SELECT r.target_question_uid AS variant_uid, r.relation_type"
        "  FROM question_relations r WHERE r.source_question_uid = ?"
        "  AND r.relation_type IN ('CLONE', 'ISOMORPHIC')"
        " UNION"
        " SELECT r.source_question_uid AS variant_uid, r.relation_type"
        "  FROM question_relations r WHERE r.target_question_uid = ?"
        "  AND r.relation_type IN ('CLONE', 'ISOMORPHIC')"
        ") v JOIN questions q ON q.question_uid = v.variant_uid"
        " WHERE v.variant_uid != ? AND NOT EXISTS ("
        "  SELECT 1 FROM blind_drill_attempts a"
        "  WHERE a.question_uid = ? AND a.drill_question_uid = v.variant_uid"
        " ) ORDER BY v.relation_type ASC, v.variant_uid ASC LIMIT 1",
        (question_uid, question_uid, question_uid, question_uid),
    )
    return str(row["variant_uid"]) if row is not None else None


def _build_item(conn: sqlite3.Connection, question: sqlite3.Row) -> BlindDrillItemDTO:
    """构造单条题单 DTO：变式优先，无可用变式回退原题（携带卡壳步 probe）。

    verification_mode 由原题运算负荷决定：L>=2 → equation_skeleton（allowed_symbols
    从原题题干与步骤公式提取符号集）；L=1 → final_answer。
    """
    orig_uid = str(question["question_uid"])
    session = _latest_session(conn, orig_uid)
    step = _stalled_step(conn, orig_uid, session)

    variant_uid = _select_unexposed_variant(conn, orig_uid)
    if variant_uid is not None:
        drill = _load_question(conn, variant_uid)
        probe: str | None = None
        requires_intermediate = False
    else:
        drill = question
        probe = str(step["probe_question_md"]) if step is not None else None
        requires_intermediate = True

    mode: Literal["final_answer", "equation_skeleton"] = (
        "equation_skeleton" if int(question["l_score"]) >= 2 else "final_answer"
    )
    allowed_symbols: list[str] = []
    if mode == "equation_skeleton":
        formulas = [
            str(r["step_formula_latex"])
            for r in _query_all(
                conn,
                "SELECT step_formula_latex FROM question_steps WHERE question_uid = ?"
                " ORDER BY solution_branch_id ASC, step_index ASC",
                (orig_uid,),
            )
        ]
        allowed_symbols = _extract_symbols([str(question["stem_md"]), *formulas])

    return BlindDrillItemDTO(
        question_uid=orig_uid,
        drill_question_uid=str(drill["question_uid"]),
        stem_md=_strip_spoiler_tags(str(drill["stem_md"])),
        verification_mode=mode,
        allowed_symbols=allowed_symbols,
        probe_question_md=probe,
        requires_intermediate=requires_intermediate,
    )


# ---------------------------------------------------------------------------
# 每日题单：幂等锁 + 配额熔断 + 溢出顺延 + 衰减接线
# ---------------------------------------------------------------------------


def get_daily_blind_drill_queue(
    conn: sqlite3.Connection, active_date: date, limit: int | None = None
) -> list[BlindDrillItemDTO]:
    """获取当日综合盲测题单（<= cap 道，已剥离剧透标签），全程单事务。

    - 当日已锁定（任何题 slated_drill_date == active_date，含已作答离队者）→
      幂等返回仍在 PENDING_RETEST 的锁定题单，不重复递增曝光、不重复顺延；
    - 否则选 PENDING_RETEST 且 blind_test_unlock_at <= active_date 的题，按
      unlock_at ASC 取前 cap 道：slated_drill_date=active_date、曝光 +1；
    - 溢出到期题 unlock_at/due_at 各 +1 天、system_deferred_days +1，当日不排入
      （未 slated → 衰减判据天然冻结）；
    - 锁定后接线 elo_engine.apply_overdue_escrow_decay（当日已排入且曝光 > 阈值
      的题按日衰减；函数自身按 (uid, date) 幂等，重复刷新不重复衰减）。

    limit 缺省 settings.DAILY_BLIND_DRILL_CAP；负数抛 ValueError。limit=0 时
    不产生锁定记录，所有到期题顺延（退化用法，同日重复调用会再次顺延）。
    """
    cap = settings.DAILY_BLIND_DRILL_CAP if limit is None else limit
    if cap < 0:
        raise ValueError(f"每日盲测配额不能为负：limit={cap}")
    day = active_date.isoformat()

    with _savepoint(conn):
        locked_any = _query_one(
            conn, "SELECT question_uid FROM questions WHERE slated_drill_date = ? LIMIT 1", (day,)
        )
        if locked_any is not None:
            # 当日已锁定 → 幂等返回仍在待测的锁定题单（已作答者自然离队）
            rows = _query_all(
                conn,
                "SELECT * FROM questions WHERE slated_drill_date = ?"
                " AND mastery_state = 'PENDING_RETEST'"
                " ORDER BY blind_test_unlock_at ASC, question_uid ASC",
                (day,),
            )
        else:
            due = _query_all(
                conn,
                "SELECT * FROM questions WHERE mastery_state = 'PENDING_RETEST'"
                " AND blind_test_unlock_at IS NOT NULL AND date(blind_test_unlock_at) <= date(?)"
                " ORDER BY blind_test_unlock_at ASC, question_uid ASC",
                (day,),
            )
            rows = due[:cap]
            for row in rows:
                conn.execute(
                    "UPDATE questions SET slated_drill_date = ?,"
                    " slated_exposure_count = slated_exposure_count + 1,"
                    " updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
                    (day, str(row["question_uid"])),
                )
            for row in due[cap:]:
                conn.execute(
                    "UPDATE questions SET"
                    " blind_test_unlock_at = date(blind_test_unlock_at, '+1 day'),"
                    " blind_test_due_at = date(blind_test_due_at, '+1 day'),"
                    " system_deferred_days = system_deferred_days + 1,"
                    " updated_at = CURRENT_TIMESTAMP WHERE question_uid = ?",
                    (str(row["question_uid"]),),
                )
        items = [_build_item(conn, row) for row in rows]
        # 衰减接线：仅当日已排入且曝光 > 阈值的题衰减；顺延题未 slated → 冻结
        elo_engine.apply_overdue_escrow_decay(conn, active_date)
    return items


# ---------------------------------------------------------------------------
# 提交销账：确定性重放 + 双重核验 + 连带销账 + 失败重训分流
# ---------------------------------------------------------------------------


def _cascade_mastered_children(conn: sqlite3.Connection, parent_uid: str) -> list[str]:
    """上位母题连带销账：SCAFFOLD_PRE(A→B) 且 I >= 0.85 且 A 为 PENDING_RETEST。

    逐题独立兑现各自 escrow（realize + MASTERED），并同步递减各自标红边
    （CUJ 3：知识网节点与跨考点组合边状态同步更新）。确定性按子题 uid ASC。
    """
    children = _query_all(
        conn,
        "SELECT c.question_uid FROM question_relations r"
        " JOIN questions c ON c.question_uid = r.source_question_uid"
        " WHERE r.target_question_uid = ? AND r.relation_type = 'SCAFFOLD_PRE'"
        " AND r.containment_score >= ? AND c.mastery_state = 'PENDING_RETEST'"
        " ORDER BY c.question_uid ASC",
        (parent_uid, _CASCADE_CONTAINMENT_MIN),
    )
    cascaded: list[str] = []
    for row in children:
        child_uid = str(row["question_uid"])
        child_session = _latest_session(conn, child_uid)
        session_id = (
            str(child_session["session_id"])
            if child_session is not None
            else f"blind-drill-cascade:{child_uid}"
        )
        elo_engine.realize_or_forfeit_blind_test(conn, child_uid, session_id, passed=True)
        conn.execute(
            "UPDATE questions SET mastery_state = 'MASTERED', updated_at = CURRENT_TIMESTAMP"
            " WHERE question_uid = ?",
            (child_uid,),
        )
        knowledge_graph.decrement_co_error_edges_on_mastery(conn, child_uid)
        cascaded.append(child_uid)
    return cascaded


def _create_retrain_session(
    conn: sqlite3.Connection,
    question_uid: str,
    origin_session: sqlite3.Row | None,
    stage: str,
) -> str:
    """新建重训 diagnostic_sessions：error_category 等诊断结论沿用原会话。"""
    new_id = f"retrain-{uuid.uuid4().hex}"
    if origin_session is not None:
        conn.execute(
            "INSERT INTO diagnostic_sessions ("
            " session_id, question_uid, draft_image_cas_uris_json, draft_steps_latex_json,"
            " matched_branch_id, last_valid_step_index, stalled_step_index, error_category,"
            " trap_taxonomy_tag, user_confirmed_gate, user_overridden, micro_stage"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)",
            (
                new_id,
                question_uid,
                str(origin_session["draft_image_cas_uris_json"]),
                str(origin_session["draft_steps_latex_json"]),
                str(origin_session["matched_branch_id"]),
                int(origin_session["last_valid_step_index"]),
                int(origin_session["stalled_step_index"]),
                str(origin_session["error_category"]),
                origin_session["trap_taxonomy_tag"],
                stage,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO diagnostic_sessions ("
            " session_id, question_uid, draft_image_cas_uris_json, draft_steps_latex_json,"
            " error_category, user_confirmed_gate, micro_stage"
            ") VALUES (?, ?, '[]', '[]', 'CONCEPT_BOTTLENECK', 1, ?)",
            (new_id, question_uid, stage),
        )
    return new_id


def evaluate_blind_drill_submission(
    conn: sqlite3.Connection,
    question_uid: str,
    final_answer: str,
    intermediate_sub_answer: str | None = None,
    active_date: date | None = None,
) -> BlindDrillOutcomeDTO:
    """提交盲测作答完成 🟡→🟢 销账或 🔴 重训分流，全程单事务。

    - 呈现题确定性重放：与排单共用 _build_item（同 active_date 同选择逻辑）；
      答案一律经 verify_equation_equivalence 代数等价判定。
    - 回退原题：中间量（对曾卡壳步 step_formula_latex）与最终答案双正确才 pass；
      中间量缺失/错误 → fail（即使最终对）。变式只核最终答案。
    - pass → realize 兑现 escrow + MASTERED + 标红边递减 + 合规子题连带销账；
    - fail → forfeit 清零 escrow + ACTIVE + 新建重训会话：中间量也错/缺 →
      STAGE_1_BLIND_PROBE；中间量对仅最终错 → STAGE_4_DECRUTCH_VARIANT。
    - 无论成败均写 blind_drill_attempts 作答日志（变式曝光追踪判据来源）。

    题目不存在或不在 PENDING_RETEST 状态抛 ValueError（回滚，不落任何写入）。
    active_date 缺省 date.today()。
    """
    day = active_date if active_date is not None else date.today()
    with _savepoint(conn):
        question = _load_question(conn, question_uid)
        state = str(question["mastery_state"])
        if state != "PENDING_RETEST":
            raise ValueError(
                f"题目 {question_uid!r} 当前宏观状态为 {state}，非 PENDING_RETEST，无法盲测销账"
            )

        item = _build_item(conn, question)  # 确定性重放呈现题
        drill = _load_question(conn, item.drill_question_uid)
        final_ok = verify_equation_equivalence(final_answer, str(drill["standard_answer_latex"]))

        session = _latest_session(conn, question_uid)
        step = _stalled_step(conn, question_uid, session)
        intermediate_correct: int | None
        if item.requires_intermediate:
            inter_ok = (
                intermediate_sub_answer is not None
                and step is not None
                and verify_equation_equivalence(
                    intermediate_sub_answer, str(step["step_formula_latex"])
                )
            )
            intermediate_correct = int(inter_ok)
            passed = final_ok and inter_ok
        else:
            intermediate_correct = None  # 变式呈现：本题不要求中间量
            passed = final_ok

        conn.execute(
            "INSERT INTO blind_drill_attempts ("
            " question_uid, drill_question_uid, active_date,"
            " intermediate_correct, final_correct, passed"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                question_uid,
                item.drill_question_uid,
                day.isoformat(),
                intermediate_correct,
                int(final_ok),
                int(passed),
            ),
        )
        session_id = (
            str(session["session_id"]) if session is not None else f"blind-drill:{question_uid}"
        )

        if passed:
            result = elo_engine.realize_or_forfeit_blind_test(
                conn, question_uid, session_id, passed=True
            )
            conn.execute(
                "UPDATE questions SET mastery_state = 'MASTERED', updated_at = CURRENT_TIMESTAMP"
                " WHERE question_uid = ?",
                (question_uid,),
            )
            knowledge_graph.decrement_co_error_edges_on_mastery(conn, question_uid)
            cascaded = _cascade_mastered_children(conn, question_uid)
            explanation = result.explanation_text
            if cascaded:
                explanation += (
                    f" 上位母题连带销账：{len(cascaded)} 道合规前置子题"
                    f"（SCAFFOLD_PRE 包含度 I >= {_CASCADE_CONTAINMENT_MIN}）"
                    "同步升入 MASTERED，各自托管差额独立兑现。"
                )
            return BlindDrillOutcomeDTO(
                passed=True,
                mastery_state="MASTERED",
                retrain_stage=None,
                cascaded_question_uids=cascaded,
                explanation_text=explanation,
            )

        forfeit = elo_engine.realize_or_forfeit_blind_test(
            conn, question_uid, session_id, passed=False
        )
        conn.execute(
            "UPDATE questions SET mastery_state = 'ACTIVE', updated_at = CURRENT_TIMESTAMP"
            " WHERE question_uid = ?",
            (question_uid,),
        )
        if intermediate_correct == 1 and not final_ok:
            stage = "STAGE_4_DECRUTCH_VARIANT"  # 中间子目标已对，仅最终计算错
        else:
            stage = "STAGE_1_BLIND_PROBE"  # 中间量也错/缺（含变式只核最终的情形）
        _create_retrain_session(conn, question_uid, session, stage)
        explanation = (
            forfeit.explanation_text + f" 已新建重训会话，从 {stage} 启动。"
        )
        return BlindDrillOutcomeDTO(
            passed=False,
            mastery_state="ACTIVE",
            retrain_stage=stage,
            cascaded_question_uids=[],
            explanation_text=explanation,
        )
