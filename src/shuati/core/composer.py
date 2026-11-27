"""专题小卷自适应梯度组卷引擎（DESIGN.md §5.2 & v2.6 高低端护栏）。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from shuati.config import ComposerConfig, LearnerEloConfig
from shuati.core.knowledge_graph import expand_canonical_concept_ids, expand_canonical_node_ids
from shuati.core.profiler import _parse_utc_dt, compute_learner_profile
from shuati.db.connection import PreconditionError


def _parse_mix(mix_str: str, count: int) -> tuple[int, int, int]:
    """按最大余数法将 'a:b:c' 配比缩放到总题数 count。"""
    if count <= 0:
        raise PreconditionError(f"组卷题数 count 必须为正整数，收到 {count}")
    parts = [p.strip() for p in (mix_str or "2:2:1").split(":")]
    if len(parts) != 3:
        raise PreconditionError(f"mix 格式必须为 '稳固:拔高:排雷'（如 2:2:1），收到 {mix_str!r}")
    try:
        weights = [max(0, int(x)) for x in parts]
    except ValueError as exc:
        raise PreconditionError(f"mix 权重必须为整数: {mix_str!r}") from exc
    total_w = sum(weights)
    if total_w <= 0:
        raise PreconditionError(f"mix 权重之和必须大于 0: {mix_str!r}")

    exact = [count * w / total_w for w in weights]
    floors = [int(x) for x in exact]
    rem = count - sum(floors)
    order = sorted(range(3), key=lambda idx: (-(exact[idx] - floors[idx]), idx))
    for i in range(rem):
        floors[order[i]] += 1
    return floors[0], floors[1], floors[2]


def _resolve_filter_l1(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    l1: str | None,
    l2: str | None,
    l3: str | None,
) -> tuple[str | None, list[str]]:
    """根据显式 l1 或 l2/l3 反查唯一所属 L1 模块（若跨多 L1 则返回 None 表示取 R_all）。"""
    warnings: list[str] = []
    if l1:
        return l1.strip(), warnings
    if l2:
        rows = conn.execute(
            """
            SELECT DISTINCT l1_module
            FROM knowledge_nodes
            WHERE grade_subject = ? AND l2_entity = ? AND redirect_to_id IS NULL;
            """,
            (grade_subject, l2.strip()),
        ).fetchall()
        l1s = sorted({str(r["l1_module"]) for r in rows if r["l1_module"]})
        if len(l1s) == 1:
            return l1s[0], warnings
    if l3:
        l3_clean = l3.strip()
        rows = conn.execute(
            """
            SELECT DISTINCT COALESCE(m.l1_module, kn.l1_module) AS l1_module
            FROM knowledge_nodes kn
            LEFT JOIN knowledge_nodes m ON m.node_id = kn.redirect_to_id
            WHERE kn.grade_subject = ? AND (kn.node_id = ? OR kn.l3_operator = ?);
            """,
            (grade_subject, l3_clean, l3_clean),
        ).fetchall()
        l1s = sorted({str(r["l1_module"]) for r in rows if r["l1_module"]})
        if len(l1s) == 1:
            return l1s[0], warnings
    return None, warnings


def _match_node_uids(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    l2: str | None,
    l3: str | None,
) -> set[str] | None:
    """按 l2 / l3（含并查集展开）返回匹配的 question_uid 集合（严格只读 eff_node_ids_json）。"""
    l2_clean = (l2 or "").strip()
    l3_clean = (l3 or "").strip()
    if not l2_clean and not l3_clean:
        return None

    target_node_ids: set[str] | None = None
    if l2_clean:
        rows = conn.execute(
            """
            SELECT node_id FROM knowledge_nodes
            WHERE grade_subject = ? AND l2_entity = ?;
            """,
            (grade_subject, l2_clean),
        ).fetchall()
        matched_l2: set[str] = set()
        for r in rows:
            matched_l2.update(expand_canonical_node_ids(conn, str(r["node_id"])))
        if not matched_l2:
            return set()
        target_node_ids = matched_l2

    if l3_clean:
        rows = conn.execute(
            """
            SELECT node_id FROM knowledge_nodes
            WHERE grade_subject = ? AND (node_id = ? OR l3_operator = ?);
            """,
            (grade_subject, l3_clean, l3_clean),
        ).fetchall()
        matched_l3: set[str] = set()
        for r in rows:
            matched_l3.update(expand_canonical_node_ids(conn, str(r["node_id"])))
        if target_node_ids is not None:
            target_node_ids &= matched_l3
        else:
            target_node_ids = matched_l3

    if not target_node_ids:
        return set()

    q_rows = conn.execute(
        """
        SELECT question_uid, eff_node_ids_json
        FROM questions
        WHERE grade_subject = ? AND is_retired = 0 AND eff_is_excluded = 0 AND eff_is_servable = 1;
        """,
        (grade_subject,),
    ).fetchall()
    matched_uids: set[str] = set()
    for qr in q_rows:
        try:
            nids = set(json.loads(qr["eff_node_ids_json"] or "[]"))
        except ValueError:
            nids = set()
        if nids & target_node_ids:
            matched_uids.add(str(qr["question_uid"]))

    return matched_uids


def _match_concept_uids(
    conn: sqlite3.Connection,
    *,
    motif: str | None,
    method: str | None,
    trap: str | None,
) -> set[str] | None:
    """按 motif / method / trap（含概念并查集与别名展开）返回交集 question_uid。"""
    filters = [(k, v.strip()) for k, v in (("MOTIF", motif), ("METHOD", method), ("TRAP", trap)) if v and v.strip()]
    if not filters:
        return None

    result: set[str] | None = None
    for kind, needle in filters:
        rows = conn.execute(
            """
            SELECT concept_id, name, aliases_json
            FROM concepts
            WHERE kind = ?;
            """,
            (kind,),
        ).fetchall()
        matched_cids: set[str] = set()
        for r in rows:
            cid = str(r["concept_id"])
            cname = str(r["name"])
            try:
                aliases = [str(a) for a in json.loads(r["aliases_json"] or "[]")]
            except ValueError:
                aliases = []
            if needle == cid or needle == cname or needle in aliases or cid == f"{kind}:{needle}":
                matched_cids.update(expand_canonical_concept_ids(conn, cid))
        if not matched_cids:
            return set()
        ph = ",".join("?" for _ in matched_cids)
        qrows = conn.execute(
            f"SELECT DISTINCT question_uid FROM question_concepts WHERE concept_id IN ({ph});",
            sorted(matched_cids),
        ).fetchall()
        cur_set = {str(qr["question_uid"]) for qr in qrows}
        result = cur_set if result is None else (result & cur_set)
    return result


def compose_quiz(
    conn: sqlite3.Connection,
    *,
    learner_id: str,
    grade_subject: str,
    elo_cfg: LearnerEloConfig,
    composer_cfg: ComposerConfig,
    l1: str | None = None,
    l2: str | None = None,
    l3: str | None = None,
    motif: str | None = None,
    method: str | None = None,
    trap: str | None = None,
    count: int = 5,
    mix: str = "2:2:1",
    exclude_solved_days: int | None = None,
    exclude_clone_days: int | None = None,
    exclude_recent_quiz_batches: int | None = None,
    target_elo: int | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """生成自适应梯度专题小卷并落盘 quiz_batches。"""
    if now_utc is None:
        now_dt = datetime.now(UTC)
    elif now_utc.tzinfo is None:
        now_dt = now_utc.replace(tzinfo=UTC)
    else:
        now_dt = now_utc.astimezone(UTC)
    warnings: list[str] = []

    q_solid, q_stretch, q_trap = _parse_mix(mix, count)

    solved_days = int(
        composer_cfg.exclude_solved_days if exclude_solved_days is None else exclude_solved_days
    )
    clone_days = int(
        composer_cfg.exclude_clone_days if exclude_clone_days is None else exclude_clone_days
    )
    recent_batches_n = int(
        composer_cfg.exclude_recent_quiz_batches
        if exclude_recent_quiz_batches is None
        else exclude_recent_quiz_batches
    )

    # 1. 维护降级检查（仅当按 l2/l3 过滤且词表未锁或 S3C/S3D 处于 STALE）
    gs_row = conn.execute(
        "SELECT taxonomy_status FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if (l2 or l3) and gs_row:
        stale_cnt = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM compile_state cs
            JOIN questions q ON q.question_uid = cs.question_uid
            WHERE q.grade_subject = ? AND cs.stage IN ('S3C', 'S3D') AND cs.status = 'STALE';
            """,
            (grade_subject,),
        ).fetchone()["cnt"]
        if str(gs_row["taxonomy_status"]) != "LOCKED" or int(stale_cnt) > 0:
            warnings.append("MAINTENANCE_DEGRADED: 词表或知识网处于未锁表/待重编状态，--l2/--l3 覆盖范围可能不完整")

    # 2. 计算能力画像与选取基准水位 R
    profile = compute_learner_profile(
        conn,
        learner_id=learner_id,
        grade_subject=grade_subject,
        elo_cfg=elo_cfg,
        now_utc=now_dt,
    )
    resolved_l1, l1_warns = _resolve_filter_l1(
        conn, grade_subject=grade_subject, l1=l1, l2=l2, l3=l3
    )
    warnings.extend(l1_warns)

    if target_elo is not None:
        base_r = float(target_elo)
        rating_source = "target_elo_override"
    elif resolved_l1 and not (motif or method or trap):
        mod_stat = profile["modules"].get(resolved_l1)
        base_r = float(mod_stat["rating"] if mod_stat else elo_cfg.initial)
        rating_source = f"module:{resolved_l1}"
    else:
        base_r = float(profile["overall"]["rating"])
        rating_source = "overall"

    # 3. 加载专题候选池
    where_sql = [
        "q.grade_subject = ?",
        "q.is_retired = 0",
        "q.eff_is_excluded = 0",
        "q.eff_is_servable = 1",
    ]
    params: list[Any] = [grade_subject]
    if l1:
        where_sql.append("q.eff_primary_l1_module = ?")
        params.append(l1.strip())

    node_uid_filter = _match_node_uids(conn, grade_subject=grade_subject, l2=l2, l3=l3)
    concept_uid_filter = _match_concept_uids(conn, motif=motif, method=method, trap=trap)

    rows = conn.execute(
        f"""
        SELECT q.question_uid, r.source_id, q.eff_base_elo, q.eff_tier, q.eff_tier_rank,
               q.eff_primary_l1_module, q.eff_question_text
        FROM questions q
        JOIN raw_questions r ON r.question_uid = q.question_uid
        WHERE {' AND '.join(where_sql)}
        ORDER BY q.question_uid ASC;
        """,
        params,
    ).fetchall()

    pool: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = str(row["question_uid"])
        if node_uid_filter is not None and uid not in node_uid_filter:
            continue
        if concept_uid_filter is not None and uid not in concept_uid_filter:
            continue
        pool[uid] = {
            "question_uid": uid,
            "source_id": str(row["source_id"] or ""),
            "elo": int(row["eff_base_elo"] if row["eff_base_elo"] is not None else 1500),
            "tier": str(row["eff_tier"] or ""),
            "tier_rank": int(row["eff_tier_rank"] or 3),
            "primary_l1_module": str(row["eff_primary_l1_module"] or ""),
        }

    if not pool:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段 '{grade_subject}' 在当前筛选条件下无任何可出题候选")

    # 4. 加载历史作答、在途 quiz_batches 与五边拓扑
    voided_ids = {
        int(r["voids_log_id"])
        for r in conn.execute(
            "SELECT voids_log_id FROM practice_log WHERE learner_id = ? AND outcome = 'VOIDED' AND voids_log_id IS NOT NULL;",
            (learner_id,),
        ).fetchall()
    }
    pl_rows = conn.execute(
        """
        SELECT log_id, question_uid, practiced_at, outcome, quiz_batch_id
        FROM practice_log
        WHERE learner_id = ? AND outcome != 'VOIDED'
        ORDER BY practiced_at ASC, log_id ASC;
        """,
        (learner_id,),
    ).fetchall()

    cutoff_solved_dt = now_dt - timedelta(days=solved_days)
    cutoff_clone_dt = now_dt - timedelta(days=clone_days)

    solved_recent_uids: set[str] = set()
    practiced_clone_window_uids: set[str] = set()
    latest_outcome_by_q: dict[str, str] = {}
    practiced_times_by_q: dict[str, list[datetime]] = {}
    logged_batch_pairs: set[tuple[str, str]] = set()

    # 按 UTC 时间点与 log_id 升序重放，消除 ISO-8601 与 SQLite 空格时间混合时的字典序误差
    ordered_pl_rows = sorted(
        (pr for pr in pl_rows if int(pr["log_id"]) not in voided_ids),
        key=lambda pr: (
            _parse_utc_dt(str(pr["practiced_at"]))
            if pr["practiced_at"]
            else datetime.min.replace(tzinfo=UTC),
            int(pr["log_id"]),
        ),
    )

    for pr in ordered_pl_rows:
        uid = str(pr["question_uid"])
        raw_ts = pr["practiced_at"]
        ts_dt = _parse_utc_dt(str(raw_ts)) if raw_ts else None
        out = str(pr["outcome"])
        if out != "SKIPPED":
            latest_outcome_by_q[uid] = out
        if ts_dt is not None:
            practiced_times_by_q.setdefault(uid, []).append(ts_dt)
        if pr["quiz_batch_id"]:
            logged_batch_pairs.add((str(pr["quiz_batch_id"]), uid))
        if ts_dt is not None:
            if out == "SOLVED_SOLO" and ts_dt >= cutoff_solved_dt:
                solved_recent_uids.add(uid)
            if ts_dt >= cutoff_clone_dt:
                practiced_clone_window_uids.add(uid)

    unredeemed_uids = {
        uid for uid, out in latest_outcome_by_q.items() if out in ("WRONG", "SOLVED_WITH_HINT")
    }

    # 最近 N 份非空 quiz_batches 未作答避重（按 UTC 时间点降序排列，避免 ASCII 'T' > ' ' 与时区偏移倒置）
    unlogged_batch_uids: set[str] = set()
    if recent_batches_n > 0:
        all_qb_rows = conn.execute(
            """
            SELECT quiz_batch_id, items_json, created_at
            FROM quiz_batches
            WHERE learner_id = ? AND grade_subject = ? AND items_json != '[]' AND TRIM(items_json) != '[]';
            """,
            (learner_id, grade_subject),
        ).fetchall()
        qb_rows = sorted(
            all_qb_rows,
            key=lambda qb: (
                _parse_utc_dt(str(qb["created_at"]))
                if qb["created_at"]
                else datetime.min.replace(tzinfo=UTC),
                str(qb["quiz_batch_id"]),
            ),
            reverse=True,
        )[:recent_batches_n]
        for qb in qb_rows:
            qbid = str(qb["quiz_batch_id"])
            qb_dt = _parse_utc_dt(str(qb["created_at"])) if qb["created_at"] else None
            try:
                items = json.loads(qb["items_json"] or "[]")
            except ValueError:
                items = []
            for it in items:
                quid = str(it.get("question_uid") or "") if isinstance(it, dict) else ""
                if not quid:
                    continue
                is_logged = (qbid, quid) in logged_batch_pairs or (
                    qb_dt is not None and any(t >= qb_dt for t in practiced_times_by_q.get(quid, ()))
                )
                if not is_logged:
                    unlogged_batch_uids.add(quid)

    hard_excluded_uids = solved_recent_uids | unlogged_batch_uids

    # 五边拓扑图邻接加载
    clone_neighbors: dict[str, set[str]] = {}
    scaffold_out: dict[str, list[str]] = {}  # source(子题) -> target(母题)
    isomorphic_neighbors: dict[str, list[str]] = {}
    contrastive_neighbors: dict[str, list[str]] = {}
    shared_trap_neighbors: dict[str, list[str]] = {}

    for er in conn.execute(
        "SELECT source_question_uid, target_question_uid, relation_type FROM question_relations;"
    ).fetchall():
        u = str(er["source_question_uid"])
        v = str(er["target_question_uid"])
        rt = str(er["relation_type"])
        if rt == "CLONE":
            clone_neighbors.setdefault(u, set()).add(v)
            clone_neighbors.setdefault(v, set()).add(u)
        elif rt == "SCAFFOLD":
            scaffold_out.setdefault(u, []).append(v)
        elif rt == "ISOMORPHIC":
            isomorphic_neighbors.setdefault(u, []).append(v)
            isomorphic_neighbors.setdefault(v, []).append(u)
        elif rt == "CONTRASTIVE":
            contrastive_neighbors.setdefault(u, []).append(v)
            contrastive_neighbors.setdefault(v, []).append(u)
        elif rt == "SHARED_TRAP":
            shared_trap_neighbors.setdefault(u, []).append(v)
            shared_trap_neighbors.setdefault(v, []).append(u)

    cooldown_orig_uids: set[str] = set(practiced_clone_window_uids)
    cooldown_clone_uids: set[str] = set()
    for seed_uid in practiced_clone_window_uids | unlogged_batch_uids:
        cooldown_clone_uids.update(clone_neighbors.get(seed_uid, ()))
    cooldown_all_uids = cooldown_orig_uids | cooldown_clone_uids

    # 题目 TRAP 概念映射（按单跳并查集归集主概念，排除 TRAP_ARITHMETIC_SLIP）
    pool_ph = ",".join("?" for _ in pool)
    traps_by_q: dict[str, set[str]] = {}
    for tr_row in conn.execute(
        f"""
        SELECT
            qc.question_uid,
            COALESCE(c.redirect_to_id, qc.concept_id) AS canon_id,
            COALESCE(m.name, c.name) AS canon_name
        FROM question_concepts qc
        JOIN concepts c ON c.concept_id = qc.concept_id
        LEFT JOIN concepts m ON m.concept_id = c.redirect_to_id
        WHERE qc.question_uid IN ({pool_ph}) AND qc.kind = 'TRAP';
        """,
        sorted(pool.keys()),
    ).fetchall():
        cid = str(tr_row["canon_id"])
        cname = str(tr_row["canon_name"])
        if cname == "TRAP_ARITHMETIC_SLIP" or cid == "TRAP:TRAP_ARITHMETIC_SLIP":
            continue
        traps_by_q.setdefault(str(tr_row["question_uid"]), set()).add(cid)

    weak_trap_ids = [
        str(t["concept_id"])
        for t in profile["weak_radar"].get("trap", [])
        if str(t.get("name")) != "TRAP_ARITHMETIC_SLIP"
        and str(t.get("concept_id")) != "TRAP:TRAP_ARITHMETIC_SLIP"
    ]

    # 5. 三段梯度选拔
    selected_items: list[dict[str, Any]] = []
    selected_uids: set[str] = set()
    intra_clone_blocked: set[str] = set()
    slot_counts = {"SOLID": 0, "STRETCH": 0, "TRAP": 0}

    def _can_pick(
        uid: str,
        *,
        allow_cooldown_clone: bool = False,
        allow_cooldown_orig: bool = False,
    ) -> bool:
        if uid not in pool or uid in selected_uids or uid in hard_excluded_uids:
            return False
        if uid in intra_clone_blocked:
            return False
        if not allow_cooldown_clone:
            if uid in cooldown_clone_uids:
                return False
            if not allow_cooldown_orig and uid in cooldown_orig_uids:
                return False
        return True

    def _pick(
        uid: str,
        *,
        slot: str,
        reason_public: str,
        reason_full: str,
        used_cooldown_fallback: bool = False,
    ) -> bool:
        # 防御性同卷硬互斥与防重入守卫
        if uid not in pool or uid in selected_uids or uid in hard_excluded_uids or uid in intra_clone_blocked:
            return False
        q = pool[uid]
        selected_uids.add(uid)
        intra_clone_blocked.add(uid)
        intra_clone_blocked.update(clone_neighbors.get(uid, ()))
        slot_counts[slot] += 1
        if used_cooldown_fallback:
            if uid in cooldown_clone_uids:
                warnings.append(f"候选不足：已启用 CLONE 冷却邻域题 {q['source_id']} 兜底")
            else:
                warnings.append(f"候选不足：已启用 14 天冷却窗内原题 {q['source_id']} 兜底")
        selected_items.append(
            {
                "question_uid": uid,
                "source_id": q["source_id"],
                "slot": slot,
                "base_elo": q["elo"],
                "tier": q["tier"],
                "tier_rank": q["tier_rank"],
                "reason_public": reason_public,
                "reason_full": reason_full,
            }
        )
        return True

    def _select_by_window(
        *,
        lo: float,
        hi: float,
        target_mid: float,
        limit: int,
        slot: str,
        reason_public: str,
        reason_full_tpl: str,
        prefer_uids: list[str] | None = None,
        allow_cooldown_clone: bool = False,
    ) -> int:
        if limit <= 0:
            return 0
        picked_now = 0
        # 优先链表（如错题回练 / SCAFFOLD / TRAP）
        if prefer_uids:
            for uid in prefer_uids:
                if picked_now >= limit:
                    break
                if not _can_pick(uid, allow_cooldown_clone=allow_cooldown_clone):
                    continue
                if lo <= pool[uid]["elo"] <= hi:
                    if _pick(
                        uid,
                        slot=slot,
                        reason_public=reason_public,
                        reason_full=reason_full_tpl.format(elo=pool[uid]["elo"], tier=pool[uid]["tier"]),
                        used_cooldown_fallback=allow_cooldown_clone and uid in cooldown_all_uids,
                    ):
                        picked_now += 1
        if picked_now >= limit:
            return picked_now

        cands = [
            uid
            for uid in pool
            if _can_pick(uid, allow_cooldown_clone=allow_cooldown_clone)
            and lo <= pool[uid]["elo"] <= hi
        ]
        cands.sort(key=lambda u: (abs(pool[u]["elo"] - target_mid), pool[u]["elo"], u))
        for uid in cands:
            if picked_now >= limit:
                break
            if not _can_pick(uid, allow_cooldown_clone=allow_cooldown_clone):
                continue
            if _pick(
                uid,
                slot=slot,
                reason_public=reason_public,
                reason_full=reason_full_tpl.format(elo=pool[uid]["elo"], tier=pool[uid]["tier"]),
                used_cooldown_fallback=allow_cooldown_clone and uid in cooldown_all_uids,
            ):
                picked_now += 1
        return picked_now

    # ---- A. 稳固槽 ----
    if q_solid > 0:
        # 最多 1 题错题/提示题回练（仅此单题槽显式放行 14 天冷却窗内的未转正原题）
        retry_cands = sorted(
            [u for u in unredeemed_uids if _can_pick(u, allow_cooldown_orig=True)],
            key=lambda u: (abs(pool[u]["elo"] - base_r), u),
        )
        if retry_cands:
            ru = retry_cands[0]
            if _can_pick(ru, allow_cooldown_orig=True):
                _pick(
                    ru,
                    slot="SOLID",
                    reason_public="稳固错题回练 · 巩固近期薄弱项",
                    reason_full=f"历史 WRONG/SOLVED_WITH_HINT 未转正回练（Elo={pool[ru]['elo']}，R={int(round(base_r))}）",
                )
        rem_solid = q_solid - slot_counts["SOLID"]
        solid_lo = max(1120.0, base_r - 150.0)
        solid_hi = max(1200.0, base_r + 50.0)
        _select_by_window(
            lo=solid_lo,
            hi=solid_hi,
            target_mid=base_r - 30.0,
            limit=rem_solid,
            slot="SOLID",
            reason_public="稳固梯度 · 匹配当前水位",
            reason_full_tpl=f"稳固槽命中 [{int(solid_lo)}, {int(solid_hi)}]（基准 R={int(round(base_r))}，题分 {{elo}}）",
        )

    # ---- B. 拔高槽（含低水位钳位与高水位合并爬坡） ----
    if q_stretch > 0:
        # 优先沿已选稳固题的 SCAFFOLD 正向出边（source=子题 -> target=母题）或 ISOMORPHIC 变式延伸
        edge_prefer_stretch: list[tuple[str, str]] = []
        for s_item in list(selected_items):
            suid = s_item["question_uid"]
            for tgt in scaffold_out.get(suid, ()):
                if tgt in pool and pool[tgt]["elo"] > pool[suid]["elo"]:
                    edge_prefer_stretch.append((tgt, f"沿 SCAFFOLD 边由 {s_item['source_id']} 升维母题"))
            for iso in isomorphic_neighbors.get(suid, ()):
                if iso in pool:
                    edge_prefer_stretch.append((iso, f"沿 ISOMORPHIC 边承接 {s_item['source_id']} 跨域同构变式"))

        for cand_uid, edge_note in edge_prefer_stretch:
            if slot_counts["STRETCH"] >= q_stretch:
                break
            if _can_pick(cand_uid):
                _pick(
                    cand_uid,
                    slot="STRETCH",
                    reason_public="拔高拓展 · 关联思维迁移",
                    reason_full=f"{edge_note}（Elo={pool[cand_uid]['elo']}）",
                )

        rem_stretch = q_stretch - slot_counts["STRETCH"]
        if rem_stretch > 0:
            if base_r >= 1800.0:
                # v2.6/v2.7 高水位护栏：
                # 1800 <= R < 2350 时先在 {L4-B(5), L5-1(6)} 合并池选拔不倒挂题（elo >= base_r - 50，优先 >= base_r）；
                # R >= 2350 或合并池无合适推进题时直接向 L5-2(7) / L5-3(8) 按接近 base_r + 100 爬坡；
                # 仍不足再回退当前专题未刷最高 Elo 题（自然优先剩余较高分 {L4-B, L5-1}）。
                if base_r < 2350.0:
                    high_band = [
                        u
                        for u in pool
                        if _can_pick(u)
                        and pool[u]["tier_rank"] in (5, 6)
                        and pool[u]["elo"] >= base_r - 50.0
                    ]
                    high_band.sort(
                        key=lambda u: (
                            0 if pool[u]["elo"] >= base_r else 1,
                            abs(pool[u]["elo"] - (base_r + 100.0)),
                            pool[u]["elo"],
                            u,
                        )
                    )
                    for u in high_band:
                        if slot_counts["STRETCH"] >= q_stretch:
                            break
                        if not _can_pick(u):
                            continue
                        _pick(
                            u,
                            slot="STRETCH",
                            reason_public="拔高冲刺 · 进阶巧思与压轴挑战",
                            reason_full=f"高水位合并池 {{L4-B, L5-1}} 选拔（{pool[u]['tier']}，Elo={pool[u]['elo']}）",
                        )
                if slot_counts["STRETCH"] < q_stretch:
                    climb_cands = [
                        u for u in pool if _can_pick(u) and pool[u]["tier_rank"] in (7, 8)
                    ]
                    climb_cands.sort(
                        key=lambda u: (
                            0 if pool[u]["elo"] >= base_r else 1,
                            abs(pool[u]["elo"] - (base_r + 100.0)),
                            pool[u]["elo"],
                            u,
                        )
                    )
                    for u in climb_cands:
                        if slot_counts["STRETCH"] >= q_stretch:
                            break
                        if not _can_pick(u):
                            continue
                        _pick(
                            u,
                            slot="STRETCH",
                            reason_public="拔高冲刺 · 压轴爬坡",
                            reason_full=f"高段向 {pool[u]['tier']} 爬坡（Elo={pool[u]['elo']}）",
                        )
                if slot_counts["STRETCH"] < q_stretch:
                    fallback_high = [u for u in pool if _can_pick(u)]
                    fallback_high.sort(key=lambda u: (-pool[u]["elo"], u))
                    for u in fallback_high:
                        if slot_counts["STRETCH"] >= q_stretch:
                            break
                        if not _can_pick(u):
                            continue
                        _pick(
                            u,
                            slot="STRETCH",
                            reason_public="拔高冲刺 · 当前专题高阶题兜底",
                            reason_full=f"高阶档位稀疏，回退专题未刷最高 Elo 题（{pool[u]['tier']}，Elo={pool[u]['elo']}）",
                        )
                    warnings.append("高水位目标档位（L5-1/L5-2/L5-3）候选不足，已回退抽取当前专题未刷最高 Elo 题")
            else:
                if base_r + 200.0 < 1200.0:
                    st_lo = 1120.0
                    st_hi = max(1250.0, base_r + 250.0)
                    warnings.append(f"低水位 ZPD 钳位生效：拔高窗口已下界保护至 [{int(st_lo)}, {int(st_hi)}]")
                else:
                    st_lo = base_r + 50.0
                    st_hi = base_r + 200.0
                _select_by_window(
                    lo=st_lo,
                    hi=st_hi,
                    target_mid=min(st_hi, base_r + 125.0),
                    limit=q_stretch - slot_counts["STRETCH"],
                    slot="STRETCH",
                    reason_public="拔高梯度 · 最近发展区挑战",
                    reason_full_tpl=f"ZPD 拔高槽 [{int(st_lo)}, {int(st_hi)}]（基准 R={int(round(base_r))}，题分 {{elo}}）",
                )

    # ---- C. 排雷槽 ----
    if q_trap > 0:
        # 1) 薄弱 TRAP 雷达命中（优先 SHARED_TRAP 邻接与同 TRAP 题）
        if weak_trap_ids:
            weak_set = set(weak_trap_ids)
            radar_cands = [
                u for u in pool if _can_pick(u) and (traps_by_q.get(u, set()) & weak_set)
            ]
            radar_cands.sort(
                key=lambda u: (-len(traps_by_q.get(u, set()) & weak_set), abs(pool[u]["elo"] - base_r), u)
            )
            for u in radar_cands:
                if slot_counts["TRAP"] >= q_trap:
                    break
                if not _can_pick(u):
                    continue
                matched_t = sorted(traps_by_q.get(u, set()) & weak_set)
                _pick(
                    u,
                    slot="TRAP",
                    reason_public="排雷专项 · 易错思维辨析",
                    reason_full=f"靶向薄弱陷阱 {','.join(matched_t)}（Elo={pool[u]['elo']}）",
                )

        # 2) 与前选题目相连的 CONTRASTIVE 辨析题 / SHARED_TRAP 邻接
        if slot_counts["TRAP"] < q_trap:
            for prev in list(selected_items):
                puid = prev["question_uid"]
                for cu in contrastive_neighbors.get(puid, ()):
                    if slot_counts["TRAP"] >= q_trap:
                        break
                    if _can_pick(cu):
                        _pick(
                            cu,
                            slot="TRAP",
                            reason_public="排雷专项 · 形似神异辨析",
                            reason_full=f"与 {prev['source_id']} 构成 CONTRASTIVE 形似神异辨析边（Elo={pool[cu]['elo']}）",
                        )
                if weak_trap_ids:
                    for stu in shared_trap_neighbors.get(puid, ()):
                        if slot_counts["TRAP"] >= q_trap:
                            break
                        if _can_pick(stu):
                            _pick(
                                stu,
                                slot="TRAP",
                                reason_public="排雷专项 · 同坑排雷",
                                reason_full=f"与 {prev['source_id']} 共享高危认知陷阱 SHARED_TRAP（Elo={pool[stu]['elo']}）",
                            )

        # 3) 冷启动降级：域内任意非计算笔误 TRAP 题 -> ZPD 常规兜底
        if slot_counts["TRAP"] < q_trap:
            non_slip_cands = [u for u in pool if _can_pick(u) and traps_by_q.get(u)]
            non_slip_cands.sort(key=lambda u: (abs(pool[u]["elo"] - base_r), u))
            for u in non_slip_cands:
                if slot_counts["TRAP"] >= q_trap:
                    break
                if not _can_pick(u):
                    continue
                _pick(
                    u,
                    slot="TRAP",
                    reason_public="排雷专项 · 常见隐含条件防踩坑",
                    reason_full=f"域内非计算笔误高危陷阱兜底 {sorted(traps_by_q[u])}（Elo={pool[u]['elo']}）",
                )

    # ---- D. 通用梯级窗口放宽与邻槽借位补齐 ----
    target_by_slot = {"SOLID": q_solid, "STRETCH": q_stretch, "TRAP": q_trap}
    for slot_name, target_n in target_by_slot.items():
        missing = target_n - slot_counts[slot_name]
        if missing <= 0:
            continue
        for delta in (100.0, 250.0, 2000.0):
            if slot_counts[slot_name] >= target_n:
                break
            got = _select_by_window(
                lo=max(800.0, base_r - delta),
                hi=min(2800.0, base_r + delta),
                target_mid=base_r,
                limit=target_n - slot_counts[slot_name],
                slot=slot_name,
                reason_public="智能补位 · 扩展梯度匹配",
                reason_full_tpl=f"{slot_name} 槽放宽窗口 ±{int(delta)} 借位补齐（Elo={{elo}}）",
            )
            if got > 0 and delta < 2000.0:
                warnings.append(f"{slot_name} 槽常规窗口不足，已放宽 Elo 窗口至 ±{int(delta)} 补位")
            elif got > 0:
                warnings.append(f"{slot_name} 槽邻域候选不足，已向同专题邻槽借位补齐")

    # ---- E. 最后一步（仅当仍不足 count 时允许动用 CLONE 冷却邻域，但仍死守同卷互斥） ----
    if len(selected_items) < count:
        for slot_name, target_n in target_by_slot.items():
            if slot_counts[slot_name] < target_n and len(selected_items) < count:
                _select_by_window(
                    lo=800.0,
                    hi=2800.0,
                    target_mid=base_r,
                    limit=min(target_n - slot_counts[slot_name], count - len(selected_items)),
                    slot=slot_name,
                    reason_public="候选兜底 · 专题可出题池复用",
                    reason_full_tpl="启用冷却期 CLONE 邻域末位兜底（保持同卷硬互斥，Elo={elo}）",
                    allow_cooldown_clone=True,
                )

    if len(selected_items) < count:
        warnings.append(
            f"专题可出题候选不足：目标 {count} 题，实际组出 {len(selected_items)} 题（已跳过近期已做及未登记在途卷题目）"
        )

    quiz_batch_id: str | None = None
    if selected_items:
        quiz_batch_id = f"qz_{now_dt.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
        params_snapshot = {
            "grade_subject": grade_subject,
            "l1": l1,
            "l2": l2,
            "l3": l3,
            "motif": motif,
            "method": method,
            "trap": trap,
            "count": count,
            "mix": mix,
            "base_elo_used": round(base_r, 1),
            "rating_source": rating_source,
        }
        conn.execute(
            """
            INSERT INTO quiz_batches(quiz_batch_id, learner_id, grade_subject, params_json, items_json)
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                quiz_batch_id,
                learner_id,
                grade_subject,
                json.dumps(params_snapshot, ensure_ascii=False),
                json.dumps(selected_items, ensure_ascii=False),
            ),
        )
        conn.commit()

    return {
        "quiz_batch_id": quiz_batch_id,
        "learner_id": learner_id,
        "grade_subject": grade_subject,
        "base_elo_used": round(base_r, 1),
        "rating_source": rating_source,
        "requested_count": count,
        "actual_count": len(selected_items),
        "mix_counts": slot_counts,
        "items": selected_items,
        "warnings": warnings,
    }
