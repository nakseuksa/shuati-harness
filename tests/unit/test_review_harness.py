"""Unit tests for scripts/review_harness.py, config/review_plan.yaml, and external review rules."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_harness as rh  # noqa: E402

EXPECTED_SPLIT_MEGA_FILES = {
    "src/shuati/core/taxonomy.py",
    "src/shuati/pipeline/stage0_ingest.py",
    "src/shuati/pipeline/stage1_solve.py",
    "src/shuati/pipeline/stage3_taxonomy.py",
    "src/shuati/pipeline/unified_import.py",
}


# ---------------------------------------------------------------------------
# 1. 真实仓库配置、覆盖率与 AGENTS.md / 速查卡契约测试
# ---------------------------------------------------------------------------


def test_real_repo_validate_plan_passes_100_percent() -> None:
    res = rh.validate_plan(REPO_ROOT)
    assert res.ok is True, f"真实仓库 review_plan.yaml 校验失败: {res.errors}"
    assert res.errors == []
    assert res.batch_count == 34
    assert res.total_src_modules >= 48
    assert res.covered_src_modules == res.total_src_modules
    assert set(res.split_files_verified) == EXPECTED_SPLIT_MEGA_FILES

    plan = rh.load_plan_data(REPO_ROOT)
    batches = plan["batches"]
    assert [b["id"] for b in batches] == [f"B{i:02d}" for i in range(1, 35)]

    ro_probe_ids = [b["id"] for b in batches if b.get("allow_ro_db_probe")]
    assert ro_probe_ids == ["B04"]

    cross_ids = [b["id"] for b in batches if b.get("is_cross_review")]
    assert cross_ids == ["B30", "B31", "B32", "B33", "B34"]
    for b in batches:
        if b["id"] in cross_ids:
            assert "异构模型家族交叉盲审" in str(b.get("prior_review_note", ""))


def test_external_review_brief_and_agents_md_exemption_contract() -> None:
    assert not (REPO_ROOT / "CLAUDE.md").exists(), "严禁在仓库根目录创建 CLAUDE.md"

    agents_lines = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    expected_line3 = (
        "**开始工作前先读 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、"
        "[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) 与 [docs/records/INDEX.md](docs/records/INDEX.md)**"
        "（注：若当前会话仅执行 `.agents/teamwork/external_review/B*_task.md` 专项切片审查，"
        "严格按任务单白名单直奔目标源码，免读上述全局长文档）。"
    )
    assert agents_lines[2] == expected_line3

    agents_text = "\n".join(agents_lines)
    for marker in (
        "1. **权威文档（主从双层架构）**:",
        "2. **历史归档保护**:",
        "3. **确认优先**:",
        "4. **提交纪律**:",
        "5. **汇报、裁决与执行记录三件套落盘规范**:",
        "6. **批处理编排与 Windows 工程经验沉淀**:",
        "shuati-init",
        "shuati-compiler",
        "`shuati`",
    ):
        assert marker in agents_text

    brief_path = REPO_ROOT / ".agents" / "rules" / "external_review_brief.md"
    assert brief_path.is_file()
    brief_text = brief_path.read_text(encoding="utf-8")
    brief_line_count = len(brief_text.splitlines())
    assert 130 <= brief_line_count <= 170, f"速查卡行数应控制在 ~130-170 行，当前 {brief_line_count}"

    for key_term in (
        "I1",
        "I2",
        "I3",
        "I4",
        "I5",
        "I6",
        "I7",
        "I8",
        "I9",
        "SOURCE_CHANGED",
        "redirect_to_id",
        "NEEDS_REVIEW",
        "SCAFFOLD",
        "TRAP_ARITHMETIC_SLIP",
        "_walk_stmt_list",
        "transition_taxonomy",
        "eff_base_elo",
    ):
        assert key_term in brief_text, f"external_review_brief.md 缺少关键契约项: {key_term}"


# ---------------------------------------------------------------------------
# 2. SQLite DDL AST 抽取器测试（严禁 import/exec，支持虚表与多行索引）
# ---------------------------------------------------------------------------


def test_extract_ddl_for_tables_real_and_synthetic(tmp_path: Path) -> None:
    real_schema = REPO_ROOT / "src" / "shuati" / "db" / "schema.py"
    ddl = rh.extract_ddl_for_tables(real_schema, ["questions", "knowledge_nodes", "questions_fts"])

    assert "CREATE TABLE IF NOT EXISTS questions (" in ddl
    assert "CREATE INDEX IF NOT EXISTS idx_q_eff_serve ON questions(" in ddl
    assert "CREATE TABLE IF NOT EXISTS knowledge_nodes (" in ddl
    # 多行 CREATE UNIQUE INDEX ... \n ON knowledge_nodes(...) 必须完整提取
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_kn_active" in ddl
    assert "CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(" in ddl
    # 严禁因表名前缀重合误把 question_steps 或 question_concepts 带出
    assert "CREATE TABLE IF NOT EXISTS question_steps" not in ddl
    assert "CREATE TABLE IF NOT EXISTS question_concepts" not in ddl

    assert rh.extract_ddl_for_tables(real_schema, []) == ""

    with pytest.raises(ValueError, match="nonexistent_tbl"):
        rh.extract_ddl_for_tables(real_schema, ["questions", "nonexistent_tbl"])

    # 验证纯 AST 静态解析，绝不执行模块顶层副作用代码
    synthetic_schema = tmp_path / "schema.py"
    synthetic_schema.write_text(
        "raise RuntimeError('严禁 import 或 exec 执行此文件!')\n"
        "DDL_STATEMENTS: list[str] = [\n"
        '    "create table items (id INTEGER PRIMARY KEY);",\n'
        '    "Create Unique Index idx_items_id\\n ON items(id);",\n'
        "]\n",
        encoding="utf-8",
    )
    syn_ddl = rh.extract_ddl_for_tables(synthetic_schema, ["items"])
    assert "create table items" in syn_ddl
    assert "Create Unique Index idx_items_id" in syn_ddl


# ---------------------------------------------------------------------------
# 3. 跨模块与同文件切片外 AST Stub 抽取器测试
# ---------------------------------------------------------------------------


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_extract_dependency_stubs_cross_file_relative_and_intra_slice(tmp_path: Path) -> None:
    _write_text(tmp_path / "src/shuati/__init__.py", "")
    _write_text(
        tmp_path / "src/shuati/models/__init__.py",
        "from .domain import BoxModel\n",
    )
    _write_text(
        tmp_path / "src/shuati/models/domain.py",
        "MAX_SCORE: int = 100\n\n"
        "class BoxModel:\n"
        '    """核心盒子模型。\n    多行文档第二行不应输出。\n    """\n'
        "    title: str = 'ok'\n"
        "    def validate(self, strict: bool = True) -> bool:\n"
        "        return strict\n",
    )
    _write_text(
        tmp_path / "src/shuati/utils/__init__.py",
        "",
    )
    _write_text(
        tmp_path / "src/shuati/utils/math_ops.py",
        "def calc_ratio(a: int, b: int = 1) -> float:\n"
        '    """计算安全比值。"""\n'
        "    return a / b\n\n"
        "def unused_helper() -> None:\n"
        "    pass\n",
    )
    _write_text(
        tmp_path / "src/shuati/services/__init__.py",
        "",
    )
    _write_text(
        tmp_path / "src/shuati/services/peer.py",
        "async def peer_fetch(uid: str) -> dict[str, str]:\n"
        "    return {'uid': uid}\n",
    )
    # 切片目标文件：L1-L7 为头部与区间外辅助函数，L8-L15 为被审区间
    _write_text(
        tmp_path / "src/shuati/services/worker.py",
        "from shuati.models import BoxModel\n"
        "from shuati.models.domain import MAX_SCORE\n"
        "from shuati.utils import math_ops\n"
        "from .peer import peer_fetch\n"
        "def _out_of_slice_helper(raw: str) -> str:\n"
        "    return raw.strip()\n"
        "def _in_slice_helper(x: int) -> int:\n"
        "    return x + 1\n"
        "async def run_job(box: BoxModel) -> float:\n"
        "    cleaned = _out_of_slice_helper(box.title)\n"
        "    _ = await peer_fetch(cleaned)\n"
        "    val = _in_slice_helper(MAX_SCORE)\n"
        "    return math_ops.calc_ratio(val, 2)\n",
    )
    _write_text(tmp_path / "config/dummy.yaml", "key: value\n")

    # 场景 A：仅审查 worker.py 的 L7-L13 切片（不含 L5-L6 的 _out_of_slice_helper，也不含 peer.py）
    stubs_partial = rh.extract_dependency_stubs(
        tmp_path,
        [
            {"path": "src/shuati/services/worker.py", "ranges": [[7, 13]]},
            {"path": "config/dummy.yaml"},
        ],
    )
    assert "# [同文件切片外辅助定义] src/shuati/services/worker.py" in stubs_partial
    assert "def _out_of_slice_helper(raw: str) -> str: ..." in stubs_partial
    # _in_slice_helper 在 L7-L8 已被包含于本切片，严禁重复输出 Stub
    assert "def _in_slice_helper" not in stubs_partial
    # __init__.py 重导出追踪到 models/domain.py
    assert "# [跨模块依赖签名] src/shuati/models/domain.py" in stubs_partial
    assert "class BoxModel:" in stubs_partial
    assert "MAX_SCORE: int = 100" in stubs_partial
    # 模块属性调用 math_ops.calc_ratio
    assert "def calc_ratio(a: int, b: int=1) -> float:" in stubs_partial or (
        "def calc_ratio(a: int, b: int = 1) -> float:" in stubs_partial
    )
    assert "unused_helper" not in stubs_partial
    # 相对导入 .peer.peer_fetch
    assert "async def peer_fetch(uid: str) -> dict[str, str]: ..." in stubs_partial

    # 场景 B：当 peer.py 全文也加入同批次 targets 时，peer_fetch 不再冗余生成 Stub
    stubs_with_peer = rh.extract_dependency_stubs(
        tmp_path,
        [
            {"path": "src/shuati/services/worker.py"},
            {"path": "src/shuati/services/peer.py"},
        ],
    )
    assert "peer_fetch" not in stubs_with_peer
    assert "_out_of_slice_helper" not in stubs_with_peer


# ---------------------------------------------------------------------------
# 4. 隔离 tmp_path 仓库全生命周期：validate / prepare / status / record / mark-fixed
# ---------------------------------------------------------------------------


def _bootstrap_mini_repo(tmp_path: Path) -> Path:
    """构建一个最小完整可验证的隔离测试仓库。"""
    _write_text(
        tmp_path / ".agents/rules/external_review_brief.md",
        "# 测试速查卡\n- I1 严禁污染\n- I2 物理隔离\n",
    )
    _write_text(
        tmp_path / "src/shuati/db/schema.py",
        "DDL_STATEMENTS = [\n"
        '    """CREATE TABLE IF NOT EXISTS questions (question_uid TEXT PRIMARY KEY);""",\n'
        '    """CREATE INDEX IF NOT EXISTS idx_q_uid ON questions(question_uid);""",\n'
        '    """CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(question_uid);""",\n'
        "]\n",
    )
    _write_text(
        tmp_path / "src/shuati/shared.py",
        "def shared_add(x: int, y: int) -> int:\n"
        "    return x + y\n"
        "\n"
        "def shared_mul(x: int, y: int) -> int:\n"
        "    return x * y\n",
    )
    _write_text(
        tmp_path / "src/shuati/alpha.py",
        "from shuati.shared import shared_add\n\n"
        "def run_alpha(v: int) -> int:\n"
        "    return shared_add(v, 1)\n",
    )

    plan_data: dict[str, Any] = {
        "schema_version": 1,
        "batches": [
            {
                "id": "B01",
                "tier": 1,
                "title": "Alpha 与 Shared 上半切片",
                "is_cross_review": False,
                "allow_ro_db_probe": False,
                "targets": [
                    {"path": "src/shuati/db/schema.py"},
                    {"path": "src/shuati/alpha.py"},
                    {"path": "src/shuati/shared.py", "ranges": [[1, 3]]},
                ],
                "tables": ["questions"],
                "focus_points": ["检查 run_alpha 边界"],
                "status": "PENDING",
                "reviewed_by_models": [],
                "reviewed_at": None,
                "target_sha256": {},
                "notes": "",
            },
            {
                "id": "B02",
                "tier": 1,
                "title": "Shared 下半切片与只读探针",
                "is_cross_review": True,
                "prior_review_note": "Round 1 已审，本轮须换异构模型家族交叉盲审",
                "allow_ro_db_probe": True,
                "targets": [
                    {"path": "src/shuati/shared.py", "ranges": [[4, 5]]},
                ],
                "tables": ["questions_fts"],
                "focus_points": ["检查 shared_mul 与只读库探针"],
                "status": "PENDING",
                "reviewed_by_models": [],
                "reviewed_at": None,
                "target_sha256": {},
                "notes": "",
            },
        ],
    }
    _write_text(
        tmp_path / "config/review_plan.yaml",
        yaml.safe_dump(plan_data, allow_unicode=True, sort_keys=False),
    )
    return tmp_path


def test_prepare_generates_distinct_ro_rules_and_inlines_context(tmp_path: Path) -> None:
    repo = _bootstrap_mini_repo(tmp_path)
    assert rh.run_cli(["--repo-root", str(repo), "validate"]) == 0

    # 小写 b01 查找 + allow_ro_db_probe=False
    assert rh.run_cli(["--repo-root", str(repo), "prepare", "b01"]) == 0
    b01_card = (repo / ".agents/teamwork/external_review/B01_task.md").read_text(encoding="utf-8")
    assert "`src/shuati/shared.py`（仅限行号区间 `L1-L3`" in b01_card
    assert '仅允许使用内存数据库 `sqlite3.connect(":memory:")`' in b01_card
    assert "mode=ro" not in b01_card
    assert "CREATE TABLE IF NOT EXISTS questions" in b01_card
    assert "# 测试速查卡" in b01_card
    assert ".agents/teamwork/external_review/B01_findings.md" in b01_card
    assert "【异构交叉盲审警告】" not in b01_card

    # B02: allow_ro_db_probe=True + is_cross_review=True + 自定义 --out
    custom_out = repo / ".agents/teamwork/external_review/custom_B02.md"
    assert (
        rh.run_cli(["--repo-root", str(repo), "prepare", "B02", "--out", str(custom_out)]) == 0
    )
    b02_card = custom_out.read_text(encoding="utf-8")
    assert 'sqlite3.connect("file:data/shuati.db?mode=ro", uri=True)' in b02_card
    assert "【异构交叉盲审警告】" in b02_card
    assert "CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts" in b02_card

    # prepare 绝不污染 review_plan.yaml 中的 PENDING 状态
    reloaded = rh.load_plan_data(repo)
    assert all(b["status"] == "PENDING" and b["target_sha256"] == {} for b in reloaded["batches"])


def test_lifecycle_status_record_dirty_and_mark_fixed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _bootstrap_mini_repo(tmp_path)

    # 1. 初始状态检查
    st0 = rh.get_plan_status(repo)
    assert st0["next_batch_id"] == "B01"
    assert st0["pending_agy_triage"] == []
    assert st0["dirty_batches"] == []

    # 2. 外部审查落盘 B01_findings.md（尚未 record 时也应出现在 pending_agy_triage）
    _write_text(
        repo / ".agents/teamwork/external_review/B01_findings.md",
        "# B01 外部审查报告\n- Verdict: `FINDINGS_REPORTED`\n",
    )
    st_with_file = rh.get_plan_status(repo)
    assert st_with_file["batches"][0]["has_findings"] is True
    assert st_with_file["pending_agy_triage"] == ["B01"]

    # 3. 登记 B01 为 REVIEWED（首次使用 codex-cli，无重复模型告警）
    capsys.readouterr()
    rc = rh.run_cli(
        [
            "--repo-root",
            str(repo),
            "record",
            "b01",
            "--status",
            "REVIEWED",
            "--model",
            "codex-cli",
            "--note",
            "发现 1 处边界问题",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING:" not in captured.err

    # 同家族模型再次记录 B01 REVIEWED -> 触发 stderr 异构盲审警告，且列表保持顺序与去重
    rc_dup = rh.run_cli(
        [
            "--repo-root",
            str(repo),
            "record",
            "B01",
            "--status",
            "REVIEWED",
            "--model",
            "gpt-5-codex",
        ]
    )
    assert rc_dup == 0
    dup_captured = capsys.readouterr()
    assert "WARNING: 模型与历史记录重复" in dup_captured.err

    # 再次传入完全同名 codex-cli 验证去重
    rh.record_batch(repo, "B01", status="REVIEWED", model="codex-cli")
    st1 = rh.get_plan_status(repo)
    assert st1["batches"][0]["reviewed_by_models"] == ["codex-cli", "gpt-5-codex"]
    assert st1["batches"][0]["reviewed_at"].endswith("+08:00")
    assert st1["next_batch_id"] == "B02"

    # 4. B02 为 is_cross_review=True，记录 REVIEWED 时也应输出交叉复审提醒，随后标为 TRIAGED_CLEAN
    rh.run_cli(["--repo-root", str(repo), "record", "B02", "--status", "REVIEWED", "--model", "claude-code"])
    cross_captured = capsys.readouterr()
    assert "WARNING: 模型与历史记录重复" in cross_captured.err
    rh.record_batch(repo, "B02", status="TRIAGED_CLEAN")

    # 5. 修改 B01 与 B02 共享的 src/shuati/shared.py -> 两者同时变为 is_dirty=True (MODIFIED_SINCE_REVIEW)
    _write_text(
        repo / "src/shuati/shared.py",
        "def shared_add(x: int, y: int) -> int:\n"
        "    return int(x) + int(y)\n"
        "\n"
        "def shared_mul(x: int, y: int) -> int:\n"
        "    return x * y\n",
    )

    rc_json = rh.run_cli(["--repo-root", str(repo), "status", "--json"])
    assert rc_json == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["dirty_batches"] == ["B01", "B02"]
    assert status_payload["counts"]["MODIFIED_SINCE_REVIEW"] == 2
    assert status_payload["batches"][0]["is_dirty"] is True
    assert status_payload["batches"][0]["effective_status"] == "MODIFIED_SINCE_REVIEW"
    assert status_payload["next_batch_id"] == "B01"

    # 6. mark-fixed B01 -> 清除 B01 脏状态，同时精确上报连带受影响的 B02!
    rc_fix = rh.run_cli(
        ["--repo-root", str(repo), "mark-fixed", "B01", "--note", "修复类型转换"]
    )
    assert rc_fix == 0
    fix_out = capsys.readouterr().out
    assert "B02" in fix_out and "MODIFIED_SINCE_REVIEW" in fix_out

    st_after_fix = rh.get_plan_status(repo)
    assert st_after_fix["batches"][0]["status"] == "FIXED"
    assert st_after_fix["batches"][0]["is_dirty"] is False
    assert st_after_fix["batches"][1]["is_dirty"] is True
    assert st_after_fix["dirty_batches"] == ["B02"]

    # 支持逗号分隔批量 mark-fixed B01,B02
    assert rh.run_cli(["--repo-root", str(repo), "mark-fixed", "b01,b02"]) == 0
    st_all_clean = rh.get_plan_status(repo)
    assert st_all_clean["dirty_batches"] == []
    assert st_all_clean["next_batch_id"] is None

    # 重置回 PENDING 应清空 target_sha256 与 reviewed_at
    rh.record_batch(repo, "B01", status="PENDING")
    b01_reset = rh.load_plan_data(repo)["batches"][0]
    assert b01_reset["target_sha256"] == {}
    assert b01_reset["reviewed_at"] is None


def test_validation_and_cli_negative_edge_cases(tmp_path: Path) -> None:
    repo = _bootstrap_mini_repo(tmp_path)

    # 非法批次 ID 退出码非 0
    assert rh.run_cli(["--repo-root", str(repo), "prepare", "B99"]) != 0
    assert rh.run_cli(["--repo-root", str(repo), "record", "B99", "--status", "REVIEWED"]) != 0
    assert rh.run_cli(["--repo-root", str(repo), "mark-fixed", "B99"]) != 0

    # 1) 纯行号漏审空洞检测（区间均合法，但漏掉第 3 行 L3）
    gap_plan = rh.load_plan_data(repo)
    gap_plan["batches"][0]["targets"][2]["ranges"] = [[1, 2]]
    gap_plan["batches"][1]["targets"][0]["ranges"] = [[4, 5]]
    rh.save_plan_data(repo, gap_plan)
    gap_val = rh.validate_plan(repo)
    assert gap_val.ok is False
    assert any("漏审行空洞 src/shuati/shared.py: 缺失 L3" in e for e in gap_val.errors)

    # 2) 越界区间 + 反转区间 + 未知表名 + 目录逃逸 + 未覆盖新模块
    _write_text(repo / "src/shuati/untracked_mod.py", "X = 1\n")
    bad_plan = rh.load_plan_data(repo)
    bad_plan["batches"][0]["tables"].append("ghost_table")
    bad_plan["batches"][1]["targets"].append({"path": "../outside.py"})
    bad_plan["batches"][1]["targets"][0]["ranges"] = [[5, 3], [4, 99]]
    rh.save_plan_data(repo, bad_plan)

    val = rh.validate_plan(repo)
    assert val.ok is False
    joined_errors = "\n".join(val.errors)
    assert "ghost_table" in joined_errors
    assert "../outside.py" in joined_errors
    assert "越界" in joined_errors
    assert "untracked_mod.py" in joined_errors
    assert rh.run_cli(["--repo-root", str(repo), "validate"]) == 1

    # CJK 感知 Token 估算器基本性质
    assert rh.estimate_tokens("") == 0
    assert rh.estimate_tokens("中文汉字测试文本") > rh.estimate_tokens("abcdefgh")
