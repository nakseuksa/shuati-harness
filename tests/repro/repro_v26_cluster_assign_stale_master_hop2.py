"""Repro v26: apply_cluster_assignments 的 master_cache 在规范节点自身被重定向后失效，
导致后续同名规范归并产生 redirect 跳数 == 2（违反路径压缩跳数 <= 1 不变量）。

场景（LLM 常见：把输入里已存在的某条 raw_l3 同时用作 canonical_name）：
  assignments: a -> X ; X -> Y ; b -> X
  X 是已存在活跃 L3 节点。a->X 缓存 master_cache['X']=X；X->Y 后 X.redirect=Y；
  b->X 仍用缓存的 X 作为目标 => b.redirect = X, X.redirect = Y （2 跳）。
运行：.venv\\Scripts\\python.exe tests/repro/repro_v26_cluster_assign_stale_master_hop2.py
期望修复后 Exit 0。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v26_common import GS, L1, L2, add_l3, make_conn, seed_q  # noqa: E402

from shuati.core.knowledge_graph import apply_cluster_assignments  # noqa: E402


def max_hops(conn) -> int:
    worst = 0
    for r in conn.execute("SELECT node_id, redirect_to_id FROM knowledge_nodes WHERE node_level='L3';").fetchall():
        hops, cur, seen = 0, r["node_id"], set()
        while True:
            nxt = conn.execute("SELECT redirect_to_id FROM knowledge_nodes WHERE node_id=?;", (cur,)).fetchone()[0]
            if not nxt or nxt in seen:
                break
            seen.add(cur)
            hops += 1
            cur = nxt
        worst = max(worst, hops)
    return worst


def main() -> int:
    conn, d = make_conn("hop2")
    for nid, nm in (("a", "op_a"), ("b", "op_b"), ("x", "op_x"), ("y", "op_y")):
        add_l3(conn, nid, nm)
        seed_q(conn, f"q_{nid}", steps=[(L1, L2, nm, nid)])
    scope = {"grade_subject": GS, "taxonomy_version": "1.0", "l1_module": L1, "l2_entity": L2}
    apply_cluster_assignments(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict=scope,
        canonical_operators=[],
        assignments=[
            {"raw_l3": "op_a", "canonical_name": "op_x"},
            {"raw_l3": "op_x", "canonical_name": "op_y"},
            {"raw_l3": "op_b", "canonical_name": "op_x"},
        ],
        batch_id="hop2_b1",
        slug="primary_olympiad",
        grade_subject=GS,
        taxonomy_version="1.0",
    )
    rows = {r[0]: r[1] for r in conn.execute("SELECT node_id, redirect_to_id FROM knowledge_nodes WHERE node_level='L3';")}
    print("redirects:", rows)
    h = max_hops(conn)
    assert h <= 1, f"redirect 跳数 {h} > 1（stale master_cache）"
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
