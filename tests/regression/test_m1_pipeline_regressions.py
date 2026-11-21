"""M1 Round 1 实证审查 P1-P9 回归测试套件。"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import sqlite3
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from shuati.core import effective
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm.gateway import LLMCallResult, LLMGateway, LLMGatewayError
from shuati.models.domain import AnswerPart
from shuati.models.schemas import (
    MultiSolutionJudgeOutput,
    QualitativeJudgeOutput,
    Stage1PrimaryOutput,
    Stage1SecondaryOutput,
)
from shuati.pipeline.stage1_solve import materialize_solutions, run_stage1_compile
from shuati.review.server import create_review_server
from shuati.services import facade

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
REVIEW_HTML_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "shuati" / "review" / "review.html"
)


def _make_db(db_path: Path) -> sqlite3.Connection:
    conn = get_connection(db_path)
    init_db(conn, config_dir=CONFIG_DIR)
    return conn


def _seed_question(
    conn: sqlite3.Connection,
    *,
    uid: str,
    text: str = "求长 3、宽 4 的长方形面积。",
    content_hash: str = "h0",
    cas_images: list[str] | None = None,
    image_captions: list[str] | None = None,
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
        (uid, uid.split(":")[-1], text, json.dumps(cas), json.dumps(caps), content_hash),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, cas_images_json, image_captions_json,
            has_images, m_question_text, m_options_json, m_format
        ) VALUES (?, '小学奥数', ?, ?, ?, ?, '{}', '解答题')
        """,
        (uid, json.dumps(cas), json.dumps(caps), 1 if cas else 0, text),
    )
    for stg in ("S1", "S2", "S3C", "S3D", "S4"):
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, 'PENDING')",
            (uid, stg),
        )
    effective.recompute(conn, uid)
    conn.commit()


def _num_part(answer: str, label: str = "主问") -> AnswerPart:
    return AnswerPart(
        label=label,
        answer_type="NUMERIC",
        verify_mode="SYMBOLIC",
        answer=answer,
        sympy_expr=answer,
    )


def _make_fake_gateway(
    *,
    fail_uids: set[str] | None = None,
    secondary_label: str = "主问",
) -> Any:
    fail = fail_uids or set()

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
        if question_uid in fail:
            raise LLMGatewayError("Simulated 503", error_kind="GATEWAY_ERROR")
        if stage == "S1" and slot == "vlm_primary":
            return LLMCallResult(
                parsed=Stage1PrimaryOutput(
                    visual_facts="",
                    method_summary="长乘宽",
                    solution_markdown="答案 12",
                    final_answers=[_num_part("12")],
                    sympy_code="def verify():\n    return {'主问': str(12 + 0)}",
                ),
                model_name="mock-primary",
                raw_response="{}",
            )
        if stage == "S1" and slot == "vlm_secondary":
            return LLMCallResult(
                parsed=Stage1SecondaryOutput(
                    visual_facts="",
                    method_summary="长乘宽",
                    solution_markdown="12",
                    final_answers=(
                        [_num_part("12"), _num_part("13")]
                        if secondary_label == "DUP"
                        else [_num_part("12", secondary_label)]
                    ),
                ),
                model_name="mock-secondary",
                raw_response="{}",
            )
        if schema is MultiSolutionJudgeOutput:
            return LLMCallResult(
                parsed=MultiSolutionJudgeOutput(is_distinct_method=False, reason="t"),
                model_name="mock-judge",
                raw_response="{}",
            )
        return LLMCallResult(
            parsed=QualitativeJudgeOutput(equivalent=True, confidence="HIGH", reason="ok"),
            model_name="mock-judge",
            raw_response="{}",
        )

    return fake_call_structured


def _http_req(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


@pytest.mark.asyncio
async def test_p1_missing_image_fails_single_question_only(tmp_path: Path) -> None:
    assets = tmp_path / "assets" / "images"
    assets.mkdir(parents=True)
    db = tmp_path / "t.db"
    good = "primary_olympiad:ch_test:a_good"
    bad = "primary_olympiad:ch_test:b_bad"
    with closing(_make_db(db)) as conn:
        _seed_question(conn, uid=good, content_hash="hg")
        _seed_question(
            conn, uid=bad, content_hash="hb", cas_images=["0123456789abcdef.png"], image_captions=[""]
        )

    with patch.object(LLMGateway, "call_structured", new=_make_fake_gateway()):
        res = await run_stage1_compile(db_path=db, config_dir=CONFIG_DIR, assets_dir=assets)

    assert res.failed == 1
    with closing(get_connection(db)) as conn:
        rows = {
            r["question_uid"]: r["status"]
            for r in conn.execute("SELECT question_uid, status FROM compile_state WHERE stage='S1'")
        }
        assert rows[good] == "OK"
        assert rows[bad] == "FAILED"


def test_p2_submit_manual_review_rejects_empty_final_answers(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    uid = "primary_olympiad:ch_test:q_empty"
    with closing(_make_db(db)) as conn:
        _seed_question(conn, uid=uid)
        with pytest.raises(PreconditionError):
            facade.submit_manual_review(
                conn,
                uid,
                visual_facts="",
                final_answers=[],
                solution_markdown="",
                human_dir=tmp_path / "human",
            )
        row = conn.execute(
            "SELECT eff_verification_status, eff_is_servable FROM questions WHERE question_uid = ?",
            (uid,),
        ).fetchone()
        assert row["eff_verification_status"] == "PENDING"
        assert row["eff_is_servable"] == 0


@pytest.mark.asyncio
async def test_p3_human_verified_exhausted_does_not_block_s2(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    uid = "primary_olympiad:ch_test:q_hv"
    with closing(_make_db(db)) as conn:
        _seed_question(conn, uid=uid)
        facade.submit_manual_review(
            conn,
            uid,
            visual_facts="",
            final_answers=[_num_part("12")],
            solution_markdown="人工定稿 12",
            human_dir=tmp_path / "human",
        )

    with patch.object(LLMGateway, "call_structured", new=_make_fake_gateway(fail_uids={uid})):
        for _ in range(3):
            await run_stage1_compile(
                db_path=db,
                config_dir=CONFIG_DIR,
                assets_dir=tmp_path / "assets",
                include_human_verified=True,
            )

    with closing(get_connection(db)) as conn:
        eff = conn.execute(
            "SELECT eff_verification_status FROM questions WHERE question_uid=?", (uid,)
        ).fetchone()[0]
        s2 = conn.execute(
            "SELECT status FROM compile_state WHERE question_uid=? AND stage='S2'", (uid,)
        ).fetchone()[0]
        assert eff == "HUMAN_VERIFIED"
        assert s2 != "BLOCKED"


def test_p4_answer_solution_conflict_downgrades_effective_status(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    uid = "primary_olympiad:ch_test:q_conf"
    with closing(_make_db(db)) as conn:
        _seed_question(conn, uid=uid, content_hash="hc")
        conn.execute(
            """
            INSERT INTO stage1_vlm_runs(question_uid, role, model_name, prompt_version, input_hash,
                run_status, visual_facts, method_summary, solution_markdown, final_answers_json,
                sympy_code, sympy_status)
            VALUES (?, 'PRIMARY', 'm', 's1_v1', 'ih', 'OK', '', '法', '解 12', ?, 'x', 'PASSED')
            """,
            (uid, json.dumps([_num_part("12").model_dump()], ensure_ascii=False)),
        )
        conn.execute(
            """
            INSERT INTO human_overrides(question_uid, field_name, override_value, reason, source,
                base_fingerprint, is_active, is_suspended, suspended_reason)
            VALUES (?, 'final_answers_json', ?, 'r', 'PI_CLI', 'hc', 1, 0, '')
            """,
            (uid, json.dumps([_num_part("99").model_dump()], ensure_ascii=False)),
        )
        conn.commit()
        effective.recompute(conn, uid)
        materialize_solutions(conn, uid)
        effective.recompute(conn, uid)
        conn.commit()
        row = conn.execute(
            "SELECT eff_verification_status, eff_review_reason, eff_is_servable FROM questions WHERE question_uid=?",
            (uid,),
        ).fetchone()
        assert "ANSWER_SOLUTION_CONFLICT" in row["eff_review_reason"]
        assert row["eff_verification_status"] == "NEEDS_REVIEW"
        assert row["eff_is_servable"] == 0


@pytest.mark.asyncio
async def test_p5_normalize_error_exhausted_blocks_s2(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    uid = "primary_olympiad:ch_test:q_norm"
    with closing(_make_db(db)) as conn:
        _seed_question(conn, uid=uid)

    with patch.object(LLMGateway, "call_structured", new=_make_fake_gateway(secondary_label="DUP")):
        for _ in range(3):
            await run_stage1_compile(
                db_path=db, config_dir=CONFIG_DIR, assets_dir=tmp_path / "assets"
            )

    with closing(get_connection(db)) as conn:
        eff = conn.execute(
            "SELECT eff_verification_status FROM questions WHERE question_uid=?", (uid,)
        ).fetchone()[0]
        s2 = conn.execute(
            "SELECT status FROM compile_state WHERE question_uid=? AND stage='S2'", (uid,)
        ).fetchone()[0]
        assert eff == "NEEDS_REVIEW"
        assert s2 == "BLOCKED"


def test_p6_and_p8_review_server_csrf_and_sympy_code_validation(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    uid = "primary_olympiad:ch_test:q_sec"
    with closing(_make_db(db)) as conn:
        _seed_question(conn, uid=uid)

    server = create_review_server(
        db_path=db,
        config_dir=CONFIG_DIR,
        assets_dir=tmp_path / "assets",
        human_dir=tmp_path / "human",
        port=0,
        use_mock=True,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # P6: 跨站 text/plain 与非法 Origin/Host 被拒，且不写覆写
        st_ct, _ = _http_req(
            port,
            "POST",
            "/api/exclude",
            body=json.dumps({"question_uid": uid}).encode(),
            headers={
                "Content-Type": "text/plain",
                "Origin": "http://evil.example",
                "Host": "evil.example:8765",
            },
        )
        assert st_ct in (403, 415)

        st_orig, _ = _http_req(
            port,
            "POST",
            "/api/exclude",
            body=json.dumps({"question_uid": uid}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
                "Host": f"127.0.0.1:{port}",
            },
        )
        assert st_orig == 403

        # P8: sympy_code 非字符串返回 400 而非 500
        st_edit, _ = _http_req(
            port,
            "POST",
            "/api/edit",
            body=json.dumps(
                {
                    "question_uid": uid,
                    "visual_facts": "",
                    "final_answers": [_num_part("12").model_dump()],
                    "solution_markdown": "x",
                    "sympy_code": ["def verify(): pass"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert st_edit == 400
    finally:
        server.shutdown()
        server.server_close()


def test_p7_and_p9_review_html_keydown_guard_and_alt_escaping(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")

    html = REVIEW_HTML_PATH.read_text(encoding="utf-8")

    # P7: 修饰键组合不得误触发快捷键
    m_kd = re.search(
        r'document\.addEventListener\("keydown",\s*\((ev)\)\s*=>\s*\{(.*?)\n\}\);',
        html,
        re.S,
    )
    assert m_kd
    js_kd = r"""
const calls = [];
const doAdopt = (r) => calls.push("adopt:" + r);
const toggleEditor = () => calls.push("toggleEditor");
const doResolve = () => calls.push("doResolve");
const doExclude = () => calls.push("doExclude");
const doSkip = () => calls.push("doSkip");
const doReconfirm = () => calls.push("doReconfirm");
const doUndo = () => calls.push("doUndo");
const doSaveEdit = () => calls.push("doSaveEdit");
const document = { activeElement: { tagName: "BODY", blur() {} } };
function handler(ev) {
__BODY__
}
const bad = [];
for (const key of ["r", "u", "x", "s", "b", "k", "e", "1", "2"]) {
  for (const mod of ["ctrlKey", "metaKey", "altKey"]) {
    calls.length = 0;
    handler({ key, [mod]: true, preventDefault() {} });
    if (calls.length) bad.push(mod + "+" + key);
  }
}
console.log(JSON.stringify(bad));
""".replace("__BODY__", m_kd.group(2))
    p_kd = tmp_path / "kd.js"
    p_kd.write_text(js_kd, encoding="utf-8")
    out_kd = subprocess.run([node, str(p_kd)], capture_output=True, text=True, timeout=30)
    assert out_kd.returncode == 0, out_kd.stderr
    assert json.loads(out_kd.stdout.strip().splitlines()[-1]) == []

    # P9: escapeHtml 必须转义双引号，阻止 alt 属性注入
    fn = re.search(r"function escapeHtml\(str\) \{.*?\n\}", html, re.S)
    tpl = re.search(
        r"gallery\.innerHTML = imgs\.map\(\(fn, idx\) => \{(.*?)\n    \}\)\.join\(\"\"\);",
        html,
        re.S,
    )
    assert fn and tpl
    js_alt = (
        fn.group(0)
        + r"""
const imgs = ["0123456789abcdef.png"];
const caps = ['x" onerror="window.__pwned=1'];
const out = imgs.map((fn, idx) => {"""
        + tpl.group(1)
        + r"""
}).join("");
console.log(out);
"""
    )
    p_alt = tmp_path / "alt.js"
    p_alt.write_text(js_alt, encoding="utf-8")
    out_alt = subprocess.run([node, str(p_alt)], capture_output=True, text=True, timeout=30)
    assert out_alt.returncode == 0, out_alt.stderr
    img_tag = re.search(r"<img[^>]*>", out_alt.stdout, re.S)
    assert img_tag
    assert re.search(r'"\s*onerror="', img_tag.group(0)) is None
