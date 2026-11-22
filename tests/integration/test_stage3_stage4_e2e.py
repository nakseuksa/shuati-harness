"""Layer 3/4 集成测试：Stage 3a -> 3c -> 3d -> compact -> 3e -> S4 -> lock(1.0) 双通道全链路。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shuati.config import Settings
from shuati.core import effective
from shuati.core.taxonomy import transition_taxonomy
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.pipeline.stage3_taxonomy import (
    propose_taxonomy_async,
    run_compaction_async,
    run_stage3c_async,
    run_stage3d_mount,
    run_stage3e_async,
)
from shuati.pipeline.stage4_weave import run_stage4_weave_async
from shuati.vectors.store import VectorStore


def _populate_e2e_db(conn) -> None:
    samples = [
        ("primary_olympiad:ch01:q01", "已知三角形底为6厘米高为4厘米，求三角形面积", "底乘高除二模型", "平面几何", [("平面几何", "等高模型", "底乘高除二"), ("平面几何", "等高模型", "等底等高面积比")], "VERIFIED_CONSENSUS", "假设法", "等高模型"),
        ("primary_olympiad:ch01:q02", "已知三角形底为8厘米高为5厘米，求三角形面积", "底乘高除二变式", "几何图形", [("直线型几何", "面积比例", "同底等高面积转化"), ("平面几何", "等高模型", "等底等高面积比")], "VERIFIED_CONSENSUS", "假设代入法", "等高变换"),
        ("primary_olympiad:ch02:q03", "一个整数除以3余1且除以5余2，求最小正整数", "中国剩余定理同余", "数论", [("整数问题", "复杂余数生造标签", "枚举余数法")], "VERIFIED_CONSENSUS", "枚举法", "同余模型"),
        ("primary_olympiad:ch02:q04", "求能同时被4整除且除以5余3的最小两位数", "整除与余数", "数论基础", [("数论基础", "整除与同余", "整除判定"), ("数论基础", "鸡兔与动物腿数变形", "枚举余数")], "VERIFIED_CONSENSUS", "枚举法", "同余模型"),
        ("primary_olympiad:ch03:q05", "待人工复核的模糊几何图形题", "模糊题骨架", "平面几何", [("平面几何", "等高模型", "模糊算子")], "NEEDS_REVIEW", "待核方法", "等高模型"),
    ]
    for uid, text, skel, prim_l1, steps, ver_status, meth, motif in samples:
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection, source_id,
                raw_chapter, raw_question, legacy_tags_json, content_hash
            )
            VALUES (?, '小学奥数', 'BATCH_JSONL', 'secret_col', ?, 'secret_chapter_leak', ?, '{"tag":"secret_legacy"}', ?);
            """,
            (uid, uid.split(":")[-1], text, f"ch_{uid}"),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_format, m_verification_status,
                m_abstract_skeleton, m_primary_l1_module, m_c_score, m_s_score, m_t_score, m_l_score
            )
            VALUES (?, '小学奥数', ?, '解答题', ?, ?, ?, 2, 2, 2, 2);
            """,
            (uid, text, ver_status, skel, prim_l1),
        )
        conn.execute(
            "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解');",
            (f"{uid}::branch_1", uid),
        )
        for s_idx, (rl1, rl2, rl3) in enumerate(steps, start=1):
            conn.execute(
                """
                INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, raw_l1, raw_l2, raw_l3)
                VALUES (?, ?, 'branch_1', ?, '步骤', ?, ?, ?);
                """,
                (f"{uid}::branch_1::{s_idx}", uid, s_idx, rl1, rl2, rl3),
            )
        cm_id = f"METHOD:{meth}"
        co_id = f"MOTIF:{motif}"
        conn.execute("INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed) VALUES (?, 'METHOD', ?, 0);", (cm_id, meth))
        conn.execute("INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed) VALUES (?, 'MOTIF', ?, 0);", (co_id, motif))
        conn.execute("INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'METHOD', 'MACHINE');", (uid, cm_id))
        conn.execute("INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'MOTIF', 'MACHINE');", (uid, co_id))
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, 'S1', 'OK'), (?, 'S2', 'OK'), (?, 'S3C', 'STALE'), (?, 'S3D', 'STALE'), (?, 'S4', 'STALE');",
            (uid, uid, uid, uid, uid),
        )
        effective.recompute(conn, uid)
    conn.commit()


@pytest.mark.asyncio
async def test_e2e_draft_pipeline_and_dual_channel_sanitization(tmp_path: Path) -> None:
    db_path = tmp_path / "e2e.db"
    cfg_dir = tmp_path / "config"
    vec_dir = tmp_path / "vectors"
    conn = get_connection(db_path)
    init_db(conn, cfg_dir)
    _populate_e2e_db(conn)

    settings = Settings()
    settings.pipeline.use_mock_llm = True

    # 1. Stage 3a propose(DRAFT)
    p_res = await propose_taxonomy_async(
        conn, settings, grade_subject="小学奥数", mode="api", config_dir=cfg_dir, use_mock=True, n_clusters=3
    )
    assert p_res["taxonomy_status"] == "DRAFT"
    assert Path(p_res["taxonomy_file"]).exists()

    # 2. 双通道 export/import 测试（验证零 uid / 章节 / collection 泄露 + 回灌等价）
    export_dir = tmp_path / "unified_3c"
    exp_res = await run_stage3c_async(
        conn, settings, grade_subject="小学奥数", mode="export", batch_size=2, tasks_dir=export_dir, lock_path=tmp_path / ".lock"
    )
    assert exp_res["exported_questions"] >= 1
    batch_files = sorted((export_dir / "batches").glob("batch_*.jsonl"))
    assert batch_files
    for bf in batch_files:
        txt = bf.read_text(encoding="utf-8")
        assert "primary_olympiad:ch" not in txt
        assert "secret_chapter_leak" not in txt
        assert "secret_col" not in txt
        assert "secret_legacy" not in txt

    # 构造合规与一条坏结果检验 import 与 retry_tasks.jsonl 生成
    results_file = tmp_path / "3c_results.jsonl"
    result_lines: list[str] = []
    for bf in batch_files:
        for line in bf.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            items = [
                {
                    "task_item_id": tid,
                    "primary_l1_module": "数论基础",
                    "secondary_l1_modules": [],
                    "steps": [{"step_index": 1, "mapped_l1": "数论基础", "mapped_l2": "整除与同余", "is_novel_l2": False}],
                }
                for tid in rec["task_item_ids"]
            ]
            result_lines.append(json.dumps({"batch_id": rec["batch_id"], "result": {"items": items}}, ensure_ascii=False))
    # 追加一条校验失败记录验证 retry_tasks.jsonl 落盘
    result_lines.append('{"batch_id": "bad_batch", "result": {"wrong_key": 123}}')
    results_file.write_text("\n".join(result_lines) + "\n", encoding="utf-8")

    imp_res = await run_stage3c_async(
        conn, settings, grade_subject="小学奥数", mode="import", tasks_dir=export_dir, results_path=results_file, lock_path=tmp_path / ".lock"
    )
    assert imp_res["updated"] >= 1
    assert imp_res["batch_errors"] == 1
    assert (export_dir / "retry_tasks.jsonl").exists()

    # 3. 完整运行 api 模式 S3D -> compact -> S3E -> S4
    s3d_res = run_stage3d_mount(conn, grade_subject="小学奥数", lock_path=tmp_path / ".lock")
    assert s3d_res["mounted_questions"] == 5

    store = VectorStore(vec_dir)
    cmp_res = await run_compaction_async(
        conn, settings, store, grade_subject="小学奥数", mode="api", use_mock=True, lock_path=tmp_path / ".lock"
    )
    assert cmp_res["candidate_pairs"] >= 1

    s3e_res = await run_stage3e_async(conn, settings, grade_subject="小学奥数", mode="api", lock_path=tmp_path / ".lock")
    assert s3e_res["broken_cycles_edges"] == 0

    s4_res = await run_stage4_weave_async(
        conn, settings, grade_subject="小学奥数", mode="api", vectors_dir=vec_dir, use_mock=True, lock_path=tmp_path / ".lock"
    )
    assert s4_res["eligible_questions"] == 4
    assert s4_res["blocked_unservable"] == 1
    q5_s4 = conn.execute("SELECT status FROM compile_state WHERE question_uid = 'primary_olympiad:ch03:q05' AND stage = 'S4';").fetchone()["status"]
    assert q5_s4 == "BLOCKED"

    # 4. 锁定 1.0 后 S3C/S3D/S4 全置 STALE，并可无错全量重编
    lock_res = transition_taxonomy(
        conn,
        grade_subject="小学奥数",
        yaml_path=p_res["taxonomy_file"],
        target_version="1.0",
        target_status="LOCKED",
        config_dir=cfg_dir,
        backups_dir=tmp_path / "backups",
        human_dir=tmp_path / "human",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".lock",
    )
    assert lock_res["target_status"] == "LOCKED"
    assert lock_res["stale_questions"] == 5

    await run_stage3c_async(conn, settings, grade_subject="小学奥数", mode="api", lock_path=tmp_path / ".lock")
    run_stage3d_mount(conn, grade_subject="小学奥数", lock_path=tmp_path / ".lock")
    await run_stage3e_async(conn, settings, grade_subject="小学奥数", mode="api", lock_path=tmp_path / ".lock")
    s4_v1 = await run_stage4_weave_async(
        conn, settings, grade_subject="小学奥数", mode="api", vectors_dir=vec_dir, use_mock=True, lock_path=tmp_path / ".lock"
    )
    assert s4_v1["eligible_questions"] == 4
    conn.close()
