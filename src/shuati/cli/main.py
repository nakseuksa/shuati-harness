"""Typer CLI 入口与统一 JSON 信封处理（DESIGN.md §5.4-3 与 §7 M0）。"""

from __future__ import annotations

import asyncio
import io
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

from shuati.config import load_settings
from shuati.core import effective
from shuati.db import human_mirror, repository
from shuati.db.connection import (
    CompileLock,
    PreconditionError,
    SingleWriterExecutor,
    get_connection,
)
from shuati.db.repository import AmbiguousUidError, NotFoundError
from shuati.db.schema import init_db
from shuati.llm.gateway import LLMGatewayError
from shuati.models.domain import (
    EXIT_INTERNAL,
    EXIT_LLM_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PRECONDITION,
    Envelope,
    ErrorDetail,
)
from shuati.pipeline.stage0_ingest import (
    run_stage0_ingest,
    sync_cold_start_learners_if_seeded,
)
from shuati.pipeline.stage1_solve import run_stage1_compile
from shuati.review.server import serve_review

# Windows 管道 UTF-8 编码重设（DESIGN.md §5.4-3）
if isinstance(sys.stdout, io.TextIOWrapper) and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if isinstance(sys.stderr, io.TextIOWrapper) and sys.stderr.encoding.lower() != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

app = typer.Typer(help="刷题 Harness v2 命令行工具")
grade_subject_app = typer.Typer(name="grade-subject", help="学段学科管理")
human_app = typer.Typer(name="human", help="人工资产管理与恢复")
taxonomy_app = typer.Typer(name="taxonomy", help="知识词表治理（DRAFT / LOCKED / 迁移）")
graph_app = typer.Typer(name="graph", help="知识网归并、DAG 检查与五边拓扑校准")

app.add_typer(grade_subject_app)
app.add_typer(human_app)
app.add_typer(taxonomy_app)
app.add_typer(graph_app)


def output_envelope(
    command: str,
    data: Any = None,
    warnings: list[str] | None = None,
    error: ErrorDetail | None = None,
    meta: dict[str, Any] | None = None,
    as_json: bool = True,
    exit_code: int = EXIT_OK,
) -> None:
    """输出标准统一 JSON 信封（ensure_ascii=False，日志只走 stderr）。"""
    meta_dict = meta or {}
    meta_dict.setdefault("schema_version", 1)

    envelope = Envelope(
        ok=(error is None),
        command=command,
        data=data,
        warnings=warnings or [],
        error=error,
        meta=meta_dict,
    )
    if as_json:
        # 必须输出到 stdout 且 ensure_ascii=False
        print(envelope.model_dump_json(exclude_none=True))
    else:
        if error:
            print(f"[{error.code}] {error.message}", file=sys.stderr)
            if error.details:
                print(f"Details: {error.details}", file=sys.stderr)
        else:
            print(f"命令 '{command}' 执行成功。")
            if warnings:
                for w in warnings:
                    print(f"告警: {w}", file=sys.stderr)
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)


def _ensure_db_initialized(conn: sqlite3.Connection, config_dir: Path | str) -> None:
    """确保数据库已建表并完成基础 bootstrap。"""
    init_db(conn, config_dir=config_dir)


@app.callback()
def main_callback() -> None:
    """刷题 Harness 主命令入口。"""
    pass


@app.command()
def version(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """查看版本号。"""
    from shuati import __version__

    output_envelope("version", data={"version": __version__}, as_json=json)


@app.command()
def ingest(
    root: list[Path] | None = typer.Option(
        None,
        "--root",
        help="原始题库根目录（可多次传参；省略时读取 settings.yaml 的 ingest.roots）",
    ),
    grade_subject: str | None = typer.Option(
        None, "--grade-subject", help="显式指定学段学科（仅限单集合导入）"
    ),
    dedup_near: bool | None = typer.Option(
        None,
        "--dedup-near/--no-dedup-near",
        help="开启/关闭 plain_fingerprint+dHash 近重复归并（默认读取 settings.yaml 的 ingest.dedup_near_default）",
    ),
    apply_retire: bool = typer.Option(
        False, "--apply-retire", help="下架源端已消失的题目 (is_retired=1)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只读体检预览，不写数据库、不锁库、不复制图片"
    ),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    reports_dir: Path | None = typer.Option(None, "--reports-dir", help="体检报告输出目录"),
    assets_dir: Path | None = typer.Option(None, "--assets-dir", help="CAS 图片归档目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """Stage 0：数据入库体检与清洗。"""
    t0 = time.time()
    try:
        settings = load_settings(config_dir / "settings.yaml")
        resolved_roots: list[Path | str] = (
            list(root) if root else list(settings.ingest.roots or ["题库"])
        )
        resolved_dedup_near = (
            bool(dedup_near) if dedup_near is not None else bool(settings.ingest.dedup_near_default)
        )
        report = run_stage0_ingest(
            db_path=db,
            root_dir=resolved_roots[0],
            root_dirs=resolved_roots,
            config_dir=config_dir,
            assets_dir=assets_dir,
            reports_dir=reports_dir,
            grade_subject_override=grade_subject,
            dedup_near=resolved_dedup_near,
            apply_retire=apply_retire,
            dry_run=dry_run,
        )
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "ingest",
            data=report.to_dict(),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
    except PreconditionError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "ingest",
            error=ErrorDetail(code="PRECONDITION_FAILED", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "ingest",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )


@grade_subject_app.command(name="add")
def grade_subject_add(
    name: str = typer.Option(..., "--name", help="学段学科名称，如 '初中物理'"),
    slug: str = typer.Option(..., "--slug", help="短名标识，如 'junior_physics'"),
    family: str = typer.Option("MATH", "--family", help="学科家族：MATH/PHYSICS/CHEMISTRY/STEM_OTHER"),
    display_name: str | None = typer.Option(None, "--display-name", help="前端显示名称"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """注册新的学段学科。"""
    t0 = time.time()
    conn = None
    try:
        conn = get_connection(db)
        _ensure_db_initialized(conn, config_dir)
        repository.add_grade_subject(
            conn, name=name, slug=slug, family=family, display_name=display_name
        )
        sync_cold_start_learners_if_seeded(conn, config_dir)
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "grade-subject.add",
            data={"grade_subject": name, "slug": slug, "family": family},
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
    except sqlite3.IntegrityError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "grade-subject.add",
            error=ErrorDetail(
                code="PRECONDITION_FAILED",
                message=f"学段学科 '{name}' 或 slug '{slug}' 已存在或约束冲突: {e}",
            ),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "grade-subject.add",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )
    finally:
        if conn is not None:
            conn.close()


@grade_subject_app.command(name="list")
def grade_subject_list(
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """列出全部已注册学段学科。"""
    t0 = time.time()
    conn = None
    try:
        conn = get_connection(db)
        _ensure_db_initialized(conn, config_dir)
        items = repository.list_grade_subjects(conn)
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "grade-subject.list",
            data={"items": items, "count": len(items)},
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "grade-subject.list",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )
    finally:
        if conn is not None:
            conn.close()


@human_app.command(name="restore")
def human_restore(
    from_dir: Path | None = typer.Option(None, "--from", help="人工资产镜像源目录"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅预览恢复行数，不写数据库"),
    force_wipe_logs: bool = typer.Option(
        False, "--force-wipe-logs", help="当快照 practice_log.jsonl 为空时强制清空库内做题日志"
    ),
    backups_dir: Path | None = typer.Option(None, "--backups-dir", help="备份目录"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """从 human/*.jsonl 恢复人工资产与外部错题（UPSERT 幂等恢复）。"""
    t0 = time.time()
    settings = load_settings(config_dir / "settings.yaml")
    actual_from_dir = from_dir if from_dir is not None else Path(settings.human_mirror.dir)
    actual_backups_dir = backups_dir if backups_dir is not None else Path("data/backups")

    def _execute_restore():
        conn = None
        try:
            conn = get_connection(db)
            _ensure_db_initialized(conn, config_dir)
            effective.install_eff_write_guard(conn)
            counts = human_mirror.restore_snapshot(
                conn,
                human_dir=actual_from_dir,
                backups_dir=actual_backups_dir,
                dry_run=dry_run,
                backups_keep=settings.human_mirror.backups_keep,
                include_practice_log=settings.human_mirror.include_practice_log,
                force_wipe_logs=force_wipe_logs,
            )
            duration_ms = int((time.time() - t0) * 1000)
            output_envelope(
                "human.restore",
                data={"counts": counts, "dry_run": dry_run},
                meta={"duration_ms": duration_ms},
                as_json=json,
                exit_code=EXIT_OK,
            )
        finally:
            if conn is not None:
                conn.close()

    try:
        if not dry_run:
            with CompileLock(db.parent / ".compile.lock"):
                _execute_restore()
        else:
            _execute_restore()
    except typer.Exit:
        raise
    except PreconditionError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "human.restore",
            error=ErrorDetail(code="PRECONDITION_FAILED", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "human.restore",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )


@human_app.command(name="verify")
def human_verify(
    from_dir: Path | None = typer.Option(None, "--from", help="人工资产镜像源目录"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """验证数据库内人工资产与磁盘 human/*.jsonl 的 SHA-256 一致性。"""
    t0 = time.time()
    conn = None
    settings = load_settings(config_dir / "settings.yaml")
    actual_from_dir = from_dir if from_dir is not None else Path(settings.human_mirror.dir)
    try:
        conn = get_connection(db)
        _ensure_db_initialized(conn, config_dir)
        results = human_mirror.verify_snapshot(
            conn,
            human_dir=actual_from_dir,
            include_practice_log=settings.human_mirror.include_practice_log,
        )
        duration_ms = int((time.time() - t0) * 1000)
        is_consistent = results.get("is_consistent", False)
        if is_consistent:
            output_envelope(
                "human.verify",
                data=results,
                meta={"duration_ms": duration_ms},
                as_json=json,
                exit_code=EXIT_OK,
            )
        else:
            output_envelope(
                "human.verify",
                data=results,
                error=ErrorDetail(code="PRECONDITION_FAILED", message="人工资产快照与数据库不一致"),
                meta={"duration_ms": duration_ms},
                as_json=json,
                exit_code=EXIT_PRECONDITION,
            )
    except typer.Exit:
        raise
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "human.verify",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )
    finally:
        if conn is not None:
            conn.close()


@human_app.command(name="reset")
def human_reset(
    learner_id: str = typer.Option("default", "--learner-id", help="重置后的默认学习者 ID"),
    learner_name: str = typer.Option("默认学习者", "--learner-name", help="重置后的默认学习者名称"),
    subject: str = typer.Option("小学奥数", "--subject", help="默认学段学科"),
    archive_dir: Path = typer.Option(Path("data/backups"), "--archive-dir", help="旧资产无损归档目录"),
    human_dir: Path | None = typer.Option(
        None, "--human-dir", help="人工镜像目录（省略时读取 settings.yaml 的 human_mirror.dir）"
    ),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅预览归档与清理范围，不写磁盘与数据库"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """无损归档既有 human/*.jsonl 并重置为干净单学习者起跑态。"""
    from shuati.services.compiler_tools import reset_human_workspace

    _run_cli_action(
        "human.reset",
        json,
        lambda: reset_human_workspace(
            learner_id=learner_id,
            learner_name=learner_name,
            subject=subject,
            archive_dir=archive_dir,
            human_dir=human_dir,
            db_path=db,
            config_dir=config_dir,
            dry_run=dry_run,
        ),
    )


@app.command()
def reindex(
    fts: bool = typer.Option(False, "--fts", help="全量重建 FTS5 全文索引"),
    effective_flag: bool = typer.Option(False, "--effective", help="全量重算生效层 eff_* 列"),
    vectors: bool = typer.Option(False, "--vectors", help="增量/全量同步 5 类向量 .npz 矩阵"),
    quantiles: bool = typer.Option(False, "--quantiles", help="重算并冻结经验分位表 meta.json"),
    grade_subject: str | None = typer.Option(None, "--grade-subject", help="限定学段学科"),
    force: bool = typer.Option(False, "--force", help="强制清空旧模型绑定并全量重算向量"),
    mock: bool | None = typer.Option(None, "--mock/--no-mock", help="使用离线确定性 Mock 向量"),
    vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """全量重建全文检索索引、生效层字段、本地向量矩阵或冻结分位表。"""
    t0 = time.time()
    conn = None
    try:
        lock = CompileLock(db.parent / ".compile.lock")
        with lock:
            conn = get_connection(db)
            _ensure_db_initialized(conn, config_dir)
            effective.install_eff_write_guard(conn)

            do_fts = fts
            do_eff = effective_flag
            if not do_fts and not do_eff and not vectors and not quantiles:
                do_fts = True
                do_eff = True

            res_data: dict[str, Any] = {}
            writer = SingleWriterExecutor()
            if do_eff or do_fts:
                with writer.transaction(conn):
                    if do_eff:
                        ver = effective.verify_effective_consistency(conn)
                        res_data["effective_recomputed"] = ver["total_checked"]
                        res_data["diff_count"] = ver["diff_count"]

                    if do_fts:
                        count_fts = repository.rebuild_fts(conn)
                        res_data["fts_rebuilt"] = count_fts

            if vectors or quantiles:
                from shuati.pipeline.stage4_weave import reindex_vectors_and_quantiles_async

                settings = load_settings(config_dir / "settings.yaml")
                use_mock_flag = bool(mock) if mock is not None else settings.pipeline.use_mock_llm
                vec_res = asyncio.run(
                    reindex_vectors_and_quantiles_async(
                        conn,
                        settings,
                        grade_subject=grade_subject,
                        rebuild_vectors=vectors,
                        rebuild_quantiles=quantiles,
                        force_rebuild=force,
                        use_mock=use_mock_flag,
                        vectors_dir=vectors_dir,
                    )
                )
                res_data["vectors"] = vec_res

            duration_ms = int((time.time() - t0) * 1000)
            output_envelope(
                "reindex",
                data=res_data,
                meta={"duration_ms": duration_ms},
                as_json=json,
                exit_code=EXIT_OK,
            )
    except typer.Exit:
        raise
    except PreconditionError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "reindex",
            error=ErrorDetail(code="PRECONDITION_FAILED", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "reindex",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )
    finally:
        if conn is not None:
            conn.close()


@app.command(name="compile")
def compile_stage(
    stage: str = typer.Option(..., "--stage", help="编译阶段（支持: 1|2|3c|3d|3e|4|all）"),
    grade_subject: str | None = typer.Option(None, "--grade-subject", help="限定学段学科（Stage 3/4 默认小学奥数）"),
    only_uids: list[str] | None = typer.Option(
        None, "--only-uids", help="限定题目 UID 或 source_id（可重复传参或逗号分隔）"
    ),
    stress_test_25: bool = typer.Option(False, "--stress-test-25", help="仅编译 25 题压力测试清单"),
    force: bool = typer.Option(False, "--force", help="强制重跑已 OK 题目"),
    include_human_verified: bool = typer.Option(
        False, "--include-human-verified", help="包含已人工定稿题目重跑"
    ),
    mode: str = typer.Option("api", "--mode", help="执行通道：api / export / import"),
    tasks_dir: Path | None = typer.Option(None, "--tasks-dir", help="Subagent 脱敏批任务包目录"),
    results: Path | None = typer.Option(None, "--results", help="Subagent 回传结果 JSONL 路径"),
    batch_size: int | None = typer.Option(None, "--batch-size", help="覆盖默认批大小"),
    include_unservable: bool = typer.Option(False, "--include-unservable", help="Stage 4 包含 NEEDS_REVIEW 题目"),
    ignore_drift: bool = typer.Option(False, "--ignore-drift", help="Stage 4 跳过向量分位漂移自动重算"),
    subagent_model: str | None = typer.Option(None, "--subagent-model", help="覆盖 Subagent 锁模 ID（export 模式）"),
    prompt_profile: str | None = typer.Option(None, "--prompt-profile", help="加载 Prompt profile（export 模式）"),
    overwrite_pending: bool = typer.Option(False, "--overwrite-pending", help="允许覆盖挂起未回灌批（export 模式）"),
    vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
    mock: bool | None = typer.Option(None, "--mock/--no-mock", help="启用离线 Mock LLM"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    assets_dir: Path | None = typer.Option(None, "--assets-dir", help="CAS 图片归档目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """离线知识编译流水线（断点续跑）。"""
    t0 = time.time()
    norm_stage = stage.strip().upper()
    norm_mode = mode.strip().lower()
    if norm_mode not in ("api", "export", "import"):
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "compile",
            error=ErrorDetail(code="PRECONDITION_FAILED", message=f"不支持的 --mode: {mode!r}"),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
        return

    allowed_stages = ("1", "S1", "2", "S2", "3C", "S3C", "3D", "S3D", "3E", "S3E", "4", "S4", "ALL")
    if norm_stage not in allowed_stages:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "compile",
            error=ErrorDetail(
                code="PRECONDITION_FAILED",
                message=f"不支持编译阶段 '{stage}'（支持: 1, 2, 3c, 3d, 3e, 4, all）",
            ),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
        return

    parsed_uids: list[str] | None = None
    if only_uids:
        parsed_uids = []
        for raw_entry in only_uids:
            for part in raw_entry.split(","):
                cleaned = part.strip()
                if cleaned:
                    parsed_uids.append(cleaned)

    try:
        if norm_stage in ("1", "S1"):
            res = asyncio.run(
                run_stage1_compile(
                    db_path=db,
                    config_dir=config_dir,
                    assets_dir=assets_dir,
                    grade_subject=grade_subject,
                    only_uids=parsed_uids,
                    stress_test_25=stress_test_25,
                    force=force,
                    include_human_verified=include_human_verified,
                    use_mock=mock,
                    mode=norm_mode,  # type: ignore[arg-type]
                    tasks_dir=tasks_dir or Path("data/unified/stage3_4/stage1"),
                    results_path=results,
                    batch_size=batch_size or 10,
                    subagent_model=subagent_model,
                    prompt_profile=prompt_profile,
                    overwrite_pending=overwrite_pending,
                )
            )
            data_out: dict[str, Any] = asdict(res)
        else:
            from shuati.pipeline.stage2_decompose import run_stage2_decompose_async
            from shuati.pipeline.stage3_taxonomy import (
                run_compaction_async,
                run_stage3c_async,
                run_stage3d_mount,
                run_stage3e_async,
            )
            from shuati.pipeline.stage4_weave import run_stage4_weave_async
            from shuati.vectors.store import VectorStore

            gs = grade_subject or "小学奥数"
            settings = load_settings(config_dir / "settings.yaml")
            if mock is not None:
                settings.pipeline.use_mock_llm = bool(mock)
            use_mock_flag = bool(mock) if mock is not None else settings.pipeline.use_mock_llm

            conn = get_connection(db)
            try:
                _ensure_db_initialized(conn, config_dir)
                effective.install_eff_write_guard(conn)
                if norm_stage in ("2", "S2"):
                    data_out = asyncio.run(
                        run_stage2_decompose_async(
                            conn,
                            settings,
                            grade_subject=gs,
                            mode=norm_mode,  # type: ignore[arg-type]
                            only_uids=parsed_uids,
                            force=force,
                            batch_size=batch_size or 10,
                            tasks_dir=tasks_dir or Path("data/unified/stage2"),
                            results_path=results,
                            lock_path=db.parent / ".compile.lock",
                            config_dir=config_dir,
                            assets_dir=assets_dir,
                            subagent_model=subagent_model,
                            prompt_profile=prompt_profile,
                            overwrite_pending=overwrite_pending,
                            use_mock=use_mock_flag,
                        )
                    )
                elif norm_stage in ("3C", "S3C"):
                    data_out = asyncio.run(
                        run_stage3c_async(
                            conn,
                            settings,
                            grade_subject=gs,
                            mode=norm_mode,  # type: ignore[arg-type]
                            batch_size=batch_size or 20,
                            tasks_dir=tasks_dir or Path("data/unified/stage3_4/3c"),
                            results_path=results,
                            lock_path=db.parent / ".compile.lock",
                            config_dir=config_dir,
                            subagent_model=subagent_model,
                            prompt_profile=prompt_profile,
                            overwrite_pending=overwrite_pending,
                        )
                    )
                elif norm_stage in ("3D", "S3D"):
                    data_out = run_stage3d_mount(
                        conn,
                        grade_subject=gs,
                        lock_path=db.parent / ".compile.lock",
                        config_dir=config_dir,
                        prompt_profile=prompt_profile,
                    )
                elif norm_stage in ("3E", "S3E"):
                    data_out = asyncio.run(
                        run_stage3e_async(
                            conn,
                            settings,
                            grade_subject=gs,
                            mode=norm_mode,  # type: ignore[arg-type]
                            tasks_dir=tasks_dir or Path("data/unified/stage3_4/3e"),
                            results_path=results,
                            lock_path=db.parent / ".compile.lock",
                            config_dir=config_dir,
                            subagent_model=subagent_model,
                            prompt_profile=prompt_profile,
                            overwrite_pending=overwrite_pending,
                        )
                    )
                elif norm_stage in ("4", "S4"):
                    data_out = asyncio.run(
                        run_stage4_weave_async(
                            conn,
                            settings,
                            grade_subject=gs,
                            mode=norm_mode,  # type: ignore[arg-type]
                            batch_size=batch_size or 25,
                            include_unservable=include_unservable,
                            tasks_dir=tasks_dir or Path("data/unified/stage3_4/stage4"),
                            results_path=results,
                            vectors_dir=vectors_dir,
                            use_mock=use_mock_flag,
                            lock_path=db.parent / ".compile.lock",
                            config_dir=config_dir,
                            subagent_model=subagent_model,
                            prompt_profile=prompt_profile,
                            overwrite_pending=overwrite_pending,
                            only_uids=parsed_uids,
                            ignore_drift=ignore_drift,
                        )
                    )
                else:  # ALL
                    if norm_mode != "api":
                        raise PreconditionError("--stage all 仅支持 --mode api；双通道请分阶段 export/import")
                    s3c_res = asyncio.run(
                        run_stage3c_async(
                            conn,
                            settings,
                            grade_subject=gs,
                            mode="api",
                            batch_size=batch_size or 20,
                            lock_path=db.parent / ".compile.lock",
                            config_dir=config_dir,
                            prompt_profile=prompt_profile,
                        )
                    )
                    s3d_res = run_stage3d_mount(
                        conn,
                        grade_subject=gs,
                        lock_path=db.parent / ".compile.lock",
                        config_dir=config_dir,
                        prompt_profile=prompt_profile,
                    )
                    cmp_res = asyncio.run(
                        run_compaction_async(
                            conn,
                            settings,
                            VectorStore(vectors_dir),
                            grade_subject=gs,
                            mode="api",
                            use_mock=use_mock_flag,
                            lock_path=db.parent / ".compile.lock",
                            config_dir=config_dir,
                            prompt_profile=prompt_profile,
                        )
                    )
                    s3e_res = asyncio.run(
                        run_stage3e_async(
                            conn,
                            settings,
                            grade_subject=gs,
                            mode="api",
                            lock_path=db.parent / ".compile.lock",
                            config_dir=config_dir,
                            prompt_profile=prompt_profile,
                        )
                    )
                    s4_res = asyncio.run(
                        run_stage4_weave_async(
                            conn,
                            settings,
                            grade_subject=gs,
                            mode="api",
                            include_unservable=include_unservable,
                            vectors_dir=vectors_dir,
                            use_mock=use_mock_flag,
                            lock_path=db.parent / ".compile.lock",
                            config_dir=config_dir,
                            prompt_profile=prompt_profile,
                        )
                    )
                    data_out = {
                        "stage3c": s3c_res,
                        "stage3d": s3d_res,
                        "compact": cmp_res,
                        "stage3e": s3e_res,
                        "stage4": s4_res,
                    }
            finally:
                conn.close()

        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "compile",
            data=data_out,
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
    except AmbiguousUidError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "compile",
            error=ErrorDetail(
                code="AMBIGUOUS_UID",
                message=str(e),
                details={"candidates": e.candidates},
            ),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except PreconditionError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "compile",
            error=ErrorDetail(code="PRECONDITION_FAILED", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except NotFoundError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "compile",
            error=ErrorDetail(code="NOT_FOUND", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_NOT_FOUND,
        )
    except LLMGatewayError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "compile",
            error=ErrorDetail(code="LLM_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_LLM_ERROR,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "compile",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )


@app.command()
def review(
    grade_subject: str | None = typer.Option(None, "--grade-subject", help="限定学段学科"),
    port: int = typer.Option(8765, "--port", help="本地服务端口（强制绑定 127.0.0.1）"),
    audit: bool = typer.Option(False, "--audit", help="假共识抽检模式"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    assets_dir: Path | None = typer.Option(None, "--assets-dir", help="CAS 图片归档目录"),
) -> None:
    """启动极简本地键盘复核页（http://127.0.0.1:<port>/）。"""
    serve_review(
        db_path=db,
        config_dir=config_dir,
        assets_dir=assets_dir,
        grade_subject=grade_subject,
        audit_mode=audit,
        port=port,
    )


unified_app = typer.Typer(help="主会话 Subagent 合一初始化编译器（Stage 1.5 + Stage 1+2）。")
app.add_typer(unified_app, name="unified")


@unified_app.command(name="calibrate-anchors")
def unified_calibrate_anchors(
    grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="学段学科"),
    anchors_file: Path | None = typer.Option(None, "--anchors-file", help="指定黄金锚点 YAML"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """加载 Stage 1.5 黄金锚点并置 anchors_ready=1。"""
    from shuati.pipeline.unified_import import register_anchors_from_yaml

    t0 = time.time()
    try:
        res = register_anchors_from_yaml(
            db_path=db,
            config_dir=config_dir,
            grade_subject=grade_subject,
            anchors_file=anchors_file,
        )
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.calibrate-anchors",
            data=asdict(res),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
    except PreconditionError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.calibrate-anchors",
            error=ErrorDetail(code="PRECONDITION_FAILED", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.calibrate-anchors",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )


@unified_app.command(name="export")
def unified_export(
    grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="学段学科"),
    out_dir: Path = typer.Option(Path("data/unified"), "--out-dir", help="脱敏任务包输出目录"),
    only_uids: list[str] | None = typer.Option(None, "--only-uids", help="限定题目 UID 或 source_id"),
    limit: int | None = typer.Option(None, "--limit", help="最大导出题数"),
    batch_size: int = typer.Option(10, "--batch-size", help="每批任务卡数量"),
    force: bool = typer.Option(False, "--force", help="忽略锁定状态强制导出"),
    subagent_model: str | None = typer.Option(None, "--subagent-model", help="覆盖 Subagent 锁模 ID"),
    prompt_profile: str | None = typer.Option(None, "--prompt-profile", help="加载 Prompt profile"),
    overwrite_pending: bool = typer.Option(False, "--overwrite-pending", help="允许覆盖挂起未回灌批"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """导出脱敏任务 JSONL（UNIFIED_S1_S2 / STAGE2_ONLY）与隔离 task_map.json。"""
    from shuati.pipeline.unified_import import export_unified_tasks

    t0 = time.time()
    parsed_uids: list[str] | None = None
    if only_uids:
        parsed_uids = [p.strip() for raw in only_uids for p in raw.split(",") if p.strip()]
    try:
        res = export_unified_tasks(
            db_path=db,
            out_dir=out_dir,
            config_dir=config_dir,
            grade_subject=grade_subject,
            only_uids=parsed_uids,
            limit=limit,
            batch_size=batch_size,
            force=force,
            subagent_model=subagent_model,
            prompt_profile=prompt_profile,
            overwrite_pending=overwrite_pending,
        )
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.export",
            data=asdict(res),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
    except (PreconditionError, AmbiguousUidError) as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.export",
            error=ErrorDetail(code="PRECONDITION_FAILED", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except NotFoundError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.export",
            error=ErrorDetail(code="NOT_FOUND", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_NOT_FOUND,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.export",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )


@unified_app.command(name="import")
def unified_import(
    results: Path = typer.Option(..., "--results", help="Subagent 输出 JSONL 路径"),
    task_map: Path = typer.Option(Path("data/unified/task_map.json"), "--task-map", help="task_map.json 路径"),
    retry_out: Path | None = typer.Option(None, "--retry-out", help="失败任务重试输出路径"),
    final_round: bool = typer.Option(False, "--final", help="终轮回灌：SymPy 仍败者降级 NEEDS_REVIEW 保留 Stage 2"),
    force: bool = typer.Option(False, "--force", help="强制覆盖已锁定条目"),
    worker_tag: str = typer.Option("main-session", "--worker-tag", help="记录来源标识"),
    subagent_model: str | None = typer.Option(None, "--subagent-model", help="显式指定回灌记录的 Subagent 模型 ID"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """确定性校验并事务回灌主会话 Subagent 结果。"""
    from shuati.pipeline.unified_import import import_unified_results

    t0 = time.time()
    try:
        res = import_unified_results(
            db_path=db,
            results_file=results,
            task_map_file=task_map,
            config_dir=config_dir,
            retry_out=retry_out,
            final_round=final_round,
            force=force,
            worker_tag=worker_tag,
            subagent_model=subagent_model,
        )
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.import",
            data=asdict(res),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
    except PreconditionError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.import",
            error=ErrorDetail(code="PRECONDITION_FAILED", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_PRECONDITION,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "unified.import",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )


def _run_cli_action(cmd_name: str, as_json: bool, fn: Any) -> None:
    t0 = time.time()
    try:
        res = fn()
        duration_ms = int((time.time() - t0) * 1000)
        warnings = []
        if isinstance(res, dict) and isinstance(res.get("warnings"), list):
            warnings = [str(w) for w in res.get("warnings", [])]
        output_envelope(
            cmd_name,
            data=res,
            warnings=warnings,
            meta={"duration_ms": duration_ms},
            as_json=as_json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
    except AmbiguousUidError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            cmd_name,
            error=ErrorDetail(code="AMBIGUOUS_UID", message=str(e), details={"candidates": e.candidates}),
            meta={"duration_ms": duration_ms},
            as_json=as_json,
            exit_code=EXIT_PRECONDITION,
        )
    except PreconditionError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            cmd_name,
            error=ErrorDetail(code="PRECONDITION_FAILED", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=as_json,
            exit_code=EXIT_PRECONDITION,
        )
    except NotFoundError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            cmd_name,
            error=ErrorDetail(code="NOT_FOUND", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=as_json,
            exit_code=EXIT_NOT_FOUND,
        )
    except LLMGatewayError as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            cmd_name,
            error=ErrorDetail(code="LLM_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=as_json,
            exit_code=EXIT_LLM_ERROR,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            cmd_name,
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=as_json,
            exit_code=EXIT_INTERNAL,
        )


@taxonomy_app.command(name="propose")
def taxonomy_propose(
    grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="目标学段学科"),
    mode: str = typer.Option("api", "--mode", help="执行模式：api / export / import"),
    tasks_dir: Path = typer.Option(Path("data/unified/stage3_4/propose"), "--tasks-dir", help="Subagent 任务包目录"),
    results: Path | None = typer.Option(None, "--results", help="Subagent 回传结果 JSONL 路径"),
    n_clusters: int | None = typer.Option(None, "--n-clusters", help="覆盖聚类簇数（默认 60~100）"),
    subagent_model: str | None = typer.Option(None, "--subagent-model", help="覆盖 Subagent 锁模 ID（export 模式）"),
    prompt_profile: str | None = typer.Option(None, "--prompt-profile", help="加载 Prompt profile（export 模式）"),
    overwrite_pending: bool = typer.Option(False, "--overwrite-pending", help="允许覆盖挂起未回灌批（export 模式）"),
    mock: bool | None = typer.Option(None, "--mock/--no-mock", help="使用离线 Mock Embedding/LLM"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """Stage 3a：对全库 S2 有效题原始步骤标签聚类并涌现标准词表草案。"""
    from shuati.pipeline.stage3_taxonomy import propose_taxonomy_async

    def _work() -> dict[str, Any]:
        settings = load_settings(config_dir / "settings.yaml")
        if mock is not None:
            settings.pipeline.use_mock_llm = bool(mock)
        use_mock_flag = bool(mock) if mock is not None else settings.pipeline.use_mock_llm
        conn = get_connection(db)
        try:
            _ensure_db_initialized(conn, config_dir)
            effective.install_eff_write_guard(conn)
            return asyncio.run(
                propose_taxonomy_async(
                    conn,
                    settings,
                    grade_subject=grade_subject,
                    mode=mode.strip().lower(),  # type: ignore[arg-type]
                    tasks_dir=tasks_dir,
                    results_path=results,
                    config_dir=config_dir,
                    use_mock=use_mock_flag,
                    n_clusters=n_clusters,
                    subagent_model=subagent_model,
                    prompt_profile=prompt_profile,
                    overwrite_pending=overwrite_pending,
                )
            )
        finally:
            conn.close()

    _run_cli_action("taxonomy.propose", json, _work)


@taxonomy_app.command(name="lock")
def taxonomy_lock(
    grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="目标学段学科"),
    file: Path | None = typer.Option(None, "--file", help="指定 YAML 文件（默认读取学段当前 DRAFT YAML）"),
    to_version: str = typer.Option("1.0", "--to-version", help="目标版本号"),
    status: str = typer.Option("LOCKED", "--status", help="目标状态（LOCKED 或 DRAFT）"),
    allow_stale_overrides: bool = typer.Option(False, "--allow-stale-overrides", help="无法自动迁移的人工覆写置为 TAXONOMY_UNMAPPED 挂起"),
    force_recompile: bool = typer.Option(False, "--force-recompile", help="强制清空旧节点与挂载并全学段重编"),
    vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅预检人工覆写迁移影响，不写库"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工覆盖层镜像目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """Stage 3b：四步原子事务执行 DRAFT → LOCKED 1.0 锁表迁移（或 DRAFT 换版）。"""
    from shuati.core.taxonomy import default_taxonomy_path, transition_taxonomy

    def _work() -> dict[str, Any]:
        conn = get_connection(db)
        try:
            _ensure_db_initialized(conn, config_dir)
            effective.install_eff_write_guard(conn)
            gs = conn.execute(
                "SELECT slug, taxonomy_file FROM grade_subjects WHERE grade_subject = ?;",
                (grade_subject,),
            ).fetchone()
            if gs is None:
                raise PreconditionError(f"PRECONDITION_FAILED: 学段学科不存在: {grade_subject!r}")
            src_yaml = file or (Path(gs["taxonomy_file"]) if gs["taxonomy_file"] else default_taxonomy_path(config_dir, str(gs["slug"]), "DRAFT"))
            norm_status = status.strip().upper()
            if norm_status not in ("DRAFT", "LOCKED"):
                raise PreconditionError(f"PRECONDITION_FAILED: 非法 --status: {status!r}")
            return transition_taxonomy(
                conn,
                grade_subject=grade_subject,
                yaml_path=src_yaml,
                target_version=to_version,
                target_status=norm_status,  # type: ignore[arg-type]
                allow_stale_overrides=allow_stale_overrides,
                dry_run=dry_run,
                force_recompile=force_recompile,
                vectors_dir=vectors_dir,
                config_dir=config_dir,
                backups_dir=db.parent / "backups",
                human_dir=human_dir,
                reports_dir=db.parent / "reports",
                lock_path=db.parent / ".compile.lock",
            )
        finally:
            conn.close()

    _run_cli_action("taxonomy.lock", json, _work)


@taxonomy_app.command(name="migrate")
def taxonomy_migrate(
    grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="目标学段学科"),
    file: Path = typer.Option(..., "--file", help="包含 changelog 的新 LOCKED YAML 文件"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅预检影响范围，不写库"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工覆盖层镜像目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """按 LOCKED YAML 中的 changelog（add/rename/merge）精准增量迁移词表版本。"""
    from shuati.core.taxonomy import migrate_taxonomy_incremental

    def _work() -> dict[str, Any]:
        conn = get_connection(db)
        try:
            _ensure_db_initialized(conn, config_dir)
            effective.install_eff_write_guard(conn)
            return migrate_taxonomy_incremental(
                conn,
                grade_subject=grade_subject,
                new_yaml_path=file,
                dry_run=dry_run,
                config_dir=config_dir,
                backups_dir=db.parent / "backups",
                human_dir=human_dir,
                lock_path=db.parent / ".compile.lock",
            )
        finally:
            conn.close()

    _run_cli_action("taxonomy.migrate", json, _work)


@taxonomy_app.command(name="candidates")
def taxonomy_candidates(
    grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="目标学段学科"),
    status: str = typer.Option("PENDING", "--status", help="筛选候选状态（PENDING/APPROVED/REJECTED/MAPPED/ALL）"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """查看 Stage 3c 运行期间收集的新 L1/L2 词表候选队列。"""
    import json as json_lib

    def _work() -> dict[str, Any]:
        conn = get_connection(db, readonly=True)
        try:
            norm = status.strip().upper()
            if norm == "ALL":
                rows = conn.execute(
                    "SELECT * FROM taxonomy_candidates WHERE grade_subject = ? ORDER BY candidate_id;",
                    (grade_subject,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM taxonomy_candidates WHERE grade_subject = ? AND status = ? ORDER BY candidate_id;",
                    (grade_subject, norm),
                ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                d["example_question_uids"] = json_lib.loads(d.pop("example_question_uids_json", "[]") or "[]")
                items.append(d)
            return {"grade_subject": grade_subject, "count": len(items), "candidates": items}
        finally:
            conn.close()

    _run_cli_action("taxonomy.candidates", json, _work)


@graph_app.command(name="compact")
def graph_compact(
    grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="目标学段学科"),
    target: str = typer.Option("all", "--target", help="归并对象：all / l3 / concepts"),
    mode: str = typer.Option("api", "--mode", help="执行模式：api / export / import"),
    batch_size: int = typer.Option(40, "--batch-size", help="批裁判候选对数量"),
    tasks_dir: Path = typer.Option(Path("data/unified/stage3_4/compact"), "--tasks-dir", help="Subagent 任务包目录"),
    results: Path | None = typer.Option(None, "--results", help="Subagent 回传结果路径"),
    vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
    subagent_model: str | None = typer.Option(None, "--subagent-model", help="覆盖 Subagent 锁模 ID（export 模式）"),
    prompt_profile: str | None = typer.Option(None, "--prompt-profile", help="加载 Prompt profile（export 模式）"),
    overwrite_pending: bool = typer.Option(False, "--overwrite-pending", help="允许覆盖挂起未回灌批（export 模式）"),
    mock: bool | None = typer.Option(None, "--mock/--no-mock", help="使用离线 Mock 向量与 LLM"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """Stage 3d：全量同域双路召回并执行可逆并查集同义归并。"""
    from shuati.pipeline.stage3_taxonomy import run_compaction_async
    from shuati.vectors.store import VectorStore

    def _work() -> dict[str, Any]:
        settings = load_settings(config_dir / "settings.yaml")
        if mock is not None:
            settings.pipeline.use_mock_llm = bool(mock)
        use_mock_flag = bool(mock) if mock is not None else settings.pipeline.use_mock_llm
        conn = get_connection(db)
        try:
            _ensure_db_initialized(conn, config_dir)
            effective.install_eff_write_guard(conn)
            return asyncio.run(
                run_compaction_async(
                    conn,
                    settings,
                    VectorStore(vectors_dir),
                    grade_subject=grade_subject,
                    target=target.strip().lower(),  # type: ignore[arg-type]
                    mode=mode.strip().lower(),  # type: ignore[arg-type]
                    batch_size=batch_size,
                    tasks_dir=tasks_dir,
                    results_path=results,
                    use_mock=use_mock_flag,
                    lock_path=db.parent / ".compile.lock",
                    config_dir=config_dir,
                    subagent_model=subagent_model,
                    prompt_profile=prompt_profile,
                    overwrite_pending=overwrite_pending,
                )
            )
        finally:
            conn.close()

    _run_cli_action("graph.compact", json, _work)


@graph_app.command(name="revert-merge")
def graph_revert_merge(
    batch_id: str = typer.Option(..., "--batch-id", help="要无损逆序回滚的归并批次 ID"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """按 merge_id 严格逆序无损回滚指定归并批次（拒绝乱序回滚）。"""
    from shuati.core.knowledge_graph import revert_merge_batch

    def _work() -> dict[str, Any]:
        conn = get_connection(db)
        try:
            _ensure_db_initialized(conn, config_dir)
            effective.install_eff_write_guard(conn)
            return revert_merge_batch(
                conn,
                batch_id,
                backups_dir=db.parent / "backups",
                lock_path=db.parent / ".compile.lock",
            )
        finally:
            conn.close()

    _run_cli_action("graph.revert-merge", json, _work)


@graph_app.command(name="check-dag")
def graph_check_dag(
    grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="目标学段学科"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """校验当前学段 PREREQUISITE 知识点拓扑边强无环（DAG）特性。"""
    from shuati.core.knowledge_graph import check_dag_and_break_cycles

    def _work() -> dict[str, Any]:
        conn = get_connection(db, readonly=True)
        try:
            rows = conn.execute(
                """
                SELECT source_node_id, target_node_id, weight
                FROM knowledge_edges
                WHERE grade_subject = ? AND edge_type = 'PREREQUISITE';
                """,
                (grade_subject,),
            ).fetchall()
            acyclic, broken = check_dag_and_break_cycles([dict(r) for r in rows])
            return {
                "grade_subject": grade_subject,
                "total_edges": len(rows),
                "is_acyclic": len(broken) == 0,
                "acyclic_edges": len(acyclic),
                "cycle_edges_found": len(broken),
                "broken_edges": broken,
            }
        finally:
            conn.close()

    _run_cli_action("graph.check-dag", json, _work)


@graph_app.command(name="calibrate-eval")
def graph_calibrate_eval(
    stress_yaml: Path = typer.Option(Path("config/stress_test_25.yaml"), "--stress-yaml", help="校准配置文件路径"),
    propose_candidates: bool = typer.Option(False, "--propose-candidates", help="从五边与边界带抽样 ~150 对候选供审定"),
    per_type_samples: int = typer.Option(30, "--per-type-samples", help="每类边抽样上限"),
    vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
    reports_dir: Path = typer.Option(Path("data/reports"), "--reports-dir", help="输出报告目录"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """评估五边校准样本命中率/误报率，或抽样 ~150 对五边候选供阈值校准。"""
    from shuati.pipeline.stage4_weave import evaluate_calibration_pairs

    def _work() -> dict[str, Any]:
        settings = load_settings(config_dir / "settings.yaml")
        conn = get_connection(db, readonly=True)
        try:
            return evaluate_calibration_pairs(
                conn,
                settings,
                stress_yaml_path=stress_yaml,
                propose_candidates=propose_candidates,
                per_type_samples=per_type_samples,
                vectors_dir=vectors_dir,
                reports_dir=reports_dir,
            )
        finally:
            conn.close()

    _run_cli_action("graph.calibrate-eval", json, _work)


@app.command(name="audit")
def audit_cmd(
    grade_subject: str | None = typer.Option(None, "--grade-subject", help="限定学段学科"),
    prune_runs: bool = typer.Option(False, "--prune-runs", help="清理未被引用的 FAILED stage1_vlm_runs"),
    human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工覆盖层镜像目录"),
    vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
    reports_dir: Path = typer.Option(Path("data/reports"), "--reports-dir", help="输出报告目录"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """Stage Q 全库自动质量体检，输出 7 大板块报告（Markdown + JSON）。"""
    from shuati.pipeline.audit import run_stage_q_audit

    def _work() -> dict[str, Any]:
        settings = load_settings(config_dir / "settings.yaml")
        # verify_effective_consistency 会临时执行 effective.recompute 并对比 diff；非 --prune-runs 时末尾回滚以保持无副作用只读审计
        conn = get_connection(db, readonly=False)
        try:
            if prune_runs:
                _ensure_db_initialized(conn, config_dir)
            report = run_stage_q_audit(
                conn,
                settings,
                grade_subject=grade_subject,
                human_dir=human_dir,
                vectors_dir=vectors_dir,
                reports_dir=reports_dir,
                config_dir=config_dir,
                prune_runs=prune_runs,
            )
            if not prune_runs and conn.in_transaction:
                conn.rollback()
            return report
        finally:
            conn.close()

    _run_cli_action("audit", json, _work)


@app.command(name="export")
def export_cmd(
    grade_subject: str | None = typer.Option(None, "--grade-subject", help="限定学段学科"),
    export_dir: Path = typer.Option(Path("data/exports"), "--export-dir", help="标准 JSONL 导出目录"),
    include_retired: bool = typer.Option(False, "--include-retired", help="是否包含已废题/屏蔽题"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """导出 4 份脱离 SQLite 的标准 JSONL 资产。"""
    from shuati.db.exporter import export_all_jsonl

    def _work() -> dict[str, Any]:
        conn = get_connection(db, readonly=True)
        try:
            return export_all_jsonl(
                conn,
                export_dir=export_dir,
                grade_subject=grade_subject,
                include_retired=include_retired,
            )
        finally:
            conn.close()

    _run_cli_action("export", json, _work)


from shuati.cli.compiler_commands import register_compiler_commands as _register_compiler_commands  # noqa: E402
from shuati.cli.m3_commands import register_m3_commands as _register_m3_commands  # noqa: E402

_register_m3_commands(app, taxonomy_app, _run_cli_action)
_register_compiler_commands(app, _run_cli_action)


if __name__ == "__main__":
    app()
