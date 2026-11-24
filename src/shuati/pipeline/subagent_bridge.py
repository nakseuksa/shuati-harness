"""统一 Subagent JSONL 脱敏双通道（export / import），复用于 propose/3c/compact/3e/4。"""

from __future__ import annotations

import json
import os
import types
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from shuati.db.connection import PreconditionError
from shuati.llm.prompt_builder import flatten_json_schema

T = TypeVar("T", bound=BaseModel)

# 防泄露自检黑名单字段（严禁进入 batches/*.jsonl）
_FORBIDDEN_EXPORT_KEYS = {
    "question_uid",
    "source_collection",
    "raw_chapter",
    "legacy_tags",
    "legacy_tags_json",
    "source_file",
    "raw_images_json",
}


def _assert_sanitized(obj: Any, *, path: str = "root") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _FORBIDDEN_EXPORT_KEYS:
                raise ValueError(f"脱敏导出校验失败：在 {path}.{k} 检测到禁止外泄字段 {k!r}")
            _assert_sanitized(v, path=f"{path}.{k}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _assert_sanitized(item, path=f"{path}[{idx}]")


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _check_pending_export_lock(
    base: Path,
    *,
    manifest_file: Path,
    task_map_file: Path,
    overwrite_pending: bool,
) -> None:
    """防踩踏锁：拦截未回灌的挂起批、未消费结果文件或损坏的状态文件（fail-closed）。"""
    if overwrite_pending:
        return

    manifest_data: dict[str, Any] = {}
    if manifest_file.is_file():
        try:
            loaded = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PreconditionError(
                f"PENDING_UNIMPORTED_BATCH: 清单文件 '{manifest_file.as_posix()}' 损坏或无法解析 ({exc})，"
                "已保守拦截覆盖导出；请人工核查或显式传入 --overwrite-pending"
            ) from exc
        if not isinstance(loaded, dict):
            raise PreconditionError(
                f"PENDING_UNIMPORTED_BATCH: 清单文件 '{manifest_file.as_posix()}' 根结构非对象，"
                "已保守拦截覆盖导出；请人工核查或显式传入 --overwrite-pending"
            )
        manifest_data = loaded

    if task_map_file.is_file():
        try:
            loaded_tm = json.loads(task_map_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PreconditionError(
                f"PENDING_UNIMPORTED_BATCH: 映射文件 '{task_map_file.as_posix()}' 损坏或无法解析 ({exc})，"
                "已保守拦截覆盖导出；请人工核查或显式传入 --overwrite-pending"
            ) from exc
        if not isinstance(loaded_tm, dict):
            raise PreconditionError(
                f"PENDING_UNIMPORTED_BATCH: 映射文件 '{task_map_file.as_posix()}' 根结构非对象，"
                "已保守拦截覆盖导出；请人工核查或显式传入 --overwrite-pending"
            )

    if manifest_data.get("status") == "PENDING_IMPORT":
        raise PreconditionError(
            f"PENDING_UNIMPORTED_BATCH: 目录 '{base.as_posix()}' 存在未回灌批 "
            f"({manifest_file.name}, status=PENDING_IMPORT)，请先执行 import 或显式传入 --overwrite-pending"
        )

    ref_mtime: float | None = None
    manifest_imported = manifest_data.get("status") == "IMPORTED"
    if manifest_file.is_file():
        ref_mtime = manifest_file.stat().st_mtime
    elif task_map_file.is_file():
        ref_mtime = task_map_file.stat().st_mtime

    if ref_mtime is not None and base.is_dir():
        result_files = sorted(list(base.glob("results*.jsonl")) + list(base.glob("results*.json")))
        for rf in result_files:
            if not manifest_imported or rf.stat().st_mtime > ref_mtime:
                raise PreconditionError(
                    f"PENDING_UNIMPORTED_BATCH: 目录 '{base.as_posix()}' 存在未消费结果文件 "
                    f"'{rf.name}'，请先执行 import 或显式传入 --overwrite-pending"
                )


def export_task_batches(
    step_kind: str,
    batches: list[dict[str, Any]],
    task_map: dict[str, Any],
    out_dir: Path | str,
    *,
    schema_cls: type[BaseModel],
    subagent_model: str | None = None,
    prompt_version: str | None = None,
    prompt_digest: str | None = None,
    prompt_profile: str | None = None,
    max_parallel_workers: int = 4,
    overwrite_pending: bool = False,
) -> dict[str, Any]:
    """写出脱敏批任务至 out_dir/batches/batch_XXXX.jsonl，并将真实 ID 映射隔离保存。"""
    base = Path(out_dir)
    task_map_file = base / f"{step_kind}_task_map.json"
    manifest_file = base / f"{step_kind}_manifest.json"
    _check_pending_export_lock(
        base,
        manifest_file=manifest_file,
        task_map_file=task_map_file,
        overwrite_pending=overwrite_pending,
    )

    batches_dir = base / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    # 清理旧批次文件避免残留干扰
    for old_f in batches_dir.glob("batch_*.jsonl"):
        old_f.unlink(missing_ok=True)

    flat_schema = flatten_json_schema(schema_cls)
    flat_schema_file = base / "flat_schema.json"
    _atomic_write_text(flat_schema_file, json.dumps(flat_schema, ensure_ascii=False, indent=2))

    schema_json = json.dumps(flat_schema, ensure_ascii=False)
    batch_files: list[str] = []
    for idx, batch in enumerate(batches, start=1):
        record = {
            "batch_id": str(batch["batch_id"]),
            "step_kind": step_kind,
            "schema_name": schema_cls.__name__,
            "schema_json": schema_json,
            "messages": batch["messages"],
            "task_item_ids": list(batch.get("task_item_ids", [])),
        }
        _assert_sanitized(record, path=f"batch_{idx}")
        b_file = batches_dir / f"batch_{idx:04d}.jsonl"
        _atomic_write_text(b_file, json.dumps(record, ensure_ascii=False) + "\n")
        batch_files.append(str(b_file))

    _atomic_write_text(task_map_file, json.dumps(task_map, ensure_ascii=False, indent=2))

    batch_item_ids = {
        str(b["batch_id"]): [str(x) for x in (b.get("task_item_ids") or [])]
        for b in batches
    }

    manifest = {
        "step_kind": step_kind,
        "schema_name": schema_cls.__name__,
        "flat_schema_file": str(flat_schema_file),
        "batch_count": len(batches),
        "batch_ids": [str(b["batch_id"]) for b in batches],
        "batch_item_ids": batch_item_ids,
        "imported_batch_ids": [],
        "batch_files": batch_files,
        "task_map_file": str(task_map_file),
        "subagent_model": (subagent_model or "gemini-4-neon-medium"),
        "dispatch_driver": "swarm",
        "max_parallel_workers": max(1, int(max_parallel_workers)),
        "prompt_version": prompt_version or "",
        "prompt_digest": prompt_digest or "",
        "prompt_profile": prompt_profile,
        "status": "PENDING_IMPORT",
        "exported_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write_text(manifest_file, json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def _iter_result_records(results_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    files = sorted(results_path.glob("*.json*")) if results_path.is_dir() else [results_path]
    for fp in files:
        text = fp.read_text(encoding="utf-8").strip()
        if not text:
            continue
        if fp.suffix.lower() == ".json":
            obj = json.loads(text)
            if isinstance(obj, list):
                records.extend(x for x in obj if isinstance(x, dict))
            elif isinstance(obj, dict):
                records.append(obj)
        else:
            for line_no, line in enumerate(text.splitlines(), start=1):
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        records.append(obj)
                    else:
                        records.append({"_parse_error": f"{fp.name}:{line_no} 非对象", "_raw": s})
                except ValueError as exc:
                    records.append({"_parse_error": f"{fp.name}:{line_no} JSON 解析失败: {exc}", "_raw": s})
    return records


def _unwrap_element_model_fields(annotation: Any) -> dict[str, Any]:
    """解析容器字段标注（如 list[ItemModel] / Optional[list[ItemModel]]）中的元素模型字段表。"""
    if annotation is None:
        return {}
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            found = _unwrap_element_model_fields(arg)
            if found:
                return found
        return {}
    if origin in (list, tuple, set, frozenset):
        for arg in get_args(annotation):
            if hasattr(arg, "model_fields"):
                return dict(arg.model_fields)
            nested = _unwrap_element_model_fields(arg)
            if nested:
                return nested
        return {}
    if origin is not None:
        args = get_args(annotation)
        if args:
            return _unwrap_element_model_fields(args[0])
    if hasattr(annotation, "model_fields"):
        return dict(annotation.model_fields)
    return {}


def _resolve_item_id_extractor(
    schema_cls: type[T],
    id_extractor: Callable[[T], set[str]] | None,
) -> Callable[[T], set[str]] | None:
    """自动识别条目型批输出 Schema 的 ID 提取器；非条目型单对象返回 None。"""
    if id_extractor is not None:
        return id_extractor

    fields = getattr(schema_cls, "model_fields", {}) or {}
    if "items" in fields:
        elem_fields = _unwrap_element_model_fields(fields["items"].annotation)
        if "task_item_id" in elem_fields:
            return lambda obj: {
                str(getattr(x, "task_item_id", "")).strip()
                for x in (getattr(obj, "items", None) or [])
                if str(getattr(x, "task_item_id", "")).strip()
            }
        if "task_id" in elem_fields:
            return lambda obj: {
                str(getattr(x, "task_id", "")).strip()
                for x in (getattr(obj, "items", None) or [])
                if str(getattr(x, "task_id", "")).strip()
            }
    if "decisions" in fields:
        elem_fields = _unwrap_element_model_fields(fields["decisions"].annotation)
        if "pair_id" in elem_fields:
            return lambda obj: {
                str(getattr(x, "pair_id", "")).strip()
                for x in (getattr(obj, "decisions", None) or [])
                if str(getattr(x, "pair_id", "")).strip()
            }
    if "assignments" in fields:

        def _extract_assignments(obj: T) -> set[str]:
            val = getattr(obj, "assignments", None)
            if isinstance(val, dict):
                return {str(k).strip() for k in val.keys() if str(k).strip()}
            ids: set[str] = set()
            for x in (val or []):
                ident = str(getattr(x, "item_id", "") or getattr(x, "raw_l3", "")).strip()
                if ident:
                    ids.add(ident)
            return ids

        return _extract_assignments
    return None


def _load_batch_metadata(
    m_path: Path,
    step_kind: str,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    """读取 manifest 与 batches/batch_*.jsonl 中的批次预期条目 ID 与原始任务行。"""
    manifest_batch_item_ids: dict[str, list[str]] = {}
    manifest_file = m_path.parent / f"{step_kind}_manifest.json"
    if manifest_file.is_file():
        try:
            m_obj = json.loads(manifest_file.read_text(encoding="utf-8"))
            if isinstance(m_obj, dict) and isinstance(m_obj.get("batch_item_ids"), dict):
                for bid, id_list in m_obj["batch_item_ids"].items():
                    if isinstance(id_list, list):
                        manifest_batch_item_ids[str(bid)] = [
                            str(x).strip() for x in id_list if str(x).strip()
                        ]
        except Exception:
            pass

    file_batch_item_ids: dict[str, list[str]] = {}
    original_by_batch: dict[str, str] = {}
    batches_dir = m_path.parent / "batches"
    if batches_dir.exists():
        for bf in sorted(batches_dir.glob("batch_*.jsonl")):
            for line in bf.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    bobj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(bobj, dict) and bobj.get("batch_id"):
                    bid = str(bobj["batch_id"])
                    original_by_batch[bid] = line
                    if isinstance(bobj.get("task_item_ids"), list):
                        file_batch_item_ids[bid] = [
                            str(x).strip() for x in bobj["task_item_ids"] if str(x).strip()
                        ]

    return manifest_batch_item_ids, file_batch_item_ids, original_by_batch


def _resolve_expected_item_ids(
    batch_id: str,
    manifest_batch_item_ids: dict[str, list[str]],
    file_batch_item_ids: dict[str, list[str]],
) -> set[str]:
    if batch_id:
        raw_list = manifest_batch_item_ids.get(batch_id)
        if raw_list is None:
            raw_list = file_batch_item_ids.get(batch_id)
        return {x for x in (raw_list or []) if x}
    if len(manifest_batch_item_ids) == 1:
        return {x for x in next(iter(manifest_batch_item_ids.values())) if x}
    if len(file_batch_item_ids) == 1:
        return {x for x in next(iter(file_batch_item_ids.values())) if x}
    return set()


def load_and_validate_results(
    step_kind: str,
    results_path: Path | str,
    task_map_file: Path | str,
    schema_cls: type[T],
    *,
    retry_dir: Path | str | None = None,
    verify_item_completeness: bool = True,
    id_extractor: Callable[[T], set[str]] | None = None,
) -> tuple[dict[str, T], dict[str, Any], list[dict[str, Any]]]:
    """逐批校验 Subagent 回传结果（含条目 100% 完整性硬校验），不合规条写入 retry_tasks.jsonl。"""
    r_path = Path(results_path)
    m_path = Path(task_map_file)
    if not r_path.exists():
        raise FileNotFoundError(f"结果文件不存在: {r_path}")
    if not m_path.exists():
        raise FileNotFoundError(f"隔离映射文件不存在: {m_path}")

    task_map: dict[str, Any] = json.loads(m_path.read_text(encoding="utf-8"))
    raw_records = _iter_result_records(r_path)
    manifest_batch_item_ids, file_batch_item_ids, original_by_batch = _load_batch_metadata(
        m_path, step_kind
    )
    extractor = _resolve_item_id_extractor(schema_cls, id_extractor) if verify_item_completeness else None

    valid_by_batch: dict[str, T] = {}
    errors: list[dict[str, Any]] = []

    for rec in raw_records:
        if "_parse_error" in rec:
            errors.append({"batch_id": "", "error": rec["_parse_error"], "raw": rec.get("_raw", "")})
            continue
        batch_id = str(rec.get("batch_id") or "")
        key = batch_id or f"{step_kind}_single"
        payload = rec.get("result") if isinstance(rec.get("result"), dict) else {
            k: v for k, v in rec.items() if k not in ("batch_id", "step_kind")
        }
        try:
            validated = schema_cls.model_validate(payload)
        except ValidationError as exc:
            if batch_id:
                valid_by_batch.pop(key, None)
            errors.append({"batch_id": batch_id, "error": str(exc), "payload": payload})
            continue

        if extractor is not None:
            expected_item_ids = _resolve_expected_item_ids(
                batch_id, manifest_batch_item_ids, file_batch_item_ids
            )
            if expected_item_ids:
                returned_ids = extractor(validated)
                missing = sorted(expected_item_ids - returned_ids)
                if missing:
                    valid_by_batch.pop(key, None)
                    errors.append(
                        {
                            "batch_id": batch_id,
                            "error_code": "INCOMPLETE_BATCH_ITEMS",
                            "error": (
                                f"INCOMPLETE_BATCH_ITEMS: batch_id={batch_id or key} "
                                f"缺失 {len(missing)} 项: {missing[:10]}"
                            ),
                            "missing_item_ids": missing,
                            "expected_count": len(expected_item_ids),
                            "returned_count": len(returned_ids),
                        }
                    )
                    continue

        valid_by_batch[key] = validated

    if errors:
        out_base = Path(retry_dir) if retry_dir is not None else m_path.parent
        retry_path = out_base / "retry_tasks.jsonl"
        retry_lines: list[str] = []
        single_fallback_line = (
            next(iter(original_by_batch.values())) if len(original_by_batch) == 1 else None
        )
        for err in errors:
            bid = str(err.get("batch_id") or "")
            if bid and bid in original_by_batch:
                retry_lines.append(original_by_batch[bid])
            elif not bid and err.get("error_code") == "INCOMPLETE_BATCH_ITEMS" and single_fallback_line:
                retry_lines.append(single_fallback_line)
            else:
                retry_lines.append(json.dumps({"step_kind": step_kind, **err}, ensure_ascii=False))
        _atomic_write_text(retry_path, "\n".join(retry_lines) + "\n")

    manifest_file = m_path.parent / f"{step_kind}_manifest.json"
    if manifest_file.is_file() and valid_by_batch:
        try:
            m_obj = json.loads(manifest_file.read_text(encoding="utf-8"))
            if isinstance(m_obj, dict):
                prev_imported = [
                    str(x)
                    for x in (m_obj.get("imported_batch_ids") or [])
                    if isinstance(x, str) and x
                ]
                merged_imported = list(dict.fromkeys([*prev_imported, *valid_by_batch.keys()]))
                m_obj["imported_batch_ids"] = merged_imported

                expected_ids = [
                    str(x)
                    for x in (m_obj.get("batch_ids") or [])
                    if isinstance(x, str) and x
                ]
                expected_count = int(m_obj.get("batch_count") or 0)
                if expected_ids:
                    all_covered = set(expected_ids).issubset(set(merged_imported)) or (
                        len(expected_ids) == 1
                        and f"{step_kind}_single" in valid_by_batch
                    )
                elif expected_count > 0:
                    all_covered = len(merged_imported) >= expected_count
                else:
                    all_covered = True

                if not errors and all_covered:
                    m_obj["status"] = "IMPORTED"
                    m_obj["imported_at"] = datetime.now(UTC).isoformat()
                _atomic_write_text(
                    manifest_file,
                    json.dumps(m_obj, ensure_ascii=False, indent=2),
                )
        except Exception:
            pass

    return valid_by_batch, task_map, errors
