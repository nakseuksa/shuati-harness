"""统一 Subagent JSONL 脱敏双通道（export / import），复用于 propose/3c/compact/3e/4。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

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


def export_task_batches(
    step_kind: str,
    batches: list[dict[str, Any]],
    task_map: dict[str, Any],
    out_dir: Path | str,
    *,
    schema_cls: type[BaseModel],
) -> dict[str, Any]:
    """写出脱敏批任务至 out_dir/batches/batch_XXXX.jsonl，并将真实 ID 映射隔离保存。"""
    base = Path(out_dir)
    batches_dir = base / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    # 清理旧批次文件避免残留干扰
    for old_f in batches_dir.glob("batch_*.jsonl"):
        old_f.unlink(missing_ok=True)

    schema_json = json.dumps(schema_cls.model_json_schema(), ensure_ascii=False)
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

    task_map_file = base / f"{step_kind}_task_map.json"
    _atomic_write_text(task_map_file, json.dumps(task_map, ensure_ascii=False, indent=2))

    manifest_file = base / f"{step_kind}_manifest.json"
    manifest = {
        "step_kind": step_kind,
        "schema_name": schema_cls.__name__,
        "batch_count": len(batches),
        "batch_files": batch_files,
        "task_map_file": str(task_map_file),
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


def load_and_validate_results(
    step_kind: str,
    results_path: Path | str,
    task_map_file: Path | str,
    schema_cls: type[T],
    *,
    retry_dir: Path | str | None = None,
) -> tuple[dict[str, T], dict[str, Any], list[dict[str, Any]]]:
    """逐批校验 Subagent 回传结果，不合规条写入 retry_tasks.jsonl。"""
    r_path = Path(results_path)
    m_path = Path(task_map_file)
    if not r_path.exists():
        raise FileNotFoundError(f"结果文件不存在: {r_path}")
    if not m_path.exists():
        raise FileNotFoundError(f"隔离映射文件不存在: {m_path}")

    task_map: dict[str, Any] = json.loads(m_path.read_text(encoding="utf-8"))
    raw_records = _iter_result_records(r_path)

    valid_by_batch: dict[str, T] = {}
    errors: list[dict[str, Any]] = []

    for rec in raw_records:
        if "_parse_error" in rec:
            errors.append({"batch_id": "", "error": rec["_parse_error"], "raw": rec.get("_raw", "")})
            continue
        batch_id = str(rec.get("batch_id") or "")
        payload = rec.get("result") if isinstance(rec.get("result"), dict) else {
            k: v for k, v in rec.items() if k not in ("batch_id", "step_kind")
        }
        try:
            validated = schema_cls.model_validate(payload)
            key = batch_id or f"{step_kind}_single"
            valid_by_batch[key] = validated
        except ValidationError as exc:
            errors.append({"batch_id": batch_id, "error": str(exc), "payload": payload})

    if errors:
        out_base = Path(retry_dir) if retry_dir is not None else m_path.parent
        retry_path = out_base / "retry_tasks.jsonl"
        # 从 batches 目录回查失败 batch_id 对应的原始脱敏任务，找不到则直接记录错误信息
        original_by_batch: dict[str, str] = {}
        batches_dir = m_path.parent / "batches"
        if batches_dir.exists():
            for bf in sorted(batches_dir.glob("batch_*.jsonl")):
                for line in bf.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        bobj = json.loads(line)
                        if isinstance(bobj, dict) and bobj.get("batch_id"):
                            original_by_batch[str(bobj["batch_id"])] = line
                    except ValueError:
                        pass
        retry_lines: list[str] = []
        for err in errors:
            bid = err.get("batch_id", "")
            if bid and bid in original_by_batch:
                retry_lines.append(original_by_batch[bid])
            else:
                retry_lines.append(json.dumps({"step_kind": step_kind, **err}, ensure_ascii=False))
        _atomic_write_text(retry_path, "\n".join(retry_lines) + "\n")

    return valid_by_batch, task_map, errors
