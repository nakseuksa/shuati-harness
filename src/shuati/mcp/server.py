"""零外部依赖的 JSON-RPC 2.0 换行分隔 stdio MCP 服务端。

物理隔离铁律：
- 模块顶层与 `role == "worker"` 路径严禁导入 `sqlite3`、`shuati.db` 或 `shuati.services.facade`；
- `worker` 角色仅暴露 `verify_sympy` 与 `validate_draft` 两个纯无状态工具；
- `compiler` 角色暴露编译器运维与接缝工具；
- `practice` 角色同构透传 M3 核心刷题工具（默认 `reveal="quiz"` 防剧透）；
- 全部日志仅写 `sys.stderr`，stdout 专用于 UTF-8 换行分隔 JSON-RPC 2.0 报文。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO

from shuati import __version__

McpRole = Literal["worker", "compiler", "practice", "all"]
_VALID_ROLES: frozenset[str] = frozenset({"worker", "compiler", "practice", "all"})
_DEFAULT_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class McpServerConfig:
    """MCP 服务端运行上下文路径配置。"""

    role: McpRole = "worker"
    db_path: Path = Path("data/shuati.db")
    config_dir: Path = Path("config")
    vectors_dir: Path = Path("data/vectors")
    assets_dir: Path = Path("data/assets/images")
    unified_dir: Path = Path("data/unified")
    human_dir: Path = Path("human")
    drafts_dir: Path = Path("data/drafts")


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], McpServerConfig], dict[str, Any]]


def _ensure_utf8_streams() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            try:
                if (stream.encoding or "").lower() != "utf-8":
                    stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def _log_stderr(msg: str) -> None:
    print(f"[shuati-mcp] {msg}", file=sys.stderr, flush=True)


def _run_coro_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _validate_reveal_mode(raw: Any) -> Literal["quiz", "hint", "full"]:
    val = str(raw or "quiz").strip().lower()
    if val not in ("quiz", "hint", "full"):
        raise ValueError(f"reveal 仅支持 quiz / hint / full，收到 {raw!r}")
    return val  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Worker 工具（严格零 DB / 零 sqlite3）
# ---------------------------------------------------------------------------


def _tool_verify_sympy(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.verify.draft_validator import verify_sympy_payload

    final_answers = args.get("final_answers")
    if not isinstance(final_answers, list):
        raise ValueError("final_answers 必须为数组")
    options = args.get("options")
    if options is not None and not isinstance(options, dict):
        raise ValueError("options 必须为对象或 null")
    return verify_sympy_payload(
        sympy_code=args.get("sympy_code"),
        final_answers=final_answers,
        grade_subject=str(args.get("grade_subject") or "小学奥数"),
        options={str(k): str(v) for k, v in options.items()} if options else None,
        config_dir=cfg.config_dir,
    )


def _tool_validate_draft(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.verify.draft_validator import validate_draft_payload

    mode = str(args.get("mode") or "UNIFIED_S1_S2").strip().upper()
    if mode not in ("UNIFIED_S1_S2", "STAGE2_ONLY", "DIAGNOSE"):
        raise ValueError(f"无效 mode: {mode!r}")
    candidate = args.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("candidate 必须为对象")
    question_text = str(args.get("question_text") or "")
    options = args.get("options")
    if options is not None and not isinstance(options, dict):
        raise ValueError("options 必须为对象或 null")
    verified_final_answers = args.get("verified_final_answers")
    if verified_final_answers is not None and not isinstance(verified_final_answers, list):
        raise ValueError("verified_final_answers 必须为数组或 null")

    return validate_draft_payload(
        mode=mode,  # type: ignore[arg-type]
        candidate=candidate,
        question_text=question_text,
        grade_subject=str(args.get("grade_subject") or "小学奥数"),
        options={str(k): str(v) for k, v in options.items()} if options else None,
        verified_final_answers=verified_final_answers,
        run_sympy=bool(args.get("run_sympy", True)),
        config_dir=cfg.config_dir,
    )


# ---------------------------------------------------------------------------
# Compiler 工具（仅 compiler / all 角色可见）
# ---------------------------------------------------------------------------


def _tool_prompt_list(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.llm.seam_loader import list_prompt_seams

    return list_prompt_seams(config_dir=cfg.config_dir)


def _tool_prompt_render(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.llm.prompt_builder import render_prompt_preview

    stage = str(args.get("stage") or "").strip()
    if not stage:
        raise ValueError("stage 不能为空")
    sample = args.get("sample_payload")
    if sample is not None and not isinstance(sample, dict):
        raise ValueError("sample_payload 必须为对象或 null")
    return render_prompt_preview(
        stage=stage,
        grade_subject=str(args.get("grade_subject") or "小学奥数"),
        config_dir=cfg.config_dir,
        prompt_profile=args.get("prompt_profile"),
        sample_payload=sample,
    )


def _tool_prompt_lint(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.llm.seam_loader import lint_prompt_seams

    return lint_prompt_seams(config_dir=cfg.config_dir)


def _tool_pipeline_status(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.services.compiler_tools import get_pipeline_status

    gs = args.get("grade_subject")
    return get_pipeline_status(
        db_path=cfg.db_path,
        config_dir=cfg.config_dir,
        vectors_dir=cfg.vectors_dir,
        unified_dir=cfg.unified_dir,
        grade_subject=str(gs) if gs is not None else None,
    )


def _tool_diagnose_preflight(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.services.diagnose_batch import preflight_question_list_markdown

    file_path = str(args.get("file_path") or "").strip()
    if not file_path:
        raise ValueError("file_path 不能为空")
    out_norm = args.get("out_normalized")
    return preflight_question_list_markdown(
        file_path,
        out_normalized=str(out_norm) if out_norm else None,
        drafts_dir=cfg.drafts_dir,
    )


def _tool_ingest_dry_run(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.pipeline.stage0_ingest import run_stage0_ingest

    root_dir = Path(str(args.get("root_dir") or "题库"))
    gs = args.get("grade_subject")
    report = run_stage0_ingest(
        db_path=cfg.db_path,
        root_dir=root_dir,
        config_dir=cfg.config_dir,
        assets_dir=cfg.assets_dir,
        grade_subject_override=str(gs) if gs is not None else None,
        dedup_near=bool(args.get("dedup_near", False)),
        apply_retire=bool(args.get("apply_retire", False)),
        dry_run=True,
    )
    return report.to_dict()


# ---------------------------------------------------------------------------
# Practice 工具（仅 practice / all 角色可见，同构透传 facade）
# ---------------------------------------------------------------------------


def _tool_learner_list(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.db.connection import get_connection
    from shuati.services import facade

    conn = get_connection(cfg.db_path, readonly=True)
    try:
        items = facade.list_learners(conn)
        return {"count": len(items), "learners": items}
    finally:
        conn.close()


def _tool_profile(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.config import load_settings
    from shuati.db.connection import get_connection
    from shuati.services import facade

    settings = load_settings(cfg.config_dir / "settings.yaml")
    conn = get_connection(cfg.db_path, readonly=True)
    try:
        lid = facade.resolve_learner_id(conn, args.get("learner"))
        return facade.compute_learner_profile(
            conn,
            learner_id=lid,
            grade_subject=str(args.get("grade_subject") or "小学奥数"),
            elo_cfg=settings.learner_elo,
        )
    finally:
        conn.close()


def _tool_taxonomy(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.config import load_settings
    from shuati.db.connection import get_connection
    from shuati.services import facade

    settings = load_settings(cfg.config_dir / "settings.yaml")
    conn = get_connection(cfg.db_path, readonly=True)
    try:
        return facade.browse_taxonomy(
            conn,
            grade_subject=str(args.get("grade_subject") or "小学奥数"),
            learner_id=args.get("learner"),
            l1=args.get("l1"),
            settings=settings,
        )
    finally:
        conn.close()


def _tool_search(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.config import load_settings
    from shuati.db.connection import get_connection
    from shuati.services import facade

    rev_mode = _validate_reveal_mode(args.get("reveal", "quiz"))
    settings = load_settings(cfg.config_dir / "settings.yaml")
    conn = get_connection(cfg.db_path, readonly=True)
    try:
        return facade.search_questions(
            conn,
            settings,
            grade_subject=args.get("grade_subject"),
            l1=args.get("l1"),
            l2=args.get("l2"),
            l3=args.get("l3"),
            motif=args.get("motif"),
            method=args.get("method"),
            trap=args.get("trap"),
            tier=args.get("tier"),
            elo_min=int(args["elo_min"]) if args.get("elo_min") is not None else None,
            elo_max=int(args["elo_max"]) if args.get("elo_max") is not None else None,
            query=args.get("query"),
            semantic=args.get("semantic"),
            exclude_solved=bool(args.get("exclude_solved", False)),
            learner_id=args.get("learner"),
            reveal=rev_mode,
            hint_level=int(args.get("hint_level", 1)),
            include_visual_facts=bool(args.get("include_visual_facts", False)),
            limit=int(args.get("limit", 20)),
            assets_dir=cfg.assets_dir,
            vectors_dir=cfg.vectors_dir,
            use_mock=args.get("mock"),
        )
    finally:
        conn.close()


def _tool_related(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.db.connection import get_connection
    from shuati.services import facade

    uid = str(args.get("uid") or "").strip()
    if not uid:
        raise ValueError("uid 不能为空")
    rev_mode = _validate_reveal_mode(args.get("reveal", "quiz"))
    raw_types = args.get("types")
    type_list: list[str] | None = None
    if isinstance(raw_types, list):
        type_list = [str(t).strip() for t in raw_types if str(t).strip()]
    elif isinstance(raw_types, str) and raw_types.strip():
        type_list = [t.strip() for t in raw_types.split(",") if t.strip()]

    conn = get_connection(cfg.db_path, readonly=True)
    try:
        return facade.get_related_questions(
            conn,
            uid,
            types=type_list,
            direction=str(args.get("direction") or "both"),
            exclude_solved=bool(args.get("exclude_solved", False)),
            learner_id=args.get("learner"),
            reveal=rev_mode,
            hint_level=int(args.get("hint_level", 1)),
            include_visual_facts=bool(args.get("include_visual_facts", False)),
            limit=int(args.get("limit", 20)),
            assets_dir=cfg.assets_dir,
        )
    finally:
        conn.close()


def _tool_detail(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.db.connection import get_connection
    from shuati.services import facade

    uid = str(args.get("uid") or "").strip()
    if not uid:
        raise ValueError("uid 不能为空")
    rev_mode = _validate_reveal_mode(args.get("reveal", "quiz"))
    conn = get_connection(cfg.db_path, readonly=True)
    try:
        return facade.get_question_detail(
            conn,
            uid,
            assets_dir=cfg.assets_dir,
            reveal=rev_mode,
            hint_level=int(args.get("hint_level", 1)),
            include_visual_facts=bool(args.get("include_visual_facts", False)),
        )
    finally:
        conn.close()


def _tool_check(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.config import load_settings
    from shuati.db.connection import get_connection
    from shuati.db.schema import init_db
    from shuati.services import facade

    uid = str(args.get("uid") or "").strip()
    if not uid:
        raise ValueError("uid 不能为空")
    if "answer" not in args or args.get("answer") is None:
        raise ValueError("answer 不能为空")
    answer_input = str(args["answer"])
    do_log = bool(args.get("log", False))
    settings = load_settings(cfg.config_dir / "settings.yaml")
    conn = get_connection(cfg.db_path, readonly=not do_log)
    try:
        if do_log:
            init_db(conn, config_dir=cfg.config_dir)
        return _run_coro_sync(
            facade.check_answer_async(
                conn,
                settings,
                uid,
                answer_input=answer_input,
                learner_id=args.get("learner"),
                hint_level=int(args.get("hint_level", 0)),
                log=do_log,
                request_id=args.get("request_id"),
                quiz_batch_id=args.get("quiz_batch_id"),
                judge_override_json=args.get("judge_verdict"),
                human_dir=cfg.human_dir,
                use_mock=args.get("mock"),
            )
        )
    finally:
        conn.close()


def _tool_compose_quiz(args: dict[str, Any], cfg: McpServerConfig) -> dict[str, Any]:
    from shuati.config import load_settings
    from shuati.db.connection import get_connection
    from shuati.db.schema import init_db
    from shuati.services import facade
    from shuati.services.reveal import apply_reveal

    rev_mode = _validate_reveal_mode(args.get("reveal", "quiz"))
    settings = load_settings(cfg.config_dir / "settings.yaml")
    conn = get_connection(cfg.db_path)
    try:
        init_db(conn, config_dir=cfg.config_dir)
        lid = facade.resolve_learner_id(conn, args.get("learner"))
        res = facade.compose_quiz(
            conn,
            learner_id=lid,
            grade_subject=str(args.get("grade_subject") or "小学奥数"),
            elo_cfg=settings.learner_elo,
            composer_cfg=settings.composer,
            l1=args.get("l1"),
            l2=args.get("l2"),
            l3=args.get("l3"),
            motif=args.get("motif"),
            method=args.get("method"),
            trap=args.get("trap"),
            count=int(args.get("count", 5)),
            mix=str(args.get("mix") or "2:2:1"),
            exclude_solved_days=(
                int(args["exclude_solved_days"]) if args.get("exclude_solved_days") is not None else None
            ),
            exclude_clone_days=(
                int(args["exclude_clone_days"]) if args.get("exclude_clone_days") is not None else None
            ),
            exclude_recent_quiz_batches=(
                int(args["exclude_recent_quiz_batches"])
                if args.get("exclude_recent_quiz_batches") is not None
                else None
            ),
            target_elo=int(args["target_elo"]) if args.get("target_elo") is not None else None,
        )
        include_vf = bool(args.get("include_visual_facts", False))
        hydrated_items: list[dict[str, Any]] = []
        for it in res["items"]:
            raw_d = facade.hydrate_question_dict(
                conn,
                it["question_uid"],
                assets_dir=cfg.assets_dir,
                reason_public=it["reason_public"],
                reason_full=it["reason_full"],
            )
            trimmed = apply_reveal(
                raw_d,
                reveal=rev_mode,
                include_visual_facts=include_vf,
            )
            trimmed["slot"] = it["slot"]
            hydrated_items.append(trimmed)
        res["items"] = hydrated_items
        return res
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool Registry (Role-isolated)
# ---------------------------------------------------------------------------

_WORKER_TOOLS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        name="verify_sympy",
        description="无状态执行 AnswerPart 归一、SymPy 沙盒验算与等价性核对（零 DB 访问）。",
        input_schema={
            "type": "object",
            "properties": {
                "sympy_code": {"type": ["string", "null"]},
                "final_answers": {"type": "array", "items": {"type": "object"}},
                "grade_subject": {"type": "string", "default": "小学奥数"},
                "options": {"type": ["object", "null"]},
            },
            "required": ["final_answers"],
        },
        handler=_tool_verify_sympy,
    ),
    _ToolSpec(
        name="validate_draft",
        description="无状态草稿全量自检核（Schema、AnswerPart、四维打标、防剧透/去数/黑名单与可选 SymPy）。",
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["UNIFIED_S1_S2", "STAGE2_ONLY", "DIAGNOSE"]},
                "candidate": {"type": "object"},
                "question_text": {"type": "string"},
                "grade_subject": {"type": "string", "default": "小学奥数"},
                "options": {"type": ["object", "null"]},
                "verified_final_answers": {"type": ["array", "null"], "items": {"type": "object"}},
                "run_sympy": {"type": "boolean", "default": True},
            },
            "required": ["mode", "candidate", "question_text"],
        },
        handler=_tool_validate_draft,
    ),
)

_COMPILER_EXTRA_TOOLS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        name="prompt_list",
        description="列出三层提示词接缝层（default / subjects / profiles / snippets）。",
        input_schema={"type": "object", "properties": {}},
        handler=_tool_prompt_list,
    ),
    _ToolSpec(
        name="prompt_render",
        description="按阶段渲染合并后的 Prompt 预览、摘要与扁平化 Schema。",
        input_schema={
            "type": "object",
            "properties": {
                "stage": {"type": "string"},
                "grade_subject": {"type": "string", "default": "小学奥数"},
                "prompt_profile": {"type": ["string", "null"]},
                "sample_payload": {"type": ["object", "null"]},
            },
            "required": ["stage"],
        },
        handler=_tool_prompt_render,
    ),
    _ToolSpec(
        name="prompt_lint",
        description="静态校验提示词接缝必填槽位与外链路径安全性。",
        input_schema={"type": "object", "properties": {}},
        handler=_tool_prompt_lint,
    ),
    _ToolSpec(
        name="pipeline_status",
        description="只读采集流水线全景态势（Stage 0~4 矩阵、挂起批、向量漂移与下一步建议）。",
        input_schema={
            "type": "object",
            "properties": {
                "grade_subject": {"type": ["string", "null"]},
            },
        },
        handler=_tool_pipeline_status,
    ),
    _ToolSpec(
        name="diagnose_preflight",
        description="只读规范化批量诊断 Markdown 题单并检查配图路径。",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "out_normalized": {"type": ["string", "null"]},
            },
            "required": ["file_path"],
        },
        handler=_tool_diagnose_preflight,
    ),
    _ToolSpec(
        name="ingest_dry_run",
        description="只读预览 Stage 0 入库体检统计（零写库、不锁库、不拷贝图片）。",
        input_schema={
            "type": "object",
            "properties": {
                "root_dir": {"type": "string", "default": "题库"},
                "grade_subject": {"type": ["string", "null"]},
                "dedup_near": {"type": "boolean", "default": False},
                "apply_retire": {"type": "boolean", "default": False},
            },
        },
        handler=_tool_ingest_dry_run,
    ),
)

_PRACTICE_TOOLS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        name="learner_list",
        description="列出全部学习者角色。",
        input_schema={"type": "object", "properties": {}},
        handler=_tool_learner_list,
    ),
    _ToolSpec(
        name="profile",
        description="查看学习者分模块 Elo、总评水位、8 档阶梯与薄弱雷达。",
        input_schema={
            "type": "object",
            "properties": {
                "learner": {"type": ["string", "null"]},
                "grade_subject": {"type": "string", "default": "小学奥数"},
            },
        },
        handler=_tool_profile,
    ),
    _ToolSpec(
        name="taxonomy",
        description="浏览学段知识树、先修 DAG 与学习者掌握热力。",
        input_schema={
            "type": "object",
            "properties": {
                "grade_subject": {"type": "string", "default": "小学奥数"},
                "learner": {"type": ["string", "null"]},
                "l1": {"type": ["string", "null"]},
            },
        },
        handler=_tool_taxonomy,
    ),
    _ToolSpec(
        name="search",
        description="多维条件 + FTS5 + 语义检索题目（默认 reveal='quiz' 防剧透）。",
        input_schema={
            "type": "object",
            "properties": {
                "grade_subject": {"type": ["string", "null"]},
                "l1": {"type": ["string", "null"]},
                "l2": {"type": ["string", "null"]},
                "l3": {"type": ["string", "null"]},
                "motif": {"type": ["string", "null"]},
                "method": {"type": ["string", "null"]},
                "trap": {"type": ["string", "null"]},
                "tier": {"type": ["string", "null"]},
                "elo_min": {"type": ["integer", "null"]},
                "elo_max": {"type": ["integer", "null"]},
                "query": {"type": ["string", "null"]},
                "semantic": {"type": ["string", "null"]},
                "exclude_solved": {"type": "boolean", "default": False},
                "learner": {"type": ["string", "null"]},
                "reveal": {"type": "string", "enum": ["quiz", "hint", "full"], "default": "quiz"},
                "hint_level": {"type": "integer", "default": 1},
                "include_visual_facts": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 20},
                "mock": {"type": ["boolean", "null"]},
            },
        },
        handler=_tool_search,
    ),
    _ToolSpec(
        name="related",
        description="沿五边拓扑查询关联题（默认 reveal='quiz' 防剧透）。",
        input_schema={
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "types": {"type": ["array", "string", "null"]},
                "direction": {"type": "string", "enum": ["pre", "post", "both"], "default": "both"},
                "exclude_solved": {"type": "boolean", "default": False},
                "learner": {"type": ["string", "null"]},
                "reveal": {"type": "string", "enum": ["quiz", "hint", "full"], "default": "quiz"},
                "hint_level": {"type": "integer", "default": 1},
                "include_visual_facts": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["uid"],
        },
        handler=_tool_related,
    ),
    _ToolSpec(
        name="detail",
        description="按防剧透档位查看单题详情（默认 reveal='quiz'）。",
        input_schema={
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "reveal": {"type": "string", "enum": ["quiz", "hint", "full"], "default": "quiz"},
                "hint_level": {"type": "integer", "default": 1},
                "include_visual_facts": {"type": "boolean", "default": False},
            },
            "required": ["uid"],
        },
        handler=_tool_detail,
    ),
    _ToolSpec(
        name="check",
        description="不泄露标准答案的逐小问判题（可选登记 practice_log）。",
        input_schema={
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "answer": {"type": "string"},
                "learner": {"type": ["string", "null"]},
                "hint_level": {"type": "integer", "default": 0},
                "log": {"type": "boolean", "default": False},
                "request_id": {"type": ["string", "null"]},
                "quiz_batch_id": {"type": ["string", "null"]},
                "judge_verdict": {"type": ["object", "null"]},
                "mock": {"type": ["boolean", "null"]},
            },
            "required": ["uid", "answer"],
        },
        handler=_tool_check,
    ),
    _ToolSpec(
        name="compose_quiz",
        description="按学习者分模块水位梯度组专题小卷（默认 reveal='quiz' 防剧透）。",
        input_schema={
            "type": "object",
            "properties": {
                "grade_subject": {"type": "string", "default": "小学奥数"},
                "learner": {"type": ["string", "null"]},
                "l1": {"type": ["string", "null"]},
                "l2": {"type": ["string", "null"]},
                "l3": {"type": ["string", "null"]},
                "motif": {"type": ["string", "null"]},
                "method": {"type": ["string", "null"]},
                "trap": {"type": ["string", "null"]},
                "count": {"type": "integer", "default": 5},
                "mix": {"type": "string", "default": "2:2:1"},
                "exclude_solved_days": {"type": ["integer", "null"]},
                "exclude_clone_days": {"type": ["integer", "null"]},
                "exclude_recent_quiz_batches": {"type": ["integer", "null"]},
                "target_elo": {"type": ["integer", "null"]},
                "reveal": {"type": "string", "enum": ["quiz", "hint", "full"], "default": "quiz"},
                "include_visual_facts": {"type": "boolean", "default": False},
            },
        },
        handler=_tool_compose_quiz,
    ),
)


def _registry_for_role(role: McpRole) -> dict[str, _ToolSpec]:
    if role not in _VALID_ROLES:
        raise ValueError(f"不支持的 MCP role: {role!r}（仅支持 worker/compiler/practice/all）")
    specs: list[_ToolSpec] = []
    if role == "worker":
        specs.extend(_WORKER_TOOLS)
    elif role == "compiler":
        specs.extend(_WORKER_TOOLS)
        specs.extend(_COMPILER_EXTRA_TOOLS)
    elif role == "practice":
        specs.extend(_PRACTICE_TOOLS)
    else:  # "all"
        specs.extend(_WORKER_TOOLS)
        specs.extend(_COMPILER_EXTRA_TOOLS)
        specs.extend(_PRACTICE_TOOLS)
    return {t.name: t for t in specs}


def get_tools_for_role(role: McpRole) -> list[dict[str, Any]]:
    """返回指定角色暴露的 MCP 工具声明列表。"""
    reg = _registry_for_role(role)
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.input_schema,
        }
        for spec in reg.values()
    ]


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def dispatch_jsonrpc_message(
    raw_message: str | dict[str, Any],
    *,
    role: McpRole = "worker",
    config: McpServerConfig | None = None,
    db_path: Path = Path("data/shuati.db"),
    config_dir: Path = Path("config"),
    vectors_dir: Path = Path("data/vectors"),
    assets_dir: Path = Path("data/assets/images"),
    unified_dir: Path = Path("data/unified"),
    human_dir: Path = Path("human"),
    drafts_dir: Path = Path("data/drafts"),
) -> dict[str, Any] | None:
    """分发单条 JSON-RPC 2.0 MCP 请求；通知（无 id）返回 None。"""
    cfg = config or McpServerConfig(
        role=role,
        db_path=Path(db_path),
        config_dir=Path(config_dir),
        vectors_dir=Path(vectors_dir),
        assets_dir=Path(assets_dir),
        unified_dir=Path(unified_dir),
        human_dir=Path(human_dir),
        drafts_dir=Path(drafts_dir),
    )

    if isinstance(raw_message, str):
        try:
            msg = json.loads(raw_message)
        except (ValueError, RecursionError) as exc:
            return _jsonrpc_error(None, -32700, f"Parse error: {exc}")
    else:
        msg = raw_message

    if not isinstance(msg, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request: message must be a JSON object")

    has_id = "id" in msg
    req_id = msg.get("id")
    method = msg.get("method")
    if not isinstance(method, str) or not method:
        return _jsonrpc_error(req_id, -32600, "Invalid Request: missing method") if has_id else None

    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _jsonrpc_error(req_id, -32602, "Invalid params: expected object") if has_id else None

    # Notifications (no id) or explicit notifications/*
    if method == "notifications/initialized" or (not has_id and method.startswith("notifications/")):
        return None
    if not has_id:
        return None

    if method == "initialize":
        proto = str(params.get("protocolVersion") or _DEFAULT_PROTOCOL_VERSION)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": f"shuati-mcp-{cfg.role}",
                    "version": __version__,
                },
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": get_tools_for_role(cfg.role)},
        }

    if method == "tools/call":
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            return _jsonrpc_error(req_id, -32602, "Invalid params: missing tool name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _jsonrpc_error(req_id, -32602, "Invalid params: arguments must be an object")

        reg = _registry_for_role(cfg.role)
        spec = reg.get(tool_name)
        if spec is None:
            return _jsonrpc_error(
                req_id,
                -32602,
                f"Tool '{tool_name}' is not registered for role '{cfg.role}'",
            )

        try:
            payload = spec.handler(arguments, cfg)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                    "structuredContent": payload,
                    "isError": False,
                },
            }
        except Exception as exc:
            _log_stderr(f"tool '{tool_name}' error: {exc}")
            err_payload = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(err_payload, ensure_ascii=False)}
                    ],
                    "structuredContent": err_payload,
                    "isError": True,
                },
            }

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


def serve_stdio(
    *,
    role: McpRole = "worker",
    db_path: Path = Path("data/shuati.db"),
    config_dir: Path = Path("config"),
    vectors_dir: Path = Path("data/vectors"),
    assets_dir: Path = Path("data/assets/images"),
    unified_dir: Path = Path("data/unified"),
    human_dir: Path = Path("human"),
    drafts_dir: Path = Path("data/drafts"),
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """运行换行分隔的 UTF-8 JSON-RPC 2.0 stdio MCP 服务主循环。"""
    if role not in _VALID_ROLES:
        raise ValueError(f"不支持的 MCP role: {role!r}")
    _ensure_utf8_streams()

    cfg = McpServerConfig(
        role=role,
        db_path=Path(db_path),
        config_dir=Path(config_dir),
        vectors_dir=Path(vectors_dir),
        assets_dir=Path(assets_dir),
        unified_dir=Path(unified_dir),
        human_dir=Path(human_dir),
        drafts_dir=Path(drafts_dir),
    )
    out_stream: TextIO = stdout if stdout is not None else sys.stdout

    def _emit(payload: dict[str, Any]) -> None:
        out_stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        out_stream.flush()

    def _handle_text_line(raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        try:
            resp = dispatch_jsonrpc_message(line, config=cfg)
        except Exception as exc:
            _log_stderr(f"internal dispatch error: {exc}")
            resp = _jsonrpc_error(None, -32603, f"Internal error: {exc}")
        if resp is not None:
            _emit(resp)

    _log_stderr(f"started stdio server (role={role})")
    if stdin is None and hasattr(sys.stdin, "buffer") and sys.stdin.buffer is not None:
        for raw_bytes in sys.stdin.buffer:
            try:
                decoded = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                _log_stderr(f"invalid UTF-8 line: {exc}")
                _emit(_jsonrpc_error(None, -32700, f"Parse error: invalid UTF-8 ({exc})"))
                continue
            _handle_text_line(decoded)
    else:
        in_stream: TextIO = stdin if stdin is not None else sys.stdin
        for raw_line in in_stream:
            _handle_text_line(raw_line)
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="刷题 Harness stdio MCP Server")
    parser.add_argument(
        "--role",
        default="worker",
        choices=sorted(_VALID_ROLES),
        help="角色隔离档位：worker / compiler / practice / all",
    )
    parser.add_argument("--db", default="data/shuati.db")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--vectors-dir", default="data/vectors")
    parser.add_argument("--assets-dir", default="data/assets/images")
    parser.add_argument("--unified-dir", default="data/unified")
    parser.add_argument("--human-dir", default="human")
    parser.add_argument("--drafts-dir", default="data/drafts")
    ns = parser.parse_args(argv)
    return serve_stdio(
        role=ns.role,
        db_path=Path(ns.db),
        config_dir=Path(ns.config_dir),
        vectors_dir=Path(ns.vectors_dir),
        assets_dir=Path(ns.assets_dir),
        unified_dir=Path(ns.unified_dir),
        human_dir=Path(ns.human_dir),
        drafts_dir=Path(ns.drafts_dir),
    )


if __name__ == "__main__":
    raise SystemExit(_main())
