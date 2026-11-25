"""Repro: migrate_taxonomy_incremental leaves merged primary L1 duplicated in m_secondary_l1_modules_json."""

from __future__ import annotations

import json
from pathlib import Path

from shuati.core import effective
from shuati.core.taxonomy import (
    TaxonomyChangeAction,
    TaxonomyChangelogEntry,
    TaxonomyL1Entry,
    TaxonomyL2Entry,
    TaxonomySpec,
    make_node_id,
    migrate_taxonomy_incremental,
    save_taxonomy_spec,
)
from shuati.db.connection import get_connection
from shuati.db.schema import init_db

GS = "小学奥数"
SLUG = "primary_olympiad"


def test_repro_l1_merge_primary_into_existing_secondary_deduplicates_secondary(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "repro_l1_merge.db")
    init_db(conn)

    v1_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="LOCKED",
        version="1.0",
        modules=[
            TaxonomyL1Entry(name="L1_Old", l2_entities=[TaxonomyL2Entry(name="L2_A")]),
            TaxonomyL1Entry(name="L1_Target", l2_entities=[TaxonomyL2Entry(name="L2_B")]),
        ],
    )
    cfg_dir = tmp_path / "config"
    v1_yaml = cfg_dir / "taxonomies" / f"taxonomy_{SLUG}.yaml"
    save_taxonomy_spec(v1_spec, v1_yaml)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status = 'LOCKED', taxonomy_version = '1.0', taxonomy_file = ? WHERE grade_subject = ?;",
        (str(v1_yaml), GS),
    )

    l2_a = make_node_id(SLUG, "1.0", "L2", "L1_Old", "L2_A", "")
    l2_b = make_node_id(SLUG, "1.0", "L2", "L1_Target", "L2_B", "")
    l3_a = make_node_id(SLUG, "1.0", "L3", "L1_Old", "L2_A", "算子A")
    for nid, lvl, l1, l2, l3 in [
        (l2_a, "L2", "L1_Old", "L2_A", ""),
        (l2_b, "L2", "L1_Target", "L2_B", ""),
        (l3_a, "L3", "L1_Old", "L2_A", "算子A"),
    ]:
        conn.execute(
            """
            INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
            VALUES (?, ?, '1.0', ?, ?, ?, ?);
            """,
            (nid, GS, lvl, l1, l2, l3),
        )

    # q1 has primary=L1_Old and secondary=["L1_Target"]
    conn.execute(
        """
        INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash)
        VALUES ('q1', ?, 'BATCH_JSONL', 'c1', 'q1', '测试题干', 'h_q1');
        """,
        (GS,),
    )
    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, m_question_text, m_format,
            m_verification_status, m_primary_l1_module, m_secondary_l1_modules_json
        )
        VALUES ('q1', ?, '测试题干', '解答题', 'VERIFIED_CONSENSUS', 'L1_Old', '["L1_Target"]');
        """,
        (GS,),
    )
    conn.execute(
        """
        INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown)
        VALUES ('q1::b1', 'q1', 'branch_1', 1, 'PRIMARY_VLM', '解答');
        """
    )
    conn.execute(
        """
        INSERT INTO question_steps(
            step_id, question_uid, branch_id, step_index, sub_goal,
            raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id
        )
        VALUES ('q1::b1::1', 'q1', 'branch_1', 1, '步骤1', 'L1_Old', 'L2_A', '算子A', 'L1_Old', 'L2_A', ?);
        """,
        (l3_a,),
    )
    conn.execute(
        "INSERT INTO compile_state(question_uid, stage, status) VALUES ('q1', 'S3D', 'OK'), ('q1', 'S4', 'OK');"
    )
    effective.recompute(conn, "q1")
    conn.commit()

    # Merge L1_Old -> L1_Target
    v2_spec = TaxonomySpec(
        grade_subject=GS,
        slug=SLUG,
        status="LOCKED",
        version="1.1",
        modules=[
            TaxonomyL1Entry(
                name="L1_Target",
                l2_entities=[TaxonomyL2Entry(name="L2_A"), TaxonomyL2Entry(name="L2_B")],
            ),
        ],
        changelog=[
            TaxonomyChangelogEntry(
                from_version="1.0",
                to_version="1.1",
                actions=[
                    TaxonomyChangeAction(op="merge", level="L1", target="L1_Old", merge_into="L1_Target"),
                ],
            )
        ],
    )
    v2_yaml = tmp_path / "v1.1.yaml"
    save_taxonomy_spec(v2_spec, v2_yaml)

    migrate_taxonomy_incremental(
        conn,
        grade_subject=GS,
        new_yaml_path=v2_yaml,
        config_dir=cfg_dir,
        backups_dir=tmp_path / "bk",
        human_dir=tmp_path / "human",
        lock_path=tmp_path / ".lock",
        vectors_dir=tmp_path / "vec",
    )

    row = conn.execute(
        """
        SELECT m_primary_l1_module, m_secondary_l1_modules_json,
               eff_primary_l1_module, eff_secondary_l1_modules_json
        FROM questions WHERE question_uid = 'q1';
        """
    ).fetchone()
    assert row is not None
    assert row["m_primary_l1_module"] == "L1_Target"
    assert row["eff_primary_l1_module"] == "L1_Target"
    # Primary L1 must NOT be duplicated in secondary L1 modules after merge (matches transition_taxonomy invariant)
    assert "L1_Target" not in json.loads(row["m_secondary_l1_modules_json"])
    assert "L1_Target" not in json.loads(row["eff_secondary_l1_modules_json"])
    conn.close()
