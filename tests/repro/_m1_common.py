"""M1 Round 1 repro 共享夹具（仅测试辅助，不修改 src/）。

运行方式：在仓库根目录执行 `.venv\\Scripts\\python.exe tests\\repro\\repro_m1_<issue>.py`。
涉及 asyncio / 回环 TCP 的脚本需在非沙盒（BypassSandbox=true）下运行。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from shuati.core import effective  # noqa: E402
from shuati.db.connection import get_connection  # noqa: E402
from shuati.db.schema import init_db  # noqa: E402
from shuati.llm.gateway import LLMCallResult, LLMGateway, LLMGatewayError  # noqa: E402
from shuati.models.domain import AnswerPart  # noqa: E402
from shuati.models.schemas import (  # noqa: E402
    MultiSolutionJudgeOutput,
    QualitativeJudgeOutput,
    Stage1PrimaryOutput,
    Stage1SecondaryOutput,
)

CONFIG_DIR = ROOT / "config"


def make_db(db_path: Path) -> sqlite3.Connection:
    conn = get_connection(db_path)
    init_db(conn, config_dir=CONFIG_DIR)
    return conn


def seed_question(
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


def num_part(answer: str, label: str = "主问") -> AnswerPart:
    return AnswerPart(
        label=label,
        answer_type="NUMERIC",
        verify_mode="SYMBOLIC",
        answer=answer,
        sympy_expr=answer,
    )


def primary_ok(answer: str = "12") -> Stage1PrimaryOutput:
    return Stage1PrimaryOutput(
        visual_facts="",
        method_summary="长乘宽",
        solution_markdown=f"答案 {answer}",
        final_answers=[num_part(answer)],
        sympy_code=f"def verify():\n    return {{'主问': str({answer} + 0)}}",
    )


def make_fake_gateway(
    *,
    fail_uids: set[str] | None = None,
    secondary_label: str = "主问",
):
    """返回可 patch 到 LLMGateway.call_structured 的假实现。"""
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
            return LLMCallResult(parsed=primary_ok(), model_name="mock-primary", raw_response="{}")
        if stage == "S1" and slot == "vlm_secondary":
            return LLMCallResult(
                parsed=Stage1SecondaryOutput(
                    visual_facts="",
                    method_summary="长乘宽",
                    solution_markdown="12",
                    final_answers=(
                        [num_part("12"), num_part("13")]
                        if secondary_label == "DUP"
                        else [num_part("12", secondary_label)]
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


def start_review_server(db_path: Path, assets_dir: Path, human_dir: Path):
    from shuati.review.server import create_review_server

    server = create_review_server(
        db_path=db_path,
        config_dir=CONFIG_DIR,
        assets_dir=assets_dir,
        human_dir=human_dir,
        port=0,
        use_mock=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def http_request(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()
