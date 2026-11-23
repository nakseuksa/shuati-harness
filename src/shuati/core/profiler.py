"""学习者在线启发式能力画像与薄弱雷达重放引擎（DESIGN.md §5.1）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from shuati.config import LearnerEloConfig
from shuati.core.knowledge_graph import find_canonical_concept_id, find_canonical_node_id

_TIER_ORDER: tuple[tuple[int, str], ...] = (
    (1, "L1_基础直达"),
    (2, "L2_常规巩固"),
    (3, "L3_综合中档"),
    (4, "L4-A_进阶拔高(繁)"),
    (5, "L4-B_进阶拔高(巧)"),
    (6, "L5-1_压轴挑战"),
    (7, "L5-2_竞赛超难"),
    (8, "L5-3_殿堂极限"),
)


def _parse_utc_dt(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(UTC)
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _compute_actual_score(
    outcome: str,
    score_ratio: float | None,
    hint_level_used: int | None,
    elo_cfg: LearnerEloConfig,
) -> float:
    """按 DESIGN.md §5.1-1 计算实际得分 A ∈ [0, 1]。"""
    has_hint = hint_level_used is not None and int(hint_level_used) >= 1
    hint_lvl = max(1, int(hint_level_used or 1))
    hint_mult = max(0.2, float(elo_cfg.hint_score) - 0.1 * (hint_lvl - 1))

    if score_ratio is not None:
        base = max(0.0, min(1.0, float(score_ratio)))
        return base * hint_mult if has_hint else base

    if outcome == "SOLVED_SOLO":
        return 1.0
    if outcome == "SOLVED_WITH_HINT":
        return hint_mult
    return 0.0


def _elo_expected(question_elo: float, learner_rating: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((float(question_elo) - float(learner_rating)) / 400.0))


def _load_valid_logs(
    conn: sqlite3.Connection,
    *,
    learner_id: str,
    grade_subject: str,
) -> tuple[list[sqlite3.Row], int]:
    """加载有效作答日志（剔除 VOIDED 冲销对），返回 (有序有效日志, skipped_count)。"""
    voided_rows = conn.execute(
        """
        SELECT voids_log_id
        FROM practice_log
        WHERE learner_id = ? AND outcome = 'VOIDED' AND voids_log_id IS NOT NULL;
        """,
        (learner_id,),
    ).fetchall()
    voided_ids = {int(r["voids_log_id"]) for r in voided_rows}

    rows = conn.execute(
        """
        SELECT
            pl.log_id, pl.question_uid, pl.practiced_at, pl.outcome, pl.score_ratio,
            pl.stuck_step_index, pl.stuck_node_id, pl.stuck_sub_goal, pl.hint_level_used,
            pl.quiz_batch_id,
            q.eff_base_elo, q.eff_tier, q.eff_tier_rank, q.eff_primary_l1_module,
            q.eff_secondary_l1_modules_json, q.eff_node_ids_json
        FROM practice_log pl
        JOIN questions q ON q.question_uid = pl.question_uid
        WHERE pl.learner_id = ?
          AND q.grade_subject = ?
          AND pl.outcome != 'VOIDED'
        ORDER BY pl.practiced_at ASC, pl.log_id ASC;
        """,
        (learner_id, grade_subject),
    ).fetchall()

    valid: list[sqlite3.Row] = []
    skipped = 0
    for r in rows:
        if int(r["log_id"]) in voided_ids:
            continue
        if r["outcome"] == "SKIPPED":
            skipped += 1
            continue
        valid.append(r)
    return valid, skipped


def compute_learner_profile(
    conn: sqlite3.Connection,
    *,
    learner_id: str,
    grade_subject: str,
    elo_cfg: LearnerEloConfig,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """从 practice_log 实时重放计算学段总水位、分模块 Elo、8 档阶梯与薄弱雷达。"""
    now_dt = now_utc or datetime.now(UTC)
    logs, skipped_count = _load_valid_logs(
        conn, learner_id=learner_id, grade_subject=grade_subject
    )

    r_init = float(elo_cfg.initial)
    overall_rating = r_init
    overall_attempts = 0
    overall_history: list[float] = []
    overall_peak: int | None = None

    module_state: dict[str, dict[str, Any]] = {}

    def _get_mod(l1: str) -> dict[str, Any]:
        if l1 not in module_state:
            module_state[l1] = {
                "rating": r_init,
                "attempts": 0,
                "primary_attempts": 0,
                "history": [],
                "peak_elo": None,
                "solo": 0,
                "hint": 0,
                "wrong": 0,
            }
        return module_state[l1]

    tier_stats: dict[int, dict[str, Any]] = {
        rank: {
            "tier_rank": rank,
            "tier": name,
            "attempts": 0,
            "solo_count": 0,
            "hint_count": 0,
            "wrong_count": 0,
        }
        for rank, name in _TIER_ORDER
    }

    last_attempt_dt_by_q: dict[str, datetime] = {}
    latest_outcome_by_q: dict[str, str] = {}

    for r in logs:
        uid = str(r["question_uid"])
        dt = _parse_utc_dt(r["practiced_at"])
        outcome = str(r["outcome"])
        latest_outcome_by_q[uid] = outcome
        q_elo = float(r["eff_base_elo"] if r["eff_base_elo"] is not None else 1500)
        actual = _compute_actual_score(
            outcome,
            r["score_ratio"],
            r["hint_level_used"],
            elo_cfg,
        )

        # 7 天重刷半 K 防刷分
        prev_dt = last_attempt_dt_by_q.get(uid)
        repeat_mult = 1.0
        if prev_dt is not None:
            delta_days = (dt - prev_dt).total_seconds() / 86400.0
            if delta_days <= float(elo_cfg.repeat_within_days):
                repeat_mult = 0.5
        last_attempt_dt_by_q[uid] = dt

        # 1. 全学段总评流 R_all
        k_base_all = (
            elo_cfg.k_provisional
            if overall_attempts < elo_cfg.provisional_attempts
            else elo_cfg.k_stable
        )
        e_all = _elo_expected(q_elo, overall_rating)
        overall_rating += (float(k_base_all) * repeat_mult) * (actual - e_all)
        overall_attempts += 1
        overall_history.append(round(overall_rating, 1))
        if outcome == "SOLVED_SOLO":
            q_elo_int = int(round(q_elo))
            overall_peak = q_elo_int if overall_peak is None else max(overall_peak, q_elo_int)

        # 2. 主模块 L1 更新
        primary_l1 = str(r["eff_primary_l1_module"] or "").strip() or "未分类"
        mst = _get_mod(primary_l1)
        k_base_mod = (
            elo_cfg.k_provisional
            if mst["attempts"] < elo_cfg.provisional_attempts
            else elo_cfg.k_stable
        )
        e_mod = _elo_expected(q_elo, mst["rating"])
        mst["rating"] += (float(k_base_mod) * repeat_mult) * (actual - e_mod)
        mst["attempts"] += 1
        mst["primary_attempts"] += 1
        mst["history"].append(round(mst["rating"], 1))
        if outcome == "SOLVED_SOLO":
            q_elo_int = int(round(q_elo))
            mst["peak_elo"] = (
                q_elo_int if mst["peak_elo"] is None else max(mst["peak_elo"], q_elo_int)
            )
            mst["solo"] += 1
        elif outcome == "SOLVED_WITH_HINT":
            mst["hint"] += 1
        else:
            mst["wrong"] += 1

        # 3. 辅模块 L1 按自身水位算期望 + secondary_l1_weight(0.5) 权重更新
        try:
            sec_list = json.loads(r["eff_secondary_l1_modules_json"] or "[]")
        except ValueError:
            sec_list = []
        seen_sec: set[str] = {primary_l1}
        for sec in sec_list:
            sec_name = str(sec or "").strip()
            if not sec_name or sec_name in seen_sec:
                continue
            seen_sec.add(sec_name)
            sst = _get_mod(sec_name)
            k_sec_base = (
                elo_cfg.k_provisional
                if sst["attempts"] < elo_cfg.provisional_attempts
                else elo_cfg.k_stable
            )
            e_sec = _elo_expected(q_elo, sst["rating"])
            sst["rating"] += (
                float(k_sec_base) * repeat_mult * float(elo_cfg.secondary_l1_weight)
            ) * (actual - e_sec)
            sst["attempts"] += 1
            sst["history"].append(round(sst["rating"], 1))

        # 4. 8 档难度阶梯统计
        tr_rank = int(r["eff_tier_rank"] or 0)
        if tr_rank in tier_stats:
            ts = tier_stats[tr_rank]
            ts["attempts"] += 1
            if outcome == "SOLVED_SOLO":
                ts["solo_count"] += 1
            elif outcome == "SOLVED_WITH_HINT":
                ts["hint_count"] += 1
            else:
                ts["wrong_count"] += 1

    # 整理模块画像输出
    modules_out: dict[str, dict[str, Any]] = {}
    for l1_name, mst in sorted(module_state.items()):
        cur_r = int(round(mst["rating"]))
        modules_out[l1_name] = {
            "l1_module": l1_name,
            "rating": round(mst["rating"], 1),
            "rating_int": cur_r,
            "attempts": mst["attempts"],
            "primary_attempts": mst["primary_attempts"],
            "is_provisional": mst["attempts"] < 5,
            "recent_10_ratings": mst["history"][-10:],
            "r_peak": mst["peak_elo"],
            "zpd": [cur_r + 50, cur_r + 200],
            "solo_count": mst["solo"],
            "hint_count": mst["hint"],
            "wrong_count": mst["wrong"],
        }

    # 整理 8 档通过率阶梯与 L4-A / L4-B 并列倾向诊断
    tier_ladder: list[dict[str, Any]] = []
    for rank, _ in _TIER_ORDER:
        ts = tier_stats[rank]
        att = ts["attempts"]
        tier_ladder.append(
            {
                "tier_rank": rank,
                "tier": ts["tier"],
                "attempts": att,
                "solo_rate": round(ts["solo_count"] / att, 4) if att else None,
                "hint_rate": round(ts["hint_count"] / att, 4) if att else None,
                "wrong_rate": round(ts["wrong_count"] / att, 4) if att else None,
            }
        )

    ability_diagnosis: list[str] = []
    l4a = tier_ladder[3]
    l4b = tier_ladder[4]
    if l4a["attempts"] >= 3 and l4b["attempts"] >= 3:
        sr_a = float(l4a["solo_rate"] or 0.0)
        sr_b = float(l4b["solo_rate"] or 0.0)
        if sr_b - sr_a >= 0.20:
            ability_diagnosis.append("偏怕繁琐计算与分类讨论")
        elif sr_a - sr_b >= 0.20:
            ability_diagnosis.append("偏怕技巧构造与隐蔽转化")

    # 5. 薄弱考点与卡点雷达
    weak_radar = _compute_weak_radar(
        conn,
        logs=logs,
        latest_outcome_by_q=latest_outcome_by_q,
        now_dt=now_dt,
    )

    overall_int = int(round(overall_rating))
    return {
        "learner_id": learner_id,
        "grade_subject": grade_subject,
        "overall": {
            "rating": round(overall_rating, 1),
            "rating_int": overall_int,
            "valid_attempts": overall_attempts,
            "skipped_attempts": skipped_count,
            "is_provisional": overall_attempts < 5,
            "recent_10_ratings": overall_history[-10:],
            "r_peak": overall_peak,
            "zpd": [overall_int + 50, overall_int + 200],
        },
        "modules": modules_out,
        "tier_ladder": tier_ladder,
        "ability_diagnosis": ability_diagnosis,
        "weak_radar": weak_radar,
    }


def _compute_weak_radar(
    conn: sqlite3.Connection,
    *,
    logs: list[sqlite3.Row],
    latest_outcome_by_q: dict[str, str],
    now_dt: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """按 30 天 x1.5、未转正 x2.0 统计薄弱 L2/L3/MOTIF/TRAP 与 stuck_node_id 卡点。"""
    unredeemed_uids = {
        uid for uid, out in latest_outcome_by_q.items() if out != "SOLVED_SOLO"
    }
    weak_logs = [
        r for r in logs if str(r["outcome"]) in ("WRONG", "SOLVED_WITH_HINT")
    ]
    if not weak_logs:
        return {"l2": [], "l3": [], "motif": [], "trap": [], "stuck_points": []}

    node_canon_cache: dict[str, str | None] = {}
    concept_canon_cache: dict[str, str | None] = {}
    node_meta_cache: dict[str, tuple[str, str, str]] = {}
    concept_meta_cache: dict[str, tuple[str, str]] = {}

    def _c_node(nid: str) -> str | None:
        if nid not in node_canon_cache:
            node_canon_cache[nid] = find_canonical_node_id(conn, nid)
        return node_canon_cache[nid]

    def _c_concept(cid: str) -> str | None:
        if cid not in concept_canon_cache:
            concept_canon_cache[cid] = find_canonical_concept_id(conn, cid)
        return concept_canon_cache[cid]

    def _node_info(nid: str) -> tuple[str, str, str]:
        if nid not in node_meta_cache:
            row = conn.execute(
                "SELECT l1_module, l2_entity, l3_operator FROM knowledge_nodes WHERE node_id = ?;",
                (nid,),
            ).fetchone()
            node_meta_cache[nid] = (
                (str(row["l1_module"]), str(row["l2_entity"]), str(row["l3_operator"]))
                if row
                else ("", "", nid)
            )
        return node_meta_cache[nid]

    def _concept_info(cid: str) -> tuple[str, str]:
        if cid not in concept_meta_cache:
            row = conn.execute(
                "SELECT kind, name FROM concepts WHERE concept_id = ?;",
                (cid,),
            ).fetchone()
            concept_meta_cache[cid] = (
                (str(row["kind"]), str(row["name"])) if row else ("", cid)
            )
        return concept_meta_cache[cid]

    q_uids = sorted({str(r["question_uid"]) for r in weak_logs})
    ph = ",".join("?" for _ in q_uids)
    q_concepts: dict[str, list[str]] = {}
    for cr in conn.execute(
        f"SELECT question_uid, concept_id FROM question_concepts WHERE question_uid IN ({ph}) AND kind IN ('MOTIF', 'TRAP');",
        q_uids,
    ).fetchall():
        q_concepts.setdefault(str(cr["question_uid"]), []).append(str(cr["concept_id"]))

    l2_scores: dict[tuple[str, str], dict[str, Any]] = {}
    l3_scores: dict[str, dict[str, Any]] = {}
    motif_scores: dict[str, dict[str, Any]] = {}
    trap_scores: dict[str, dict[str, Any]] = {}
    stuck_scores: dict[str, dict[str, Any]] = {}

    for r in weak_logs:
        uid = str(r["question_uid"])
        dt = _parse_utc_dt(r["practiced_at"])
        w = 1.0
        if (now_dt - dt).total_seconds() <= 30.0 * 86400.0:
            w *= 1.5
        if uid in unredeemed_uids:
            w *= 2.0

        # L3 与 L2 统计
        try:
            raw_nids = json.loads(r["eff_node_ids_json"] or "[]")
        except ValueError:
            raw_nids = []
        seen_l3: set[str] = set()
        seen_l2: set[tuple[str, str]] = set()
        for raw_nid in raw_nids:
            cn = _c_node(str(raw_nid))
            if not cn or cn in seen_l3:
                continue
            seen_l3.add(cn)
            l1, l2, l3_op = _node_info(cn)
            if l3_op:
                ent = l3_scores.setdefault(
                    cn,
                    {
                        "node_id": cn,
                        "l1_module": l1,
                        "l2_entity": l2,
                        "l3_operator": l3_op,
                        "weighted_score": 0.0,
                        "count": 0,
                    },
                )
                ent["weighted_score"] += w
                ent["count"] += 1
            if l2 and (l1, l2) not in seen_l2:
                seen_l2.add((l1, l2))
                ent2 = l2_scores.setdefault(
                    (l1, l2),
                    {
                        "l1_module": l1,
                        "l2_entity": l2,
                        "weighted_score": 0.0,
                        "count": 0,
                    },
                )
                ent2["weighted_score"] += w
                ent2["count"] += 1

        # MOTIF 与 TRAP 统计
        seen_concepts: set[str] = set()
        for raw_cid in q_concepts.get(uid, []):
            ccid = _c_concept(raw_cid)
            if not ccid or ccid in seen_concepts:
                continue
            seen_concepts.add(ccid)
            kind, cname = _concept_info(ccid)
            bucket = motif_scores if kind == "MOTIF" else trap_scores if kind == "TRAP" else None
            if bucket is not None:
                cent = bucket.setdefault(
                    ccid,
                    {"concept_id": ccid, "name": cname, "weighted_score": 0.0, "count": 0},
                )
                cent["weighted_score"] += w
                cent["count"] += 1

        # 卡点快照统计（按 stuck_node_id 优先）
        raw_stuck_nid = r["stuck_node_id"]
        if raw_stuck_nid:
            csn = _c_node(str(raw_stuck_nid)) or str(raw_stuck_nid)
            l1, l2, l3_op = _node_info(csn)
            sent = stuck_scores.setdefault(
                f"node:{csn}",
                {
                    "node_id": csn,
                    "label": f"{l1}/{l2}/{l3_op}" if l2 else csn,
                    "stuck_sub_goal": str(r["stuck_sub_goal"] or ""),
                    "weighted_score": 0.0,
                    "count": 0,
                },
            )
            sent["weighted_score"] += w
            sent["count"] += 1
        elif r["stuck_sub_goal"]:
            sg = str(r["stuck_sub_goal"]).strip()
            sent = stuck_scores.setdefault(
                f"goal:{sg}",
                {
                    "node_id": None,
                    "label": sg,
                    "stuck_sub_goal": sg,
                    "weighted_score": 0.0,
                    "count": 0,
                },
            )
            sent["weighted_score"] += w
            sent["count"] += 1

    def _top5(items: dict[Any, dict[str, Any]]) -> list[dict[str, Any]]:
        ranked = sorted(items.values(), key=lambda x: (-x["weighted_score"], -x["count"]))
        for it in ranked[:5]:
            it["weighted_score"] = round(float(it["weighted_score"]), 2)
        return ranked[:5]

    return {
        "l2": _top5(l2_scores),
        "l3": _top5(l3_scores),
        "motif": _top5(motif_scores),
        "trap": _top5(trap_scores),
        "stuck_points": _top5(stuck_scores),
    }
