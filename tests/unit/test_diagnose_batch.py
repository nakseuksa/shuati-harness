"""批量外部错题 Markdown 解析、断点续跑、重复题提示、Markdown 汇总报告与 CLI 集成测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from shuati.config import Settings
from shuati.core import effective
from shuati.core.text_norm import compute_plain_fingerprint, normalize_text
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.services.diagnose_batch import (
    diagnose_external_file_async,
    parse_question_list_markdown,
    render_batch_markdown_report,
)


def _run_cli(args: list[str], cwd: Path) -> tuple[int, dict]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(
        [sys.executable, "-m", "shuati.cli.main", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout.strip(), f"CLI stdout 为空, stderr={proc.stderr}"
    payload = json.loads(proc.stdout.strip())
    return proc.returncode, payload


def _seed_db(tmp_path: Path, duplicate_text: str) -> tuple[Path, Path]:
    db_path = tmp_path / "data" / "shuati.db"
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    repo_cfg = Path("config")
    (cfg_dir / "settings.yaml").write_text(
        (repo_cfg / "settings.yaml").read_text(encoding="utf-8").replace("use_mock_llm: false", "use_mock_llm: true"),
        encoding="utf-8",
    )
    if (repo_cfg / "method_blacklist.yaml").exists():
        (cfg_dir / "method_blacklist.yaml").write_text(
            (repo_cfg / "method_blacklist.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)
    norm_dup = normalize_text(duplicate_text).normalized_text
    plain_fp = compute_plain_fingerprint(norm_dup, 1)
    uid = "primary_olympiad:ch01:q_dup"
    fa = [
        {
            "label": "主问",
            "answer_type": "NUMERIC",
            "verify_mode": "SYMBOLIC",
            "collection_mode": "SINGLE",
            "answer": "24",
            "sympy_expr": "24",
        }
    ]
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id,
            raw_question, content_hash, plain_fingerprint
        ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', 'ch01_001', ?, 'h_dup', ?);
        """,
        (uid, norm_dup, plain_fp),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format, m_verification_status,
            m_visual_facts, m_abstract_skeleton, m_primary_l1_module,
            m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json, has_images
        ) VALUES (?, '小学奥数', ?, '解答题', 'VERIFIED_CONSENSUS', '图中有直角三角形', '等高面积模型', '平面几何', 2, 1, 1, 1, ?, 1);
        """,
        (uid, norm_dup, json.dumps(fa, ensure_ascii=False)),
    )
    for stg in ("S1", "S2", "S3C", "S3D", "S4"):
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, 'OK');",
            (uid, stg),
        )
    effective.recompute(conn, uid)
    conn.commit()
    conn.close()
    return db_path, cfg_dir


def test_parse_question_list_markdown_headings_hr_and_images(tmp_path: Path) -> None:
    img_dir = tmp_path / "imgs"
    img_dir.mkdir()
    img1 = img_dir / "fig1.png"
    img2 = img_dir / "fig2.jpg"
    Image.new("RGB", (20, 20), color=(255, 0, 0)).save(img1, format="PNG")
    Image.new("RGB", (20, 20), color=(0, 255, 0)).save(img2, format="JPEG")

    md_file = tmp_path / "mistakes.md"
    md_file.write_text(
        (
            "# 本周错题清单\n\n"
            "## 第1题 行程相遇\n"
            "甲乙两车相向而行：\n"
            "(1) 求相遇时间；\n"
            "(2) 求相遇点距离。\n\n"
            "---\n\n"
            "如图，求阴影部分面积。\n"
            "![阴影图](imgs/fig1.png \"示意图\")\n"
            f"图片：{img2.resolve()}\n\n"
            "### 第3题 选择题\n"
            "下列哪个数是质数？\n"
            "A. 9\n"
            "B. 11\n"
            "图片: imgs/missing.png\n"
        ),
        encoding="utf-8",
    )

    items = parse_question_list_markdown(md_file)
    assert len(items) == 3

    assert items[0].index == 1
    assert items[0].title == "第1题 行程相遇"
    assert "(1) 求相遇时间；" in items[0].question_text
    assert "(2) 求相遇点距离。" in items[0].question_text
    assert items[0].image_paths == []

    assert items[1].index == 2
    assert items[1].title == "第 2 题"
    assert items[1].question_text == "如图，求阴影部分面积。"
    assert items[1].image_paths == [img1.resolve(), img2.resolve()]

    assert items[2].index == 3
    assert items[2].title == "第3题 选择题"
    assert "B. 11" in items[2].question_text
    assert "missing.png" not in items[2].question_text
    assert items[2].image_paths == [(tmp_path / "imgs" / "missing.png").resolve()]

    with pytest.raises(PreconditionError, match="找不到题目列表文件"):
        parse_question_list_markdown(tmp_path / "nope.md")


@pytest.mark.asyncio
async def test_diagnose_batch_reuse_duplicate_and_partial_failure(tmp_path: Path) -> None:
    dup_text = "如图，已知矩形长8宽6，求对角线分割出的三角形面积。"
    db_path, cfg_dir = _seed_db(tmp_path, dup_text)

    img_path = tmp_path / "tri.png"
    Image.new("RGB", (40, 30), color=(0, 128, 255)).save(img_path, format="PNG")

    md_file = tmp_path / "list.md"
    md_file.write_text(
        (
            "## 题A（纯文本）\n"
            "一个数加上 15 等于 42，求这个数。\n\n"
            "## 题B（带图+库内精确重复）\n"
            f"{dup_text}\n"
            "![](tri.png)\n\n"
            "## 题C（缺图应单题失败）\n"
            "求图中角度。\n"
            "图片: not_found.png\n"
        ),
        encoding="utf-8",
    )

    drafts_dir = tmp_path / "drafts"
    vec_dir = tmp_path / "vectors"
    settings = Settings()
    settings.pipeline.use_mock_llm = True

    conn = get_connection(db_path, readonly=True)
    try:
        # 第 1 次运行：2 成功 + 1 失败（缺图不阻断）
        res1 = await diagnose_external_file_async(
            conn,
            settings,
            file_path=md_file,
            grade_subject="小学奥数",
            concurrency=2,
            force=False,
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )
        assert res1["mode"] == "batch"
        assert res1["total"] == 3
        assert res1["succeeded"] == 2
        assert res1["reused"] == 0
        assert res1["failed"] == 1
        assert len(res1["warnings"]) == 1

        # 校验每题状态与库内重复提示
        assert res1["items"][0]["status"] == "OK"
        assert res1["items"][1]["status"] == "OK"
        dups_b = res1["items"][1]["result"]["duplicate_candidates"]
        assert len(dups_b) == 1
        assert dups_b[0]["source_id"] == "ch01_001"
        assert dups_b[0]["match_type"] == "EXACT_FINGERPRINT"
        assert res1["items"][2]["status"] == "FAILED"
        assert res1["items"][2]["error"]["code"] == "PRECONDITION_FAILED"

        # 校验 JSON 与可读 Markdown 汇总报告双落盘
        batch_json = Path(res1["batch_json_file"])
        batch_md = Path(res1["batch_markdown_file"])
        assert batch_json.exists() and batch_md.exists()
        md_content = batch_md.read_text(encoding="utf-8")
        assert "题A（纯文本）" in md_content
        assert "ch01_001" in md_content
        assert "FAILED" in md_content
        assert res1["items"][0]["result"]["draft_id"] in md_content

        # 第 2 次运行（不带 --force）：自动复用 2 份草稿，不重跑 VLM
        res2 = await diagnose_external_file_async(
            conn,
            settings,
            file_path=md_file,
            grade_subject="小学奥数",
            concurrency=1,
            force=False,
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )
        assert res2["succeeded"] == 0
        assert res2["reused"] == 2
        assert res2["failed"] == 1
        assert res2["items"][0]["status"] == "REUSED"
        assert res2["items"][0]["result"]["draft_id"] == res1["items"][0]["result"]["draft_id"]
        assert res2["items"][1]["result"]["draft_id"] == res1["items"][1]["result"]["draft_id"]

        # 第 3 次运行（带 --force）：强制重跑生成新 draft_id
        res3 = await diagnose_external_file_async(
            conn,
            settings,
            file_path=md_file,
            grade_subject="小学奥数",
            concurrency=1,
            force=True,
            config_dir=cfg_dir,
            vectors_dir=vec_dir,
            drafts_dir=drafts_dir,
            use_mock=True,
        )
        assert res3["succeeded"] == 2
        assert res3["reused"] == 0
        assert res3["items"][0]["result"]["draft_id"] != res1["items"][0]["result"]["draft_id"]
    finally:
        conn.close()


def test_cli_diagnose_file_and_mutual_exclusion(tmp_path: Path) -> None:
    db_path, cfg_dir = _seed_db(tmp_path, "已知矩形长8宽6")
    repo_root = Path.cwd()
    drafts_dir = tmp_path / "drafts"
    vec_dir = tmp_path / "vectors"

    md_file = tmp_path / "batch.md"
    md_file.write_text("第一道题干\n\n---\n\n第二道题干\n", encoding="utf-8")

    # 1. 正常 --file 批量诊断
    rc, res = _run_cli(
        [
            "diagnose",
            "--file",
            str(md_file),
            "--grade-subject",
            "小学奥数",
            "--mock",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--vectors-dir",
            str(vec_dir),
            "--drafts-dir",
            str(drafts_dir),
            "--json",
        ],
        repo_root,
    )
    assert rc == 0 and res["ok"] is True
    assert res["data"]["mode"] == "batch"
    assert res["data"]["total"] == 2 and res["data"]["succeeded"] == 2
    assert Path(res["data"]["batch_markdown_file"]).exists()

    # 2. --text 与 --file 互斥 -> exit code 2
    rc_both, err_both = _run_cli(
        [
            "diagnose",
            "--text",
            "测试",
            "--file",
            str(md_file),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
        repo_root,
    )
    assert rc_both == 2 and err_both["ok"] is False and err_both["error"]["code"] == "PRECONDITION_FAILED"

    # 3. 全军覆没 -> 落盘汇总报告且返回非 0
    bad_file = tmp_path / "all_bad.md"
    bad_file.write_text("缺图题干\n图片: does_not_exist.png\n", encoding="utf-8")
    rc_bad, err_bad = _run_cli(
        [
            "diagnose",
            "--file",
            str(bad_file),
            "--mock",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--vectors-dir",
            str(vec_dir),
            "--drafts-dir",
            str(drafts_dir),
            "--json",
        ],
        repo_root,
    )
    assert rc_bad == 2 and err_bad["ok"] is False and "全部 1 道题均失败" in err_bad["error"]["message"]


def test_render_batch_markdown_cn_traps_and_rec_original_questions(tmp_path: Path) -> None:
    db_path, cfg_dir = _seed_db(tmp_path, "如图，已知矩形长8宽6，求对角线分割出的三角形面积。")
    conn = get_connection(db_path)
    try:
        # 新增一道带选项的无图选择题，验证选项渲染与 DB 兜底补全
        uid_choice = "primary_olympiad:ch01:q_choice"
        fa = [
            {
                "label": "主问",
                "answer_type": "CHOICE",
                "verify_mode": "EXACT",
                "collection_mode": "SINGLE",
                "answer": "B",
                "choice_label": "B",
            }
        ]
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection, source_id,
                raw_question, content_hash, plain_fingerprint
            ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch01', 'ch01_002', '下列哪个数是质数？', 'h_ch', 'fp_ch');
            """,
            (uid_choice,),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_options_json, m_format, m_verification_status,
                m_primary_l1_module, m_c_score, m_s_score, m_t_score, m_l_score, m_final_answers_json, has_images
            ) VALUES (?, '小学奥数', '下列哪个数是质数？', '{"A": "9", "B": "11"}', '选择题', 'VERIFIED_CONSENSUS',
                      '数论', 1, 1, 1, 1, ?, 0);
            """,
            (uid_choice, json.dumps(fa, ensure_ascii=False)),
        )
        effective.recompute(conn, uid_choice)
        conn.commit()

        summary = {
            "batch_id": "batch_test_cn",
            "source_file": "test.md",
            "grade_subject": "小学奥数",
            "created_at": "2026-11-23 10:45:00 CST",
            "total": 1,
            "succeeded": 1,
            "reused": 0,
            "failed": 0,
            "items": [
                {
                    "index": 1,
                    "title": "示例题",
                    "question_text": "原错题题干",
                    "status": "OK",
                    "result": {
                        "draft_id": "drf_demo123456",
                        "tier": "L3_综合中档",
                        "base_elo": 1510,
                        "primary_l1_module": "行程问题",
                        "matched_l2": ["相遇与追及基础模型"],
                        "final_answers": [{"label": "主问", "answer": "10", "unit": "米"}],
                        "method_summary": "列方程求解。",
                        "trap_tags": ["TRAP_FRAME_SHIFT", "TRAP_SIGN_DIRECTION", "TRAP_FRAME_SHIFT", "CUSTOM_TRAP"],
                        "duplicate_candidates": [],
                        "recommendations": {
                            "scaffold_foundations": [
                                {
                                    "question_uid": "primary_olympiad:ch01:q_dup",
                                    "source_id": "ch01_001",
                                    "question_text": "如图，已知矩形长8宽6，求对角线分割出的三角形面积。",
                                    "base_elo": 1225,
                                    "tier": "L2_常规巩固",
                                    "similarity": 0.85,
                                }
                            ],
                            "isomorphic_variants": [
                                {
                                    "question_uid": uid_choice,
                                    "source_id": "ch01_002",
                                    "question_text": "下列哪个数是质数？",
                                    "base_elo": 1510,
                                    "tier": "L3_综合中档",
                                    "similarity": 0.88,
                                }
                            ],
                            "contrastive_and_traps": [
                                {
                                    "question_uid": "primary_olympiad:ch01:q_inline",
                                    "source_id": "ch01_003",
                                    "question_text": "第一行题干\n第二行题干",
                                    "has_images": False,
                                    "base_elo": 1575,
                                    "tier": "L3_综合中档",
                                    "similarity": 0.91,
                                }
                            ],
                        },
                    },
                }
            ],
        }

        md = render_batch_markdown_report(summary, conn=conn, config_dir=cfg_dir)

        # 1. 易错陷阱中文名称行内拼接（去重保序，不含已映射英文 code，未知 code 原样回退）
        assert "- **易错陷阱**：参考系转换与基准漂移、符号方向与正负歧义、CUSTOM_TRAP" in md
        assert "TRAP_FRAME_SHIFT" not in md
        assert "TRAP_SIGN_DIRECTION" not in md

        # 2. 三类推荐题均展开纯文本原题、选项与 [含配图] 标注，不嵌入图片语法
        assert "- **推荐前置铺垫题**：" in md
        assert "1. `ch01_001`（L2_常规巩固 / 1225 Elo，sim=0.85） [含配图]" in md
        assert "     > 如图，已知矩形长8宽6，求对角线分割出的三角形面积。" in md
        assert "- **推荐同构变式题**：" in md
        assert "1. `ch01_002`（L3_综合中档 / 1510 Elo，sim=0.88）\n" in md
        assert "     > A. 9" in md and "     > B. 11" in md
        assert "- **推荐辨析/同坑排雷题**：" in md
        assert "     > 第一行题干\n     > 第二行题干" in md
        assert "![" not in md
    finally:
        conn.close()
