"""Repro: 空结果文件（零回传）即把 subagent_bridge 批置为 IMPORTED，防踩踏锁随后被绕过。

load_and_validate_results 仅要求 `errors == []` 即写 status=IMPORTED：
空 results 文件 -> raw_records=[] -> errors=[] -> IMPORTED（实际 0 批被校验/回灌）。
随后 export_task_batches 不再被 PENDING_UNIMPORTED_BATCH 拦截，task_map 被覆盖，
仍在途的 Worker 产出将无法映射。

运行：.venv\\Scripts\\python.exe tests/repro/repro_seam_bridge_empty_results_marks_imported.py
期望修复后 Exit Code == 0（manifest 仍应为 PENDING_IMPORT，且再次导出被拦截）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from shuati.db.connection import PreconditionError  # noqa: E402
from shuati.models.schemas import TaxonomyProposeOutput  # noqa: E402
from shuati.pipeline.subagent_bridge import (  # noqa: E402
    export_task_batches,
    load_and_validate_results,
)


def main() -> int:
    d = Path(tempfile.mkdtemp(prefix="repro_bridge_"))
    batches = [
        {"batch_id": "b1", "messages": [{"role": "user", "content": "hello"}], "task_item_ids": ["t1"]},
        {"batch_id": "b2", "messages": [{"role": "user", "content": "world"}], "task_item_ids": ["t2"]},
    ]
    task_map = {"b1": {"x": 1}, "b2": {"x": 2}}
    export_task_batches("stage3a", batches, task_map, d, schema_cls=TaxonomyProposeOutput)

    mf = d / "stage3a_manifest.json"
    assert json.loads(mf.read_text(encoding="utf-8"))["status"] == "PENDING_IMPORT"

    # Worker 尚未回传任何内容（0 字节结果文件）
    res = d / "results.jsonl"
    res.write_text("", encoding="utf-8")
    valid, _tm, errors = load_and_validate_results(
        "stage3a", res, d / "stage3a_task_map.json", TaxonomyProposeOutput
    )
    status = json.loads(mf.read_text(encoding="utf-8"))["status"]
    print(f"valid_batches={len(valid)} errors={len(errors)} manifest.status={status}")
    assert status == "PENDING_IMPORT", (
        f"manifest 被置为 {status}，但 0/{len(batches)} 个批被回灌（空结果文件不应释放防踩踏锁）"
    )

    try:
        export_task_batches("stage3a", batches, task_map, d, schema_cls=TaxonomyProposeOutput)
    except PreconditionError:
        print("OK: re-export blocked")
        return 0
    raise AssertionError("再次导出未被拦截：未回灌批被静默覆盖")


if __name__ == "__main__":
    raise SystemExit(main())
