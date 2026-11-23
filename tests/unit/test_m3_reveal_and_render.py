"""M3 单元测试：reveal 防剧透四档裁剪与 sheet_renderer 离线 KaTeX/双模式练习单。"""

from __future__ import annotations

from pathlib import Path

from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.render.sheet_renderer import render_practice_sheet
from shuati.services.reveal import apply_check_reveal, apply_related_reveal, apply_reveal


def test_reveal_modes_strict_anti_spoiler() -> None:
    full_q = {
        "question_uid": "primary_olympiad:ch01:q01",
        "source_id": "ch01_001",
        "grade_subject": "小学奥数",
        "format": "解答题",
        "question_text": "求面积",
        "options": {},
        "has_images": True,
        "cas_images": ["abc.png"],
        "image_paths": ["/abs/abc.png"],
        "image_captions": ["图1"],
        "base_elo": 1520,
        "tier": "L3_综合中档",
        "tier_rank": 3,
        "primary_l1_module": "平面几何",
        "verification_status": "VERIFIED_CONSENSUS",
        "is_servable": True,
        "reason_public": "稳固梯度 · 匹配当前水位",
        "reason_full": "沿 SCAFFOLD 边从 ch01_000 升维",
        "visual_facts": "直角三角形 ABC 中 D 为斜边中点",
        "abstract_skeleton": "直角三角形斜边中线性质",
        "final_answers": [{"label": "主问", "answer": "24"}],
        "solutions": [
            {
                "branch_id": "branch_1",
                "is_optimal": True,
                "steps": [
                    {"step_index": 1, "sub_goal": "连中线", "core_formula": "CD=AB/2", "hint": "关注斜边特殊点性质"},
                    {"step_index": 2, "sub_goal": "求面积", "core_formula": "S=ab/2", "hint": "利用面积比例得出结果"},
                ],
            }
        ],
        "l2_entities": ["直角三角形"],
        "l3_nodes": [{"node_id": "n1"}],
        "thinking_methods": ["构造法"],
        "classic_motifs": ["斜边中线"],
        "trap_tags": ["TRAP_DOMAIN_BOUNDARY"],
        "pedagogical_note": "先画高再找比例",
    }

    q_quiz = apply_reveal(full_q, reveal="quiz")
    assert "visual_facts" not in q_quiz
    assert "final_answers" not in q_quiz
    assert "solutions" not in q_quiz
    assert "hints" not in q_quiz
    assert "l2_entities" not in q_quiz
    assert "pedagogical_note" not in q_quiz
    assert "reason_full" not in q_quiz
    assert q_quiz["reason_public"] == "稳固梯度 · 匹配当前水位"

    q_quiz_vf = apply_reveal(full_q, reveal="quiz", include_visual_facts=True)
    assert q_quiz_vf["visual_facts"] == full_q["visual_facts"]
    assert "final_answers" not in q_quiz_vf

    q_hint = apply_reveal(full_q, reveal="hint", hint_level=1)
    assert q_hint["visual_facts"] == full_q["visual_facts"]
    assert len(q_hint["hints"]) == 1
    assert q_hint["hints"][0] == {"step_index": 1, "hint": "关注斜边特殊点性质"}
    assert "sub_goal" not in q_hint["hints"][0]
    assert "core_formula" not in q_hint["hints"][0]
    assert "final_answers" not in q_hint

    q_full = apply_reveal(full_q, reveal="full")
    assert q_full["final_answers"] == full_q["final_answers"]
    assert q_full["reason_full"] == full_q["reason_full"]

    rel_quiz = apply_related_reveal({"relation_type": "CONTRASTIVE", "pedagogical_note": "剧透说明", "question": full_q}, reveal="quiz")
    assert "pedagogical_note" not in rel_quiz
    assert "final_answers" not in rel_quiz["question"]

    chk = apply_check_reveal(
        {
            "question_uid": "u1",
            "source_id": "s1",
            "all_correct": False,
            "parts_correct": 0,
            "parts_total": 1,
            "missing_labels": [],
            "extra_labels": [],
            "part_results": [{"label": "主问", "verdict": "incorrect", "judge_reason": "标准答案应为作垂线"}],
            "expected_answer": "48",
        }
    )
    assert "expected_answer" not in chk
    assert "judge_reason" not in chk["part_results"][0]


def test_sheet_renderer_offline_html_dual_mode(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "render.db")
    init_db(conn)

    # 创建一张极小 PNG 供 CAS Base64 内联
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    tiny_png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
        "0000000c4944415408d763f8ffff3f0005fe02fea73581840000000049454e44ae426082"
    )
    (assets_dir / "fig1.png").write_bytes(tiny_png)

    for uid, fmt, has_img, imgs in (
        ("primary_olympiad:c:q1", "作图操作题", 1, '["fig1.png"]'),
        ("primary_olympiad:c:q2", "解答题", 1, '["fig1.png"]'),
        ("primary_olympiad:c:q3", "填空题", 0, "[]"),
    ):
        conn.execute(
            "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, '小学奥数', 'BATCH_JSONL', 'c', ?, ?, ?);",
            (uid, uid.split(":")[-1], f"计算 $\\frac{{1}}{{2}} + \\frac{{1}}{{3}}$ 的题 {uid}", f"h_{uid}"),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_format, m_verification_status,
                m_primary_l1_module, has_images, cas_images_json, image_captions_json,
                m_final_answers_json
            )
            VALUES (?, '小学奥数', ?, ?, 'VERIFIED_CONSENSUS', '平面几何', ?, ?, '["示意图"]', '[{"label":"主问","answer":"5/6"}]');
            """,
            (uid, f"计算 $\\frac{{1}}{{2}} + \\frac{{1}}{{3}}$ 的题 {uid}", fmt, has_img, imgs),
        )
        conn.execute(
            "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, method_summary, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '通分', '通分得 5/6');",
            (f"{uid}::branch_1", uid),
        )
        conn.execute(
            "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, core_formula, hint) VALUES (?, ?, 'branch_1', 1, '通分相加', '1/2+1/3=5/6', '先找分母公倍数');",
            (f"{uid}::branch_1::1", uid),
        )
        effective.recompute(conn, uid)
    conn.commit()

    res = render_practice_sheet(
        conn,
        question_uids=["primary_olympiad:c:q1", "primary_olympiad:c:q2", "primary_olympiad:c:q3"],
        assets_dir=assets_dir,
        sheets_dir=tmp_path / "sheets",
        embed_images=True,
        print_answers_at_end=True,
    )
    html_text = Path(res["sheet_path"]).read_text(encoding="utf-8")
    assert res["question_count"] == 3
    assert res["katex_inlined"] is True
    assert "data:font/woff2;base64," in html_text
    assert "data:image/png;base64," in html_text
    assert "<details><summary>① 逐步思路提示</summary>" in html_text
    assert "@media print" in html_text
    assert "height: 8.0cm;" in html_text
    assert "height: 7.0cm;" in html_text
    assert "height: 3.5cm;" in html_text
    assert "参考答案与核心步骤速查" in html_text
    conn.close()
