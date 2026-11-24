"""Repro v26: Stage 2 export 批处理只取第一题的 system prompt，导致 D-Audit-4 人工最高权威指令对同批其它题丢失/错配。

build_stage2_messages 把 `has_human_visual_override/has_human_answer_override` 对应的"必须无条件以人工核定为准、
严禁自行推翻重读"规则写在 **system** 消息里（每题一份）；`_build_batch_messages` 却只用 chunk_items[0] 的 system，
其余题的 system 被丢弃。同批 q1(无覆写) + q2(人工视觉/答案覆写) 时，整批 system 为"已验真事实约束"，
q2 的人工最高权威指令完全缺失（反向：首题有覆写则对无覆写题误称"已由人工复核核定"）。
运行：.venv\\Scripts\\python.exe tests/repro/repro_v26_stage2_batch_drops_human_authority_rule.py
期望修复后 Exit 0（批消息中必须含 q2 的人工最高权威规则，且与 q1 区分）。
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
RULE_MARK = "\u4e25\u7981\u81ea\u884c\u63a8\u7ffb\u91cd\u8bfb"  # 严禁自行推翻重读 (人工视觉事实最高权威规则)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="repro_s2b_"))
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
        ans = json.dumps([{"label": "main", "answer_type": "NUMERIC", "verify_mode": "SYMBOLIC",
                           "collection_mode": "SINGLE", "answer": "24", "sympy_expr": "24"}])
        for fname, val in (("visual_facts", "HUMAN VISUAL FACTS"), ("final_answers_json", ans)):
            conn.execute(
                "INSERT INTO human_overrides(question_uid, field_name, override_value, reason, source, bundle_id, "
                "base_fingerprint, taxonomy_version, is_active, is_suspended, suspended_reason) "
                "VALUES ('primary_olympiad:ch02:q2', ?, ?, 'r', 'REVIEW_UI', 'bnd_h', 'hc_2', '', 1, 0, '');",
                (fname, val),
            )
        effective.recompute(conn, "primary_olympiad:ch02:q2")
        conn.commit()
        st = conn.execute("SELECT eff_verification_status FROM questions WHERE question_uid='primary_olympiad:ch02:q2'").fetchone()[0]
        assert st == "HUMAN_VERIFIED", st
        tasks = tmp / "tasks"
        exp = asyncio.run(
            run_stage2_decompose_async(conn, Settings(), grade_subject=GS, mode="export", tasks_dir=tasks,
                                       config_dir=cfg, batch_size=2)
        )
        assert exp["exported_questions"] == 2 and exp["batch_count"] == 1, exp
    rec = json.loads((tasks / "batches" / "batch_0001.jsonl").read_text(encoding="utf-8").splitlines()[0])
    blob = json.dumps(rec["messages"], ensure_ascii=False)
    print("batch contains 'HUMAN VISUAL FACTS' (q2 override content):", "HUMAN VISUAL FACTS" in blob)
    print("batch contains human-authority rule:", RULE_MARK in blob)
    assert "HUMAN VISUAL FACTS" in blob
    assert RULE_MARK in blob, "同批 q2 的人工视觉事实最高权威规则在批量 export 的 system 消息中丢失（仅取首题 system）"
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
