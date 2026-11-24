"""Repro: unified 部分回灌（2 道任务只回传 1 道、无失败）即把 manifest 置为 IMPORTED，防踩踏锁被绕过。

import_unified_results 末尾仅以 `retry_count == 0 and failed_final_count == 0` 判定 IMPORTED，
未核对 task_map 中是否仍有未回传任务；同目录另一个先于 manifest 重写时刻生成、尚未消费的
results_b.jsonl 因 mtime 早于刷新后的 manifest 而逃过 `_check_unified_pending_lock`，
随后 export_unified_tasks 覆盖 task_map.json，results_b 里的 T0002 永久失配。

运行：.venv\\Scripts\\python.exe tests/repro/repro_seam_unified_partial_import_marks_imported.py
期望修复后 Exit Code == 0。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))

import test_compiler_seams_tools_mcp as H  # noqa: E402

from shuati.db.connection import PreconditionError, get_connection  # noqa: E402
from shuati.db.schema import init_db  # noqa: E402
from shuati.pipeline.unified_import import (  # noqa: E402
    export_unified_tasks,
    import_unified_results,
    register_anchors_from_yaml,
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="repro_unified_"))
    cfg_dir = tmp / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    db_path = tmp / "t.db"
    anchors = [f"primary_olympiad:ch_t:anc_{i}" for i in range(1, 5)]
    qa, qb = "primary_olympiad:ch_t:qa", "primary_olympiad:ch_t:qb"

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        for i, uid in enumerate(anchors, 1):
            H._seed_question(conn, uid=uid, source_id=f"anc_{i}", text=f"锚点 {i}", content_hash=f"ha{i}", verified=True)
            conn.execute(
                "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin,"
                " method_summary, solution_markdown, sympy_verified) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '直算', '解析', 1)",
                (f"{uid}::branch_1", uid),
            )
        H._seed_question(conn, uid=qa, source_id="qa", text="计算 12 + 30 等于多少？", content_hash="hqa")
        H._seed_question(conn, uid=qb, source_id="qb", text="计算 12 + 30 等于多少？", content_hash="hqb")
        conn.commit()
    ay = H._write_anchors(cfg_dir, anchors)
    register_anchors_from_yaml(db_path=db_path, config_dir=cfg_dir, anchors_file=ay)

    out = tmp / "unified"
    exp = export_unified_tasks(db_path=db_path, out_dir=out, config_dir=cfg_dir, only_uids=[qa, qb])
    assert exp.exported_unified == 2, exp
    tmap = json.loads((out / "task_map.json").read_text(encoding="utf-8"))
    assert sorted(tmap) == ["T0001", "T0002"], tmap

    # Worker B 的结果已落盘但尚未被导入（先于后续 manifest 重写）
    (out / "results_b.jsonl").write_text(
        json.dumps(H._sample_unified_candidate("T0002"), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # 仅回灌 Worker A 的 T0001
    res_a = out / "results_a.jsonl"
    res_a.write_text(json.dumps(H._sample_unified_candidate("T0001"), ensure_ascii=False) + "\n", encoding="utf-8")
    imp = import_unified_results(
        db_path=db_path, results_file=res_a, task_map_file=out / "task_map.json", config_dir=cfg_dir
    )
    status = json.loads((out / "unified_manifest.json").read_text(encoding="utf-8"))["status"]
    print(f"imported={imp.ingested_verified} retry={imp.retry_count} failed={imp.failed_final_count} manifest.status={status}")
    assert status == "PENDING_IMPORT", f"仅回灌 1/2 任务却被置为 {status}"

    try:
        export_unified_tasks(db_path=db_path, out_dir=out, config_dir=cfg_dir, only_uids=[qa, qb])
    except PreconditionError as exc:
        assert "PENDING_UNIMPORTED_BATCH" in str(exc)
        print("OK: re-export blocked")
        return 0
    raise AssertionError("存在未回灌的 T0002 / results_b.jsonl，但再次导出未被防踩踏锁拦截并已覆盖 task_map.json")


if __name__ == "__main__":
    raise SystemExit(main())
