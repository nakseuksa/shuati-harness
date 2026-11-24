"""v2.6 P1 Stage 2 回归测试：吸收 B2（批处理逐题 D-Audit-4 权威隔离）与 B3（import 导出后状态与指纹漂移复核）。"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from shuati.config import Settings
from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.pipeline.stage2_decompose import run_stage2_decompose_async
from shuati.pipeline.unified_import import register_anchors_from_yaml


def _setup_cfg(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("settings.yaml", "subjects.yaml", "method_blacklist.yaml"):
        src = Path("config") / fname
        if src.exists():
            txt = src.read_text(encoding="utf-8")
            if fname == "settings.yaml":
                txt = txt.replace('dir: "human/"', f'dir: "{(tmp_path / "human").as_posix()}"')
            (cfg_dir / fname).write_text(txt, encoding="utf-8")
    prompts_src = Path("config/prompts")
    if prompts_src.is_dir():
        for fp in prompts_src.rglob("*"):
            if fp.is_file():
                rel = fp.relative_to(prompts_src)
                dst = cfg_dir / "prompts" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(fp.read_text(encoding="utf-8"), encoding="utf-8")
    return cfg_dir


def _seed_question(
    conn,
    *,
    uid: str,
    source_id: str,
    text: str,
    content_hash: str,
    has_images: bool = False,
    cas_images: list[str] | None = None,
) -> None:
    ans_json = json.dumps(
        [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "24",
                "sympy_expr": "24",
            }
        ],
        ensure_ascii=False,
    )
    cas_json = json.dumps(cas_images or [], ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id,
            raw_chapter, raw_question, cas_images_json, legacy_tags_json, content_hash
        ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'secret_col', ?, 'secret_chapter_leak', ?, ?, '{}', ?)
        """,
        (uid, source_id, text, cas_json, content_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, cas_images_json, has_images,
            m_question_text, m_format, m_visual_facts, m_verification_status, m_final_answers_json
        ) VALUES (?, '小学奥数', ?, ?, ?, '解答题', '', 'VERIFIED_CONSENSUS', ?)
        """,
        (uid, cas_json, 1 if (has_images or cas_images) else 0, text, ans_json),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin,
            method_summary, solution_markdown, sympy_verified
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '等高面积转化', '解：面积为 24。', 1)
        """,
        (f"{uid}::branch_1", uid),
    )
    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status, input_hash) VALUES (?, 'S1', 'OK', ?)",
        (uid, content_hash),
    )
    effective.recompute(conn, uid)


def _seed_anchors(db_path: Path, cfg_dir: Path, conn) -> None:
    anchors = [f"primary_olympiad:ch_anc:a{i}" for i in range(1, 5)]
    for i, uid in enumerate(anchors, 1):
        _seed_question(
            conn,
            uid=uid,
            source_id=f"a{i}",
            text=f"锚点题 {i}：求面积",
            content_hash=f"ha_{i}",
        )
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status, input_hash, model_name) VALUES (?, 'S2', 'OK', ?, 'subagent-unified')",
            (uid, f"ha_{i}"),
        )
    conn.commit()

    anchors_path = cfg_dir / "anchors" / "anchors_primary_olympiad.yaml"
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    tiers = [
        ("L1_基础直达", 1, 1, 1, 1, 1120),
        ("L3_综合中档", 2, 2, 2, 1, 1510),
        ("L4-A_进阶拔高(繁)", 2, 3, 2, 3, 1745),
        ("L5-2_竞赛超难", 4, 2, 2, 1, 2530),
    ]
    lines = ["grade_subject: 小学奥数", 'version: "1.0"', "anchors:"]
    for uid, (tier, c, s, t, l, elo) in zip(anchors, tiers, strict=True):
        lines.extend(
            [
                f'  - question_uid: "{uid}"',
                f'    target_tier: "{tier}"',
                "    scores:",
                f"      C: {c}",
                f"      S: {s}",
                f"      T: {t}",
                f"      L: {l}",
                f"    base_elo: {elo}",
                f'    rationale: "校准 {tier}"',
            ]
        )
    anchors_path.write_text("\n".join(lines), encoding="utf-8")
    register_anchors_from_yaml(db_path=db_path, config_dir=cfg_dir, anchors_file=anchors_path)


def _sample_stage2_item(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "steps": [
            {
                "step_index": 1,
                "sub_goal": "确定底边比例并转化面积关系",
                "core_formula": "S_1 / S_2 = b_1 / b_2",
                "hint": "先观察共顶点三角形的高是否相同",
                "raw_l1": "平面几何",
                "raw_l2": "等高模型",
                "raw_l3": "等高三角形面积比",
            }
        ],
        "abstract_skeleton": "利用几何面积比建立方程求解目标面积",
        "primary_l1_module": "平面几何",
        "secondary_l1_modules": [],
        "thinking_methods": ["数形结合"],
        "classic_motifs": ["等高模型"],
        "trap_tags": [],
        "difficulty": {
            "C_concept_count": {"evidence": "单一等高模型转化", "score": 2},
            "S_chain_length": {"evidence": "两步比例换算", "score": 1},
            "T_trickiness": {"evidence": "图形条件明晰无陷阱", "score": 1},
            "L_computation_load": {"evidence": "基础整数比例运算", "score": 1},
        },
    }


@pytest.mark.asyncio
async def test_b2_stage2_batch_isolates_per_item_human_authority(tmp_path: Path) -> None:
    cfg = _setup_cfg(tmp_path)
    db = tmp_path / "b2.db"
    assets_dir = tmp_path / "assets" / "images"
    assets_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), color=(255, 0, 0)).save(assets_dir / "q2.jpg", format="JPEG")

    with closing(get_connection(db)) as conn:
        init_db(conn, config_dir=cfg)
        _seed_anchors(db, cfg, conn)

        _seed_question(
            conn,
            uid="primary_olympiad:ch02:q1",
            source_id="q1",
            text="无覆写题目 1，求数值 24。",
            content_hash="hc_1",
        )
        _seed_question(
            conn,
            uid="primary_olympiad:ch02:q2",
            source_id="q2",
            text="有人工覆写题目 2，求面积。",
            content_hash="hc_2",
            has_images=True,
            cas_images=["q2.jpg"],
        )
        ans = json.dumps(
            [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "24",
                    "sympy_expr": "24",
                }
            ],
            ensure_ascii=False,
        )
        for fname, val in (("visual_facts", "HUMAN VISUAL FACTS"), ("final_answers_json", ans)):
            conn.execute(
                """
                INSERT INTO human_overrides(
                    question_uid, field_name, override_value, reason, source, bundle_id,
                    base_fingerprint, taxonomy_version, is_active, is_suspended, suspended_reason
                ) VALUES ('primary_olympiad:ch02:q2', ?, ?, 'r', 'REVIEW_UI', 'bnd_h', 'hc_2', '', 1, 0, '')
                """,
                (fname, val),
            )
        effective.recompute(conn, "primary_olympiad:ch02:q2")
        conn.commit()

        tasks_dir = tmp_path / "tasks_1"
        exp = await run_stage2_decompose_async(
            conn,
            Settings(),
            grade_subject="小学奥数",
            mode="export",
            only_uids=["primary_olympiad:ch02:q1", "primary_olympiad:ch02:q2"],
            tasks_dir=tasks_dir,
            config_dir=cfg,
            assets_dir=assets_dir,
            batch_size=2,
        )
        assert exp["exported_questions"] == 2
        assert exp["batch_count"] == 1

        rec = json.loads((tasks_dir / "batches" / "batch_0001.jsonl").read_text(encoding="utf-8").splitlines()[0])
        sys_msg = rec["messages"][0]["content"]
        user_blob = json.dumps(rec["messages"][1]["content"], ensure_ascii=False)
        assert "逐题 D-Audit-4 权威隔离总原则" in sys_msg
        assert "严禁自行推翻重读" in sys_msg
        assert "HUMAN VISUAL FACTS" in user_blob
        assert "[task_id=s2_0001] 【已验真事实约束】" in user_blob
        assert "[task_id=s2_0002] 【D-Audit-4 人工视觉事实最高权威】" in user_blob
        assert "[task_id=s2_0002] 【D-Audit-4 人工标准答案最高权威】" in user_blob

        # 反向校验：即使首题（若仅导出 q2 为首题且批内含无覆写题），system 与 user 亦不得误传人工覆写声明
        tasks_rev = tmp_path / "tasks_rev"
        exp_rev = await run_stage2_decompose_async(
            conn,
            Settings(),
            grade_subject="小学奥数",
            mode="export",
            only_uids=["primary_olympiad:ch02:q2", "primary_olympiad:ch02:q1"],
            tasks_dir=tasks_rev,
            config_dir=cfg,
            assets_dir=assets_dir,
            batch_size=2,
        )
        assert exp_rev["exported_questions"] == 2
        rec_rev = json.loads(
            (tasks_rev / "batches" / "batch_0001.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        sys_rev = rec_rev["messages"][0]["content"]
        user_rev = json.dumps(rec_rev["messages"][1]["content"], ensure_ascii=False)
        assert "本题的 `verified_visual_facts` 已由人工复核核定" not in sys_rev
        assert "[task_id=s2_0001] 【已验真事实约束】" in user_rev
        assert "[task_id=s2_0002] 【D-Audit-4 人工视觉事实最高权威】" in user_rev


@pytest.mark.asyncio
async def test_b3_stage2_import_rejects_post_export_exclusion_and_hash_drift(tmp_path: Path) -> None:
    cfg = _setup_cfg(tmp_path)
    db = tmp_path / "b3.db"

    with closing(get_connection(db)) as conn:
        init_db(conn, config_dir=cfg)
        _seed_anchors(db, cfg, conn)
        for idx in (1, 2, 3):
            _seed_question(
                conn,
                uid=f"primary_olympiad:ch02:q{idx}",
                source_id=f"q{idx}",
                text=f"题目 {idx}，求数值 24。",
                content_hash=f"hc_{idx}",
            )
        conn.commit()

        tasks = tmp_path / "tasks"
        settings = Settings()
        exp = await run_stage2_decompose_async(
            conn,
            settings,
            grade_subject="小学奥数",
            mode="export",
            only_uids=[
                "primary_olympiad:ch02:q1",
                "primary_olympiad:ch02:q2",
                "primary_olympiad:ch02:q3",
            ],
            tasks_dir=tasks,
            config_dir=cfg,
            batch_size=5,
        )
        assert exp["exported_questions"] == 3

        # 导出后：q2 被人工下架（is_excluded=1）；q3 被人工修改 visual_facts+final_answers_json（保持 HUMAN_VERIFIED 但输入指纹漂移）
        ans3 = json.dumps(
            [
                {
                    "label": "主问",
                    "answer_type": "NUMERIC",
                    "verify_mode": "SYMBOLIC",
                    "collection_mode": "SINGLE",
                    "answer": "48",
                    "sympy_expr": "48",
                }
            ],
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT INTO human_overrides(
                question_uid, field_name, override_value, reason, source, bundle_id,
                base_fingerprint, taxonomy_version, is_active, is_suspended, suspended_reason
            ) VALUES
                ('primary_olympiad:ch02:q2', 'is_excluded', '1', 'r', 'REVIEW_UI', NULL, 'hc_2', '', 1, 0, ''),
                ('primary_olympiad:ch02:q3', 'visual_facts', '导出后人工新补充视觉事实', 'r', 'REVIEW_UI', 'bnd_3', 'hc_3', '', 1, 0, ''),
                ('primary_olympiad:ch02:q3', 'final_answers_json', ?, 'r', 'REVIEW_UI', 'bnd_3', 'hc_3', '', 1, 0, '')
            """,
            (ans3,),
        )
        effective.recompute(conn, "primary_olympiad:ch02:q2")
        effective.recompute(conn, "primary_olympiad:ch02:q3")
        conn.commit()

        res_file = tmp_path / "results.jsonl"
        res_file.write_text(
            json.dumps(
                {
                    "batch_id": "stage2_batch_0001",
                    "result": {
                        "items": [
                            _sample_stage2_item("s2_0001"),
                            _sample_stage2_item("s2_0002"),
                            _sample_stage2_item("s2_0003"),
                        ]
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        imp = await run_stage2_decompose_async(
            conn,
            settings,
            grade_subject="小学奥数",
            mode="import",
            tasks_dir=tasks,
            results_path=res_file,
            config_dir=cfg,
        )
        assert imp["ingested"] == 1
        assert imp["failed"] == 2
        assert imp["manifest_status"] == "PENDING_IMPORT"
        err_codes = {e["question_uid"]: e["error_code"] for e in imp["errors"]}
        assert err_codes["primary_olympiad:ch02:q2"] == "INELIGIBLE_POST_EXPORT"
        assert err_codes["primary_olympiad:ch02:q3"] == "STALE_STAGE2_INPUT_HASH"

        for blocked_uid in ("primary_olympiad:ch02:q2", "primary_olympiad:ch02:q3"):
            cs2 = conn.execute(
                "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S2'",
                (blocked_uid,),
            ).fetchone()
            assert cs2 is None or cs2["status"] != "OK"

        cs1 = conn.execute(
            "SELECT status FROM compile_state WHERE question_uid = 'primary_olympiad:ch02:q1' AND stage = 'S2'"
        ).fetchone()
        assert cs1 is not None and cs1["status"] == "OK"
