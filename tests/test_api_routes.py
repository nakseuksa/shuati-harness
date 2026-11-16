"""P4-A：FastAPI 入口与全量 REST 路由测试（设计 §4 契约表 / Target File #13 / CUJ 1~4）。

约束：USE_MOCK_LLM=True + monkeypatch settings.SQLITE_PATH/CAS_ASSETS_DIR 到 tmp，
httpx.AsyncClient(ASGITransport) 直驱 ASGI 应用（手动触发 lifespan），零真实网络。
服务层全部走真实实现（Mock 网关 + 真实 SQLite + 真实 SymPy 沙盒子进程）。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import re
import shutil
import sqlite3
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.main import app as default_app
from backend.main import create_app
from backend.services import ingest_service, llm_gateway, llm_mocks, scaffold_engine
from backend.services.cas_store import ingest_image_bytes, resolve_cas_uri
from backend.services.llm_gateway import LLMGatewayError
from backend.services.vlm_diagnosis import DiagnosisPayload

GRADE = "高中物理-高二"
STEM_MD = (
    "汽车以速度 $v$ 通过半径 $R = 40\\,\\text{m}$ 的拱形桥最高点，"
    "取 $g = 10\\,\\text{m/s}^2$。求汽车不脱离桥面的最大速度 $v_{max}$。"
)
EXPECTED_BASE_ELO = 2085
PREFIX_SUB_ELO = 1290

STAGE1_CORRECT = r"v_{max} = \sqrt{gR}"
STAGE1_WRONG = r"v_{max} = \sqrt{2gR}"
STAGE3_CORRECT = r"v_{max} = 20\ \text{m/s}"
STAGE4_CORE_EQUATION = "2*(x + 3) = 16"  # 与 x = 5 常数比率等价

MODULE = "电磁学"
DAY = date(2026, 11, 12)
INTERMEDIATE_STD = "R = 2*d"
INTERMEDIATE_RIGHT = "R/2 = d"
INTERMEDIATE_WRONG = "R = d"
FINAL_RIGHT = "x = 2"
FINAL_WRONG = "x = 9"

NAME_A = "动量定理求电荷量"
NAME_B = "动量定理求电荷量大小与位移"

# P10-E：看板/账本/盲测读取端点均要求 grade_subject 必填 Query（缺失 → 422）。
GRAPH_PARAMS: dict[str, str] = {"grade_subject": GRADE}


def _drill_params(active_date: date | None = None) -> dict[str, str]:
    """盲测题单查询参数：学段必填 + 可选 active_date（时间旅行注入）。"""
    params = dict(GRAPH_PARAMS)
    if active_date is not None:
        params["active_date"] = active_date.isoformat()
    return params


# ---------------------------------------------------------------------------
# 夹具：环境隔离 / ASGI 客户端 / 预热 Task 清理
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "USE_MOCK_LLM", True)
    monkeypatch.setattr(settings, "SQLITE_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(settings, "CAS_ASSETS_DIR", tmp_path / "assets")


@pytest.fixture(autouse=True)
async def _isolate_prewarm_tasks() -> AsyncIterator[None]:
    scaffold_engine.in_flight_prewarm_tasks.clear()
    yield
    for task in list(scaffold_engine.in_flight_prewarm_tasks.values()):
        task.cancel()
    scaffold_engine.in_flight_prewarm_tasks.clear()
    await asyncio.sleep(0)


@pytest.fixture()
def frontend_dir(tmp_path: Path) -> Path:
    """P4-B 并行创建的前端目录：测试期用空目录模拟。"""
    directory = tmp_path / "frontend"
    directory.mkdir()
    return directory


@pytest.fixture()
async def client(frontend_dir: Path) -> AsyncIterator[AsyncClient]:
    app = create_app(frontend_dir=frontend_dir)
    async with app.router.lifespan_context(app):  # 触发 lifespan：init_db()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as async_client:
            yield async_client


@pytest.fixture()
def install_diagnosis(monkeypatch: pytest.MonkeyPatch) -> Any:
    """monkeypatch VLM 诊断：注入定制 DiagnosisPayload（类别/前缀/负荷）。"""

    def _install(payload: DiagnosisPayload) -> None:
        async def fake(
            stem_md: str,
            stem_images: list[Path],
            draft_images: list[Path],
            target_grade: str,
            existing_l1_l2_tree: dict[str, list[str]],
        ) -> DiagnosisPayload:
            return payload

        monkeypatch.setattr(ingest_service, "analyze_question_and_draft", fake)

    return _install


@pytest.fixture(autouse=True)
def _default_diagnosis(install_diagnosis: Any) -> None:
    """默认诊断：概念卡点（Mock 原始 fixture 为 TRAP，测试内可再注入覆盖）。"""
    install_diagnosis(_payload("CONCEPT_BOTTLENECK"))


def _payload(category: str = "CONCEPT_BOTTLENECK") -> DiagnosisPayload:
    payload = DiagnosisPayload.model_validate(copy.deepcopy(llm_mocks.MOCK_RESPONSES["diagnosis"]))
    payload.alignment.error_category = category  # type: ignore[assignment]
    payload.alignment.trap_taxonomy_tag = (
        "TRAP_DOMAIN_BOUNDARY" if category == "TRAP_OR_CALC_ERROR" else None
    )
    return payload


# ---------------------------------------------------------------------------
# 工具：multipart 录入 / SQL 直查 / 盲测数据构造
# ---------------------------------------------------------------------------


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


async def _ingest(
    client: AsyncClient,
    stem_md: str = STEM_MD,
    *,
    initial_state: str | None = None,
    with_images: bool = False,
    user_answer: str | None = None,
    mastered_claim: bool | None = None,
) -> dict[str, Any]:
    data: dict[str, str] = {"stem_md": stem_md, "target_grade": GRADE}
    if initial_state is not None:
        data["initial_state"] = initial_state
    if user_answer is not None:
        data["user_answer"] = user_answer
    if mastered_claim is not None:
        data["mastered_claim"] = "true" if mastered_claim else "false"
    files = []
    if with_images:
        files = [
            ("stem_images", ("stem.png", _png_bytes((30, 30, 200)), "image/png")),
            ("draft_images", ("draft.png", _png_bytes((200, 30, 30)), "image/png")),
        ]
    resp = await client.post("/api/v1/questions/ingest", data=data, files=files)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with get_db_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row:
    rows = _query(sql, params)
    assert len(rows) == 1, f"期望恰好一行: {sql} -> {len(rows)}"
    return rows[0]


def _execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with get_db_connection() as conn:
        conn.execute(sql, params)


async def _confirm(client: AsyncClient, session_id: str, category: str) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/sessions/{session_id}/confirm-gate",
        json={"stalled_step_index": 2, "error_category": category},
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _submit(
    client: AsyncClient, session_id: str, answer: str, self_assessment: str | None = None
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/sessions/{session_id}/step-submit",
        json={"user_answer": answer, "self_assessment": self_assessment},
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _run_full_chain(client: AsyncClient) -> dict[str, Any]:
    """概念分支全链路：录入 → 门禁 → 一阶错 → 二阶对 → 三阶对 → 四阶核心列式通关。"""
    card = await _ingest(client)
    sid = card["session_id"]
    await _confirm(client, sid, "CONCEPT_BOTTLENECK")
    resp = await _submit(client, sid, STAGE1_WRONG)
    assert resp["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    resp = await _submit(client, sid, "x = 5")
    assert resp["micro_stage"] == "STAGE_3_REDO_ORIGINAL"
    resp = await _submit(client, sid, STAGE3_CORRECT)
    assert resp["micro_stage"] == "STAGE_4_DECRUTCH_VARIANT"
    resp = await _submit(client, sid, STAGE4_CORE_EQUATION)
    assert resp["micro_stage"] == "SESSION_COMPLETED"
    return card


def _insert_drill_question(
    uid: str,
    *,
    grade: str = GRADE,
    module: str = MODULE,
    answer: str = FINAL_RIGHT,
    origin: str = "USER_CANONICAL",
    mastery: str = "PENDING_RETEST",
    unlock: date | None = DAY - timedelta(days=1),
    due: date | None = DAY + timedelta(days=3),
    stem: str | None = None,
    l_score: int = 2,
) -> None:
    _execute(
        "INSERT INTO questions (question_uid, origin_type, target_grade_and_subject,"
        " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
        " base_elo, tier, primary_l1_module, mastery_state, blind_test_unlock_at,"
        " blind_test_due_at) VALUES (?, ?, ?, ?, ?, ?, 2, 2, 2, ?, 2085, 'L3_综合中档',"
        " ?, ?, ?, ?)",
        (
            uid,
            origin,
            grade,
            stem if stem is not None else f"题干 {uid}：求 x。",
            f"hash-{uid}",
            answer,
            l_score,
            module,
            mastery,
            unlock.isoformat() if unlock is not None else None,
            due.isoformat() if due is not None else None,
        ),
    )


def _insert_drill_scaffold(
    uid: str, *, stalled_formula: str = INTERMEDIATE_STD, grade: str = GRADE
) -> None:
    """节点 + 卡壳步 + 已完成会话（回退原题时中间量核验依赖）。"""
    _execute(
        "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
        " l3_operator) VALUES (?, ?, ?, ?, ?)",
        (f"node-{uid}", grade, MODULE, f"实体-{uid}", f"算子-{uid}"),
    )
    _execute(
        "INSERT INTO question_steps (step_id, question_uid, step_index, step_goal_desc,"
        " step_formula_latex, knowledge_node_id, masked_conditions_json, probe_question_md,"
        " is_blocked_step, c_sub, s_sub, t_sub, l_sub, sub_elo)"
        " VALUES (?, ?, 2, '卡壳步子目标', ?, ?, '[]', ?, 1, 1, 1, 1, 1, 900)",
        (f"step-{uid}", uid, stalled_formula, f"node-{uid}", "中间几何关系式是什么？"),
    )
    _execute(
        "INSERT INTO diagnostic_sessions (session_id, question_uid,"
        " draft_image_cas_uris_json, draft_steps_latex_json, matched_branch_id,"
        " last_valid_step_index, stalled_step_index, error_category,"
        " user_confirmed_gate, micro_stage)"
        " VALUES (?, ?, '[]', '[]', 'canonical', 1, 2, 'CONCEPT_BOTTLENECK', 1,"
        " 'SESSION_COMPLETED')",
        (f"sess-{uid}", uid),
    )


def _insert_pool(uid: str, *, escrow: float = 20.0, module: str = MODULE) -> None:
    _execute(
        "INSERT INTO question_escrow_pool (question_uid, l1_module,"
        " initial_combat_elo_snapshot, floor_elo_applied, escrow_combat_delta,"
        " is_cov_settled, decay_factor, escrow_status)"
        " VALUES (?, ?, 1200.0, 0.0, ?, 1, 1.0, 'FROZEN_PENDING_BLIND')",
        (uid, module, escrow),
    )


def _insert_relation(src: str, tgt: str, rtype: str) -> None:
    _execute(
        "INSERT INTO question_relations (source_question_uid, target_question_uid,"
        " relation_type, is_directed, surface_sim, skeleton_sim, containment_score,"
        " trap_overlap, confidence_source) VALUES (?, ?, ?, ?, 0.7, 0.9, 0.9, 0.2,"
        " 'SYMPY_KERNEL_GEN')",
        (src, tgt, rtype, 1 if rtype == "SCAFFOLD_PRE" else 0),
    )


# ---------------------------------------------------------------------------
# CUJ 1：/api/v1/questions/ingest（multipart 全链路 + 幂等重入库）
# ---------------------------------------------------------------------------


async def test_ingest_multipart_full_chain(client: AsyncClient) -> None:
    card = await _ingest(client, with_images=True)
    assert card["session_id"] and card["question_uid"]
    assert len(card["steps"]) == 3
    assert card["base_elo"] == EXPECTED_BASE_ELO
    assert card["floor_preview_elo"] == PREFIX_SUB_ELO
    assert card["tier"] == "L5-1_压轴挑战"
    assert card["error_category"] == "CONCEPT_BOTTLENECK"
    assert card["is_reimport"] is False
    assert [s["status"] for s in card["steps"]] == ["completed", "blocked", "untouched"]

    # 题干/草稿图片全部归档 CAS；草稿 URI 落会话表
    session = _one("SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card["session_id"],))
    draft_uris = json.loads(session["draft_image_cas_uris_json"])
    assert len(draft_uris) == 1 and draft_uris[0].startswith("cas://")
    cas_files = list(settings.CAS_ASSETS_DIR.rglob("*.webp"))
    assert len(cas_files) == 2  # stem + draft
    # CAS 静态路由可回读草稿图
    digest = draft_uris[0].removeprefix("cas://")
    asset = await client.get(f"/assets/{digest}")
    assert asset.status_code == 200
    assert asset.headers["content-type"] == "image/webp"


async def test_ingest_reimport_idempotent(client: AsyncClient) -> None:
    first = await _ingest(client)
    second = await _ingest(client)
    assert second["question_uid"] == first["question_uid"]
    assert second["is_reimport"] is True
    assert second["session_id"] == first["session_id"]  # 无新草稿 → 不新建会话
    assert _one("SELECT COUNT(*) AS n FROM questions")["n"] == 1
    assert _one("SELECT COUNT(*) AS n FROM diagnostic_sessions")["n"] == 1


async def test_ingest_reimport_mastered_echo(client: AsyncClient) -> None:
    """P9-A：首次 mastered_claim=true 后同题干二次 POST → 掌握态回显，零重复结算。"""
    first = await _ingest(client, mastered_claim=True)
    assert first["auto_mastered"] is True
    assert first["is_reimport"] is False
    ledger_n = _one("SELECT COUNT(*) AS n FROM elo_mutation_ledger")["n"]

    second = await _ingest(client, mastered_claim=True)
    assert second["auto_mastered"] is True
    assert second["is_reimport"] is True
    assert second["question_uid"] == first["question_uid"]
    assert second["session_id"] == first["session_id"]
    assert second["mastery_explanation"] and "未重复结算" in second["mastery_explanation"]
    assert second["floor_preview_elo"] is None
    assert [s["status"] for s in second["steps"]] == ["completed"] * len(second["steps"])
    # 零漂移：账本/会话行数不变
    assert _one("SELECT COUNT(*) AS n FROM elo_mutation_ledger")["n"] == ledger_n
    assert _one("SELECT COUNT(*) AS n FROM diagnostic_sessions")["n"] == 1
    assert _one("SELECT mastery_state FROM questions")["mastery_state"] == "MASTERED"


async def test_ingest_invalid_initial_state_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/questions/ingest",
        data={"stem_md": STEM_MD, "target_grade": GRADE, "initial_state": "半会不会"},
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()


async def test_ingest_gateway_error_502_with_retry_hint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> DiagnosisPayload:
        raise LLMGatewayError("上游模型服务超时")

    monkeypatch.setattr(ingest_service, "analyze_question_and_draft", boom)
    resp = await client.post(
        "/api/v1/questions/ingest", data={"stem_md": STEM_MD, "target_grade": GRADE}
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "上游模型服务超时" in detail and "重试" in detail
    # 事务回滚无残留
    assert _one("SELECT COUNT(*) AS n FROM questions")["n"] == 0


# ---------------------------------------------------------------------------
# CUJ 1/2：confirm-gate 三分支 + 404/422/400
# ---------------------------------------------------------------------------


async def test_confirm_gate_concept_branch_floor_and_probe(client: AsyncClient) -> None:
    card = await _ingest(client)
    resp = await _confirm(client, card["session_id"], "CONCEPT_BOTTLENECK")
    assert resp["micro_stage"] == "STAGE_1_BLIND_PROBE"
    assert resp["mastery_state"] == "ACTIVE"
    assert resp["explanation_text"] and "保底" in resp["explanation_text"]
    view = resp["view_payload"]
    assert view["stalled_step_index"] == 2
    assert view["masked_conditions"] and view["probe_question_md"]
    events = _query(
        "SELECT * FROM elo_mutation_ledger WHERE mutation_trigger = ?",
        ("PARTIAL_DRAFT_SUBGOAL_FLOOR",),
    )
    assert len(events) == 1


async def test_confirm_gate_trap_branch_direct_stage4(
    client: AsyncClient, install_diagnosis: Any
) -> None:
    install_diagnosis(_payload("TRAP_OR_CALC_ERROR"))
    card = await _ingest(client)
    resp = await _confirm(client, card["session_id"], "TRAP_OR_CALC_ERROR")
    assert resp["micro_stage"] == "STAGE_4_DECRUTCH_VARIANT"
    assert resp["prewarm_status"] in ("READY_SYMPY", "READY_FALLBACK")
    assert resp["view_payload"]["variant"]["stem_md"]


async def test_confirm_gate_mastered_branch(client: AsyncClient, install_diagnosis: Any) -> None:
    """P7-A：initial_state='已掌握' 录入即结算；已 auto_mastered 会话不可重复确认（400）。"""
    install_diagnosis(_payload("ALREADY_MASTERED"))
    card = await _ingest(client, initial_state="已掌握")
    assert card["auto_mastered"] is True
    assert card["mastery_explanation"] and "已掌握" in card["mastery_explanation"]
    assert card["error_category"] == "ALREADY_MASTERED"
    assert card["floor_preview_elo"] is None
    assert _one("SELECT mastery_state FROM questions")["mastery_state"] == "MASTERED"
    sess = _one("SELECT * FROM diagnostic_sessions")
    assert sess["micro_stage"] == "SESSION_COMPLETED"
    assert sess["user_confirmed_gate"] == 1
    assert _one("SELECT COUNT(*) AS n FROM elo_mutation_ledger")["n"] == 1
    resp = await client.post(
        f"/api/v1/sessions/{card['session_id']}/confirm-gate",
        json={"stalled_step_index": 2, "error_category": "ALREADY_MASTERED"},
    )
    assert resp.status_code == 400  # 门禁已确认/会话已流转，不可重复确认


async def test_confirm_gate_unknown_session_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/sessions/no-such-session/confirm-gate",
        json={"stalled_step_index": 2, "error_category": "CONCEPT_BOTTLENECK"},
    )
    assert resp.status_code == 404
    assert "no-such-session" in resp.json()["detail"]


async def test_confirm_gate_invalid_category_422(client: AsyncClient) -> None:
    card = await _ingest(client)
    resp = await client.post(
        f"/api/v1/sessions/{card['session_id']}/confirm-gate",
        json={"stalled_step_index": 2, "error_category": "CARELESS_MISTAKE"},
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()


async def test_confirm_gate_double_confirm_400(client: AsyncClient) -> None:
    card = await _ingest(client)
    await _confirm(client, card["session_id"], "CONCEPT_BOTTLENECK")
    resp = await client.post(
        f"/api/v1/sessions/{card['session_id']}/confirm-gate",
        json={"stalled_step_index": 2, "error_category": "CONCEPT_BOTTLENECK"},
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


# ---------------------------------------------------------------------------
# CUJ 2/3：step-submit 一阶→二阶→三阶→四阶全链路 + 非法阶段
# ---------------------------------------------------------------------------


async def test_step_submit_full_chain(client: AsyncClient) -> None:
    card = await _run_full_chain(client)
    final = _one("SELECT * FROM questions WHERE question_uid = ?", (card["question_uid"],))
    assert final["mastery_state"] == "PENDING_RETEST"
    pool = _one(
        "SELECT * FROM question_escrow_pool WHERE question_uid = ?", (card["question_uid"],)
    )
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND"
    assert float(pool["escrow_combat_delta"]) > 0


async def test_step_submit_stage_views_and_self_assessment(client: AsyncClient) -> None:
    card = await _ingest(client)
    sid = card["session_id"]
    await _confirm(client, sid, "CONCEPT_BOTTLENECK")
    # 一阶结构化自评「仍无思路」→ 直接请求二阶铺垫
    resp = await _submit(client, sid, "", self_assessment="no_idea")
    assert resp["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    assert "3x + 5 = 20" in resp["view_payload"]["micro_scaffold"]["stem_md"]
    # 二阶对 → 三阶展开原题
    resp = await _submit(client, sid, "x = 5")
    assert resp["micro_stage"] == "STAGE_3_REDO_ORIGINAL"
    assert "拱形桥" in resp["view_payload"]["stem_md"]
    # 三阶对 → 四阶核心列式模式
    resp = await _submit(client, sid, STAGE3_CORRECT)
    assert resp["micro_stage"] == "STAGE_4_DECRUTCH_VARIANT"
    assert resp["view_payload"]["verification_mode"] == "equation_skeleton"
    assert resp["view_payload"]["allowed_symbols"] == ["x"]


async def test_step_submit_before_gate_400(client: AsyncClient) -> None:
    card = await _ingest(client)
    resp = await client.post(
        f"/api/v1/sessions/{card['session_id']}/step-submit",
        json={"user_answer": "x = 5", "self_assessment": None},
    )
    assert resp.status_code == 400
    assert "门禁" in resp.json()["detail"]


async def test_step_submit_unknown_session_404(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/sessions/ghost/step-submit", json={"user_answer": "x = 5"})
    assert resp.status_code == 404


async def test_step_submit_after_completed_400(client: AsyncClient) -> None:
    card = await _run_full_chain(client)
    resp = await client.post(
        f"/api/v1/sessions/{card['session_id']}/step-submit", json={"user_answer": "x = 5"}
    )
    assert resp.status_code == 400
    assert "已完成" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# CUJ 3：/api/v1/drills/daily GET/POST（幂等题单 / 双核验 / 时间旅行）
# ---------------------------------------------------------------------------


async def test_drills_daily_time_travel_and_idempotent(client: AsyncClient) -> None:
    _insert_drill_question("q-due")
    _insert_drill_scaffold("q-due")
    # 解锁日之前：空题单
    resp = await client.get(
        "/api/v1/drills/daily", params=_drill_params(DAY - timedelta(days=2))
    )
    assert resp.status_code == 200 and resp.json() == []
    # 解锁日：排入 1 道（回退原题，携带卡壳步 probe）
    resp = await client.get("/api/v1/drills/daily", params=_drill_params(DAY))
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["question_uid"] == "q-due"
    assert item["drill_question_uid"] == "q-due"
    assert item["requires_intermediate"] is True
    assert item["probe_question_md"]
    assert item["verification_mode"] == "equation_skeleton"
    # 同日重复 GET：幂等锁定，不重复递增曝光
    resp = await client.get("/api/v1/drills/daily", params=_drill_params(DAY))
    assert [i["question_uid"] for i in resp.json()] == ["q-due"]
    row = _one("SELECT * FROM questions WHERE question_uid = 'q-due'")
    assert row["slated_exposure_count"] == 1
    assert row["slated_drill_date"] == DAY.isoformat()


async def test_drills_daily_default_active_date_today(client: AsyncClient) -> None:
    today = date.today()
    _insert_drill_question(
        "q-today", unlock=today - timedelta(days=1), due=today + timedelta(days=3)
    )
    _insert_drill_scaffold("q-today")
    resp = await client.get("/api/v1/drills/daily", params=GRAPH_PARAMS)  # 缺省 today
    assert resp.status_code == 200
    assert [i["question_uid"] for i in resp.json()] == ["q-today"]


async def test_drills_submit_original_double_verification(client: AsyncClient) -> None:
    _insert_drill_question("q-pass")
    _insert_drill_scaffold("q-pass")
    _insert_pool("q-pass", escrow=20.0)
    _insert_drill_question("q-fail")
    _insert_drill_scaffold("q-fail")
    _insert_pool("q-fail", escrow=20.0)

    # 回退原题：中间量 + 最终答案双正确才销账
    resp = await client.post(
        "/api/v1/drills/daily",
        json={
            "question_uid": "q-pass",
            "final_answer": FINAL_RIGHT,
            "intermediate_sub_answer": INTERMEDIATE_RIGHT,
            "active_date": DAY.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["passed"] is True
    assert outcome["mastery_state"] == "MASTERED"
    assert outcome["explanation_text"] and "解冻" in outcome["explanation_text"]
    pool = _one("SELECT * FROM question_escrow_pool WHERE question_uid = 'q-pass'")
    assert pool["escrow_status"] == "REALIZED_MASTERED"
    # active_date 注入：作答日志落在时间旅行日
    attempt = _one("SELECT * FROM blind_drill_attempts WHERE question_uid = 'q-pass'")
    assert attempt["active_date"] == DAY.isoformat()

    # 中间量错误（即使最终对）→ 失败退回重训
    resp = await client.post(
        "/api/v1/drills/daily",
        json={
            "question_uid": "q-fail",
            "final_answer": FINAL_RIGHT,
            "intermediate_sub_answer": INTERMEDIATE_WRONG,
            "active_date": DAY.isoformat(),
        },
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["passed"] is False
    assert outcome["mastery_state"] == "ACTIVE"
    assert outcome["retrain_stage"] == "STAGE_1_BLIND_PROBE"
    pool = _one("SELECT * FROM question_escrow_pool WHERE question_uid = 'q-fail'")
    assert pool["escrow_status"] == "FORFEITED_RESET"
    assert float(pool["escrow_combat_delta"]) == 0.0


async def test_drills_submit_variant_final_only(client: AsyncClient) -> None:
    _insert_drill_question("q-var")
    _insert_drill_scaffold("q-var")
    _insert_pool("q-var")
    _insert_drill_question(
        "v-1", origin="AI_SYNTHETIC_VARIANT", mastery="ACTIVE", unlock=None, due=None
    )
    _insert_relation("q-var", "v-1", "ISOMORPHIC")

    resp = await client.get("/api/v1/drills/daily", params=_drill_params(DAY))
    items = resp.json()
    assert len(items) == 1
    assert items[0]["drill_question_uid"] == "v-1"  # 变式优先
    assert items[0]["requires_intermediate"] is False
    assert items[0]["probe_question_md"] is None

    # 变式只核最终答案，无需中间量
    resp = await client.post(
        "/api/v1/drills/daily",
        json={
            "question_uid": "q-var",
            "final_answer": FINAL_RIGHT,
            "active_date": DAY.isoformat(),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["passed"] is True
    assert resp.json()["mastery_state"] == "MASTERED"


async def test_drills_submit_strips_spoiler_frontmatter(client: AsyncClient) -> None:
    stem = f"---\nquestion_uid: q-meta\ntarget_grade_and_subject: {GRADE}\n---\n\n求 x。"
    _insert_drill_question("q-meta", stem=stem)
    _insert_drill_scaffold("q-meta")
    resp = await client.get("/api/v1/drills/daily", params=_drill_params(DAY))
    item = resp.json()[0]
    assert "question_uid" not in item["stem_md"]
    assert "求 x" in item["stem_md"]


async def test_drills_submit_unknown_question_400(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/drills/daily",
        json={"question_uid": "ghost", "final_answer": FINAL_RIGHT},
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


# ---------------------------------------------------------------------------
# CUJ 4：/api/v1/knowledge-graph 看板 + compaction 回滚往返
# ---------------------------------------------------------------------------


async def test_knowledge_graph_structure(client: AsyncClient) -> None:
    # 直接 SQL 构造：两个 L1 模块、挂载题目、标红边
    _insert_drill_question("q-kg", mastery="ACTIVE", unlock=None, due=None)
    _execute(
        "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
        " l3_operator, aliases_json)"
        " VALUES ('n1', ?, ?, '圆周运动', '临界边界条件', '[\"N=0 临界\"]')",
        (GRADE, MODULE),
    )
    _execute(
        "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
        " l3_operator) VALUES ('n2', ?, ?, '圆周运动', '向心力来源')",
        (GRADE, MODULE),
    )
    _execute(
        "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
        " l3_operator) VALUES ('n3', ?, '力学', '抛体运动', '分解与合成')",
        (GRADE,),
    )
    _execute(
        "INSERT INTO question_steps (step_id, question_uid, step_index, step_goal_desc,"
        " step_formula_latex, knowledge_node_id, masked_conditions_json, probe_question_md,"
        " is_blocked_step, c_sub, s_sub, t_sub, l_sub, sub_elo)"
        " VALUES ('s-kg', 'q-kg', 1, '子目标', 'x=1', 'n1', '[]', 'probe', 1, 1, 1, 1, 1, 900)",
    )
    _execute(
        "INSERT INTO concept_co_errors (node_u_id, node_v_id, grade_subject,"
        " l1_module_u, l1_module_v, active_error_count, total_historical_count,"
        " source_question_uids_json) VALUES ('n1', 'n2', ?, ?, ?, 2, 5, '[\"q-kg\"]')",
        (GRADE, MODULE, MODULE),
    )

    resp = await client.get("/api/v1/knowledge-graph", params=GRAPH_PARAMS)
    assert resp.status_code == 200
    body = resp.json()
    modules = {m["l1_module"]: m for m in body["modules"]}
    assert set(modules) == {MODULE, "力学"}
    em = modules[MODULE]
    assert [g["l2_entity"] for g in em["l2_groups"]] == ["圆周运动"]
    nodes = {n["node_id"]: n for n in em["l2_groups"][0]["nodes"]}
    assert set(nodes) == {"n1", "n2"}
    assert nodes["n1"]["aliases"] == ["N=0 临界"]
    assert nodes["n1"]["state"] == "ACTIVE"  # 挂载 ACTIVE 题的卡壳步
    assert nodes["n2"]["state"] == "MASTERED"  # 无卡壳挂载 → 绿
    assert nodes["n1"]["redirected_node_ids"] == []
    # 各 L1 模块 GraphMetricsDTO
    assert set(em["metrics"]) == {"mastery_rate", "convergence", "active_nodes"}
    assert em["metrics"]["active_nodes"] == 2
    # 仅 n1 有 USER_CANONICAL 卡壳挂载（ACTIVE）→ 攻克率 0%
    assert em["metrics"]["mastery_rate"] == 0.0
    # 标红边（含 active/historical 计数）
    assert len(body["red_edges"]) == 1
    edge = body["red_edges"][0]
    assert (edge["node_u_id"], edge["node_v_id"]) == ("n1", "n2")
    assert edge["active_error_count"] == 2
    assert edge["total_historical_count"] == 5
    assert edge["source_question_uids"] == ["q-kg"]
    assert body["compaction_batches"] == []


async def test_knowledge_graph_compaction_revert_roundtrip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services.knowledge_graph import NodeJudgeResult, run_scoped_compaction

    async def judge(
        messages: list[dict[str, Any]],
        schema: type[Any],
        *,
        model: str,
        purpose: str,
        temperature: float | None = None,
    ) -> NodeJudgeResult:
        assert purpose == "node_judge"
        payload = json.loads(messages[-1]["content"])
        if payload["task"] == "scoped_compaction":
            return NodeJudgeResult(
                match_node_id=str(payload["existing_nodes"][0]["node_id"]),
                evidence="API 测试裁判确认近义合并",
            )
        return NodeJudgeResult(match_node_id=None, evidence="未命中")

    monkeypatch.setattr(llm_gateway, "chat_json", judge)

    # 构造近义对 A/B + 跨 L2 标红边 B-X，真实跑一次同域 compaction
    with get_db_connection() as conn:
        for nid, name, ts in (
            ("A", NAME_A, "2026-01-01 00:00:01"),
            ("B", NAME_B, "2026-01-01 00:00:02"),
        ):
            conn.execute(
                "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
                " l3_operator, created_at) VALUES (?, ?, ?, '电磁感应单双杆模型', ?, ?)",
                (nid, GRADE, MODULE, name, ts),
            )
        conn.execute(
            "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
            " l3_operator) VALUES ('X', ?, ?, '磁场边界', '放缩圆')",
            (GRADE, MODULE),
        )
        conn.execute(
            "INSERT INTO concept_co_errors (node_u_id, node_v_id, grade_subject,"
            " l1_module_u, l1_module_v, active_error_count, total_historical_count,"
            " source_question_uids_json) VALUES ('B', 'X', ?, ?, ?, 1, 1, '[\"q1\"]')",
            (GRADE, MODULE, MODULE),
        )
        batch_id = run_scoped_compaction(conn, GRADE, MODULE, "电磁感应单双杆模型")
    assert batch_id is not None

    # 合并后看板：B 被 resolve 出活跃树，标红边端点改写为 A
    resp = await client.get("/api/v1/knowledge-graph", params=GRAPH_PARAMS)
    body = resp.json()
    em = next(m for m in body["modules"] if m["l1_module"] == MODULE)
    group = next(g for g in em["l2_groups"] if g["l2_entity"] == "电磁感应单双杆模型")
    node_ids = {n["node_id"] for n in group["nodes"]}
    assert node_ids == {"A"}
    assert group["nodes"][0]["redirected_node_ids"] == ["B"]
    assert {(e["node_u_id"], e["node_v_id"]) for e in body["red_edges"]} == {("A", "X")}
    assert len(body["compaction_batches"]) == 1
    batch = body["compaction_batches"][0]
    assert batch["batch_id"] == batch_id
    assert batch["is_reverted"] is False
    assert batch["merges"][0]["from_node_id"] == "B"
    assert batch["merges"][0]["to_node_id"] == "A"
    assert batch["merges"][0]["is_reverted"] is False

    # 一键回滚 → 节点与标红边精确还原
    resp = await client.post(f"/api/v1/knowledge-graph/compaction/{batch_id}/revert")
    assert resp.status_code == 200
    assert resp.json() == {"batch_id": batch_id, "reverted_merges": 1}
    body = (await client.get("/api/v1/knowledge-graph", params=GRAPH_PARAMS)).json()
    em = next(m for m in body["modules"] if m["l1_module"] == MODULE)
    group = next(g for g in em["l2_groups"] if g["l2_entity"] == "电磁感应单双杆模型")
    assert {n["node_id"] for n in group["nodes"]} == {"A", "B"}
    assert all(n["redirected_node_ids"] == [] for n in group["nodes"])
    assert {(e["node_u_id"], e["node_v_id"]) for e in body["red_edges"]} == {("B", "X")}
    batch = body["compaction_batches"][0]
    assert batch["is_reverted"] is True
    assert batch["merges"][0]["is_reverted"] is True
    assert batch["merges"][0]["reverted_at"] is not None

    # 已回滚批次 → 409；未知批次 → 404
    resp = await client.post(f"/api/v1/knowledge-graph/compaction/{batch_id}/revert")
    assert resp.status_code == 409
    resp = await client.post("/api/v1/knowledge-graph/compaction/no-such-batch/revert")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/v1/elo/ledger：模块过滤 / 曲线点 / 托管明细 / 解释条
# ---------------------------------------------------------------------------


async def test_elo_ledger_full_flow(client: AsyncClient) -> None:
    card = await _run_full_chain(client)  # 力学模块：保底 + 覆盖分结算 + 托管冻结
    resp = await client.get("/api/v1/elo/ledger", params=GRAPH_PARAMS)
    assert resp.status_code == 200
    modules = resp.json()["modules"]
    assert [m["l1_module"] for m in modules] == ["力学"]
    ledger = modules[0]
    # 当前双轨水位：覆盖分 > 实战分（托管差额未兑现）
    assert ledger["current"]["coverage_elo"] > ledger["current"]["combat_elo"]
    # 保底发放后实战分正向抬升（ΔR_floor = K_FLOOR·(1-E)，向保底水位修正而非直达）
    assert ledger["current"]["combat_elo"] == pytest.approx(ledger["events"][0]["combat_elo_after"])
    assert settings.MODULE_ELO_INITIAL < ledger["current"]["combat_elo"] < PREFIX_SUB_ELO
    # 事件流水（含解释条）：保底 + 覆盖分
    triggers = [e["mutation_trigger"] for e in ledger["events"]]
    assert triggers == ["PARTIAL_DRAFT_SUBGOAL_FLOOR", "SCAFFOLD_COVER_GAIN"]
    assert all(e["explanation_text"] for e in ledger["events"])
    assert all(e["question_uid"] == card["question_uid"] for e in ledger["events"])
    # 历史曲线点与事件 after 值一致
    assert len(ledger["curve"]) == 2
    for point, event in zip(ledger["curve"], ledger["events"], strict=True):
        assert point["event_id"] == event["event_id"]
        assert point["coverage_elo"] == event["coverage_elo_after"]
        assert point["combat_elo"] == event["combat_elo_after"]
    # 待兑现托管池明细
    assert len(ledger["pending_escrows"]) == 1
    escrow = ledger["pending_escrows"][0]
    assert escrow["question_uid"] == card["question_uid"]
    assert escrow["escrow_combat_delta"] > 0
    assert escrow["decay_factor"] == 1.0


async def test_elo_ledger_module_filter(client: AsyncClient) -> None:
    await _run_full_chain(client)
    resp = await client.get("/api/v1/elo/ledger", params={**GRAPH_PARAMS, "l1_module": "力学"})
    assert [m["l1_module"] for m in resp.json()["modules"]] == ["力学"]
    resp = await client.get(
        "/api/v1/elo/ledger", params={**GRAPH_PARAMS, "l1_module": "不存在的模块"}
    )
    assert resp.status_code == 200
    assert resp.json()["modules"] == []


async def test_elo_ledger_cold_start_module(client: AsyncClient) -> None:
    _insert_drill_question("q-cold", mastery="ACTIVE", unlock=None, due=None)
    resp = await client.get("/api/v1/elo/ledger", params=GRAPH_PARAMS)
    modules = resp.json()["modules"]
    assert [m["l1_module"] for m in modules] == [MODULE]
    ledger = modules[0]
    initial = settings.MODULE_ELO_INITIAL
    assert ledger["current"] == {"coverage_elo": initial, "combat_elo": initial}
    assert ledger["events"] == []
    assert ledger["curve"] == []
    assert ledger["pending_escrows"] == []


# ---------------------------------------------------------------------------
# /assets/{cas_hash}：合法 200 webp / 非法 400 / 缺失 404
# ---------------------------------------------------------------------------


async def test_assets_valid_hash_serves_webp(client: AsyncClient) -> None:
    uri = ingest_image_bytes(_png_bytes((10, 200, 10)))
    digest = uri.removeprefix("cas://")
    resp = await client.get(f"/assets/{digest}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    assert resp.content == resolve_cas_uri(uri).read_bytes()


@pytest.mark.parametrize("bad_hash", ["xyz", "A" * 64, "a" * 63, "a" * 65])
async def test_assets_invalid_hash_400(client: AsyncClient, bad_hash: str) -> None:
    resp = await client.get(f"/assets/{bad_hash}")
    assert resp.status_code == 400
    assert "detail" in resp.json()


async def test_assets_missing_hash_404(client: AsyncClient) -> None:
    resp = await client.get(f"/assets/{'a' * 64}")
    assert resp.status_code == 404
    assert "detail" in resp.json()


# ---------------------------------------------------------------------------
# 前端静态挂载容错：目录缺失 / 为空 / 有 index.html
# ---------------------------------------------------------------------------


async def _with_client(app: Any) -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as async_client:
            yield async_client


async def test_frontend_missing_app_still_starts(tmp_path: Path) -> None:
    app = create_app(frontend_dir=tmp_path / "nonexistent-frontend")
    async for client in _with_client(app):
        resp = await client.get("/api/v1/drills/daily", params=GRAPH_PARAMS)
        assert resp.status_code == 200 and resp.json() == []
        assert (await client.get("/")).status_code == 404  # 无挂载，根路径无服务


async def test_frontend_empty_dir_app_still_starts(tmp_path: Path) -> None:
    empty = tmp_path / "empty-frontend"
    empty.mkdir()
    app = create_app(frontend_dir=empty)
    async for client in _with_client(app):
        assert (await client.get("/api/v1/elo/ledger", params=GRAPH_PARAMS)).status_code == 200
        assert (await client.get("/")).status_code == 404  # 无 index.html


async def test_frontend_index_served(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>刷题 Harness 工作台</html>", encoding="utf-8")
    app = create_app(frontend_dir=frontend)
    async for client in _with_client(app):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "刷题 Harness 工作台" in resp.text
        # API 路由不被 "/" 挂载遮蔽
        assert (await client.get("/api/v1/drills/daily", params=GRAPH_PARAMS)).status_code == 200


# ---------------------------------------------------------------------------
# P9-E：根路由内容哈希版本号查询参数注入（缓存击穿）
# ---------------------------------------------------------------------------


@pytest.fixture()
def frontend_copy(tmp_path: Path) -> Path:
    """真实 frontend 目录复制到 tmp：版本号测试改/删副本，不触碰真实文件。"""
    src = Path(__file__).resolve().parent.parent / "frontend"
    dest = tmp_path / "frontend"
    shutil.copytree(src, dest)
    return dest


def _content_version(path: Path) -> str:
    """期望值从真实文件字节推导（契约：不得硬编码哈希）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


async def test_root_injects_content_hash_versions(frontend_copy: Path) -> None:
    """GET / → 200，app.js/styles.css 引用携带 ?v=<内容 SHA-256 前 12 位 hex>。"""
    app = create_app(frontend_dir=frontend_copy)
    js_v = _content_version(frontend_copy / "app.js")
    css_v = _content_version(frontend_copy / "styles.css")
    async for client in _with_client(app):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert f'src="./app.js?v={js_v}"' in resp.text
        assert f'href="./styles.css?v={css_v}"' in resp.text
        assert re.search(r"app\.js\?v=[0-9a-f]{12}", resp.text)
        assert re.search(r"styles\.css\?v=[0-9a-f]{12}", resp.text)
        assert resp.text.count("?v=") == 2  # 仅两处本地引用；CDN 锁版本不受影响
        # 确定性：内容不变 → 两次请求 HTML 完全一致（缓存友好）
        assert (await client.get("/")).text == resp.text
        # 根路由不遮蔽 API 与静态资源路径（mount 仍服务其余文件）
        assert (await client.get("/api/v1/drills/daily", params=GRAPH_PARAMS)).status_code == 200
        assert (await client.get("/app.js")).status_code == 200
        assert (await client.get("/styles.css")).status_code == 200


async def test_root_version_tracks_content_change_independently(frontend_copy: Path) -> None:
    """修改副本 app.js → 版本号变为新内容哈希；styles.css 版本号不受影响（独立）。"""
    app = create_app(frontend_dir=frontend_copy)
    css_v = _content_version(frontend_copy / "styles.css")
    async for client in _with_client(app):
        old_js_v = _content_version(frontend_copy / "app.js")
        assert f'src="./app.js?v={old_js_v}"' in (await client.get("/")).text

        (frontend_copy / "app.js").write_text("// P9-E cache-bust probe\n", encoding="utf-8")
        new_js_v = _content_version(frontend_copy / "app.js")
        assert new_js_v != old_js_v
        after = (await client.get("/")).text
        assert f'src="./app.js?v={new_js_v}"' in after
        assert f"app.js?v={old_js_v}" not in after
        assert f'href="./styles.css?v={css_v}"' in after


async def test_root_missing_js_keeps_reference_unversioned(frontend_copy: Path) -> None:
    """副本删除 app.js → GET / 仍 200，app.js 引用保持无版本号原样（容错）。"""
    (frontend_copy / "app.js").unlink()
    app = create_app(frontend_dir=frontend_copy)
    css_v = _content_version(frontend_copy / "styles.css")
    async for client in _with_client(app):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'src="./app.js"' in resp.text
        assert "app.js?v=" not in resp.text
        assert f'href="./styles.css?v={css_v}"' in resp.text  # 另一资源正常注入


async def test_root_missing_css_keeps_reference_unversioned(frontend_copy: Path) -> None:
    """对称容错：styles.css 缺失 → 其引用保持原样，app.js 正常注入。"""
    (frontend_copy / "styles.css").unlink()
    app = create_app(frontend_dir=frontend_copy)
    js_v = _content_version(frontend_copy / "app.js")
    async for client in _with_client(app):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'href="./styles.css"' in resp.text
        assert "styles.css?v=" not in resp.text
        assert f'src="./app.js?v={js_v}"' in resp.text


async def test_root_index_read_failure_404(frontend_copy: Path) -> None:
    """index.html 读取失败（同名变目录 → IsADirectoryError）→ 404，不崩溃 500。"""
    app = create_app(frontend_dir=frontend_copy)
    index = frontend_copy / "index.html"
    index.unlink()
    index.mkdir()
    async for client in _with_client(app):
        resp = await client.get("/")
        assert resp.status_code == 404
        assert "detail" in resp.json()


def test_default_app_registers_all_contract_routes() -> None:
    paths = {getattr(route, "path", None) for route in default_app.routes}
    assert {
        "/api/v1/questions/ingest",
        "/api/v1/sessions/{session_id}/confirm-gate",
        "/api/v1/sessions/{session_id}/step-submit",
        "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/retry-prewarm",
        "/api/v1/drills/daily",
        "/api/v1/knowledge-graph",
        "/api/v1/knowledge-graph/compaction/{batch_id}/revert",
        "/api/v1/elo/ledger",
        "/api/v1/settings/grade-subjects",  # P10-E 新增
        "/api/v1/settings/grade-subjects/{name}",  # P10-E 新增
        "/assets/{cas_hash}",
    } <= paths


# ---------------------------------------------------------------------------
# 会话视图刷新 / 预热重试端点（二阶题干死循环修复回归）
# ---------------------------------------------------------------------------


async def test_get_session_view_returns_nested_stem(client: AsyncClient) -> None:
    card = await _ingest(client)
    sid = card["session_id"]
    await _confirm(client, sid, "CONCEPT_BOTTLENECK")
    resp = await _submit(client, sid, STAGE1_WRONG)
    assert resp["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    r = await client.get(f"/api/v1/sessions/{sid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    scaffold = body["view_payload"]["micro_scaffold"]
    assert scaffold is not None and str(scaffold["stem_md"]).strip()


async def test_get_session_unknown_404(client: AsyncClient) -> None:
    r = await client.get("/api/v1/sessions/no-such-session")
    assert r.status_code == 404


async def test_retry_prewarm_route_noop_and_unknown_404(client: AsyncClient) -> None:
    card = await _ingest(client)
    sid = card["session_id"]
    await _confirm(client, sid, "CONCEPT_BOTTLENECK")
    r = await client.post(f"/api/v1/sessions/{sid}/retry-prewarm")
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/sessions/no-such-session/retry-prewarm")
    assert r.status_code == 404


async def test_stage2_no_idea_descends_via_api(client: AsyncClient) -> None:
    """二阶输入「不会」→ 递归降阶：新铺垫题难度更低、层级+1（API 层回归）。"""
    card = await _ingest(client)
    sid = card["session_id"]
    await _confirm(client, sid, "CONCEPT_BOTTLENECK")
    resp = await _submit(client, sid, STAGE1_WRONG)
    assert resp["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    l0 = resp["view_payload"]["micro_scaffold"]["question_uid"]
    resp = await _submit(client, sid, "不会", None)
    assert resp["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    assert resp["view_payload"]["micro_scaffold"]["question_uid"] != l0
    assert resp["view_payload"]["scaffold_depth"] == 1
    assert resp["view_payload"].get("notice")


# ---------------------------------------------------------------------------
# P7-A：录入答题区 multipart 新字段（user_answer / mastered_claim）
# ---------------------------------------------------------------------------


async def test_ingest_multipart_mastered_claim(client: AsyncClient) -> None:
    """勾选「已掌握」（Form bool "true"）→ auto_mastered + 录入即结算。"""
    card = await _ingest(client, mastered_claim=True)
    assert card["auto_mastered"] is True
    assert card["mastery_explanation"] and "已掌握" in card["mastery_explanation"]
    assert card["error_category"] == "ALREADY_MASTERED"
    assert card["floor_preview_elo"] is None
    assert _one("SELECT mastery_state FROM questions")["mastery_state"] == "MASTERED"
    sess = _one("SELECT * FROM diagnostic_sessions")
    assert sess["micro_stage"] == "SESSION_COMPLETED"
    assert sess["user_confirmed_gate"] == 1
    assert (
        _one(
            "SELECT COUNT(*) AS n FROM elo_mutation_ledger "
            "WHERE mutation_trigger='MASTERED_INIT'"
        )["n"]
        == 1
    )
    steps = _query("SELECT is_blocked_step FROM question_steps")
    assert steps and all(s["is_blocked_step"] == 0 for s in steps)


@pytest.mark.parametrize("answer", ["已掌握", "20"])
async def test_ingest_multipart_user_answer_auto_mastered(
    client: AsyncClient, answer: str
) -> None:
    """声明文本 / 正确答案（Mock 标准答案 v_{max}=20 m/s）→ auto_mastered。"""
    card = await _ingest(client, user_answer=answer)
    assert card["auto_mastered"] is True
    assert card["mastery_explanation"] and "已掌握" in card["mastery_explanation"]
    assert _one("SELECT mastery_state FROM questions")["mastery_state"] == "MASTERED"
    assert _one("SELECT COUNT(*) AS n FROM elo_mutation_ledger")["n"] == 1


async def test_ingest_multipart_wrong_answer_structure_unchanged(client: AsyncClient) -> None:
    """错误答案 → 未掌握响应结构不变：auto_mastered=False + 默认门禁流程。"""
    card = await _ingest(client, user_answer="x=999")
    assert card["auto_mastered"] is False
    assert card["mastery_explanation"] is None
    assert card["error_category"] == "CONCEPT_BOTTLENECK"  # autouse 默认诊断
    assert card["is_reimport"] is False
    assert _one("SELECT mastery_state FROM questions")["mastery_state"] == "ACTIVE"
    sess = _one("SELECT * FROM diagnostic_sessions")
    assert sess["micro_stage"] == "STAGE_0_CONFIRM_GATE"
    assert sess["user_confirmed_gate"] == 0
    assert _one("SELECT COUNT(*) AS n FROM elo_mutation_ledger")["n"] == 0


async def test_ingest_multipart_mastered_claim_false_string(client: AsyncClient) -> None:
    """Form(bool) 接受 "false" → 未掌握默认流程零漂移。"""
    card = await _ingest(client, mastered_claim=False)
    assert card["auto_mastered"] is False
    assert card["mastery_explanation"] is None
    assert _one("SELECT mastery_state FROM questions")["mastery_state"] == "ACTIVE"
    assert _one("SELECT COUNT(*) AS n FROM elo_mutation_ledger")["n"] == 0


async def test_retry_prewarm_force_swap_and_completed_escrow_route(client: AsyncClient) -> None:
    """POST /retry-prewarm?force=true 重生变式且 SESSION_COMPLETED 回传 escrow_combat_delta。"""
    card = await _ingest(client)
    sid = card["session_id"]
    await _confirm(client, sid, "CONCEPT_BOTTLENECK")
    for task in list(scaffold_engine.in_flight_prewarm_tasks.values()):
        await task
    before = (await client.get(f"/api/v1/sessions/{sid}")).json()
    assert before["prewarm_status"] == "READY_SYMPY"

    r_force = await client.post(f"/api/v1/sessions/{sid}/retry-prewarm?force=true")
    assert r_force.status_code == 200
    assert r_force.json()["prewarm_status"] == "RUNNING"
    for task in list(scaffold_engine.in_flight_prewarm_tasks.values()):
        await task

    await _submit(client, sid, STAGE1_WRONG)
    await _submit(client, sid, "x = 5")
    await _submit(client, sid, STAGE3_CORRECT)
    done = await _submit(client, sid, STAGE4_CORE_EQUATION)
    assert done["micro_stage"] == "SESSION_COMPLETED"
    assert float(done["view_payload"]["escrow_combat_delta"]) > 0


async def test_ingest_upload_exceeding_max_bytes_returns_400(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MAX_UPLOAD_IMAGE_BYTES", 32)
    resp = await client.post(
        "/api/v1/questions/ingest",
        data={"stem_md": "超限图测试", "target_grade": GRADE, "initial_state": "错题"},
        files={"stem_images": ("big.png", b"x" * 64, "image/png")},
    )
    assert resp.status_code == 400
    assert "上限" in resp.json()["detail"]


async def test_drill_failure_returns_retrain_session_id_and_prewarms(
    client: AsyncClient,
) -> None:
    _insert_drill_question("q-ret")
    _insert_drill_scaffold("q-ret")
    _insert_pool("q-ret", escrow=20.0)
    resp = await client.post(
        "/api/v1/drills/daily",
        json={
            "question_uid": "q-ret",
            "final_answer": FINAL_WRONG,
            "intermediate_sub_answer": INTERMEDIATE_WRONG,
            "active_date": DAY.isoformat(),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["passed"] is False
    assert data["retrain_session_id"]
    sess_resp = await client.get(f"/api/v1/sessions/{data['retrain_session_id']}")
    assert sess_resp.status_code == 200
    assert sess_resp.json()["micro_stage"] == "STAGE_1_BLIND_PROBE"
    for task in list(scaffold_engine.in_flight_prewarm_tasks.values()):
        await task


# ---------------------------------------------------------------------------
# P10-E：学段必填 Query / Settings 条目 CRUD / 跨学段响应隔离 / 录入自动注册
# ---------------------------------------------------------------------------

ALT_GRADE = "初中数学"


@pytest.mark.parametrize(
    "path",
    ["/api/v1/knowledge-graph", "/api/v1/elo/ledger", "/api/v1/drills/daily"],
)
async def test_grade_read_endpoints_require_grade_subject(
    client: AsyncClient, path: str
) -> None:
    """P10-E：看板/账本/盲测读取端点缺 grade_subject → FastAPI 422（必填 Query）。"""
    resp = await client.get(path)
    assert resp.status_code == 422
    assert "grade_subject" in resp.text


async def test_ingest_auto_registers_grade_subject_entry(client: AsyncClient) -> None:
    """P10-D/E：录入即 get_or_create 学段条目 → settings list 立即可见且计数实时。"""
    assert (await client.get("/api/v1/settings/grade-subjects")).json() == []

    card = await _ingest(client)
    items = (await client.get("/api/v1/settings/grade-subjects")).json()
    assert [i["grade_subject"] for i in items] == [GRADE]
    assert items[0]["question_count"] == 1
    assert card["question_uid"]

    # 同学段再录一道（改题干避免幂等重入库）→ 条目仍唯一、计数递增
    await _ingest(client, f"{STEM_MD}\n（同学段第二题）")
    items = (await client.get("/api/v1/settings/grade-subjects")).json()
    assert [i["grade_subject"] for i in items] == [GRADE]
    assert items[0]["question_count"] == 2


async def test_settings_grade_subjects_crud_full_chain(client: AsyncClient) -> None:
    """Settings CRUD 全链：新建（幂等/空名 400）→ list → rename 级联 → delete 409/204。"""
    # 新建：strip 规范化 + created=True
    resp = await client.post(
        "/api/v1/settings/grade-subjects", json={"grade_subject": f"  {ALT_GRADE}  "}
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["created"] is True
    assert created["item"]["grade_subject"] == ALT_GRADE
    assert created["item"]["question_count"] == 0
    assert created["item"]["created_at"]
    # 幂等：同学段再建 → created=False，条目不变
    resp = await client.post(
        "/api/v1/settings/grade-subjects", json={"grade_subject": ALT_GRADE}
    )
    assert resp.status_code == 200
    assert resp.json()["created"] is False
    assert resp.json()["item"] == created["item"]
    # 空/纯空白名称 → 400
    resp = await client.post("/api/v1/settings/grade-subjects", json={"grade_subject": "   "})
    assert resp.status_code == 400
    assert "detail" in resp.json()

    # 真实数据：概念分支全链路（3 节点 / 3 标红边 / 2 账本事件，均为 GRADE 学段）
    card = await _run_full_chain(client)
    items = (await client.get("/api/v1/settings/grade-subjects")).json()
    assert [i["grade_subject"] for i in items] == [ALT_GRADE, GRADE]  # 按 created_at 升序

    scoped = {
        "questions": (
            "SELECT COUNT(*) AS n FROM questions WHERE target_grade_and_subject = ?",
            (GRADE,),
        ),
        "knowledge_nodes": (
            "SELECT COUNT(*) AS n FROM knowledge_nodes WHERE grade_subject = ?",
            (GRADE,),
        ),
        "elo_mutation_ledger": (
            "SELECT COUNT(*) AS n FROM elo_mutation_ledger WHERE grade_subject = ?",
            (GRADE,),
        ),
        "concept_co_errors": (
            "SELECT COUNT(*) AS n FROM concept_co_errors WHERE grade_subject = ?",
            (GRADE,),
        ),
        "compaction_audit_ledger": (
            "SELECT COUNT(*) AS n FROM compaction_audit_ledger WHERE grade_subject = ?",
            (GRADE,),
        ),
    }
    before = {key: int(_one(sql, params)["n"]) for key, (sql, params) in scoped.items()}
    assert items[1]["question_count"] == before["questions"]  # 条目计数实时统计
    assert before["questions"] >= 1
    assert before["knowledge_nodes"] == 3
    assert before["elo_mutation_ledger"] == 2
    assert before["concept_co_errors"] == 3

    # rename：五处级联改标，返回各表受影响行数
    new_grade = "高中物理-高三"
    resp = await client.patch(
        f"/api/v1/settings/grade-subjects/{GRADE}", json={"new_name": new_grade}
    )
    assert resp.status_code == 200, resp.text
    renamed = resp.json()
    assert renamed["renamed"] == new_grade
    assert renamed["affected"] == {
        "grade_subjects": 1,
        "questions": before["questions"],
        "knowledge_nodes": before["knowledge_nodes"],
        "elo_mutation_ledger": before["elo_mutation_ledger"],
        "concept_co_errors": before["concept_co_errors"],
        "compaction_audit_ledger": before["compaction_audit_ledger"],
    }
    # 旧学段零残留、新学段行数与改标前一致
    for key, (sql, _params) in scoped.items():
        assert int(_one(sql, (new_grade,))["n"]) == before[key]
        assert int(_one(sql, (GRADE,))["n"]) == 0
    assert _one(
        "SELECT target_grade_and_subject FROM questions WHERE question_uid = ?",
        (card["question_uid"],),
    )["target_grade_and_subject"] == new_grade
    # 条目主键亦已改标（list 中旧名消失、新名出现）
    listed = [
        i["grade_subject"] for i in (await client.get("/api/v1/settings/grade-subjects")).json()
    ]
    assert listed == [ALT_GRADE, new_grade]

    # rename 目标已存在 → 400（写前拦截，零残留）
    resp = await client.patch(
        f"/api/v1/settings/grade-subjects/{new_grade}", json={"new_name": ALT_GRADE}
    )
    assert resp.status_code == 400
    assert "已存在" in resp.json()["detail"]
    # rename 新名为空 → 400
    resp = await client.patch(
        f"/api/v1/settings/grade-subjects/{new_grade}", json={"new_name": "  "}
    )
    assert resp.status_code == 400
    # rename 原条目不存在 → 400
    resp = await client.patch(
        "/api/v1/settings/grade-subjects/不存在的学段", json={"new_name": "新学段"}
    )
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]

    # delete：有题条目 → 409（detail 含题目计数）；空条目 → 204 且 list 消失
    resp = await client.delete(f"/api/v1/settings/grade-subjects/{new_grade}")
    assert resp.status_code == 409
    assert str(before["questions"]) in resp.json()["detail"]
    assert new_grade in resp.json()["detail"]
    resp = await client.delete(f"/api/v1/settings/grade-subjects/{ALT_GRADE}")
    assert resp.status_code == 204
    assert resp.content == b""
    assert [
        i["grade_subject"] for i in (await client.get("/api/v1/settings/grade-subjects")).json()
    ] == [new_grade]


async def test_grade_partition_isolation_graph_elo_drill(client: AsyncClient) -> None:
    """P10-E：两学段同名 L1 各有题/节点/事件 → graph/elo/drill 响应逐学段隔离。"""
    card = await _run_full_chain(client)  # 学段 A（GRADE）：力学 3 节点/3 边/2 事件/1 托管
    # 学段 B（ALT_GRADE）：同 L1 名 MODULE 的题目 + 节点对 + 标红边 + 到期盲测题
    _insert_drill_question("q-alt", grade=ALT_GRADE, mastery="ACTIVE", unlock=None, due=None)
    _insert_drill_question("q-par-a")
    _insert_drill_question("q-par-b", grade=ALT_GRADE)
    _execute(
        "INSERT INTO knowledge_nodes (node_id, grade_subject, l1_module, l2_entity,"
        " l3_operator) VALUES ('nb1', ?, ?, '函数极限', '等价无穷小'),"
        " ('nb2', ?, ?, '函数极限', '洛必达')",
        (ALT_GRADE, MODULE, ALT_GRADE, MODULE),
    )
    _execute(
        "INSERT INTO question_steps (step_id, question_uid, step_index, step_goal_desc,"
        " step_formula_latex, knowledge_node_id, masked_conditions_json,"
        " probe_question_md, is_blocked_step, c_sub, s_sub, t_sub, l_sub, sub_elo)"
        " VALUES ('step-alt', 'q-alt', 1, '子目标', 'x=1', 'nb1', '[]', 'probe',"
        " 1, 1, 1, 1, 1, 900)",
    )
    _execute(
        "INSERT INTO concept_co_errors (node_u_id, node_v_id, grade_subject, l1_module_u,"
        " l1_module_v, active_error_count, total_historical_count, source_question_uids_json)"
        " VALUES ('nb1', 'nb2', ?, ?, ?, 3, 4, '[\"q-alt\"]')",
        (ALT_GRADE, MODULE, MODULE),
    )
    alt_params = {"grade_subject": ALT_GRADE}

    # graph：节点/标红边/指标逐学段隔离
    graph_a = (await client.get("/api/v1/knowledge-graph", params=GRAPH_PARAMS)).json()
    graph_b = (await client.get("/api/v1/knowledge-graph", params=alt_params)).json()
    nodes_a = {
        n["node_id"] for m in graph_a["modules"] for g in m["l2_groups"] for n in g["nodes"]
    }
    nodes_b = {
        n["node_id"] for m in graph_b["modules"] for g in m["l2_groups"] for n in g["nodes"]
    }
    assert len(nodes_a) == 3
    assert nodes_b == {"nb1", "nb2"}
    assert nodes_a.isdisjoint(nodes_b)
    assert [m["l1_module"] for m in graph_b["modules"]] == [MODULE]
    assert {(e["node_u_id"], e["node_v_id"]) for e in graph_b["red_edges"]} == {("nb1", "nb2")}
    assert all(
        e["node_u_id"] not in nodes_b and e["node_v_id"] not in nodes_b
        for e in graph_a["red_edges"]
    )
    assert len(graph_a["red_edges"]) == 3  # 全链路 3 节点两两成边，不含 B 学段边
    metrics_a = next(m for m in graph_a["modules"] if m["l1_module"] == "力学")["metrics"]
    metrics_b = next(m for m in graph_b["modules"] if m["l1_module"] == MODULE)["metrics"]
    assert metrics_a["active_nodes"] == 3
    assert metrics_b["active_nodes"] == 2
    assert metrics_b["mastery_rate"] == 0.0  # 仅 B 的 ACTIVE 卡壳挂载
    assert graph_b["compaction_batches"] == []

    # elo：事件/托管池/当前水位逐学段隔离（B 无事件 → 1200 冷启动）
    ledger_a = (await client.get("/api/v1/elo/ledger", params=GRAPH_PARAMS)).json()
    ledger_b = (await client.get("/api/v1/elo/ledger", params=alt_params)).json()
    la = next(m for m in ledger_a["modules"] if m["l1_module"] == "力学")
    lb = next(m for m in ledger_b["modules"] if m["l1_module"] == MODULE)
    assert [e["question_uid"] for e in la["events"]] == [card["question_uid"]] * 2
    assert lb["events"] == [] and lb["curve"] == []
    initial = settings.MODULE_ELO_INITIAL
    assert lb["current"] == {"coverage_elo": initial, "combat_elo": initial}
    assert lb["pending_escrows"] == []
    assert [e["question_uid"] for e in la["pending_escrows"]] == [card["question_uid"]]

    # drill：题单只含当前学段题（同名 L1、同一 active_date 互不串）
    drill_a = (await client.get("/api/v1/drills/daily", params=_drill_params(DAY))).json()
    drill_b = (
        await client.get(
            "/api/v1/drills/daily", params={**alt_params, "active_date": DAY.isoformat()}
        )
    ).json()
    assert "q-par-a" in [i["question_uid"] for i in drill_a]
    assert "q-par-b" not in [i["question_uid"] for i in drill_a]
    assert [i["question_uid"] for i in drill_b] == ["q-par-b"]
