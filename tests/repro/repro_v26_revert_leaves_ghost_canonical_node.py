"""Repro v26: apply_cluster_assignments 新建的规范主 L3 节点，在 revert_merge_batch 后残留为"幽灵"活跃空节点（回滚非无损）。

契约/架构要求 revert 可无损拆回；实际账本不记录新建主节点，回滚后图谱活跃 L3 集合 != 归并前。
运行：.venv\\Scripts\\python.exe tests/repro/repro_v26_revert_leaves_ghost_canonical_node.py
期望修复后 Exit 0。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v26_common import GS, L1, L2, add_l3, make_conn, seed_q  # noqa: E402

from shuati.core.knowledge_graph import apply_cluster_assignments, revert_merge_batch  # noqa: E402


def active_l3(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT l3_operator FROM knowledge_nodes WHERE node_level='L3' AND redirect_to_id IS NULL;"
        )
    }


def main() -> int:
    conn, d = make_conn("ghost")
    add_l3(conn, "n1", "op_alpha")
    add_l3(conn, "n2", "op_beta")
    seed_q(conn, "q1", steps=[(L1, L2, "op_alpha", "n1")])
    seed_q(conn, "q2", steps=[(L1, L2, "op_beta", "n2")])
    before = active_l3(conn)
    scope = {"grade_subject": GS, "taxonomy_version": "1.0", "l1_module": L1, "l2_entity": L2}
    res = apply_cluster_assignments(
        conn,
        target_kind="KNOWLEDGE_NODE",
        scope_dict=scope,
        canonical_operators=[{"canonical_name": "canon_new", "description": ""}],
        assignments=[
            {"raw_l3": "op_alpha", "canonical_name": "canon_new"},
            {"raw_l3": "op_beta", "canonical_name": "canon_new"},
        ],
        batch_id="ghost_b1",
        slug="primary_olympiad",
        grade_subject=GS,
        taxonomy_version="1.0",
    )
    assert res["canonical_nodes_created"] == 1, res
    revert_merge_batch(conn, "ghost_b1", backups_dir=d / "bk", lock_path=d / ".lock")
    after = active_l3(conn)
    print("before:", sorted(before))
    print("after :", sorted(after))
    assert after == before, f"revert 未无损：残留幽灵活跃 L3 节点 {sorted(after - before)}"
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
