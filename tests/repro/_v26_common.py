"""v26 repro 共享夹具（仅 ASCII 标识，避免控制台编码干扰）。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from shuati.core import effective  # noqa: E402
from shuati.db.connection import get_connection  # noqa: E402
from shuati.db.schema import init_db  # noqa: E402

GS = "\u5c0f\u5b66\u5965\u6570"  # 小学奥数
L1 = "\u5e73\u9762\u51e0\u4f55"  # 平面几何
L2 = "\u7b49\u9ad8\u6a21\u578b"  # 等高模型


def make_conn(tag: str = "v26") -> Any:
    d = Path(tempfile.mkdtemp(prefix=f"repro_{tag}_"))
    conn = get_connection(d / "t.db")
    init_db(conn)
    conn.execute(
        "UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;",
        (GS,),
    )
    conn.commit()
    return conn, d


def seed_q(
    conn: Any,
    uid: str,
    *,
    text: str = "t",
    visual_facts: str = "",
    ver_status: str = "VERIFIED_CONSENSUS",
    steps: list[tuple[str, str, str, str | None]] | None = None,
    skel: str = "skeleton text",
    primary_l1: str = L1,
    c: int = 2,
    s: int = 2,
    t: int = 2,
    l_val: int = 1,
    motifs: list[str] | None = None,
    s_states: tuple[str, ...] = ("S1", "S2", "S3C", "S3D"),
) -> None:
    conn.execute(
        """
        INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id,
                                  raw_chapter, raw_question, legacy_tags_json, content_hash)
        VALUES (?, ?, 'BATCH_JSONL', 'secret_col', ?, 'secret_chap', ?, '{}', ?);
        """,
        (uid, GS, uid, text, f"h_{uid}"),
    )
    conn.execute(
        """
        INSERT INTO questions(question_uid, grade_subject, m_question_text, m_visual_facts, m_format,
                              m_verification_status, m_abstract_skeleton, m_primary_l1_module,
                              m_c_score, m_s_score, m_t_score, m_l_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (uid, GS, text, visual_facts, "\u89e3\u7b54\u9898", ver_status, skel, primary_l1, c, s, t, l_val),
    )
    conn.execute(
        "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) "
        "VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', 'sol');",
        (f"{uid}::b1", uid),
    )
    for idx, (l1, l2, l3, nid) in enumerate(steps or [], start=1):
        conn.execute(
            """
            INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal,
                                       raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id)
            VALUES (?, ?, 'branch_1', ?, 'step', ?, ?, ?, ?, ?, ?);
            """,
            (f"{uid}::b1::{idx}", uid, idx, l1, l2, l3, l1, l2, nid),
        )
    for m in motifs or []:
        cid = f"MOTIF:{m}"
        conn.execute("INSERT OR IGNORE INTO concepts(concept_id, kind, name, is_seed) VALUES (?, 'MOTIF', ?, 0);", (cid, m))
        conn.execute(
            "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, 'MOTIF', 'MACHINE');",
            (uid, cid),
        )
    for st in s_states:
        conn.execute("INSERT INTO compile_state(question_uid, stage, status) VALUES (?, ?, 'OK');", (uid, st))
    effective.recompute(conn, uid)
    conn.commit()


def add_l3(conn: Any, node_id: str, name: str, l1: str = L1, l2: str = L2, ver: str = "1.0") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) "
        "VALUES (?, ?, ?, 'L2', ?, ?, '');",
        (f"l2::{l1}::{l2}::{ver}", GS, ver, l1, l2),
    )
    conn.execute(
        "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) "
        "VALUES (?, ?, ?, 'L3', ?, ?, ?);",
        (node_id, GS, ver, l1, l2, name),
    )
    conn.commit()
