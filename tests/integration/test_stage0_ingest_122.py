"""测试 Stage 0 对本地真实 题库/ 122 题全量入库、体检与边界样本验证。"""

import json
from pathlib import Path

import pytest

from shuati.db.connection import get_connection
from shuati.pipeline.stage0_ingest import run_stage0_ingest


@pytest.fixture
def clean_env(tmp_path: Path):
    db_path = tmp_path / "stage0_test.db"
    assets_dir = tmp_path / "assets"
    reports_dir = tmp_path / "reports"
    config_dir = Path("config")
    root_dir = Path("题库")

    if not root_dir.exists():
        pytest.skip("题库目录不存在，跳过集成实测")

    return {
        "db_path": db_path,
        "assets_dir": assets_dir,
        "reports_dir": reports_dir,
        "config_dir": config_dir,
        "root_dir": root_dir,
    }


def test_stage0_ingest_all_122_questions(clean_env):
    db_path = clean_env["db_path"]
    assets_dir = clean_env["assets_dir"]
    reports_dir = clean_env["reports_dir"]
    config_dir = clean_env["config_dir"]
    root_dir = clean_env["root_dir"]

    # 1. 首次全量入库（不开启 dedup_near，预期 122 题完整入库）
    report = run_stage0_ingest(
        db_path=db_path,
        root_dir=root_dir,
        config_dir=config_dir,
        assets_dir=assets_dir,
        reports_dir=reports_dir,
        dedup_near=False,
    )

    # 基础指标断言
    assert report.total_scanned == 122
    assert len(report.newly_ingested) == 122
    assert len(report.rejected_questions) == 0, f"坏题/缺失图片拒入不应存在: {report.rejected_questions}"
    assert len(report.skipped_unchanged) == 0
    assert len(report.exact_duplicates) == 0

    conn = get_connection(db_path)

    # 数据库行数校验
    count_raw = conn.execute("SELECT COUNT(*) as c FROM raw_questions").fetchone()["c"]
    count_q = conn.execute("SELECT COUNT(*) as c FROM questions").fetchone()["c"]
    count_cs = conn.execute("SELECT COUNT(*) as c FROM compile_state").fetchone()["c"]

    assert count_raw == 122
    assert count_q == 122
    assert count_cs == 122 * 5  # 每题 5 个阶段 (S1, S2, S3C, S3D, S4)

    # 2. 检查 ch28_003 和 ch28_016 选择题纠偏与选项提取
    q003 = conn.execute(
        "SELECT m_format, eff_format, m_options_json, eff_options_json FROM questions WHERE question_uid LIKE '%:ch28_003'"
    ).fetchone()
    assert q003 is not None
    assert q003["m_format"] == "选择题"
    assert q003["eff_format"] == "选择题"
    opts_003 = json.loads(q003["m_options_json"])
    assert opts_003.get("A") == "1000 米"
    assert opts_003.get("D") == "1700 米"

    q016 = conn.execute(
        "SELECT m_format, eff_format, m_options_json, eff_options_json FROM questions WHERE question_uid LIKE '%:ch28_016'"
    ).fetchone()
    assert q016 is not None
    assert q016["m_format"] == "选择题"
    assert q016["eff_format"] == "选择题"
    opts_016 = json.loads(q016["m_options_json"])
    assert opts_016.get("A") == "3"
    assert opts_016.get("D") == "6"

    # 全库其余 120 题零误伤（无多余选择题）
    choice_count = conn.execute(
        "SELECT COUNT(*) as c FROM questions WHERE eff_format = '选择题'"
    ).fetchone()["c"]
    assert choice_count == 2

    # 3. 检查 ch28_031 操作构造题映射为 作图操作题
    q031 = conn.execute(
        "SELECT m_format, eff_format FROM questions WHERE question_uid LIKE '%:ch28_031'"
    ).fetchone()
    assert q031 is not None
    assert q031["m_format"] == "作图操作题"
    assert q031["eff_format"] == "作图操作题"

    # 4. 检查 ch01_012 和 ch28_083 多图题注
    q012 = conn.execute(
        "SELECT image_captions_json, cas_images_json FROM questions WHERE question_uid LIKE '%:ch01_012'"
    ).fetchone()
    assert q012 is not None
    assert json.loads(q012["image_captions_json"]) == ["A", "B"]
    assert len(json.loads(q012["cas_images_json"])) == 2

    q083 = conn.execute(
        "SELECT image_captions_json, cas_images_json FROM questions WHERE question_uid LIKE '%:ch28_083'"
    ).fetchone()
    assert q083 is not None
    assert json.loads(q083["image_captions_json"]) == ["图1", "图2"]
    assert len(json.loads(q083["cas_images_json"])) == 2

    # 5. 检查同题重扫对 ch28_015 / ch28_057 检出
    near_dups = report.near_duplicate_candidates
    assert len(near_dups) >= 1
    found_15_57 = any(
        ("ch28_057" in c["question_uid"] and "ch28_015" in c["candidate_uid"])
        or ("ch28_015" in c["question_uid"] and "ch28_057" in c["candidate_uid"])
        for c in near_dups
    )
    assert found_15_57 is True, f"未检出 ch28_015 与 ch28_057 同题重扫对: {near_dups}"

    # 6. 检查 raw_chapter 与 legacy_tags_json 与 questions 物理隔离
    # questions 表 DDL 无 raw_chapter 和 legacy_tags_json
    q_cols = [r["name"] for r in conn.execute("PRAGMA table_info(questions)").fetchall()]
    assert "raw_chapter" not in q_cols
    assert "legacy_tags_json" not in q_cols

    raw_cols = [r["name"] for r in conn.execute("PRAGMA table_info(raw_questions)").fetchall()]
    assert "raw_chapter" in raw_cols
    assert "legacy_tags_json" in raw_cols

    # 7. 再次运行：幂等性测试（全量跳过）
    report2 = run_stage0_ingest(
        db_path=db_path,
        root_dir=root_dir,
        config_dir=config_dir,
        assets_dir=assets_dir,
        reports_dir=reports_dir,
    )
    assert report2.total_scanned == 122
    assert len(report2.newly_ingested) == 0
    assert len(report2.skipped_unchanged) == 122


def test_stage0_dedup_near_flag(tmp_path: Path):
    # 测试传入 --dedup-near 时自动归并重扫对
    db_path = tmp_path / "stage0_dedup.db"
    assets_dir = tmp_path / "assets"
    reports_dir = tmp_path / "reports"
    config_dir = Path("config")
    root_dir = Path("题库")

    if not root_dir.exists():
        pytest.skip("题库目录不存在，跳过")

    report = run_stage0_ingest(
        db_path=db_path,
        root_dir=root_dir,
        config_dir=config_dir,
        assets_dir=assets_dir,
        reports_dir=reports_dir,
        dedup_near=True,
    )

    assert report.total_scanned == 122
    # ch28_057 被归并为 duplicate_of_uid，不建 questions 行
    assert len(report.merged_near_duplicates) >= 1

    conn = get_connection(db_path)
    count_raw = conn.execute("SELECT COUNT(*) as c FROM raw_questions").fetchone()["c"]
    count_q = conn.execute("SELECT COUNT(*) as c FROM questions").fetchone()["c"]

    assert count_raw == 122
    assert count_q == 121

    dup_row = conn.execute(
        "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid LIKE '%:ch28_057'"
    ).fetchone()
    assert dup_row is not None
    assert "ch28_015" in dup_row["duplicate_of_uid"]
