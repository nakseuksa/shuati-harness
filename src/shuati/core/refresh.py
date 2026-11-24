"""局部刷新引擎与状态过期传播（DESIGN.md §4.1 与 §4.8）。"""

from __future__ import annotations

import sqlite3

from shuati.core import effective

_DOWNSTREAM_STAGES = {
    "S1": ["S2", "S3C", "S3D", "S4"],
    "S2": ["S3C", "S3D", "S4"],
    "S3C": ["S3D", "S4"],
    "S3D": ["S4"],
    "S4": [],
}


def mark_stages_stale(conn: sqlite3.Connection, uid: str, stages: list[str]) -> None:
    """确保指定题目的 compile_state 存在并置为 STALE。"""
    for stage in stages:
        conn.execute(
            """
            INSERT INTO compile_state(question_uid, stage, status, updated_at)
            VALUES (?, ?, 'STALE', datetime('now'))
            ON CONFLICT(question_uid, stage) DO UPDATE SET
                status = 'STALE',
                updated_at = datetime('now');
            """,
            (uid, stage),
        )


def propagate_stage_stale(conn: sqlite3.Connection, uid: str, from_stage: str) -> None:
    """向下游阶段传播 STALE 状态。"""
    downstream = _DOWNSTREAM_STAGES.get(from_stage, [])
    if downstream:
        mark_stages_stale(conn, uid, downstream)


def handle_source_content_changed(
    conn: sqlite3.Connection,
    uid: str,
    new_content_hash: str,
) -> None:
    """源题内容变更（content_hash 变化）时的局部刷新与覆写挂起治理。

    1. 核对该题的激活覆写：仅对验真类覆写（VERIFICATION_OVERRIDE_FIELDS）校验
       base_fingerprint != new_content_hash 并自动挂起；标注类与词表类解绑 content_hash；
       若触发挂起的覆写带 bundle_id，则同一 bundle_id 整组一并挂起
       (is_suspended=1, suspended_reason='SOURCE_CHANGED')；
    2. 将 S1, S2, S3C, S3D, S4 全阶段 compile_state 置为 STALE；
    3. 调用 effective.recompute(conn, uid)，进而触发 OVERRIDE_STALE 降级并刷新 questions_fts。
    """
    # 1. 查找所有激活中的覆写
    active_overrides = conn.execute(
        """
        SELECT override_id, field_name, bundle_id, base_fingerprint, is_suspended
        FROM human_overrides
        WHERE question_uid = ? AND is_active = 1
        """,
        (uid,),
    ).fetchall()

    suspended_override_ids: set[int] = set()
    suspended_bundle_ids: set[str] = set()

    for ov in active_overrides:
        if (
            ov["field_name"] in effective.VERIFICATION_OVERRIDE_FIELDS
            and ov["base_fingerprint"] != new_content_hash
        ):
            suspended_override_ids.add(ov["override_id"])
            b_id = ov["bundle_id"]
            if b_id:
                suspended_bundle_ids.add(b_id)

    # 同 bundle_id 整组挂起
    if suspended_bundle_ids:
        for ov in active_overrides:
            if ov["bundle_id"] in suspended_bundle_ids:
                suspended_override_ids.add(ov["override_id"])

    # 执行挂起更新（不覆盖已因其他原因挂起行）
    for ov_id in suspended_override_ids:
        conn.execute(
            """
            UPDATE human_overrides
            SET is_suspended = 1, suspended_reason = 'SOURCE_CHANGED'
            WHERE override_id = ? AND is_suspended = 0
            """,
            (ov_id,),
        )

    # 2. 全阶段标记为 STALE
    mark_stages_stale(conn, uid, ["S1", "S2", "S3C", "S3D", "S4"])

    # 3. 重新计算生效层（会在内部更新 eff_* 并刷新 questions_fts）
    effective.recompute(conn, uid)
