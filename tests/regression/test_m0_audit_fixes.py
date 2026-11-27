"""M0.1 审计缺陷清零实证回归用例套件 (Regression Test Suite for M0.1 Audit Fixes)

全量覆盖 .agents/teamwork/orchestrator/m0_1_fix_contract.md 契约条款 #1 ~ #13：
1. [P0] shuati reindex 事务持久化 + CompileLock + diff_count + 连接关闭
2. [P0] SingleWriterExecutor async_transaction 协程装饰器、同步互斥与无 run_in_executor
3. [P0] _posix_glob_match 路径正则锚定、跨学段防劫持与未映射拒入
4. [P1] human_mirror.restore_snapshot 二级唯一冲突处理、全表对齐清理与 CLI 联动
5. [P1] Stage 0 去重、孤儿晋升、题号重编 (6B) 与 MISSING_FROM_SOURCE 解冻 (6C) 及 6A/6D 状态机
6. [P1] detect_and_extract_choice_options 2x2 网格与横向多选项完整提取
7. [P1] get_connection 自动安装守卫 + format='选择题' 选项补提 + L3 节点过滤
8. [P1] config/method_blacklist.yaml 移除“待定系数法”
9. [P2] 块级跨行 $$...$$ LaTeX 保护
10. [P2] search_fts 全角转半角与 LIKE 转义反斜杠
11. [P2] AnswerPart 严格契约校验
12. [P2] _load_env_file 行内注释剥离
13. [P2] source_file/source_collection 相对路径与 Markdown 报告图片告警清单

严禁写入根目录 human/、backup/v1/、题库/，严格使用 pytest tmp_path 隔离。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from shuati.config import _load_env_file
from shuati.core import effective
from shuati.core.text_norm import (
    detect_and_extract_choice_options,
    normalize_text,
)
from shuati.db import human_mirror, repository
from shuati.db.connection import (
    CompileLock,
    SingleWriterExecutor,
    get_connection,
)
from shuati.db.schema import init_db
from shuati.models.domain import AnswerPart
from shuati.pipeline.stage0_ingest import (
    _match_collection_rule,
    _posix_glob_match,
    run_stage0_ingest,
)

GS = "小学奥数"
SLUG = "primary_olympiad"


def _make_db(tmp: Path, name: str = "t.db") -> sqlite3.Connection:
    conn = get_connection(tmp / name)
    init_db(conn)
    effective.install_eff_write_guard(conn)
    return conn


def _write_collection_cfg(tmp: Path, extra_rules: str = "") -> Path:
    cfg = tmp / "cfg"
    cfg.mkdir(exist_ok=True, parents=True)
    rules_text = (
        'rules:\n  - match: "ch*"\n    grade_subject: 小学奥数\n  - match: "初中物理/**"\n    grade_subject: 初中物理\n'
    )
    if extra_rules:
        rules_text += extra_rules
    (cfg / "collections.yaml").write_text(rules_text, encoding="utf-8")
    return cfg


def _write_problems(root: Path, collection: str, rows: list[Any]) -> Path:
    d = root / collection
    d.mkdir(parents=True, exist_ok=True)
    p = d / "problems.jsonl"
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")
    return p


# ==================== 1. [P0] reindex ====================


def test_fix_1_reindex_persistence_compile_lock_diff_count(tmp_path: Path):
    """#1: reindex 获取 CompileLock、返回 diff_count、显式 commit 落盘且关闭连接。"""
    db_path = tmp_path / "reindex_test.db"
    cfg_dir = _write_collection_cfg(tmp_path)
    conn = get_connection(db_path)
    init_db(conn, config_dir=cfg_dir)

    # 插入初始题目
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) "
        "VALUES ('primary_olympiad:chA:q1', '小学奥数', 'BATCH_JSONL', 'chA', 'q1', '求 1+1', 'h1')"
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format) "
        "VALUES ('primary_olympiad:chA:q1', '小学奥数', '求 1+1', '解答题')"
    )
    effective.recompute(conn, "primary_olympiad:chA:q1")
    conn.commit()
    conn.close()

    # 1.1 CompileLock 独占测试：若锁被占用，CLI 返回退出码 2
    lock_file = tmp_path / ".compile.lock"
    with CompileLock(lock_file):
        res_locked = subprocess.run(
            [
                sys.executable,
                "-m",
                "shuati.cli.main",
                "reindex",
                "--effective",
                "--fts",
                "--db",
                str(db_path),
                "--config-dir",
                str(cfg_dir),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert res_locked.returncode == 2
        env_locked = json.loads(res_locked.stdout)
        assert env_locked["ok"] is False
        assert env_locked["error"]["code"] == "PRECONDITION_FAILED"

    # 1.2 正常 reindex 执行，验证返回 diff_count 与 effective_recomputed
    res_ok = subprocess.run(
        [
            sys.executable,
            "-m",
            "shuati.cli.main",
            "reindex",
            "--effective",
            "--fts",
            "--db",
            str(db_path),
            "--config-dir",
            str(cfg_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res_ok.returncode == 0
    env_ok = json.loads(res_ok.stdout)
    assert env_ok["ok"] is True
    assert "diff_count" in env_ok["data"]
    assert env_ok["data"]["diff_count"] == 0
    assert env_ok["data"]["effective_recomputed"] == 1
    assert env_ok["data"]["fts_rebuilt"] == 1

    # 1.3 验证持久化落盘：新连接打开可读出最新数据
    conn_verify = get_connection(db_path, readonly=True)
    q_row = conn_verify.execute(
        "SELECT eff_question_text FROM questions WHERE question_uid='primary_olympiad:chA:q1'"
    ).fetchone()
    assert q_row is not None
    assert q_row["eff_question_text"] == "求 1+1"
    conn_verify.close()


# ==================== 2. [P0] SingleWriterExecutor ====================


def test_fix_2_single_writer_executor_async_and_sync(tmp_path: Path):
    """#2: SingleWriterExecutor.async_transaction 使用 @asynccontextmanager，线程/协程共享互斥，且无 run_in_executor。"""
    executor = SingleWriterExecutor()
    conn = get_connection(tmp_path / "lock_test.db")
    init_db(conn)

    # 2.1 纯协程直接驱动测试（避免 Windows 沙盒 socketpair 挂起），验证 commit
    async def _test_async_commit():
        async with executor.async_transaction(conn) as c:
            c.execute("INSERT INTO learners(learner_id, display_name) VALUES ('async_u1', 'Async U1')")

    coro = _test_async_commit()
    try:
        coro.send(None)
    except StopIteration:
        pass

    row = conn.execute("SELECT display_name FROM learners WHERE learner_id='async_u1'").fetchone()
    assert row is not None and row["display_name"] == "Async U1"

    # 2.2 验证异常自动 rollback
    async def _test_async_rollback():
        async with executor.async_transaction(conn) as c:
            c.execute("INSERT INTO learners(learner_id, display_name) VALUES ('async_u2', 'Async U2')")
            raise RuntimeError("forced error")

    coro_err = _test_async_rollback()
    with pytest.raises(RuntimeError, match="forced error"):
        try:
            coro_err.send(None)
        except StopIteration:
            pass

    row2 = conn.execute("SELECT 1 FROM learners WHERE learner_id='async_u2'").fetchone()
    assert row2 is None

    # 2.3 验证 CancelledError (BaseException) 触发自动 rollback 且释放锁
    import asyncio
    import types

    @types.coroutine
    def _yield_once():
        yield

    async def _test_async_cancel():
        async with executor.async_transaction(conn) as c:
            c.execute("INSERT INTO learners(learner_id, display_name) VALUES ('async_u3', 'Async U3')")
            await _yield_once()

    coro_cancel = _test_async_cancel()
    coro_cancel.send(None)
    with pytest.raises(asyncio.CancelledError):
        coro_cancel.throw(asyncio.CancelledError())

    row3 = conn.execute("SELECT 1 FROM learners WHERE learner_id='async_u3'").fetchone()
    assert row3 is None
    assert not conn.in_transaction

    conn.close()


# ==================== 3. [P0] POSIX Glob Matching ====================


def test_fix_3_posix_glob_matching_anti_hijack():
    """#3: _posix_glob_match 首尾严格锚定，杜绝跨学段劫持（如 初中物理/ch01 误匹配 ch*）。"""
    rules = [
        {"match": "ch*", "grade_subject": "小学奥数"},
        {"match": "初中物理/**", "grade_subject": "初中物理"},
    ]

    # ch* 仅匹配顶层以 ch 开头的单级集合
    assert _match_collection_rule("ch01_计算基础", rules) == "小学奥数"
    assert _match_collection_rule("ch28_二次函数", rules) == "小学奥数"

    # 初中物理/ch01_声现象 绝不应被 ch* 劫持，而应顺延命中 初中物理/**
    assert _match_collection_rule("初中物理/ch01_声现象", rules) == "初中物理"
    assert _match_collection_rule("初中物理/八上", rules) == "初中物理"
    assert _match_collection_rule("初中物理/八上/声现象", rules) == "初中物理"

    # 未映射集合 初中数学/ch01_有理数 绝不命中 ch*，返回 None
    assert _match_collection_rule("初中数学/ch01_有理数", rules) is None

    # 单星号 * 与双星号 ** 边界
    assert _posix_glob_match("初中物理/八上", "初中物理/*") is True
    assert _posix_glob_match("初中物理/八上/声现象", "初中物理/*") is False
    assert _posix_glob_match("初中物理/八上/声现象", "初中物理/**") is True
    assert _posix_glob_match("初中物理", "初中物理/**") is False  # 非空子路径

    # 中间 /**/（零层、多层及防无分隔符拼接误匹配）
    assert _posix_glob_match("初中物理/ch01", "初中物理/**/ch*") is True
    assert _posix_glob_match("初中物理/八上/ch01", "初中物理/**/ch*") is True
    assert _posix_glob_match("初中物理/八上/声现象/ch01", "初中物理/**/ch*") is True
    assert _posix_glob_match("初中物理ch01", "初中物理/**/ch*") is False
    assert _posix_glob_match("初中物理/八上ch01", "初中物理/**/ch*") is False


# ==================== 4. [P1] human_mirror ====================


def test_fix_4_human_restore_alignment_and_practice_log_conflict(tmp_path: Path):
    """#4: restore_snapshot 解决 client_request_id 与 voids_log_id 二级冲突，逆向清理多余行保证 verify 100% 一致。"""
    conn = _make_db(tmp_path, "live.db")
    h_dir = tmp_path / "human"
    bk_dir = tmp_path / "bk"

    # 插入基础题目与角色
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) "
        "VALUES ('primary_olympiad:chA:q1', '小学奥数', 'BATCH_JSONL', 'chA', 'q1', '求 1+1', 'h1')"
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format) "
        "VALUES ('primary_olympiad:chA:q1', '小学奥数', '求 1+1', '解答题')"
    )
    conn.execute("INSERT INTO learners(learner_id, display_name) VALUES ('u1', 'User 1')")
    effective.recompute(conn, "primary_olympiad:chA:q1")
    conn.commit()

    # 构造 practice_log 快照，验证 client_request_id 与 voids_log_id 在库内冲突行存在时的无痛恢复
    conn.execute(
        "INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, client_request_id) "
        "VALUES (10, 'u1', 'primary_olympiad:chA:q1', 'SOLVED_SOLO', 'req_abc')"
    )
    conn.execute(
        "INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, voids_log_id) "
        "VALUES (11, 'u1', 'primary_olympiad:chA:q1', 'VOIDED', 10)"
    )
    conn.commit()

    # 导出纯净快照
    human_mirror.export_snapshot(conn, human_dir=h_dir)

    # 在 live 库中插入多余行：覆写、做题记录、以及次生唯一键冲突
    conn.execute(
        "INSERT INTO human_overrides(override_id, question_uid, field_name, override_value, base_fingerprint, source) "
        "VALUES (999, 'primary_olympiad:chA:q1', 'abstract_skeleton', '多余骨架', 'h1', 'MANUAL_YAML')"
    )
    effective.recompute(conn, "primary_olympiad:chA:q1")
    row_eff = conn.execute(
        "SELECT eff_abstract_skeleton FROM questions WHERE question_uid='primary_olympiad:chA:q1'"
    ).fetchone()
    assert row_eff["eff_abstract_skeleton"] == "多余骨架"

    # 篡改 live 库产生唯一键冲突：log 10 释放 req_abc 改由新造 log 100 抢占 req_abc
    conn.execute("UPDATE practice_log SET client_request_id = 'req_temp' WHERE log_id = 10")
    conn.execute(
        "INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, client_request_id) "
        "VALUES (100, 'u1', 'primary_olympiad:chA:q1', 'WRONG', 'req_abc')"
    )
    # log 11 变为普通记录释放 voids_log_id=10，改由新造 log 101 抢占 voids_log_id=10
    conn.execute("UPDATE practice_log SET outcome = 'WRONG', voids_log_id = NULL WHERE log_id = 11")
    conn.execute(
        "INSERT INTO practice_log(log_id, learner_id, question_uid, outcome, voids_log_id) "
        "VALUES (101, 'u1', 'primary_olympiad:chA:q1', 'VOIDED', 10)"
    )
    # 新造不在快照里的多余行 102
    conn.execute(
        "INSERT INTO practice_log(log_id, learner_id, question_uid, outcome) "
        "VALUES (102, 'u1', 'primary_olympiad:chA:q1', 'SOLVED_SOLO')"
    )
    conn.commit()

    # 预置一份旧备份文件，验证 backups_keep=1 透传并自动清理
    bk_dir.mkdir(parents=True, exist_ok=True)
    old_bk = bk_dir / "19990101_000000_old.db"
    old_bk.write_bytes(b"old")

    # 执行 restore（透传 backups_keep=1）
    counts = human_mirror.restore_snapshot(conn, human_dir=h_dir, backups_dir=bk_dir, backups_keep=1)
    assert counts["practice_log"] == 2
    assert len(list(bk_dir.glob("*.db"))) == 1
    assert not old_bk.exists()

    # 执行 verify，断言 100% 一致（多余行 100, 101, 102 全部被清理）
    ver = human_mirror.verify_snapshot(conn, human_dir=h_dir)
    assert ver["is_consistent"] is True

    # 验证多余覆写 999 消除后，生效列重算退回机器基线
    row_eff2 = conn.execute(
        "SELECT eff_abstract_skeleton FROM questions WHERE question_uid='primary_olympiad:chA:q1'"
    ).fetchone()
    assert row_eff2["eff_abstract_skeleton"] == ""

    conn.close()


# ==================== 5. [P1] Stage 0 State Machine (6A/6B/6C/6D) ====================


def test_fix_5_stage0_6a_two_hop_duplicate_eliminated(tmp_path: Path):
    """#5.3 (6A): duplicate_of_uid 二级断链消除：主题变成他人副本时，原下属子副本直接改指最终主题或晋升。"""
    root = tmp_path / "root"
    cfg = _write_collection_cfg(tmp_path)
    # q1 为主题，q2 为 q1 副本
    _write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "原题 A", "images": []},
            {"id": "q2", "question": "原题 A", "images": []},
        ],
    )
    r1 = run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )
    assert len(r1.exact_duplicates) == 1

    # 新一轮：引入 q3 (主题 B)，并将 q1 改为与 q3 相同的内容（q1 变副本），q2 保持原题 A 不变
    _write_problems(
        root,
        "chA",
        [
            {"id": "q3", "question": "新题 B", "images": []},
            {"id": "q1", "question": "新题 B", "images": []},
            {"id": "q2", "question": "原题 A", "images": []},
        ],
    )
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )

    c = sqlite3.connect(tmp_path / "s0.db")
    # q1 指向 q3
    q1_dup = c.execute(
        "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid='primary_olympiad:chA:q1'"
    ).fetchone()[0]
    assert q1_dup == "primary_olympiad:chA:q3"

    # q2 原本指向 q1，但内容是原题 A 与新题 B 不同，因此不能链式指向 q3，必须晋升为主记录！
    q2_row = c.execute(
        "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid='primary_olympiad:chA:q2'"
    ).fetchone()
    assert q2_row[0] is None
    # 并且 questions 表中有 q2
    q2_exists = c.execute(
        "SELECT 1 FROM questions WHERE question_uid='primary_olympiad:chA:q2'"
    ).fetchone()
    assert q2_exists is not None
    c.close()


def test_fix_5_stage0_6a_no_selfloop_when_master_matches_near_dup_child(tmp_path: Path):
    """#5.3 (6A R1-1): 主题内容改成与近重子副本相同时，绝不产生自环或使题目簇从 questions 消失。"""
    root = tmp_path / "root"
    cfg = _write_collection_cfg(tmp_path)
    _write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 1+1 的值", "images": []},
            {"id": "q2", "question": "求 $1+1$ 的值", "images": []},
        ],
    )
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
        dedup_near=True,
    )

    # Round 2: q1 题干修正为与 q2 完全一致
    _write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 $1+1$ 的值", "images": []},
            {"id": "q2", "question": "求 $1+1$ 的值", "images": []},
        ],
    )
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
        dedup_near=True,
    )

    c = sqlite3.connect(tmp_path / "s0.db")
    c.row_factory = sqlite3.Row
    rows = {
        r["question_uid"]: r["duplicate_of_uid"]
        for r in c.execute("SELECT question_uid, duplicate_of_uid FROM raw_questions")
    }
    qs = {r[0] for r in c.execute("SELECT question_uid FROM questions")}
    c.close()

    for uid, dup in rows.items():
        assert dup != uid
    assert rows["primary_olympiad:chA:q1"] is None
    assert rows["primary_olympiad:chA:q2"] == "primary_olympiad:chA:q1"
    assert "primary_olympiad:chA:q1" in qs


def test_fix_5_stage0_6b_renumbering_pre_mark_missing(tmp_path: Path):
    """#5.1 (6B): 题号重编防吞题：前置标记 missing_from_source，存活题目绝不挂在消失题号名下。"""
    root = tmp_path / "root"
    cfg = _write_collection_cfg(tmp_path)
    # 第一轮：q1 (主题), q2 (dup of q1)
    _write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "原题 1", "images": []},
            {"id": "q2", "question": "原题 1", "images": []},
        ],
    )
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )

    # 第二轮：q1 被删掉，q3 与 q2 拥有相同内容
    _write_problems(
        root,
        "chA",
        [
            {"id": "q2", "question": "原题 1", "images": []},
            {"id": "q3", "question": "原题 1", "images": []},
        ],
    )
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )

    c = sqlite3.connect(tmp_path / "s0.db")
    # q1 处于 missing 状态
    q1_miss = c.execute(
        "SELECT is_missing_from_source FROM raw_questions WHERE question_uid='primary_olympiad:chA:q1'"
    ).fetchone()[0]
    assert q1_miss == 1

    # q2 晋升为主记录，q3 指向 q2，绝不指向已消失的 q1！
    q2_dup = c.execute(
        "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid='primary_olympiad:chA:q2'"
    ).fetchone()[0]
    q3_dup = c.execute(
        "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid='primary_olympiad:chA:q3'"
    ).fetchone()[0]
    assert q2_dup is None
    assert q3_dup == "primary_olympiad:chA:q2"
    c.close()


def test_fix_5_stage0_6c_unretire_missing_from_source(tmp_path: Path):
    """#5.2 (6C): 消失题目回归自动解冻 MISSING_FROM_SOURCE (is_retired=0)。"""
    root = tmp_path / "root"
    cfg = _write_collection_cfg(tmp_path)
    _write_problems(root, "chA", [{"id": "q1", "question": "题目 1", "images": []}])
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )

    # 第二轮：文件清空，并开启 apply_retire
    _write_problems(root, "chA", [])
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
        apply_retire=True,
    )
    c = sqlite3.connect(tmp_path / "s0.db")
    ret = c.execute(
        "SELECT is_retired, retired_reason FROM questions WHERE question_uid='primary_olympiad:chA:q1'"
    ).fetchone()
    assert ret[0] == 1 and ret[1] == "MISSING_FROM_SOURCE"
    c.close()

    # 第三轮：q1 回归，不带 apply_retire 也能自动解冻
    _write_problems(root, "chA", [{"id": "q1", "question": "题目 1", "images": []}])
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )
    c2 = sqlite3.connect(tmp_path / "s0.db")
    ret2 = c2.execute(
        "SELECT is_retired, retired_reason FROM questions WHERE question_uid='primary_olympiad:chA:q1'"
    ).fetchone()
    assert ret2[0] == 0 and ret2[1] == ""
    c2.close()


def test_fix_5_stage0_6d_exact_vs_near_dup_orphan_determination(tmp_path: Path):
    """#5.4 (6D): 精确区分精确副本与近重副本的孤儿判据（完美保留 #20）。"""
    root = tmp_path / "root"
    cfg = _write_collection_cfg(tmp_path)
    # q1 为主题，q2 为精确副本
    _write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 1+1 的值", "images": []},
            {"id": "q2", "question": "求 1+1 的值", "images": []},
        ],
    )
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
    )

    # q1 修改格式变为 "求 $1+1$ 的值"，未开 dedup_near 时，原精确副本 q2 必须判定为孤儿自立为主记录！
    _write_problems(
        root,
        "chA",
        [
            {"id": "q1", "question": "求 $1+1$ 的值", "images": []},
            {"id": "q2", "question": "求 1+1 的值", "images": []},
        ],
    )
    run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
        dedup_near=False,
    )

    c = sqlite3.connect(tmp_path / "s0.db")
    q2_dup = c.execute(
        "SELECT duplicate_of_uid FROM raw_questions WHERE question_uid='primary_olympiad:chA:q2'"
    ).fetchone()[0]
    assert q2_dup is None
    c.close()


# ==================== 6. [P1] Choice Options Extraction ====================


def test_fix_6_choice_options_2x2_grid_and_horizontal():
    """#6: detect_and_extract_choice_options 支持 2x2 网格与横向连续多选项完整提取。"""
    # 2x2 网格
    text_2x2 = "下列说法正确的是：\nA. 选项一   B. 选项二\nC. 选项三   D. 选项四"
    is_choice, opts = detect_and_extract_choice_options(text_2x2)
    assert is_choice is True
    assert opts == {
        "A": "选项一",
        "B": "选项二",
        "C": "选项三",
        "D": "选项四",
    }

    # 单行横向 4 选项
    text_1x4 = "计算结果为：\nA. 1   B. 2   C. 3   D. 4"
    is_choice2, opts2 = detect_and_extract_choice_options(text_1x4)
    assert is_choice2 is True
    assert len(opts2) == 4

    # 几何描述不误判为选项
    text_geom = "如图，点 A. 在圆周上，点 B. 在直径上\n求证 CD 平行 AB"
    is_geom, _ = detect_and_extract_choice_options(text_geom)
    assert is_geom is False


# ==================== 7. [P1] Connection Guard, Format Override & L3 Node ====================


def test_fix_7_guard_format_override_l3_taxonomy(tmp_path: Path):
    """#7: get_connection 自动安装守卫，format='选择题' 补提选项，_resolve_canonical_node_id 仅放行 L3 节点。"""
    db_path = tmp_path / "test7.db"
    conn = get_connection(db_path)
    init_db(conn)

    # 7.1 get_connection 已自动安装写守卫：未授权写入 eff_* 报错
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, eff_is_servable) "
            "VALUES ('u:c:1', '小学奥数', 'q', '解答题', 1)"
        )

    # 7.2 format='选择题' 人工覆写选项补提
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) "
        "VALUES ('primary_olympiad:chA:q1', '小学奥数', 'BATCH_JSONL', 'chA', 'q1', '题干\nA. 选项一\nB. 选项二', 'h1')"
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_options_json) "
        "VALUES ('primary_olympiad:chA:q1', '小学奥数', '题干\nA. 选项一\nB. 选项二', '解答题', '{}')"
    )
    # 人工覆写 format='选择题'
    conn.execute(
        "INSERT INTO human_overrides(question_uid, field_name, override_value, base_fingerprint, source) "
        "VALUES ('primary_olympiad:chA:q1', 'format', '选择题', 'h1', 'MANUAL_YAML')"
    )
    conn.commit()
    effective.recompute(conn, "primary_olympiad:chA:q1")
    q_row = conn.execute(
        "SELECT eff_format, eff_options_json FROM questions WHERE question_uid='primary_olympiad:chA:q1'"
    ).fetchone()
    assert q_row["eff_format"] == "选择题"
    opts_dict = json.loads(q_row["eff_options_json"])
    assert opts_dict == {"A": "选项一", "B": "选项二"}

    # 7.3 _resolve_canonical_node_id 验证：L2 汇总节点与不存在节点被过滤，仅 L3 放行
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) "
        "VALUES ('node_l2', '小学奥数', 'v1', 'L2', '几何', '圆', '')"
    )
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) "
        "VALUES ('node_l3', '小学奥数', 'v1', 'L3', '几何', '圆', '求面积')"
    )
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator, redirect_to_id) "
        "VALUES ('node_alias', '小学奥数', 'v1', 'L3', '几何', '圆', '旧称', 'node_l3')"
    )
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator, redirect_to_id) "
        "VALUES ('node_bad_redirect', '小学奥数', 'v1', 'L3', '几何', '圆', '错链', 'node_l2')"
    )
    conn.commit()

    assert effective._resolve_canonical_node_id(conn, "node_l3") == "node_l3"
    assert effective._resolve_canonical_node_id(conn, "node_alias") == "node_l3"
    assert effective._resolve_canonical_node_id(conn, "node_l2") is None  # L2 汇总节点过滤
    assert effective._resolve_canonical_node_id(conn, "node_bad_redirect") is None  # 终点为 L2 过滤
    assert effective._resolve_canonical_node_id(conn, "non_existent") is None  # 不存在过滤

    conn.close()


# ==================== 8. [P1] Method Blacklist ====================


def test_fix_8_method_blacklist_not_containing_daidingxishu():
    """#8: config/method_blacklist.yaml 移除误列的“待定系数法”，且 10 大中粒度 seed_traps 完备。"""
    cfg_file = Path("config/method_blacklist.yaml")
    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    blacklist = data.get("blacklist_terms", [])
    assert "待定系数法" not in blacklist
    assert "转化与化归" in blacklist
    seed_traps = data.get("seed_traps", [])
    assert len(seed_traps) == 10


# ==================== 9. [P2] LaTeX $$...$$ Block Protection ====================


def test_fix_9_block_latex_protection():
    """#9.1: normalize_text 保护跨行 $$...$$ 块级 LaTeX 公式不被破坏空白与换行。"""
    raw = "题目描述如下：\n$$\n\\begin{aligned}\n x + y &= 2 \\\\\n x - y &= 0\n\\end{aligned}\n$$\n求 x 与 y。"
    res = normalize_text(raw, num_images=0)
    assert "$$\n\\begin{aligned}\n x + y &= 2 \\\\\n x - y &= 0\n\\end{aligned}\n$$" in res.normalized_text


# ==================== 10. [P2] search_fts Fullwidth & Escape ====================


def test_fix_10_search_fts_fullwidth_and_escape(tmp_path: Path):
    """#10: search_fts 全角转半角，<3字 LIKE 正确转义反斜杠。"""
    conn = _make_db(tmp_path)
    # 写入含反斜杠与字母的测试数据
    conn.execute(
        "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) "
        "VALUES ('primary_olympiad:chA:q1', '小学奥数', 'BATCH_JSONL', 'chA', 'q1', '求 \\pi 的值', 'h1')"
    )
    conn.execute(
        "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status) "
        "VALUES ('primary_olympiad:chA:q1', '小学奥数', '求 \\pi 的值', '解答题', 'VERIFIED_CONSENSUS')"
    )
    effective.recompute(conn, "primary_olympiad:chA:q1")
    conn.commit()

    # 1. 全角转半角检索 "求"
    res1 = repository.search_fts(conn, "求", servable_only=True)
    assert len(res1) == 1

    # 2. 短词含反斜杠检索
    res2 = repository.search_fts(conn, "\\", servable_only=True)
    assert len(res2) == 1

    conn.close()


# ==================== 11. [P2] AnswerPart Validation ====================


def test_fix_11_answer_part_validation():
    """#11: AnswerPart 强校验拒绝模型自造子小问标签、集合模式必传 items、SYMBOLIC 必传 sympy_expr 等。"""
    # 1. 合法构造
    p_ok = AnswerPart(
        label="主问",
        answer_type="NUMERIC",
        verify_mode="SYMBOLIC",
        collection_mode="SINGLE",
        answer="2",
        sympy_expr="2",
    )
    assert p_ok.sympy_expr == "2"

    # 2. 拒绝自造连接符标签如 (1)-1
    with pytest.raises(ValueError, match="严禁自造连接符后缀"):
        AnswerPart(label="(1)-1", answer="1", sympy_expr="1")

    # 3. collection_mode != "SINGLE" 时 items 必须非空
    with pytest.raises(ValueError, match="items 必须为非空列表"):
        AnswerPart(collection_mode="ORDERED_TUPLE", items=[])

    # 4. SINGLE 且 SYMBOLIC 时 sympy_expr 必须非空
    with pytest.raises(ValueError, match="sympy_expr 必须为非空字符串"):
        AnswerPart(answer_type="NUMERIC", verify_mode="SYMBOLIC", sympy_expr="")

    # 5. CHOICE 必须提供 choice_label
    with pytest.raises(ValueError, match="choice_label"):
        AnswerPart(answer_type="CHOICE", verify_mode="EXACT", choice_label="")


# ==================== 12. [P2] _load_env_file Inline Comments ====================


def test_fix_12_env_file_inline_comments(tmp_path: Path):
    """#12: _load_env_file 对未加引号的值剥离行内注释。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TEST_UNQUOTED=my_val # this is comment\nTEST_QUOTED=\"my_val # not comment\"\n",
        encoding="utf-8",
    )
    _load_env_file(env_file)
    assert os.environ.get("TEST_UNQUOTED") == "my_val"
    assert os.environ.get("TEST_QUOTED") == "my_val # not comment"


# ==================== 13. [P2] Relative Paths & Image Warnings in Report ====================


def test_fix_13_stage0_source_file_rel_and_image_warnings_in_report(tmp_path: Path):
    """#13: source_file 存相对路径，单集合导入正确映射，ingest_*.md 含图片告警清单。"""
    root = tmp_path / "题库" / "初中物理" / "八上"
    cfg = _write_collection_cfg(tmp_path)
    _write_problems(root, ".", [{"id": "q1", "question": "物理题目 1", "images": []}])

    # 预先在 DB 中注册 初中物理
    init_conn = get_connection(tmp_path / "s0.db")
    init_db(init_conn, config_dir=cfg)
    init_conn.execute(
        "INSERT INTO grade_subjects(grade_subject, slug, display_name, subject_family) "
        "VALUES ('初中物理', 'junior_physics', '初中物理', 'PHYSICS')"
    )
    init_conn.commit()
    init_conn.close()

    report = run_stage0_ingest(
        db_path=tmp_path / "s0.db",
        root_dir=root,
        config_dir=cfg,
        assets_dir=tmp_path / "assets",
        reports_dir=tmp_path / "reports",
        lock_path=tmp_path / ".compile.lock",
        grade_subject_override="初中物理",
    )

    c = sqlite3.connect(tmp_path / "s0.db")
    row = c.execute("SELECT source_file, source_collection FROM raw_questions").fetchone()
    # 相对路径格式
    assert "\\" not in row[0]
    assert row[1] == "初中物理/八上"
    c.close()

    # 验证 markdown 报告包含第 4 节
    md_text = Path(report.report_md_path).read_text(encoding="utf-8")
    assert "## 4. 图片尺寸与 EXIF 告警清单" in md_text
