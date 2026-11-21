"""M0 全量红军对抗审查回归测试套件 (Regression Test Suite for M0)

涵盖 reviewer 在四轮审查中提出并验证的全部 21 个缺陷场景，作为永久防退化资产沉淀。
严格遵循 pytest tmp_path 隔离原则，不污染根目录数据。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from shuati.core import effective
from shuati.core.text_norm import detect_and_extract_choice_options, normalize_text
from shuati.db.connection import CompileLock, get_connection
from shuati.db.human_mirror import export_snapshot, restore_snapshot
from shuati.db.schema import init_db
from shuati.pipeline.stage0_ingest import run_stage0_ingest

GS = "小学奥数"
SLUG = "primary_olympiad"
ROOT = Path(__file__).resolve().parents[2]


def make_db(tmp: Path, name: str = "t.db") -> sqlite3.Connection:
    conn = get_connection(tmp / name)
    init_db(conn)
    effective.install_eff_write_guard(conn)
    return conn


def add_question(
    conn: sqlite3.Connection,
    uid: str = f"{SLUG}:chA:q1",
    text: str = "求 1+1",
    content_hash: str = "h1",
) -> str:
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id,"
        " raw_question, content_hash) VALUES (?,?,?,?,?,?,?)",
        (uid, GS, "BATCH_JSONL", "chA", uid.split(":")[-1], text, content_hash),
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format) VALUES (?,?,?,?)",
        (uid, GS, text, "解答题"),
    )
    effective.recompute(conn, uid)
    conn.commit()
    return uid


def add_override(
    conn: sqlite3.Connection,
    uid: str,
    field: str,
    value: str,
    fp: str,
    bundle: str | None = None,
    oid: int | None = None,
    active: int = 1,
    suspended: int = 0,
) -> None:
    cols = "question_uid, field_name, override_value, source, base_fingerprint, bundle_id, is_active, is_suspended"
    vals: list[object] = [uid, field, value, "MANUAL_YAML", fp, bundle, active, suspended]
    if oid is not None:
        cols = "override_id, " + cols
        vals = [oid] + vals
    conn.execute(f"INSERT INTO human_overrides({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    conn.commit()


def write_collection_cfg(tmp: Path) -> Path:
    cfg = tmp / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "collections.yaml").write_text(
        'rules:\n  - match: "ch*"\n    grade_subject: 小学奥数\n', encoding="utf-8"
    )
    return cfg


def write_problems(root: Path, collection: str, rows: list[object]) -> Path:
    d = root / collection
    d.mkdir(parents=True, exist_ok=True)
    p = d / "problems.jsonl"
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")
    return p


def ingest(tmp: Path, root: Path, **kw: object):
    return run_stage0_ingest(
        db_path=tmp / "s0.db",
        root_dir=root,
        config_dir=write_collection_cfg(tmp),
        assets_dir=tmp / "assets",
        reports_dir=tmp / "reports",
        lock_path=tmp / ".compile.lock",
        **kw,
    )


# ==================== 21 项对抗审查回归用例 ====================


def test_repro_1_restore_learner_current_unique(tmp_path: Path):
    """#1: human restore learners 恢复前预置 is_current=0，杜绝碰撞 uq_learners_current。"""
    donor = make_db(tmp_path, "donor.db")
    donor.execute("INSERT INTO learners(learner_id, display_name, is_current) VALUES ('alice','A',0)")
    donor.execute("UPDATE learners SET is_current=0")
    donor.execute("UPDATE learners SET is_current=1 WHERE learner_id='alice'")
    donor.commit()
    export_snapshot(donor, tmp_path / "human")

    live = make_db(tmp_path, "live.db")
    restore_snapshot(live, tmp_path / "human", backups_dir=tmp_path / "bk")
    cur = [r[0] for r in live.execute("SELECT learner_id FROM learners WHERE is_current=1")]
    assert cur == ["alice"], cur


def test_repro_2_restore_override_active_unique(tmp_path: Path):
    """#2: human restore overrides 恢复前预置同冲突键活跃行 is_active=0，杜绝碰撞 uq_overrides_active。"""
    donor = make_db(tmp_path, "donor.db")
    uid = add_question(donor)
    add_override(donor, uid, "abstract_skeleton", "old", "h1", oid=1, active=1)
    export_snapshot(donor, tmp_path / "human")

    live = make_db(tmp_path, "live.db")
    add_question(live)
    add_override(live, uid, "abstract_skeleton", "old", "h1", oid=1, active=0)
    add_override(live, uid, "abstract_skeleton", "new", "h1", oid=2, active=1)

    restore_snapshot(live, tmp_path / "human", backups_dir=tmp_path / "bk")
    act = [r[0] for r in live.execute("SELECT override_id FROM human_overrides WHERE is_active=1")]
    assert act == [1], act


def test_repro_3_cli_verify_exit_swallowed(tmp_path: Path):
    """#3: CLI human verify 不一致时抛 typer.Exit(2) 不被全局 Exception 拦截，保证单信封与规范退出码 2。"""
    conn = make_db(tmp_path)
    export_snapshot(conn, tmp_path / "human")
    conn.execute("INSERT INTO learners(learner_id, display_name) VALUES ('bob','B')")
    conn.commit()
    conn.close()

    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "shuati.cli.main",
            "human",
            "verify",
            "--from",
            str(tmp_path / "human"),
            "--db",
            str(tmp_path / "t.db"),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    lines = [x for x in p.stdout.splitlines() if x.strip()]
    assert p.returncode == 2, f"exit code {p.returncode} != 2"
    assert len(lines) == 1, f"{len(lines)} JSON envelopes on stdout (expected exactly 1)"


def test_repro_4_effective_human_verified(tmp_path: Path):
    """#4: 激活且未挂起的 final_answers_json 覆写使题目转为 HUMAN_VERIFIED 且 eff_is_servable=1。"""
    conn = make_db(tmp_path)
    uid = add_question(conn)
    conn.execute("UPDATE questions SET m_verification_status='NEEDS_REVIEW' WHERE question_uid=?", (uid,))
    add_override(
        conn,
        uid,
        "final_answers_json",
        '[{"label":"主问","answer_type":"NUMERIC","verify_mode":"SYMBOLIC","answer":"2","sympy_expr":"2"}]',
        "h1",
    )
    effective.recompute(conn, uid)
    r = conn.execute(
        "SELECT eff_verification_status, eff_is_servable FROM questions WHERE question_uid=?", (uid,)
    ).fetchone()
    assert r["eff_verification_status"] == "HUMAN_VERIFIED" and r["eff_is_servable"] == 1


def test_repro_5_dedup_near_after_plain_ingest(tmp_path: Path):
    """#5: 先平铺 ingest 再运行 ingest --dedup-near 正确检出并归并近重题对。"""
    root = tmp_path / "root"
    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 $1+1$ 的值", "images": []},
            {"id": "q2", "question": "求 1+1 的值", "images": []},
        ],
    )
    r1 = ingest(tmp_path, root)
    assert len(r1.newly_ingested) == 2 and len(r1.near_duplicate_candidates) == 1
    r2 = ingest(tmp_path, root, dedup_near=True)
    assert r2.near_duplicate_candidates or r2.merged_near_duplicates


def test_repro_6_duplicate_row_content_change(tmp_path: Path):
    """#6: 精确副本题源文本修正脱离副本后，清除 duplicate_of_uid 并补建 questions 记录。"""
    root = tmp_path / "root"
    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 1+1 的值", "images": []},
            {"id": "q2", "question": "求 1+1 的值", "images": []},
        ],
    )
    r1 = ingest(tmp_path, root)
    assert len(r1.exact_duplicates) == 1

    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 1+1 的值", "images": []},
            {"id": "q2", "question": "求 2+2 的值（已修正）", "images": []},
        ],
    )
    ingest(tmp_path, root)
    c = sqlite3.connect(tmp_path / "s0.db")
    uid = f"{SLUG}:chA:q2"
    dup = c.execute("SELECT duplicate_of_uid FROM raw_questions WHERE question_uid=?", (uid,)).fetchone()[0]
    has_q = c.execute("SELECT count(*) FROM questions WHERE question_uid=?", (uid,)).fetchone()[0]
    assert dup is None and has_q == 1


def test_repro_7_normalize_caption_line_join():
    """#7: 文本规范化负向预查保护独立图注行（如 图1）不被合并进上一行。"""
    out = normalize_text("请看下图，\n图1\n求阴影部分面积", 0).normalized_text
    assert "\n图1\n" in out


def test_repro_8_normalize_latex_whitespace():
    """#8: 文本规范化保护 $...$ 内联 LaTeX 中的空白不被折叠。"""
    src = r"设 $a  \quad  b$ 求值"
    out = normalize_text(src, 0).normalized_text
    assert r"$a  \quad  b$" in out


def test_repro_9_choice_horizontal_with_letters():
    """#9: 前瞻分隔横向选择题正则完整支持选项内容本身包含字母 A-E。"""
    txt = "下列关系正确的是\nA. ∠A=∠B  B. ∠A>∠B  C. ∠A<∠B  D. 无法确定"
    ok, opts = detect_and_extract_choice_options(txt)
    assert ok and set(opts) == {"A", "B", "C", "D"}


def test_repro_10_choice_option_keyword_filter():
    """#10: 选项关键词过滤收窄至纯几何声明，不误伤包含'线段'的真实选择题。"""
    txt = "下列说法正确的是\nA. 两点之间线段最短\nB. 两点确定一个圆\nC. 三点确定一个平面\nD. 过一点只能作一条直线"
    ok, opts = detect_and_extract_choice_options(txt)
    assert ok and set(opts) == {"A", "B", "C", "D"}


def test_repro_11_ingest_malformed_row_aborts(tmp_path: Path):
    """#11: 遇到非 dict 或 question=null 畸形行时不崩溃，记录到 rejected_questions 拒入清单。"""
    root = tmp_path / "root"
    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 1+1 的值", "images": []},
            "123",
            {"id": "q3", "question": "求 3+4 的值", "images": []},
        ],
    )
    r = ingest(tmp_path, root)
    assert len(r.newly_ingested) == 2 and len(r.rejected_questions) == 1

    root2 = tmp_path / "root2"
    write_problems(
        root2,
        "chB",
        [
            {"id": "q1", "question": None, "images": []},
            {"id": "q2", "question": "求 3+4 的值", "images": []},
        ],
    )
    r2 = ingest(tmp_path, root2)
    assert len(r2.newly_ingested) == 1 and len(r2.rejected_questions) == 1


def test_repro_12_recompute_bundle_suspend(tmp_path: Path):
    """#12: 指纹失配时同 bundle_id 的激活覆写整组挂起。"""
    conn = make_db(tmp_path)
    uid = add_question(conn, content_hash="h_new")
    add_override(conn, uid, "question_text", "改写题干", "h_new", bundle="b1")
    add_override(conn, uid, "final_answers_json", "[]", "h_old", bundle="b1")
    effective.recompute_all(conn)
    rows = {r["field_name"]: r["is_suspended"] for r in conn.execute("SELECT field_name,is_suspended FROM human_overrides")}
    assert rows == {"question_text": 1, "final_answers_json": 1}


def test_repro_13_scan_id_collision(tmp_path: Path):
    """#13: scan_id 高精度微秒与随机后缀防止同秒重复运行碰撞漏检源端消失题。"""
    real = time.gmtime
    time.gmtime = lambda *a: time.struct_time((2026, 1, 1, 0, 0, 0, 3, 1, 0))
    try:
        root = tmp_path / "root"
        write_problems(
            root,
            "chA",
            [
                {"id": "q1", "question": "求 1+1 的值", "images": []},
                {"id": "q2", "question": "求 3+4 的值", "images": []},
            ],
        )
        ingest(tmp_path, root)
        write_problems(root, "chA", [{"id": "q1", "question": "求 1+1 的值", "images": []}])
        r2 = ingest(tmp_path, root)
    finally:
        time.gmtime = real
    assert r2.missing_from_source == [f"{SLUG}:chA:q2"]


def test_repro_14_eff_guard_insert_bypass(tmp_path: Path):
    """#14: effective 守卫通过 TEMP TRIGGER 拦截 INSERT / INSERT OR REPLACE 越权直写 eff_* 列。"""
    conn = make_db(tmp_path)
    uid = f"{SLUG}:chA:q9"
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id,"
        " raw_question, content_hash) VALUES (?,?,?,?,?,?,?)",
        (uid, GS, "BATCH_JSONL", "chA", "q9", "t", "h"),
    )
    blocked = False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO questions(question_uid, grade_subject, m_question_text, m_format, eff_is_servable)"
            " VALUES (?,?,?,?,1)",
            (uid, GS, "t", "解答题"),
        )
    except sqlite3.DatabaseError:
        blocked = True
    assert blocked, "guard bypassed: INSERT OR REPLACE wrote eff_is_servable outside effective.py"


def test_repro_15_compile_lock_two_paths(tmp_path: Path):
    """#15: CompileLock 重入深度按规范化路径独立维护，杜绝跨路径实例共享深度。"""
    a, b = CompileLock(tmp_path / "a.lock"), CompileLock(tmp_path / "b.lock")
    a.acquire()
    b.acquire()
    src_dir = ROOT / "src"
    b_lock = tmp_path / "b.lock"
    code = (
        f"import sys; sys.path.insert(0, r'{src_dir}')\n"
        "from shuati.db.connection import CompileLock, PreconditionError\n"
        f"try:\n    CompileLock(r'{b_lock}').acquire(); print('ACQUIRED')\n"
        "except PreconditionError:\n    print('LOCKED')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    b.release()
    a.release()
    assert out == "LOCKED"


def test_repro_16_master_change_orphans_duplicates(tmp_path: Path):
    """#16: 主题题干修正后，其原精确副本自动晋升为独立主题补建 questions 行或重定向新主题。"""
    root = tmp_path / "root"
    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 1+1 的值", "images": []},
            {"id": "q2", "question": "求 1+1 的值", "images": []},
        ],
    )
    r1 = ingest(tmp_path, root)
    assert len(r1.exact_duplicates) == 1

    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 2+2 的值（已修正）", "images": []},
            {"id": "q2", "question": "求 1+1 的值", "images": []},
        ],
    )
    ingest(tmp_path, root)
    c = sqlite3.connect(tmp_path / "s0.db")
    q2 = f"{SLUG}:chA:q2"
    row = c.execute("SELECT duplicate_of_uid, content_hash FROM raw_questions WHERE question_uid=?", (q2,)).fetchone()
    has_q = c.execute("SELECT count(*) FROM questions WHERE question_uid=?", (q2,)).fetchone()[0]
    assert has_q == 1 and row[0] is None


def test_repro_17_guard_installed_before_init_db(tmp_path: Path):
    """#17: 新库首次 ingest 在 init_db 建表后立即生效 TEMP TRIGGER 守卫。"""
    results: list[str] = []
    real = effective.recompute

    def probing_recompute(conn: sqlite3.Connection, uid: str):
        try:
            conn.execute(
                "INSERT OR REPLACE INTO questions(question_uid, grade_subject, m_question_text, m_format, eff_is_servable)"
                " VALUES (?,?,?,?,1)",
                (uid, GS, "probe", "解答题"),
            )
            results.append("WRITE_ALLOWED")
        except sqlite3.DatabaseError:
            results.append("BLOCKED")
        real(conn, uid)

    effective.recompute = probing_recompute
    try:
        root = tmp_path / "root"
        write_problems(root, "chA", [{"id": "q1", "question": "求 1+1 的值", "images": []}])
        ingest(tmp_path, root)
    finally:
        effective.recompute = real

    assert results == ["BLOCKED"]


def test_repro_18_bundle_suspend_reason_mislabel(tmp_path: Path):
    """#18: 整组挂起兄弟覆写时保留原本的挂起原因（如 TAXONOMY_UNMAPPED），不被误标为 SOURCE_CHANGED。"""
    conn = make_db(tmp_path)
    uid = add_question(conn, content_hash="h1")
    add_override(conn, uid, "primary_l1_module", "平面几何", "h1", bundle="b1", suspended=1)
    conn.execute("UPDATE human_overrides SET suspended_reason='TAXONOMY_UNMAPPED' WHERE field_name='primary_l1_module'")
    add_override(conn, uid, "abstract_skeleton", "骨架", "h1", bundle="b1")
    conn.commit()
    effective.recompute(conn, uid)
    rows = {
        r["field_name"]: (r["is_suspended"], r["suspended_reason"])
        for r in conn.execute("SELECT * FROM human_overrides")
    }
    assert rows["abstract_skeleton"] == (1, "TAXONOMY_UNMAPPED")


def test_repro_19_content_change_to_duplicate_with_human_refs(tmp_path: Path):
    """#19: 题源文本被修改为与其他题同 hash（变副本）时，若有人工引用则软退场而非物理 DELETE questions（守卫不变量 2）。"""
    root = tmp_path / "root"
    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 1+1 的值", "images": []},
            {"id": "q3", "question": "求 5+5 的值", "images": []},
        ],
    )
    ingest(tmp_path, root)
    c = sqlite3.connect(tmp_path / "s0.db")
    c.execute("PRAGMA foreign_keys=ON")
    uid = f"{SLUG}:chA:q1"
    h = c.execute("SELECT content_hash FROM raw_questions WHERE question_uid=?", (uid,)).fetchone()[0]
    c.execute(
        "INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint)"
        " VALUES (?, 'abstract_skeleton', '人工骨架', 'MANUAL_YAML', ?)",
        (uid, h),
    )
    c.commit()
    c.close()

    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 5+5 的值", "images": []},
            {"id": "q3", "question": "求 5+5 的值", "images": []},
        ],
    )
    ingest(tmp_path, root)  # 绝不抛 FK IntegrityError
    c = sqlite3.connect(tmp_path / "s0.db")
    n = c.execute("SELECT count(*) FROM human_overrides WHERE question_uid=?", (uid,)).fetchone()[0]
    assert n == 1, "human override lost"


def test_repro_20_orphan_promotion_breaks_near_dup_merge(tmp_path: Path):
    """#20: 主题仅做格式性修改（plain_fingerprint 不变）时，近重副本保持归并，不被误撤销或重复编译。"""
    root = tmp_path / "root"
    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 $1+1$ 的值", "images": []},
            {"id": "q2", "question": "求 1+1 的值", "images": []},
        ],
    )
    r1 = ingest(tmp_path, root, dedup_near=True)
    assert len(r1.merged_near_duplicates) == 1

    write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 $1 + 1$ 的值", "images": []},
            {"id": "q2", "question": "求 1+1 的值", "images": []},
        ],
    )
    ingest(tmp_path, root)
    c = sqlite3.connect(tmp_path / "s0.db")
    q2 = f"{SLUG}:chA:q2"
    dup = c.execute("SELECT duplicate_of_uid FROM raw_questions WHERE question_uid=?", (q2,)).fetchone()[0]
    has_q = c.execute("SELECT count(*) FROM questions WHERE question_uid=?", (q2,)).fetchone()[0]
    assert dup is not None and has_q == 0


def test_repro_21_soft_retired_duplicate_never_revived(tmp_path: Path):
    """#21: 软退场为 DUPLICATE 的含人工数据题，当题源再次修改为唯一时，自动复活（is_retired=0, retired_reason=''）。"""
    root = tmp_path / "root"
    base = [
        {"id": "q1", "question": "求 1+1 的值", "images": []},
        {"id": "q3", "question": "求 5+5 的值", "images": []},
    ]
    write_problems(root, "chA", base)
    ingest(tmp_path, root)
    c = sqlite3.connect(tmp_path / "s0.db")
    c.execute("PRAGMA foreign_keys=ON")
    uid = f"{SLUG}:chA:q1"
    h = c.execute("SELECT content_hash FROM raw_questions WHERE question_uid=?", (uid,)).fetchone()[0]
    c.execute(
        "INSERT INTO human_overrides(question_uid, field_name, override_value, source, base_fingerprint)"
        " VALUES (?, 'abstract_skeleton', '人工骨架', 'MANUAL_YAML', ?)",
        (uid, h),
    )
    c.commit()
    c.close()

    write_problems(root, "chA", [{"id": "q1", "question": "求 5+5 的值", "images": []}, base[1]])
    ingest(tmp_path, root)

    write_problems(root, "chA", [{"id": "q1", "question": "求 7+7 的值", "images": []}, base[1]])
    ingest(tmp_path, root)

    c = sqlite3.connect(tmp_path / "s0.db")
    dup, retired, reason = c.execute(
        "SELECT r.duplicate_of_uid, q.is_retired, q.retired_reason FROM raw_questions r JOIN questions q USING(question_uid)"
        " WHERE r.question_uid=?",
        (uid,),
    ).fetchone()
    assert dup is None and retired == 0 and reason == ""
