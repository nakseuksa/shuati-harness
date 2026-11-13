"""FastAPI 应用入口与全量 REST 路由（设计 Target File #13 / §4 核心内部 API 契约）。

职责边界：路由保持轻薄，业务逻辑一律委托已验收的服务层——
ingest_service / scaffold_engine / blind_drill_scheduler / knowledge_graph /
elo_engine / cas_store / schema。本文件仅负责：

- HTTP 协议适配（multipart 落 tmp、请求模型校验、DTO 序列化）；
- 统一异常映射：ValueError→400、LookupError/FileNotFoundError→404、
  LLMGatewayError→502（含重试提示），错误体统一 ``{"detail": ...}``；
- ``active_date`` 注入贯穿 drills 端点（E2E 时间旅行依赖）；
- lifespan 启动 ``init_db()``；CAS 资产 ``/assets/{hash}`` 静态图像服务；
- ``./frontend/`` 静态前端挂载（目录缺失/为空时应用仍可启动，P4-B 并行容错）。

同源本地单机（NG2）：不加 CORS、不加鉴权。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import PlainTextResponse, Response

from backend.config import settings
from backend.db.schema import get_db_connection, init_db
from backend.observability import log_event, request_id_var, setup_logging
from backend.services import elo_engine, knowledge_graph
from backend.services.blind_drill_scheduler import (
    BlindDrillItemDTO,
    BlindDrillOutcomeDTO,
    evaluate_blind_drill_submission,
    get_daily_blind_drill_queue,
)
from backend.services.cas_store import resolve_cas_uri
from backend.services.ingest_service import GateCardDTO, InitialState, ingest_question
from backend.services.knowledge_graph import CanonicalNodeResolver, GraphMetricsDTO
from backend.services.llm_gateway import LLMGatewayError, aclose_shared_clients
from backend.services.scaffold_engine import (
    SessionStepResponse,
    confirm_diagnostic_gate,
    get_session_view,
    prewarm_session_questions,
    retry_prewarm,
    submit_stage_answer,
)

__all__ = ["app", "create_app"]

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 错误类别枚举（与 diagnostic_sessions.error_category CHECK 一致）；
# Pydantic Literal 校验使非法类别在路由层即 422（契约要求）。
ErrorCategory = Literal["ALREADY_MASTERED", "TRAP_OR_CALC_ERROR", "CONCEPT_BOTTLENECK"]


# ---------------------------------------------------------------------------
# 请求模型（响应一律复用服务层 DTO / 下方组装视图模型）
# ---------------------------------------------------------------------------


class ConfirmGateRequest(BaseModel):
    """步骤对齐微确认门禁入参。"""

    stalled_step_index: int
    error_category: ErrorCategory


class StepSubmitRequest(BaseModel):
    """微观阶段作答入参（结构化自评可空）。"""

    user_answer: str
    self_assessment: str | None = None


class BlindDrillSubmitRequest(BaseModel):
    """每日盲测提交入参；active_date 缺省 today（时间旅行注入点）。"""

    question_uid: str
    final_answer: str
    intermediate_sub_answer: str | None = None
    active_date: date | None = None


# ---------------------------------------------------------------------------
# 知识网看板组装视图（GET /api/v1/knowledge-graph）
# ---------------------------------------------------------------------------


class KnowledgeNodeView(BaseModel):
    """resolve 后的活跃 L3 主节点（含被合并重定向的子节点清单）。"""

    node_id: str
    l3_operator: str
    aliases: list[str]
    state: str  # ACTIVE / PENDING_RETEST / MASTERED（悲观半格派生值）
    redirected_node_ids: list[str]


class L2GroupView(BaseModel):
    l2_entity: str
    nodes: list[KnowledgeNodeView]


class L1ModuleView(BaseModel):
    l1_module: str
    l2_groups: list[L2GroupView]
    metrics: GraphMetricsDTO


class RedEdgeView(BaseModel):
    """跨知识点组合错题标红边（端点为 resolve 后活跃主节点）。"""

    node_u_id: str
    node_v_id: str
    l1_module_u: str
    l1_module_v: str
    active_error_count: int
    total_historical_count: int
    source_question_uids: list[str]
    last_error_at: str


class CompactionMergeView(BaseModel):
    merge_id: int
    from_node_id: str
    to_node_id: str
    ai_judge_evidence: str
    is_reverted: bool
    created_at: str
    reverted_at: str | None


class CompactionBatchView(BaseModel):
    batch_id: str
    l1_module: str
    l2_entity: str
    is_reverted: bool  # 批次内全部合并均已回滚
    created_at: str
    merges: list[CompactionMergeView]


class KnowledgeGraphResponse(BaseModel):
    modules: list[L1ModuleView]
    red_edges: list[RedEdgeView]
    compaction_batches: list[CompactionBatchView]


class RevertCompactionResponse(BaseModel):
    batch_id: str
    reverted_merges: int


# ---------------------------------------------------------------------------
# 双轨 Elo 账本看板视图（GET /api/v1/elo/ledger）
# ---------------------------------------------------------------------------


class EloEventView(BaseModel):
    """不可变账本事件流水（含自然语言算分解释条）。"""

    event_id: int
    question_uid: str
    mutation_trigger: str
    eval_difficulty_elo: int
    watermark_gamma: float
    coverage_elo_before: float
    coverage_elo_after: float
    combat_elo_before: float
    combat_elo_after: float
    explanation_text: str
    created_at: str


class EloCurvePoint(BaseModel):
    """历史水位曲线点：事件结算后的双轨分。"""

    event_id: int
    coverage_elo: float
    combat_elo: float


class EscrowPendingView(BaseModel):
    """待兑现托管池明细（FROZEN_PENDING_BLIND）。"""

    question_uid: str
    initial_combat_elo_snapshot: float
    floor_elo_applied: float
    escrow_combat_delta: float
    decay_factor: float
    updated_at: str


class ModuleLedgerView(BaseModel):
    l1_module: str
    current: elo_engine.DualEloState
    events: list[EloEventView]
    curve: list[EloCurvePoint]
    pending_escrows: list[EscrowPendingView]


class EloLedgerResponse(BaseModel):
    modules: list[ModuleLedgerView]


# ---------------------------------------------------------------------------
# 统一异常映射（错误体一律 {"detail": ...}）
# ---------------------------------------------------------------------------


def _value_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _lookup_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


def _file_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


def _gateway_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"detail": f"模型网关调用失败：{exc}（请稍后重试）"},
    )


def _unhandled_error_handler(request: Request, exc: Exception) -> Response:
    """未处理异常：保持默认 500 纯文本响应体/状态码不变，仅附加 X-Request-ID。

    500 响应由 ServerErrorMiddleware 在请求中间件外层生成，contextvar 已经复位，
    故 rid 经 request.state（scope 共享）传递；业务异常传播行为不变。
    """
    rid = getattr(request.state, "request_id", "")
    headers = {"X-Request-ID": rid} if rid else {}
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)


# ---------------------------------------------------------------------------
# 轻薄工具：会话存在性预检 / multipart 落盘
# ---------------------------------------------------------------------------


def _require_session(session_id: str) -> None:
    """未知 session → 404（服务层对缺失会话抛 ValueError，HTTP 语义在路由层前置）。"""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM diagnostic_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")


async def _save_uploads(uploads: list[UploadFile] | None, tmp_dir: Path, prefix: str) -> list[Path]:
    """上传文件落 tmp 目录（序号命名 + 大小上限检查，杜绝路径穿越与内存 OOM），返回落盘路径列表。"""
    limit = settings.MAX_UPLOAD_IMAGE_BYTES
    paths: list[Path] = []
    for idx, upload in enumerate(uploads or []):
        suffix = Path(upload.filename or "").suffix
        dest = tmp_dir / f"{prefix}_{idx}{suffix}"
        total = 0
        with dest.open("wb") as fh:
            while True:
                chunk = await upload.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"上传图片体积超过上限 ({limit} bytes)")
                fh.write(chunk)
        paths.append(dest)
    return paths


# ---------------------------------------------------------------------------
# 知识网 / Elo 账本只读组装（业务计算全部委托服务层）
# ---------------------------------------------------------------------------


def _build_graph_response(conn: sqlite3.Connection) -> KnowledgeGraphResponse:
    resolver = CanonicalNodeResolver(conn)
    node_rows = conn.execute(
        "SELECT * FROM knowledge_nodes ORDER BY l1_module, l2_entity, node_id"
    ).fetchall()
    active_rows = [row for row in node_rows if row["redirect_to_id"] is None]

    redirected_children: dict[str, list[str]] = {}
    for row in node_rows:
        if row["redirect_to_id"] is not None:
            root = resolver.resolve(str(row["node_id"]))
            redirected_children.setdefault(root, []).append(str(row["node_id"]))

    l1_modules = {str(row["l1_module"]) for row in node_rows}
    l1_modules.update(
        str(row[0]) for row in conn.execute("SELECT DISTINCT primary_l1_module FROM questions")
    )
    node_states = knowledge_graph.all_node_states(conn)
    modules: list[L1ModuleView] = []
    for l1 in sorted(l1_modules):
        l2_groups: list[L2GroupView] = []
        l2_names = sorted({str(row["l2_entity"]) for row in active_rows if row["l1_module"] == l1})
        for l2 in l2_names:
            nodes = [
                KnowledgeNodeView(
                    node_id=str(row["node_id"]),
                    l3_operator=str(row["l3_operator"]),
                    aliases=json.loads(row["aliases_json"]),
                    state=node_states.get(str(row["node_id"]), "MASTERED"),
                    redirected_node_ids=sorted(redirected_children.get(str(row["node_id"]), [])),
                )
                for row in active_rows
                if row["l1_module"] == l1 and row["l2_entity"] == l2
            ]
            l2_groups.append(L2GroupView(l2_entity=l2, nodes=nodes))
        modules.append(
            L1ModuleView(
                l1_module=l1,
                l2_groups=l2_groups,
                metrics=knowledge_graph.compute_graph_metrics(conn, l1),
            )
        )

    red_edges = [
        RedEdgeView(
            node_u_id=resolver.resolve(str(row["node_u_id"])),
            node_v_id=resolver.resolve(str(row["node_v_id"])),
            l1_module_u=str(row["l1_module_u"]),
            l1_module_v=str(row["l1_module_v"]),
            active_error_count=int(row["active_error_count"]),
            total_historical_count=int(row["total_historical_count"]),
            source_question_uids=json.loads(row["source_question_uids_json"]),
            last_error_at=str(row["last_error_at"]),
        )
        for row in conn.execute("SELECT * FROM concept_co_errors ORDER BY node_u_id, node_v_id")
    ]

    ledger_rows = conn.execute(
        "SELECT * FROM compaction_audit_ledger ORDER BY batch_id, merge_id"
    ).fetchall()
    by_batch: dict[str, list[sqlite3.Row]] = {}
    for row in ledger_rows:
        by_batch.setdefault(str(row["batch_id"]), []).append(row)
    batches = [
        CompactionBatchView(
            batch_id=batch_id,
            l1_module=str(rows[0]["l1_module"]),
            l2_entity=str(rows[0]["l2_entity"]),
            is_reverted=all(int(row["is_reverted"]) == 1 for row in rows),
            created_at=str(rows[0]["created_at"]),
            merges=[
                CompactionMergeView(
                    merge_id=int(row["merge_id"]),
                    from_node_id=str(row["from_node_id"]),
                    to_node_id=str(row["to_node_id"]),
                    ai_judge_evidence=str(row["ai_judge_evidence"]),
                    is_reverted=bool(int(row["is_reverted"])),
                    created_at=str(row["created_at"]),
                    reverted_at=(
                        str(row["reverted_at"]) if row["reverted_at"] is not None else None
                    ),
                )
                for row in rows
            ],
        )
        for batch_id, rows in by_batch.items()
    ]
    return KnowledgeGraphResponse(modules=modules, red_edges=red_edges, compaction_batches=batches)


def _build_elo_ledger_response(
    conn: sqlite3.Connection, l1_module: str | None
) -> EloLedgerResponse:
    l1_modules = {
        str(row[0]) for row in conn.execute("SELECT DISTINCT l1_module FROM elo_mutation_ledger")
    }
    l1_modules.update(
        str(row[0]) for row in conn.execute("SELECT DISTINCT l1_module FROM question_escrow_pool")
    )
    l1_modules.update(
        str(row[0]) for row in conn.execute("SELECT DISTINCT primary_l1_module FROM questions")
    )
    if l1_module is not None:
        l1_modules &= {l1_module}

    modules: list[ModuleLedgerView] = []
    for module in sorted(l1_modules):
        event_rows = conn.execute(
            "SELECT * FROM elo_mutation_ledger WHERE l1_module = ? ORDER BY event_id",
            (module,),
        ).fetchall()
        events = [
            EloEventView(
                event_id=int(row["event_id"]),
                question_uid=str(row["question_uid"]),
                mutation_trigger=str(row["mutation_trigger"]),
                eval_difficulty_elo=int(row["eval_difficulty_elo"]),
                watermark_gamma=float(row["watermark_gamma"]),
                coverage_elo_before=float(row["coverage_elo_before"]),
                coverage_elo_after=float(row["coverage_elo_after"]),
                combat_elo_before=float(row["combat_elo_before"]),
                combat_elo_after=float(row["combat_elo_after"]),
                explanation_text=str(row["explanation_text"]),
                created_at=str(row["created_at"]),
            )
            for row in event_rows
        ]
        escrow_rows = conn.execute(
            "SELECT * FROM question_escrow_pool WHERE l1_module = ?"
            " AND escrow_status = 'FROZEN_PENDING_BLIND' ORDER BY question_uid",
            (module,),
        ).fetchall()
        modules.append(
            ModuleLedgerView(
                l1_module=module,
                current=elo_engine.get_module_elo(conn, module),
                events=events,
                curve=[
                    EloCurvePoint(
                        event_id=event.event_id,
                        coverage_elo=event.coverage_elo_after,
                        combat_elo=event.combat_elo_after,
                    )
                    for event in events
                ],
                pending_escrows=[
                    EscrowPendingView(
                        question_uid=str(row["question_uid"]),
                        initial_combat_elo_snapshot=float(row["initial_combat_elo_snapshot"]),
                        floor_elo_applied=float(row["floor_elo_applied"]),
                        escrow_combat_delta=float(row["escrow_combat_delta"]),
                        decay_factor=float(row["decay_factor"]),
                        updated_at=str(row["updated_at"]),
                    )
                    for row in escrow_rows
                ],
            )
        )
    return EloLedgerResponse(modules=modules)


# ---------------------------------------------------------------------------
# 请求日志中间件（P5 观测层：api.request 事件 + request_id 贯穿 + X-Request-ID）
# ---------------------------------------------------------------------------


class _RequestLogMiddleware(BaseHTTPMiddleware):
    """每请求生成 uuid4 写入 contextvar，响应头回传 X-Request-ID。

    只记录 method/path/status/duration_ms（绝不记录请求体/Authorization 头）；
    路由层已处理的异常（400/404/502）以正常响应返回，未处理异常按 500 留痕后重抛。
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = uuid.uuid4().hex
        token = request_id_var.set(rid)
        request.state.request_id = rid  # 供外层 500 处理器回传 X-Request-ID
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            fields: dict[str, Any] = {
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": duration_ms,
            }
            path_params = request.scope.get("path_params") or {}
            if path_params.get("session_id"):
                fields["session_id"] = str(path_params["session_id"])
            if path_params.get("batch_id"):
                fields["batch_id"] = str(path_params["batch_id"])
            log_event(logger, "api.request", **fields)  # request_id 经 contextvar 自动附带
            request_id_var.reset(token)


# ---------------------------------------------------------------------------
# 应用工厂与路由注册
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()  # 幂等挂载行为日志（P5 观测层，先于业务初始化）
    init_db()  # 幂等建表（settings.SQLITE_PATH）
    try:
        yield
    finally:
        await aclose_shared_clients()


def create_app(frontend_dir: Path | None = None) -> FastAPI:
    """构建应用；frontend_dir 缺省取项目根 ./frontend（缺失时跳过挂载不崩溃）。"""
    app = FastAPI(title="数理自适应刷题 Harness", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(_RequestLogMiddleware)
    app.add_exception_handler(ValueError, _value_error_handler)
    app.add_exception_handler(LookupError, _lookup_error_handler)
    app.add_exception_handler(FileNotFoundError, _file_not_found_handler)
    app.add_exception_handler(LLMGatewayError, _gateway_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)

    # ---- CUJ 1：多模态录入 → 步骤对齐微确认卡 ----
    @app.post("/api/v1/questions/ingest", response_model=GateCardDTO)
    async def ingest(
        stem_md: Annotated[str, Form()],
        target_grade: Annotated[str, Form()],
        initial_state: Annotated[InitialState, Form()] = "错题",
        user_answer: Annotated[str, Form()] = "",
        mastered_claim: Annotated[bool, Form()] = False,
        stem_images: Annotated[list[UploadFile] | None, File()] = None,
        draft_images: Annotated[list[UploadFile] | None, File()] = None,
    ) -> GateCardDTO:
        with tempfile.TemporaryDirectory(prefix="shuati_ingest_") as tmp:
            tmp_dir = Path(tmp)
            stem_paths = await _save_uploads(stem_images, tmp_dir, "stem")
            draft_paths = await _save_uploads(draft_images, tmp_dir, "draft")
            return await ingest_question(
                stem_md,
                stem_paths,
                draft_paths,
                target_grade,
                initial_state=initial_state,
                base_dir=tmp_dir,
                user_answer=user_answer,
                mastered_claim=mastered_claim,
            )

    # ---- CUJ 1/2：门禁确认三分支 + 微观状态机作答 ----
    @app.post("/api/v1/sessions/{session_id}/confirm-gate", response_model=SessionStepResponse)
    async def confirm_gate(session_id: str, body: ConfirmGateRequest) -> SessionStepResponse:
        await asyncio.to_thread(_require_session, session_id)
        return await confirm_diagnostic_gate(
            session_id, body.stalled_step_index, body.error_category
        )

    @app.post("/api/v1/sessions/{session_id}/step-submit", response_model=SessionStepResponse)
    async def step_submit(session_id: str, body: StepSubmitRequest) -> SessionStepResponse:
        await asyncio.to_thread(_require_session, session_id)
        return await submit_stage_answer(session_id, body.user_answer, body.self_assessment)

    @app.get("/api/v1/sessions/{session_id}", response_model=SessionStepResponse)
    async def get_session(session_id: str) -> SessionStepResponse:
        """只读刷新会话视图：前端预热轮询与断点续传的服务端真相源。"""
        await asyncio.to_thread(_require_session, session_id)
        return await get_session_view(session_id)

    @app.post("/api/v1/sessions/{session_id}/retry-prewarm", response_model=SessionStepResponse)
    async def retry_session_prewarm(
        session_id: str,
        force: Annotated[bool, Query()] = False,
    ) -> SessionStepResponse:
        """预热 FAILED / 产物缺失时重试；READY 默认幂等，force=true 强制换新重生成。"""
        await asyncio.to_thread(_require_session, session_id)
        return await retry_prewarm(session_id, force=force)

    # ---- CUJ 3：每日盲测题单 / 销账（active_date 注入贯穿，支持时间旅行） ----
    @app.get("/api/v1/drills/daily", response_model=list[BlindDrillItemDTO])
    def daily_drills(
        active_date: Annotated[date | None, Query()] = None,
    ) -> list[BlindDrillItemDTO]:
        day = active_date if active_date is not None else date.today()
        with get_db_connection() as conn:
            return get_daily_blind_drill_queue(conn, day)

    @app.post("/api/v1/drills/daily", response_model=BlindDrillOutcomeDTO)
    async def submit_drill(body: BlindDrillSubmitRequest) -> BlindDrillOutcomeDTO:
        day = body.active_date if body.active_date is not None else date.today()

        def _run_submit() -> BlindDrillOutcomeDTO:
            with get_db_connection() as conn:
                return evaluate_blind_drill_submission(
                    conn,
                    body.question_uid,
                    body.final_answer,
                    intermediate_sub_answer=body.intermediate_sub_answer,
                    active_date=day,
                )

        outcome = await asyncio.to_thread(_run_submit)
        if outcome.retrain_session_id:
            await prewarm_session_questions(
                outcome.retrain_session_id,
                refresh_scaffold=outcome.retrain_stage != "STAGE_4_DECRUTCH_VARIANT",
            )
        return outcome

    # ---- CUJ 4：三层知识网看板 + 并查集批次一键回滚 ----
    @app.get("/api/v1/knowledge-graph", response_model=KnowledgeGraphResponse)
    def get_knowledge_graph() -> KnowledgeGraphResponse:
        with get_db_connection() as conn:
            return _build_graph_response(conn)

    @app.post(
        "/api/v1/knowledge-graph/compaction/{batch_id}/revert",
        response_model=RevertCompactionResponse,
    )
    def revert_compaction(batch_id: str) -> RevertCompactionResponse:
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT is_reverted FROM compaction_audit_ledger WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
            if not rows:
                raise HTTPException(status_code=404, detail=f"合并批次不存在: {batch_id}")
            pending = [row for row in rows if int(row["is_reverted"]) == 0]
            if not pending:
                raise HTTPException(status_code=409, detail=f"合并批次已回滚: {batch_id}")
            knowledge_graph.revert_compaction_batch(conn, batch_id)
        return RevertCompactionResponse(batch_id=batch_id, reverted_merges=len(pending))

    # ---- 双轨 Elo 账本看板 ----
    @app.get("/api/v1/elo/ledger", response_model=EloLedgerResponse)
    def get_elo_ledger(
        l1_module: Annotated[str | None, Query()] = None,
    ) -> EloLedgerResponse:
        with get_db_connection() as conn:
            return _build_elo_ledger_response(conn, l1_module)

    # ---- CAS 受管资产静态图像服务 ----
    @app.get("/assets/{cas_hash}")
    def get_asset(cas_hash: str) -> FileResponse:
        path = resolve_cas_uri(f"cas://{cas_hash}")  # 非法 hash→ValueError→400；缺失→404
        return FileResponse(path, media_type="image/webp")

    # ---- 前端静态挂载（必须最后注册，避免 "/" 遮蔽 API 路由；目录缺失容错） ----
    frontend = frontend_dir if frontend_dir is not None else _PROJECT_ROOT / "frontend"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

    return app


app = create_app()
