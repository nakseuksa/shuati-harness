"""Tier 2 外部审查 B09（练习单渲染与复核服务）+ B10（MCP 服务端）修复单元测试。"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from contextlib import closing
from email.parser import BytesParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from shuati.core import effective
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.mcp.server import _parse_bool, dispatch_jsonrpc_message
from shuati.models.domain import AnswerPart
from shuati.render.sheet_renderer import (
    _encode_cas_image,
    _format_answer_part_display,
    _nl2br_preserving_math,
    render_practice_sheet,
)
from shuati.review.server import create_review_server
from shuati.services import facade

_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8ffff3f0005fe02fea73581840000000049454e44ae426082"
)


def _setup_isolated_config(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "config"
    if not cfg_dir.exists():
        shutil.copytree(Path("config"), cfg_dir)
    settings_path = cfg_dir / "settings.yaml"
    settings_txt = settings_path.read_text(encoding="utf-8")
    settings_txt = settings_txt.replace(
        'dir: "human/"', f'dir: "{(tmp_path / "human").as_posix()}"'
    )
    settings_path.write_text(settings_txt, encoding="utf-8")
    return cfg_dir


def _seed_question(
    conn: sqlite3.Connection,
    *,
    uid: str,
    grade_subject: str = "小学奥数",
    source_id: str | None = None,
    text: str = "求 $3 + 5$ 的值。",
    content_hash: str = "h_seed",
    cas_images: list[str] | None = None,
    image_captions: list[str] | None = None,
    visual_facts: str = "",
    verification_status: str = "VERIFIED_CONSENSUS",
    review_reason: str = "",
    final_answers: list[dict[str, Any]] | None = None,
    solution_markdown: str = "计算得 $3 + 5 = 8$。",
) -> None:
    sid = source_id or uid.split(":")[-1]
    cas = cas_images or []
    caps = image_captions or []
    answers = (
        final_answers
        if final_answers is not None
        else [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "8",
                "sympy_expr": "8",
            }
        ]
    )
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_file, source_id, raw_question, cas_images_json,
            image_captions_json, content_hash
        ) VALUES (?, ?, 'BATCH_JSONL', 'ch_test', 'ch_test/problems.jsonl', ?, ?, ?, ?, ?)
        """,
        (uid, grade_subject, sid, text, json.dumps(cas), json.dumps(caps), content_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, cas_images_json, image_captions_json,
            has_images, m_question_text, m_options_json, m_format,
            m_visual_facts, m_verification_status, review_reason,
            m_primary_l1_module, m_final_answers_json,
            m_c_score, m_s_score, m_t_score, m_l_score
        ) VALUES (?, ?, ?, ?, ?, ?, '{}', '解答题', ?, ?, ?, '计算与数感', ?, 1, 1, 1, 1)
        """,
        (
            uid,
            grade_subject,
            json.dumps(cas),
            json.dumps(caps),
            1 if cas else 0,
            text,
            visual_facts,
            verification_status,
            review_reason,
            json.dumps(answers, ensure_ascii=False),
        ),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin,
            method_summary, solution_markdown, sympy_verified
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '直接计算', ?, 1)
        """,
        (f"{uid}::branch_1", uid, solution_markdown),
    )
    conn.execute(
        """
        INSERT INTO question_steps(
            step_id, question_uid, branch_id, step_index, sub_goal, core_formula, hint
        ) VALUES (?, ?, 'branch_1', 1, '求和计算', 'a+b=c', '将两数直接相加')
        """,
        (f"{uid}::branch_1::1", uid),
    )
    for stg in ("S1", "S2", "S3C", "S3D", "S4"):
        st = "OK" if stg == "S1" else "PENDING"
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, ?)",
            (uid, stg, st),
        )
    effective.recompute(conn, uid)
    conn.commit()


class _InProcessSocket:
    """用真实 HTTP/1.1 字节流驱动 BaseHTTPRequestHandler（兼容沙盒禁用回环 TCP 场景）。"""

    def __init__(self, raw_request: bytes) -> None:
        self._rfile = io.BytesIO(raw_request)
        self._wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self._rfile if "r" in mode else self._wfile

    def sendall(self, data: bytes) -> None:
        self._wfile.write(data)

    def close(self) -> None:
        pass


def _send_http(
    server: ThreadingHTTPServer,
    method: str,
    path_with_query: str,
    *,
    body_bytes: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, str]:
    port = server.server_address[1]
    hdrs = dict(headers or {})
    hdrs.setdefault("Host", f"127.0.0.1:{port}")
    if body_bytes is not None:
        hdrs.setdefault("Content-Length", str(len(body_bytes)))
    head = (
        f"{method} {path_with_query} HTTP/1.1\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
        + "\r\n"
    )
    raw_req = head.encode("utf-8") + (body_bytes or b"")
    sock = _InProcessSocket(raw_req)
    server.RequestHandlerClass(sock, ("127.0.0.1", 0), server)  # type: ignore[arg-type]
    raw_resp = sock._wfile.getvalue()
    sep_idx = raw_resp.find(b"\r\n\r\n")
    header_block = raw_resp[:sep_idx]
    resp_body = raw_resp[sep_idx + 4 :]
    status_line, _, rest_hdrs = header_block.partition(b"\r\n")
    status_code = int(status_line.split(b" ", 2)[1])
    parsed_hdrs = BytesParser().parsebytes(rest_hdrs)
    return status_code, resp_body, parsed_hdrs.get("Content-Type", "")


# ---------------------------------------------------------------------------
# B09-F1: _encode_cas_image 路径穿越 / 符号链接 / 目录 / 非图片扩展名防线
# ---------------------------------------------------------------------------


def test_b09_f1_encode_cas_image_hardening_and_sheet_render(tmp_path: Path) -> None:
    cfg_dir = _setup_isolated_config(tmp_path)
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir(parents=True)
    secret_file = tmp_path / "secret.env"
    secret_file.write_text("API_KEY=SUPER_SECRET_12345", encoding="utf-8")
    (assets_dir / "fig1.png").write_bytes(_TINY_PNG)
    (assets_dir / "0123456789abcdef.png").write_bytes(_TINY_PNG)
    (assets_dir / "notes.txt").write_text("not an image", encoding="utf-8")
    (assets_dir / "evil.svg").write_text("<svg onload=alert(1)/>", encoding="utf-8")
    (assets_dir / "subdir").mkdir()
    (assets_dir / "dir_as.png").mkdir()

    # 1. 越界、空串、目录、非法扩展名均安全降级返回空串，绝不抛异常或读盘泄露
    bad_inputs = [
        "",
        "   ",
        ".",
        "..",
        "../secret.env",
        "..\\secret.env",
        str(secret_file),
        "subdir",
        "subdir/fig1.png",
        "dir_as.png",
        "notes.txt",
        "evil.svg",
        "missing.png",
        "fig1.png\x00.txt",
        "C:fig1.png",
    ]
    for bad in bad_inputs:
        assert _encode_cas_image(bad, assets_dir, embed_images=True) == "", f"expected reject for {bad!r}"
        assert _encode_cas_image(bad, assets_dir, embed_images=False) == "", f"expected reject for {bad!r}"

    # 2. 符号链接防线（确定性验证 is_symlink 拦截 + 若系统允许则测真实符号链接）
    with patch.object(Path, "is_symlink", return_value=True):
        assert _encode_cas_image("fig1.png", assets_dir, embed_images=True) == ""
        assert _encode_cas_image("fig1.png", assets_dir, embed_images=False) == ""

    sym_link: Path | None = assets_dir / "abcdef0123456789.png"
    assert sym_link is not None
    try:
        os.symlink(secret_file, sym_link)
    except OSError:
        sym_link = None
    if sym_link is not None and sym_link.exists():
        assert _encode_cas_image(sym_link.name, assets_dir, embed_images=True) == ""
        assert _encode_cas_image(sym_link.name, assets_dir, embed_images=False) == ""

    # 3. 合法单段图片文件名正常内联 / 返回 file URI
    encoded_fixture = _encode_cas_image("fig1.png", assets_dir, embed_images=True)
    assert encoded_fixture.startswith("data:image/png;base64,")
    encoded_cas = _encode_cas_image("0123456789abcdef.png", assets_dir, embed_images=True)
    assert encoded_cas.startswith("data:image/png;base64,")
    uri_cas = _encode_cas_image("0123456789abcdef.png", assets_dir, embed_images=False)
    assert uri_cas.startswith("file:///")

    # 4. render_practice_sheet 端到端防泄露且不崩溃
    db_path = tmp_path / "render_sec.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_question(
            conn,
            uid="primary_olympiad:ch_sec:q1",
            cas_images=["../secret.env", "", "dir_as.png", "fig1.png"],
            image_captions=["越界图", "空名", "目录伪装", "合法图"],
        )
        res = render_practice_sheet(
            conn,
            question_uids=["primary_olympiad:ch_sec:q1"],
            assets_dir=assets_dir,
            sheets_dir=tmp_path / "sheets",
            embed_images=True,
        )
    html_text = Path(res["sheet_path"]).read_text(encoding="utf-8")
    assert "QVBJX0tFWT1TVVBFUl9TRUNSRVRfMTIzNDU=" not in html_text
    assert "SUPER_SECRET_12345" not in html_text
    assert "data:image/png;base64," in html_text
    assert "fig-missing" in html_text


# ---------------------------------------------------------------------------
# B09-F2: I4 合规答案格式化（items 多值结构、sympy_expr 回退、unit 去重）
# ---------------------------------------------------------------------------


def test_b09_f2_format_answer_part_display_and_sheet_render(tmp_path: Path) -> None:
    cfg_dir = _setup_isolated_config(tmp_path)
    human_dir = tmp_path / "human"

    # 单元测试所有 I4 合法形态
    assert _format_answer_part_display({"answer": "12", "unit": "cm"}) == "12 cm"
    assert _format_answer_part_display({"answer": "12 cm", "unit": "cm"}) == "12 cm"
    assert _format_answer_part_display({"answer": "", "sympy_expr": "2*x + 1"}) == "2*x + 1"
    assert _format_answer_part_display({"answer": "", "choice_label": "AC"}) == "AC"
    assert _format_answer_part_display({"answer": "", "canonical": ">="}) == ">="
    assert (
        _format_answer_part_display(
            {
                "answer": "",
                "collection_mode": "UNORDERED_MULTISET",
                "items": [{"sympy_expr": "2"}, {"sympy_expr": "-3"}],
            }
        )
        == "2, -3"
    )
    assert (
        _format_answer_part_display(
            {
                "answer": "",
                "collection_mode": "ORDERED_TUPLE",
                "items": [{"canonical": ">"}, {"canonical": "<"}],
            }
        )
        == ">, <"
    )
    assert (
        _format_answer_part_display(
            {
                "answer": "",
                "collection_mode": "LABELED_MAP",
                "items": [
                    {"key": "x", "sympy_expr": "4"},
                    {"key": "y", "sympy_expr": "5"},
                ],
                "unit": "cm",
            }
        )
        == "x=4, y=5 cm"
    )
    assert (
        _format_answer_part_display(
            {
                "answer": "",
                "collection_mode": "LABELED_MAP",
                "items": [{"甲": 10}, {"label": "乙", "value": "20"}],
            }
        )
        == "甲=10, 乙=20"
    )

    # 端到端：经 submit_manual_review 写入四类合法 I4 AnswerPart 并渲染 HTML
    db_path = tmp_path / "render_i4.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        uid = "primary_olympiad:ch_i4:q1"
        _seed_question(conn, uid=uid, text="多小问 I4 渲染测试")
        i4_parts: list[AnswerPart | dict[str, Any]] = [
            AnswerPart(
                label="(1)",
                answer_type="EXPRESSION",
                verify_mode="SYMBOLIC",
                collection_mode="SINGLE",
                answer="",
                sympy_expr="x + 1",
            ),
            AnswerPart(
                label="(2)",
                answer_type="NUMERIC",
                verify_mode="SYMBOLIC",
                collection_mode="UNORDERED_MULTISET",
                answer="",
                items=[{"sympy_expr": "2"}, {"sympy_expr": "-3"}],
            ),
            AnswerPart(
                label="(3)",
                answer_type="SYMBOL_FILL",
                verify_mode="EXACT",
                collection_mode="ORDERED_TUPLE",
                answer="",
                items=[{"item_type": "SYMBOL_FILL", "canonical": ">="}, {"item_type": "SYMBOL_FILL", "canonical": "<"}],
            ),
            AnswerPart(
                label="(4)",
                answer_type="NUMERIC",
                verify_mode="SYMBOLIC",
                collection_mode="LABELED_MAP",
                answer="",
                items=[{"key": "x", "sympy_expr": "4"}, {"key": "y", "sympy_expr": "5"}],
            ),
        ]
        facade.submit_manual_review(
            conn,
            uid,
            visual_facts="",
            final_answers=i4_parts,
            solution_markdown="分别求解得结果。",
            human_dir=human_dir,
        )
        res = render_practice_sheet(
            conn,
            question_uids=[uid],
            assets_dir=tmp_path / "assets",
            sheets_dir=tmp_path / "sheets",
            print_answers_at_end=True,
        )

    html_text = Path(res["sheet_path"]).read_text(encoding="utf-8")
    # ② 标准答案区域
    assert "<li><strong>(1)：</strong>x + 1</li>" in html_text
    assert "<li><strong>(2)：</strong>2, -3</li>" in html_text
    assert "<li><strong>(3)：</strong>&gt;=, &lt;</li>" in html_text
    assert "<li><strong>(4)：</strong>x=4, y=5</li>" in html_text
    # 打印附页区域
    assert "(1): x + 1" in html_text
    assert "(2): 2, -3" in html_text
    assert "(3): &gt;=, &lt;" in html_text
    assert "(4): x=4, y=5" in html_text


# ---------------------------------------------------------------------------
# B09-F3: .print-answer-appendix 屏幕默认隐藏、@media print 显示并分页
# ---------------------------------------------------------------------------


def test_b09_f3_print_answer_appendix_hidden_on_screen_shown_in_print(tmp_path: Path) -> None:
    cfg_dir = _setup_isolated_config(tmp_path)
    db_path = tmp_path / "render_css.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_question(conn, uid="primary_olympiad:ch_css:q1")
        res = render_practice_sheet(
            conn,
            question_uids=["primary_olympiad:ch_css:q1"],
            assets_dir=tmp_path / "assets",
            sheets_dir=tmp_path / "sheets",
            print_answers_at_end=True,
        )

    html_text = Path(res["sheet_path"]).read_text(encoding="utf-8")
    print_idx = html_text.index("@media print")
    screen_css = html_text[:print_idx]
    print_css = html_text[print_idx:]

    m_screen = re.search(r"\.print-answer-appendix\s*\{([^}]*)\}", screen_css, re.S)
    assert m_screen is not None
    assert "display: none;" in m_screen.group(1)

    m_print = re.search(r"\.print-answer-appendix\s*\{([^}]*)\}", print_css, re.S)
    assert m_print is not None
    assert "display: block;" in m_print.group(1)
    assert "page-break-before: always;" in m_print.group(1)


# ---------------------------------------------------------------------------
# B09-F4: _nl2br_preserving_math 保护多行 $...$ / $$...$$ 公式并处理 \$ 转义
# ---------------------------------------------------------------------------


def test_b09_f4_nl2br_preserving_math_unit_and_render(tmp_path: Path) -> None:
    # 1. 多行块级 $$...$$ 与普通换行共存
    multiline_q = (
        "解方程组：\n"
        "$$\n"
        "\\begin{cases}\n"
        "x + y = 9 \\\\\n"
        "x - y = -1\n"
        "\\end{cases}\n"
        "$$\n"
        "求 $x < y$ 时 $x$ 的值。"
    )
    rendered = _nl2br_preserving_math(multiline_q)
    assert rendered.startswith("解方程组：<br/>$$\n\\begin{cases}\nx + y = 9 \\\\\nx - y = -1\n\\end{cases}\n$$<br/>")
    assert "$x &lt; y$" in rendered

    # 2. 多行行内 $...$ 与反斜杠转义 \$ / \\$
    assert _nl2br_preserving_math("单价 \\$5\n第二行") == "单价 \\$5<br/>第二行"
    assert _nl2br_preserving_math("$a + \\$b\n+ c$\n外行") == "$a + \\$b\n+ c$<br/>外行"
    assert _nl2br_preserving_math("$a + b \\\\$\n下一行") == "$a + b \\\\$<br/>下一行"

    # 3. 未闭合 $$ 不破坏后续合法 $...$，并规范化 \r\n
    assert _nl2br_preserving_math("$$未闭合\r\n$x +\r\ny$\r\n末行") == "$$未闭合<br/>$x +\ny$<br/>末行"

    # 4. 端到端 render_practice_sheet 验证 question_text 与 solution_markdown
    cfg_dir = _setup_isolated_config(tmp_path)
    db_path = tmp_path / "render_math.db"
    sol_multiline = "步骤：\n$$\n\\begin{aligned}\n2x &= 8 \\\\\nx &= 4\n\\end{aligned}\n$$\n结论：4。"
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_question(
            conn,
            uid="primary_olympiad:ch_math:q1",
            text=multiline_q,
            solution_markdown=sol_multiline,
        )
        res = render_practice_sheet(
            conn,
            question_uids=["primary_olympiad:ch_math:q1"],
            assets_dir=tmp_path / "assets",
            sheets_dir=tmp_path / "sheets",
        )
    html_text = Path(res["sheet_path"]).read_text(encoding="utf-8")
    assert "$$\n\\begin{cases}\nx + y = 9 \\\\\nx - y = -1\n\\end{cases}\n$$" in html_text
    assert "$$\n\\begin{aligned}\n2x &amp;= 8 \\\\\nx &amp;= 4\n\\end{aligned}\n$$" in html_text
    assert "<br/>\\begin{cases}<br/>" not in html_text
    assert "<br/>\\begin{aligned}<br/>" not in html_text


# ---------------------------------------------------------------------------
# B09-F5, B09-F6, B09-F7: review/server.py 修复
# ---------------------------------------------------------------------------


def test_b09_f5_review_queue_blank_grade_subject_clears_default(tmp_path: Path) -> None:
    cfg_dir = _setup_isolated_config(tmp_path)
    db_path = tmp_path / "review_q.db"
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        conn.execute(
            """
            INSERT OR IGNORE INTO grade_subjects(grade_subject, slug, display_name, subject_family)
            VALUES ('初中数学', 'junior_math', '初中数学', 'MATH')
            """
        )
        _seed_question(
            conn,
            uid="primary_olympiad:ch_r:q1",
            grade_subject="小学奥数",
            verification_status="NEEDS_REVIEW",
            review_reason="ANSWER_MISMATCH",
        )
        _seed_question(
            conn,
            uid="junior_math:ch_r:q2",
            grade_subject="初中数学",
            verification_status="NEEDS_REVIEW",
            review_reason="ANSWER_MISMATCH",
        )

    server = create_review_server(
        db_path=db_path,
        config_dir=cfg_dir,
        assets_dir=tmp_path / "assets",
        human_dir=tmp_path / "human",
        grade_subject="小学奥数",
        port=0,
        use_mock=True,
    )
    try:
        # 不传参 -> 使用默认学段
        st1, body1, _ = _send_http(server, "GET", "/api/queue")
        d1 = json.loads(body1.decode("utf-8"))
        assert st1 == 200
        assert d1["grade_subject"] == "小学奥数"
        assert d1["total"] == 1

        # 显式 ?grade_subject= -> 清空默认学段，返回全部学段且响应为 null
        st2, body2, _ = _send_http(server, "GET", "/api/queue?grade_subject=")
        d2 = json.loads(body2.decode("utf-8"))
        assert st2 == 200
        assert d2["grade_subject"] is None
        assert d2["total"] == 2

        # 显式指定另一学段
        st3, body3, _ = _send_http(
            server,
            "GET",
            f"/api/queue?grade_subject={urllib.parse.quote('初中数学')}",
        )
        d3 = json.loads(body3.decode("utf-8"))
        assert st3 == 200
        assert d3["grade_subject"] == "初中数学"
        assert d3["total"] == 1
    finally:
        server.server_close()


def test_b09_f6_create_review_server_derives_paths_from_db_parent(tmp_path: Path) -> None:
    cfg_dir = _setup_isolated_config(tmp_path)
    # 故意构造末尾为 data/shuati.db 的非 CWD 隔离路径
    iso_data_dir = tmp_path / "other_workspace" / "data"
    iso_assets_dir = iso_data_dir / "assets" / "images"
    iso_assets_dir.mkdir(parents=True)
    img_name = "0123456789abcdef.png"
    (iso_assets_dir / img_name).write_bytes(_TINY_PNG)
    db_path = iso_data_dir / "shuati.db"

    uid = "primary_olympiad:ch_iso:q1"
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_question(
            conn,
            uid=uid,
            verification_status="NEEDS_REVIEW",
            review_reason="ANSWER_MISMATCH",
            cas_images=[img_name],
            image_captions=["隔离图"],
        )

    server = create_review_server(
        db_path=db_path,
        config_dir=cfg_dir,
        assets_dir=None,  # 触发默认推导
        human_dir=tmp_path / "human",
        port=0,
        use_mock=True,
    )
    try:
        # 1. GET /assets 必须读到 iso_assets_dir 中的图片（而非 CWD data/assets/images）
        st_img, raw_img, ctype = _send_http(server, "GET", f"/assets/{img_name}")
        assert st_img == 200
        assert "image/png" in ctype
        assert raw_img == _TINY_PNG

        # 2. POST /api/resolve 传入的 llm_raw_dir 必须是 iso_data_dir / "llm_raw"（绝不指向 CWD）
        captured_llm_raw: list[Path] = []
        orig_resolve = facade.resolve_review_draft

        def _spy_resolve(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_llm_raw.append(Path(kwargs["llm_raw_dir"]))
            return orig_resolve(*args, **kwargs)

        with patch.object(facade, "resolve_review_draft", side_effect=_spy_resolve):
            st_res, body_res, _ = _send_http(
                server,
                "POST",
                "/api/resolve",
                body_bytes=json.dumps(
                    {"question_uid": uid, "visual_facts": "测试读图"}, ensure_ascii=False
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        assert st_res == 200, body_res.decode("utf-8")
        assert captured_llm_raw == [iso_data_dir / "llm_raw"]
    finally:
        server.server_close()


def test_b09_f7_review_server_host_allowlist_blocks_get_and_post(tmp_path: Path) -> None:
    cfg_dir = _setup_isolated_config(tmp_path)
    assets_dir = tmp_path / "assets" / "images"
    assets_dir.mkdir(parents=True)
    img_name = "fedcba9876543210.png"
    (assets_dir / img_name).write_bytes(_TINY_PNG)
    db_path = tmp_path / "review_host.db"
    uid = "primary_olympiad:ch_host:q1"
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_question(
            conn,
            uid=uid,
            verification_status="NEEDS_REVIEW",
            review_reason="ANSWER_MISMATCH",
            cas_images=[img_name],
        )

    server = create_review_server(
        db_path=db_path,
        config_dir=cfg_dir,
        assets_dir=assets_dir,
        human_dir=tmp_path / "human",
        port=0,
        use_mock=True,
    )
    port = server.server_address[1]
    try:
        # 1. DNS Rebinding 伪造 Host 在所有 GET 端点均返回 403 FORBIDDEN_HOST
        for path in (
            "/",
            "/api/queue",
            f"/api/item/{urllib.parse.quote(uid, safe='')}",
            f"/assets/{img_name}",
        ):
            st, body, _ = _send_http(
                server, "GET", path, headers={"Host": f"attacker.example:{port}"}
            )
            payload = json.loads(body.decode("utf-8"))
            assert st == 403
            assert payload["code"] == "FORBIDDEN_HOST"

        # 错误端口同样拒绝
        st_wp, body_wp, _ = _send_http(
            server, "GET", "/api/queue", headers={"Host": f"127.0.0.1:{port + 1}"}
        )
        assert st_wp == 403
        assert json.loads(body_wp.decode("utf-8"))["code"] == "FORBIDDEN_HOST"

        # 2. 合法本地回环 Host 正常放行
        for good_host in (
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            "127.0.0.1",
            "localhost",
        ):
            st_ok, _, _ = _send_http(server, "GET", "/api/queue", headers={"Host": good_host})
            assert st_ok == 200, f"expected 200 for Host={good_host}"

        # 3. POST 伪造 Host 拦截且不写库
        st_post, body_post, _ = _send_http(
            server,
            "POST",
            "/api/exclude",
            body_bytes=json.dumps({"question_uid": uid}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Host": f"attacker.example:{port}",
            },
        )
        assert st_post == 403
        assert json.loads(body_post.decode("utf-8"))["code"] == "FORBIDDEN_HOST"
        with closing(get_connection(db_path, readonly=True)) as conn:
            row = conn.execute(
                "SELECT eff_is_excluded FROM questions WHERE question_uid = ?", (uid,)
            ).fetchone()
            assert row["eff_is_excluded"] == 0
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# B10-F1: MCP _tool_check 在活跃 asyncio 事件循环下同线程创建/使用/关闭 SQLite 连接
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b10_f1_mcp_check_works_inside_running_asyncio_loop(tmp_path: Path) -> None:
    cfg_dir = _setup_isolated_config(tmp_path)
    db_path = tmp_path / "mcp_async_check.db"
    human_dir = tmp_path / "human"
    assets_dir = tmp_path / "assets"
    vectors_dir = tmp_path / "vectors"
    drafts_dir = tmp_path / "drafts"
    unified_dir = tmp_path / "unified"
    uid = "primary_olympiad:ch_mcp:q_check"

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_question(conn, uid=uid, text="计算 3 + 5。")

    # 1. 只读判题（log=False）在活跃事件循环内不应触发 SQLite 跨线程 ProgrammingError
    resp_ro = dispatch_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "check",
                "arguments": {"uid": uid, "answer": "8", "log": False},
            },
        },
        role="practice",
        db_path=db_path,
        config_dir=cfg_dir,
        assets_dir=assets_dir,
        vectors_dir=vectors_dir,
        human_dir=human_dir,
        drafts_dir=drafts_dir,
        unified_dir=unified_dir,
    )
    assert resp_ro is not None
    assert resp_ro["result"]["isError"] is False, resp_ro
    sc_ro = resp_ro["result"]["structuredContent"]
    assert sc_ro["all_correct"] is True
    assert "expected_answer" not in sc_ro

    # 2. 落库判题（log=True）在活跃事件循环内同样正常写库并导出到 tmp_path/human
    resp_rw = dispatch_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "check",
                "arguments": {"uid": uid, "answer": "8", "log": True, "request_id": "req_async_1"},
            },
        },
        role="practice",
        db_path=db_path,
        config_dir=cfg_dir,
        assets_dir=assets_dir,
        vectors_dir=vectors_dir,
        human_dir=human_dir,
        drafts_dir=drafts_dir,
        unified_dir=unified_dir,
    )
    assert resp_rw is not None
    assert resp_rw["result"]["isError"] is False, resp_rw
    assert resp_rw["result"]["structuredContent"]["all_correct"] is True

    with closing(get_connection(db_path, readonly=True)) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM practice_log WHERE question_uid = ? AND client_request_id = 'req_async_1'",
            (uid,),
        ).fetchone()[0]
        assert cnt == 1
    assert (human_dir / "practice_log.jsonl").exists()


# ---------------------------------------------------------------------------
# B10-F2: MCP 严格布尔校验，阻断 "false" 绕过防剧透与只读守卫
# ---------------------------------------------------------------------------


def test_b10_f2_mcp_strict_parse_bool_blocks_string_coercion(tmp_path: Path) -> None:
    # 1. 直接单测 _parse_bool
    assert _parse_bool(None, default=False, name="f") is False
    assert _parse_bool(None, default=True, name="f") is True
    assert _parse_bool(None, default=None, name="m", allow_none=True) is None
    assert _parse_bool(True, default=False, name="f") is True
    assert _parse_bool(False, default=True, name="f") is False
    bad_vals: tuple[Any, ...] = ("false", "true", "0", "1", "", 0, 1, [], {})
    for bad_val in bad_vals:
        with pytest.raises(ValueError, match="必须为布尔值"):
            _parse_bool(bad_val, default=False, name="flag")
        with pytest.raises(ValueError, match="必须为布尔值"):
            _parse_bool(bad_val, default=None, name="mock", allow_none=True)

    cfg_dir = _setup_isolated_config(tmp_path)
    db_path = tmp_path / "mcp_bool.db"
    human_dir = tmp_path / "human"
    assets_dir = tmp_path / "assets"
    vectors_dir = tmp_path / "vectors"
    drafts_dir = tmp_path / "drafts"
    unified_dir = tmp_path / "unified"
    uid = "primary_olympiad:ch_bool:q1"
    secret_vf = "SECRET_VISUAL_FACT_DO_NOT_LEAK"

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        _seed_question(conn, uid=uid, text="有图几何题", visual_facts=secret_vf)

    common_kwargs: dict[str, Any] = {
        "db_path": db_path,
        "config_dir": cfg_dir,
        "assets_dir": assets_dir,
        "vectors_dir": vectors_dir,
        "human_dir": human_dir,
        "drafts_dir": drafts_dir,
        "unified_dir": unified_dir,
    }

    # 2. practice 工具：字符串 "false" 绝不能翻转为 True 泄露 visual_facts 或写库
    practice_cases = [
        ("detail", {"uid": uid, "include_visual_facts": "false"}),
        ("search", {"include_visual_facts": "false"}),
        ("search", {"exclude_solved": "false"}),
        ("search", {"mock": "false"}),
        ("related", {"uid": uid, "include_visual_facts": "false"}),
        ("related", {"uid": uid, "exclude_solved": "0"}),
        ("compose_quiz", {"include_visual_facts": "false", "count": 1}),
        ("check", {"uid": uid, "answer": "8", "log": "false"}),
        ("check", {"uid": uid, "answer": "8", "mock": "false"}),
    ]
    for idx, (tool_name, tool_args) in enumerate(practice_cases, start=1):
        resp = dispatch_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": idx,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": tool_args},
            },
            role="practice",
            **common_kwargs,
        )
        assert resp is not None
        assert resp["result"]["isError"] is True, f"{tool_name}({tool_args}) should fail"
        assert resp["result"]["structuredContent"]["error_type"] == "ValueError"
        assert secret_vf not in json.dumps(resp, ensure_ascii=False)

    # 确认 check(log="false") 与 compose_quiz(include_visual_facts="false") 均未写库或导出快照
    with closing(get_connection(db_path, readonly=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM practice_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM quiz_batches").fetchone()[0] == 0
    assert not human_dir.exists()

    # 3. worker / compiler 工具同样严格拒绝非布尔值
    worker_resp = dispatch_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {
                "name": "validate_draft",
                "arguments": {
                    "mode": "UNIFIED_S1_S2",
                    "question_text": "1+1=?",
                    "candidate": {},
                    "run_sympy": "false",
                },
            },
        },
        role="worker",
        **common_kwargs,
    )
    assert worker_resp is not None
    assert worker_resp["result"]["isError"] is True
    assert worker_resp["result"]["structuredContent"]["error_type"] == "ValueError"

    for cid, (cname, cargs) in enumerate(
        [
            ("doctor", {"check_llm_api": "false"}),
            ("doctor", {"mock": "false"}),
            ("ingest_dry_run", {"dedup_near": "false"}),
            ("ingest_dry_run", {"apply_retire": 1}),
        ],
        start=200,
    ):
        c_resp = dispatch_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": cid,
                "method": "tools/call",
                "params": {"name": cname, "arguments": cargs},
            },
            role="compiler",
            **common_kwargs,
        )
        assert c_resp is not None
        assert c_resp["result"]["isError"] is True
        assert c_resp["result"]["structuredContent"]["error_type"] == "ValueError"


# ---------------------------------------------------------------------------
# B10-F3: JSON-RPC 2.0 响应契约 + Worker 物理零 DB 隔离回归验证
# ---------------------------------------------------------------------------


def test_b10_f3_jsonrpc_response_contract_and_worker_zero_db_isolation(tmp_path: Path) -> None:
    cfg_dir = _setup_isolated_config(tmp_path)

    # 1. 无 id 但缺少/非字符串 method -> 必须返回 -32600（绝非 None）
    for bad_msg in (
        {},
        {"jsonrpc": "2.0"},
        {"jsonrpc": "2.0", "method": 1, "params": "bar"},
        {"jsonrpc": "2.0", "method": ""},
    ):
        resp = dispatch_jsonrpc_message(bad_msg, role="worker", config_dir=cfg_dir)
        assert resp is not None
        assert resp["id"] is None
        assert resp["error"]["code"] == -32600

    # 带 id 且缺失 method -> 返回相同 id 与 -32600
    resp_with_id = dispatch_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 99, "method": None}, role="worker", config_dir=cfg_dir
    )
    assert resp_with_id is not None
    assert resp_with_id["id"] == 99
    assert resp_with_id["error"]["code"] == -32600

    # 2. 纯通知（无 id）返回 None；带 id 的 notifications/initialized 必须返回 -32601（不可挂死客户端）
    assert (
        dispatch_jsonrpc_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            role="worker",
            config_dir=cfg_dir,
        )
        is None
    )
    resp_notif_id = dispatch_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 42, "method": "notifications/initialized"},
        role="worker",
        config_dir=cfg_dir,
    )
    assert resp_notif_id is not None
    assert resp_notif_id["id"] == 42
    assert resp_notif_id["error"]["code"] == -32601

    # 3. 子进程实证：worker 角色在执行新老路径后仍保持零 sqlite3 / shuati.db / facade 污染
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
    probe = (
        "import io, json, sys\n"
        "from pathlib import Path\n"
        "from shuati.mcp.server import serve_stdio\n"
        f"cfg = Path({str(cfg_dir)!r})\n"
        "reqs = [\n"
        "  {},\n"
        "  {'jsonrpc': '2.0', 'method': 'notifications/initialized'},\n"
        "  {'jsonrpc': '2.0', 'id': 42, 'method': 'notifications/initialized'},\n"
        "  {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},\n"
        "  {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'validate_draft', 'arguments': {'mode': 'UNIFIED_S1_S2', 'question_text': '1+1', 'candidate': {}, 'run_sympy': 'false'}}},\n"
        "]\n"
        "buf_in = io.StringIO('\\n'.join(json.dumps(r, ensure_ascii=False) for r in reqs) + '\\n')\n"
        "buf_out = io.StringIO()\n"
        "serve_stdio(role='worker', config_dir=cfg, stdin=buf_in, stdout=buf_out)\n"
        "assert 'sqlite3' not in sys.modules, 'sqlite3 contaminated worker!'\n"
        "assert 'shuati.db' not in sys.modules, 'shuati.db contaminated worker!'\n"
        "assert 'shuati.services.facade' not in sys.modules, 'facade contaminated worker!'\n"
        "out = [json.loads(line) for line in buf_out.getvalue().splitlines() if line.strip()]\n"
        "assert len(out) == 4, f'unexpected responses: {out}'\n"
        "assert out[0]['error']['code'] == -32600 and out[0]['id'] is None\n"
        "assert out[1]['error']['code'] == -32601 and out[1]['id'] == 42\n"
        "assert [t['name'] for t in out[2]['result']['tools']] == ['verify_sympy', 'validate_draft']\n"
        "assert out[3]['result']['isError'] is True\n"
        "print('WORKER_ZERO_DB_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        check=True,
    )
    assert "WORKER_ZERO_DB_OK" in proc.stdout
