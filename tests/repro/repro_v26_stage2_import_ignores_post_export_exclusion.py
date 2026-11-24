"""Repro v26: Stage 2 import 不复核导出后状态漂移——导出后被下架(is_excluded)/降级的题仍被写入 S2=OK。

export 时仅筛 eligible（eff_is_excluded=0 且验真通过）；import 仅校验 raw content_hash，
不复核 eff_is_excluded / eff_verification_status / stage2_input_hash（meta 里已存却从不比对）。
导出后人工下架的题，回灌后机器列被覆盖、compile_state(S2)=OK 且被 subagent 锁定，污染下游 S3/S4 且 force 前不再重算。
运行：.venv\\Scripts\\python.exe tests/repro/repro_v26_stage2_import_ignores_post_export_exclusion.py
期望修复后 Exit 0（被下架题应拒收并进 retry，S2 != OK）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location("_s2t", ROOT / "tests/integration/test_v26_p1_stage2_and_bridge.py")
T = importlib.util.module_from_spec(spec)
sys.modules["_s2t"] = T
spec.loader.exec_module(T)

from shuati.config import Settings  # noqa: E402
from shuati.core import effective  # noqa: E402
from shuati.db.connection import get_connection  # noqa: E402
from shuati.db.schema import init_db  # noqa: E402
from shuati.pipeline.stage2_decompose import run_stage2_decompose_async  # noqa: E402

GS = "\u5c0f\u5b66\u5965\u6570"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="repro_s2_"))
    cfg = T._setup_cfg(tmp)
    db = tmp / "s2.db"
    with closing(get_connection(db)) as conn:
        init_db(conn, config_dir=cfg)
        T._seed_anchors(db, cfg, conn)
        for i in (1, 2):
            T._seed_question(
                conn, uid=f"primary_olympiad:ch02:q{i}", source_id=f"q{i}", text=f"question {i} value 24",
                content_hash=f"hc_{i}",
            )
        conn.commit()
        tasks = tmp / "tasks"
        s = Settings()
        exp = asyncio.run(
            run_stage2_decompose_async(conn, s, grade_subject=GS, mode="export", tasks_dir=tasks, config_dir=cfg, batch_size=5)
        )
        assert exp["exported_questions"] == 2, exp
        # 导出后：q2 被人工下架（is_excluded override），并按设施惯例置 S2 STALE
        conn.execute(
            "INSERT INTO human_overrides(question_uid, field_name, override_value, reason, source, bundle_id, "
            "base_fingerprint, taxonomy_version, is_active, is_suspended, suspended_reason) "
            "VALUES ('primary_olympiad:ch02:q2', 'is_excluded', '1', 'r', 'REVIEW_UI', NULL, 'hc_2', '', 1, 0, '');"
        )
        effective.recompute(conn, "primary_olympiad:ch02:q2")
        conn.execute("UPDATE compile_state SET status='STALE' WHERE question_uid='primary_olympiad:ch02:q2' AND stage='S2';") if conn.execute(
            "SELECT 1 FROM compile_state WHERE question_uid='primary_olympiad:ch02:q2' AND stage='S2'"
        ).fetchone() else None
        conn.commit()
        excl = conn.execute("SELECT eff_is_excluded FROM questions WHERE question_uid='primary_olympiad:ch02:q2'").fetchone()[0]
        assert excl == 1

        res = tmp / "results.jsonl"
        res.write_text(
            json.dumps(
                {"batch_id": "stage2_batch_0001",
                 "result": {"items": [T._sample_stage2_item("s2_0001"), T._sample_stage2_item("s2_0002")]}},
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        imp = asyncio.run(
            run_stage2_decompose_async(conn, s, grade_subject=GS, mode="import", tasks_dir=tasks, results_path=res, config_dir=cfg)
        )
        print("import:", {k: imp[k] for k in ("ingested", "failed", "skipped_locked", "manifest_status")})
        row = conn.execute(
            "SELECT status, model_name FROM compile_state WHERE question_uid='primary_olympiad:ch02:q2' AND stage='S2'"
        ).fetchone()
        print("q2 S2 after import:", tuple(row) if row else None)
        assert row is None or row["status"] != "OK", "已下架题 q2 在 import 后被写入 S2=OK（未复核导出后状态漂移）"
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
