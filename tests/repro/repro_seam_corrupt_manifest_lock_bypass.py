"""Repro: 防踩踏锁对损坏的 manifest / task_map 采取 fail-open，未回灌批被静默放行覆盖。

_check_unified_pending_lock 与 subagent_bridge._check_pending_export_lock 均将解析失败的
manifest 视为 `{}`（既非 PENDING_IMPORT 也非 IMPORTED），且 unified 对损坏 task_map 视为空，
结果：目录里仍有 3 道未回灌任务，但锁判定放行。

判定方式：lock 通过后 export_unified_tasks 才会访问 DB；这里 db 指向不存在文件，
若锁拦截则抛 PreconditionError(PENDING_UNIMPORTED_BATCH)，否则抛 OperationalError（说明已越过锁）。

运行：.venv\\Scripts\\python.exe tests/repro/repro_seam_corrupt_manifest_lock_bypass.py
期望修复后 Exit Code == 0（损坏状态下必须保守拦截）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from shuati.db.connection import PreconditionError  # noqa: E402
from shuati.pipeline.unified_import import export_unified_tasks  # noqa: E402

TASK_MAP = json.dumps({f"T{i:04d}": {"question_uid": f"u{i}"} for i in range(1, 4)})


def blocked(manifest: str | None, task_map: str | None) -> tuple[bool, str]:
    d = Path(tempfile.mkdtemp(prefix="repro_lock_"))
    if manifest is not None:
        (d / "unified_manifest.json").write_text(manifest, encoding="utf-8")
    if task_map is not None:
        (d / "task_map.json").write_text(task_map, encoding="utf-8")
    try:
        export_unified_tasks(db_path=d / "nope.db", out_dir=d, config_dir=ROOT / "config")
    except PreconditionError as exc:
        return "PENDING_UNIMPORTED_BATCH" in str(exc), f"PreconditionError: {str(exc)[:60]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:60]} (已越过防踩踏锁)"
    return False, "no error"


def main() -> int:
    cases = {
        "manifest 被截断为 0 字节 + 3 任务 task_map": ("", TASK_MAP),
        "manifest 非法 JSON + 3 任务 task_map": ("{not json", TASK_MAP),
        "manifest=PENDING_IMPORT 但 task_map 损坏": (json.dumps({"status": "PENDING_IMPORT"}), "{bad"),
    }
    failed = 0
    for label, (mf, tm) in cases.items():
        ok, detail = blocked(mf, tm)
        print(f"[{'OK ' if ok else 'BUG'}] {label} -> {detail}")
        failed += 0 if ok else 1
    if failed:
        print(f"REPRO FAILED: {failed}/{len(cases)} 损坏态被放行")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
