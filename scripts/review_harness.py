#!/usr/bin/env python3
"""增量式外部 AI 代码与数据审查脚手架（Zero-LLM-Cost Local Review Harness）。

提供五大确定性子命令与同名可测试库函数：
- validate   : 校验 config/review_plan.yaml 架构、行号边界、100% 模块/切片无死角覆盖与表名合法性
- prepare    : 组装自包含审查任务单 (<ID>_task.md)，内联速查卡、精准表 DDL 与跨文件/切片外 AST Stub
- status     : 计算目标文件实时 SHA256 脏状态 (MODIFIED_SINCE_REVIEW)、待裁决队列与下一推荐批次
- record     : 记录单批次审查状态、异构模型家族历史警告与目标文件 SHA256 快照
- mark-fixed : 批量标记修复完成、刷新哈希快照并巡检连带受影响脏批次
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import textwrap
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

# 固定使用 UTC+8 偏移，彻底规避 Windows 缺少 tzdata 包导致 ZoneInfoNotFoundError
TZ_UTC8 = timezone(timedelta(hours=8))

VALID_STATUSES: frozenset[str] = frozenset({"PENDING", "REVIEWED", "TRIAGED_CLEAN", "FIXED"})
VALID_TIERS: frozenset[int] = frozenset({1, 2, 3, 4})

_BATCH_ID_RE = re.compile(r"^B(?:0[1-9]|[1-9]\d+)$")

_LEADING_SQL_TRIVIA_RE = re.compile(r"\A(?:\s|--[^\r\n]*|/\*[\s\S]*?\*/)+")

_SQL_IDENT_CAPTURE = (
    r'(?:"(?P<dq>[A-Za-z_][A-Za-z0-9_]*)"'
    r"|`(?P<bt>[A-Za-z_][A-Za-z0-9_]*)`"
    r"|\[(?P<br>[A-Za-z_][A-Za-z0-9_]*)\]"
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*)\b)"
)

_SQL_IDENT_NONCAP = (
    r'(?:"[A-Za-z_][A-Za-z0-9_]*"'
    r"|`[A-Za-z_][A-Za-z0-9_]*`"
    r"|\[[A-Za-z_][A-Za-z0-9_]*\]"
    r"|[A-Za-z_][A-Za-z0-9_]*\b)"
)

_CREATE_TABLE_RE = re.compile(
    rf"\ACREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?!IF\s+NOT\s+EXISTS\b){_SQL_IDENT_CAPTURE}",
    re.IGNORECASE,
)

_CREATE_INDEX_RE = re.compile(
    rf"\ACREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(?!IF\s+NOT\s+EXISTS\b){_SQL_IDENT_NONCAP}\s+ON\s+{_SQL_IDENT_CAPTURE}",
    re.IGNORECASE,
)

_SEMANTIC_DECORATORS: frozenset[str] = frozenset(
    {
        "dataclass",
        "contextmanager",
        "asynccontextmanager",
        "classmethod",
        "staticmethod",
        "property",
    }
)

_PROTOCOL_DUNDERS: frozenset[str] = frozenset(
    {
        "__init__",
        "__enter__",
        "__exit__",
        "__aenter__",
        "__aexit__",
        "__call__",
    }
)


@dataclass
class ValidationResult:
    """审查计划静态完整性校验报告。"""

    ok: bool
    errors: list[str] = field(default_factory=list)
    batch_count: int = 0
    covered_src_modules: int = 0
    total_src_modules: int = 0
    split_files_verified: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class PreparedTaskInfo:
    """prepare_batch 生成产物摘要。"""

    batch_id: str
    output_path: Path
    whitelist_lines: int
    task_card_tokens: int
    target_code_tokens: int
    total_initial_tokens: int


# ---------------------------------------------------------------------------
# 基础 I/O、哈希、时间与 Token 估算工具
# ---------------------------------------------------------------------------


def _configure_utf8_stdio() -> None:
    """确保 Windows 终端下 UTF-8 标准输出/错误流不因 GBK 编码崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def now_iso_utc8() -> str:
    """返回固定 +08:00 时区的秒级 ISO-8601 时间戳。"""
    return datetime.now(TZ_UTC8).isoformat(timespec="seconds")


def atomic_write_text(path: Path, content: str) -> None:
    """使用同目录 pid+uuid 临时文件与 os.replace 原子写入 UTF-8 文本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def sha256_file(path: Path) -> str:
    """计算文件内容 SHA256 十六进制摘要。"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def estimate_tokens(text: str) -> int:
    """CJK 感知的确定性离线 Token 估算器。"""
    if not text:
        return 0
    cjk_chars = 0
    other_chars = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3000 <= cp <= 0x303F
            or 0xFF00 <= cp <= 0xFFEF
        ):
            cjk_chars += 1
        else:
            other_chars += 1
    return max(1, round(cjk_chars * 1.3 + other_chars * 0.28))


def _resolve_safe_repo_path(repo_root: Path, raw_rel_path: str) -> Path | None:
    """将配置相对路径解析至 repo_root 内，若绝对路径、带盘符/根锚点或逃逸出仓库则返回 None。"""
    if not isinstance(raw_rel_path, str) or not raw_rel_path.strip():
        return None
    p = Path(raw_rel_path)
    if p.is_absolute() or p.drive or p.root or p.anchor:
        return None
    root_resolved = repo_root.resolve()
    try:
        resolved = (root_resolved / p).resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root_resolved):
        return None
    return resolved


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _plan_path(repo_root: Path) -> Path:
    return repo_root / "config" / "review_plan.yaml"


def _brief_path(repo_root: Path) -> Path:
    return repo_root / ".agents" / "rules" / "external_review_brief.md"


def _schema_path(repo_root: Path) -> Path:
    return repo_root / "src" / "shuati" / "db" / "schema.py"


def _external_review_dir(repo_root: Path) -> Path:
    return repo_root / ".agents" / "teamwork" / "external_review"


def load_plan_data(repo_root: Path) -> dict[str, Any]:
    """加载并做基础顶层映射检查。"""
    p = _plan_path(repo_root)
    if not p.is_file():
        raise FileNotFoundError(f"未找到审查计划文件: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"审查计划 YAML 语法错误 ({p}): {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"审查计划顶层必须为 YAML 映射: {p}")
    return raw


def _validate_ranges_pairs(ranges: Any, context_label: str, line_limit: int | None = None) -> list[tuple[int, int]]:
    """严格校验 ranges 为非空 [start, end] 整数对列表，显式拒绝 bool 与反转/越界区间。"""
    if not isinstance(ranges, list) or not ranges:
        raise ValueError(f"{context_label} ranges 必须为非空区间列表")
    validated: list[tuple[int, int]] = []
    for r_idx, pair in enumerate(ranges):
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or isinstance(pair[0], bool)
            or isinstance(pair[1], bool)
            or not isinstance(pair[0], int)
            or not isinstance(pair[1], int)
        ):
            raise ValueError(f"{context_label} ranges[{r_idx}] 必须为严格的 [start, end] 整数对（拒绝 bool）")
        s_i, e_i = pair[0], pair[1]
        if s_i < 1 or e_i < s_i:
            raise ValueError(f"{context_label} ranges[{r_idx}]=[{s_i}, {e_i}] 区间非法")
        if line_limit is not None and e_i > line_limit:
            raise ValueError(
                f"{context_label} ranges[{r_idx}]=[{s_i}, {e_i}] 越界（1..{line_limit}）"
            )
        validated.append((s_i, e_i))
    return validated


def _require_valid_plan_batches(data: dict[str, Any]) -> list[dict[str, Any]]:
    """校验计划顶层与各批次字段类型，防止畸形配置引发 TypeError/AttributeError 或静默放行。"""
    if data.get("schema_version") != 1:
        raise ValueError(f"schema_version 必须为 1，当前为: {data.get('schema_version')!r}")
    batches = data.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ValueError("batches 必须为非空列表")

    seen_ids: set[str] = set()
    for idx, batch in enumerate(batches):
        if not isinstance(batch, dict):
            raise ValueError(f"batches[{idx}] 必须为字典")
        bid = batch.get("id")
        if not isinstance(bid, str) or not _BATCH_ID_RE.match(bid):
            raise ValueError(f"batches[{idx}] id 非法: {bid!r}")
        if bid in seen_ids:
            raise ValueError(f"重复的批次 ID: {bid}")
        seen_ids.add(bid)

        tier = batch.get("tier")
        if isinstance(tier, bool) or not isinstance(tier, int) or tier not in VALID_TIERS:
            raise ValueError(f"批次 {bid} tier 非法: {tier!r}（允许整数 1..4）")

        title = batch.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"批次 {bid} title 必须为非空字符串")

        status = batch.get("status", "PENDING")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise ValueError(f"批次 {bid} status 非法: {status!r}")

        if not isinstance(batch.get("is_cross_review", False), bool):
            raise ValueError(f"批次 {bid} is_cross_review 必须为布尔值")
        if not isinstance(batch.get("allow_ro_db_probe", False), bool):
            raise ValueError(f"批次 {bid} allow_ro_db_probe 必须为布尔值")

        targets = batch.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError(f"批次 {bid} targets 必须为非空字典列表")
        for t_idx, target in enumerate(targets):
            if not isinstance(target, dict):
                raise ValueError(f"批次 {bid} targets[{t_idx}] 必须为字典")
            raw_path = target.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError(f"批次 {bid} targets[{t_idx}] 缺少合法字符串 path")
            if target.get("ranges") is not None:
                _validate_ranges_pairs(target["ranges"], f"批次 {bid} 目标 {raw_path}")

        tables = batch.get("tables", [])
        if not isinstance(tables, list) or not all(isinstance(tb, str) for tb in tables):
            raise ValueError(f"批次 {bid} tables 必须为字符串列表")

        focus_points = batch.get("focus_points")
        if (
            not isinstance(focus_points, list)
            or not focus_points
            or not all(isinstance(fp, str) and fp.strip() for fp in focus_points)
        ):
            raise ValueError(f"批次 {bid} focus_points 必须为非空字符串列表")

        reviewed_by = batch.get("reviewed_by_models", [])
        if not isinstance(reviewed_by, list) or not all(isinstance(m, str) for m in reviewed_by):
            raise ValueError(f"批次 {bid} reviewed_by_models 必须为字符串列表")

        reviewed_at = batch.get("reviewed_at")
        if reviewed_at is not None and not isinstance(reviewed_at, str):
            raise ValueError(f"批次 {bid} reviewed_at 必须为 null 或字符串")

        sha_map = batch.get("target_sha256", {})
        if not isinstance(sha_map, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in sha_map.items()
        ):
            raise ValueError(f"批次 {bid} target_sha256 必须为字符串键值映射")

        notes = batch.get("notes", "")
        if notes is not None and not isinstance(notes, str):
            raise ValueError(f"批次 {bid} notes 必须为字符串")

    return batches


def save_plan_data(repo_root: Path, data: dict[str, Any]) -> None:
    """原子保存 config/review_plan.yaml。"""
    dumped = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
    atomic_write_text(_plan_path(repo_root), dumped)


# ---------------------------------------------------------------------------
# SQLite DDL AST 精准抽取器
# ---------------------------------------------------------------------------


def _extract_raw_ddl_list(schema_py_path: Path) -> list[str]:
    """通过 ast.parse 从 schema.py 定位顶层 DDL_STATEMENTS 字符串常量列表（严禁 import/exec）。"""
    if not schema_py_path.is_file():
        raise FileNotFoundError(f"未找到 schema.py: {schema_py_path}")
    source = schema_py_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(schema_py_path))

    for stmt in tree.body:
        value_node: ast.AST | None = None
        if isinstance(stmt, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "DDL_STATEMENTS" for t in stmt.targets):
                value_node = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "DDL_STATEMENTS":
                value_node = stmt.value

        if value_node is not None:
            if not isinstance(value_node, (ast.List, ast.Tuple)):
                raise ValueError("schema.py 中 DDL_STATEMENTS 必须为字符串列表或元组字面量")
            statements: list[str] = []
            for elt in value_node.elts:
                if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                    raise ValueError("DDL_STATEMENTS 包含非字符串字面量元素")
                statements.append(elt.value)
            return statements

    raise ValueError(f"在 {schema_py_path} 中未找到顶层 DDL_STATEMENTS 定义")


def _extract_matched_sql_ident(m: re.Match[str]) -> str:
    return m.group("dq") or m.group("bt") or m.group("br") or m.group("bare")


def _classify_ddl_statement(stmt: str) -> tuple[str, str] | None:
    """剥离前置 SQL 行/块注释后识别 DDL 语句类型与归属表名，返回 ('table' | 'index', table_name) 或 None。"""
    stripped = _LEADING_SQL_TRIVIA_RE.sub("", stmt)
    if not stripped:
        return None

    m_tbl = _CREATE_TABLE_RE.match(stripped)
    if m_tbl is not None:
        return ("table", _extract_matched_sql_ident(m_tbl))

    m_idx = _CREATE_INDEX_RE.match(stripped)
    if m_idx is not None:
        return ("index", _extract_matched_sql_ident(m_idx))

    return None


def list_schema_tables(schema_py_path: Path) -> set[str]:
    """提取 schema.py 中声明的全部实表与虚表名集合（统一归一化为小写）。"""
    tables: set[str] = set()
    for stmt in _extract_raw_ddl_list(schema_py_path):
        classified = _classify_ddl_statement(stmt)
        if classified is not None and classified[0] == "table":
            tables.add(classified[1].lower())
    return tables


def extract_ddl_for_tables(schema_py_path: Path, table_names: Sequence[str]) -> str:
    """按原声明顺序提取指定表的 CREATE [VIRTUAL] TABLE 与关联 CREATE [UNIQUE] INDEX 语句（表名大小写不敏感）。"""
    if not table_names:
        return ""

    requested_ordered: list[str] = []
    requested_lower_set: set[str] = set()
    for name in table_names:
        low = name.lower()
        if low not in requested_lower_set:
            requested_lower_set.add(low)
            requested_ordered.append(name)

    raw_statements = _extract_raw_ddl_list(schema_py_path)
    found_tables_lower: set[str] = set()
    matched_blocks: list[str] = []

    for raw_stmt in raw_statements:
        classified = _classify_ddl_statement(raw_stmt)
        if classified is None:
            continue
        kind, tbl_name = classified
        tbl_low = tbl_name.lower()
        if tbl_low in requested_lower_set:
            if kind == "table":
                found_tables_lower.add(tbl_low)
            cleaned = textwrap.dedent(raw_stmt).strip()
            if cleaned:
                matched_blocks.append(cleaned)

    missing = [t for t in requested_ordered if t.lower() not in found_tables_lower]
    if missing:
        raise ValueError(f"schema.py 中未找到以下表的 CREATE TABLE 定义: {', '.join(missing)}")

    return "\n\n".join(matched_blocks)


# ---------------------------------------------------------------------------
# 计划完整性校验器 (validate)
# ---------------------------------------------------------------------------


def _format_missing_lines(missing_sorted: list[int]) -> str:
    """将缺失行号数组压缩为紧凑区间字符串（如 L103-L110, L205）。"""
    if not missing_sorted:
        return ""
    spans: list[str] = []
    start = prev = missing_sorted[0]
    for ln in missing_sorted[1:]:
        if ln == prev + 1:
            prev = ln
        else:
            spans.append(f"L{start}-L{prev}" if start != prev else f"L{start}")
            start = prev = ln
    spans.append(f"L{start}-L{prev}" if start != prev else f"L{start}")
    return ", ".join(spans)


def validate_plan(repo_root: Path) -> ValidationResult:
    """全面校验 config/review_plan.yaml 的结构、路径安全、切片无缝覆盖及表名合法性。"""
    errors: list[str] = []
    root_resolved = repo_root.resolve()

    plan_file = _plan_path(root_resolved)
    brief_file = _brief_path(root_resolved)
    schema_file = _schema_path(root_resolved)

    if not plan_file.is_file():
        errors.append(f"缺少审查计划文件: {plan_file}")
    if not brief_file.is_file():
        errors.append(f"缺少审查速查卡文件: {brief_file}")
    if not schema_file.is_file():
        errors.append(f"缺少数据库架构文件: {schema_file}")

    if errors:
        return ValidationResult(ok=False, errors=errors)

    try:
        known_tables = list_schema_tables(schema_file)
    except Exception as exc:
        return ValidationResult(ok=False, errors=[f"解析 schema.py 失败: {exc}"])

    try:
        data = load_plan_data(root_resolved)
    except Exception as exc:
        return ValidationResult(ok=False, errors=[f"加载 review_plan.yaml 失败: {exc}"])

    if data.get("schema_version") != 1:
        errors.append(f"schema_version 必须为 1，当前为: {data.get('schema_version')!r}")

    batches = data.get("batches")
    if not isinstance(batches, list) or not batches:
        errors.append("batches 必须为非空列表")
        return ValidationResult(ok=False, errors=errors)

    seen_ids: set[str] = set()
    # 记录每个仓库相对路径的文件总行数、是否存在全文覆盖、以及各切片行集合
    file_line_counts: dict[str, int] = {}
    file_has_full_coverage: dict[str, bool] = {}
    file_covered_lines: dict[str, set[int]] = {}

    for idx, batch in enumerate(batches):
        if not isinstance(batch, dict):
            errors.append(f"batches[{idx}] 必须为字典")
            continue

        bid = batch.get("id")
        if not isinstance(bid, str) or not _BATCH_ID_RE.match(bid):
            errors.append(f"batches[{idx}] id 非法: {bid!r}（期望格式如 B01）")
            bid_label = f"#{idx}"
        else:
            bid_label = bid
            if bid in seen_ids:
                errors.append(f"重复的批次 ID: {bid}")
            seen_ids.add(bid)

        tier = batch.get("tier")
        if isinstance(tier, bool) or not isinstance(tier, int) or tier not in VALID_TIERS:
            errors.append(f"批次 {bid_label} tier 非法: {tier!r}（允许 1..4）")

        status = batch.get("status")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            errors.append(f"批次 {bid_label} status 非法: {status!r}（允许 {sorted(VALID_STATUSES)}）")

        title = batch.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"批次 {bid_label} title 不能为空")

        if not isinstance(batch.get("is_cross_review"), bool):
            errors.append(f"批次 {bid_label} is_cross_review 必须为布尔值")

        if not isinstance(batch.get("allow_ro_db_probe"), bool):
            errors.append(f"批次 {bid_label} allow_ro_db_probe 必须为布尔值")

        focus_points = batch.get("focus_points")
        if (
            not isinstance(focus_points, list)
            or not focus_points
            or not all(isinstance(fp, str) and fp.strip() for fp in focus_points)
        ):
            errors.append(f"批次 {bid_label} focus_points 必须为非空字符串列表")

        reviewed_by = batch.get("reviewed_by_models")
        if not isinstance(reviewed_by, list) or not all(isinstance(m, str) for m in reviewed_by):
            errors.append(f"批次 {bid_label} reviewed_by_models 必须为字符串列表")

        reviewed_at = batch.get("reviewed_at")
        if reviewed_at is not None and not isinstance(reviewed_at, str):
            errors.append(f"批次 {bid_label} reviewed_at 必须为 null 或字符串")

        sha_map = batch.get("target_sha256")
        if not isinstance(sha_map, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in sha_map.items()
        ):
            errors.append(f"批次 {bid_label} target_sha256 必须为字符串键值映射")

        if not isinstance(batch.get("notes", ""), str):
            errors.append(f"批次 {bid_label} notes 必须为字符串")

        # 校验关联表名
        tables = batch.get("tables")
        if not isinstance(tables, list) or not all(isinstance(t, str) for t in tables):
            errors.append(f"批次 {bid_label} tables 必须为表名字符串列表")
        else:
            for tbl in tables:
                if tbl.lower() not in known_tables:
                    errors.append(f"批次 {bid_label} 引用了 schema.py 中不存在的表名: {tbl!r}")

        # 校验目标文件与行号区间
        targets = batch.get("targets")
        if not isinstance(targets, list) or not targets:
            errors.append(f"批次 {bid_label} targets 必须为非空列表")
            continue

        for t_idx, target in enumerate(targets):
            if not isinstance(target, dict):
                errors.append(f"批次 {bid_label} targets[{t_idx}] 必须为字典")
                continue

            raw_path = target.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                errors.append(f"批次 {bid_label} targets[{t_idx}] 缺少有效 path")
                continue

            resolved = _resolve_safe_repo_path(root_resolved, raw_path)
            if resolved is None:
                errors.append(f"批次 {bid_label} 目标路径越界或非法: {raw_path!r}")
                continue
            if not resolved.is_file():
                errors.append(f"批次 {bid_label} 目标文件不存在: {raw_path!r}")
                continue

            rel_posix = resolved.relative_to(root_resolved).as_posix()
            if rel_posix not in file_line_counts:
                file_line_counts[rel_posix] = len(_read_lines(resolved))
                file_has_full_coverage[rel_posix] = False
                file_covered_lines[rel_posix] = set()

            line_count = file_line_counts[rel_posix]
            ranges = target.get("ranges")
            if ranges is None:
                file_has_full_coverage[rel_posix] = True
                file_covered_lines[rel_posix].update(range(1, line_count + 1))
            else:
                if not isinstance(ranges, list) or not ranges:
                    errors.append(
                        f"批次 {bid_label} 目标 {rel_posix} 的 ranges 必须为非空区间列表"
                    )
                    continue
                for r_idx, pair in enumerate(ranges):
                    if (
                        not isinstance(pair, (list, tuple))
                        or len(pair) != 2
                        or isinstance(pair[0], bool)
                        or isinstance(pair[1], bool)
                        or not isinstance(pair[0], int)
                        or not isinstance(pair[1], int)
                    ):
                        errors.append(
                            f"批次 {bid_label} 目标 {rel_posix} ranges[{r_idx}] 必须为 [start, end] 整数对"
                        )
                        continue
                    start_ln, end_ln = pair[0], pair[1]
                    if not (1 <= start_ln <= end_ln <= line_count):
                        errors.append(
                            f"批次 {bid_label} 目标 {rel_posix} ranges[{r_idx}]=[{start_ln}, {end_ln}] 越界"
                            f"（有效行范围 1..{line_count}）"
                        )
                        continue
                    file_covered_lines[rel_posix].update(range(start_ln, end_ln + 1))

    # 校验 src/shuati 下所有非 __init__.py 模块 100% 纳入计划
    src_shuati_dir = root_resolved / "src" / "shuati"
    all_src_modules: list[str] = []
    if src_shuati_dir.is_dir():
        for py_file in sorted(src_shuati_dir.rglob("*.py")):
            if py_file.name == "__init__.py":
                continue
            all_src_modules.append(py_file.relative_to(root_resolved).as_posix())

    covered_src_count = 0
    for mod_rel in all_src_modules:
        if mod_rel in file_covered_lines:
            covered_src_count += 1
        else:
            errors.append(f"src/shuati 业务模块未被任何审查批次覆盖: {mod_rel}")

    # 校验所有仅通过切片 (ranges) 覆盖的文件必须达到 1..N 零空洞并集覆盖
    split_verified: list[str] = []
    for rel_posix, has_full in sorted(file_has_full_coverage.items()):
        if has_full:
            continue
        expected_lines = set(range(1, file_line_counts[rel_posix] + 1))
        actual_lines = file_covered_lines[rel_posix]
        missing_lines = sorted(expected_lines - actual_lines)
        if missing_lines:
            errors.append(
                f"切片文件存在漏审行空洞 {rel_posix}: 缺失 {_format_missing_lines(missing_lines)}"
            )
        else:
            split_verified.append(rel_posix)

    return ValidationResult(
        ok=len(errors) == 0,
        errors=errors,
        batch_count=len(batches),
        covered_src_modules=covered_src_count,
        total_src_modules=len(all_src_modules),
        split_files_verified=split_verified,
    )


# ---------------------------------------------------------------------------
# 跨文件及同文件切片外 AST 依赖 Stub 抽取器
# ---------------------------------------------------------------------------


@dataclass
class _ParsedModuleCacheItem:
    abs_path: Path
    rel_posix: str
    tree: ast.Module
    defs_by_name: dict[str, ast.AST]
    reexports_by_name: dict[str, tuple[list[str], str]]


def _module_pkg_parts(repo_root: Path, file_path: Path) -> list[str] | None:
    """计算 src/shuati/... 文件所属的包路径分段（如 ['shuati', 'pipeline']）。"""
    src_root = (repo_root / "src").resolve()
    file_resolved = file_path.resolve()
    if not file_resolved.is_relative_to(src_root):
        return None
    rel = file_resolved.relative_to(src_root)
    return list(rel.parent.parts)


def _resolve_import_from_parts(
    pkg_parts: list[str] | None, module: str | None, level: int
) -> list[str] | None:
    """将绝对或相对 ImportFrom 解析为 ['shuati', ...] 模块分段；外部第三方包返回 None。"""
    if level > 0:
        if pkg_parts is None:
            return None
        trim = level - 1
        if trim > len(pkg_parts):
            return None
        base = pkg_parts[: len(pkg_parts) - trim]
        extra = module.split(".") if module else []
        full = base + extra
        return full if full and full[0] == "shuati" else None

    if not module:
        return None
    if module == "shuati" or module.startswith("shuati."):
        return module.split(".")
    return None


def _locate_module_file(repo_root: Path, mod_parts: Sequence[str]) -> Path | None:
    """在 repo_root/src 下定位模块分段对应的 .py 或包 __init__.py 文件。"""
    if not mod_parts:
        return None
    src_root = repo_root / "src"
    py_candidate = src_root.joinpath(*mod_parts[:-1], f"{mod_parts[-1]}.py")
    if py_candidate.is_file():
        return py_candidate.resolve()
    init_candidate = src_root.joinpath(*mod_parts, "__init__.py")
    if init_candidate.is_file():
        return init_candidate.resolve()
    return None


def _is_type_checking_guard(test_expr: ast.expr) -> bool:
    if isinstance(test_expr, ast.Name) and test_expr.id == "TYPE_CHECKING":
        return True
    if isinstance(test_expr, ast.Attribute) and test_expr.attr == "TYPE_CHECKING":
        return True
    return False


def _collect_module_level_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """提取模块顶层导入，包含顶层 if TYPE_CHECKING: 分支内的类型导入。"""
    collected: list[ast.Import | ast.ImportFrom] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            collected.append(stmt)
        elif isinstance(stmt, ast.If) and _is_type_checking_guard(stmt.test):
            for sub in stmt.body:
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    collected.append(sub)
    return collected


def _parse_module_cached(
    repo_root: Path, abs_path: Path, cache: dict[Path, _ParsedModuleCacheItem | None]
) -> _ParsedModuleCacheItem | None:
    resolved = abs_path.resolve()
    if resolved in cache:
        return cache[resolved]
    if not resolved.is_file() or resolved.suffix != ".py":
        cache[resolved] = None
        return None
    try:
        tree = ast.parse(resolved.read_text(encoding="utf-8"), filename=str(resolved))
    except SyntaxError:
        cache[resolved] = None
        return None

    defs_by_name: dict[str, ast.AST] = {}
    reexports_by_name: dict[str, tuple[list[str], str]] = {}
    pkg_parts = _module_pkg_parts(repo_root, resolved)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs_by_name[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defs_by_name[t.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defs_by_name[node.target.id] = node
        elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):  # Python 3.12+
            defs_by_name[node.name.id] = node

    for imp_node in _collect_module_level_imports(tree):
        if isinstance(imp_node, ast.ImportFrom):
            mod_parts = _resolve_import_from_parts(pkg_parts, imp_node.module, imp_node.level)
            if mod_parts is not None:
                for alias in imp_node.names:
                    if alias.name != "*":
                        local_nm = alias.asname or alias.name
                        reexports_by_name[local_nm] = (mod_parts, alias.name)

    rel_posix = resolved.relative_to(repo_root.resolve()).as_posix()
    item = _ParsedModuleCacheItem(
        abs_path=resolved,
        rel_posix=rel_posix,
        tree=tree,
        defs_by_name=defs_by_name,
        reexports_by_name=reexports_by_name,
    )
    cache[resolved] = item
    return item


def _resolve_symbol_in_module(
    repo_root: Path,
    mod_file: Path,
    symbol_name: str,
    cache: dict[Path, _ParsedModuleCacheItem | None],
    depth: int = 0,
) -> tuple[_ParsedModuleCacheItem, ast.AST] | None:
    """在目标模块查找顶层符号定义，支持 __init__.py 等单跳/双跳重导出追踪。"""
    if depth > 3:
        return None
    parsed = _parse_module_cached(repo_root, mod_file, cache)
    if parsed is None:
        return None
    if symbol_name in parsed.defs_by_name:
        return (parsed, parsed.defs_by_name[symbol_name])
    if symbol_name in parsed.reexports_by_name:
        next_mod_parts, orig_name = parsed.reexports_by_name[symbol_name]
        next_file = _locate_module_file(repo_root, next_mod_parts)
        if next_file is not None and next_file != parsed.abs_path:
            return _resolve_symbol_in_module(repo_root, next_file, orig_name, cache, depth + 1)
    return None


def _one_line_doc(node: ast.AST) -> str | None:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return None
    doc = ast.get_docstring(node)
    if not doc:
        return None
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:96] + ("..." if len(stripped) > 96 else "")
    return None


def _format_docstring_literal(doc_line: str) -> str:
    """转义反斜杠与三引号并隔离末尾双引号，确保渲染出的三引号字符串 100% 通过 ast.parse。"""
    escaped = doc_line.replace("\\", "\\\\").replace('"""', "'''")
    if escaped.endswith('"'):
        escaped += " "
    return f'"""{escaped}"""'


def _extract_semantic_decorators(decorator_list: Sequence[ast.expr], indent: str = "") -> list[str]:
    """保留影响调用契约的关键装饰器（@dataclass、@contextmanager、@classmethod 等）。"""
    out: list[str] = []
    for dec in decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        dec_name = ""
        if isinstance(target, ast.Name):
            dec_name = target.id
        elif isinstance(target, ast.Attribute):
            dec_name = target.attr
        if dec_name in _SEMANTIC_DECORATORS:
            out.append(f"{indent}@{ast.unparse(dec)}")
    return out


def _compact_expr(node: ast.AST | None, max_len: int = 72) -> str:
    if node is None:
        return "..."
    text = ast.unparse(node)
    if "\n" in text or len(text) > max_len:
        return "..."
    return text


def _render_function_stub(
    node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str = "", include_doc: bool = True
) -> str:
    dec_lines = _extract_semantic_decorators(node.decorator_list, indent=indent)
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    args_str = ast.unparse(node.args)
    ret_str = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    doc_line = _one_line_doc(node) if include_doc else None
    if doc_line:
        sig_lines = [
            *dec_lines,
            f"{indent}{prefix}{node.name}({args_str}){ret_str}:",
            f"{indent}    {_format_docstring_literal(doc_line)}",
            f"{indent}    ...",
        ]
        return "\n".join(sig_lines)
    return "\n".join([*dec_lines, f"{indent}{prefix}{node.name}({args_str}){ret_str}: ..."])


def _render_node_stub(node: ast.AST, referenced_attrs: set[str] | None = None) -> str:
    """将 AST 顶层节点渲染为紧凑且语法合法的 Python 签名 Stub。"""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _render_function_stub(node, indent="", include_doc=True)

    if isinstance(node, ast.ClassDef):
        dec_lines = _extract_semantic_decorators(node.decorator_list, indent="")
        bases = [ast.unparse(b) for b in node.bases]
        bases_str = f"({', '.join(bases)})" if bases else ""
        lines: list[str] = [*dec_lines, f"class {node.name}{bases_str}:"]
        doc_line = _one_line_doc(node)
        if doc_line:
            lines.append(f"    {_format_docstring_literal(doc_line)}")

        body_added = 0
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                ann = ast.unparse(item.annotation)
                if item.value is not None:
                    lines.append(f"    {item.target.id}: {ann} = {_compact_expr(item.value, 48)}")
                else:
                    lines.append(f"    {item.target.id}: {ann}")
                body_added += 1
            elif isinstance(item, ast.Assign):
                names = [
                    t.id for t in item.targets if isinstance(t, ast.Name) and not t.id.startswith("_")
                ]
                if names:
                    lines.append(f"    {', '.join(names)} = {_compact_expr(item.value, 48)}")
                    body_added += 1
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_protocol_dunder = item.name in _PROTOCOL_DUNDERS
                is_public = not item.name.startswith("_")
                is_ref = referenced_attrs is not None and item.name in referenced_attrs
                if is_protocol_dunder or is_public or is_ref:
                    lines.append(_render_function_stub(item, indent="    ", include_doc=False))
                    body_added += 1
        if body_added == 0 and not doc_line:
            lines.append("    ...")
        return "\n".join(lines)

    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        ann = ast.unparse(node.annotation)
        val = f" = {_compact_expr(node.value)}" if node.value is not None else ""
        return f"{node.target.id}: {ann}{val}"

    if isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        lhs = ", ".join(names) if names else ast.unparse(node.targets[0])
        return f"{lhs} = {_compact_expr(node.value)}"

    return ast.unparse(node).splitlines()[0]


def _dotted_attr_chain(node: ast.Attribute) -> list[str] | None:
    parts: list[str] = [node.attr]
    curr: ast.AST = node.value
    while isinstance(curr, ast.Attribute):
        parts.append(curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        parts.append(curr.id)
        parts.reverse()
        return parts
    return None


def extract_dependency_stubs(repo_root: Path, targets: Sequence[dict[str, Any]]) -> str:
    """扫描目标 .py 切片使用的跨模块 shuati.* 依赖及同文件切片外辅助函数，生成紧凑 AST Stub。"""
    root_resolved = repo_root.resolve()
    cache: dict[Path, _ParsedModuleCacheItem | None] = {}

    # 1. 严格校验目标列表与 ranges，并计算当前批次内每个 .py 文件已覆盖的行号集合
    covered_lines_map: dict[Path, set[int]] = {}
    ordered_py_targets: list[Path] = []

    for idx, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ValueError(f"targets[{idx}] 必须为字典")
        raw_path = target.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"targets[{idx}] 缺少有效字符串 path")
        resolved = _resolve_safe_repo_path(root_resolved, raw_path)
        if resolved is None:
            raise ValueError(f"目标路径越界或非法: {raw_path!r}")
        ranges = target.get("ranges")
        if not resolved.is_file():
            if ranges is not None:
                _validate_ranges_pairs(ranges, f"目标 {raw_path}")
            continue
        line_total = len(_read_lines(resolved))
        validated_ranges = (
            _validate_ranges_pairs(ranges, f"目标 {raw_path}", line_limit=line_total)
            if ranges is not None
            else None
        )
        if resolved.suffix != ".py":
            continue
        if resolved not in covered_lines_map:
            covered_lines_map[resolved] = set()
            ordered_py_targets.append(resolved)

        if validated_ranges is None:
            covered_lines_map[resolved].update(range(1, line_total + 1))
        else:
            for start_ln, end_ln in validated_ranges:
                covered_lines_map[resolved].update(range(start_ln, end_ln + 1))

    def _is_wholly_covered(file_path: Path, def_node: ast.AST) -> bool:
        covered = covered_lines_map.get(file_path)
        if not covered:
            return False
        lineno = getattr(def_node, "lineno", None)
        end_lineno = getattr(def_node, "end_lineno", lineno)
        if lineno is None or end_lineno is None:
            return False
        return set(range(lineno, end_lineno + 1)).issubset(covered)

    # 收集结果：(file_path, symbol_name) -> (lineno, stub_node, attr_refs)
    intra_file_stubs: dict[Path, dict[str, tuple[int, ast.AST, set[str]]]] = {}
    cross_file_stubs: dict[Path, dict[str, tuple[int, ast.AST, set[str]]]] = {}

    def _record_stub(
        bucket: dict[Path, dict[str, tuple[int, ast.AST, set[str]]]],
        dep_file: Path,
        sym_name: str,
        def_node: ast.AST,
        extra_attrs: set[str] | None = None,
    ) -> None:
        if _is_wholly_covered(dep_file, def_node):
            return
        per_file = bucket.setdefault(dep_file, {})
        lineno = getattr(def_node, "lineno", 0)
        if sym_name in per_file:
            if extra_attrs:
                per_file[sym_name][2].update(extra_attrs)
        else:
            per_file[sym_name] = (lineno, def_node, set(extra_attrs or ()))

    # 2. 扫描每个目标文件的已覆盖行引用与导入绑定
    for py_path in ordered_py_targets:
        parsed = _parse_module_cached(root_resolved, py_path, cache)
        if parsed is None:
            continue
        covered_lines = covered_lines_map[py_path]
        line_total = len(_read_lines(py_path))
        is_partial_slice = len(covered_lines) < line_total

        used_names: set[str] = set()
        used_attrs: dict[str, set[str]] = {}
        used_dotted_chains: list[list[str]] = []

        for node in ast.walk(parsed.tree):
            lineno = getattr(node, "lineno", None)
            if lineno is None or lineno not in covered_lines:
                continue
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name):
                    used_attrs.setdefault(node.value.id, set()).add(node.attr)
                chain = _dotted_attr_chain(node)
                if chain is not None and len(chain) >= 2:
                    used_dotted_chains.append(chain)

        # 2a. 若当前文件是局部行号切片，提取同文件切片外被引用的顶层辅助定义
        if is_partial_slice:
            for def_name, def_node in parsed.defs_by_name.items():
                if def_name in used_names:
                    _record_stub(
                        intra_file_stubs,
                        py_path,
                        def_name,
                        def_node,
                        used_attrs.get(def_name),
                    )

        # 2b. 解析顶层导入（含顶层 if TYPE_CHECKING:）与白名单已覆盖行内的局部延迟导入
        pkg_parts = _module_pkg_parts(root_resolved, py_path)
        top_imports = _collect_module_level_imports(parsed.tree)
        top_set = {id(n) for n in top_imports}
        inner_imports = [
            n
            for n in ast.walk(parsed.tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            and id(n) not in top_set
            and getattr(n, "lineno", 0) in covered_lines
        ]
        for node in [*top_imports, *inner_imports]:
            if isinstance(node, ast.ImportFrom):
                mod_parts = _resolve_import_from_parts(pkg_parts, node.module, node.level)
                if mod_parts is None:
                    continue
                for alias in node.names:
                    orig_name = alias.name
                    local_name = alias.asname or orig_name
                    if orig_name == "*":
                        mod_file = _locate_module_file(root_resolved, mod_parts)
                        if mod_file is not None:
                            dep_parsed = _parse_module_cached(root_resolved, mod_file, cache)
                            if dep_parsed is not None:
                                for cand_name, cand_node in dep_parsed.defs_by_name.items():
                                    if cand_name in used_names:
                                        target_bucket = (
                                            intra_file_stubs
                                            if dep_parsed.abs_path == py_path
                                            else cross_file_stubs
                                        )
                                        _record_stub(
                                            target_bucket,
                                            dep_parsed.abs_path,
                                            cand_name,
                                            cand_node,
                                            used_attrs.get(cand_name),
                                        )
                        continue

                    # 情况 A：from shuati.x import y，其中 y 是子模块文件 y.py，调用形式为 y.func(...)
                    submod_file = _locate_module_file(root_resolved, [*mod_parts, orig_name])
                    if submod_file is not None and local_name in used_attrs:
                        for attr_nm in sorted(used_attrs[local_name]):
                            resolved_sym = _resolve_symbol_in_module(
                                root_resolved, submod_file, attr_nm, cache
                            )
                            if resolved_sym is not None:
                                dep_mod, def_node = resolved_sym
                                target_bucket = (
                                    intra_file_stubs
                                    if dep_mod.abs_path == py_path
                                    else cross_file_stubs
                                )
                                _record_stub(target_bucket, dep_mod.abs_path, attr_nm, def_node)

                    # 情况 B：from shuati.x.y import sym，其中 sym 是函数/类/常量
                    if local_name in used_names:
                        mod_file = _locate_module_file(root_resolved, mod_parts)
                        if mod_file is not None:
                            resolved_sym = _resolve_symbol_in_module(
                                root_resolved, mod_file, orig_name, cache
                            )
                            if resolved_sym is not None:
                                dep_mod, def_node = resolved_sym
                                target_bucket = (
                                    intra_file_stubs
                                    if dep_mod.abs_path == py_path
                                    else cross_file_stubs
                                )
                                _record_stub(
                                    target_bucket,
                                    dep_mod.abs_path,
                                    orig_name,
                                    def_node,
                                    used_attrs.get(local_name),
                                )

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if not (alias.name == "shuati" or alias.name.startswith("shuati.")):
                        continue
                    mod_parts = alias.name.split(".")
                    mod_file = _locate_module_file(root_resolved, mod_parts)
                    if alias.asname:
                        if mod_file is None:
                            continue
                        for attr_nm in sorted(used_attrs.get(alias.asname, set())):
                            resolved_sym = _resolve_symbol_in_module(
                                root_resolved, mod_file, attr_nm, cache
                            )
                            if resolved_sym is not None:
                                dep_mod, def_node = resolved_sym
                                target_bucket = (
                                    intra_file_stubs
                                    if dep_mod.abs_path == py_path
                                    else cross_file_stubs
                                )
                                _record_stub(target_bucket, dep_mod.abs_path, attr_nm, def_node)
                    else:
                        for chain in used_dotted_chains:
                            if chain[: len(mod_parts)] == mod_parts and len(chain) > len(mod_parts):
                                # 先尝试最长模块前缀（如 import shuati.models; shuati.models.ItemDTO）
                                resolved_sym = None
                                deep_mod = _locate_module_file(root_resolved, chain[:-1])
                                if deep_mod is not None:
                                    resolved_sym = _resolve_symbol_in_module(
                                        root_resolved, deep_mod, chain[-1], cache
                                    )
                                if resolved_sym is None and mod_file is not None:
                                    resolved_sym = _resolve_symbol_in_module(
                                        root_resolved, mod_file, chain[len(mod_parts)], cache
                                    )
                                if resolved_sym is not None:
                                    dep_mod, def_node = resolved_sym
                                    sym_key = getattr(def_node, "name", chain[-1])
                                    if not isinstance(sym_key, str):
                                        sym_key = chain[len(mod_parts)]
                                    target_bucket = (
                                        intra_file_stubs
                                        if dep_mod.abs_path == py_path
                                        else cross_file_stubs
                                    )
                                    _record_stub(
                                        target_bucket, dep_mod.abs_path, sym_key, def_node
                                    )

    # 3. 组装稳定有序的 Stub 输出文本
    sections: list[str] = []

    for py_path in ordered_py_targets:
        entries = intra_file_stubs.get(py_path)
        if not entries:
            continue
        rel_posix = py_path.relative_to(root_resolved).as_posix()
        sorted_items = sorted(entries.values(), key=lambda item: item[0])
        rendered = "\n\n".join(_render_node_stub(node, attrs) for _, node, attrs in sorted_items)
        sections.append(f"# [同文件切片外辅助定义] {rel_posix}\n{rendered}")

    for dep_path in sorted(
        cross_file_stubs.keys(), key=lambda p: p.relative_to(root_resolved).as_posix()
    ):
        entries = cross_file_stubs[dep_path]
        if not entries:
            continue
        rel_posix = dep_path.relative_to(root_resolved).as_posix()
        sorted_items = sorted(entries.values(), key=lambda item: item[0])
        rendered = "\n\n".join(_render_node_stub(node, attrs) for _, node, attrs in sorted_items)
        sections.append(f"# [跨模块依赖签名] {rel_posix}\n{rendered}")

    return "\n\n\n".join(sections)


# ---------------------------------------------------------------------------
# 任务卡生成器 (prepare)
# ---------------------------------------------------------------------------


def _find_batch(data: dict[str, Any], batch_id: str) -> dict[str, Any]:
    batches = _require_valid_plan_batches(data)
    norm_id = batch_id.strip().upper()
    for batch in batches:
        if batch["id"].upper() == norm_id:
            return batch
    raise KeyError(f"未找到批次 ID: {batch_id!r}")


def prepare_batch(
    repo_root: Path, batch_id: str, out_path: Path | None = None
) -> PreparedTaskInfo:
    """构建指定批次的自包含审查任务卡 (<ID>_task.md) 并原子落盘。"""
    root_resolved = repo_root.resolve()
    brief_file = _brief_path(root_resolved)
    schema_file = _schema_path(root_resolved)
    if not brief_file.is_file():
        raise FileNotFoundError(f"缺少审查速查卡文件: {brief_file}")
    if not schema_file.is_file():
        raise FileNotFoundError(f"缺少数据库架构文件: {schema_file}")

    data = load_plan_data(root_resolved)
    batch = _find_batch(data, batch_id)
    bid: str = batch["id"]
    title: str = batch["title"]
    tier: int = int(batch["tier"])
    is_cross_review: bool = bool(batch.get("is_cross_review", False))
    allow_ro_db_probe: bool = bool(batch.get("allow_ro_db_probe", False))
    reviewed_by_models: list[str] = list(batch.get("reviewed_by_models") or [])
    prior_note: str = str(batch.get("prior_review_note") or "").strip()

    targets = batch.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError(f"批次 {bid} targets 不能为空")

    # 统计白名单读取范围与源码切片总文本（用于精确估算首轮上下文 Token）
    whitelist_bullets: list[str] = []
    sliced_code_chunks: list[str] = []
    total_whitelist_lines = 0

    for target in targets:
        raw_path = target.get("path", "")
        resolved = _resolve_safe_repo_path(root_resolved, raw_path)
        if resolved is None:
            raise ValueError(f"批次 {bid} 目标路径越界或非法: {raw_path!r}")
        if not resolved.is_file():
            raise FileNotFoundError(f"批次 {bid} 目标文件不存在: {raw_path!r}")
        rel_posix = resolved.relative_to(root_resolved).as_posix()
        lines = _read_lines(resolved)
        line_count = len(lines)
        ranges = target.get("ranges")

        if ranges is None:
            total_whitelist_lines += line_count
            whitelist_bullets.append(
                f"- `{rel_posix}`（全文 `L1-L{line_count}`，共 {line_count} 行）"
            )
            sliced_code_chunks.append("\n".join(lines))
        else:
            if not isinstance(ranges, list) or not ranges:
                raise ValueError(f"批次 {bid} 目标 {rel_posix} ranges 必须为非空区间列表")
            range_labels: list[str] = []
            slice_line_sum = 0
            for pair in ranges:
                if (
                    not isinstance(pair, (list, tuple))
                    or len(pair) != 2
                    or isinstance(pair[0], bool)
                    or isinstance(pair[1], bool)
                    or not isinstance(pair[0], int)
                    or not isinstance(pair[1], int)
                ):
                    raise ValueError(f"批次 {bid} 目标 {rel_posix} ranges 元素必须为 [start, end] 整数对")
                s_i, e_i = pair[0], pair[1]
                if not (1 <= s_i <= e_i <= line_count):
                    raise ValueError(
                        f"批次 {bid} 目标 {rel_posix} 行号区间 [{s_i}, {e_i}] 越界（1..{line_count}）"
                    )
                range_labels.append(f"`L{s_i}-L{e_i}`")
                slice_line_sum += e_i - s_i + 1
                sliced_code_chunks.append("\n".join(lines[s_i - 1 : e_i]))
            total_whitelist_lines += slice_line_sum
            whitelist_bullets.append(
                f"- `{rel_posix}`（仅限行号区间 {', '.join(range_labels)}，共 {slice_line_sum} 行；区间外签名见 §5）"
            )

    brief_content = brief_file.read_text(encoding="utf-8").strip()
    ddl_content = extract_ddl_for_tables(schema_file, batch.get("tables") or [])
    stubs_content = extract_dependency_stubs(root_resolved, targets)

    findings_rel_posix = f".agents/teamwork/external_review/{bid}_findings.md"
    target_out = out_path if out_path is not None else (_external_review_dir(root_resolved) / f"{bid}_task.md")

    cross_banner_lines: list[str] = []
    if is_cross_review or reviewed_by_models:
        models_desc = ", ".join(reviewed_by_models) if reviewed_by_models else "首轮模型"
        note_suffix = f"（{prior_note}）" if prior_note else ""
        cross_banner_lines = [
            "",
            f"> **【异构交叉盲审警告】** 本批次标记为交叉复审{note_suffix}，历史已审模型记录：`[{models_desc}]`。",
            "> 请务必切换**不同模型家族**（例如首轮为 Claude/Gemini，则本轮换用 OpenAI Codex/GPT，反之亦然）独立盲审！",
        ]

    if allow_ro_db_probe:
        sqlite_rule = (
            "3. **受控只读探针特许（`allow_ro_db_probe = true`）**：允许使用内存库 `sqlite3.connect(\":memory:\")` 验证边界，"
            "**且特许以严格只读 URI 连接** `sqlite3.connect(\"file:data/shuati.db?mode=ro\", uri=True)` 执行 1～2 次只读聚合核查；"
            "**严禁不带 `?mode=ro` 打开 `data/shuati.db`**，严禁执行任何写库、DDL/DML 修改或落盘临时数据库。"
        )
    else:
        sqlite_rule = (
            "3. **数据库物理隔离红线（`allow_ro_db_probe = false`）**：若需运行最小化 Python 探针验证边界逻辑，"
            "**仅允许使用内存数据库 `sqlite3.connect(\":memory:\")`**；"
            "**严禁以任何方式打开或读取 `data/shuati.db`**（含只读模式亦禁止），严禁触碰 `human/` 与 `data/`。"
        )

    focus_md = "\n".join(
        f"{idx}. {pt}" for idx, pt in enumerate(batch.get("focus_points") or [], start=1)
    )
    ddl_block = ddl_content if ddl_content else "-- （本批次无直接关联 SQLite 表）"
    stubs_block = (
        stubs_content
        if stubs_content
        else "# （本批目标文件无额外跨模块 shuati.* 符号依赖，或被调用定义已完整包含在本批白名单行区间内）"
    )

    target_code_tokens = estimate_tokens("\n\n".join(sliced_code_chunks))

    def _render_card(est_card_tokens: int) -> str:
        est_total = est_card_tokens + target_code_tokens
        header_extra = "\n".join(cross_banner_lines)
        return f"""# 外部增量专项代码与数据审查任务单 — `{bid}`

- **批次编号**: `{bid}`
- **批次标题**: {title}
- **优先级梯队**: Tier {tier}
- **白名单源码总行数**: {total_whitelist_lines} 行
- **预估首轮上下文开销**: ~{est_total} tokens（任务单 ~{est_card_tokens} + 白名单切片源码 ~{target_code_tokens}）
- **唯一允许写入结果路径**: `{findings_rel_posix}`{header_extra}

---

## §1 六条安全红线与读取白名单（违反任一条即判无效）

### 允许读取的文件与精确行号白名单
{chr(10).join(whitelist_bullets)}

### 强制执行红线
1. **严格白名单直读**：仅允许按上述行号区间精准读取目标文件；**严禁读取** `docs/`、`docs/records/`、`tests/`、`题库/`、`backup/`、`human/` 及任何未列出的源码文件。
2. **禁止全仓搜索**：**严禁执行全仓检索命令**（禁止全仓 `rg`、`grep`、`find`、`Get-ChildItem -Recurse`）；所需表结构与跨模块调用签名已完整内联于 §4 与 §5。
{sqlite_rule}
4. **工具调用轮次硬上限**：全程工具调用严格控制在 **`<= 5` 轮** 以内（第 1～2 轮分段读取白名单源码，第 3～4 轮可选轻量 `:memory:`/特许只读探针验证，第 5 轮写入报告），禁止发散探索。
5. **唯一写路径铁律**：本次会话**唯一允许创建/覆写的文件为 `{findings_rel_posix}`**；严禁修改任何业务源码或配置文件，严禁在根目录创建 `CLAUDE.md`，严禁执行 `git commit` / `git push`。
6. **高信噪比实证门槛**：先对照 §3 避坑表剔除已知设计约定，仅报告具备明确触发条件与真实危害的 `P0`/`P1`/`P2` 漏洞；若未发现确凿缺陷，直接按 §6 模板输出 `NO_CONFIRMED_BUGS`。

---

## §2 本批高危猎杀焦点

{focus_md}

---

## §3 全局架构不变量与避坑速查卡

{brief_content}

---

## §4 本批关联 SQLite 表结构切片（自动抽取自 `schema.py`）

```sql
{ddl_block}
```

---

## §5 上下游及切片外依赖函数签名 Stub（AST 自动抽取）

```python
{stubs_block}
```

---

## §6 审查报告输出规范与模板

请将最终审查结论写入 **`{findings_rel_posix}`**（UTF-8 编码），严格采用以下两种格式之一：

### 格式 A：发现确凿缺陷（`FINDINGS_REPORTED`）
```markdown
# {bid} 外部审查报告

- **Batch**: `{bid}` ({title})
- **Model**: `<填写当前模型标识>`
- **Verdict**: `FINDINGS_REPORTED`

## Findings

### [F1] <一句话高密度缺陷标题>
- **Severity**: `P0` | `P1` | `P2`
- **Location**: `<path>:L<start>-L<end>`
- **Verification Basis**: `STATIC_HIGH` | `PROBE_CONFIRMED`
- **Trigger Input / Scenario**: <给出确定性触发输入、调用序列或边界状态>
- **Actual vs Expected**: <实际错误行为 vs 架构不变量/表约束/数学契约期望行为>
- **Minimal Fix**: <最小化修复方向，不贴大段重构代码>
```

### 格式 B：未发现确凿缺陷（`NO_CONFIRMED_BUGS`）
```markdown
# {bid} 外部审查报告

- **Batch**: `{bid}` ({title})
- **Model**: `<填写当前模型标识>`
- **Verdict**: `NO_CONFIRMED_BUGS`
- **Checked Invariants & Focus Points**: <简述已逐项核对通过的关键状态机与边界点（3～5 行以内）>
```
"""

    draft_card = _render_card(0)
    card_tokens = estimate_tokens(draft_card)
    final_card = _render_card(card_tokens)
    card_tokens = estimate_tokens(final_card)
    final_card = _render_card(card_tokens)

    atomic_write_text(target_out, final_card)

    return PreparedTaskInfo(
        batch_id=bid,
        output_path=target_out,
        whitelist_lines=total_whitelist_lines,
        task_card_tokens=card_tokens,
        target_code_tokens=target_code_tokens,
        total_initial_tokens=card_tokens + target_code_tokens,
    )


# ---------------------------------------------------------------------------
# 状态巡检、审查登记与修复闭环 (status / record / mark-fixed)
# ---------------------------------------------------------------------------


def _compute_batch_live_hashes(repo_root: Path, batch: dict[str, Any]) -> dict[str, str | None]:
    """计算批次内所有去重目标文件的实时 SHA256；若文件缺失记为 None。"""
    root_resolved = repo_root.resolve()
    live: dict[str, str | None] = {}
    for target in batch.get("targets") or []:
        raw_path = target.get("path")
        if not isinstance(raw_path, str):
            continue
        resolved = _resolve_safe_repo_path(root_resolved, raw_path)
        if resolved is None or not resolved.is_file():
            live[Path(raw_path).as_posix()] = None
        else:
            rel_posix = resolved.relative_to(root_resolved).as_posix()
            if rel_posix not in live:
                live[rel_posix] = sha256_file(resolved)
    return live


def is_batch_dirty(repo_root: Path, batch: dict[str, Any]) -> bool:
    """对比已记录的 target_sha256 与磁盘实时哈希，检测审后改动 (MODIFIED_SINCE_REVIEW)。"""
    recorded = batch.get("target_sha256")
    if not isinstance(recorded, dict) or not recorded:
        return False
    live = _compute_batch_live_hashes(repo_root, batch)
    if set(recorded.keys()) != set(live.keys()):
        return True
    for rel_posix, old_hash in recorded.items():
        cur_hash = live.get(rel_posix)
        if cur_hash is None or cur_hash != old_hash:
            return True
    return False


def get_plan_status(repo_root: Path, tier_filter: int | None = None) -> dict[str, Any]:
    """计算全量或指定 Tier 批次的实时状态、脏标记、findings 文件检测与推荐动作。"""
    if tier_filter is not None and (isinstance(tier_filter, bool) or tier_filter not in VALID_TIERS):
        raise ValueError(f"非法 tier_filter: {tier_filter!r}")
    root_resolved = repo_root.resolve()
    data = load_plan_data(root_resolved)
    raw_batches = _require_valid_plan_batches(data)
    ext_dir = _external_review_dir(root_resolved)

    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "PENDING": 0,
        "REVIEWED": 0,
        "TRIAGED_CLEAN": 0,
        "FIXED": 0,
        "MODIFIED_SINCE_REVIEW": 0,
    }

    for batch in raw_batches:
        if not isinstance(batch, dict):
            continue
        b_tier = int(batch.get("tier", 0))
        if tier_filter is not None and b_tier != tier_filter:
            continue

        bid = str(batch["id"])
        raw_status = str(batch.get("status", "PENDING"))
        dirty = is_batch_dirty(root_resolved, batch)
        eff_status = "MODIFIED_SINCE_REVIEW" if dirty else raw_status
        findings_path = ext_dir / f"{bid}_findings.md"
        has_findings = findings_path.is_file()

        if raw_status in counts:
            counts[raw_status] += 1
        if dirty:
            counts["MODIFIED_SINCE_REVIEW"] += 1

        items.append(
            {
                "id": bid,
                "tier": b_tier,
                "title": str(batch.get("title", "")),
                "status": raw_status,
                "effective_status": eff_status,
                "is_dirty": dirty,
                "is_cross_review": bool(batch.get("is_cross_review", False)),
                "allow_ro_db_probe": bool(batch.get("allow_ro_db_probe", False)),
                "has_findings": has_findings,
                "reviewed_by_models": list(batch.get("reviewed_by_models") or []),
                "reviewed_at": batch.get("reviewed_at"),
                "notes": str(batch.get("notes") or ""),
            }
        )

    ordered_for_pick = sorted(items, key=lambda x: (x["tier"], x["id"]))
    next_batch_id: str | None = None
    for it in ordered_for_pick:
        if it["status"] == "PENDING":
            next_batch_id = it["id"]
            break
    if next_batch_id is None:
        for it in ordered_for_pick:
            if it["is_dirty"]:
                next_batch_id = it["id"]
                break

    pending_agy_triage = [
        it["id"]
        for it in items
        if it["status"] == "REVIEWED" or (it["status"] == "PENDING" and it["has_findings"])
    ]
    dirty_batch_ids = [it["id"] for it in items if it["is_dirty"]]

    return {
        "schema_version": data.get("schema_version", 1),
        "tier_filter": tier_filter,
        "total_batches": len(items),
        "counts": counts,
        "next_batch_id": next_batch_id,
        "pending_agy_triage": pending_agy_triage,
        "dirty_batches": dirty_batch_ids,
        "batches": items,
    }


def _model_family_key(model_name: str) -> str:
    lowered = model_name.strip().lower()
    if not lowered:
        return ""
    token = re.split(r"[-_:/\s]+", lowered)[0]
    if token in {"gpt", "o1", "o3", "o4", "codex", "openai"}:
        return "openai"
    if token in {"claude", "sonnet", "opus", "haiku", "anthropic"}:
        return "anthropic"
    if token in {"gemini", "google"}:
        return "google"
    return token


def _snapshot_strict_hashes(repo_root: Path, batch: dict[str, Any]) -> dict[str, str]:
    live = _compute_batch_live_hashes(repo_root, batch)
    strict: dict[str, str] = {}
    for rel_posix, digest in live.items():
        if digest is None:
            raise FileNotFoundError(f"批次 {batch.get('id')} 目标文件不存在，无法快照哈希: {rel_posix}")
        strict[rel_posix] = digest
    return strict


def _append_note(existing: str, extra: str | None) -> str:
    if extra is None or not extra.strip():
        return existing
    clean = extra.strip()
    if not existing.strip():
        return clean
    return f"{existing.strip()} | {clean}"


def record_batch(
    repo_root: Path,
    batch_id: str,
    status: str,
    model: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """更新单批次审查状态、模型列表与 SHA256 快照，原子写回 review_plan.yaml。"""
    norm_status = status.strip().upper()
    if norm_status not in VALID_STATUSES:
        raise ValueError(f"非法状态 {status!r}，允许值: {sorted(VALID_STATUSES)}")

    root_resolved = repo_root.resolve()
    data = load_plan_data(root_resolved)
    batch = _find_batch(data, batch_id)

    existing_models: list[str] = list(batch.get("reviewed_by_models") or [])
    model_clean = (model or "").strip()
    warned_duplicate_model = False

    if norm_status == "REVIEWED":
        new_family = _model_family_key(model_clean)
        has_same_prefix_or_name = bool(model_clean) and any(
            m.strip().lower() == model_clean.lower()
            or (_model_family_key(m) == new_family and new_family != "")
            for m in existing_models
        )
        if bool(batch.get("is_cross_review")) or has_same_prefix_or_name:
            print(
                "WARNING: 模型与历史记录重复，建议异构交叉盲审切换不同模型家族",
                file=sys.stderr,
            )
            warned_duplicate_model = True

    if model_clean and model_clean not in existing_models:
        existing_models.append(model_clean)
    batch["reviewed_by_models"] = existing_models

    batch["status"] = norm_status
    if norm_status in {"REVIEWED", "TRIAGED_CLEAN", "FIXED"}:
        batch["reviewed_at"] = now_iso_utc8()
        batch["target_sha256"] = _snapshot_strict_hashes(root_resolved, batch)
    else:
        batch["reviewed_at"] = None
        batch["target_sha256"] = {}

    if note is not None and note.strip():
        batch["notes"] = _append_note(str(batch.get("notes") or ""), note)

    save_plan_data(root_resolved, data)

    return {
        "id": batch["id"],
        "status": batch["status"],
        "reviewed_by_models": batch["reviewed_by_models"],
        "reviewed_at": batch["reviewed_at"],
        "target_sha256": batch["target_sha256"],
        "warned_duplicate_model": warned_duplicate_model,
    }


def _parse_batch_id_tokens(raw_tokens: Sequence[str]) -> list[str]:
    parsed: list[str] = []
    for tok in raw_tokens:
        for part in tok.split(","):
            clean = part.strip().upper()
            if clean and clean not in parsed:
                parsed.append(clean)
    return parsed


def mark_fixed_batches(
    repo_root: Path,
    batch_ids: Sequence[str],
    note: str | None = None,
) -> dict[str, Any]:
    """批量标记 FIXED 并刷新其 target_sha256，同时巡检其他批次是否因共享文件改动变为脏状态。"""
    normalized_ids = _parse_batch_id_tokens(batch_ids)
    if not normalized_ids:
        raise ValueError("至少需要指定一个批次 ID")

    root_resolved = repo_root.resolve()
    data = load_plan_data(root_resolved)

    # 先全部解析确认存在，避免部分修改脏状态
    target_batches = [_find_batch(data, bid) for bid in normalized_ids]
    ts = now_iso_utc8()

    for batch in target_batches:
        batch["status"] = "FIXED"
        batch["reviewed_at"] = ts
        batch["target_sha256"] = _snapshot_strict_hashes(root_resolved, batch)
        if note is not None and note.strip():
            batch["notes"] = _append_note(str(batch.get("notes") or ""), note)

    save_plan_data(root_resolved, data)

    fixed_set = {b["id"] for b in target_batches}
    collateral_dirty: list[str] = []
    for other in data.get("batches") or []:
        if not isinstance(other, dict):
            continue
        oid = str(other.get("id", ""))
        if oid not in fixed_set and is_batch_dirty(root_resolved, other):
            collateral_dirty.append(oid)

    return {
        "fixed_batches": [b["id"] for b in target_batches],
        "reviewed_at": ts,
        "collateral_dirty_batches": collateral_dirty,
    }


# ---------------------------------------------------------------------------
# CLI 参数解析与人类友好输出
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        prog="review_harness.py",
        description="shuati-harness 零 LLM 成本增量外部代码与数据审查脚手架",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=default_root,
        help="仓库根目录路径（默认自动定位至 scripts/ 上级目录）",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("validate", help="校验 review_plan.yaml 覆盖率、行号区间与表结构合法性")

    p_prep = subparsers.add_parser("prepare", help="生成指定批次的自包含审查任务单 (<ID>_task.md)")
    p_prep.add_argument("batch_id", help="批次 ID（如 B01，大小写不敏感）")
    p_prep.add_argument("--out", type=Path, default=None, help="自定义输出文件路径")

    p_status = subparsers.add_parser("status", help="查看审查进度、哈希脏状态与待裁决批次")
    p_status.add_argument("--tier", type=int, choices=[1, 2, 3, 4], default=None, help="按梯队过滤")
    p_status.add_argument("--json", action="store_true", dest="as_json", help="输出结构化 JSON")

    p_rec = subparsers.add_parser("record", help="登记单批次审查状态与外部模型标识")
    p_rec.add_argument("batch_id", help="批次 ID（如 B01）")
    p_rec.add_argument(
        "--status",
        required=True,
        choices=sorted(VALID_STATUSES),
        type=str.upper,
        help="目标状态",
    )
    p_rec.add_argument("--model", default=None, help="执行审查的外部模型名称（如 codex-cli）")
    p_rec.add_argument("--note", default=None, help="附加备注说明")

    p_fix = subparsers.add_parser(
        "mark-fixed", help="批量标记批次已修复、刷新文件哈希并巡检连带脏批次"
    )
    p_fix.add_argument(
        "batch_ids",
        nargs="+",
        help="空格或逗号分隔的批次 ID（如 B01 B02 或 B13,B14）",
    )
    p_fix.add_argument("--note", default=None, help="附加修复说明")

    return parser


def _print_launch_commands(task_rel_posix: str, batch_id: str) -> None:
    prompt = (
        f"请阅读并严格执行 {task_rel_posix} 中的专项代码审查任务，"
        f"严守 §1 白名单与 <=5 轮红线，将结论写入 .agents/teamwork/external_review/{batch_id}_findings.md"
    )
    print("\n可直接复制执行的单行外部 CLI 启动命令：")
    print(f'  [Codex ] codex "{prompt}"')
    print(f'  [Claude] claude "{prompt}"')
    print(f'  [Gemini] gemini -p "{prompt}"')


def run_cli(argv: Sequence[str] | None = None) -> int:
    """CLI 主分发入口，返回进程退出码（0 成功，1 失败）。"""
    _configure_utf8_stdio()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    repo_root: Path = args.repo_root.resolve()

    try:
        if args.subcommand == "validate":
            res = validate_plan(repo_root)
            if not res.ok:
                print("VALIDATE FAILED:", file=sys.stderr)
                for err in res.errors:
                    print(f"  - {err}", file=sys.stderr)
                return 1
            print(
                f"VALIDATE OK: {res.batch_count} 个批次校验通过 | "
                f"src/shuati 模块覆盖 {res.covered_src_modules}/{res.total_src_modules} (100%) | "
                f"大文件无缝切片覆盖 {len(res.split_files_verified)} 个"
            )
            return 0

        if args.subcommand == "prepare":
            info = prepare_batch(repo_root, args.batch_id, out_path=args.out)
            try:
                rel_out = info.output_path.resolve().relative_to(repo_root).as_posix()
            except ValueError:
                rel_out = str(info.output_path)
            print(f"已生成审查任务单: {rel_out}")
            print(
                f"Token 预估明细: 任务单 ~{info.task_card_tokens} + "
                f"白名单源码 ({info.whitelist_lines} 行) ~{info.target_code_tokens} "
                f"= 首轮总上下文 ~{info.total_initial_tokens} tokens"
            )
            _print_launch_commands(rel_out, info.batch_id)
            return 0

        if args.subcommand == "status":
            report = get_plan_status(repo_root, tier_filter=args.tier)
            if args.as_json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            c = report["counts"]
            print(
                f"审查总览 (共 {report['total_batches']} 批): "
                f"PENDING={c['PENDING']} | REVIEWED={c['REVIEWED']} | "
                f"TRIAGED_CLEAN={c['TRIAGED_CLEAN']} | FIXED={c['FIXED']} | "
                f"MODIFIED_SINCE_REVIEW={c['MODIFIED_SINCE_REVIEW']}"
            )
            print(f"下一推荐执行批次 : {report['next_batch_id'] or '无'}")
            print(
                f"待 AGY 裁决批次  : {', '.join(report['pending_agy_triage']) if report['pending_agy_triage'] else '无'}"
            )
            if report["dirty_batches"]:
                print(f"审后代码变动批次 : {', '.join(report['dirty_batches'])}")
            print("-" * 88)
            print(f"{'ID':<5} {'Tier':<5} {'EffectiveStatus':<23} {'Findings':<9} {'Models':<18} Title")
            print("-" * 88)
            for it in report["batches"]:
                models_str = ",".join(it["reviewed_by_models"]) or "-"
                findings_flag = "YES" if it["has_findings"] else "-"
                print(
                    f"{it['id']:<5} T{it['tier']:<4} {it['effective_status']:<23} "
                    f"{findings_flag:<9} {models_str:<18} {it['title']}"
                )
            return 0

        if args.subcommand == "record":
            rec = record_batch(
                repo_root,
                args.batch_id,
                status=args.status,
                model=args.model,
                note=args.note,
            )
            print(
                f"已更新批次 {rec['id']}: status={rec['status']}, "
                f"models={rec['reviewed_by_models']}, sha256_files={len(rec['target_sha256'])}"
            )
            return 0

        if args.subcommand == "mark-fixed":
            fix_res = mark_fixed_batches(repo_root, args.batch_ids, note=args.note)
            print(f"已标记 FIXED 并刷新哈希快照: {', '.join(fix_res['fixed_batches'])}")
            if fix_res["collateral_dirty_batches"]:
                print(
                    "注意 — 以下已审批次共享改动文件，现处于 MODIFIED_SINCE_REVIEW 脏状态: "
                    + ", ".join(fix_res["collateral_dirty_batches"])
                )
            return 0

    except (KeyError, ValueError, FileNotFoundError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
