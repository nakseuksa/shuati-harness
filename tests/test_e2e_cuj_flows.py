"""P4-C：CUJ 1~4 全链路 E2E 集成测试（设计 Target File #18 / Verification Plan）。

配置：USE_MOCK_LLM=True（Mock VLM/LLM 网关）+ 真实 SQLite + 真实 SymPy 子进程沙盒；
httpx.ASGITransport 直驱 create_app() 真实 ASGI 应用（手动触发 lifespan），零真实网络。
时间旅行一律经 drills 端点的 active_date 注入，不依赖真实时钟推进。

场景覆盖（任务契约 P4-C 逐条）：
- CUJ 1+2：multipart 录入（Pillow 合成题干配图+草稿图）→ 确认卡断言 → confirm-gate
  （CONCEPT_BOTTLENECK）→ PARTIAL_DRAFT_SUBGOAL_FLOOR 账本+解释条 → 一阶遮罩盲探视图
  （无原题目标剧透）→ 异步预热 READY_SYMPY → 一阶错→二阶微母题（origin_type /
  SCAFFOLD_PRE 边）→ 二阶错停留→对→三阶原题→对→四阶核心列式。
- CUJ 3：L>=2 equation_skeleton 拆拐杖 → PENDING_RETEST + 覆盖分结算 + escrow 冻结
  + 盲测窗口（概念 +3/+7、粗心 +2/+4）→ active_date 时间旅行 GET 题单（剥标签）→
  变式只核最终 / 原题回退中间量+最终双核验 → MASTERED + escrow 兑现 + 严格守恒
  （净增 ≡ target）→ 失败分支（ACTIVE + FORFEITED_RESET + 重训起点分流）→
  4 道到期题配额熔断（3 入单 1 顺延）与同日刷新幂等锁。
- CUJ 4：批量录入（诊断 fixture 变体，两个 L1 模块）→ node_judge 命中复用 →
  满 15 新节点自动 Compaction → 审计批次/标红边去自环归并 → 双指标 → 一键回滚
  逐字段恢复 + Elo 账本零变化（正交解耦）。
- 幂等与重入库：同内容重导入零新行；改题干保进度；新草稿新会话；
  Elo 事件重复提交不重复加分（跨端点幂等键 + 服务层重放 applied=False）。
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from backend.config import settings
from backend.db.schema import get_db_connection
from backend.main import create_app
from backend.services import (
    elo_engine,
    ingest_service,
    knowledge_graph,
    llm_gateway,
    llm_mocks,
    scaffold_engine,
)
from backend.services.knowledge_graph import NodeJudgeResult
from backend.services.vlm_diagnosis import DiagnosisPayload

# ---------------------------------------------------------------------------
# 常量：Mock 诊断 fixture（拱形桥临界速度题）下的确定性期望值
# ---------------------------------------------------------------------------

GRADE = "高中物理-高二"
STEM_MD = (
    "汽车以速度 $v$ 通过半径 $R = 40\\,\\text{m}$ 的拱形桥最高点，"
    "取 $g = 10\\,\\text{m/s}^2$。求汽车不脱离桥面的最大速度 $v_{max}$。"
)
BASE_ELO = 2085  # (C,S,T,L)=(3,3,2,3)
FLOOR_SUB_ELO = 1290  # Step 1 前缀子目标保底水位
TIER = "L5-1_压轴挑战"

STAGE1_WRONG = r"v_{max} = \sqrt{2gR}"
STAGE2_ANSWER = "x = 5"  # scaffold_kernel：3x + 5 = 20
STAGE2_WRONG = "x = 999"
STAGE3_ANSWER = r"v_{max} = 20\ \text{m/s}"
STAGE4_EQUATION = "2*(x + 3) = 16"  # 与 x = 5 常数比率等价（半程立式）

# 纯代数 fixture 变体（盲测双重核验需要可被 SymPy 解析的 plain 公式）
PLAIN_STEM = (
    "[L1]力学 / [L2]圆周运动\n"
    "【来源】2026 届高二物理月考压轴题\n"
    "汽车以速度 $v$ 通过半径 $R = 40\\,\\text{m}$ 的拱形桥最高点，"
    "取 $g = 10\\,\\text{m/s}^2$。求汽车不脱离桥面的最大速度。"
)
PLAIN_INTERMEDIATE_STD = "R = 2*d"
PLAIN_INTERMEDIATE_RIGHT = "R/2 = d"
PLAIN_INTERMEDIATE_WRONG = "R = d"
PLAIN_FINAL_RIGHT = "x = 2"
PLAIN_FINAL_WRONG = "x = 9"

# CUJ 4：两个 L1 模块 + 近义节点对（alignment 复用对 / compaction 合并对）
MODULE_EM = "电磁学"
L2_EM = "电磁感应单双杆模型"
MODULE_FN = "函数与导数"
L2_FN = "导数应用"
NAME_A = "动量定理求电荷量"
NAME_B = "动量定理求电荷量大小与位移"
PAIR_A = "能量守恒求焦耳热"
PAIR_B = "能量守恒求焦耳热大小"

DAY = date(2026, 11, 12)  # 配额测试固定时间旅行日


# ---------------------------------------------------------------------------
# 夹具：环境隔离 / ASGI 客户端 / 预热 Task 清理 / 诊断 fixture 注入
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
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    app = create_app(frontend_dir=frontend)
    async with app.router.lifespan_context(app):  # 触发 lifespan：init_db()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as async_client:
            yield async_client


@pytest.fixture()
def install_diagnosis(monkeypatch: pytest.MonkeyPatch) -> Any:
    """monkeypatch VLM 诊断：注入定制 DiagnosisPayload（fixture 变体）。"""

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


def _payload(category: str = "CONCEPT_BOTTLENECK") -> DiagnosisPayload:
    payload = DiagnosisPayload.model_validate(copy.deepcopy(llm_mocks.MOCK_RESPONSES["diagnosis"]))
    payload.alignment.error_category = category  # type: ignore[assignment]
    payload.alignment.trap_taxonomy_tag = (
        "TRAP_DOMAIN_BOUNDARY" if category == "TRAP_OR_CALC_ERROR" else None
    )
    return payload


@pytest.fixture(autouse=True)
def _default_diagnosis(install_diagnosis: Any) -> None:
    """默认诊断：概念卡点（Mock 原始 fixture 为 TRAP，测试内可再注入覆盖）。"""
    install_diagnosis(_payload("CONCEPT_BOTTLENECK"))


def _plain_trap_payload(trap_tag: str) -> DiagnosisPayload:
    """纯代数 TRAP fixture 变体：卡壳步/标准答案为 plain 公式（可过 SymPy 等价判据）。

    每题独立 trap_tag，避免 SHARED_TRAP 本地检索跨题复用变式。
    """
    payload = _payload("TRAP_OR_CALC_ERROR")
    payload.trace.standard_answer_latex = PLAIN_FINAL_RIGHT
    payload.trace.steps[1].step_formula_latex = PLAIN_INTERMEDIATE_STD
    payload.trace.steps[1].probe_question_md = "临界半径 $R$ 与磁场宽度 $d$ 满足什么几何等量关系？"
    payload.trace.trap_tags = [trap_tag]
    payload.alignment.trap_taxonomy_tag = trap_tag  # type: ignore[assignment]
    return payload


def _module_payload(l1: str, l2: str, l3_names: list[str]) -> DiagnosisPayload:
    """CUJ 4 批量录入 fixture 变体：三步骨架挂到指定 (L1, L2) 与三个 L3 算子名。"""
    payload = _payload("CONCEPT_BOTTLENECK")
    for step, name in zip(payload.trace.steps, l3_names, strict=True):
        step.l1_module = l1
        step.l2_entity = l2
        step.l3_operator = name
    return payload


# ---------------------------------------------------------------------------
# 工具：multipart 录入 / SQL 直查 / 阶段提交 / 预热等待 / Elo 期望值
# ---------------------------------------------------------------------------


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


async def _ingest(
    client: AsyncClient,
    stem_md: str = STEM_MD,
    *,
    with_images: bool = False,
    draft_color: tuple[int, int, int] = (200, 30, 30),
) -> dict[str, Any]:
    data = {"stem_md": stem_md, "target_grade": GRADE, "initial_state": "错题"}
    files = []
    if with_images:
        files = [
            ("stem_images", ("stem.png", _png_bytes((30, 30, 200)), "image/png")),
            ("draft_images", ("draft.png", _png_bytes(draft_color), "image/png")),
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


def _count(table: str) -> int:
    return int(_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])


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


async def _await_prewarm() -> None:
    """等待思考窗口异步预热 Task 完成（真实 SymPy 沙盒验算）。"""
    tasks = list(scaffold_engine.in_flight_prewarm_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


async def _concept_chain(client: AsyncClient) -> dict[str, Any]:
    """CUJ 1+2 概念分支全链路：录入(带图) → 门禁 → 一阶错 → 二阶对 → 三阶对 → 四阶对。"""
    card = await _ingest(client, with_images=True)
    sid = card["session_id"]
    resp = await _confirm(client, sid, "CONCEPT_BOTTLENECK")
    assert resp["micro_stage"] == "STAGE_1_BLIND_PROBE"
    resp = await _submit(client, sid, STAGE1_WRONG)
    assert resp["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    resp = await _submit(client, sid, STAGE2_ANSWER)
    assert resp["micro_stage"] == "STAGE_3_REDO_ORIGINAL"
    resp = await _submit(client, sid, STAGE3_ANSWER)
    assert resp["micro_stage"] == "STAGE_4_DECRUTCH_VARIANT"
    resp = await _submit(client, sid, STAGE4_EQUATION)
    assert resp["micro_stage"] == "SESSION_COMPLETED"
    assert resp["mastery_state"] == "PENDING_RETEST"
    return card


async def _trap_chain(
    client: AsyncClient, install_diagnosis: Any, *, trap_tag: str
) -> dict[str, Any]:
    """TRAP 分支（纯代数变体）：录入 → 门禁直入四阶 → 核心列式通关（窗口 +2/+4）。"""
    install_diagnosis(_plain_trap_payload(trap_tag))
    # 每题独立题干（避免同内容幂等重入库复用已确认会话）
    card = await _ingest(client, f"{PLAIN_STEM}\n（题组编号：{trap_tag}）", with_images=True)
    sid = card["session_id"]
    resp = await _confirm(client, sid, "TRAP_OR_CALC_ERROR")
    assert resp["micro_stage"] == "STAGE_4_DECRUTCH_VARIANT"
    assert resp["prewarm_status"] == "READY_SYMPY"
    resp = await _submit(client, sid, STAGE4_EQUATION)
    assert resp["micro_stage"] == "SESSION_COMPLETED"
    assert resp["mastery_state"] == "PENDING_RETEST"
    return card


def _expected_pass(rating: float, difficulty: float) -> float:
    """标准期望通过率 E(R, D)（独立重算，不依赖被测引擎）。"""
    return 1.0 / (1.0 + 10.0 ** ((difficulty - rating) / 400.0))


def _module_ledger(client_body: dict[str, Any], module: str) -> dict[str, Any]:
    return next(m for m in client_body["modules"] if m["l1_module"] == module)


async def _get_ledger(client: AsyncClient) -> dict[str, Any]:
    resp = await client.get("/api/v1/elo/ledger")
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


async def _get_graph(client: AsyncClient) -> dict[str, Any]:
    resp = await client.get("/api/v1/knowledge-graph")
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


def _node_id(l3_operator: str) -> str:
    return str(_one("SELECT node_id FROM knowledge_nodes WHERE l3_operator = ?", (l3_operator,))[
        "node_id"
    ])


def _drill_day(offset: int) -> str:
    """盲测窗口基准日：服务端用 datetime.now(UTC)，时间旅行注入与其对齐。"""
    return (datetime.now(UTC).date() + timedelta(days=offset)).isoformat()


# ---------------------------------------------------------------------------
# CUJ 1：多模态录入 → 步骤对齐确认卡 → 保底门禁 → 一阶遮罩盲探
# ---------------------------------------------------------------------------


async def test_cuj1_multipart_ingest_gate_card_and_cas_archive(client: AsyncClient) -> None:
    """multipart（题干+配图+草稿图，Pillow 合成）→ 确认卡步骤状态/保底水位 + CAS 归档。"""
    card = await _ingest(client, with_images=True)
    assert card["session_id"] and card["question_uid"]
    assert card["is_reimport"] is False
    assert card["base_elo"] == BASE_ELO
    assert card["tier"] == TIER
    assert card["error_category"] == "CONCEPT_BOTTLENECK"
    assert card["floor_preview_elo"] == FLOOR_SUB_ELO
    # 步骤对齐确认卡：Step1 已完成 / Step2 卡壳 / Step3 未触及
    assert [s["step_index"] for s in card["steps"]] == [1, 2, 3]
    assert [s["status"] for s in card["steps"]] == ["completed", "blocked", "untouched"]
    assert card["steps"][0]["sub_elo"] == FLOOR_SUB_ELO

    # 图片 SHA-256 CAS 归档（WebP 转码）；草稿 URI 落会话表；/assets 可回读
    session = _one(
        "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card["session_id"],)
    )
    assert session["micro_stage"] == "STAGE_0_CONFIRM_GATE"
    assert session["user_confirmed_gate"] == 0
    draft_uris = json.loads(session["draft_image_cas_uris_json"])
    assert len(draft_uris) == 1 and draft_uris[0].startswith("cas://")
    assert len(list(settings.CAS_ASSETS_DIR.rglob("*.webp"))) == 2  # 题干配图 + 草稿
    digest = draft_uris[0].removeprefix("cas://")
    asset = await client.get(f"/assets/{digest}")
    assert asset.status_code == 200
    assert asset.headers["content-type"] == "image/webp"
    # 落库：题目 ACTIVE + 三步骨架 + 知识网节点（零预设冷启动生长）
    question = _one("SELECT * FROM questions WHERE question_uid = ?", (card["question_uid"],))
    assert question["mastery_state"] == "ACTIVE"
    assert question["origin_type"] == "USER_CANONICAL"
    assert _count("question_steps") == 3
    assert _count("knowledge_nodes") == 3


async def test_cuj1_confirm_gate_floor_ledger_and_masked_probe(client: AsyncClient) -> None:
    """confirm-gate（CONCEPT_BOTTLENECK）→ 保底账本+解释条 → 一阶遮罩视图（无剧透）。"""
    card = await _ingest(client, with_images=True)
    resp = await _confirm(client, card["session_id"], "CONCEPT_BOTTLENECK")
    assert resp["micro_stage"] == "STAGE_1_BLIND_PROBE"
    assert resp["mastery_state"] == "ACTIVE"
    assert resp["explanation_text"] and "保底" in resp["explanation_text"]

    # 账本：PARTIAL_DRAFT_SUBGOAL_FLOOR 事件 + 双轨不变量（combat <= coverage）
    event = _one(
        "SELECT * FROM elo_mutation_ledger WHERE mutation_trigger = ?",
        ("PARTIAL_DRAFT_SUBGOAL_FLOOR",),
    )
    assert event["question_uid"] == card["question_uid"]
    assert event["eval_difficulty_elo"] == FLOOR_SUB_ELO
    assert event["explanation_text"]
    floor_delta = settings.K_FLOOR * (
        1.0 - _expected_pass(settings.MODULE_ELO_INITIAL, FLOOR_SUB_ELO)
    )
    assert float(event["combat_elo_after"]) == pytest.approx(
        settings.MODULE_ELO_INITIAL + floor_delta
    )
    assert float(event["coverage_elo_after"]) >= float(event["combat_elo_after"])
    pool = _one(
        "SELECT * FROM question_escrow_pool WHERE question_uid = ?", (card["question_uid"],)
    )
    assert pool["escrow_status"] == "FLOOR_ONLY"
    assert float(pool["floor_elo_applied"]) == pytest.approx(floor_delta)

    # 一阶盲探视图：仅遮罩条件 + probe，不展开原题题干/最终目标（防剧透）
    view = resp["view_payload"]
    assert view["stalled_step_index"] == 2
    assert view["masked_conditions"]
    assert view["probe_question_md"]
    assert "stem_md" not in view
    serialized = json.dumps(view, ensure_ascii=False)
    assert r"\sqrt{gR}" not in serialized  # 核心中间结论不剧透
    assert r"20\ \text{m/s}" not in serialized  # 最终答案不剧透

    # 思考窗口异步预热：confirm 返回时 RUNNING（或已完成），await 后 READY_SYMPY
    assert resp["prewarm_status"] in ("RUNNING", "READY_SYMPY", "READY_FALLBACK")
    await _await_prewarm()
    session = _one(
        "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (card["session_id"],)
    )
    assert session["prewarm_status"] == "READY_SYMPY"
    assert session["prewarm_scaffold_uid"] and session["prewarm_variant_uid"]


# ---------------------------------------------------------------------------
# CUJ 2：三阶倒置脚手架（一阶错 → 二阶微母题 → 三阶原题 → 四阶拆拐杖）
# ---------------------------------------------------------------------------


async def test_cuj2_three_stage_scaffold_to_decrutch(client: AsyncClient) -> None:
    """一阶答错→二阶微母题（合成题沉淀+SCAFFOLD_PRE 边）→答对→三阶原题→答对→四阶。"""
    card = await _ingest(client, with_images=True)
    sid = card["session_id"]
    await _confirm(client, sid, "CONCEPT_BOTTLENECK")

    # 一阶答错 → 二阶脱壳微母题（预热已强校验）
    resp = await _submit(client, sid, STAGE1_WRONG)
    assert resp["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    assert resp["prewarm_status"] == "READY_SYMPY"
    micro = resp["view_payload"]["micro_scaffold"]
    assert "3x + 5 = 20" in micro["stem_md"]  # 剥离电磁学背景的单步代数题
    assert micro["verification_status"] == "SYMPY_VERIFIED"

    # 合成微母题沉淀入库：origin_type + 指向原题的 SCAFFOLD_PRE 有向边
    scaffold_uid = micro["question_uid"]
    scaffold = _one("SELECT * FROM questions WHERE question_uid = ?", (scaffold_uid,))
    assert scaffold["origin_type"] == "AI_SYNTHETIC_SCAFFOLD"
    assert scaffold["parent_question_uid"] == card["question_uid"]
    assert scaffold["verification_status"] == "SYMPY_VERIFIED"
    edge = _one(
        "SELECT * FROM question_relations WHERE source_question_uid = ?"
        " AND target_question_uid = ? AND relation_type = 'SCAFFOLD_PRE'",
        (scaffold_uid, card["question_uid"]),
    )
    assert edge["is_directed"] == 1
    assert edge["target_step_index"] == 2  # 针对卡壳步
    assert edge["confidence_source"] == "SYMPY_KERNEL_GEN"

    # 二阶答错 → 停留二阶（反馈条，不剧透正确答案）
    resp = await _submit(client, sid, STAGE2_WRONG)
    assert resp["micro_stage"] == "STAGE_2_MICRO_SCAFFOLD"
    assert resp["view_payload"]["feedback"]
    assert resp["view_payload"]["last_submission_correct"] is False

    # 二阶答对 → 三阶撤提示回原题（完整题干重新展开）
    resp = await _submit(client, sid, STAGE2_ANSWER)
    assert resp["micro_stage"] == "STAGE_3_REDO_ORIGINAL"
    assert "拱形桥" in resp["view_payload"]["stem_md"]
    assert "micro_scaffold" not in resp["view_payload"]

    # 三阶答对 → 四阶当场拆拐杖（L=3 >= 2 → 半程立式核心列式模式）
    resp = await _submit(client, sid, STAGE3_ANSWER)
    assert resp["micro_stage"] == "STAGE_4_DECRUTCH_VARIANT"
    view = resp["view_payload"]
    assert view["verification_mode"] == "equation_skeleton"
    assert view["allowed_symbols"] == ["x"]
    assert view["variant"]["stem_md"]

    # 四阶核心方程等价提交 → 会话完成，宏观升级 PENDING_RETEST
    resp = await _submit(client, sid, STAGE4_EQUATION)
    assert resp["micro_stage"] == "SESSION_COMPLETED"
    assert resp["mastery_state"] == "PENDING_RETEST"
    assert resp["view_payload"]["blind_test_unlock_at"]
    assert resp["view_payload"]["blind_test_due_at"]


# ---------------------------------------------------------------------------
# CUJ 3：拆拐杖结算 → 延迟盲测销账（守恒）→ 失败重训 → 配额顺延
# ---------------------------------------------------------------------------


async def test_cuj3_settlement_window_blind_mastered_conservation(client: AsyncClient) -> None:
    """L>=2 拆拐杖 → 覆盖分结算+escrow 冻结+窗口(+3/+7) → +4 天盲测变式销账 → 严格守恒。"""
    card = await _concept_chain(client)
    uid = card["question_uid"]

    # 覆盖分一次性结算 + 托管差额 100% 冻结 + 概念窗口 [+3, +7]
    question = _one("SELECT * FROM questions WHERE question_uid = ?", (uid,))
    assert question["mastery_state"] == "PENDING_RETEST"
    assert question["blind_test_unlock_at"][:10] == _drill_day(3)
    assert question["blind_test_due_at"][:10] == _drill_day(7)
    pool = _one("SELECT * FROM question_escrow_pool WHERE question_uid = ?", (uid,))
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND"
    assert pool["is_cov_settled"] == 1
    assert float(pool["decay_factor"]) == 1.0

    combat0 = settings.MODULE_ELO_INITIAL
    floor_delta = settings.K_FLOOR * (1.0 - _expected_pass(combat0, FLOOR_SUB_ELO))
    cov_after_floor = max(combat0, combat0 + floor_delta)
    cov_delta = settings.K_COV * (1.0 - _expected_pass(cov_after_floor, BASE_ELO))
    target = settings.K_COMBAT * (1.0 - _expected_pass(combat0, BASE_ELO))
    escrow = max(0.0, target - floor_delta)
    assert float(pool["initial_combat_elo_snapshot"]) == pytest.approx(combat0)
    assert float(pool["floor_elo_applied"]) == pytest.approx(floor_delta)
    assert float(pool["escrow_combat_delta"]) == pytest.approx(escrow)
    assert floor_delta + escrow == pytest.approx(target)  # 守恒：保底 + 托管 ≡ 目标

    ledger = _module_ledger(await _get_ledger(client), "力学")
    assert [e["mutation_trigger"] for e in ledger["events"]] == [
        "PARTIAL_DRAFT_SUBGOAL_FLOOR",
        "SCAFFOLD_COVER_GAIN",
    ]
    assert ledger["current"]["coverage_elo"] == pytest.approx(cov_after_floor + cov_delta)
    assert ledger["current"]["combat_elo"] == pytest.approx(combat0 + floor_delta)
    assert ledger["current"]["coverage_elo"] > ledger["current"]["combat_elo"]  # 水分区
    assert len(ledger["pending_escrows"]) == 1

    # 时间旅行 +4 天：题单剥标签，优先未曝光 ISOMORPHIC 变式（只核最终答案）
    resp = await client.get("/api/v1/drills/daily", params={"active_date": _drill_day(4)})
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["question_uid"] == uid
    assert item["drill_question_uid"] != uid  # 变式优先，隐藏原题来源
    assert item["requires_intermediate"] is False
    assert item["probe_question_md"] is None
    assert "矩形" in item["stem_md"] and "拱形桥" not in item["stem_md"]
    assert "question_uid" not in item["stem_md"]
    variant = _one(
        "SELECT * FROM questions WHERE question_uid = ?", (item["drill_question_uid"],)
    )
    assert variant["origin_type"] == "AI_SYNTHETIC_VARIANT"

    # 变式只核最终答案 → MASTERED + escrow 全额兑现
    resp = await client.post(
        "/api/v1/drills/daily",
        json={"question_uid": uid, "final_answer": STAGE2_ANSWER, "active_date": _drill_day(4)},
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["passed"] is True
    assert outcome["mastery_state"] == "MASTERED"
    assert outcome["explanation_text"] and "解冻" in outcome["explanation_text"]
    pool = _one("SELECT * FROM question_escrow_pool WHERE question_uid = ?", (uid,))
    assert pool["escrow_status"] == "REALIZED_MASTERED"
    attempt = _one("SELECT * FROM blind_drill_attempts WHERE question_uid = ?", (uid,))
    assert attempt["active_date"] == _drill_day(4)
    assert attempt["intermediate_correct"] is None  # 变式不要求中间量

    # 严格守恒：decay=1.0 时累计实战分净增 ≡ target（与直接独立做对等价）
    ledger = _module_ledger(await _get_ledger(client), "力学")
    assert [e["mutation_trigger"] for e in ledger["events"]] == [
        "PARTIAL_DRAFT_SUBGOAL_FLOOR",
        "SCAFFOLD_COVER_GAIN",
        "BLIND_TEST_REALIZE",
    ]
    assert ledger["current"]["combat_elo"] == pytest.approx(combat0 + target)
    assert ledger["current"]["coverage_elo"] >= ledger["current"]["combat_elo"]
    assert ledger["pending_escrows"] == []

    # 重复提交：已销账题目拒绝二次兑现（跨端点幂等，不重复加分）
    resp = await client.post(
        "/api/v1/drills/daily",
        json={"question_uid": uid, "final_answer": STAGE2_ANSWER, "active_date": _drill_day(4)},
    )
    assert resp.status_code == 400
    ledger2 = _module_ledger(await _get_ledger(client), "力学")
    assert ledger2["current"] == ledger["current"]
    assert len(ledger2["events"]) == 3


async def test_cuj3_original_fallback_double_verification_pass(
    client: AsyncClient, install_diagnosis: Any
) -> None:
    """TRAP 窗口(+2/+4) → 回退原题：中间量+最终答案双核验销账（防背题）。"""
    card = await _trap_chain(client, install_diagnosis, trap_tag="TRAP_DOMAIN_BOUNDARY")
    uid = card["question_uid"]
    question = _one("SELECT * FROM questions WHERE question_uid = ?", (uid,))
    assert question["blind_test_unlock_at"][:10] == _drill_day(2)
    assert question["blind_test_due_at"][:10] == _drill_day(4)
    # TRAP 变式为 SHARED_TRAP（非 CLONE/ISOMORPHIC）→ 题单回退原题
    resp = await client.get("/api/v1/drills/daily", params={"active_date": _drill_day(3)})
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["drill_question_uid"] == uid
    assert item["requires_intermediate"] is True
    assert item["probe_question_md"]  # 携带原会话卡壳步 probe
    assert item["verification_mode"] == "equation_skeleton"
    # 剥标签：[L1]/[L2] 路径行、来源行、frontmatter 元数据全部剥离
    assert "拱形桥" in item["stem_md"]
    assert "[L1]" not in item["stem_md"]
    assert "【来源】" not in item["stem_md"]
    assert "question_uid" not in item["stem_md"]

    # 冻结态托管差额 ≡ target（TRAP 无保底）；兑现后池内差额清零合入实战分
    target = settings.K_COMBAT * (1.0 - _expected_pass(settings.MODULE_ELO_INITIAL, BASE_ELO))
    pool = _one("SELECT * FROM question_escrow_pool WHERE question_uid = ?", (uid,))
    assert pool["escrow_status"] == "FROZEN_PENDING_BLIND"
    assert float(pool["floor_elo_applied"]) == 0.0
    assert float(pool["escrow_combat_delta"]) == pytest.approx(target)

    # 中间量（代数等价变形）+ 最终答案双正确 → 销账
    resp = await client.post(
        "/api/v1/drills/daily",
        json={
            "question_uid": uid,
            "final_answer": PLAIN_FINAL_RIGHT,
            "intermediate_sub_answer": PLAIN_INTERMEDIATE_RIGHT,
            "active_date": _drill_day(3),
        },
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["passed"] is True
    assert outcome["mastery_state"] == "MASTERED"
    attempt = _one("SELECT * FROM blind_drill_attempts WHERE question_uid = ?", (uid,))
    assert attempt["intermediate_correct"] == 1
    assert attempt["final_correct"] == 1
    pool = _one("SELECT * FROM question_escrow_pool WHERE question_uid = ?", (uid,))
    assert pool["escrow_status"] == "REALIZED_MASTERED"
    # 兑现后净增严格守恒：combat 净增 ≡ target（decay=1.0 全额解冻）
    ledger = _module_ledger(await _get_ledger(client), "力学")
    assert ledger["current"]["combat_elo"] == pytest.approx(
        settings.MODULE_ELO_INITIAL + target
    )


async def test_cuj3_blind_failure_forfeit_and_retrain_routing(
    client: AsyncClient, install_diagnosis: Any
) -> None:
    """盲测失败 → ACTIVE + escrow 清零 + 重训会话起点智能分流（两分支）。"""
    card_a = await _trap_chain(client, install_diagnosis, trap_tag="TRAP_SIGN_DIRECTION")
    card_b = await _trap_chain(client, install_diagnosis, trap_tag="TRAP_EXTRANEOUS_ROOT")
    ledger_before = _module_ledger(await _get_ledger(client), "力学")["current"]

    # 分支 A：中间子目标也错（即使最终对）→ 从一阶盲探重训
    resp = await client.post(
        "/api/v1/drills/daily",
        json={
            "question_uid": card_a["question_uid"],
            "final_answer": PLAIN_FINAL_RIGHT,
            "intermediate_sub_answer": PLAIN_INTERMEDIATE_WRONG,
            "active_date": _drill_day(3),
        },
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["passed"] is False
    assert outcome["mastery_state"] == "ACTIVE"
    assert outcome["retrain_stage"] == "STAGE_1_BLIND_PROBE"
    pool = _one(
        "SELECT * FROM question_escrow_pool WHERE question_uid = ?", (card_a["question_uid"],)
    )
    assert pool["escrow_status"] == "FORFEITED_RESET"
    assert float(pool["escrow_combat_delta"]) == 0.0

    # 分支 B：中间子目标对、仅最终计算错 → 直接从四阶拆拐杖重训
    resp = await client.post(
        "/api/v1/drills/daily",
        json={
            "question_uid": card_b["question_uid"],
            "final_answer": PLAIN_FINAL_WRONG,
            "intermediate_sub_answer": PLAIN_INTERMEDIATE_RIGHT,
            "active_date": _drill_day(3),
        },
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["passed"] is False
    assert outcome["mastery_state"] == "ACTIVE"
    assert outcome["retrain_stage"] == "STAGE_4_DECRUTCH_VARIANT"

    # 重训会话已创建（门禁预通过），起点与分流一致；失败绝不倒扣实战分基线
    for card, stage in (
        (card_a, "STAGE_1_BLIND_PROBE"),
        (card_b, "STAGE_4_DECRUTCH_VARIANT"),
    ):
        retrain = _one(
            "SELECT * FROM diagnostic_sessions WHERE question_uid = ?"
            " AND session_id LIKE 'retrain-%'",
            (card["question_uid"],),
        )
        assert retrain["micro_stage"] == stage
        assert retrain["user_confirmed_gate"] == 1
    ledger_after = _module_ledger(await _get_ledger(client), "力学")["current"]
    assert ledger_after == ledger_before


async def test_cuj3_daily_quota_defer_and_idempotent_lock(client: AsyncClient) -> None:
    """4 道到期题 → 3 入单 1 顺延；同日刷新幂等；清队后次日顺延题入单。"""
    for i in range(1, 5):
        uid = f"q-quota-{i}"
        _execute(
            "INSERT INTO questions (question_uid, origin_type, target_grade_and_subject,"
            " stem_md, stem_hash, standard_answer_latex, c_score, s_score, t_score, l_score,"
            " base_elo, tier, primary_l1_module, mastery_state, blind_test_unlock_at,"
            " blind_test_due_at) VALUES (?, 'USER_CANONICAL', ?, ?, ?, ?, 2, 2, 2, 2,"
            " 2085, 'L3_综合中档', ?, 'PENDING_RETEST', ?, ?)",
            (
                uid,
                GRADE,
                f"题干 {uid}：求 x。",
                f"hash-{uid}",
                PLAIN_FINAL_RIGHT,
                MODULE_EM,
                (DAY - timedelta(days=1)).isoformat(),
                (DAY + timedelta(days=3)).isoformat(),
            ),
        )
        _execute(
            "INSERT INTO knowledge_nodes (node_id, l1_module, l2_entity, l3_operator)"
            " VALUES (?, ?, ?, ?)",
            (f"node-{uid}", MODULE_EM, f"实体-{uid}", f"算子-{uid}"),
        )
        _execute(
            "INSERT INTO question_steps (step_id, question_uid, step_index, step_goal_desc,"
            " step_formula_latex, knowledge_node_id, masked_conditions_json, probe_question_md,"
            " is_blocked_step, c_sub, s_sub, t_sub, l_sub, sub_elo)"
            " VALUES (?, ?, 2, '卡壳步子目标', ?, ?, '[]', '中间关系式是什么？',"
            " 1, 1, 1, 1, 1, 900)",
            (f"step-{uid}", uid, PLAIN_INTERMEDIATE_STD, f"node-{uid}"),
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

    # 首次 GET：按 unlock ASC（同日按 uid）锁定前 3，第 4 道顺延 +1 天
    resp = await client.get("/api/v1/drills/daily", params={"active_date": DAY.isoformat()})
    assert resp.status_code == 200
    items = resp.json()
    assert [i["question_uid"] for i in items] == ["q-quota-1", "q-quota-2", "q-quota-3"]
    deferred = _one("SELECT * FROM questions WHERE question_uid = 'q-quota-4'")
    assert deferred["system_deferred_days"] == 1
    assert deferred["blind_test_unlock_at"] == DAY.isoformat()
    assert deferred["blind_test_due_at"] == (DAY + timedelta(days=4)).isoformat()
    assert deferred["slated_drill_date"] is None  # 未入单 → 衰减计时天然冻结
    for i in range(1, 4):
        row = _one("SELECT * FROM questions WHERE question_uid = ?", (f"q-quota-{i}",))
        assert row["slated_drill_date"] == DAY.isoformat()
        assert row["slated_exposure_count"] == 1

    # 同日重复刷新：幂等返回锁定题单，不重复递增曝光、不重复顺延
    resp = await client.get("/api/v1/drills/daily", params={"active_date": DAY.isoformat()})
    assert [i["question_uid"] for i in resp.json()] == [
        "q-quota-1",
        "q-quota-2",
        "q-quota-3",
    ]
    for i in range(1, 4):
        row = _one("SELECT * FROM questions WHERE question_uid = ?", (f"q-quota-{i}",))
        assert row["slated_exposure_count"] == 1
    deferred = _one("SELECT * FROM questions WHERE question_uid = 'q-quota-4'")
    assert deferred["system_deferred_days"] == 1

    # 当日清队（双核验销账）→ 已作答者自然离队，同日刷新返回空
    for i in range(1, 4):
        resp = await client.post(
            "/api/v1/drills/daily",
            json={
                "question_uid": f"q-quota-{i}",
                "final_answer": PLAIN_FINAL_RIGHT,
                "intermediate_sub_answer": PLAIN_INTERMEDIATE_RIGHT,
                "active_date": DAY.isoformat(),
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["mastery_state"] == "MASTERED"
    resp = await client.get("/api/v1/drills/daily", params={"active_date": DAY.isoformat()})
    assert resp.json() == []

    # 次日：顺延题解锁入单（曝光从 1 开始）
    next_day = (DAY + timedelta(days=1)).isoformat()
    resp = await client.get("/api/v1/drills/daily", params={"active_date": next_day})
    assert [i["question_uid"] for i in resp.json()] == ["q-quota-4"]
    deferred = _one("SELECT * FROM questions WHERE question_uid = 'q-quota-4'")
    assert deferred["slated_drill_date"] == next_day
    assert deferred["slated_exposure_count"] == 1


def _execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with get_db_connection() as conn:
        conn.execute(sql, params)


# ---------------------------------------------------------------------------
# CUJ 4：知识网生长 → 节点复用 → 满 15 Compaction → 指标 → 一键回滚
# ---------------------------------------------------------------------------


@pytest.fixture()
def install_graph_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """node_judge mock 命中规则：精确同名/NAME_B 近义复用；compaction 仅确认 PAIR 对。"""

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
        existing = payload["existing_nodes"]
        candidates = payload["candidate"]["names"]
        if payload["task"] == "match_or_create":
            for node in existing:
                if candidates[0] in node["names"]:
                    return NodeJudgeResult(match_node_id=node["node_id"], evidence="精确同名命中")
            if NAME_B in candidates:
                for node in existing:
                    if NAME_A in node["names"]:
                        return NodeJudgeResult(
                            match_node_id=node["node_id"], evidence="近义命中，复用主 ID"
                        )
            return NodeJudgeResult(match_node_id=None, evidence="未命中，新建节点")
        # scoped_compaction：仅确认 PAIR_B -> PAIR_A 近义合并，其余一律拒绝
        to_node = existing[0]
        if PAIR_A in to_node["names"] and PAIR_B in candidates:
            return NodeJudgeResult(match_node_id=to_node["node_id"], evidence="确认近义合并")
        return NodeJudgeResult(match_node_id=None, evidence="语义不同一，拒绝合并")

    monkeypatch.setattr(llm_gateway, "chat_json", judge)


def _snapshot_rows(table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in _query(f"SELECT * FROM {table} ORDER BY 1")]


async def test_cuj4_growth_reuse_compaction_metrics_and_revert(
    client: AsyncClient, install_diagnosis: Any, install_graph_judge: None
) -> None:
    """批量录入（两个 L1 模块）→ 复用 → 满 15 自动 Compaction → 指标 → 回滚逐字段恢复。"""
    # Q1：电磁学首题（带草稿）→ 门禁保底，让 Elo 账本非空（验证回滚正交性）
    install_diagnosis(_module_payload(MODULE_EM, L2_EM, [NAME_A, PAIR_A, "磁通量变化与电荷量"]))
    q1 = await _ingest(client, "电磁感应双杆模型题 1：求电荷量。", with_images=True)
    await _confirm(client, q1["session_id"], "CONCEPT_BOTTLENECK")
    await _await_prewarm()

    # Q2：NAME_B 近义命中复用 NAME_A 主 ID（aliases 扩充）；PAIR_A 精确同名复用
    install_diagnosis(_module_payload(MODULE_EM, L2_EM, [NAME_B, PAIR_B, PAIR_A]))
    q2 = await _ingest(client, "电磁感应双杆模型题 2：求电荷量大小与位移。")
    node_a = _node_id(NAME_A)
    aliases_row = _one("SELECT aliases_json FROM knowledge_nodes WHERE node_id = ?", (node_a,))
    # 复用主 ID：l3_operator 不变，新名称并入 aliases（主名称不重复入 aliases）
    assert json.loads(aliases_row["aliases_json"]) == [NAME_B]
    reused = _one(
        "SELECT reused_existing_node FROM question_steps WHERE question_uid = ?"
        " AND step_index = 1",
        (q2["question_uid"],),
    )
    assert reused["reused_existing_node"] == 1
    assert _count("knowledge_nodes") == 4  # 复用不新建：3 + PAIR_B

    # Q3~Q5：各 3 个全新 L3 → 累计 13 个节点（未达阈值 15）
    bulk_names = [
        ["安培力冲量与动量", "单杆感应电动势", "双杆电流分配"],
        ["磁场能量与做功", "单杆终端速度", "双杆电荷量守恒"],
        ["单杆动量定理", "磁通变化率", "位移与电荷量关系"],
    ]
    for i, names in enumerate(bulk_names, start=3):
        install_diagnosis(_module_payload(MODULE_EM, L2_EM, names))
        await _ingest(client, f"电磁感应双杆模型题 {i}：综合推导。")
    assert _count("knowledge_nodes") == 13
    assert _count("compaction_audit_ledger") == 0

    # 跨考点组合错题标红边：P4-F 起录入即自动建边（ingest_service 事务内调
    # record_co_error_edges），无需再手动调用（手动重复调用会重复递增计数）。
    node_pair_a = _node_id(PAIR_A)
    node_pair_b = _node_id(PAIR_B)
    # 钉住创建时序（同秒 CURRENT_TIMESTAMP 会以随机 uuid 决序）：PAIR_A 为不可变老主节点
    _execute(
        "UPDATE knowledge_nodes SET created_at = '2026-01-01 00:00:01' WHERE node_id = ?",
        (node_pair_a,),
    )
    _execute(
        "UPDATE knowledge_nodes SET created_at = '2026-01-01 00:00:02' WHERE node_id = ?",
        (node_pair_b,),
    )
    nodes_before = {row["node_id"]: row for row in _snapshot_rows("knowledge_nodes")}
    edges_before = _snapshot_rows("concept_co_errors")
    # 标红边期望值推导（录入即自动建边；每题取全部步骤的不同 resolve 节点两两建边，
    # 重合边累加）：Q1{A,PAIR_A,C1} 三边；Q2{A,PAIR_B,PAIR_A} 三对中 (A,PAIR_A) 与 Q1
    # 重合累加、另两对新增 → +2；Q3/Q4/Q5 各 3 个全新节点 → 各 +3（互不重合）。
    # 合计 3 + 2 + 3 + 3 + 3 = 14 边。
    assert len(edges_before) == 14

    # Q6：第 15 个新节点触发同域自动 Compaction（PAIR_B -> PAIR_A，裁判仅确认此对）
    install_diagnosis(
        _module_payload(MODULE_EM, L2_EM, ["双杆能量守恒", "单杆临界条件", "等效电阻化简"])
    )
    q6 = await _ingest(client, "电磁感应双杆模型题 6：能量与临界。")
    assert q6["question_uid"]
    batches = _query("SELECT * FROM compaction_audit_ledger")
    assert len(batches) == 1
    batch_id = str(batches[0]["batch_id"])
    assert batches[0]["from_node_id"] == node_pair_b
    assert batches[0]["to_node_id"] == node_pair_a
    assert batches[0]["l1_module"] == MODULE_EM and batches[0]["l2_entity"] == L2_EM

    # 合并后看板：PAIR_B 被 resolve 出活跃树；标红边去自环 + 归并累加
    graph = await _get_graph(client)
    em = _module_ledger(graph, MODULE_EM)
    em_nodes = {n["node_id"]: n for g in em["l2_groups"] for n in g["nodes"]}
    assert node_pair_b not in em_nodes
    assert em_nodes[node_pair_a]["redirected_node_ids"] == [node_pair_b]
    assert em["metrics"]["active_nodes"] == 15  # 16 节点 - 1 重定向
    for edge in graph["red_edges"]:
        assert edge["node_u_id"] != edge["node_v_id"]  # 无自环
        assert node_pair_b not in (edge["node_u_id"], edge["node_v_id"])
    merged_edge = next(
        e for e in graph["red_edges"]
        if {e["node_u_id"], e["node_v_id"]} == {node_a, node_pair_a}
    )
    # merged_edge 计数推导（自动建边下不变）：(A,PAIR_A) 原 2（Q1+Q2 各贡献 1）+
    # 归并 (A,PAIR_B) 的 1（Q2）= 3；自环 (PAIR_B,PAIR_A) 丢弃。Q3~Q6 各题节点均不
    # 触及 A/PAIR_A/PAIR_B，故自动建边不改变此计数。
    assert merged_edge["active_error_count"] == 3
    batch_view = graph["compaction_batches"][0]
    assert batch_view["batch_id"] == batch_id
    assert batch_view["is_reverted"] is False
    assert batch_view["merges"][0]["ai_judge_evidence"] == "确认近义合并"

    # Q7/Q8：第二 L1 模块（函数与导数）独立生长，跨模块绝不合并
    install_diagnosis(
        _module_payload(MODULE_FN, L2_FN, ["导数符号判单调性", "极值与最值求解", "切线方程构造"])
    )
    await _ingest(client, "函数与导数题 1：单调性与极值。")
    install_diagnosis(
        _module_payload(
            MODULE_FN, L2_FN, ["零点存在性判定", "导数符号判单调性", "不等式恒成立转化"]
        )
    )
    q8 = await _ingest(client, "函数与导数题 2：零点与恒成立。")
    assert _one(
        "SELECT reused_existing_node FROM question_steps WHERE question_uid = ?"
        " AND step_index = 2",
        (q8["question_uid"],),
    )["reused_existing_node"] == 1

    # 双指标：fixture 对齐均为 stalled=2 → 仅挂载前缀步（step1）的节点保持 🟢MASTERED
    # （设计 §3.4 悲观半格：只聚合 is_blocked_step=1 的挂载）；电磁学 5/15 绿，函数 1/5 绿
    graph = await _get_graph(client)
    em = _module_ledger(graph, MODULE_EM)
    fn = _module_ledger(graph, MODULE_FN)
    assert em["metrics"]["mastery_rate"] == pytest.approx(100.0 * 5 / 15)
    em_nodes = {n["node_id"]: n for g in em["l2_groups"] for n in g["nodes"]}
    assert em_nodes[node_a]["state"] == "MASTERED"  # 仅前缀步挂载 → 绿
    assert em_nodes[node_pair_a]["state"] == "ACTIVE"  # 卡壳步挂载 → 红
    assert 0.0 < em["metrics"]["convergence"] <= 100.0  # w=6>=5 且存在复用
    assert fn["metrics"] == {"mastery_rate": 20.0, "convergence": 0.0, "active_nodes": 5}
    assert _query(
        "SELECT * FROM compaction_audit_ledger WHERE l1_module = ?", (MODULE_FN,)
    ) == []  # 跨 L1 作用域隔离

    # 一键回滚：节点/标红边逐字段恢复至合并前快照；Elo 账本零变化（正交解耦）
    ledger_before = await _get_ledger(client)
    assert _module_ledger(ledger_before, MODULE_EM)["events"]  # 保底事件非空
    # 回滚前全表快照（Q1-Q8 均已录入，含 compaction 合并态 + Q6-Q8 正交新建边）。
    # 注：compaction 在 Q6 第 15 个新节点时自动触发，Q6/Q7/Q8 的边在其后才录入，
    # 故「compaction 前一刻」的全表快照无法在回滚前单一时刻取得（事后态含 Q6-Q8 边）。
    edges_pre_revert = _snapshot_rows("concept_co_errors")
    resp = await client.post(f"/api/v1/knowledge-graph/compaction/{batch_id}/revert")
    assert resp.status_code == 200
    assert resp.json() == {"batch_id": batch_id, "reverted_merges": 1}

    nodes_after = {row["node_id"]: row for row in _snapshot_rows("knowledge_nodes")}
    for nid, row in nodes_before.items():
        assert nodes_after[nid] == row  # 逐字段：aliases/redirect/created_at 全恢复
    # 标红边精确恢复（P4-F 方案 A + 正交边不变，全表逐字段覆盖）：
    # compaction 快照/回滚只触及 PAIR_A/PAIR_B 的边；Q6-Q8 正交边不触及 PAIR，回滚不动。
    edges_after = _snapshot_rows("concept_co_errors")
    # (1) 全部 14 条基线边（edges_before）逐字段原样恢复，含触及 PAIR 的 4 条：
    #     (A,PAIR_A)=2 / (A,PAIR_B)=1 / (PAIR_B,PAIR_A)=1 / (PAIR_A,C1)=1。
    after_by_key = {(e["node_u_id"], e["node_v_id"]): e for e in edges_after}
    for e in edges_before:
        assert after_by_key[(e["node_u_id"], e["node_v_id"])] == e
    # (2) 正交边（不触及 PAIR_A/PAIR_B）回滚前后分文不动：10 条非 PAIR 基线边
    #     + Q6/Q7/Q8 各 3 条新建边（共 19 条）逐字段相等。
    pair_ids = {node_pair_a, node_pair_b}
    non_pair_before = [
        e for e in edges_pre_revert
        if e["node_u_id"] not in pair_ids and e["node_v_id"] not in pair_ids
    ]
    non_pair_after = [
        e for e in edges_after
        if e["node_u_id"] not in pair_ids and e["node_v_id"] not in pair_ids
    ]
    assert non_pair_after == non_pair_before
    # (3) 完整性：回滚后全表恰为 14 基线（精确恢复）+ 9 正交新增（Q6-Q8），无多余/缺失。
    #     9 = Q6{3 新节点}+Q7{3}+Q8{3 不同节点}各 C(3,2)=3 边（互不重合、不触及 PAIR）。
    assert len(edges_after) == len(edges_before) + 9
    assert await _get_ledger(client) == ledger_before  # Elo 完全不动

    graph = await _get_graph(client)
    em = _module_ledger(graph, MODULE_EM)
    em_nodes = {n["node_id"]: n for g in em["l2_groups"] for n in g["nodes"]}
    assert node_pair_b in em_nodes
    assert em_nodes[node_pair_b]["redirected_node_ids"] == []
    assert em_nodes[node_pair_a]["redirected_node_ids"] == []
    assert em["metrics"]["active_nodes"] == 16
    batch_view = graph["compaction_batches"][0]
    assert batch_view["is_reverted"] is True
    assert batch_view["merges"][0]["is_reverted"] is True
    assert batch_view["merges"][0]["reverted_at"] is not None
    # 重复回滚 → 409；未知批次 → 404
    resp = await client.post(f"/api/v1/knowledge-graph/compaction/{batch_id}/revert")
    assert resp.status_code == 409
    resp = await client.post("/api/v1/knowledge-graph/compaction/no-such-batch/revert")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 幂等与重入库：零新行 / 改题干保进度 / 新草稿新会话 / Elo 幂等键
# ---------------------------------------------------------------------------


async def test_reimport_same_content_zero_new_rows(client: AsyncClient) -> None:
    """同内容重导入：同 uid 同会话，全表零新行，CAS 去重。"""
    first = await _ingest(client, with_images=True)
    tables = (
        "questions",
        "question_steps",
        "diagnostic_sessions",
        "knowledge_nodes",
        "elo_mutation_ledger",
    )
    counts = {t: _count(t) for t in tables}
    second = await _ingest(client, with_images=True)
    assert second["question_uid"] == first["question_uid"]
    assert second["session_id"] == first["session_id"]
    assert second["is_reimport"] is True
    for table in tables:
        assert _count(table) == counts[table]
    assert len(list(settings.CAS_ASSETS_DIR.rglob("*.webp"))) == 2  # 内容寻址去重


async def test_reimport_stem_edit_keeps_progress_new_draft_new_session(
    client: AsyncClient,
) -> None:
    """改题干（frontmatter 携 uid）保全部进度；补充新草稿 → 新建会话（旧会话保留）。"""
    first = await _ingest(client, with_images=True)
    uid = first["question_uid"]
    await _confirm(client, first["session_id"], "CONCEPT_BOTTLENECK")
    await _await_prewarm()
    ledger_before = await _get_ledger(client)

    # 仅修正 OCR 错别字（语义未变）：stem 更新，进度/账本/会话全部保留
    edited = f"---\nquestion_uid: {uid}\n---\n\n{STEM_MD.replace('拱形桥', '拱形桥面')}"
    second = await _ingest(client, edited)
    assert second["question_uid"] == uid
    assert second["is_reimport"] is True
    assert second["session_id"] == first["session_id"]
    question = _one("SELECT * FROM questions WHERE question_uid = ?", (uid,))
    assert "拱形桥面" in question["stem_md"]
    assert question["mastery_state"] == "ACTIVE"
    session = _one(
        "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (first["session_id"],)
    )
    assert session["micro_stage"] == "STAGE_1_BLIND_PROBE"
    assert session["user_confirmed_gate"] == 1
    assert await _get_ledger(client) == ledger_before
    assert _count("diagnostic_sessions") == 1

    # 补充新草稿截图 → 新建会话（重新对齐），旧会话与题目进度保留
    third = await _ingest(client, edited, with_images=True, draft_color=(0, 255, 0))
    assert third["question_uid"] == uid
    assert third["is_reimport"] is True
    assert third["session_id"] != first["session_id"]
    assert _one(
        "SELECT COUNT(*) AS n FROM questions WHERE origin_type = 'USER_CANONICAL'"
    )["n"] == 1
    assert _count("diagnostic_sessions") == 2
    new_session = _one(
        "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (third["session_id"],)
    )
    assert new_session["micro_stage"] == "STAGE_0_CONFIRM_GATE"
    old_session = _one(
        "SELECT * FROM diagnostic_sessions WHERE session_id = ?", (first["session_id"],)
    )
    assert old_session["micro_stage"] == "STAGE_1_BLIND_PROBE"


async def test_elo_idempotency_keys_no_double_credit(client: AsyncClient) -> None:
    """Elo 事件重复提交不重复加分：跨端点幂等键（uid:session:trigger）重放 applied=False。"""
    card = await _concept_chain(client)
    uid = card["question_uid"]
    sid = card["session_id"]
    resp = await client.post(
        "/api/v1/drills/daily",
        json={"question_uid": uid, "final_answer": STAGE2_ANSWER, "active_date": _drill_day(4)},
    )
    assert resp.status_code == 200 and resp.json()["mastery_state"] == "MASTERED"
    ledger_before = await _get_ledger(client)
    event_count = _count("elo_mutation_ledger")
    assert event_count == 3  # 保底 + 覆盖分 + 盲测兑现

    # 同幂等键服务层重放（模拟跨端点重复触发）：一律 applied=False，零新行零变化
    with get_db_connection() as conn:
        floor = elo_engine.apply_draft_subgoal_floor(conn, uid, sid, FLOOR_SUB_ELO)
        cover = elo_engine.freeze_decrutch_escrow(conn, uid, sid)
        realize = elo_engine.realize_or_forfeit_blind_test(conn, uid, sid, passed=True)
    for result in (floor, cover, realize):
        assert result.applied is False
        assert result.event_id is not None  # 回读原事件快照
    assert _count("elo_mutation_ledger") == event_count
    assert await _get_ledger(client) == ledger_before

    # 重入库已掌握进度题目：不触发任何新结算
    reimport = await _ingest(client, with_images=True)
    assert reimport["is_reimport"] is True
    assert _count("elo_mutation_ledger") == event_count
    assert await _get_ledger(client) == ledger_before
    # 知识网服务层与账本正交：resolve 不产生 Elo 副作用
    with get_db_connection() as conn:
        resolver = knowledge_graph.CanonicalNodeResolver(conn)
        node = _one(
            "SELECT knowledge_node_id FROM question_steps WHERE question_uid = ? LIMIT 1",
            (uid,),
        )
        assert resolver.resolve(str(node["knowledge_node_id"])) == str(node["knowledge_node_id"])
    assert await _get_ledger(client) == ledger_before