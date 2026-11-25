"""Stage Q 自动质量体检与巡检报告生成器（DESIGN.md §4.7）。"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from shuati.config import Settings
from shuati.core import effective
from shuati.core.knowledge_graph import (
    check_dag_and_break_cycles,
    compute_l3_distribution_entropy,
)
from shuati.core.topology import RELATION_TYPES
from shuati.db import human_mirror
from shuati.vectors.store import VectorStore, evaluate_scope_drift

_CANONICAL_TIERS: tuple[str, ...] = (
    "L1_基础直达",
    "L2_常规巩固",
    "L3_综合中档",
    "L4-A_进阶拔高(繁)",
    "L4-B_进阶拔高(巧)",
    "L5-1_压轴挑战",
    "L5-2_竞赛超难",
    "L5-3_殿堂极限",
)
_WILSON_Z_95: float = 1.959963984540054


def _quantiles_summary(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"nodes": 0, "min": 0, "p50": 0.0, "p95": 0.0, "max": 0}
    arr = np.asarray(values, dtype=np.float64)
    return {
        "nodes": len(values),
        "min": int(np.min(arr)),
        "p50": round(float(np.quantile(arr, 0.50)), 2),
        "p95": round(float(np.quantile(arr, 0.95)), 2),
        "max": int(np.max(arr)),
    }


def _wilson_95_upper_bound(k: int, n: int) -> float | None:
    """计算二项分布假共识率的 Wilson 95% 置信区间上界。"""
    if n <= 0:
        return None
    kk = max(0, min(int(k), int(n)))
    p = kk / float(n)
    z2 = _WILSON_Z_95 * _WILSON_Z_95
    denom = 1.0 + z2 / float(n)
    center = p + z2 / (2.0 * float(n))
    half_width = _WILSON_Z_95 * math.sqrt((p * (1.0 - p) / float(n)) + (z2 / (4.0 * float(n) * float(n))))
    upper = (center + half_width) / denom
    return round(min(1.0, max(0.0, upper)), 4)


def _answers_json_differ(m_json: str | None, eff_json: str | None) -> bool:
    s_m = str(m_json or "[]").strip()
    s_e = str(eff_json or "[]").strip()
    if s_m == s_e:
        return False
    try:
        return bool(json.loads(s_m) != json.loads(s_e))
    except Exception:
        return True


def _readonly_verify_effective_consistency(conn: sqlite3.Connection) -> dict[str, Any]:
    """在不污染底层数据库的条件下执行 effective.verify_effective_consistency（兼容只读连接）。"""
    try:
        conn.execute("SAVEPOINT _shuati_audit_eff;")
        try:
            res = effective.verify_effective_consistency(conn)
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT _shuati_audit_eff;")
            conn.execute("RELEASE SAVEPOINT _shuati_audit_eff;")
        return res
    except sqlite3.OperationalError:
        mem_conn = sqlite3.connect(":memory:")
        try:
            conn.backup(mem_conn)
            mem_conn.row_factory = sqlite3.Row
            effective.install_eff_write_guard(mem_conn)
            return effective.verify_effective_consistency(mem_conn)
        finally:
            mem_conn.close()


def _load_method_blacklist(config_dir: Path) -> set[str]:
    bl_path = config_dir / "method_blacklist.yaml"
    if not bl_path.is_file():
        return set()
    try:
        data = yaml.safe_load(bl_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            return {
                str(t).strip()
                for t in (data.get("blacklist_terms") or [])
                if str(t).strip()
            }
    except Exception:
        pass
    return set()


def _normalize_str_list(val: Any) -> list[str]:
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return []


def run_stage_q_audit(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str | None = None,
    human_dir: Path | str = Path("human"),
    vectors_dir: Path | str = Path("data/vectors"),
    reports_dir: Path | str = Path("data/reports"),
    config_dir: Path | str = Path("config"),
    prune_runs: bool = False,
) -> dict[str, Any]:
    """生成严格对齐 DESIGN.md §4.7 的全库质量体检报告（.md + .json，默认纯只读）。"""
    # 若指定 --prune-runs，先清理未被引用的 FAILED VLM 运行记录
    pruned_failed_runs = 0
    if prune_runs:
        cur = conn.execute(
            """
            DELETE FROM stage1_vlm_runs
            WHERE run_status = 'FAILED'
              AND run_id NOT IN (
                  SELECT origin_run_id FROM question_solutions WHERE origin_run_id IS NOT NULL
              );
            """
        )
        pruned_failed_runs = int(cur.rowcount)
        conn.commit()

    if grade_subject:
        gs_rows = conn.execute(
            "SELECT grade_subject, slug, taxonomy_status, taxonomy_version FROM grade_subjects WHERE grade_subject = ?;",
            (grade_subject,),
        ).fetchall()
    else:
        gs_rows = conn.execute(
            "SELECT grade_subject, slug, taxonomy_status, taxonomy_version FROM grade_subjects ORDER BY grade_subject;"
        ).fetchall()

    # 板块 1：编译阶段分布与可刷题覆盖率
    gs_filter = "AND q.grade_subject = ?" if grade_subject else ""
    gs_params = [grade_subject] if grade_subject else []
    stage_dist: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        f"""
        SELECT cs.stage, cs.status, COUNT(*) AS cnt
        FROM compile_state cs
        JOIN questions q ON q.question_uid = cs.question_uid
        WHERE q.is_retired = 0 {gs_filter}
        GROUP BY cs.stage, cs.status
        ORDER BY cs.stage, cs.status;
        """,
        gs_params,
    ).fetchall():
        stage_dist.setdefault(str(r["stage"]), {})[str(r["status"])] = int(r["cnt"])

    tot_row = conn.execute(
        f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN is_retired = 0 AND eff_is_excluded = 0 THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN eff_is_servable = 1 THEN 1 ELSE 0 END) AS servable,
               SUM(CASE WHEN eff_verification_status = 'NEEDS_REVIEW' THEN 1 ELSE 0 END) AS needs_review
        FROM questions q
        WHERE 1=1 {gs_filter};
        """,
        gs_params,
    ).fetchone()
    total_q = int(tot_row["total"] or 0)
    active_q = int(tot_row["active"] or 0)
    servable_q = int(tot_row["servable"] or 0)
    needs_review_q = int(tot_row["needs_review"] or 0)

    # 板块 2：SymPy 与双盲验真健康度 + 假共识抽检与 Wilson 95% 置信上界
    verif_dist = {
        str(r["eff_verification_status"]): int(r["cnt"])
        for r in conn.execute(
            f"SELECT eff_verification_status, COUNT(*) AS cnt FROM questions q WHERE is_retired = 0 {gs_filter} GROUP BY eff_verification_status;",
            gs_params,
        ).fetchall()
    }
    sympy_dist = {
        str(r["sympy_status"]): int(r["cnt"])
        for r in conn.execute(
            f"""
            SELECT r.sympy_status, COUNT(*) AS cnt
            FROM stage1_vlm_runs r
            JOIN questions q ON q.question_uid = r.question_uid
            WHERE r.run_status = 'OK' {gs_filter}
            GROUP BY r.sympy_status;
            """,
            gs_params,
        ).fetchall()
    }
    failed_runs_unreferenced = int(
        conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM stage1_vlm_runs
            WHERE run_status = 'FAILED'
              AND run_id NOT IN (SELECT origin_run_id FROM question_solutions WHERE origin_run_id IS NOT NULL);
            """
        ).fetchone()["cnt"]
    )

    consensus_rows = conn.execute(
        f"""
        SELECT q.question_uid,
               q.has_images,
               EXISTS(
                   SELECT 1 FROM stage1_vlm_runs r
                   WHERE r.question_uid = q.question_uid
                     AND r.role = 'SECONDARY'
                     AND r.run_status = 'OK'
               ) AS has_secondary_ok
        FROM questions q
        WHERE q.is_retired = 0
          AND q.eff_is_excluded = 0
          AND q.eff_verification_status = 'VERIFIED_CONSENSUS'
          {gs_filter};
        """,
        gs_params,
    ).fetchall()
    total_consensus = len(consensus_rows)
    single_run_with_images = sum(
        1 for r in consensus_rows if not bool(r["has_secondary_ok"]) and int(r["has_images"] or 0) == 1
    )
    single_run_no_images = sum(
        1 for r in consensus_rows if not bool(r["has_secondary_ok"]) and int(r["has_images"] or 0) == 0
    )
    dual_run = sum(1 for r in consensus_rows if bool(r["has_secondary_ok"]))
    if single_run_with_images > 0:
        recommended_sample_size = max(5, math.ceil(0.10 * single_run_with_images))
    elif total_consensus > 0:
        recommended_sample_size = math.ceil(
            float(settings.pipeline.consensus_audit_ratio) * total_consensus
        )
    else:
        recommended_sample_size = 0

    audited_rows = conn.execute(
        f"""
        SELECT q.question_uid,
               q.eff_is_excluded,
               q.m_final_answers_json,
               q.eff_final_answers_json
        FROM questions q
        WHERE q.is_retired = 0
          AND q.m_verification_status = 'VERIFIED_CONSENSUS'
          AND (q.eff_verification_status = 'HUMAN_VERIFIED' OR q.eff_is_excluded = 1)
          {gs_filter};
        """,
        gs_params,
    ).fetchall()
    audited_n = len(audited_rows)
    false_consensus_k = sum(
        1
        for r in audited_rows
        if int(r["eff_is_excluded"] or 0) == 1
        or _answers_json_differ(r["m_final_answers_json"], r["eff_final_answers_json"])
    )
    false_consensus_rate = round(false_consensus_k / float(audited_n), 4) if audited_n > 0 else 0.0
    wilson_95_upper = _wilson_95_upper_bound(false_consensus_k, audited_n)
    consensus_audit = {
        "total_consensus": total_consensus,
        "single_run_with_images": single_run_with_images,
        "single_run_no_images": single_run_no_images,
        "dual_run": dual_run,
        "recommended_sample_size": recommended_sample_size,
        "audited_n": audited_n,
        "false_consensus_k": false_consensus_k,
        "false_consensus_rate": false_consensus_rate,
        "wilson_95_upper": wilson_95_upper,
    }

    # 板块 2b：可服务题 8 档难度直方图与 [C, S, T, L] 边缘频次分布
    tier_histogram: dict[str, int] = {t: 0 for t in _CANONICAL_TIERS}
    c_dist: dict[str, int] = {"1": 0, "2": 0, "3": 0, "4": 0}
    s_dist: dict[str, int] = {"1": 0, "2": 0, "3": 0}
    t_dist: dict[str, int] = {"1": 0, "2": 0, "3": 0}
    l_dist: dict[str, int] = {"1": 0, "2": 0, "3": 0}
    diff_rows = conn.execute(
        f"""
        SELECT eff_tier, eff_c_score, eff_s_score, eff_t_score, eff_l_score
        FROM questions q
        WHERE q.is_retired = 0 AND q.eff_is_excluded = 0 AND q.eff_is_servable = 1 {gs_filter};
        """,
        gs_params,
    ).fetchall()
    for r in diff_rows:
        if r["eff_tier"]:
            tk = str(r["eff_tier"])
            tier_histogram[tk] = tier_histogram.get(tk, 0) + 1
        if r["eff_c_score"] is not None:
            ck = str(int(r["eff_c_score"]))
            c_dist[ck] = c_dist.get(ck, 0) + 1
        if r["eff_s_score"] is not None:
            sk = str(int(r["eff_s_score"]))
            s_dist[sk] = s_dist.get(sk, 0) + 1
        if r["eff_t_score"] is not None:
            ttk = str(int(r["eff_t_score"]))
            t_dist[ttk] = t_dist.get(ttk, 0) + 1
        if r["eff_l_score"] is not None:
            lk = str(int(r["eff_l_score"]))
            l_dist[lk] = l_dist.get(lk, 0) + 1

    difficulty_distribution = {
        "servable_count": len(diff_rows),
        "tier_histogram": tier_histogram,
        "cstl_marginals": {
            "C": c_dist,
            "S": s_dist,
            "T": t_dist,
            "L": l_dist,
        },
        "eff_c_score": c_dist,
        "eff_s_score": s_dist,
        "eff_t_score": t_dist,
        "eff_l_score": l_dist,
    }

    # 板块 3：词表与 L3 知识网健康度（熵 + 并查集折叠率 + DAG 强无环检验）
    taxonomy_health: dict[str, Any] = {}
    for gsr in gs_rows:
        gs = str(gsr["grade_subject"])
        t_ver = str(gsr["taxonomy_version"] or "")
        node_counts = {
            str(r["node_level"]): {
                "active": int(r["active_cnt"]),
                "redirected": int(r["redir_cnt"]),
            }
            for r in conn.execute(
                """
                SELECT node_level,
                       SUM(CASE WHEN redirect_to_id IS NULL THEN 1 ELSE 0 END) AS active_cnt,
                       SUM(CASE WHEN redirect_to_id IS NOT NULL THEN 1 ELSE 0 END) AS redir_cnt
                FROM knowledge_nodes
                WHERE grade_subject = ? AND taxonomy_version = ?
                GROUP BY node_level;
                """,
                (gs, t_ver),
            ).fetchall()
        }
        entropy_info = compute_l3_distribution_entropy(conn, gs, t_ver) if t_ver else {}
        prereq_rows = conn.execute(
            """
            SELECT source_node_id, target_node_id, weight
            FROM knowledge_edges
            WHERE grade_subject = ? AND edge_type = 'PREREQUISITE';
            """,
            (gs,),
        ).fetchall()
        _, broken = check_dag_and_break_cycles([dict(r) for r in prereq_rows])
        pending_cands = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM taxonomy_candidates WHERE grade_subject = ? AND status = 'PENDING';",
                (gs,),
            ).fetchone()["c"]
        )
        taxonomy_health[gs] = {
            "taxonomy_status": str(gsr["taxonomy_status"]),
            "taxonomy_version": t_ver,
            "node_counts": node_counts,
            "l3_entropy": entropy_info,
            "prerequisite_edges": len(prereq_rows),
            "dag_cycles_detected": len(broken),
            "pending_taxonomy_candidates": pending_cands,
        }

    concept_counts = {
        str(r["kind"]): {"active": int(r["active_cnt"]), "redirected": int(r["redir_cnt"])}
        for r in conn.execute(
            """
            SELECT kind,
                   SUM(CASE WHEN redirect_to_id IS NULL THEN 1 ELSE 0 END) AS active_cnt,
                   SUM(CASE WHEN redirect_to_id IS NOT NULL THEN 1 ELSE 0 END) AS redir_cnt
            FROM concepts
            GROUP BY kind;
            """
        ).fetchall()
    }

    # 板块 4：五边拓扑度分布对比期望
    servable_uids = [
        str(r["question_uid"])
        for r in conn.execute(
            f"SELECT question_uid FROM questions q WHERE eff_is_servable = 1 AND is_retired = 0 {gs_filter};",
            gs_params,
        ).fetchall()
    ]
    topology_health: dict[str, Any] = {}
    for rt in RELATION_TYPES:
        rel_rows = conn.execute(
            "SELECT source_question_uid, target_question_uid FROM question_relations WHERE relation_type = ?;",
            (rt,),
        ).fetchall()
        deg: dict[str, int] = {uid: 0 for uid in servable_uids}
        edge_cnt = 0
        for rr in rel_rows:
            su = str(rr["source_question_uid"])
            tu = str(rr["target_question_uid"])
            if grade_subject and (su not in deg and tu not in deg):
                continue
            edge_cnt += 1
            if su in deg:
                deg[su] += 1
            if tu in deg:
                deg[tu] += 1
        stats = _quantiles_summary(list(deg.values()))
        stats["edge_count"] = edge_cnt
        stats["expected_degree"] = settings.topology.expected_degree.get(rt, [0, 0])
        topology_health[rt] = stats

    # 板块 5：生效层一致性（纯只读保存点保护） + 人工镜像一致性 + LLM 成本审计
    eff_consistency = _readonly_verify_effective_consistency(conn)
    mirror_consistency = human_mirror.verify_snapshot(conn, human_dir=human_dir)
    llm_summary = [
        dict(r)
        for r in conn.execute(
            """
            SELECT stage, status, COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM llm_calls
            GROUP BY stage, status
            ORDER BY stage, status;
            """
        ).fetchall()
    ]

    # 板块 6：向量仓与分位表状态 + scope_drift 漂移巡检
    store = VectorStore(vectors_dir)
    vec_meta = store.load_meta()
    npz_counts: dict[str, int] = {}
    for npz_p in sorted(store.vectors_dir.glob("*.npz")):
        with np.load(npz_p, allow_pickle=False) as d:
            npz_counts[npz_p.name] = int(len(d["ids"]))

    scope_drift: dict[str, Any] = {}
    vector_drift_risks: list[str] = []
    for gsr in gs_rows:
        gs = str(gsr["grade_subject"])
        slug = str(gsr["slug"])
        l1_rows = conn.execute(
            """
            SELECT eff_primary_l1_module, COUNT(*) AS cnt
            FROM questions
            WHERE grade_subject = ? AND is_retired = 0 AND eff_is_excluded = 0 AND eff_is_servable = 1
            GROUP BY eff_primary_l1_module
            ORDER BY eff_primary_l1_module;
            """,
            (gs,),
        ).fetchall()
        gs_servable = sum(int(r["cnt"]) for r in l1_rows)
        cur_l1_dist = (
            {str(r["eff_primary_l1_module"] or ""): int(r["cnt"]) / float(gs_servable) for r in l1_rows}
            if gs_servable > 0
            else {}
        )
        d_eval = evaluate_scope_drift(vec_meta, slug, gs_servable, cur_l1_dist)
        scope_drift[gs] = {"slug": slug, **d_eval}
        if gs_servable > 0 and not d_eval["has_baseline"]:
            vector_drift_risks.append(
                f"VECTOR_DRIFT: {gs} ({slug}) 存在 {gs_servable} 道可服务题但尚未记录 scopes 分布基准，请运行 `shuati reindex --quantiles`"
            )
        elif d_eval["drifted"]:
            vector_drift_risks.append(
                f"VECTOR_DRIFT: {gs} ({slug}) 向量分位基准发生漂移（{'; '.join(d_eval['reasons'])}），请运行 `shuati reindex --quantiles`"
            )

    # 板块 7：待处理风险汇总 + 源题变更后仍激活的标注类覆写提示
    suspended_ov = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM human_overrides ho
            JOIN questions q ON q.question_uid = ho.question_uid
            WHERE ho.is_active = 1 AND ho.is_suspended = 1 {gs_filter};
            """,
            gs_params,
        ).fetchone()["c"]
    )
    ann_fields = sorted(effective.ANNOTATION_OVERRIDE_FIELDS)
    ann_ph = ",".join("?" for _ in ann_fields)
    ann_rows = conn.execute(
        f"""
        SELECT ho.override_id, ho.question_uid, ho.field_name
        FROM human_overrides ho
        JOIN raw_questions rq ON rq.question_uid = ho.question_uid
        JOIN questions q ON q.question_uid = ho.question_uid
        WHERE ho.is_active = 1
          AND ho.is_suspended = 0
          AND ho.field_name IN ({ann_ph})
          AND ho.base_fingerprint != rq.content_hash
          AND q.is_retired = 0
          {gs_filter}
        ORDER BY ho.question_uid, ho.field_name;
        """,
        [*ann_fields, *gs_params],
    ).fetchall()
    ann_uids = sorted({str(r["question_uid"]) for r in ann_rows})
    source_changed_annotation_overrides = {
        "count": len(ann_rows),
        "question_count": len(ann_uids),
        "question_uids": ann_uids,
    }

    risks: list[str] = []
    if eff_consistency.get("diff_count", 0) != 0:
        risks.append(f"eff_* 一致性存在差异: {eff_consistency.get('diff_count')} 题")
    if not mirror_consistency.get("is_consistent", False):
        risks.append("human/*.jsonl 与 DB 快照不一致，请运行 `shuati human export`")
    if needs_review_q > 0:
        risks.append(f"待人工复核题目 (NEEDS_REVIEW): {needs_review_q} 题")
    if suspended_ov > 0:
        risks.append(f"已挂起人工覆写: {suspended_ov} 条")
    if failed_runs_unreferenced > 0:
        risks.append(f"可清理的未引用 FAILED VLM 记录: {failed_runs_unreferenced} 条（可用 --prune-runs 清理）")
    for gs, th_info in taxonomy_health.items():
        if th_info["dag_cycles_detected"] > 0:
            risks.append(f"{gs} 先修图存在 {th_info['dag_cycles_detected']} 条环边")
        if th_info["pending_taxonomy_candidates"] > 0:
            risks.append(f"{gs} 存在 {th_info['pending_taxonomy_candidates']} 条待审核新词条候选")
    risks.extend(vector_drift_risks)
    if len(ann_rows) > 0:
        risks.append(
            f"温和抽检提示：存在 {len(ann_rows)} 条源题变更后仍保持激活的标注类覆写（涉及 {len(ann_uids)} 题），建议择机核对"
        )

    # 板块 8：新旧标签与题型纠偏对比（legacy_tags_json vs eff_* / question_concepts）
    blacklist_terms = _load_method_blacklist(Path(config_dir))
    new_methods_by_uid: dict[str, set[str]] = {}
    for mr in conn.execute(
        f"""
        SELECT qc.question_uid, c.name
        FROM question_concepts qc
        JOIN concepts c ON c.concept_id = qc.concept_id
        JOIN questions q ON q.question_uid = qc.question_uid
        WHERE qc.kind = 'METHOD' AND q.is_retired = 0 {gs_filter};
        """,
        gs_params,
    ).fetchall():
        new_methods_by_uid.setdefault(str(mr["question_uid"]), set()).add(str(mr["name"]).strip())

    legacy_rows = conn.execute(
        f"""
        SELECT q.question_uid, q.eff_format, q.eff_primary_l1_module, rq.legacy_tags_json
        FROM questions q
        JOIN raw_questions rq ON rq.question_uid = q.question_uid
        WHERE q.is_retired = 0 AND q.eff_is_excluded = 0 {gs_filter}
        ORDER BY q.question_uid;
        """,
        gs_params,
    ).fetchall()

    questions_with_legacy_tags = 0
    format_changed_count = 0
    format_changed_samples: list[dict[str, str]] = []
    domain_to_primary_l1_matrix: dict[str, dict[str, int]] = {}
    legacy_blacklist_hit_questions = 0
    blacklist_purged_questions = 0
    purged_term_counts: dict[str, int] = {}

    for lr in legacy_rows:
        uid = str(lr["question_uid"])
        raw_lt = str(lr["legacy_tags_json"] or "{}")
        try:
            lt_obj = json.loads(raw_lt)
        except Exception:
            lt_obj = {}
        if not isinstance(lt_obj, dict) or not lt_obj:
            continue
        questions_with_legacy_tags += 1

        eff_fmt = str(lr["eff_format"] or "").strip()
        eff_l1 = str(lr["eff_primary_l1_module"] or "").strip() or "(未标注)"

        fmt_list = _normalize_str_list(lt_obj.get("format"))
        legacy_fmt = fmt_list[0] if fmt_list else ""
        if legacy_fmt and eff_fmt and legacy_fmt != eff_fmt:
            format_changed_count += 1
            if len(format_changed_samples) < 10:
                format_changed_samples.append(
                    {
                        "question_uid": uid,
                        "legacy_format": legacy_fmt,
                        "eff_format": eff_fmt,
                    }
                )

        for dom in _normalize_str_list(lt_obj.get("domain")):
            row_map = domain_to_primary_l1_matrix.setdefault(dom, {})
            row_map[eff_l1] = row_map.get(eff_l1, 0) + 1

        legacy_methods = set(
            _normalize_str_list(lt_obj.get("method"))
            + _normalize_str_list(lt_obj.get("methods"))
        )
        hit_bl = legacy_methods & blacklist_terms
        if hit_bl:
            legacy_blacklist_hit_questions += 1
            cur_methods = new_methods_by_uid.get(uid, set())
            cleared = hit_bl - cur_methods
            residual = hit_bl & cur_methods
            if not residual:
                blacklist_purged_questions += 1
            for t_name in sorted(cleared):
                purged_term_counts[t_name] = purged_term_counts.get(t_name, 0) + 1

    legacy_comparison = {
        "questions_with_legacy_tags": questions_with_legacy_tags,
        "format_changed_count": format_changed_count,
        "format_changed_samples": format_changed_samples,
        "domain_to_primary_l1_matrix": domain_to_primary_l1_matrix,
        "method_blacklist_purge": {
            "blacklist_terms_loaded": len(blacklist_terms),
            "legacy_hit_question_count": legacy_blacklist_hit_questions,
            "purged_question_count": blacklist_purged_questions,
            "residual_question_count": legacy_blacklist_hit_questions - blacklist_purged_questions,
            "purged_term_counts": purged_term_counts,
        },
    }

    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "grade_subject": grade_subject or "ALL",
        "section_1_compile_coverage": {
            "total_questions": total_q,
            "active_questions": active_q,
            "servable_questions": servable_q,
            "servable_rate": round(servable_q / active_q, 4) if active_q else 0.0,
            "needs_review_questions": needs_review_q,
            "compile_state": stage_dist,
        },
        "section_2_verification_sympy": {
            "verification_status": verif_dist,
            "sympy_status": sympy_dist,
            "unreferenced_failed_runs": failed_runs_unreferenced,
            "pruned_failed_runs": pruned_failed_runs,
            "consensus_audit": consensus_audit,
        },
        "section_2b_difficulty_distribution": difficulty_distribution,
        "section_3_knowledge_graph": {
            "by_grade_subject": taxonomy_health,
            "concepts": concept_counts,
        },
        "section_4_topology_degrees": topology_health,
        "section_5_integrity_and_cost": {
            "effective_consistency": eff_consistency,
            "human_mirror_consistency": mirror_consistency,
            "llm_calls_summary": llm_summary,
        },
        "section_6_vectors": {
            "embedding_model": vec_meta.get("embedding_model", ""),
            "dimension": vec_meta.get("dimension", 0),
            "meta_version": vec_meta.get("meta_version", 0),
            "quantile_keys": sorted(list(vec_meta.get("quantiles", {}).keys())),
            "npz_row_counts": npz_counts,
            "scope_drift": scope_drift,
        },
        "section_7_risks_and_actions": {
            "suspended_overrides": suspended_ov,
            "source_changed_annotation_overrides": source_changed_annotation_overrides,
            "risks": risks,
        },
        "section_8_legacy_tags_comparison": legacy_comparison,
    }

    # 写出 Markdown + JSON 双格式报告
    r_dir = Path(reports_dir)
    r_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    json_path = r_dir / f"audit_{ts}.json"
    md_path = r_dir / f"audit_{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    wilson_str = f"{wilson_95_upper:.4f}" if wilson_95_upper is not None else "N/A"
    md_lines = [
        f"# Stage Q 知识网与全库质量体检报告 ({report['generated_at']})",
        "",
        f"- **作用域**: `{report['grade_subject']}`",
        "",
        "## 1. 编译阶段分布与可刷题覆盖率",
        f"- 总题数: **{total_q}** | 活跃非废题: **{active_q}** | 可出题 (`eff_is_servable=1`): **{servable_q}** ({report['section_1_compile_coverage']['servable_rate']*100:.1f}%) | 待复核 (`NEEDS_REVIEW`): **{needs_review_q}**",
        "",
        "| Stage | 状态分布 |",
        "|---|---|",
    ]
    for stg, st_map in sorted(stage_dist.items()):
        md_lines.append(f"| `{stg}` | `{json.dumps(st_map, ensure_ascii=False)}` |")

    md_lines.extend(
        [
            "",
            "## 2. 验真与 SymPy 求解健康度",
            f"- 验真状态: `{json.dumps(verif_dist, ensure_ascii=False)}`",
            f"- SymPy 状态: `{json.dumps(sympy_dist, ensure_ascii=False)}`",
            f"- 未引用 `FAILED` 运行残余: `{failed_runs_unreferenced}`（本次清理: `{pruned_failed_runs}`）",
            f"- 假共识抽检池: 单路有图=`{single_run_with_images}`, 单路无图=`{single_run_no_images}`, 双路共识=`{dual_run}`, 推荐抽检=`{recommended_sample_size}`",
            f"- 已审共识样本: `n={audited_n}`, 假共识=`k={false_consensus_k}` (`rate={false_consensus_rate:.4f}`, `wilson_95_upper={wilson_str}`)",
            "",
            "### 2b. 难度档位与四维边缘分布",
            f"- 8 档 `eff_tier` 直方图: `{json.dumps(tier_histogram, ensure_ascii=False)}`",
            f"- `[C, S, T, L]` 边缘频次: `C={json.dumps(c_dist)}`, `S={json.dumps(s_dist)}`, `T={json.dumps(t_dist)}`, `L={json.dumps(l_dist)}`",
            "",
            "## 3. 词表与 L3 知识网健康度",
        ]
    )
    for gs, info in taxonomy_health.items():
        ent = info["l3_entropy"]
        md_lines.append(
            f"- **{gs}** (`{info['taxonomy_status']}` `{info['taxonomy_version']}`): "
            f"节点=`{json.dumps(info['node_counts'], ensure_ascii=False)}`, "
            f"L3熵=`{ent.get('entropy', 0)}` (norm=`{ent.get('normalized_entropy', 0)}`), "
            f"PREREQUISITE边=`{info['prerequisite_edges']}`, 环=`{info['dag_cycles_detected']}`, "
            f"新词条候选=`{info['pending_taxonomy_candidates']}`"
        )
    md_lines.append(f"- 全局概念统计: `{json.dumps(concept_counts, ensure_ascii=False)}`")

    md_lines.extend(
        [
            "",
            "## 4. 五边拓扑度分布对比期望",
            "| 关系类型 | 总边数 | min | p50 | p95 | max | 期望度区间 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for rt in RELATION_TYPES:
        s = topology_health[rt]
        md_lines.append(
            f"| `{rt}` | {s['edge_count']} | {s['min']} | {s['p50']} | {s['p95']} | {s['max']} | `{s['expected_degree']}` |"
        )

    md_lines.extend(
        [
            "",
            "## 5. 生效层一致性、人工镜像与成本审计",
            f"- `effective.verify_effective_consistency`: `total_checked={eff_consistency['total_checked']}`, `diff_count={eff_consistency['diff_count']}`",
            f"- `human_mirror.verify_snapshot`: `is_consistent={mirror_consistency['is_consistent']}`",
            "",
            "## 6. 向量仓与冻结分位表",
            f"- 模型: `{report['section_6_vectors']['embedding_model']}` (`dim={report['section_6_vectors']['dimension']}`, `meta_version={report['section_6_vectors']['meta_version']}`)",
            f"- 矩阵行数: `{json.dumps(npz_counts, ensure_ascii=False)}`",
            f"- 漂移巡检 (`scope_drift`): `{json.dumps(scope_drift, ensure_ascii=False)}`",
            "",
            "## 7. 待处理风险汇总",
            f"- 源题已变仍激活标注覆写: `{source_changed_annotation_overrides['count']}` 条（涉及 `{source_changed_annotation_overrides['question_count']}` 题）",
        ]
    )
    if risks:
        for rk in risks:
            md_lines.append(f"- {rk}")
    else:
        md_lines.append("- 无阻断性风险。")

    md_lines.extend(
        [
            "",
            "## 8. 新旧标签与题型纠偏对比",
            f"- 含旧标签题数: `{questions_with_legacy_tags}` | 题型 (`format`) 纠偏数: `{format_changed_count}`",
            f"- `domain -> primary_l1` 交叉矩阵: `{json.dumps(domain_to_primary_l1_matrix, ensure_ascii=False)}`",
            f"- 黑名单泛词清除统计: 命中旧题=`{legacy_blacklist_hit_questions}`, 已彻底清除=`{blacklist_purged_questions}`, 按词统计=`{json.dumps(purged_term_counts, ensure_ascii=False)}`",
        ]
    )
    if format_changed_samples:
        md_lines.append(
            f"- 题型纠偏样例 (Top {len(format_changed_samples)}): `{json.dumps(format_changed_samples, ensure_ascii=False)}`"
        )

    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return {
        "report": report,
        "md_path": str(md_path),
        "json_path": str(json_path),
    }
