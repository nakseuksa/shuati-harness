"""Stage Q 自动质量体检与巡检报告生成器（DESIGN.md §4.7）。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from shuati.config import Settings
from shuati.core import effective
from shuati.core.knowledge_graph import (
    check_dag_and_break_cycles,
    compute_l3_distribution_entropy,
)
from shuati.core.topology import RELATION_TYPES
from shuati.db import human_mirror
from shuati.vectors.store import VectorStore


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


def run_stage_q_audit(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str | None = None,
    human_dir: Path | str = Path("human"),
    vectors_dir: Path | str = Path("data/vectors"),
    reports_dir: Path | str = Path("data/reports"),
    prune_runs: bool = False,
) -> dict[str, Any]:
    """生成严格对齐 DESIGN.md §4.7 的 7 板块体检报告（.md + .json）。"""
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

    # 板块 2：SymPy 与双盲验真健康度
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

    # 板块 5：生效层一致性 + 人工镜像一致性 + LLM 成本审计
    eff_consistency = effective.verify_effective_consistency(conn)
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

    # 板块 6：向量仓与分位表状态
    store = VectorStore(vectors_dir)
    vec_meta = store.load_meta()
    npz_counts: dict[str, int] = {}
    for npz_p in sorted(store.vectors_dir.glob("*.npz")):
        with np.load(npz_p, allow_pickle=False) as d:
            npz_counts[npz_p.name] = int(len(d["ids"]))

    # 板块 7：待处理风险汇总
    suspended_ov = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM human_overrides WHERE is_active = 1 AND is_suspended = 1;"
        ).fetchone()["c"]
    )
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
        },
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
        },
        "section_7_risks_and_actions": {
            "suspended_overrides": suspended_ov,
            "risks": risks,
        },
    }

    # 写出 Markdown + JSON 双格式报告
    r_dir = Path(reports_dir)
    r_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    json_path = r_dir / f"audit_{ts}.json"
    md_path = r_dir / f"audit_{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

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
            "",
            "## 7. 待处理风险汇总",
        ]
    )
    if risks:
        for rk in risks:
            md_lines.append(f"- {rk}")
    else:
        md_lines.append("- 无阻断性风险。")

    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return {
        "report": report,
        "md_path": str(md_path),
        "json_path": str(json_path),
    }
