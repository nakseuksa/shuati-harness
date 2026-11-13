"""P2-D：录入落库服务测试（USE_MOCK_LLM=True / monkeypatch 诊断，零真实网络）。

规格：docs/tasks/P2-D_ingest.md 测试要求 +
docs/designs/adaptive_practice_harness.md CUJ 1、§3.1 第 1/4 条。
数据库用 tmp_db 夹具；图片用 Pillow 合成 + cas_dir 夹具；
默认诊断 fixture：3 步物理题、卡在 Step 2、四维 (3,3,2,3) → base_elo 2085。
节点解析走 knowledge_graph.match_or_create_l3_node：Mock 裁判 purpose='node_judge'
默认保守判未命中（新建）；复用场景用 monkeypatch MOCK_RESPONSES['node_judge'] 构造。
"""

import asyncio
import copy
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.services import ingest_service, knowledge_graph, llm_mocks
from backend.services.cas_store import resolve_cas_uri
from backend.services.difficulty_rubric import clamp_substep_difficulty
from backend.services.ingest_service import GateCardDTO, ingest_question
from backend.services.llm_gateway import LLMGatewayError
from backend.services.vlm_diagnosis import (
    DiagnosisPayload,
    DraftAlignmentResult,
    StepSkeletonItem,
)

GRADE = "高中物理-高二"
STEM_MD = (
    "汽车以速度 $v$ 通过半径 $R = 40\\,\\text{m}$ 的拱形桥最高点，"
    "取 $g = 10\\,\\text{m/s}^2$。求汽车不脱离桥面的最大速度 $v_{max}$。"
)
# Mock fixture 确定性定级（设计 CUJ 1：(3,3,2,3) => 2085；Step 1 前缀保底 1290）
EXPECTED_BASE_ELO = 2085
EXPECTED_TIER = "L5-1_压轴挑战"
EXPECTED_SUB_ELOS = [1290, 1510, 1185]


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "USE_MOCK_LLM", True)


def _make_image(path: Path, color: tuple[int, int, int]) -> Path:
    Image.new("RGB", (64, 48), color).save(path, format="PNG")
    return path


@pytest.fixture()
def stem_image(tmp_path: Path) -> Path:
    return _make_image(tmp_path / "stem.png", (30, 30, 200))


@pytest.fixture()
def draft_image(tmp_path: Path) -> Path:
    return _make_image(tmp_path / "draft.png", (200, 30, 30))


@pytest.fixture()
def diagnose_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """间谍包装 analyze_question_and_draft：记录入参后仍走真实 Mock 诊断。"""
    calls: list[dict[str, Any]] = []
    real = ingest_service.analyze_question_and_draft

    async def spy(
        stem_md: str,
        stem_images: list[Path],
        draft_images: list[Path],
        target_grade: str,
        existing_l1_l2_tree: dict[str, list[str]],
    ) -> DiagnosisPayload:
        calls.append(
            {
                "stem_md": stem_md,
                "stem_images": stem_images,
                "draft_images": draft_images,
                "target_grade": target_grade,
                "existing_l1_l2_tree": existing_l1_l2_tree,
            }
        )
        return await real(stem_md, stem_images, draft_images, target_grade, existing_l1_l2_tree)

    monkeypatch.setattr(ingest_service, "analyze_question_and_draft", spy)
    return calls


async def _ingest(
    db_path: Path,
    *,
    stem_md: str = STEM_MD,
    stem_images: list[Path] | None = None,
    draft_images: list[Path] | None = None,
    initial_state: str = "错题",
    base_dir: Path | None = None,
    user_answer: str = "",
    mastered_claim: bool = False,
) -> GateCardDTO:
    return await ingest_question(
        stem_md,
        stem_images or [],
        draft_images or [],
        GRADE,
        initial_state=initial_state,  # type: ignore[arg-type]
        base_dir=base_dir,
        db_path=db_path,
        user_answer=user_answer,
        mastered_claim=mastered_claim,
    )


def _query(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with get_db_connection(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def _count(db_path: Path, table: str) -> int:
    return int(_query(db_path, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])


def _with_uid(uid: str, body: str) -> str:
    """携带不可变 question_uid Frontmatter 的重入库 Markdown。"""
    return f"---\nquestion_uid: {uid}\n---\n\n{body}"


def _default_payload() -> DiagnosisPayload:
    return DiagnosisPayload.model_validate(copy.deepcopy(llm_mocks.MOCK_RESPONSES["diagnosis"]))


def _install_payload(monkeypatch: pytest.MonkeyPatch, payload: DiagnosisPayload) -> None:
    async def fake(
        stem_md: str,
        stem_images: list[Path],
        draft_images: list[Path],
        target_grade: str,
        existing_l1_l2_tree: dict[str, list[str]],
    ) -> DiagnosisPayload:
        return payload

    monkeypatch.setattr(ingest_service, "analyze_question_and_draft", fake)


def _alt_payload() -> DiagnosisPayload:
    """一题多解：草稿命中 user_alt_1 分支（2 步能量观点解法）。"""
    payload = _default_payload()
    alt_steps = [
        StepSkeletonItem(
            step_index=1,
            step_goal_desc="以能量观点列出桥顶动能与临界速度关系",
            step_formula_latex=r"\frac{1}{2} m v^2 = \frac{1}{2} m g R",
            l1_module="力学",
            l2_entity="圆周运动",
            l3_operator="能量观点等价转化",
            c_sub=2,
            s_sub=2,
            t_sub=1,
            l_sub=1,
            masked_conditions=["桥顶临界状态"],
            probe_question_md="用能量观点写出临界速度关系。",
        ),
        StepSkeletonItem(
            step_index=2,
            step_goal_desc="由临界关系解出最大安全速度",
            step_formula_latex=r"v_{max} = \sqrt{gR} = 20\ \text{m/s}",
            l1_module="力学",
            l2_entity="圆周运动",
            l3_operator="代数求值与单位核对",
            c_sub=1,
            s_sub=1,
            t_sub=1,
            l_sub=2,
            masked_conditions=[r"$g = 10$", r"$R = 40$"],
            probe_question_md="解出 $v_{max}$。",
        ),
    ]
    payload.alignment = DraftAlignmentResult(
        draft_steps_latex=[r"\frac{1}{2} m v^2 = \frac{1}{2} m g R"],
        matched_branch_id="user_alt_1",
        alt_branch_steps=alt_steps,
        last_valid_step_index=1,
        stalled_step_index=2,
        error_category="CONCEPT_BOTTLENECK",
        trap_taxonomy_tag=None,
    )
    return payload


def _many_step_payload(n: int) -> DiagnosisPayload:
    """n 步同 L1/L2 骨架（各步 L3 互异），用于触发新建节点满阈值自动 compaction。"""
    payload = _default_payload()
    template = payload.trace.steps[0]
    payload.trace.steps = [
        template.model_copy(
            update={
                "step_index": i,
                "step_goal_desc": f"第 {i} 步子目标",
                "l3_operator": f"原子算子{i:02d}",
            }
        )
        for i in range(1, n + 1)
    ]
    payload.alignment = DraftAlignmentResult(
        draft_steps_latex=[],
        matched_branch_id="canonical",
        alt_branch_steps=None,
        last_valid_step_index=0,
        stalled_step_index=1,
        error_category="CONCEPT_BOTTLENECK",
        trap_taxonomy_tag=None,
    )
    return payload


# ---------------------------------------------------------------------------
# 新题全链路
# ---------------------------------------------------------------------------


async def test_new_question_full_chain(
    tmp_db: Path, cas_dir: Path, stem_image: Path, draft_image: Path
) -> None:
    card = await _ingest(tmp_db, stem_images=[stem_image], draft_images=[draft_image])

    # questions：1 行，字段正确
    questions = _query(tmp_db, "SELECT * FROM questions")
    assert len(questions) == 1
    q = questions[0]
    assert q["question_uid"] == card.question_uid
    assert q["origin_type"] == "USER_CANONICAL"
    assert q["mastery_state"] == "ACTIVE"
    assert q["verification_status"] == "HUMAN_OR_CANONICAL"
    assert q["target_grade_and_subject"] == GRADE
    assert (q["c_score"], q["s_score"], q["t_score"], q["l_score"]) == (3, 3, 2, 3)
    assert q["base_elo"] == EXPECTED_BASE_ELO
    assert q["tier"] == EXPECTED_TIER
    assert json.loads(q["trap_tags_json"]) == ["TRAP_DOMAIN_BOUNDARY"]
    assert q["primary_l1_module"] == "力学"
    assert q["stem_md"].startswith(f"---\nquestion_uid: {card.question_uid}\n")
    assert q["stem_hash"]
    # 盲测/Elo 相关字段保持未结算初始值
    assert q["blind_test_unlock_at"] is None
    assert q["blind_test_due_at"] is None
    assert q["slated_drill_date"] is None
    assert q["slated_exposure_count"] == 0

    # question_steps：canonical 3 行，blocked 标记与子步定级正确
    steps = _query(
        tmp_db, "SELECT * FROM question_steps ORDER BY step_index"
    )
    assert len(steps) == 3
    assert [s["solution_branch_id"] for s in steps] == ["canonical"] * 3
    assert [s["is_blocked_step"] for s in steps] == [0, 1, 1]
    assert [s["sub_elo"] for s in steps] == EXPECTED_SUB_ELOS
    assert [s["reused_existing_node"] for s in steps] == [0, 0, 0]
    for s in steps:
        assert s["knowledge_node_id"]
        assert json.loads(s["masked_conditions_json"])
        assert s["probe_question_md"]

    # diagnostic_sessions：1 行，门禁初始态
    sessions = _query(tmp_db, "SELECT * FROM diagnostic_sessions")
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess["session_id"] == card.session_id
    assert sess["micro_stage"] == "STAGE_0_CONFIRM_GATE"
    assert sess["prewarm_status"] == "IDLE"
    assert sess["user_confirmed_gate"] == 0
    assert sess["user_overridden"] == 0
    assert sess["matched_branch_id"] == "canonical"
    assert sess["last_valid_step_index"] == 1
    assert sess["stalled_step_index"] == 2
    assert sess["error_category"] == "TRAP_OR_CALC_ERROR"
    assert sess["trap_taxonomy_tag"] == "TRAP_DOMAIN_BOUNDARY"
    assert json.loads(sess["draft_steps_latex_json"]) == [
        r"mg - N = \frac{m v^2}{R}",
        "N = mg",
    ]
    draft_uris = json.loads(sess["draft_image_cas_uris_json"])
    assert len(draft_uris) == 1
    assert resolve_cas_uri(draft_uris[0]).is_file()
    assert cas_dir in resolve_cas_uri(draft_uris[0]).parents

    # knowledge_nodes：3 个新节点；Elo 两表零写入（录入不结算）
    assert _count(tmp_db, "knowledge_nodes") == 3
    assert _count(tmp_db, "elo_mutation_ledger") == 0
    assert _count(tmp_db, "question_escrow_pool") == 0

    # GateCardDTO：status 推导 + floor_preview（stalled=2 → step1.sub_elo）
    assert card.is_reimport is False
    assert card.base_elo == EXPECTED_BASE_ELO
    assert card.tier == EXPECTED_TIER
    assert card.error_category == "TRAP_OR_CALC_ERROR"
    assert card.trap_taxonomy_tag == "TRAP_DOMAIN_BOUNDARY"
    assert [s.status for s in card.steps] == ["completed", "blocked", "untouched"]
    assert [s.sub_elo for s in card.steps] == EXPECTED_SUB_ELOS
    assert card.floor_preview_elo == EXPECTED_SUB_ELOS[0] == 1290


async def test_blank_draft_alignment_defaults(tmp_db: Path, cas_dir: Path) -> None:
    card = await _ingest(tmp_db, draft_images=[])
    sess = _query(tmp_db, "SELECT * FROM diagnostic_sessions")[0]
    assert sess["last_valid_step_index"] == 0
    assert sess["stalled_step_index"] == 1
    assert sess["error_category"] == "CONCEPT_BOTTLENECK"
    assert sess["trap_taxonomy_tag"] is None
    assert json.loads(sess["draft_steps_latex_json"]) == []
    assert json.loads(sess["draft_image_cas_uris_json"]) == []
    assert card.floor_preview_elo is None  # last_valid=0 → 无保底
    assert [s.status for s in card.steps] == ["blocked", "untouched", "untouched"]
    assert _count(tmp_db, "questions") == 1
    assert _count(tmp_db, "question_steps") == 3


async def test_stem_local_image_rewritten_to_cas(
    tmp_db: Path, cas_dir: Path, tmp_path: Path
) -> None:
    _make_image(tmp_path / "fig.png", (10, 180, 10))
    stem_md = f"{STEM_MD}\n\n![配图](fig.png)"
    card = await _ingest(tmp_db, stem_md=stem_md, base_dir=tmp_path)
    q = _query(tmp_db, "SELECT * FROM questions")[0]
    assert "](fig.png)" not in q["stem_md"]
    assert "cas://" in q["stem_md"]
    uri = q["stem_md"].split("cas://")[1].split(")")[0]
    assert resolve_cas_uri(f"cas://{uri}").is_file()
    assert card.question_uid  # uid 正常生成


async def test_new_question_vlm_receives_normalized_stem_and_empty_tree(
    tmp_db: Path, cas_dir: Path, draft_image: Path, diagnose_calls: list[dict[str, Any]]
) -> None:
    card = await _ingest(tmp_db, draft_images=[draft_image])
    assert len(diagnose_calls) == 1
    call = diagnose_calls[0]
    assert call["existing_l1_l2_tree"] == {}  # 冷启动空树
    assert call["stem_md"].startswith(f"---\nquestion_uid: {card.question_uid}\n")
    assert call["draft_images"] == [draft_image]
    assert call["target_grade"] == GRADE


async def test_floor_preview_uses_last_completed_prefix(
    tmp_db: Path, cas_dir: Path, draft_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _default_payload()
    payload.alignment.last_valid_step_index = 2
    payload.alignment.stalled_step_index = 3
    payload.alignment.error_category = "CONCEPT_BOTTLENECK"
    payload.alignment.trap_taxonomy_tag = None
    _install_payload(monkeypatch, payload)
    card = await _ingest(tmp_db, draft_images=[draft_image])
    assert [s.status for s in card.steps] == ["completed", "completed", "blocked"]
    assert card.floor_preview_elo == EXPECTED_SUB_ELOS[1]  # steps[last_valid-1].sub_elo


# ---------------------------------------------------------------------------
# 幂等重入库
# ---------------------------------------------------------------------------


async def test_reimport_same_content_idempotent(
    tmp_db: Path, cas_dir: Path, draft_image: Path, diagnose_calls: list[dict[str, Any]]
) -> None:
    first = await _ingest(tmp_db, draft_images=[draft_image])
    tables = ("questions", "question_steps", "diagnostic_sessions", "knowledge_nodes")
    counts = {t: _count(tmp_db, t) for t in tables}
    second = await _ingest(tmp_db, draft_images=[draft_image])
    assert second.session_id == first.session_id
    assert second.question_uid == first.question_uid
    assert second.is_reimport is True
    assert second == first.model_copy(update={"is_reimport": True})
    for table, n in counts.items():
        assert _count(tmp_db, table) == n  # 不新增任何行
    assert len(diagnose_calls) == 1  # 幂等短路不再调用 VLM


async def test_reimport_stem_edit_preserves_mastery_blind_and_elo(
    tmp_db: Path, cas_dir: Path, draft_image: Path
) -> None:
    first = await _ingest(tmp_db, draft_images=[draft_image])
    uid = first.question_uid
    # 模拟后续训练流已推进：掌握态 + 盲测排期 + Elo 账本/托管池
    with get_db_connection(tmp_db) as conn:
        conn.execute(
            "UPDATE questions SET mastery_state='MASTERED', "
            "blind_test_unlock_at='2026-11-16 00:00:00', "
            "blind_test_due_at='2026-11-20 00:00:00', system_deferred_days=1, "
            "slated_drill_date='2026-11-16', slated_exposure_count=2, "
            "updated_at='2000-01-01 00:00:00' WHERE question_uid=?",
            (uid,),
        )
        conn.execute(
            "INSERT INTO question_escrow_pool (question_uid, l1_module, "
            "initial_combat_elo_snapshot, floor_elo_applied, escrow_status) "
            "VALUES (?, '力学', 1200.0, 90.0, 'FLOOR_ONLY')",
            (uid,),
        )
        conn.execute(
            "INSERT INTO elo_mutation_ledger (idempotency_key, l1_module, question_uid, "
            "mutation_trigger, eval_difficulty_elo, coverage_elo_before, coverage_elo_after, "
            "combat_elo_before, combat_elo_after, explanation_text) "
            "VALUES (?, '力学', ?, 'PARTIAL_DRAFT_SUBGOAL_FLOOR', 1290, "
            "1200.0, 1290.0, 1200.0, 1290.0, '测试快照')",
            (f"{uid}:{first.session_id}:PARTIAL_DRAFT_SUBGOAL_FLOOR", uid),
        )
    escrow_before = dict(_query(tmp_db, "SELECT * FROM question_escrow_pool")[0])
    ledger_before = dict(_query(tmp_db, "SELECT * FROM elo_mutation_ledger")[0])
    steps_before = [dict(r) for r in _query(tmp_db, "SELECT * FROM question_steps")]
    old_hash = _query(tmp_db, "SELECT stem_hash FROM questions")[0]["stem_hash"]

    edited_body = STEM_MD + "\n（补充：忽略空气阻力。）"
    card = await _ingest(tmp_db, stem_md=_with_uid(uid, edited_body))

    q = _query(tmp_db, "SELECT * FROM questions")[0]
    assert q["stem_hash"] != old_hash
    assert "忽略空气阻力" in q["stem_md"]
    assert q["updated_at"] != "2000-01-01 00:00:00"
    # 掌握态 / 盲测字段 / Elo 数据分毫未动
    assert q["mastery_state"] == "MASTERED"
    assert q["blind_test_unlock_at"] == "2026-11-16 00:00:00"
    assert q["blind_test_due_at"] == "2026-11-20 00:00:00"
    assert q["system_deferred_days"] == 1
    assert q["slated_drill_date"] == "2026-11-16"
    assert q["slated_exposure_count"] == 2
    assert dict(_query(tmp_db, "SELECT * FROM question_escrow_pool")[0]) == escrow_before
    assert dict(_query(tmp_db, "SELECT * FROM elo_mutation_ledger")[0]) == ledger_before
    assert [dict(r) for r in _query(tmp_db, "SELECT * FROM question_steps")] == steps_before
    # 无新草稿 → 不新建会话
    assert _count(tmp_db, "diagnostic_sessions") == 1
    assert card.session_id == first.session_id
    assert card.is_reimport is True


async def test_reimport_new_draft_creates_new_session_keeps_old(
    tmp_db: Path, cas_dir: Path, draft_image: Path, tmp_path: Path
) -> None:
    first = await _ingest(tmp_db, draft_images=[draft_image])
    uid = first.question_uid
    old_session = dict(_query(tmp_db, "SELECT * FROM diagnostic_sessions")[0])
    new_draft = _make_image(tmp_path / "draft2.png", (0, 255, 0))

    # stem 不变 + 补充新草稿（设计 §3.1 第 1 条：新建会话，旧会话保留）
    card = await _ingest(tmp_db, stem_md=_with_uid(uid, STEM_MD), draft_images=[new_draft])

    sessions = _query(tmp_db, "SELECT * FROM diagnostic_sessions ORDER BY rowid")
    assert len(sessions) == 2
    assert dict(sessions[0]) == old_session
    assert card.session_id == sessions[1]["session_id"] != first.session_id
    assert card.is_reimport is True
    new_uris = json.loads(sessions[1]["draft_image_cas_uris_json"])
    assert len(new_uris) == 1
    assert new_uris != json.loads(old_session["draft_image_cas_uris_json"])
    assert resolve_cas_uri(new_uris[0]).is_file()
    # 题目与步骤骨架不重建
    assert _count(tmp_db, "questions") == 1
    assert _count(tmp_db, "question_steps") == 3
    assert _count(tmp_db, "knowledge_nodes") == 3
    assert card.base_elo == EXPECTED_BASE_ELO


async def test_reimport_changed_stem_and_new_draft(
    tmp_db: Path, cas_dir: Path, draft_image: Path, tmp_path: Path
) -> None:
    first = await _ingest(tmp_db, draft_images=[draft_image])
    uid = first.question_uid
    new_draft = _make_image(tmp_path / "draft2.png", (0, 255, 0))
    card = await _ingest(
        tmp_db,
        stem_md=_with_uid(uid, STEM_MD + "\n（改述：求最大安全速率。）"),
        draft_images=[new_draft],
    )
    q = _query(tmp_db, "SELECT * FROM questions")[0]
    assert "改述" in q["stem_md"]
    assert _count(tmp_db, "diagnostic_sessions") == 2
    assert card.session_id != first.session_id
    assert card.is_reimport is True


async def test_reimport_same_drafts_no_new_session(
    tmp_db: Path, cas_dir: Path, draft_image: Path
) -> None:
    first = await _ingest(tmp_db, draft_images=[draft_image])
    uid = first.question_uid
    # 改题干但草稿无增量（同 CAS 内容）→ 仅更新 stem，不新建会话
    card = await _ingest(
        tmp_db, stem_md=_with_uid(uid, STEM_MD + "（措辞微调）"), draft_images=[draft_image]
    )
    assert _count(tmp_db, "diagnostic_sessions") == 1
    assert card.session_id == first.session_id
    assert card.is_reimport is True


# ---------------------------------------------------------------------------
# initial_state / 一题多解 / 节点复用
# ---------------------------------------------------------------------------


async def test_already_mastered_initial_state(
    tmp_db: Path, cas_dir: Path, draft_image: Path
) -> None:
    """P7-A：initial_state='已掌握'（旧开关）统一走录入即结算 auto_mastered。"""
    card = await _ingest(tmp_db, draft_images=[draft_image], initial_state="已掌握")
    _assert_auto_mastered(tmp_db, card)
    sess = _query(tmp_db, "SELECT * FROM diagnostic_sessions")[0]
    assert sess["trap_taxonomy_tag"] is None
    assert card.trap_taxonomy_tag is None


async def test_alt_branch_steps_persisted(
    tmp_db: Path, cas_dir: Path, draft_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _alt_payload()
    _install_payload(monkeypatch, payload)
    card = await _ingest(tmp_db, draft_images=[draft_image])

    canonical = _query(
        tmp_db, "SELECT * FROM question_steps WHERE solution_branch_id='canonical'"
    )
    alt = _query(
        tmp_db,
        "SELECT * FROM question_steps WHERE solution_branch_id='user_alt_1' ORDER BY step_index",
    )
    assert len(canonical) == 3
    assert len(alt) == 2
    assert [s["is_blocked_step"] for s in alt] == [0, 1]
    # 默认裁判保守判未命中：alt 分支不复用 canonical 节点，全部新建
    assert [s["reused_existing_node"] for s in alt] == [0, 0]
    assert _count(tmp_db, "knowledge_nodes") == 5
    expected_alt_elo = clamp_substep_difficulty((3, 3, 2, 3), EXPECTED_BASE_ELO, (2, 2, 1, 1))[1]
    assert alt[0]["sub_elo"] == expected_alt_elo

    sess = _query(tmp_db, "SELECT * FROM diagnostic_sessions")[0]
    assert sess["matched_branch_id"] == "user_alt_1"
    # 确认卡展示 matched 分支（对齐索引针对该分支）
    assert [s.step_index for s in card.steps] == [1, 2]
    assert [s.status for s in card.steps] == ["completed", "blocked"]
    assert card.floor_preview_elo == expected_alt_elo
    assert card.error_category == "CONCEPT_BOTTLENECK"


async def test_node_reuse_via_judge_match(
    tmp_db: Path,
    cas_dir: Path,
    draft_image: Path,
    diagnose_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _ingest(tmp_db, draft_images=[draft_image])
    nodes_first = {
        (r["l1_module"], r["l2_entity"], r["l3_operator"]): r["node_id"]
        for r in _query(tmp_db, "SELECT * FROM knowledge_nodes")
    }
    # 裁判命中既有节点（静态 Mock：后续每次 match_or_create 裁判均返回该 ID）
    target_id = str(
        _query(
            tmp_db,
            "SELECT knowledge_node_id FROM question_steps "
            "WHERE question_uid=? AND step_index=1",
            (first.question_uid,),
        )[0]["knowledge_node_id"]
    )
    monkeypatch.setitem(
        llm_mocks.MOCK_RESPONSES,
        "node_judge",
        {"match_node_id": target_id, "evidence": "测试构造：同义算子命中既有节点。"},
    )
    card2 = await _ingest(
        tmp_db, stem_md=STEM_MD + "\n（第二道同考点题：数值不同。）", draft_images=[draft_image]
    )
    assert card2.is_reimport is False
    assert _count(tmp_db, "knowledge_nodes") == 3  # 节点数不增
    nodes_second = {
        (r["l1_module"], r["l2_entity"], r["l3_operator"]): r["node_id"]
        for r in _query(tmp_db, "SELECT * FROM knowledge_nodes")
    }
    assert nodes_second == nodes_first
    steps2 = _query(
        tmp_db,
        "SELECT * FROM question_steps WHERE question_uid=? ORDER BY step_index",
        (card2.question_uid,),
    )
    assert [s["reused_existing_node"] for s in steps2] == [1, 1, 1]
    assert all(s["knowledge_node_id"] == target_id for s in steps2)
    # 命中节点的别名库扩充（候选 L3 并入 aliases，主名称除外）
    target_row = _query(
        tmp_db, "SELECT * FROM knowledge_nodes WHERE node_id=?", (target_id,)
    )[0]
    aliases = json.loads(target_row["aliases_json"])
    assert "临界边界条件（N=0）" in aliases
    assert "代数求值与单位核对" in aliases
    # 第一题的历史 reused 标记不变；第二题诊断注入了既有 L1/L2 骨架树
    steps1 = _query(
        tmp_db,
        "SELECT * FROM question_steps WHERE question_uid=? ORDER BY step_index",
        (first.question_uid,),
    )
    assert [s["reused_existing_node"] for s in steps1] == [0, 0, 0]
    assert diagnose_calls[1]["existing_l1_l2_tree"] == {"力学": ["圆周运动"]}


async def test_default_judge_conservative_creates_new_nodes(
    tmp_db: Path, cas_dir: Path, draft_image: Path
) -> None:
    first = await _ingest(tmp_db, draft_images=[draft_image])
    card2 = await _ingest(
        tmp_db, stem_md=STEM_MD + "\n（第二道同考点题。）", draft_images=[draft_image]
    )
    # 默认 node_judge fixture 判未命中：同 (l1,l2,l3) 也不复用，节点数翻倍
    assert _count(tmp_db, "knowledge_nodes") == 6
    steps2 = _query(
        tmp_db,
        "SELECT * FROM question_steps WHERE question_uid=? ORDER BY step_index",
        (card2.question_uid,),
    )
    assert [s["reused_existing_node"] for s in steps2] == [0, 0, 0]
    ids1 = {
        r["knowledge_node_id"]
        for r in _query(
            tmp_db,
            "SELECT knowledge_node_id FROM question_steps WHERE question_uid=?",
            (first.question_uid,),
        )
    }
    assert ids1.isdisjoint({r["knowledge_node_id"] for r in steps2})


async def test_auto_compaction_triggered_at_threshold_during_ingest(
    tmp_db: Path, cas_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threshold = settings.COMPACTION_BATCH_THRESHOLD
    _install_payload(monkeypatch, _many_step_payload(threshold))
    real_compaction = knowledge_graph.run_scoped_compaction
    compaction_calls: list[tuple[str, str]] = []

    def spy(conn: sqlite3.Connection, l1: str, l2: str) -> str | None:
        compaction_calls.append((l1, l2))
        return real_compaction(conn, l1, l2)

    monkeypatch.setattr(knowledge_graph, "run_scoped_compaction", spy)
    card = await _ingest(tmp_db)
    # 第 threshold 个新建节点在录入中途自动触发同域 compaction
    assert compaction_calls == [("力学", "圆周运动")]
    # 默认裁判保守判未命中：无合并，节点全部保持活跃，录入事务完整
    assert _count(tmp_db, "knowledge_nodes") == threshold
    assert all(
        n["redirect_to_id"] is None for n in _query(tmp_db, "SELECT * FROM knowledge_nodes")
    )
    assert _count(tmp_db, "compaction_audit_ledger") == 0
    assert _count(tmp_db, "questions") == 1
    assert _count(tmp_db, "question_steps") == threshold
    assert len(card.steps) == threshold
    assert _count(tmp_db, "elo_mutation_ledger") == 0


# ---------------------------------------------------------------------------
# 事务原子性
# ---------------------------------------------------------------------------


async def test_transaction_atomic_on_step_insert_failure(
    tmp_db: Path, cas_dir: Path, draft_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_insert = ingest_service._insert_step_row

    def flaky(conn: Any, question_uid: str, branch_id: str, step: Any, *args: Any) -> None:
        if step.step_index == 2:
            raise RuntimeError("步骤写入中途故障")
        real_insert(conn, question_uid, branch_id, step, *args)

    monkeypatch.setattr(ingest_service, "_insert_step_row", flaky)
    with pytest.raises(RuntimeError, match="步骤写入中途故障"):
        await _ingest(tmp_db, draft_images=[draft_image])
    for table in (
        "questions",
        "question_steps",
        "knowledge_nodes",
        "diagnostic_sessions",
        "compaction_audit_ledger",
    ):
        assert _count(tmp_db, table) == 0  # 整体回滚，无残留


async def test_vlm_failure_leaves_no_rows(
    tmp_db: Path, cas_dir: Path, draft_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> DiagnosisPayload:
        raise LLMGatewayError("诊断网关故障")

    monkeypatch.setattr(ingest_service, "analyze_question_and_draft", boom)
    with pytest.raises(LLMGatewayError):
        await _ingest(tmp_db, draft_images=[draft_image])
    for table in (
        "questions",
        "question_steps",
        "knowledge_nodes",
        "diagnostic_sessions",
        "compaction_audit_ledger",
    ):
        assert _count(tmp_db, table) == 0


# ---------------------------------------------------------------------------
# P4-F 缺口1：综合错题录入→跨考点标红边生长（CUJ 4）
# ---------------------------------------------------------------------------


def _edges(db_path: Path) -> list[sqlite3.Row]:
    return _query(db_path, "SELECT * FROM concept_co_errors ORDER BY node_u_id, node_v_id")


def _question_node_ids(db_path: Path, uid: str) -> set[str]:
    return {
        str(r["knowledge_node_id"])
        for r in _query(
            db_path,
            "SELECT DISTINCT knowledge_node_id FROM question_steps WHERE question_uid=?",
            (uid,),
        )
    }


async def test_co_error_edges_grow_on_multi_node_wrong_question(
    tmp_db: Path, cas_dir: Path, draft_image: Path
) -> None:
    """默认 Mock 诊断：3 步挂 3 个不同 L3 节点，错题录入 → C(3,2)=3 条 u<v 标红边。"""
    card = await _ingest(tmp_db, draft_images=[draft_image])
    nodes = _question_node_ids(tmp_db, card.question_uid)
    assert len(nodes) == 3
    edges = _edges(tmp_db)
    assert len(edges) == 3  # 3 个不同节点两两建边
    expected_pairs = {
        (u, v) for u in nodes for v in nodes if u < v
    }
    assert {(e["node_u_id"], e["node_v_id"]) for e in edges} == expected_pairs
    for e in edges:
        assert e["node_u_id"] < e["node_v_id"]  # u<v 规范化
        assert e["active_error_count"] == 1 and e["total_historical_count"] == 1
        assert e["l1_module_u"] == "力学" and e["l1_module_v"] == "力学"
        assert json.loads(e["source_question_uids_json"]) == [card.question_uid]


async def test_single_node_question_creates_no_edges(
    tmp_db: Path, cas_dir: Path, draft_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """所有步复用同一节点（1 个不同 resolve 节点）→ 不建边。"""
    first = await _ingest(tmp_db, draft_images=[draft_image])
    target_id = str(
        _query(
            tmp_db,
            "SELECT knowledge_node_id FROM question_steps WHERE question_uid=? AND step_index=1",
            (first.question_uid,),
        )[0]["knowledge_node_id"]
    )
    edges_before = _edges(tmp_db)
    assert len(edges_before) == 3

    async def fake_match(
        conn: Any, l1: str, l2: str, cand: str, aliases: list[str]
    ) -> tuple[str, bool]:
        return target_id, True  # 全部步复用同一节点

    monkeypatch.setattr(ingest_service, "match_or_create_l3_node", fake_match)
    second = await _ingest(
        tmp_db, stem_md=STEM_MD + "\n（单节点题。）", draft_images=[draft_image]
    )
    assert len(_question_node_ids(tmp_db, second.question_uid)) == 1
    edges_after = _edges(tmp_db)
    assert len(edges_after) == 3  # 不新增边
    assert [dict(e) for e in edges_after] == [dict(e) for e in edges_before]  # 计数不动
    for e in edges_after:
        assert second.question_uid not in json.loads(e["source_question_uids_json"])


async def test_already_mastered_initial_state_creates_no_edges(
    tmp_db: Path, cas_dir: Path, draft_image: Path
) -> None:
    """initial_state='已掌握' → ALREADY_MASTERED，即使多节点也不建边。"""
    await _ingest(tmp_db, draft_images=[draft_image], initial_state="已掌握")
    assert _count(tmp_db, "knowledge_nodes") == 3  # 节点仍解析
    assert _edges(tmp_db) == []  # 但不建标红边


async def test_wrong_state_but_mastered_category_creates_no_edges(
    tmp_db: Path, cas_dir: Path, draft_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """initial_state='错题' 但诊断结论 ALREADY_MASTERED → 不建边（条件第二子句）。"""
    payload = _default_payload()
    payload.alignment.error_category = "ALREADY_MASTERED"
    payload.alignment.trap_taxonomy_tag = None
    _install_payload(monkeypatch, payload)
    card = await _ingest(tmp_db, draft_images=[draft_image], initial_state="错题")
    assert card.error_category == "ALREADY_MASTERED"
    assert _edges(tmp_db) == []


async def test_reimport_does_not_double_increment_edges(
    tmp_db: Path, cas_dir: Path, draft_image: Path, tmp_path: Path
) -> None:
    """重入库（短路 + 新草稿两条路径）绝不重复递增标红边。"""
    first = await _ingest(tmp_db, draft_images=[draft_image])
    uid = first.question_uid
    snapshot = [dict(e) for e in _edges(tmp_db)]
    assert len(snapshot) == 3 and all(e["active_error_count"] == 1 for e in snapshot)

    # 同内容重入库（短路）
    await _ingest(tmp_db, draft_images=[draft_image])
    assert [dict(e) for e in _edges(tmp_db)] == snapshot
    # 补充新草稿（新建会话路径）
    new_draft = _make_image(tmp_path / "draft2.png", (0, 255, 0))
    await _ingest(tmp_db, stem_md=_with_uid(uid, STEM_MD), draft_images=[new_draft])
    edges = _edges(tmp_db)
    assert [dict(e) for e in edges] == snapshot  # active/historical/uids 全不动
    for e in edges:
        assert json.loads(e["source_question_uids_json"]) == [uid]


async def test_second_multi_node_question_increments_shared_edges(
    tmp_db: Path, cas_dir: Path, draft_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二道多节点题复用相同 3 节点 → 重合边 active/historical +1、uids 并入。"""
    first = await _ingest(tmp_db, draft_images=[draft_image])
    nodes_by_op = {
        str(r["l3_operator"]): str(r["node_id"])
        for r in _query(tmp_db, "SELECT node_id, l3_operator FROM knowledge_nodes")
    }

    async def fake_match(
        conn: Any, l1: str, l2: str, cand: str, aliases: list[str]
    ) -> tuple[str, bool]:
        return nodes_by_op[cand], True  # 按算子名复用第一题的对应节点

    monkeypatch.setattr(ingest_service, "match_or_create_l3_node", fake_match)
    second = await _ingest(
        tmp_db, stem_md=STEM_MD + "\n（第二道同考点综合题。）", draft_images=[draft_image]
    )
    assert _question_node_ids(tmp_db, second.question_uid) == _question_node_ids(
        tmp_db, first.question_uid
    )
    edges = _edges(tmp_db)
    assert len(edges) == 3  # 无新增边，全部重合累加
    for e in edges:
        assert e["active_error_count"] == 2 and e["total_historical_count"] == 2
        assert json.loads(e["source_question_uids_json"]) == [
            first.question_uid, second.question_uid
        ]


# ---------------------------------------------------------------------------
# P7-A：录入答题区与掌握判定（勾选/声明文本/正确答案 → 录入单事务即结算）
# ---------------------------------------------------------------------------


def _assert_auto_mastered(db_path: Path, card: GateCardDTO) -> None:
    """录入即结算全量断言：卡标记 + 三表状态 + MASTERED_INIT 账本 + 步骤全不阻断。"""
    assert card.auto_mastered is True
    assert card.mastery_explanation and "已掌握" in card.mastery_explanation
    assert card.error_category == "ALREADY_MASTERED"
    assert card.trap_taxonomy_tag is None
    assert card.floor_preview_elo is None
    assert card.steps and [s.status for s in card.steps] == ["completed"] * len(card.steps)

    q = _query(db_path, "SELECT * FROM questions")[0]
    assert q["mastery_state"] == "MASTERED"
    sess = _query(db_path, "SELECT * FROM diagnostic_sessions")[0]
    assert sess["session_id"] == card.session_id
    assert sess["micro_stage"] == "SESSION_COMPLETED"
    assert sess["user_confirmed_gate"] == 1
    assert sess["prewarm_status"] == "IDLE"  # 不启动预热
    assert sess["error_category"] == "ALREADY_MASTERED"
    steps = _query(db_path, "SELECT * FROM question_steps")
    assert steps and all(s["is_blocked_step"] == 0 for s in steps)

    events = _query(
        db_path, "SELECT * FROM elo_mutation_ledger WHERE mutation_trigger='MASTERED_INIT'"
    )
    assert len(events) == 1
    event = events[0]
    assert event["idempotency_key"] == f"{card.question_uid}:{card.session_id}:MASTERED_INIT"
    assert event["question_uid"] == card.question_uid
    assert event["combat_elo_after"] > event["combat_elo_before"]
    assert event["coverage_elo_after"] >= event["combat_elo_after"]
    assert event["explanation_text"] == card.mastery_explanation
    assert _count(db_path, "question_escrow_pool") == 0


async def test_mastered_claim_auto_settles_on_ingest(
    tmp_db: Path, cas_dir: Path, draft_image: Path
) -> None:
    """勾选「已掌握」→ 直接入库 + 提取知识点 + Elo 结算，跳过练习流程。"""
    card = await _ingest(tmp_db, draft_images=[draft_image], mastered_claim=True)
    _assert_auto_mastered(tmp_db, card)
    assert _count(tmp_db, "knowledge_nodes") == 3  # 知识点照常提取
    assert _edges(tmp_db) == []  # 掌握题不生长跨考点标红边
    assert _count(tmp_db, "questions") == 1
    assert _count(tmp_db, "diagnostic_sessions") == 1


@pytest.mark.parametrize("answer", ["已掌握", "我已掌握", "掌握了", "我会做", "  已掌握  "])
async def test_mastered_text_answer_auto_settles(
    tmp_db: Path, cas_dir: Path, answer: str
) -> None:
    """回答「已掌握」类声明文本（含首尾 whitespace）→ 录入即结算。"""
    card = await _ingest(tmp_db, user_answer=answer)
    _assert_auto_mastered(tmp_db, card)


@pytest.mark.parametrize("answer", ["20", "vmax = 20"])
async def test_correct_answer_auto_mastered(tmp_db: Path, cas_dir: Path, answer: str) -> None:
    """填入正确答案（Mock 标准答案 v_{max}=20 m/s，现有引擎等价能力）→ 录入即结算。"""
    card = await _ingest(tmp_db, user_answer=answer)
    _assert_auto_mastered(tmp_db, card)


async def test_wrong_answer_keeps_default_flow(tmp_db: Path, cas_dir: Path) -> None:
    """错误答案不改变默认流程：ACTIVE + 需门禁 + 无草稿默认 CONCEPT_BOTTLENECK。"""
    card = await _ingest(tmp_db, user_answer="x=999")
    assert card.auto_mastered is False
    assert card.mastery_explanation is None
    assert card.error_category == "CONCEPT_BOTTLENECK"
    assert card.floor_preview_elo is None
    q = _query(tmp_db, "SELECT * FROM questions")[0]
    assert q["mastery_state"] == "ACTIVE"
    sess = _query(tmp_db, "SELECT * FROM diagnostic_sessions")[0]
    assert sess["micro_stage"] == "STAGE_0_CONFIRM_GATE"
    assert sess["user_confirmed_gate"] == 0
    steps = _query(tmp_db, "SELECT * FROM question_steps ORDER BY step_index")
    assert [s["is_blocked_step"] for s in steps] == [1, 1, 1]
    assert _count(tmp_db, "elo_mutation_ledger") == 0


@pytest.mark.parametrize("answer", ["", "   "])
async def test_empty_answer_no_draft_zero_drift(
    tmp_db: Path, cas_dir: Path, answer: str
) -> None:
    """空答案无草稿 → 默认行为零漂移（回归）。"""
    card = await _ingest(tmp_db, user_answer=answer)
    assert card.auto_mastered is False
    assert card.mastery_explanation is None
    assert card.error_category == "CONCEPT_BOTTLENECK"
    assert [s.status for s in card.steps] == ["blocked", "untouched", "untouched"]
    assert card.floor_preview_elo is None
    q = _query(tmp_db, "SELECT * FROM questions")[0]
    assert q["mastery_state"] == "ACTIVE"
    sess = _query(tmp_db, "SELECT * FROM diagnostic_sessions")[0]
    assert sess["micro_stage"] == "STAGE_0_CONFIRM_GATE"
    assert sess["user_confirmed_gate"] == 0
    assert _count(tmp_db, "elo_mutation_ledger") == 0


async def test_reimport_mastered_no_double_settlement(
    tmp_db: Path, cas_dir: Path, draft_image: Path
) -> None:
    """重入库：同内容二次 ingest（即使再次勾选）不重复结算，账本事件数不变。"""
    first = await _ingest(tmp_db, draft_images=[draft_image], mastered_claim=True)
    _assert_auto_mastered(tmp_db, first)
    tables = (
        "questions",
        "question_steps",
        "diagnostic_sessions",
        "elo_mutation_ledger",
        "knowledge_nodes",
    )
    counts = {t: _count(tmp_db, t) for t in tables}
    second = await _ingest(tmp_db, draft_images=[draft_image], mastered_claim=True)
    assert second.is_reimport is True
    assert second.session_id == first.session_id
    assert second.question_uid == first.question_uid
    for table, n in counts.items():
        assert _count(tmp_db, table) == n  # 不新增任何行，不重复结算
    q = _query(tmp_db, "SELECT * FROM questions")[0]
    assert q["mastery_state"] == "MASTERED"


async def test_equivalence_failure_does_not_break_ingest(
    tmp_db: Path, cas_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """等价判定异常不得击穿录入：捕获 → 按未掌握处理 + logger.warning。"""

    def boom(user_answer: str, standard: str) -> bool:
        raise RuntimeError("沙盒判定故障")

    monkeypatch.setattr(ingest_service, "answers_equivalent", boom)
    with caplog.at_level(logging.WARNING, logger="backend.services.ingest_service"):
        card = await _ingest(tmp_db, user_answer="20")
    assert card.auto_mastered is False
    assert card.mastery_explanation is None
    q = _query(tmp_db, "SELECT * FROM questions")[0]
    assert q["mastery_state"] == "ACTIVE"
    assert _count(tmp_db, "elo_mutation_ledger") == 0
    assert any(
        r.levelno == logging.WARNING and "答案等价判定异常" in r.getMessage()
        for r in caplog.records
    )


async def test_equivalence_check_runs_in_thread(
    tmp_db: Path, cas_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """等价判定必须经 asyncio.to_thread（沙盒子进程阻塞不得卡事件循环）。"""
    real_to_thread = asyncio.to_thread
    calls: list[tuple[Any, ...]] = []

    async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((func, *args))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(ingest_service.asyncio, "to_thread", spy)
    card = await _ingest(tmp_db, user_answer="20")
    assert card.auto_mastered is True
    assert len(calls) == 1
    assert calls[0][0] is ingest_service.answers_equivalent
    assert calls[0][1] == "20"


async def test_mastered_claim_short_circuits_equivalence(
    tmp_db: Path, cas_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """勾选优先：即使答案错误也直接掌握，不再调用等价判定。"""
    calls: list[Any] = []

    async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        raise AssertionError("勾选命中时不应调用等价判定")

    monkeypatch.setattr(ingest_service.asyncio, "to_thread", spy)
    card = await _ingest(tmp_db, user_answer="x=999", mastered_claim=True)
    assert card.auto_mastered is True
    assert calls == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("已掌握", True),
        (" 我已掌握 ", True),
        ("掌握了", True),
        ("我会做", True),
        ("", False),
        ("已掌握了吗", False),
        ("这道题我已经完全掌握了", False),
        ("x=20", False),
    ],
)
def test_is_mastered_text_boundaries(text: str, expected: bool) -> None:
    assert ingest_service._is_mastered_text(text) is expected


async def test_auto_mastered_observability_events(
    tmp_db: Path, cas_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """观测：ingest.auto_mastered 标记事件 + record_mastered_init 内部 elo.event 埋点。"""
    with caplog.at_level(logging.INFO):
        card = await _ingest(tmp_db, mastered_claim=True)
    auto = [
        r for r in caplog.records if getattr(r, "event", None) == "ingest.auto_mastered"
    ]
    assert len(auto) == 1
    fields = getattr(auto[0], "fields", {})
    assert fields["question_uid"] == card.question_uid
    assert fields["session_id"] == card.session_id
    assert fields["mastered_claim"] is True
    assert fields["applied"] is True
    elo = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "elo.event"
        and getattr(r, "fields", {}).get("trigger") == "MASTERED_INIT"
    ]
    assert len(elo) == 1
    persisted = [
        r for r in caplog.records if getattr(r, "event", None) == "ingest.persisted"
    ]
    assert persisted and getattr(persisted[0], "fields", {})["auto_mastered"] is True


async def test_ingest_releases_db_lock_during_vlm_call(
    tmp_db: Path, cas_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VLM HTTP 期间不持有 SQLite 写锁，外部连接可并发写入。"""
    vlm_started = asyncio.Event()
    release_vlm = asyncio.Event()
    real_analyze = ingest_service.analyze_question_and_draft

    async def slow_analyze(
        stem_md: str,
        stem_images: list[Path],
        draft_images: list[Path],
        target_grade: str,
        existing_l1_l2_tree: dict[str, list[str]],
    ) -> DiagnosisPayload:
        vlm_started.set()
        await release_vlm.wait()
        return await real_analyze(
            stem_md, stem_images, draft_images, target_grade, existing_l1_l2_tree
        )

    monkeypatch.setattr(ingest_service, "analyze_question_and_draft", slow_analyze)
    task = asyncio.create_task(_ingest(tmp_db))
    await vlm_started.wait()

    other = sqlite3.connect(tmp_db, timeout=0.2)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO questions (question_uid, origin_type, target_grade_and_subject,"
            " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
            " base_elo, tier, primary_l1_module, mastery_state)"
            " VALUES ('concurrent-probe', 'USER_CANONICAL', '高中物理-高二', '并发探针',"
            " 'hash-probe', '1', 1, 1, 1, 1, 1200, 'L1_基础夯实', '力学', 'ACTIVE')"
        )
        other.commit()
    finally:
        other.close()
        release_vlm.set()

    card = await task
    assert card.question_uid
