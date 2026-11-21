"""Stage 1 求解流水线与极简本地键盘复核页端到端集成测试（M1）。"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from email.parser import BytesParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from shuati.core import effective, refresh
from shuati.db import human_mirror
from shuati.db.connection import get_connection
from shuati.db.schema import init_db
from shuati.llm.gateway import LLMCallResult, LLMGateway, LLMGatewayError
from shuati.models.domain import EXIT_OK, AnswerPart
from shuati.models.schemas import (
    MultiSolutionJudgeOutput,
    QualitativeJudgeOutput,
    ReviewResolveOutput,
    Stage1PrimaryOutput,
    Stage1SecondaryOutput,
)
from shuati.pipeline.stage0_ingest import run_stage0_ingest
from shuati.pipeline.stage1_solve import materialize_solutions, run_stage1_compile
from shuati.review.server import create_review_server


def _seed_question(
    conn: sqlite3.Connection,
    *,
    uid: str,
    source_id: str,
    text: str,
    content_hash: str,
    cas_images: list[str] | None = None,
    image_captions: list[str] | None = None,
    fmt: str = "解答题",
    options: dict[str, str] | None = None,
) -> None:
    cas = cas_images or []
    caps = image_captions or []
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection,
            source_file, source_id, raw_question, cas_images_json,
            image_captions_json, content_hash
        ) VALUES (?, '小学奥数', 'BATCH_JSONL', 'ch_test', 'ch_test/problems.jsonl', ?, ?, ?, ?, ?)
        """,
        (uid, source_id, text, json.dumps(cas), json.dumps(caps), content_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, cas_images_json, image_captions_json,
            has_images, m_question_text, m_options_json, m_format
        ) VALUES (?, '小学奥数', ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            json.dumps(cas),
            json.dumps(caps),
            1 if cas else 0,
            text,
            json.dumps(options or {}, ensure_ascii=False),
            fmt,
        ),
    )
    for stg in ("S1", "S2", "S3C", "S3D", "S4"):
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, 'PENDING')",
            (uid, stg),
        )
    effective.recompute(conn, uid)
    conn.commit()


class _InProcessSocket:
    """沙盒禁用回环 TCP connect 时，通过真实 HTTP/1.1 报文驱动 BaseHTTPRequestHandler。"""

    def __init__(self, raw_request: bytes) -> None:
        self._rfile = io.BytesIO(raw_request)
        self._wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        if "r" in mode:
            return self._rfile
        return self._wfile

    def sendall(self, data: bytes) -> None:
        self._wfile.write(data)

    def close(self) -> None:
        pass


class _ReviewHttpClient:
    """优先走真实 127.0.0.1 TCP；若沙盒拦截 WinError 10013 则同报文驱动 server.RequestHandlerClass。"""

    def __init__(self, server: ThreadingHTTPServer) -> None:
        self.server = server
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}"
        self._use_in_process = False

    def _dispatch_in_process(
        self,
        path_with_query: str,
        *,
        method: str = "GET",
        body_bytes: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, str]:
        hdrs = dict(headers or {})
        hdrs.setdefault("Host", f"127.0.0.1:{self.server.server_address[1]}")
        if body_bytes is not None:
            hdrs.setdefault("Content-Length", str(len(body_bytes)))
        head = f"{method} {path_with_query} HTTP/1.1\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in hdrs.items()
        ) + "\r\n"
        raw_req = head.encode("utf-8") + (body_bytes or b"")
        sock = _InProcessSocket(raw_req)
        self.server.RequestHandlerClass(sock, ("127.0.0.1", 0), self.server)  # type: ignore[arg-type]
        raw_resp = sock._wfile.getvalue()
        sep_idx = raw_resp.find(b"\r\n\r\n")
        header_block = raw_resp[:sep_idx]
        resp_body = raw_resp[sep_idx + 4 :]
        status_line, _, rest_hdrs = header_block.partition(b"\r\n")
        status_code = int(status_line.split(b" ", 2)[1])
        parsed_hdrs = BytesParser().parsebytes(rest_hdrs)
        return status_code, resp_body, parsed_hdrs.get("Content-Type", "")

    def raw(self, path_with_query: str) -> tuple[int, bytes, str]:
        if not self._use_in_process:
            req = urllib.request.Request(f"{self.base_url}{path_with_query}", method="GET")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as err:
                return err.code, err.read(), err.headers.get("Content-Type", "")
            except urllib.error.URLError as err:
                if isinstance(err.reason, PermissionError) or getattr(err.reason, "winerror", None) == 10013:
                    self._use_in_process = True
                else:
                    raise
        return self._dispatch_in_process(path_with_query, method="GET")

    def json(
        self,
        path_with_query: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if not self._use_in_process:
            req = urllib.request.Request(
                f"{self.base_url}{path_with_query}", data=data, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read().decode("utf-8"))
            except urllib.error.URLError as err:
                if isinstance(err.reason, PermissionError) or getattr(err.reason, "winerror", None) == 10013:
                    self._use_in_process = True
                else:
                    raise
        code, raw_bytes, _ = self._dispatch_in_process(
            path_with_query, method=method, body_bytes=data, headers=headers
        )
        return code, json.loads(raw_bytes.decode("utf-8"))


@pytest.mark.asyncio
async def test_stage1_consensus_multisolution_resume_and_exhaustion(tmp_path: Path):
    db_path = tmp_path / "stage1_scenarios.db"
    assets_dir = tmp_path / "assets" / "images"
    assets_dir.mkdir(parents=True)

    # 创建两张假图片（合法 16 位 hex CAS 文件名）
    from PIL import Image

    img1_name = "0123456789abcdef.png"
    img2_name = "fedcba9876543210.png"
    Image.new("RGB", (160, 120), color=(255, 255, 255)).save(assets_dir / img1_name)
    Image.new("RGB", (160, 120), color=(240, 240, 240)).save(assets_dir / img2_name)

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=Path("config"))
        _seed_question(
            conn,
            uid="primary_olympiad:ch_test:q_ok",
            source_id="q_ok",
            text="求长 3、宽 4 的长方形面积。",
            content_hash="hash_ok",
        )
        _seed_question(
            conn,
            uid="primary_olympiad:ch_test:q_multi",
            source_id="q_multi",
            text="直角三角形两直角边为 3 和 4，求面积。",
            content_hash="hash_multi",
        )
        _seed_question(
            conn,
            uid="primary_olympiad:ch_test:q_diff",
            source_id="q_diff",
            text="如图，求阴影面积。",
            content_hash="hash_diff",
            cas_images=[img1_name, img2_name],
            image_captions=["图1", "图2"],
        )
        _seed_question(
            conn,
            uid="primary_olympiad:ch_test:q_fail",
            source_id="q_fail",
            text="API 连续失败测试题。",
            content_hash="hash_fail",
        )
        # 模拟遗留 RUNNING 状态，启动时必须重置为 PENDING
        conn.execute(
            "UPDATE compile_state SET status = 'RUNNING' WHERE question_uid = 'primary_olympiad:ch_test:q_ok' AND stage = 'S1'"
        )
        conn.commit()

    async def fake_call_structured(
        self: LLMGateway,
        *,
        slot: str,
        stage: str,
        messages: list[dict[str, Any]],
        schema: type[Any],
        question_uid: str | None = None,
        prompt_version: str = "",
        mock_key: str | None = None,
    ) -> LLMCallResult[Any]:
        if question_uid == "primary_olympiad:ch_test:q_fail":
            raise LLMGatewayError("Simulated 503", error_kind="GATEWAY_ERROR")

        if stage == "S1" and slot == "vlm_primary":
            if question_uid == "primary_olympiad:ch_test:q_ok":
                parsed = Stage1PrimaryOutput(
                    visual_facts="",
                    method_summary="长乘宽直接计算",
                    solution_markdown="面积为 $3 \\times 4 = 12$。",
                    final_answers=[
                        AnswerPart(
                            label="主问",
                            answer_type="NUMERIC",
                            verify_mode="SYMBOLIC",
                            answer="12",
                            sympy_expr="12",
                        )
                    ],
                    sympy_code="def verify():\n    return {'主问': str(3 * 4)}",
                )
            elif question_uid == "primary_olympiad:ch_test:q_multi":
                parsed = Stage1PrimaryOutput(
                    visual_facts="",
                    method_summary="底乘高除以二",
                    solution_markdown="面积 $3 \\times 4 / 2 = 6$。",
                    final_answers=[
                        AnswerPart(
                            label="主问",
                            answer_type="NUMERIC",
                            verify_mode="SYMBOLIC",
                            answer="6",
                            sympy_expr="6",
                        )
                    ],
                    sympy_code="def verify():\n    return {'主问': str(3 * 4 // 2)}",
                )
            else:  # q_diff
                parsed = Stage1PrimaryOutput(
                    visual_facts="主路读图：正方形边长 4",
                    method_summary="割补法",
                    solution_markdown="主路面积为 8。",
                    final_answers=[
                        AnswerPart(
                            label="主问",
                            answer_type="NUMERIC",
                            verify_mode="SYMBOLIC",
                            answer="8",
                            sympy_expr="8",
                        )
                    ],
                    sympy_code="def verify():\n    return {'主问': str(4 * 4 // 2)}",
                )
            return LLMCallResult(parsed=parsed, model_name="mock-primary", raw_response="{}")

        if stage == "S1" and slot == "vlm_secondary":
            if question_uid == "primary_olympiad:ch_test:q_ok":
                parsed_s = Stage1SecondaryOutput(
                    visual_facts="",
                    method_summary="长乘宽",
                    solution_markdown="$3 \\times 4 = 12$",
                    final_answers=[
                        AnswerPart(
                            label="主问",
                            answer_type="NUMERIC",
                            verify_mode="SYMBOLIC",
                            answer="12",
                            sympy_expr="12",
                        )
                    ],
                )
            elif question_uid == "primary_olympiad:ch_test:q_multi":
                parsed_s = Stage1SecondaryOutput(
                    visual_facts="",
                    method_summary="海伦公式求面积",
                    solution_markdown="半周长 $s=(3+4+5)/2=6$，面积 $\\sqrt{6\\times 3\\times 2\\times 1}=6$。",
                    final_answers=[
                        AnswerPart(
                            label="主问",
                            answer_type="NUMERIC",
                            verify_mode="SYMBOLIC",
                            answer="6",
                            sympy_expr="6",
                        )
                    ],
                )
            else:  # q_diff 分歧答案 10
                parsed_s = Stage1SecondaryOutput(
                    visual_facts="辅路读图：长方形长 5 宽 4",
                    method_summary="直接减法",
                    solution_markdown="辅路面积为 10。",
                    final_answers=[
                        AnswerPart(
                            label="主问",
                            answer_type="NUMERIC",
                            verify_mode="SYMBOLIC",
                            answer="10",
                            sympy_expr="10",
                        )
                    ],
                )
            return LLMCallResult(parsed=parsed_s, model_name="mock-secondary", raw_response="{}")

        if stage == "S1_JUDGE" and schema is MultiSolutionJudgeOutput:
            is_diff = question_uid == "primary_olympiad:ch_test:q_multi"
            return LLMCallResult(
                parsed=MultiSolutionJudgeOutput(is_distinct_method=is_diff, reason="test"),
                model_name="mock-judge",
                raw_response="{}",
            )

        return LLMCallResult(
            parsed=QualitativeJudgeOutput(equivalent=True, confidence="HIGH", reason="ok"),
            model_name="mock-judge",
            raw_response="{}",
        )

    with patch.object(LLMGateway, "call_structured", new=fake_call_structured):
        # 第 1 轮：q_ok, q_multi 共识（q_multi 一题多解）；q_diff 分歧；q_fail 第 1 次失败（尚未转 NEEDS_REVIEW）
        res1 = await run_stage1_compile(
            db_path=db_path,
            config_dir=Path("config"),
            assets_dir=assets_dir,
        )
        assert res1.total_candidates == 4
        assert res1.processed == 4
        assert res1.verified_consensus == 2
        assert res1.multi_solution_count == 1
        assert res1.needs_review == 1
        assert res1.failed == 1

        with closing(get_connection(db_path)) as conn:
            q_fail = conn.execute(
                "SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid = 'primary_olympiad:ch_test:q_fail'"
            ).fetchone()
            assert q_fail["eff_verification_status"] == "PENDING"
            assert "RUN_FAILED_EXHAUSTED" not in q_fail["eff_review_reason"]

            cs_fail = conn.execute(
                "SELECT status, attempts FROM compile_state WHERE question_uid = 'primary_olympiad:ch_test:q_fail' AND stage = 'S1'"
            ).fetchone()
            assert cs_fail["status"] == "FAILED"
            assert cs_fail["attempts"] == 1

            # 验证一题多解物化为 branch_1 + branch_2
            sols = conn.execute(
                "SELECT branch_id, is_optimal, source_origin FROM question_solutions WHERE question_uid = 'primary_olympiad:ch_test:q_multi' ORDER BY branch_id"
            ).fetchall()
            assert [(s["branch_id"], s["is_optimal"], s["source_origin"]) for s in sols] == [
                ("branch_1", 1, "PRIMARY_VLM"),
                ("branch_2", 0, "SECONDARY_VLM"),
            ]

            # 验证 q_diff 进入 NEEDS_REVIEW 且 S2 被 BLOCKED
            q_diff = conn.execute(
                "SELECT eff_verification_status, eff_review_reason, eff_is_servable FROM questions WHERE question_uid = 'primary_olympiad:ch_test:q_diff'"
            ).fetchone()
            assert q_diff["eff_verification_status"] == "NEEDS_REVIEW"
            assert "ANSWER_MISMATCH" in q_diff["eff_review_reason"]
            assert q_diff["eff_is_servable"] == 0
            s2_diff = conn.execute(
                "SELECT status FROM compile_state WHERE question_uid = 'primary_olympiad:ch_test:q_diff' AND stage = 'S2'"
            ).fetchone()
            assert s2_diff["status"] == "BLOCKED"

        # 第 2 轮断点续跑：跳过 3 个 OK，只重试 q_fail (attempt 2)
        res2 = await run_stage1_compile(db_path=db_path, config_dir=Path("config"), assets_dir=assets_dir)
        assert res2.skipped_ok == 3
        assert res2.processed == 1
        assert res2.failed == 1

        # 第 3 轮：达到 max_stage_attempts=3，转 NEEDS_REVIEW + RUN_FAILED_EXHAUSTED
        res3 = await run_stage1_compile(db_path=db_path, config_dir=Path("config"), assets_dir=assets_dir)
        assert res3.skipped_ok == 3
        assert res3.processed == 1
        assert res3.failed == 1
        assert res3.needs_review == 1

        with closing(get_connection(db_path)) as conn:
            q_fail3 = conn.execute(
                "SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid = 'primary_olympiad:ch_test:q_fail'"
            ).fetchone()
            assert q_fail3["eff_verification_status"] == "NEEDS_REVIEW"
            assert "RUN_FAILED_EXHAUSTED" in q_fail3["eff_review_reason"]

            # 验证 ANSWER_SOLUTION_CONFLICT：若人工只覆写了不同的 final_answers_json 而未覆写 solutions_override_json
            raw_ok = conn.execute(
                "SELECT content_hash FROM raw_questions WHERE question_uid = 'primary_olympiad:ch_test:q_ok'"
            ).fetchone()
            conn.execute(
                """
                INSERT INTO human_overrides(
                    question_uid, field_name, override_value, reason, source,
                    base_fingerprint, is_active, is_suspended, suspended_reason
                ) VALUES (?, 'final_answers_json', ?, '人工改答案制造冲突', 'PI_CLI', ?, 1, 0, '')
                """,
                (
                    "primary_olympiad:ch_test:q_ok",
                    json.dumps(
                        [
                            AnswerPart(
                                label="主问",
                                answer_type="NUMERIC",
                                verify_mode="SYMBOLIC",
                                answer="99",
                                sympy_expr="99",
                            ).model_dump()
                        ],
                        ensure_ascii=False,
                    ),
                    raw_ok["content_hash"],
                ),
            )
            effective.recompute(conn, "primary_olympiad:ch_test:q_ok")
            materialize_solutions(conn, "primary_olympiad:ch_test:q_ok")
            effective.recompute(conn, "primary_olympiad:ch_test:q_ok")
            q_conflict = conn.execute(
                "SELECT m_verification_status, eff_verification_status, eff_is_servable, eff_review_reason FROM questions WHERE question_uid = 'primary_olympiad:ch_test:q_ok'"
            ).fetchone()
            assert q_conflict["m_verification_status"] == "NEEDS_REVIEW"
            assert q_conflict["eff_verification_status"] == "NEEDS_REVIEW"
            assert q_conflict["eff_is_servable"] == 0
            assert "ANSWER_SOLUTION_CONFLICT" in q_conflict["eff_review_reason"]


def test_review_server_full_workflow_and_empty_restore(tmp_path: Path):
    db_path = tmp_path / "review_flow.db"
    assets_dir = tmp_path / "assets" / "images"
    human_dir = tmp_path / "human"
    assets_dir.mkdir(parents=True)

    from PIL import Image

    img1_name = "0123456789abcdef.png"
    img2_name = "fedcba9876543210.png"
    Image.new("RGB", (120, 120), color=(255, 255, 255)).save(assets_dir / img1_name)
    Image.new("RGB", (120, 120), color=(220, 220, 220)).save(assets_dir / img2_name)

    uid_a = "primary_olympiad:ch_test:q_rev_a"
    uid_b = "primary_olympiad:ch_test:q_rev_b"
    uid_c = "primary_olympiad:ch_test:q_rev_c"

    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=Path("config"))
        for uid, sid, h in [(uid_a, "q_rev_a", "h_a"), (uid_b, "q_rev_b", "h_b"), (uid_c, "q_rev_c", "h_c")]:
            _seed_question(
                conn,
                uid=uid,
                source_id=sid,
                text=f"测试题目 {sid}",
                content_hash=h,
                cas_images=[img1_name, img2_name],
                image_captions=["A", "B"],
            )
            conn.execute(
                """
                INSERT INTO stage1_vlm_runs(
                    question_uid, role, model_name, prompt_version, input_hash,
                    run_status, visual_facts, method_summary, solution_markdown,
                    final_answers_json, sympy_code, sympy_status
                ) VALUES (?, 'PRIMARY', 'm1', 's1_v1', 'ih', 'OK', '主路读图', '主法', '主路详细解法全文', ?, 'def verify(): return {\"主问\": str(1+1)}', 'PASSED')
                """,
                (
                    uid,
                    json.dumps(
                        [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="2", sympy_expr="2").model_dump()],
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO stage1_vlm_runs(
                    question_uid, role, model_name, prompt_version, input_hash,
                    run_status, visual_facts, method_summary, solution_markdown,
                    final_answers_json, sympy_status
                ) VALUES (?, 'SECONDARY', 'm2', 's1_v1', 'ih', 'OK', '辅路正确读图', '辅法', '辅路详细解法全文', ?, 'NOT_RUN')
                """,
                (
                    uid,
                    json.dumps(
                        [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="3", sympy_expr="3").model_dump()],
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.execute(
                "UPDATE questions SET m_verification_status = 'NEEDS_REVIEW', review_reason = 'ANSWER_MISMATCH' WHERE question_uid = ?",
                (uid,),
            )
            conn.execute(
                "UPDATE compile_state SET status = 'OK' WHERE question_uid = ? AND stage = 'S1'",
                (uid,),
            )
            conn.execute(
                "UPDATE compile_state SET status = 'BLOCKED' WHERE question_uid = ? AND stage = 'S2'",
                (uid,),
            )
            effective.recompute(conn, uid)
        conn.commit()

    server = create_review_server(
        db_path=db_path,
        config_dir=Path("config"),
        assets_dir=assets_dir,
        human_dir=human_dir,
        port=0,
        use_mock=True,
    )
    assert server.server_address[0] == "127.0.0.1"
    client = _ReviewHttpClient(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        # 1. 首页 HTML 与静态资源白名单 / 路径穿越防线
        st_html, html_bytes, ctype = client.raw("/")
        assert st_html == 200
        assert "text/html" in ctype
        assert "配图" in html_bytes.decode("utf-8")

        st_img, img_bytes, img_ctype = client.raw(f"/assets/{img1_name}")
        assert st_img == 200
        assert "image/png" in img_ctype
        assert len(img_bytes) > 0

        for bad_asset in [
            "../../config/settings.yaml",
            "..%2f..%2fconfig%2fsettings.yaml",
            "not_hex_16_chars.png",
            "0123456789abcdef.svg",
        ]:
            st_bad, _, _ = client.raw(f"/assets/{bad_asset}")
            assert st_bad in (400, 404)

        # 2. 队列与详情
        st_q, q_data = client.json("/api/queue")
        assert st_q == 200 and q_data["total"] == 3

        st_it, it_data = client.json(f"/api/item/{urllib.parse.quote(uid_a, safe='')}")
        assert st_it == 200
        assert it_data["item"]["image_captions"] == ["A", "B"]

        # 3. [2] 采纳辅路（验证连带采纳辅路 visual_facts + 4条同 bundle_id + 解除 S2 BLOCKED + 镜像导出）
        st_ad2, ad2_data = client.json(
            "/api/adopt",
            method="POST",
            body={"question_uid": uid_a, "role": "SECONDARY"},
        )
        assert st_ad2 == 200
        bundle_a = ad2_data["result"]["bundle_id"]

        with closing(get_connection(db_path)) as conn:
            qa = conn.execute("SELECT * FROM questions WHERE question_uid = ?", (uid_a,)).fetchone()
            assert qa["eff_verification_status"] == "HUMAN_VERIFIED"
            assert qa["eff_visual_facts"] == "辅路正确读图"
            assert qa["eff_is_servable"] == 1
            s2a = conn.execute(
                "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S2'",
                (uid_a,),
            ).fetchone()
            assert s2a["status"] == "STALE"
            ovs_a = conn.execute(
                "SELECT field_name FROM human_overrides WHERE bundle_id = ? AND is_active = 1 ORDER BY field_name",
                (bundle_a,),
            ).fetchall()
            assert [r["field_name"] for r in ovs_a] == [
                "final_answers_json",
                "solutions_override_json",
                "verification_status",
                "visual_facts",
            ]
            assert human_mirror.verify_snapshot(conn, human_dir=human_dir)["is_consistent"] is True

        # 4. [U] 撤销 bundle_a，验证退回 NEEDS_REVIEW 与 S2 BLOCKED
        st_undo, _ = client.json("/api/undo", method="POST", body={"bundle_id": bundle_a})
        assert st_undo == 200
        with closing(get_connection(db_path)) as conn:
            qa_u = conn.execute("SELECT eff_verification_status FROM questions WHERE question_uid = ?", (uid_a,)).fetchone()
            assert qa_u["eff_verification_status"] == "NEEDS_REVIEW"
            s2a_u = conn.execute(
                "SELECT status FROM compile_state WHERE question_uid = ? AND stage = 'S2'",
                (uid_a,),
            ).fetchone()
            assert s2a_u["status"] == "BLOCKED"

        # 5. [B] 一题多解全采（branch_1 + branch_2 均为 HUMAN）
        st_adb, _ = client.json(
            "/api/adopt",
            method="POST",
            body={"question_uid": uid_a, "role": "BOTH"},
        )
        assert st_adb == 200
        with closing(get_connection(db_path)) as conn:
            sols_a = conn.execute(
                "SELECT branch_id, source_origin FROM question_solutions WHERE question_uid = ? ORDER BY branch_id",
                (uid_a,),
            ).fetchall()
            assert [(r["branch_id"], r["source_origin"]) for r in sols_a] == [
                ("branch_1", "HUMAN"),
                ("branch_2", "HUMAN"),
            ]

        # 6. [R] 辅助重算草稿（不落覆写，记入 llm_calls(stage='REVIEW_RESOLVE')）+ [E] 保存定稿
        # 先验证真实 Mock 路由下 [R] 会落入 llm_calls(stage='REVIEW_RESOLVE') 且不修改 questions 覆写状态
        st_res_mock, res_mock_data = client.json(
            "/api/resolve",
            method="POST",
            body={"question_uid": uid_b, "visual_facts": "人工修正读图：标准底6高8"},
        )
        assert st_res_mock == 200
        assert res_mock_data["draft"]["sympy_status"] == "PASSED"
        with closing(get_connection(db_path)) as conn:
            llm_rows = conn.execute(
                "SELECT stage, slot, status FROM llm_calls WHERE question_uid = ? AND stage = 'REVIEW_RESOLVE'",
                (uid_b,),
            ).fetchall()
            assert len(llm_rows) == 1
            assert llm_rows[0]["slot"] == "text_llm"
            assert llm_rows[0]["status"] == "OK"

        async def fake_resolve_llm(
            self: LLMGateway,
            *,
            slot: str,
            stage: str,
            messages: list[dict[str, Any]],
            schema: type[Any],
            question_uid: str | None = None,
            prompt_version: str = "",
            mock_key: str | None = None,
        ) -> LLMCallResult[Any]:
            return LLMCallResult(
                parsed=ReviewResolveOutput(
                    method_summary="人工修正读图重算",
                    solution_markdown="计算得 $6 \\times 7 = 42$。",
                    final_answers=[
                        AnswerPart(
                            label="主问",
                            answer_type="NUMERIC",
                            verify_mode="SYMBOLIC",
                            answer="42",
                            sympy_expr="42",
                        )
                    ],
                    sympy_code="def verify():\n    return {'主问': str(6 * 7)}",
                ),
                model_name="mock-text",
                raw_response="{}",
                call_id=1,
            )

        with patch.object(LLMGateway, "call_structured", new=fake_resolve_llm):
            st_res, res_data = client.json(
                "/api/resolve",
                method="POST",
                body={"question_uid": uid_b, "visual_facts": "人工修正：长6宽7"},
            )
        assert st_res == 200
        assert res_data["draft"]["sympy_status"] == "PASSED"
        assert res_data["draft"]["final_answers"][0]["sympy_expr"] == "42"

        # 草稿尚未落覆写
        with closing(get_connection(db_path)) as conn:
            qb_pre = conn.execute("SELECT eff_verification_status FROM questions WHERE question_uid = ?", (uid_b,)).fetchone()
            assert qb_pre["eff_verification_status"] == "NEEDS_REVIEW"

        # 提交 [E] 定稿
        st_ed, _ = client.json(
            "/api/edit",
            method="POST",
            body={
                "question_uid": uid_b,
                "visual_facts": res_data["draft"]["visual_facts"],
                "method_summary": res_data["draft"]["method_summary"],
                "final_answers": res_data["draft"]["final_answers"],
                "solution_markdown": res_data["draft"]["solution_markdown"],
                "sympy_code": res_data["draft"]["sympy_code"],
            },
        )
        assert st_ed == 200

        # 7. [X] 下架 uid_c
        st_ex, _ = client.json("/api/exclude", method="POST", body={"question_uid": uid_c})
        assert st_ex == 200
        with closing(get_connection(db_path)) as conn:
            qc = conn.execute("SELECT eff_is_excluded, eff_is_servable FROM questions WHERE question_uid = ?", (uid_c,)).fetchone()
            assert qc["eff_is_excluded"] == 1
            assert qc["eff_is_servable"] == 0

        # 8. 源题变更导致 OVERRIDE_STALE + [K] 重签恢复
        with closing(get_connection(db_path)) as conn:
            conn.execute("UPDATE raw_questions SET content_hash = 'h_a_v2' WHERE question_uid = ?", (uid_a,))
            refresh.handle_source_content_changed(conn, uid_a, "h_a_v2")
            conn.commit()
            qa_stale = conn.execute(
                "SELECT eff_verification_status, eff_review_reason FROM questions WHERE question_uid = ?",
                (uid_a,),
            ).fetchone()
            assert qa_stale["eff_verification_status"] == "NEEDS_REVIEW"
            assert "OVERRIDE_STALE" in qa_stale["eff_review_reason"]

        st_k, _ = client.json("/api/reconfirm", method="POST", body={"question_uid": uid_a})
        assert st_k == 200
        with closing(get_connection(db_path)) as conn:
            qa_k = conn.execute("SELECT eff_verification_status FROM questions WHERE question_uid = ?", (uid_a,)).fetchone()
            assert qa_k["eff_verification_status"] == "HUMAN_VERIFIED"

        # 9. 空库 human restore 零依赖复现（无 stage1_vlm_runs 行也能完整还原生效层与双分支解法）
        db2_path = tmp_path / "empty_restore.db"
        with closing(get_connection(db2_path)) as conn2:
            init_db(conn2, config_dir=Path("config"))
            _seed_question(conn2, uid=uid_a, source_id="q_rev_a", text="测试题目 q_rev_a", content_hash="h_a_v2")
            _seed_question(conn2, uid=uid_b, source_id="q_rev_b", text="测试题目 q_rev_b", content_hash="h_b")
            _seed_question(conn2, uid=uid_c, source_id="q_rev_c", text="测试题目 q_rev_c", content_hash="h_c")
            human_mirror.restore_snapshot(conn2, human_dir=human_dir, backups_dir=None)
            materialize_solutions(conn2, uid_a)
            materialize_solutions(conn2, uid_b)
            conn2.commit()

            assert conn2.execute("SELECT COUNT(*) FROM stage1_vlm_runs").fetchone()[0] == 0
            qa2 = conn2.execute("SELECT eff_verification_status, eff_is_servable FROM questions WHERE question_uid = ?", (uid_a,)).fetchone()
            assert qa2["eff_verification_status"] == "HUMAN_VERIFIED"
            assert qa2["eff_is_servable"] == 1
            sols2 = conn2.execute(
                "SELECT branch_id, source_origin, solution_markdown FROM question_solutions WHERE question_uid = ? ORDER BY branch_id",
                (uid_a,),
            ).fetchall()
            assert [(r["branch_id"], r["source_origin"], r["solution_markdown"]) for r in sols2] == [
                ("branch_1", "HUMAN", "主路详细解法全文"),
                ("branch_2", "HUMAN", "辅路详细解法全文"),
            ]
    finally:
        server.shutdown()
        server.server_close()


def test_cli_compile_stress_test_25_mock(tmp_path: Path):
    db_path = tmp_path / "stress25.db"
    assets_dir = tmp_path / "assets" / "images"
    reports_dir = tmp_path / "reports"

    run_stage0_ingest(
        db_path=db_path,
        root_dir=Path("题库"),
        config_dir=Path("config"),
        assets_dir=assets_dir,
        reports_dir=reports_dir,
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "shuati.cli.main",
            "compile",
            "--stage",
            "1",
            "--stress-test-25",
            "--mock",
            "--db",
            str(db_path),
            "--config-dir",
            "config",
            "--assets-dir",
            str(assets_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == EXIT_OK, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["command"] == "compile"
    assert payload["data"]["total_candidates"] == 25
    assert payload["data"]["processed"] == 25

    # 再次运行应全部命中指纹跳过
    proc2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "shuati.cli.main",
            "compile",
            "--stage",
            "1",
            "--stress-test-25",
            "--mock",
            "--db",
            str(db_path),
            "--config-dir",
            "config",
            "--assets-dir",
            str(assets_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc2.returncode == EXIT_OK
    payload2 = json.loads(proc2.stdout)
    assert payload2["data"]["processed"] == 0
    assert payload2["data"]["skipped_ok"] == 25
