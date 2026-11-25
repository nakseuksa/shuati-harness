r"""Repro P2-1: `transition_taxonomy` 智能无损晋升在算子撞名与跨 L2 合并时污染并查集账本并导致 `revert_merge_batch` 崩溃。

覆盖两类确定性崩溃与状态污染（全部基于临时目录 SQLite 实测）：
1. 同 L2 顺序归并标准名复用（`N0 -> N1("等高面积比")`, `N1 -> N2("共高底边比")`, `N2 -> N3("等高面积比", _c1)`）：
   `transition_taxonomy` 将重定向子节点 `N1` 折叠到活跃主节点 `new_N3`（`new_child_id == new_target_id`），
   但 Step B5 未识别 `old_from` 已坍缩为活跃根节点，将 `batch_1` 反向改写为 `from_id=new_N3, to_id=new_N2, affected_child_ids=[new_N0]`。
   随后按严格逆序执行 `revert_merge_batch("batch_2")`（回收孤立 `created_master_id=new_N3`）与 `revert_merge_batch("batch_1")` 时，
   直接抛出未捕获的 `sqlite3.IntegrityError: FOREIGN KEY constraint failed`！
2. 跨 L2 `changelog` `merge` 同名算子碰撞（`"相遇问题"` 中 `B1("相对速度法") -> M1("相遇速度和模型")` vs `"追及问题"` 中活跃主节点 `A2("相对速度法")`）：
   Step A2 在 `active_conflict` 分支将已重定向子节点 `B1` 错映射到另一 L2 的活跃节点 `new_A2`（而非 `new_M1`），
   导致原挂载在 `B1` 的题目 `eff_node_ids_json` 被静默篡改为 `new_A2`，且 Step B5 生成 `from_id=new_A2(redirect_to_id=NULL) -> to_id=new_M1`
   的虚假活跃账本行，与 `"追及问题"` 原有 `batch_2(C2 -> A2)` 伪造跨批依赖，导致 `revert_merge_batch("batch_1")` 抛 `PreconditionError` 崩溃。

运行：`.\.venv\Scripts\python.exe tests/repro/repro_p2_taxonomy_lock_ledger_and_collision.py`
期望修复后 Exit Code == 0；当前代码实测 Exit Code != 0。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shuati.core import effective  # noqa: E402
from shuati.core.knowledge_graph import (  # noqa: E402
    apply_cluster_assignments,
    apply_merge_pairs,
    revert_merge_batch,
)
from shuati.core.taxonomy import (  # noqa: E402
    TaxonomyChangeAction,
    TaxonomyChangelogEntry,
    TaxonomyL1Entry,
    TaxonomyL2Entry,
    TaxonomySpec,
    make_node_id,
    save_taxonomy_spec,
    transition_taxonomy,
)
from shuati.db.connection import get_connection  # noqa: E402
from shuati.db.schema import init_db  # noqa: E402
from shuati.models.domain import AnswerPart  # noqa: E402

GS = "小学奥数"
SLUG = "primary_olympiad"


def _seed_question_with_step(conn, uid: str, l1: str, l2: str, op: str, nid: str) -> None:
    sid = uid.split(":")[-1]
    ans = json.dumps(
        [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="12", sympy_expr="12").model_dump()],
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO raw_questions(
            question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash
        ) VALUES (?, ?, 'BATCH_JSONL', 'ch01', ?, '测试题', ?);
        """,
        (uid, GS, sid, f"h_{sid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_final_answers_json, m_primary_l1_module,
            m_c_score, m_s_score, m_t_score, m_l_score
        ) VALUES (?, ?, '测试题', '解答题', 'VERIFIED_CONSENSUS', ?, ?, 1, 1, 1, 1);
        """,
        (uid, GS, ans, l1),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown
        ) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '标准解答');
        """,
        (f"{uid}::b1", uid),
    )
    conn.execute(
        """
        INSERT INTO question_steps(
            step_id, question_uid, branch_id, step_index, sub_goal,
            raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id
        ) VALUES (?, ?, 'branch_1', 1, '核心步骤', ?, ?, ?, ?, ?, ?);
        """,
        (f"{uid}::s1", uid, l1, l2, op, l1, l2, nid),
    )


def _check_case_1_sequential_compaction_fk_crash(base_tmp: Path) -> None:
    tmp = base_tmp / "case1_fk"
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "config"
    conn = get_connection(tmp / "case1.db")
    try:
        init_db(conn)
        draft_spec = TaxonomySpec(
            grade_subject=GS,
            slug=SLUG,
            status="DRAFT",
            version="draft-1",
            modules=[TaxonomyL1Entry(name="平面几何", l2_entities=[TaxonomyL2Entry(name="等高模型")])],
        )
        dyaml = cfg / "taxonomies" / f"taxonomy_{SLUG}.draft.yaml"
        save_taxonomy_spec(draft_spec, dyaml)
        conn.execute(
            "UPDATE grade_subjects SET taxonomy_status='DRAFT', taxonomy_version='draft-1', taxonomy_file=? WHERE grade_subject=?;",
            (str(dyaml), GS),
        )

        n0 = make_node_id(SLUG, "draft-1", "L3", "平面几何", "等高模型", "底比求面积")
        n1 = make_node_id(SLUG, "draft-1", "L3", "平面几何", "等高模型", "等高面积比")
        n2 = make_node_id(SLUG, "draft-1", "L3", "平面几何", "等高模型", "共高底边比")
        for nid, op in ((n0, "底比求面积"), (n1, "等高面积比"), (n2, "共高底边比")):
            conn.execute(
                """
                INSERT INTO knowledge_nodes(
                    node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator
                ) VALUES (?, ?, 'draft-1', 'L3', '平面几何', '等高模型', ?);
                """,
                (nid, GS, op),
            )

        _seed_question_with_step(conn, "primary_olympiad:ch01:q0", "平面几何", "等高模型", "底比求面积", n0)
        _seed_question_with_step(conn, "primary_olympiad:ch01:q2", "平面几何", "等高模型", "共高底边比", n2)
        conn.commit()

        sc = {"grade_subject": GS, "taxonomy_version": "draft-1", "l1_module": "平面几何", "l2_entity": "等高模型"}
        apply_merge_pairs(conn, target_kind="KNOWLEDGE_NODE", scope_dict=sc, merges=[{"from_id": n0, "to_id": n1, "evidence": "同义0"}], batch_id="batch_0")
        apply_merge_pairs(conn, target_kind="KNOWLEDGE_NODE", scope_dict=sc, merges=[{"from_id": n1, "to_id": n2, "evidence": "同义1"}], batch_id="batch_1")
        apply_cluster_assignments(
            conn,
            target_kind="KNOWLEDGE_NODE",
            scope_dict=sc,
            canonical_operators=[{"canonical_name": "等高面积比", "description": "标准算子"}],
            assignments=[{"item_id": n2, "raw_l3": "共高底边比", "canonical_name": "等高面积比", "reason": "标准命名"}],
            batch_id="batch_2",
            slug=SLUG,
            grade_subject=GS,
            taxonomy_version="draft-1",
        )

        transition_taxonomy(
            conn,
            grade_subject=GS,
            yaml_path=dyaml,
            target_version="1.0",
            target_status="LOCKED",
            vectors_dir=tmp / "vec",
            config_dir=cfg,
            backups_dir=tmp / "bak",
            human_dir=tmp / "hum",
            reports_dir=tmp / "rep",
            lock_path=tmp / ".lock",
        )

        # 1) 对锁表后仍保持活跃 (is_reverted=0) 的批次按 merge_id 逆序执行回滚，不得触发 sqlite3.IntegrityError 外键崩溃
        active_batches: list[str] = []
        for r in conn.execute(
            "SELECT batch_id FROM compaction_audit_ledger WHERE is_reverted = 0 ORDER BY merge_id DESC;"
        ).fetchall():
            bid = str(r["batch_id"])
            if bid not in active_batches:
                active_batches.append(bid)

        # 同时核验：任何保持 is_reverted=0 的 KNOWLEDGE_NODE 账本行，其 from_id 绝不能是活跃根节点 (redirect_to_id IS NULL)
        bad_active_from = [
            dict(r)
            for r in conn.execute(
                """
                SELECT l.merge_id, l.batch_id, l.from_id, l.to_id, kn.redirect_to_id
                FROM compaction_audit_ledger l
                JOIN knowledge_nodes kn ON kn.node_id = l.from_id
                WHERE l.target_kind = 'KNOWLEDGE_NODE' AND l.is_reverted = 0 AND kn.redirect_to_id IS NULL;
                """
            ).fetchall()
        ]

        for bid in active_batches:
            revert_merge_batch(conn, bid, backups_dir=tmp / "bak", lock_path=tmp / ".lock")
        assert conn.execute("PRAGMA foreign_key_check;").fetchall() == []
        assert not bad_active_from, (
            f"Case 1 账本失真：活跃账本行 from_id 指向了 redirect_to_id=NULL 的根节点: {bad_active_from}"
        )
    finally:
        conn.close()


def _check_case_2_cross_l2_merge_collision(base_tmp: Path) -> None:
    tmp = base_tmp / "case2_cross_l2"
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "config"
    conn = get_connection(tmp / "case2.db")
    try:
        init_db(conn)
        draft_spec = TaxonomySpec(
            grade_subject=GS,
            slug=SLUG,
            status="DRAFT",
            version="draft-1",
            modules=[
                TaxonomyL1Entry(
                    name="行程问题",
                    l2_entities=[TaxonomyL2Entry(name="相遇问题"), TaxonomyL2Entry(name="追及问题")],
                )
            ],
        )
        dyaml = cfg / "taxonomies" / f"taxonomy_{SLUG}.draft.yaml"
        save_taxonomy_spec(draft_spec, dyaml)
        conn.execute(
            "UPDATE grade_subjects SET taxonomy_status='DRAFT', taxonomy_version='draft-1', taxonomy_file=? WHERE grade_subject=?;",
            (str(dyaml), GS),
        )

        b1 = make_node_id(SLUG, "draft-1", "L3", "行程问题", "相遇问题", "相对速度法")
        m1 = make_node_id(SLUG, "draft-1", "L3", "行程问题", "相遇问题", "相遇速度和模型")
        c2 = make_node_id(SLUG, "draft-1", "L3", "行程问题", "追及问题", "追及时间公式")
        a2 = make_node_id(SLUG, "draft-1", "L3", "行程问题", "追及问题", "相对速度法")
        for nid, l2, op in (
            (b1, "相遇问题", "相对速度法"),
            (m1, "相遇问题", "相遇速度和模型"),
            (c2, "追及问题", "追及时间公式"),
            (a2, "追及问题", "相对速度法"),
        ):
            conn.execute(
                """
                INSERT INTO knowledge_nodes(
                    node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator
                ) VALUES (?, ?, 'draft-1', 'L3', '行程问题', ?, ?);
                """,
                (nid, GS, l2, op),
            )

        _seed_question_with_step(conn, "primary_olympiad:ch01:q_meet", "行程问题", "相遇问题", "相对速度法", b1)
        _seed_question_with_step(conn, "primary_olympiad:ch01:q_chase", "行程问题", "追及问题", "相对速度法", a2)
        conn.commit()

        apply_merge_pairs(
            conn,
            target_kind="KNOWLEDGE_NODE",
            scope_dict={"grade_subject": GS, "taxonomy_version": "draft-1", "l1_module": "行程问题", "l2_entity": "相遇问题"},
            merges=[{"from_id": b1, "to_id": m1, "evidence": "相遇同义"}],
            batch_id="batch_meet",
        )
        apply_merge_pairs(
            conn,
            target_kind="KNOWLEDGE_NODE",
            scope_dict={"grade_subject": GS, "taxonomy_version": "draft-1", "l1_module": "行程问题", "l2_entity": "追及问题"},
            merges=[{"from_id": c2, "to_id": a2, "evidence": "追及同义"}],
            batch_id="batch_chase",
        )
        effective.recompute_all(conn, GS)
        conn.commit()

        lock_spec = TaxonomySpec(
            grade_subject=GS,
            slug=SLUG,
            status="DRAFT",
            version="draft-1",
            modules=[TaxonomyL1Entry(name="行程问题", l2_entities=[TaxonomyL2Entry(name="直线行程")])],
            changelog=[
                TaxonomyChangelogEntry(
                    from_version="draft-1",
                    to_version="1.0",
                    actions=[
                        TaxonomyChangeAction(op="merge", level="L2", parent_l1="行程问题", target="相遇问题", merge_into="直线行程"),
                        TaxonomyChangeAction(op="merge", level="L2", parent_l1="行程问题", target="追及问题", merge_into="直线行程"),
                    ],
                )
            ],
        )
        save_taxonomy_spec(lock_spec, dyaml)
        transition_taxonomy(
            conn,
            grade_subject=GS,
            yaml_path=dyaml,
            target_version="1.0",
            target_status="LOCKED",
            vectors_dir=tmp / "vec",
            config_dir=cfg,
            backups_dir=tmp / "bak",
            human_dir=tmp / "hum",
            reports_dir=tmp / "rep",
            lock_path=tmp / ".lock",
        )

        new_m1 = make_node_id(SLUG, "1.0", "L3", "行程问题", "直线行程", "相遇速度和模型")
        q_meet_eff = json.loads(
            conn.execute("SELECT eff_node_ids_json FROM questions WHERE question_uid='primary_olympiad:ch01:q_meet';").fetchone()[0]
        )
        assert q_meet_eff == [new_m1], (
            f"Case 2 语义漂移：q_meet 在锁表前已归并至 '相遇速度和模型' ({new_m1})，锁表后却被错绑到 {q_meet_eff}"
        )

        bad_ledgers = conn.execute(
            """
            SELECT l.merge_id, l.batch_id, l.from_id, l.to_id, kn.redirect_to_id
            FROM compaction_audit_ledger l
            JOIN knowledge_nodes kn ON kn.node_id = l.from_id
            WHERE l.target_kind = 'KNOWLEDGE_NODE' AND l.is_reverted = 0 AND kn.redirect_to_id IS NULL;
            """
        ).fetchall()
        assert not bad_ledgers, (
            f"Case 2 账本失真：已坍缩归并被保留为活跃账本行且 from_id.redirect_to_id IS NULL: {[dict(r) for r in bad_ledgers]}"
        )
    finally:
        conn.close()


def test_repro_p2_taxonomy_lock_ledger_and_collision(tmp_path: Path) -> None:
    _check_case_2_cross_l2_merge_collision(tmp_path)
    _check_case_1_sequential_compaction_fk_crash(tmp_path)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="repro_p2_tax_"))
    try:
        errors: list[str] = []
        for label, fn in (
            ("Case1_SequentialCompaction_FKCrash", _check_case_1_sequential_compaction_fk_crash),
            ("Case2_CrossL2Merge_Collision", _check_case_2_cross_l2_merge_collision),
        ):
            try:
                fn(tmp)
                print(f"[PASS] {label}")
            except Exception as exc:
                msg = f"[FAIL] {label}: {type(exc).__name__}: {exc}"
                print(msg)
                errors.append(msg)
        if errors:
            return 1
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
