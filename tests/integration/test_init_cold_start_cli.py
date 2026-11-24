"""冷启动底层 CLI 补强端到端集成测试（human reset / 多目录 ingest / loopback Ollama / doctor / anchors sample / Stage 1 export-import / skill install）。"""

from __future__ import annotations

import json
import shutil
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

from shuati.cli.main import app
from shuati.config import Settings, load_settings
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm.embeddings import EmbeddingClient
from shuati.mcp.server import dispatch_jsonrpc_message, get_tools_for_role
from shuati.models.domain import EXIT_PRECONDITION
from shuati.pipeline.stage0_ingest import run_stage0_ingest


def _copy_config_dir(tmp_path: Path, *, roots: list[str] | None = None) -> Path:
    cfg_dir = tmp_path / "config"
    shutil.copytree(Path("config"), cfg_dir)
    settings_path = cfg_dir / "settings.yaml"
    txt = settings_path.read_text(encoding="utf-8")
    txt = txt.replace('dir: "human/"', f'dir: "{(tmp_path / "human").as_posix()}"')
    if roots is not None:
        roots_yaml = "\n".join(f'    - "{Path(r).as_posix()}"' for r in roots)
        old_block = 'ingest:\n  roots:\n    - "题库"\n  dedup_near_default: true'
        new_block = f"ingest:\n  roots:\n{roots_yaml}\n  dedup_near_default: true"
        assert old_block in txt
        txt = txt.replace(old_block, new_block)
    settings_path.write_text(txt, encoding="utf-8")
    return cfg_dir


def _make_png(path: Path, color: tuple[int, int, int] = (20, 120, 220)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (64, 48), color=color)
    img.save(path, format="PNG")


@pytest.mark.asyncio
async def test_loopback_ollama_allows_empty_api_key_and_remote_rejects() -> None:
    seen_auth: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization"))
        payload = json.loads(request.content.decode("utf-8"))
        n = len(payload["input"])
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [1.0, 0.5, 0.25, 0.0]} for i in range(n)],
                "usage": {"prompt_tokens": 5},
            },
        )

    transport = httpx.MockTransport(_handler)
    for host in ("http://localhost:11434/v1", "http://127.0.0.1:11434/v1", "http://[::1]:11434/v1"):
        settings = Settings()
        settings.llm.embedding.base_url = host
        settings.llm.embedding.api_key = ""
        settings.llm.embedding.model = "bge-m3"
        client = EmbeddingClient(settings, transport=transport)
        try:
            vecs = await client.embed_texts(["三角形面积模型"], stage="TEST_LOOPBACK")
            assert vecs.shape == (1, 4)
        finally:
            await client.aclose()

    assert seen_auth == [None, None, None]

    # 非本地回环地址空 api_key 仍应拦截报 PreconditionError
    remote_settings = Settings()
    remote_settings.llm.embedding.base_url = "https://api.openai.com/v1"
    remote_settings.llm.embedding.api_key = ""
    remote_client = EmbeddingClient(remote_settings, transport=transport)
    try:
        with pytest.raises(PreconditionError, match="EMBEDDING_API_KEY"):
            await remote_client.embed_texts(["测试远程无 Key"], stage="TEST_REMOTE")
    finally:
        await remote_client.aclose()


def test_human_reset_with_existing_db_and_lossless_archive(tmp_path: Path) -> None:
    runner = CliRunner()
    cfg_dir = _copy_config_dir(tmp_path)
    db_path = tmp_path / "data" / "shuati.db"
    human_dir = tmp_path / "human"
    archive_dir = tmp_path / "data" / "backups"

    human_dir.mkdir(parents=True, exist_ok=True)
    old_learner_line = json.dumps(
        {
            "learner_id": "legacy_user",
            "display_name": "旧用户",
            "default_grade_subject": "小学奥数",
            "is_current": 1,
            "notes": "",
            "created_at": "2026-01-01 00:00:00",
        },
        ensure_ascii=False,
    )
    (human_dir / "learners.jsonl").write_text(old_learner_line + "\n", encoding="utf-8")
    (human_dir / "practice_log.jsonl").write_text('{"log_id": 1}\n', encoding="utf-8")

    # 初始化库并写入旧 learner 与外部错题
    with closing(get_connection(db_path)) as conn:
        init_db(conn, config_dir=cfg_dir)
        conn.execute(
            """
            INSERT INTO raw_questions(
                question_uid, grade_subject, source_type, source_collection,
                source_file, source_id, raw_question, content_hash
            ) VALUES ('primary_olympiad:ext:e1', '小学奥数', 'EXTERNAL_MISTAKE', 'external_mistake', '', 'e1', '外部错题', 'h_ext')
            """
        )
        conn.execute(
            """
            INSERT INTO questions(
                question_uid, grade_subject, m_question_text, m_format
            ) VALUES ('primary_olympiad:ext:e1', '小学奥数', '外部错题', '解答题')
            """
        )
        conn.execute("UPDATE learners SET is_current = 0;")
        conn.execute(
            """
            INSERT INTO learners(learner_id, display_name, default_grade_subject, is_current)
            VALUES ('legacy_user', '旧用户', '小学奥数', 1)
            """
        )
        conn.commit()

    # 1. dry-run 不改盘不删库
    r_dry = runner.invoke(
        app,
        [
            "human",
            "reset",
            "--learner-id",
            "alice",
            "--learner-name",
            "爱丽丝",
            "--archive-dir",
            str(archive_dir),
            "--human-dir",
            str(human_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--dry-run",
            "--json",
        ],
    )
    assert r_dry.exit_code == 0, r_dry.output
    d_dry = json.loads(r_dry.stdout)["data"]
    assert d_dry["dry_run"] is True
    assert len(d_dry["would_archive_files"]) == 2
    assert "legacy_user" in (human_dir / "learners.jsonl").read_text(encoding="utf-8")

    # 2. 真实 reset
    r_reset = runner.invoke(
        app,
        [
            "human",
            "reset",
            "--learner-id",
            "alice",
            "--learner-name",
            "爱丽丝",
            "--archive-dir",
            str(archive_dir),
            "--human-dir",
            str(human_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_reset.exit_code == 0, r_reset.output
    d_reset = json.loads(r_reset.stdout)["data"]
    assert d_reset["dry_run"] is False
    assert d_reset["archived_dir"] is not None
    arch_p = Path(d_reset["archived_dir"])
    assert (arch_p / "learners.jsonl").is_file()
    assert (arch_p / "practice_log.jsonl").is_file()
    assert d_reset["db_backup_path"] is not None and Path(d_reset["db_backup_path"]).is_file()

    # 校验 5 份干净 human/*.jsonl 且 human verify 一致
    learners_txt = (human_dir / "learners.jsonl").read_text(encoding="utf-8").strip()
    assert len(learners_txt.splitlines()) == 1
    assert json.loads(learners_txt)["learner_id"] == "alice"
    for empty_fn in (
        "external_questions.jsonl",
        "overrides.jsonl",
        "relation_overrides.jsonl",
        "practice_log.jsonl",
    ):
        assert (human_dir / empty_fn).read_text(encoding="utf-8") == ""

    r_ver = runner.invoke(
        app,
        [
            "human",
            "verify",
            "--from",
            str(human_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_ver.exit_code == 0, r_ver.output
    assert json.loads(r_ver.stdout)["data"]["is_consistent"] is True


def test_cold_start_full_loop_cli(tmp_path: Path) -> None:
    """模拟空仓克隆冷启动全闭环：
    human reset -> 多外部根目录 ingest (dry-run & 实写) -> doctor --mock ->
    anchors sample -> compile --stage 1 --mode export -> import ->
    unified calibrate-anchors (anchors_ready == 1) -> skill install --all。
    """
    runner = CliRunner()

    # 构造两个外部题库目录（不放入 repo 根目录）
    bank_a = tmp_path / "external_bank_a"
    bank_b = tmp_path / "external_bank_b"

    ch_arith = bank_a / "ch01_arith"
    ch_geom = bank_a / "ch02_geom"
    ch_phys = bank_b / "初中物理" / "ch01_motion"
    ch_arith.mkdir(parents=True)
    ch_geom.mkdir(parents=True)
    ch_phys.mkdir(parents=True)
    _make_png(ch_geom / "images" / "tri.png")

    arith_rows = [
        {"id": "a_1", "chapter": "第一章 secret", "question": "计算 12 + 30 等于多少？"},
        {"id": "a_2", "chapter": "第一章 secret", "question": "若长方形长为 15 厘米、宽为 8 厘米，求它的面积是多少平方厘米？"},
        {"id": "a_3", "chapter": "第一章 secret", "question": "甲乙两地相距 120 千米，货车每小时行 40 千米，几小时到达？"},
        # 近重复题（仅标点空白差异），验证 settings.ingest.dedup_near_default=True
        {"id": "a_4_dup", "chapter": "第一章 secret", "question": "计算 12+30 等于多少？"},
    ]
    (ch_arith / "problems.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in arith_rows) + "\n",
        encoding="utf-8",
    )

    geom_rows = [
        {
            "id": "g_1",
            "chapter": "第二章 几何",
            "question": "如图所示，直角三角形两直角边分别为 6 和 8，求斜边长。",
            "images": ["images/tri.png"],
        },
        {
            "id": "g_2",
            "chapter": "第二章 几何",
            "question": "一个正方形周长为 36 厘米，求其面积是多少平方厘米？",
        },
    ]
    (ch_geom / "problems.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in geom_rows) + "\n",
        encoding="utf-8",
    )

    phys_rows = [
        {"id": "p_1", "chapter": "机械运动", "question": "某物体匀速运动 10 秒通过 50 米，求速度是多少米每秒？"},
    ]
    (ch_phys / "problems.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in phys_rows) + "\n",
        encoding="utf-8",
    )

    cfg_dir = _copy_config_dir(tmp_path, roots=[str(bank_a), str(bank_b)])
    loaded_cfg = load_settings(cfg_dir / "settings.yaml")
    assert loaded_cfg.ingest.roots == [bank_a.as_posix(), bank_b.as_posix()]
    assert loaded_cfg.ingest.dedup_near_default is True

    db_path = tmp_path / "data" / "shuati.db"
    human_dir = tmp_path / "human"
    archive_dir = tmp_path / "data" / "backups"
    assets_dir = tmp_path / "data" / "assets" / "images"
    reports_dir = tmp_path / "data" / "reports"
    stage1_dir = tmp_path / "data" / "unified" / "stage3_4" / "stage1"

    # 1. 空仓状态先跑 doctor --mock（无需 SQLite 库）
    r_doc_ok = runner.invoke(
        app,
        [
            "doctor",
            "--mock",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_doc_ok.exit_code == 0, r_doc_ok.output
    doc_data = json.loads(r_doc_ok.stdout)["data"]
    assert doc_data["ok"] is True
    assert doc_data["db_exists"] is False
    assert not db_path.exists()
    assert doc_data["checks"]["sympy_sandbox"]["values"]["主问"] == "5"
    assert doc_data["checks"]["prompt_seams"]["ok"] is True
    assert doc_data["checks"]["embedding"]["dimension"] == 64

    # 验证 doctor 必选检查失败时返回 ok=false 且退出码 EXIT_PRECONDITION
    bad_cfg_dir = tmp_path / "bad_config"
    shutil.copytree(cfg_dir, bad_cfg_dir)
    bad_settings_path = bad_cfg_dir / "settings.yaml"
    bad_settings_txt = bad_settings_path.read_text(encoding="utf-8")
    bad_settings_txt = bad_settings_txt.replace(
        'base_url: "${EMBEDDING_BASE_URL:-https://api.openai.com/v1}"',
        'base_url: "https://api.openai.com/v1"',
    ).replace(
        'api_key: "${EMBEDDING_API_KEY:-}"',
        'api_key: ""',
    )
    bad_settings_path.write_text(bad_settings_txt, encoding="utf-8")

    r_doc_fail = runner.invoke(
        app,
        [
            "doctor",
            "--no-mock",
            "--db",
            str(db_path),
            "--config-dir",
            str(bad_cfg_dir),
            "--json",
        ],
    )
    assert r_doc_fail.exit_code == EXIT_PRECONDITION, r_doc_fail.output
    doc_fail_env = json.loads(r_doc_fail.stdout)
    assert doc_fail_env["ok"] is False
    assert doc_fail_env["data"]["checks"]["embedding"]["ok"] is False

    # 2. human reset（无数据库场景）
    r_hr = runner.invoke(
        app,
        [
            "human",
            "reset",
            "--learner-id",
            "bob",
            "--learner-name",
            "鲍勃",
            "--archive-dir",
            str(archive_dir),
            "--human-dir",
            str(human_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_hr.exit_code == 0, r_hr.output
    assert (human_dir / "learners.jsonl").is_file()
    assert not db_path.exists()

    # 3. 注册初中物理后，执行省略 --root 的多目录 ingest --dry-run 与正式 ingest
    r_gs = runner.invoke(
        app,
        [
            "grade-subject",
            "add",
            "--name",
            "初中物理",
            "--slug",
            "junior_physics",
            "--family",
            "PHYSICS",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_gs.exit_code == 0, r_gs.output

    r_ing_dry = runner.invoke(
        app,
        [
            "ingest",
            "--dry-run",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--assets-dir",
            str(assets_dir),
            "--reports-dir",
            str(reports_dir),
            "--json",
        ],
    )
    assert r_ing_dry.exit_code == 0, r_ing_dry.output
    d_dry = json.loads(r_ing_dry.stdout)["data"]
    assert d_dry["dry_run"] is True
    assert d_dry["total_scanned"] == 7
    assert len(d_dry["merged_near_duplicates"]) == 1
    assert not assets_dir.exists()

    # 测试多次 --root 传参 + 省略 --root 自动读取 settings.ingest.roots
    r_ing = runner.invoke(
        app,
        [
            "ingest",
            "--root",
            str(bank_a),
            "--root",
            str(bank_b),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--assets-dir",
            str(assets_dir),
            "--reports-dir",
            str(reports_dir),
            "--json",
        ],
    )
    assert r_ing.exit_code == 0, r_ing.output
    d_ing = json.loads(r_ing.stdout)["data"]
    assert d_ing["total_scanned"] == 7
    assert len(d_ing["merged_near_duplicates"]) == 1
    assert "primary_olympiad:ch01_arith:a_1" in d_ing["newly_ingested"]
    assert "junior_physics:初中物理/ch01_motion:p_1" in d_ing["newly_ingested"]

    # 单目录向后兼容校验（root_dir=... 不改变 UID 规则）
    single_rep = run_stage0_ingest(
        db_path=tmp_path / "single_compat.db",
        root_dir=bank_a,
        config_dir=cfg_dir,
        assets_dir=tmp_path / "single_assets",
        reports_dir=tmp_path / "single_reports",
        dry_run=True,
    )
    assert "primary_olympiad:ch01_arith:a_1" in single_rep.newly_ingested

    # 4. shuati anchors sample 分层抽样（严禁泄露 raw_chapter）
    r_samp = runner.invoke(
        app,
        [
            "anchors",
            "sample",
            "--grade-subject",
            "小学奥数",
            "--count",
            "4",
            "--db",
            str(db_path),
            "--json",
        ],
    )
    assert r_samp.exit_code == 0, r_samp.output
    samp_data = json.loads(r_samp.stdout)["data"]
    assert samp_data["total_pool"] == 5
    assert samp_data["count"] == 4
    cands = samp_data["candidates"]
    assert any(c["has_images"] for c in cands)
    assert any(not c["has_images"] for c in cands)
    for c in cands:
        assert "raw_chapter" not in c
        assert "secret" not in json.dumps(c, ensure_ascii=False)

    # 5. anchors_ready == 0 时，先用 compile --stage 1 --mode export -> import 验真 4 道候选锚点题
    cand_uids = [c["question_uid"] for c in cands]
    r_s1_exp = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "export",
            "--grade-subject",
            "小学奥数",
            "--only-uids",
            ",".join(cand_uids),
            "--tasks-dir",
            str(stage1_dir),
            "--batch-size",
            "10",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--assets-dir",
            str(assets_dir),
            "--json",
        ],
    )
    assert r_s1_exp.exit_code == 0, r_s1_exp.output
    exp_data = json.loads(r_s1_exp.stdout)["data"]
    assert exp_data["exported_questions"] == 4
    assert exp_data["subagent_model"] == "gemini-4-neon-medium"
    assert exp_data["manifest"]["status"] == "PENDING_IMPORT"

    # 验证批文件无 base64 大块且无禁止泄露字段，visual_facts 在 flat_schema 首位
    batch_txt = (stage1_dir / "batches" / "batch_0001.jsonl").read_text(encoding="utf-8")
    assert "data:image/" not in batch_txt
    assert "raw_chapter" not in batch_txt
    assert "question_uid" not in batch_txt
    assert '"type": "image_path"' in batch_txt

    flat_schema = json.loads((stage1_dir / "flat_schema.json").read_text(encoding="utf-8"))
    item_props = flat_schema["properties"]["items"]["items"]["properties"]
    assert next(iter(item_props.keys())) == "visual_facts"
    assert '"$defs"' not in json.dumps(flat_schema)

    # 防踩踏锁校验：未回灌再次 export 必须被拦截
    r_stomp = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "export",
            "--grade-subject",
            "小学奥数",
            "--tasks-dir",
            str(stage1_dir),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--assets-dir",
            str(assets_dir),
            "--json",
        ],
    )
    assert r_stomp.exit_code == EXIT_PRECONDITION

    task_map = json.loads((stage1_dir / "stage1_task_map.json").read_text(encoding="utf-8"))
    ans_by_uid: dict[str, tuple[str, str]] = {
        "primary_olympiad:ch01_arith:a_1": ("42", "def verify():\n    return {'主问': str(12 + 30)}\n"),
        "primary_olympiad:ch01_arith:a_2": ("120", "def verify():\n    return {'主问': str(15 * 8)}\n"),
        "primary_olympiad:ch01_arith:a_3": ("3", "def verify():\n    return {'主问': str(120 // 40)}\n"),
        "primary_olympiad:ch02_geom:g_1": ("10", "import math\ndef verify():\n    return {'主问': str(int(math.hypot(6, 8)))}\n"),
        "primary_olympiad:ch02_geom:g_2": ("81", "def verify():\n    return {'主问': str((36 // 4) ** 2)}\n"),
    }

    built_items: list[dict[str, Any]] = []
    for tid, meta in task_map["items"].items():
        ans, code = ans_by_uid[meta["question_uid"]]
        built_items.append(
            {
                "visual_facts": "直角三角形" if meta["has_images"] else "",
                "visual_ambiguity_flag": False,
                "visual_ambiguity_note": "",
                "method_summary": "直接代数计算",
                "solution_markdown": f"计算得答案为 {ans}。",
                "final_answers": [
                    {
                        "label": "主问",
                        "answer_type": "NUMERIC",
                        "verify_mode": "SYMBOLIC",
                        "collection_mode": "SINGLE",
                        "answer": ans,
                        "sympy_expr": ans,
                    }
                ],
                "sympy_code": code,
                "task_item_id": tid,
            }
        )

    # 5a. 先测残缺回传（少 1 题）被 bridge 强制拦截
    trunc_results = stage1_dir / "results_trunc.jsonl"
    trunc_results.write_text(
        json.dumps(
            {"batch_id": "stage1_batch_0001", "result": {"items": built_items[:-1]}},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    r_imp_trunc = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "import",
            "--tasks-dir",
            str(stage1_dir),
            "--results",
            str(trunc_results),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_imp_trunc.exit_code == 0, r_imp_trunc.output
    trunc_data = json.loads(r_imp_trunc.stdout)["data"]
    assert trunc_data["ingested"] == 0
    assert trunc_data["batch_errors"] == 1
    assert trunc_data["manifest_status"] == "PENDING_IMPORT"
    assert (stage1_dir / "retry_tasks.jsonl").is_file()

    # 5b. 完整回传 -> 通过 SymPy 验算晋升 VERIFIED_CONSENSUS 并锁定 subagent:gemini-4-neon-medium
    full_results = stage1_dir / "results.jsonl"
    full_results.write_text(
        json.dumps(
            {"batch_id": "stage1_batch_0001", "result": {"items": built_items}},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    r_s1_imp = runner.invoke(
        app,
        [
            "compile",
            "--stage",
            "1",
            "--mode",
            "import",
            "--tasks-dir",
            str(stage1_dir),
            "--results",
            str(full_results),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--assets-dir",
            str(assets_dir),
            "--json",
        ],
    )
    assert r_s1_imp.exit_code == 0, r_s1_imp.output
    imp_data = json.loads(r_s1_imp.stdout)["data"]
    assert imp_data["ingested"] == 4
    assert imp_data["verified_consensus"] == 4
    assert imp_data["manifest_status"] == "IMPORTED"

    with closing(get_connection(db_path, readonly=True)) as conn:
        for uid in cand_uids:
            q_r = conn.execute(
                "SELECT eff_verification_status FROM questions WHERE question_uid = ?",
                (uid,),
            ).fetchone()
            cs_r = conn.execute(
                "SELECT status, model_name FROM compile_state WHERE question_uid = ? AND stage = 'S1'",
                (uid,),
            ).fetchone()
            assert q_r["eff_verification_status"] == "VERIFIED_CONSENSUS"
            assert cs_r["status"] == "OK"
            assert cs_r["model_name"] == "subagent:gemini-4-neon-medium"

    # 6. 写入 anchors_primary_olympiad.yaml 并执行 unified calibrate-anchors 解锁 anchors_ready == 1
    anchors_yaml = cfg_dir / "anchors" / "anchors_primary_olympiad.yaml"
    anchors_yaml.parent.mkdir(parents=True, exist_ok=True)
    tiers = [
        ("L1_基础直达", 1, 1, 1, 1, 1120),
        ("L2_常规巩固", 1, 2, 1, 1, 1225),
        ("L3_综合中档", 2, 2, 2, 1, 1510),
        ("L4-A_进阶拔高(繁)", 2, 3, 2, 3, 1745),
    ]
    a_lines = ["grade_subject: 小学奥数", 'version: "1.0"', "anchors:"]
    for uid, (tier, c, s, t, l_score, elo) in zip(cand_uids, tiers, strict=True):
        a_lines.extend(
            [
                f'  - question_uid: "{uid}"',
                f'    target_tier: "{tier}"',
                "    scores:",
                f"      C: {c}",
                f"      S: {s}",
                f"      T: {t}",
                f"      L: {l_score}",
                f"    base_elo: {elo}",
                f'    rationale: "冷启动锚点 {tier}"',
            ]
        )
    anchors_yaml.write_text("\n".join(a_lines) + "\n", encoding="utf-8")

    r_cal = runner.invoke(
        app,
        [
            "unified",
            "calibrate-anchors",
            "--grade-subject",
            "小学奥数",
            "--anchors-file",
            str(anchors_yaml),
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
    )
    assert r_cal.exit_code == 0, r_cal.output
    with closing(get_connection(db_path, readonly=True)) as conn:
        gs_check = conn.execute(
            "SELECT anchors_ready FROM grade_subjects WHERE grade_subject = '小学奥数'"
        ).fetchone()
        assert gs_check["anchors_ready"] == 1

    # 7. 验证 MCP compiler 角色包含 doctor 与 anchors_sample，worker 仍严格仅有 2 个工具
    assert [t["name"] for t in get_tools_for_role("worker")] == ["verify_sympy", "validate_draft"]
    compiler_names = [t["name"] for t in get_tools_for_role("compiler")]
    assert "doctor" in compiler_names and "anchors_sample" in compiler_names

    mcp_samp = dispatch_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "anchors_sample", "arguments": {"grade_subject": "小学奥数", "count": 3}},
        },
        role="compiler",
        db_path=db_path,
        config_dir=cfg_dir,
    )
    assert mcp_samp is not None and mcp_samp["result"]["isError"] is False
    assert mcp_samp["result"]["structuredContent"]["count"] == 3

    # 8. shuati skill install --all + 兼容旧调用
    pi_skills_dir = tmp_path / ".pi" / "skills"
    r_sk_all = runner.invoke(
        app,
        [
            "skill",
            "install",
            "--all",
            "--target-dir",
            str(pi_skills_dir),
            "--copy",
            "--json",
        ],
    )
    assert r_sk_all.exit_code == 0, r_sk_all.output
    sk_all_data = json.loads(r_sk_all.stdout)["data"]
    assert sk_all_data["count"] == 3
    assert sk_all_data["all_skill_md_exists"] is True
    for name in ("shuati", "shuati-init", "shuati-compiler"):
        assert (pi_skills_dir / name / "SKILL.md").is_file()

    legacy_dst = tmp_path / "legacy_skill" / "shuati"
    r_sk_legacy = runner.invoke(
        app,
        ["skill", "install", "--target", str(legacy_dst), "--copy", "--json"],
    )
    assert r_sk_legacy.exit_code == 0, r_sk_legacy.output
    legacy_data = json.loads(r_sk_legacy.stdout)["data"]
    assert set(legacy_data.keys()) == {"source", "target", "mode", "skill_md_exists"}
    assert legacy_data["skill_md_exists"] is True
