r"""Repro P2-3: `shuati audit --config-dir <dir>` 漏传 `config_dir` 至 `run_stage_q_audit`。

`cli/main.py` 的 `audit_cmd` 声明了 `--config-dir` 并用于加载 `settings.yaml`，
但在调用 `run_stage_q_audit(...)` 时漏传 `config_dir=config_dir`，导致 Stage Q 新增的
`section_8_legacy_tags_comparison["method_blacklist_purge"]` 始终硬编码读取当前工作目录下的
`Path("config") / "method_blacklist.yaml"`，完全忽略用户指定的 `--config-dir`。

运行：`.\.venv\Scripts\python.exe tests/repro/repro_p2_cli_audit_drops_config_dir.py`
期望修复后 Exit Code == 0；当前代码实测 Exit Code != 0。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shuati.cli.main import app  # noqa: E402
from shuati.core import effective  # noqa: E402
from shuati.db import human_mirror  # noqa: E402
from shuati.db.connection import get_connection  # noqa: E402
from shuati.db.schema import init_db  # noqa: E402
from shuati.models.domain import AnswerPart  # noqa: E402

GS = "小学奥数"


def _run_check(tmp: Path) -> None:
    cfg = tmp / "custom_config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.yaml").write_text("pipeline:\n  use_mock_llm: true\n", encoding="utf-8")
    (cfg / "method_blacklist.yaml").write_text(
        "blacklist_terms:\n  - 自定义万金油泛词X\n",
        encoding="utf-8",
    )

    db_path = tmp / "audit_cfg.db"
    conn = get_connection(db_path)
    try:
        init_db(conn, cfg)
        ans = json.dumps(
            [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="5", sympy_expr="5").model_dump()],
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection, source_id,
                raw_question, legacy_tags_json, content_hash
            ) VALUES ('p:c:q1', ?, 'BATCH_JSONL', 'ch01', 'q1', '1+4=?', ?, 'h1');
            """,
            (GS, json.dumps({"method": ["自定义万金油泛词X"]}, ensure_ascii=False)),
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_format,
                m_verification_status, m_final_answers_json, m_primary_l1_module,
                m_c_score, m_s_score, m_t_score, m_l_score
            ) VALUES ('p:c:q1', ?, '1+4=?', '解答题', 'VERIFIED_CONSENSUS', ?, '计算', 1, 1, 1, 1);
            """,
            (GS, ans),
        )
        effective.recompute(conn, "p:c:q1")
        human_mirror.export_snapshot(conn, tmp / "human")
        conn.commit()
    finally:
        conn.close()

    res = CliRunner().invoke(
        app,
        [
            "audit",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg),
            "--human-dir",
            str(tmp / "human"),
            "--vectors-dir",
            str(tmp / "vec"),
            "--reports-dir",
            str(tmp / "rep"),
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    mb = payload["data"]["report"]["section_8_legacy_tags_comparison"]["method_blacklist_purge"]
    assert mb["blacklist_terms_loaded"] == 1 and mb["legacy_hit_question_count"] == 1, (
        f"BUG: `shuati audit --config-dir` 未将 config_dir 传给 run_stage_q_audit，"
        f"实测 method_blacklist_purge={mb}"
    )


def test_repro_p2_cli_audit_drops_config_dir(tmp_path: Path) -> None:
    _run_check(tmp_path)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="repro_p2_audit_cfg_"))
    try:
        _run_check(tmp)
        print("[PASS] shuati audit --config-dir forwarded properly")
        return 0
    except AssertionError as exc:
        print(f"[FAIL] {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
