"""Typer CLI 入口与统一 JSON 信封处理（DESIGN.md §5.4-3 与 §7 M0）。"""

from __future__ import annotations

import io
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import typer

from shuati.core import effective
from shuati.db import human_mirror, repository
from shuati.db.connection import (
    PreconditionError,
    get_connection,
)
from shuati.db.schema import init_db
from shuati.models.domain import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_PRECONDITION,
    Envelope,
    ErrorDetail,
)
from shuati.pipeline.stage0_ingest import run_stage0_ingest

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

app.add_typer(grade_subject_app)
app.add_typer(human_app)


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
    root: Path = typer.Option(Path("题库"), "--root", help="原始题库根目录"),
    grade_subject: str | None = typer.Option(
        None, "--grade-subject", help="显式指定学段学科（仅限单集合导入）"
    ),
    dedup_near: bool = typer.Option(
        False, "--dedup-near", help="开启 plain_fingerprint+dHash 近重复归并"
    ),
    apply_retire: bool = typer.Option(
        False, "--apply-retire", help="下架源端已消失的题目 (is_retired=1)"
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
        report = run_stage0_ingest(
            db_path=db,
            root_dir=root,
            config_dir=config_dir,
            assets_dir=assets_dir,
            reports_dir=reports_dir,
            grade_subject_override=grade_subject,
            dedup_near=dedup_near,
            apply_retire=apply_retire,
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
    try:
        conn = get_connection(db)
        _ensure_db_initialized(conn, config_dir)
        repository.add_grade_subject(
            conn, name=name, slug=slug, family=family, display_name=display_name
        )
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


@grade_subject_app.command(name="list")
def grade_subject_list(
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """列出全部已注册学段学科。"""
    t0 = time.time()
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


@human_app.command(name="restore")
def human_restore(
    from_dir: Path = typer.Option(Path("human"), "--from", help="人工资产镜像源目录"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅预览恢复行数，不写数据库"),
    backups_dir: Path = typer.Option(Path("data/backups"), "--backups-dir", help="备份目录"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """从 human/*.jsonl 恢复人工资产（UPSERT 幂等恢复）。"""
    t0 = time.time()
    try:
        conn = get_connection(db)
        _ensure_db_initialized(conn, config_dir)
        effective.install_eff_write_guard(conn)
        counts = human_mirror.restore_snapshot(
            conn, human_dir=from_dir, backups_dir=backups_dir, dry_run=dry_run
        )
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "human.restore",
            data={"counts": counts, "dry_run": dry_run},
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK,
        )
    except typer.Exit:
        raise
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
    from_dir: Path = typer.Option(Path("human"), "--from", help="人工资产镜像源目录"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """验证数据库内人工资产与磁盘 human/*.jsonl 的 SHA-256 一致性。"""
    t0 = time.time()
    try:
        conn = get_connection(db)
        _ensure_db_initialized(conn, config_dir)
        results = human_mirror.verify_snapshot(conn, human_dir=from_dir)
        duration_ms = int((time.time() - t0) * 1000)
        is_consistent = results.get("is_consistent", False)
        output_envelope(
            "human.verify",
            data=results,
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_OK if is_consistent else EXIT_PRECONDITION,
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


@app.command()
def reindex(
    fts: bool = typer.Option(False, "--fts", help="全量重建 FTS5 全文索引"),
    effective_flag: bool = typer.Option(False, "--effective", help="全量重算生效层 eff_* 列"),
    db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
    config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
) -> None:
    """全量重建全文检索索引或重算生效层字段。"""
    t0 = time.time()
    try:
        conn = get_connection(db)
        _ensure_db_initialized(conn, config_dir)
        effective.install_eff_write_guard(conn)

        do_fts = fts
        do_eff = effective_flag
        if not do_fts and not do_eff:
            do_fts = True
            do_eff = True

        res_data: dict[str, Any] = {}
        if do_eff:
            count_eff = effective.recompute_all(conn)
            res_data["effective_recomputed"] = count_eff

        if do_fts:
            count_fts = repository.rebuild_fts(conn)
            res_data["fts_rebuilt"] = count_fts

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
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        output_envelope(
            "reindex",
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(e)),
            meta={"duration_ms": duration_ms},
            as_json=json,
            exit_code=EXIT_INTERNAL,
        )


if __name__ == "__main__":
    app()
