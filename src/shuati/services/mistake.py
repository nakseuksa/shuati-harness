"""外部新错题实时诊断与增量入库服务（DESIGN.md §5.3）。"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError

from shuati.config import Settings
from shuati.core import difficulty, effective
from shuati.core.image_store import archive_image, prepare_external_image
from shuati.core.knowledge_graph import find_canonical_concept_id, find_canonical_node_id
from shuati.core.text_norm import compute_plain_fingerprint, normalize_text
from shuati.core.topology import weave_single_question
from shuati.db import human_mirror, repository
from shuati.db.connection import CompileLock, PreconditionError
from shuati.llm.embeddings import EmbeddingClient
from shuati.llm.gateway import LLMGateway, LLMGatewayError
from shuati.llm.prompt_builder import (
    STAGE1_PROMPT_VERSION,
    build_diagnose_rerank_messages,
    build_external_diagnose_messages,
    build_qualitative_judge_messages,
    build_stage1_messages,
)
from shuati.llm.seam_loader import load_prompt_seams
from shuati.models.domain import AnswerPart
from shuati.models.schemas import (
    DiagnoseRerankBatchOutput,
    ExternalDiagnosisOutput,
    QualitativeJudgeOutput,
    Stage1Input,
    Stage1SecondaryOutput,
)
from shuati.pipeline.stage3_taxonomy import _best_fallback_l2, _load_active_taxonomy
from shuati.pipeline.unified_import import (
    _format_sanitized_anchors_text,
    _get_or_create_concept,
    _load_blacklist_and_traps,
)
from shuati.vectors.store import VectorStore
from shuati.verify.answer_parts import (
    normalize_answer_parts,
    normalize_choice_label,
    normalize_symbol_fill,
)
from shuati.verify.equivalence import compare_answer_parts
from shuati.verify.sympy_sandbox import run_verify_script, split_top_level

DIAGNOSE_PROMPT_VERSION = "diag_v3"
RECOMMENDATION_ENGINE_VERSION = "rec_v2"

_FIG_NOTE_STRIP_RE = re.compile(r"[（(]\s*(?:第\s*\d+\s*题|例\s*\d+)[^）)]*[）)]")
_NUM_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
_NON_CORE_CHAR_RE = re.compile(r"[\s\\_$，,。．.；;：:？?！!、\"'“”‘’（）()【】\[\]{}<>《》—\-~·]+")
_RARE_THINKING_METHODS: frozenset[str] = frozenset(
    {"极端原理", "抽屉原理", "最不利原则", "容斥原理", "奇偶分析", "不变量", "对称构造", "染色分析"}
)


def _normalize_num_token(tok: str) -> str:
    if "." in tok:
        stripped = tok.rstrip("0").rstrip(".")
        return stripped.lstrip("0") or "0"
    return tok.lstrip("0") or "0"


def _extract_cmp_signature(text: str) -> tuple[str, list[str], str]:
    """提取用于原题判重与纯换数孪生判定的三元签名：(去格式全文, 数值序列, 去数字骨架串)。"""
    cleaned = _FIG_NOTE_STRIP_RE.sub("", text or "")
    cleaned = cleaned.replace("\\_", "").replace("_", "")
    nums = [_normalize_num_token(m.group(0)) for m in _NUM_TOKEN_RE.finditer(cleaned)]
    core_str = _NON_CORE_CHAR_RE.sub("", cleaned).lower()
    no_digit_str = _NUM_TOKEN_RE.sub("#", core_str)
    return core_str, nums, no_digit_str


def _is_same_original_question(
    query_text: str,
    cand_text: str,
    *,
    match_type: str = "",
    surf_sim: float = 0.0,
) -> bool:
    """区分库内真原题重复（同数值+高文本重合）与换数孪生题（如 ch17_012 / ch17_048）。"""
    if match_type == "EXACT_FINGERPRINT":
        return True
    q_core, q_nums, _ = _extract_cmp_signature(query_text)
    c_core, c_nums, _ = _extract_cmp_signature(cand_text)
    if not q_core or not c_core:
        return False
    if (q_nums or c_nums) and q_nums != c_nums:
        return False
    if q_core == c_core:
        return True
    if min(len(q_core), len(c_core)) >= 18 and (c_core in q_core or q_core in c_core):
        return True
    seq_ratio = difflib.SequenceMatcher(None, q_core, c_core).ratio()
    return seq_ratio >= 0.88 or (surf_sim >= 0.94 and seq_ratio >= 0.80)


def _format_answer_parts_brief(parts: Sequence[AnswerPart]) -> str:
    segs: list[str] = []
    for p in parts:
        val = (p.answer or p.choice_label or p.canonical or p.sympy_expr or "").strip()
        if not val and p.items:
            val = ", ".join(
                str(it.get("expr") or it.get("canonical") or it.get("choice_label") or it.get("answer") or "")
                for it in p.items
                if isinstance(it, dict)
            )
        unit = (p.unit or "").strip()
        disp = f"{val}{unit}" if val and unit and not val.endswith(unit) else val
        segs.append(disp if p.label == "主问" and len(parts) == 1 else f"{p.label}: {disp}")
    return "；".join(s for s in segs if s) or "（无）"


def _scaffold_elo_ok(q_elo: int, ref_elo: int) -> bool:
    if ref_elo <= 1250:
        return q_elo <= 1185
    if ref_elo <= 1650:
        return q_elo <= min(1450, ref_elo - 120)
    return q_elo <= min(1620, ref_elo - 200)


def _isomorphic_elo_ok(q_elo: int, ref_elo: int, *, high_struct_match: bool = False) -> bool:
    if ref_elo <= 1250:
        return 1120 <= q_elo <= 1520
    if ref_elo < 1800:
        return (ref_elo - 180) <= q_elo <= (ref_elo + 240)
    lo = 1400 if high_struct_match else (ref_elo - 280)
    return lo <= q_elo <= (ref_elo + 260)


def _contrastive_elo_ok(
    q_elo: int,
    ref_elo: int,
    *,
    same_l2: bool = False,
    cand_c_score: int = 2,
    has_shared_trap: bool = False,
) -> bool:
    lo = 1120 if ref_elo <= 1450 else (ref_elo - 320)
    if ref_elo < 1600 and cand_c_score >= 3 and q_elo >= 1850:
        return False
    if same_l2 and has_shared_trap and cand_c_score <= 2:
        hi = max(1550, min(1800, ref_elo + 560))
    else:
        hi = max(1450, ref_elo + 300)
    return lo <= q_elo <= hi


def _load_servable_structural_catalog(
    conn: sqlite3.Connection,
    grade_subject: str,
    servable_uids: set[str],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, set[str]],
    dict[str, tuple[str, str, str]],
]:
    """批量加载可服务题的 L2/L3/概念特征、CLONE 连通分量、先修 L2 映射与活跃标准 L3 表。"""
    if not servable_uids:
        return {}, {}, {}, {}

    uid_list = sorted(servable_uids)
    ph = ",".join("?" for _ in uid_list)

    # 1. 活跃 L3 节点元数据与单跳并查集映射
    node_canon: dict[str, str] = {}
    active_l3_meta: dict[str, tuple[str, str, str]] = {}
    l2_node_to_pair: dict[str, str] = {}
    for nr in conn.execute(
        """
        SELECT node_id, redirect_to_id, node_level, l1_module, l2_entity, l3_operator
        FROM knowledge_nodes
        WHERE grade_subject = ?;
        """,
        (grade_subject,),
    ).fetchall():
        nid = str(nr["node_id"])
        canon = str(nr["redirect_to_id"]) if nr["redirect_to_id"] else nid
        node_canon[nid] = canon
        l1_s = str(nr["l1_module"] or "").strip()
        l2_s = str(nr["l2_entity"] or "").strip()
        l3_s = str(nr["l3_operator"] or "").strip()
        if nr["node_level"] == "L2" and nr["redirect_to_id"] is None and l1_s and l2_s:
            l2_node_to_pair[nid] = f"{l1_s}/{l2_s}"
        elif nr["node_level"] == "L3" and nr["redirect_to_id"] is None:
            active_l3_meta[nid] = (l1_s, l2_s, l3_s)

    # 2. 先修 L2 边：target_l2_pair -> set[prereq_source_l2_pair]
    prereq_of_l2: dict[str, set[str]] = {}
    for er in conn.execute(
        """
        SELECT source_node_id, target_node_id
        FROM knowledge_edges
        WHERE grade_subject = ? AND edge_type = 'PREREQUISITE';
        """,
        (grade_subject,),
    ).fetchall():
        s_pair = l2_node_to_pair.get(str(er["source_node_id"]))
        t_pair = l2_node_to_pair.get(str(er["target_node_id"]))
        if s_pair and t_pair and s_pair != t_pair:
            prereq_of_l2.setdefault(t_pair, set()).add(s_pair)

    struct_by_uid: dict[str, dict[str, Any]] = {
        u: {
            "l2_pairs": set(),
            "l2_names": set(),
            "l3_nids": set(),
            "l3_labels": [],
            "methods": set(),
            "motifs": set(),
            "traps": set(),
            "step_count": 0,
        }
        for u in uid_list
    }

    # 3. 步骤与 L2/L3 挂载
    for sr in conn.execute(
        f"""
        SELECT question_uid, branch_id, m_l1, m_l2, m_knowledge_node_id
        FROM question_steps
        WHERE question_uid IN ({ph});
        """,
        uid_list,
    ).fetchall():
        u = str(sr["question_uid"])
        entry = struct_by_uid[u]
        if str(sr["branch_id"]) == "branch_1":
            entry["step_count"] = int(entry["step_count"]) + 1
        ml1 = str(sr["m_l1"] or "").strip()
        ml2 = str(sr["m_l2"] or "").strip()
        if ml1 and ml2:
            entry["l2_pairs"].add(f"{ml1}/{ml2}")
            entry["l2_names"].add(ml2)
        raw_nid = sr["m_knowledge_node_id"]
        if raw_nid:
            cn = node_canon.get(str(raw_nid), str(raw_nid))
            entry["l3_nids"].add(cn)
            if cn in active_l3_meta:
                nl1, nl2, nl3 = active_l3_meta[cn]
                if nl1 and nl2:
                    entry["l2_pairs"].add(f"{nl1}/{nl2}")
                    entry["l2_names"].add(nl2)
                lbl = f"{nl2}:{nl3}" if nl2 else nl3
                if lbl and lbl not in entry["l3_labels"]:
                    entry["l3_labels"].append(lbl)

    # 4. 全局概念（METHOD / MOTIF / 非笔误 TRAP）
    for cr in conn.execute(
        f"""
        SELECT qc.question_uid, qc.kind, COALESCE(m.name, c.name) AS cname
        FROM question_concepts qc
        JOIN concepts c ON c.concept_id = qc.concept_id
        LEFT JOIN concepts m ON m.concept_id = c.redirect_to_id
        WHERE qc.question_uid IN ({ph});
        """,
        uid_list,
    ).fetchall():
        u = str(cr["question_uid"])
        cname = str(cr["cname"] or "").strip()
        if not cname:
            continue
        k = str(cr["kind"])
        if k == "METHOD":
            struct_by_uid[u]["methods"].add(cname)
        elif k == "MOTIF":
            struct_by_uid[u]["motifs"].add(cname)
        elif k == "TRAP" and cname != "TRAP_ARITHMETIC_SLIP":
            struct_by_uid[u]["traps"].add(cname)

    # 5. CLONE 连通分量（用于推荐栏内去重，防止连推同母题）
    parent: dict[str, str] = {u: u for u in uid_list}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for rel in conn.execute(
        f"""
        SELECT source_question_uid, target_question_uid
        FROM question_relations
        WHERE relation_type = 'CLONE'
          AND source_question_uid IN ({ph})
          AND target_question_uid IN ({ph});
        """,
        [*uid_list, *uid_list],
    ).fetchall():
        ra = _find(str(rel["source_question_uid"]))
        rb = _find(str(rel["target_question_uid"]))
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    clone_root = {u: _find(u) for u in uid_list}
    return struct_by_uid, clone_root, prereq_of_l2, active_l3_meta


def _resolve_diagnose_prompt_version(
    config_dir: Path | str = Path("config"),
    grade_subject: str = "小学奥数",
    prompt_profile: str | None = None,
) -> str:
    cfg = Path(config_dir)
    if (cfg / "prompts" / "default.yaml").is_file() or (
        prompt_profile is not None and prompt_profile.strip()
    ):
        bundle = load_prompt_seams(
            config_dir=cfg,
            grade_subject=grade_subject,
            profile=prompt_profile,
        )
        return bundle.prompt_versions.get("diagnose", DIAGNOSE_PROMPT_VERSION)
    return DIAGNOSE_PROMPT_VERSION

_DRAFT_TTL_SECONDS = 7 * 86400


def _atomic_write_text(target: Path, content: str) -> None:
    """通过同目录 pid+uuid 临时文件 + os.replace 原子落盘，失败时清理临时文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _prune_expired_drafts(drafts_dir: Path) -> None:
    if not drafts_dir.exists():
        return
    now = time.time()
    for p in drafts_dir.glob("drf_*.json"):
        try:
            if now - p.stat().st_mtime > _DRAFT_TTL_SECONDS:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _build_taxonomy_enum_summary(conn: sqlite3.Connection, grade_subject: str) -> str:
    gs_row = conn.execute(
        "SELECT taxonomy_version FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    tax_ver = str(gs_row["taxonomy_version"] or "").strip() if gs_row else ""
    if tax_ver:
        rows = conn.execute(
            """
            SELECT DISTINCT l1_module, l2_entity
            FROM knowledge_nodes
            WHERE grade_subject = ? AND taxonomy_version = ? AND redirect_to_id IS NULL
            ORDER BY l1_module, l2_entity;
            """,
            (grade_subject, tax_ver),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT l1_module, l2_entity
            FROM knowledge_nodes
            WHERE grade_subject = ? AND redirect_to_id IS NULL
            ORDER BY l1_module, l2_entity;
            """,
            (grade_subject,),
        ).fetchall()
    if not rows:
        return "（当前学段暂无已初始化标准 L1/L2 词表，请使用合理学科模块名）"
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(str(r["l1_module"]), []).append(str(r["l2_entity"]))
    return "\n".join(f"- {l1}: {', '.join(l2s)}" for l1, l2s in grouped.items())


def _has_wrapping_delimiters(text: str, left: str, right: str) -> bool:
    if len(text) < 2 or text[0] != left or text[-1] != right:
        return False
    depth = 0
    for idx, ch in enumerate(text):
        if ch == left:
            depth += 1
        elif ch == right:
            depth -= 1
            if depth == 0 and idx < len(text) - 1:
                return False
            if depth < 0:
                return False
    return depth == 0


def _split_collection_tokens(val: str, *, allow_unwrap_parens: bool) -> list[str]:
    inner = val.strip()
    if _has_wrapping_delimiters(inner, "{", "}"):
        inner = inner[1:-1].strip()
    elif allow_unwrap_parens and _has_wrapping_delimiters(inner, "(", ")"):
        inner = inner[1:-1].strip()
    tokens = split_top_level(inner.replace("，", ",").replace("、", ","), ",")
    return tokens or [val.strip()]


_LABELED_EQ_RE = re.compile(r"^\s*([^=＝<>＞＜!！+\-*/×÷:：]+?)\s*[=＝]\s*(.+)$")


def _split_labeled_map_token(
    tok: str,
    *,
    answer_type: str,
    ref_label_set: set[str],
    require_known_key_for_eq: bool = False,
) -> tuple[str | None, str]:
    """解析 LABELED_MAP 单项：优先 `：`/`:`（排除键侧含 `=`/`＝` 并保 RATIO 数值比值守卫），再谨慎按 `=` 拆分。"""
    if "：" in tok:
        k_cand, v_cand = tok.split("：", 1)
        k_clean = k_cand.strip()
        if k_clean and "=" not in k_clean and "＝" not in k_clean:
            return k_clean, v_cand.strip()
    if ":" in tok:
        k_cand, v_cand = tok.split(":", 1)
        k_clean = k_cand.strip()
        if k_clean and "=" not in k_clean and "＝" not in k_clean:
            if answer_type != "RATIO" or (
                re.sub(r"\s+", "", k_clean) in ref_label_set
                or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", k_clean)
            ):
                return k_clean, v_cand.strip()
    m_eq = _LABELED_EQ_RE.match(tok)
    if m_eq:
        k_clean = m_eq.group(1).strip()
        v_clean = m_eq.group(2).strip()
        if k_clean and v_clean:
            norm_k = re.sub(r"\s+", "", k_clean)
            must_match_ref = require_known_key_for_eq or (
                bool(ref_label_set) and answer_type in ("EXPRESSION", "SET_OR_INTERVAL")
            )
            if not must_match_ref or norm_k in ref_label_set:
                return k_clean, v_clean
    return None, tok.strip()


def _extract_ref_map_schema(ref: AnswerPart) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for it in ref.items or []:
        if isinstance(it, dict):
            rk = it.get("label") or it.get("key") or it.get("name") or it.get("entity")
            rtype = str(it.get("item_type") or it.get("answer_type") or "NUMERIC").upper()
            if rk is not None and str(rk).strip():
                entries.append((str(rk).strip(), rtype))
            elif len(it) == 1:
                k0 = next(iter(it.keys()))
                entries.append((str(k0).strip(), rtype))
    return entries


def _build_known_part(ref: AnswerPart, val: str) -> AnswerPart:
    val = val.strip()
    if ref.answer_type == "CHOICE":
        norm_ch = normalize_choice_label(val) or "INVALID"
        if ref.collection_mode == "SINGLE":
            return AnswerPart(
                label=ref.label,
                answer_type="CHOICE",
                verify_mode="EXACT",
                collection_mode="SINGLE",
                answer=val,
                choice_label=norm_ch,
            )
        tokens = _split_collection_tokens(val, allow_unwrap_parens=True)
        return AnswerPart(
            label=ref.label,
            answer_type="CHOICE",
            verify_mode="EXACT",
            collection_mode=ref.collection_mode,
            answer=val,
            choice_label=norm_ch,
            items=[{"choice_label": normalize_choice_label(t) or "INVALID"} for t in tokens],
        )

    if ref.answer_type == "SYMBOL_FILL":
        if ref.collection_mode == "SINGLE":
            return AnswerPart(
                label=ref.label,
                answer_type="SYMBOL_FILL",
                verify_mode="EXACT",
                collection_mode="SINGLE",
                answer=val,
                canonical=normalize_symbol_fill(val) or "INVALID",
            )
        if ref.collection_mode == "LABELED_MAP":
            ref_entries = _extract_ref_map_schema(ref)
            ref_labels = [lbl for lbl, _ in ref_entries]
            ref_label_set = {re.sub(r"\s+", "", lbl) for lbl in ref_labels}
            raw_tokens = _split_collection_tokens(val, allow_unwrap_parens=True)
            map_items: list[dict[str, str]] = []
            for idx, tok in enumerate(raw_tokens):
                key_str, val_str = _split_labeled_map_token(
                    tok,
                    answer_type=ref.answer_type,
                    ref_label_set=ref_label_set,
                    require_known_key_for_eq=True,
                )
                if not key_str:
                    key_str = ref_labels[idx] if idx < len(ref_labels) else f"k{idx + 1}"
                map_items.append(
                    {
                        "label": key_str,
                        "item_type": "SYMBOL_FILL",
                        "canonical": normalize_symbol_fill(val_str) or "INVALID",
                    }
                )
            norm_ref_keys = [re.sub(r"\s+", "", rk) for rk in ref_labels]
            if (
                ref_labels
                and len(ref_labels) == len(map_items)
                and len(set(norm_ref_keys)) == len(ref_labels)
            ):
                by_key = {re.sub(r"\s+", "", it["label"]): it for it in map_items}
                if len(by_key) == len(map_items) and set(by_key.keys()) == set(norm_ref_keys):
                    map_items = [by_key[rk] for rk in norm_ref_keys]
            return AnswerPart(
                label=ref.label,
                answer_type="SYMBOL_FILL",
                verify_mode="EXACT",
                collection_mode="LABELED_MAP",
                answer=val,
                items=map_items,
            )
        tokens = _split_collection_tokens(val, allow_unwrap_parens=True)
        return AnswerPart(
            label=ref.label,
            answer_type="SYMBOL_FILL",
            verify_mode="EXACT",
            collection_mode=ref.collection_mode,
            answer=val,
            items=[
                {"item_type": "SYMBOL_FILL", "canonical": normalize_symbol_fill(t) or "INVALID"}
                for t in tokens
            ],
        )

    if ref.verify_mode == "SYMBOLIC":
        if ref.collection_mode == "SINGLE":
            return AnswerPart(
                label=ref.label,
                answer_type=ref.answer_type,
                verify_mode="SYMBOLIC",
                collection_mode="SINGLE",
                answer=val,
                sympy_expr=val,
            )
        if ref.collection_mode == "LABELED_MAP":
            ref_entries = _extract_ref_map_schema(ref)
            ref_label_set = {re.sub(r"\s+", "", lbl) for lbl, _ in ref_entries}
            ref_type_by_label = {re.sub(r"\s+", "", lbl): t for lbl, t in ref_entries}
            raw_tokens = _split_collection_tokens(val, allow_unwrap_parens=False)
            sym_map_items: list[dict[str, str]] = []
            for idx, tok in enumerate(raw_tokens):
                key_str, val_str = _split_labeled_map_token(
                    tok,
                    answer_type=ref.answer_type,
                    ref_label_set=ref_label_set,
                    require_known_key_for_eq=False,
                )
                if not key_str:
                    key_str = ref_entries[idx][0] if idx < len(ref_entries) else f"k{idx + 1}"
                norm_k = re.sub(r"\s+", "", key_str)
                expected_itype = ref_type_by_label.get(
                    norm_k, ref_entries[idx][1] if idx < len(ref_entries) else "NUMERIC"
                )
                if expected_itype == "SYMBOL_FILL":
                    sym_map_items.append(
                        {
                            "label": key_str,
                            "item_type": "SYMBOL_FILL",
                            "canonical": normalize_symbol_fill(val_str) or "INVALID",
                        }
                    )
                else:
                    sym_map_items.append({"label": key_str, "expr": val_str})
            return AnswerPart(
                label=ref.label,
                answer_type=ref.answer_type,
                verify_mode="SYMBOLIC",
                collection_mode="LABELED_MAP",
                answer=val,
                items=sym_map_items,
            )

        allow_parens = ref.answer_type != "SET_OR_INTERVAL"
        tokens = _split_collection_tokens(val, allow_unwrap_parens=allow_parens)
        seq_items: list[dict[str, str]] = []
        ref_items_list = ref.items or []
        for idx, tok in enumerate(tokens):
            ref_it = ref_items_list[idx] if ref.collection_mode == "ORDERED_TUPLE" and idx < len(ref_items_list) else None
            if (
                isinstance(ref_it, dict)
                and str(ref_it.get("item_type") or ref_it.get("answer_type") or "").upper() == "SYMBOL_FILL"
            ):
                seq_items.append(
                    {
                        "item_type": "SYMBOL_FILL",
                        "canonical": normalize_symbol_fill(tok) or "INVALID",
                    }
                )
            else:
                seq_items.append({"expr": tok})
        return AnswerPart(
            label=ref.label,
            answer_type=ref.answer_type,
            verify_mode="SYMBOLIC",
            collection_mode=ref.collection_mode,
            answer=val,
            items=seq_items,
        )

    if ref.collection_mode == "SINGLE":
        return AnswerPart(
            label=ref.label,
            answer_type=ref.answer_type,
            verify_mode="QUALITATIVE",
            collection_mode="SINGLE",
            answer=val,
        )
    tokens = _split_collection_tokens(val, allow_unwrap_parens=True)
    return AnswerPart(
        label=ref.label,
        answer_type=ref.answer_type,
        verify_mode="QUALITATIVE",
        collection_mode=ref.collection_mode,
        answer=val,
        items=[{"answer": t} for t in tokens],
    )


def _parse_single_known_answer(raw_answer: str, reference_parts: list[AnswerPart]) -> list[AnswerPart]:
    """将命令行 --answer 解析为与 reference_parts 对齐的 AnswerPart 列表用于一致性比对。"""
    if not reference_parts:
        return []
    cleaned = raw_answer.strip()
    if len(reference_parts) == 1:
        return normalize_answer_parts([_build_known_part(reference_parts[0], cleaned)])

    # 多小问顶层分号切分
    segs = [s.strip() for s in split_top_level(cleaned.replace("；", ";"), ";") if s.strip()]
    out: list[AnswerPart] = []
    for idx, ref in enumerate(reference_parts):
        val = segs[idx] if idx < len(segs) else cleaned
        out.append(_build_known_part(ref, val))
    return normalize_answer_parts(out)


def _hash_file_bytes(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_diagnose_content_hash(
    grade_subject: str,
    norm_text: str,
    image_paths: Sequence[Path],
) -> str:
    """基于学段、规范化题干与各原图内容哈希计算稳定的诊断缓存键（不依赖临时文件名）。"""
    img_hashes = [_hash_file_bytes(p) for p in image_paths]
    payload = {
        "grade_subject": grade_subject,
        "text": norm_text,
        "img_hashes": img_hashes,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def find_duplicate_candidates(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    norm_text: str,
    image_count: int,
    surf_hits: Sequence[tuple[str, float]],
    servable_map: dict[str, dict[str, Any]],
    surface_sim_threshold: float = 0.85,
    max_candidates: int = 3,
) -> list[dict[str, Any]]:
    """检索库内与当前外部错题精确重复（plain_fingerprint）或表皮高相似的原题候选。"""
    candidates: list[dict[str, Any]] = []
    seen_uids: set[str] = set()

    plain_fp = compute_plain_fingerprint(norm_text, image_count)
    fp_rows = conn.execute(
        """
        SELECT q.question_uid, r.source_id, q.eff_question_text, q.eff_base_elo,
               q.eff_tier, q.eff_primary_l1_module
        FROM raw_questions r
        JOIN questions q ON q.question_uid = r.question_uid
        WHERE q.grade_subject = ? AND q.is_retired = 0 AND q.eff_is_excluded = 0
          AND r.plain_fingerprint = ?
        LIMIT ?;
        """,
        (grade_subject, plain_fp, max_candidates),
    ).fetchall()
    for row in fp_rows:
        uid = str(row["question_uid"])
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        q_txt = str(row["eff_question_text"] or "").strip()
        candidates.append(
            {
                "question_uid": uid,
                "source_id": str(row["source_id"] or ""),
                "base_elo": int(row["eff_base_elo"] or 1500),
                "tier": row["eff_tier"],
                "primary_l1_module": row["eff_primary_l1_module"],
                "similarity": 1.0,
                "match_type": "EXACT_FINGERPRINT",
                "question_preview": q_txt[:120] + ("..." if len(q_txt) > 120 else ""),
            }
        )

    for uid, sim in surf_hits:
        if len(candidates) >= max_candidates:
            break
        if uid in seen_uids or float(sim) < surface_sim_threshold:
            continue
        meta = servable_map.get(uid)
        if not meta:
            continue
        seen_uids.add(uid)
        q_txt = str(meta["eff_question_text"] or "").strip()
        candidates.append(
            {
                "question_uid": uid,
                "source_id": str(meta["source_id"] or ""),
                "base_elo": int(meta["eff_base_elo"] or 1500),
                "tier": meta["eff_tier"],
                "primary_l1_module": meta["eff_primary_l1_module"],
                "similarity": round(float(sim), 4),
                "match_type": "HIGH_SURFACE_SIM",
                "question_preview": q_txt[:120] + ("..." if len(q_txt) > 120 else ""),
            }
        )

    return candidates


def _build_anchors_prompt_text(
    conn: sqlite3.Connection,
    grade_subject: str,
    config_dir: Path | str = Path("config"),
) -> str:
    """加载该学段 Stage 1.5 黄金难度锚点与黑名单/陷阱枚举，用于对齐实时诊断标尺。"""
    cfg_dir = Path(config_dir)
    blacklist, seed_traps = _load_blacklist_and_traps(cfg_dir)
    gs_row = conn.execute(
        "SELECT slug, anchors_file, anchors_ready FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        return ""
    candidates: list[Path] = []
    if gs_row["anchors_file"]:
        raw_p = Path(str(gs_row["anchors_file"]))
        candidates.append(raw_p)
        candidates.append(cfg_dir / "anchors" / raw_p.name)
    slug = str(gs_row["slug"] or "")
    if slug:
        candidates.append(cfg_dir / "anchors" / f"anchors_{slug}.yaml")
    for cand in candidates:
        if cand.exists():
            return _format_sanitized_anchors_text(conn, cand, blacklist, seed_traps)
    return ""


async def build_diagnose_recommendations_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str,
    scope_slug: str,
    norm_text: str,
    image_count: int,
    diag: ExternalDiagnosisOutput,
    diag_parts: list[AnswerPart],
    diff_res: difficulty.DifficultyResult,
    vectors_dir: Path | str = Path("data/vectors"),
    use_mock: bool = False,
) -> dict[str, Any]:
    """执行前置查重排除、库内验真答案/Elo 锚定、Stage 4 边复用、多特征召回与轻量 judge_llm 二级精裁。"""
    warnings: list[str] = []
    servable_rows = conn.execute(
        """
        SELECT q.question_uid, r.source_id, q.eff_question_text, q.eff_options_json, q.has_images,
               q.eff_base_elo, q.eff_tier, q.eff_tier_rank, q.eff_c_score,
               q.eff_primary_l1_module, q.eff_abstract_skeleton,
               q.eff_verification_status, q.eff_final_answers_json
        FROM questions q
        JOIN raw_questions r ON r.question_uid = q.question_uid
        WHERE q.grade_subject = ? AND q.is_retired = 0 AND q.eff_is_excluded = 0 AND q.eff_is_servable = 1;
        """,
        (grade_subject,),
    ).fetchall()
    servable_map = {str(r["question_uid"]): dict(r) for r in servable_rows}
    servable_uids = set(servable_map.keys())

    # 1. 加载全量可服务题结构目录、CLONE 分量与本地 1 vs N 精确向量余弦
    struct_by_uid, clone_root, prereq_of_l2, active_l3_meta = _load_servable_structural_catalog(
        conn, grade_subject, servable_uids
    )
    digit_fp_by_uid: dict[str, str] = {
        u: _extract_cmp_signature(str(meta.get("eff_question_text") or ""))[2]
        for u, meta in servable_map.items()
    }

    store = VectorStore(vectors_dir)
    surf_sim_map: dict[str, float] = {}
    skel_sim_map: dict[str, float] = {}
    surf_hits: list[tuple[str, float]] = []
    skel_hits: list[tuple[str, float]] = []
    l3_vec_hits: list[list[tuple[str, float]]] = []

    surf_text = (norm_text + ("\n" + diag.visual_facts if diag.visual_facts else "")).strip()
    skel_text = diag.abstract_skeleton.strip() or norm_text

    has_surf_npz = store.npz_path("surface", scope_slug).exists()
    has_skel_npz = store.npz_path("skeleton", scope_slug).exists()
    has_l3_npz = store.npz_path("l3_nodes", scope_slug).exists()

    # 归一诊断词表标签（L1 / L2）
    tax_lookup = None
    try:
        _, _, _, tax_lookup = _load_active_taxonomy(conn, grade_subject)
    except PreconditionError:
        tax_lookup = None

    norm_primary_l1 = diag.primary_l1_module.strip()
    if tax_lookup is not None:
        r_l1 = tax_lookup.resolve_l1(norm_primary_l1)
        if r_l1:
            norm_primary_l1 = r_l1

    all_l1_set: set[str] = {norm_primary_l1} if norm_primary_l1 else set()
    for sec_l1 in diag.secondary_l1_modules:
        s_clean = sec_l1.strip()
        if tax_lookup is not None and s_clean:
            s_clean = tax_lookup.resolve_l1(s_clean) or s_clean
        if s_clean:
            all_l1_set.add(s_clean)

    primary_l2_pairs: set[str] = set()
    step_l2_pairs: set[str] = set()
    step_l3_queries: list[str] = []

    for m_l2 in diag.matched_l2:
        m_clean = m_l2.strip()
        if not m_clean:
            continue
        resolved = False
        if tax_lookup is not None:
            for cand_l1 in [norm_primary_l1, *sorted(all_l1_set)]:
                if cand_l1:
                    hit = tax_lookup.resolve_l2(cand_l1, m_clean)
                    if hit:
                        primary_l2_pairs.add(f"{hit[0]}/{hit[1]}")
                        all_l1_set.add(hit[0])
                        resolved = True
                        break
        if not resolved and norm_primary_l1:
            primary_l2_pairs.add(f"{norm_primary_l1}/{m_clean}")

    for st in diag.steps:
        sl1 = st.raw_l1.strip() or norm_primary_l1
        sl2 = st.raw_l2.strip()
        sl3 = st.raw_l3.strip()
        if tax_lookup is not None:
            hit = tax_lookup.resolve_l2(sl1, sl2)
            if hit:
                sl1, sl2 = hit
            else:
                rl1 = tax_lookup.resolve_l1(sl1)
                if rl1:
                    sl1 = rl1
        if sl1 and sl2:
            pair_lbl = f"{sl1}/{sl2}"
            step_l2_pairs.add(pair_lbl)
            if sl1 == norm_primary_l1 or not primary_l2_pairs:
                primary_l2_pairs.add(pair_lbl)
        if sl1 and sl2 and sl3:
            step_l3_queries.append(f"{sl1}/{sl2}/{sl3}")

    if not primary_l2_pairs:
        primary_l2_pairs = set(step_l2_pairs)
    step_l2_pairs |= primary_l2_pairs

    prereq_l2_pairs: set[str] = set()
    for lp in step_l2_pairs:
        prereq_l2_pairs |= prereq_of_l2.get(lp, set())

    if servable_uids and (has_surf_npz or has_skel_npz):
        texts_to_embed = [surf_text, skel_text]
        if has_l3_npz and step_l3_queries:
            texts_to_embed.extend(step_l3_queries)
        emb_client = EmbeddingClient(settings, conn=None, use_mock=use_mock)
        try:
            q_vecs = await emb_client.embed_texts(texts_to_embed, stage="DIAGNOSE_EMBED")
        finally:
            await emb_client.aclose()

        if has_surf_npz:
            s_ids, s_mat = store.load_vectors("surface", scope_slug=scope_slug)
            if len(s_ids) > 0:
                s_scores = s_mat @ np.asarray(q_vecs[0], dtype=np.float32)
                for uid_i, sc_i in zip(s_ids, s_scores.tolist(), strict=False):
                    if uid_i in servable_uids:
                        surf_sim_map[uid_i] = float(sc_i)
                surf_hits = sorted(surf_sim_map.items(), key=lambda kv: (-kv[1], kv[0]))[:50]

        if has_skel_npz:
            k_ids, k_mat = store.load_vectors("skeleton", scope_slug=scope_slug)
            if len(k_ids) > 0:
                k_scores = k_mat @ np.asarray(q_vecs[1], dtype=np.float32)
                for uid_i, sc_i in zip(k_ids, k_scores.tolist(), strict=False):
                    if uid_i in servable_uids:
                        skel_sim_map[uid_i] = float(sc_i)
                skel_hits = sorted(skel_sim_map.items(), key=lambda kv: (-kv[1], kv[0]))[:60]

        if has_l3_npz and step_l3_queries:
            for idx_q in range(len(step_l3_queries)):
                l3_vec_hits.append(
                    store.query_top_k("l3_nodes", q_vecs[2 + idx_q], scope_slug=scope_slug, top_k=3)
                )

    # 2. 前置查重排除 + 高置信库内原题锚定（答案核对 + 借用 eff_base_elo + Stage 4 边提取）
    duplicate_candidates = find_duplicate_candidates(
        conn,
        grade_subject=grade_subject,
        norm_text=norm_text,
        image_count=image_count,
        surf_hits=surf_hits,
        servable_map=servable_map,
        surface_sim_threshold=0.85,
        max_candidates=5,
    )

    excluded_uids: set[str] = set()
    matched_orig_uid: str | None = None
    for d in duplicate_candidates:
        duid = str(d["question_uid"])
        meta = servable_map.get(duid)
        cand_txt = str(meta["eff_question_text"] if meta else d.get("question_preview") or "")
        if _is_same_original_question(
            norm_text,
            cand_txt,
            match_type=str(d.get("match_type") or ""),
            surf_sim=float(d.get("similarity") or 0.0),
        ):
            excluded_uids.add(duid)
            if matched_orig_uid is None and duid in servable_map:
                matched_orig_uid = duid

    # 补充扫描表皮前 10 近邻（无向量库时按数值签名 O(N) 轻量兜底），防止遗漏带题号后缀的真原题
    if surf_hits:
        for suid, ssim in surf_hits[:10]:
            if suid in excluded_uids:
                continue
            cand_txt = str(servable_map[suid].get("eff_question_text") or "")
            if _is_same_original_question(norm_text, cand_txt, surf_sim=ssim):
                excluded_uids.add(suid)
                if matched_orig_uid is None:
                    matched_orig_uid = suid
    else:
        _, q_nums_fast, _ = _extract_cmp_signature(norm_text)
        for suid, meta in servable_map.items():
            if suid in excluded_uids:
                continue
            cand_txt = str(meta.get("eff_question_text") or "")
            _, c_nums_fast, _ = _extract_cmp_signature(cand_txt)
            if (q_nums_fast or c_nums_fast) and q_nums_fast != c_nums_fast:
                continue
            if _is_same_original_question(norm_text, cand_txt):
                excluded_uids.add(suid)
                if matched_orig_uid is None:
                    matched_orig_uid = suid
                if not any(d["question_uid"] == suid for d in duplicate_candidates):
                    duplicate_candidates.append(
                        {
                            "question_uid": suid,
                            "source_id": str(meta.get("source_id") or ""),
                            "base_elo": int(meta.get("eff_base_elo") or 1500),
                            "tier": meta.get("eff_tier"),
                            "primary_l1_module": meta.get("eff_primary_l1_module"),
                            "similarity": 0.99,
                            "match_type": "HIGH_SURFACE_SIM",
                            "question_preview": cand_txt[:120] + ("..." if len(cand_txt) > 120 else ""),
                        }
                    )

    ref_elo = int(diff_res.base_elo)
    ref_tier = str(diff_res.tier)
    ref_tier_rank = int(diff_res.tier_rank)
    elo_calibration_source = "SINGLE_PASS"
    matched_db_question: dict[str, Any] | None = None

    if matched_orig_uid is not None and matched_orig_uid in servable_map:
        orig_meta = servable_map[matched_orig_uid]
        orig_sid = str(orig_meta.get("source_id") or matched_orig_uid)
        orig_ver = str(orig_meta.get("eff_verification_status") or "")
        orig_elo = int(orig_meta.get("eff_base_elo") or ref_elo)
        orig_tier = str(orig_meta.get("eff_tier") or ref_tier)
        orig_tier_rank = int(orig_meta.get("eff_tier_rank") or ref_tier_rank)

        answer_conflict = False
        db_ans_display = ""
        if orig_ver in ("VERIFIED_CONSENSUS", "HUMAN_VERIFIED"):
            ref_elo = orig_elo
            ref_tier = orig_tier
            ref_tier_rank = orig_tier_rank
            elo_calibration_source = "DB_VERIFIED_MATCH"
            try:
                raw_db_ans = json.loads(orig_meta.get("eff_final_answers_json") or "[]")
                if isinstance(raw_db_ans, list) and raw_db_ans:
                    db_parts = normalize_answer_parts([AnswerPart.model_validate(x) for x in raw_db_ans])
                    db_ans_display = _format_answer_parts_brief(db_parts)
                    eq_db = await compare_answer_parts(
                        diag_parts,
                        db_parts,
                        grade_subject=grade_subject,
                        allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
                        sympy_result=None,
                        timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                        memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
                    )
                    answer_conflict = not bool(eq_db.all_dual_equal)
            except Exception:
                answer_conflict = False

        if answer_conflict:
            warnings.append(
                f"DUPLICATE_ANSWER_CONFLICT: 单路草稿参考答案与库内已验真原题 {orig_sid} ({orig_ver}) 不一致"
                f"（库内标准答案：{db_ans_display}），单路读图或建模可能存在偏差，请以库内验真答案为准。"
            )

        matched_db_question = {
            "question_uid": matched_orig_uid,
            "source_id": orig_sid,
            "verification_status": orig_ver,
            "db_base_elo": orig_elo,
            "db_tier": orig_tier,
            "answer_conflict": answer_conflict,
            "db_answer_display": db_ans_display if answer_conflict else "",
        }
    else:
        # 库外新题：同 L2 高相似骨架近邻中位数锚定，抑制单路极端 Elo 漂移
        close_neighbor_elos = [
            int(servable_map[u]["eff_base_elo"] or 1500)
            for u, ksim in skel_hits[:10]
            if u not in excluded_uids
            and ksim >= 0.88
            and bool(struct_by_uid.get(u, {}).get("l2_pairs", set()) & primary_l2_pairs)
        ]
        if len(close_neighbor_elos) >= 3:
            sorted_elos = sorted(close_neighbor_elos[:5])
            med_elo = sorted_elos[len(sorted_elos) // 2]
            if abs(ref_elo - med_elo) >= 350:
                ref_elo = int(round(0.5 * ref_elo + 0.5 * med_elo))
                elo_calibration_source = "KNN_ANCHOR_BLEND"

    # 3. 映射标准 L3 算子并统计题库内细分模型真实库存数
    matched_l3_nids: set[str] = set()
    if matched_orig_uid and matched_orig_uid in struct_by_uid:
        matched_l3_nids |= set(struct_by_uid[matched_orig_uid]["l3_nids"])

    for st in diag.steps:
        raw_op = st.raw_l3.strip()
        if not raw_op:
            continue
        for nid, (nl1, nl2, nl3) in active_l3_meta.items():
            if f"{nl1}/{nl2}" in step_l2_pairs and (raw_op == nl3 or raw_op in nl3 or nl3 in raw_op):
                matched_l3_nids.add(nid)
    for hit_group in l3_vec_hits:
        for nid, lsim in hit_group:
            if lsim >= 0.82 and nid in active_l3_meta:
                nl1, nl2, _ = active_l3_meta[nid]
                if f"{nl1}/{nl2}" in step_l2_pairs:
                    matched_l3_nids.add(nid)
                    break

    diag_motifs = {m.strip() for m in diag.classic_motifs if m.strip()}
    diag_methods = {m.strip() for m in diag.thinking_methods if m.strip()}
    if matched_orig_uid and matched_orig_uid in struct_by_uid:
        diag_motifs |= set(struct_by_uid[matched_orig_uid]["motifs"])
        diag_methods |= set(struct_by_uid[matched_orig_uid]["methods"])

    # 计算细分模型匹配题库存（区分跨模块复合题、专属 MOTIF/L3 子模型与常规 L2）
    non_tool_l1s = {
        x.split("/", 1)[0]
        for x in step_l2_pairs
        if "/" in x and x.split("/", 1)[0] not in ("计算与代数巧算", "典型应用题")
    }
    if len(non_tool_l1s) >= 2 and norm_primary_l1:
        sec_l2s = {x for x in step_l2_pairs if not x.startswith(f"{norm_primary_l1}/") and x.split("/", 1)[0] in non_tool_l1s}
        prim_l2s = {x for x in step_l2_pairs if x.startswith(f"{norm_primary_l1}/")}
        submodel_uids = {
            u
            for u, st_meta in struct_by_uid.items()
            if (st_meta["l2_pairs"] & sec_l2s) and (st_meta["l2_pairs"] & prim_l2s)
        }
        sec_short = "、".join(sorted({x.split("/", 1)[1] for x in sec_l2s}))
        prim_short = "、".join(sorted({x.split("/", 1)[1] for x in prim_l2s}))
        submodel_label = f"{sec_short} × {prim_short}"
    elif matched_l3_nids:
        # 取诊断命中的最具辨识度（最小 df）标准 L3 算子衡量细分模型题量
        l3_doc_sets: list[tuple[int, str, set[str]]] = []
        for nid in matched_l3_nids:
            if nid not in active_l3_meta:
                continue
            nl1, nl2, nl3 = active_l3_meta[nid]
            if norm_primary_l1 and nl1 != norm_primary_l1:
                continue
            holders = {u for u, sm in struct_by_uid.items() if nid in sm["l3_nids"]}
            if holders:
                l3_doc_sets.append((len(holders), f"{nl2} · {nl3}", holders))
        if l3_doc_sets:
            l3_doc_sets.sort(key=lambda x: (x[0], x[1]))
            _, submodel_label, submodel_uids = l3_doc_sets[0]
        else:
            submodel_uids = {u for u, sm in struct_by_uid.items() if sm["l2_pairs"] & primary_l2_pairs}
            submodel_label = "、".join(sorted({x.split("/", 1)[-1] for x in primary_l2_pairs})) or norm_primary_l1 or grade_subject
    else:
        submodel_uids = {u for u, sm in struct_by_uid.items() if sm["l2_pairs"] & primary_l2_pairs}
        submodel_label = "、".join(sorted({x.split("/", 1)[-1] for x in primary_l2_pairs})) or norm_primary_l1 or grade_subject

    submodel_total = len(submodel_uids)
    submodel_peers = len(submodel_uids - excluded_uids)
    submodel_inventory = {
        "submodel_label": submodel_label,
        "total_in_db": submodel_total,
        "available_peers": submodel_peers,
    }

    # 有效非笔误陷阱集合（草稿陷阱 + 命中原题陷阱 + 近邻高频共现陷阱兜底）
    eff_traps: set[str] = {str(t) for t in diag.trap_tags if t != "TRAP_ARITHMETIC_SLIP"}
    if matched_orig_uid and matched_orig_uid in struct_by_uid:
        eff_traps |= set(struct_by_uid[matched_orig_uid]["traps"])
    if not eff_traps:
        nbr_trap_counts: dict[str, int] = {}
        for u, ksim in skel_hits[:6]:
            if u not in excluded_uids and ksim >= 0.86 and (struct_by_uid[u]["l2_pairs"] & primary_l2_pairs):
                for tr in struct_by_uid[u]["traps"]:
                    nbr_trap_counts[tr] = nbr_trap_counts.get(tr, 0) + 1
        for tr, cnt in nbr_trap_counts.items():
            if cnt >= 2:
                eff_traps.add(tr)

    def _card_item(uid: str, sim: float, rel_hint: str) -> dict[str, Any]:
        meta = servable_map[uid]
        raw_opts = meta.get("eff_options_json") or "{}"
        try:
            opts = json.loads(raw_opts) if isinstance(raw_opts, str) else dict(raw_opts)
        except ValueError:
            opts = {}
        return {
            "question_uid": uid,
            "source_id": meta["source_id"],
            "question_text": meta["eff_question_text"],
            "options": opts if isinstance(opts, dict) else {},
            "has_images": bool(meta.get("has_images")),
            "base_elo": int(meta["eff_base_elo"] or 1500),
            "tier": meta["eff_tier"],
            "primary_l1_module": meta["eff_primary_l1_module"],
            "similarity": round(float(sim), 4),
            "match_reason": rel_hint,
        }

    # 4. 优先提取已命中原题的 Stage 4 关联边（LLM_VERIFIED / HUMAN_VERIFIED 直接受信，RULE_MATRIX 入精裁池）
    trusted_by_slot: dict[str, list[tuple[float, str, float, str]]] = {
        "SCAFFOLD": [],
        "ISOMORPHIC": [],
        "CONTRASTIVE_TRAP": [],
    }
    pool_by_slot: dict[str, dict[str, tuple[float, float, str]]] = {
        "SCAFFOLD": {},
        "ISOMORPHIC": {},
        "CONTRASTIVE_TRAP": {},
    }

    def _push_pool(slot: str, uid: str, rank_score: float, disp_sim: float, default_reason: str) -> None:
        if uid in excluded_uids or uid not in servable_map:
            return
        prev = pool_by_slot[slot].get(uid)
        if prev is None or rank_score > prev[0]:
            pool_by_slot[slot][uid] = (rank_score, disp_sim, default_reason)

    if matched_orig_uid is not None:
        rel_rows = conn.execute(
            """
            SELECT * FROM question_relations
            WHERE source_question_uid = ? OR target_question_uid = ?;
            """,
            (matched_orig_uid, matched_orig_uid),
        ).fetchall()
        for rr in rel_rows:
            rtype = str(rr["relation_type"])
            src_u = str(rr["source_question_uid"])
            tgt_u = str(rr["target_question_uid"])
            conf = str(rr["confidence_source"] or "")
            note = str(rr["pedagogical_note"] or "").strip()
            k_sim_r = float(rr["skeleton_sim"] or 0.0)
            s_sim_r = float(rr["surface_sim"] or 0.0)

            if rtype == "SCAFFOLD":
                if tgt_u != matched_orig_uid:
                    continue  # 仅取降阶前置子题（pre 方向：source -> orig）
                peer = src_u
                if peer in excluded_uids or peer not in servable_map:
                    continue
                p_elo = int(servable_map[peer]["eff_base_elo"] or 1500)
                if not _scaffold_elo_ok(p_elo, ref_elo):
                    continue
                if conf in ("HUMAN_VERIFIED", "LLM_VERIFIED"):
                    trusted_by_slot["SCAFFOLD"].append(
                        (10.0 + k_sim_r, peer, k_sim_r, note or "库内已验证前置铺垫子题")
                    )
                else:
                    _push_pool("SCAFFOLD", peer, 1.15 + k_sim_r, k_sim_r, "同子模型前置铺垫题")
            elif rtype in ("ISOMORPHIC", "CLONE"):
                peer = tgt_u if src_u == matched_orig_uid else src_u
                if peer in excluded_uids or peer not in servable_map:
                    continue
                p_elo = int(servable_map[peer]["eff_base_elo"] or 1500)
                if not _isomorphic_elo_ok(p_elo, ref_elo, high_struct_match=True):
                    continue
                if conf in ("HUMAN_VERIFIED", "LLM_VERIFIED"):
                    trusted_by_slot["ISOMORPHIC"].append(
                        (10.0 + k_sim_r, peer, k_sim_r, note or "库内已验证同构变式题")
                    )
                else:
                    _push_pool("ISOMORPHIC", peer, 1.25 + k_sim_r, k_sim_r, "同核心模型变式巩固题")
            elif rtype == "CONTRASTIVE":
                peer = tgt_u if src_u == matched_orig_uid else src_u
                if peer in excluded_uids or peer not in servable_map:
                    continue
                p_elo = int(servable_map[peer]["eff_base_elo"] or 1500)
                p_c = int(servable_map[peer].get("eff_c_score") or 2)
                if not _contrastive_elo_ok(p_elo, ref_elo, same_l2=True, cand_c_score=p_c, has_shared_trap=False):
                    continue
                if conf in ("HUMAN_VERIFIED", "LLM_VERIFIED"):
                    trusted_by_slot["CONTRASTIVE_TRAP"].append(
                        (10.0 + s_sim_r, peer, s_sim_r, note or "库内已验证形似神异辨析题")
                    )
                else:
                    _push_pool("CONTRASTIVE_TRAP", peer, 1.10 + s_sim_r, s_sim_r, "易混条件对比辨析题")
            elif rtype == "SHARED_TRAP":
                peer = tgt_u if src_u == matched_orig_uid else src_u
                if peer in excluded_uids or peer not in servable_map:
                    continue
                p_elo = int(servable_map[peer]["eff_base_elo"] or 1500)
                p_c = int(servable_map[peer].get("eff_c_score") or 2)
                same_l2 = bool(struct_by_uid[peer]["l2_pairs"] & primary_l2_pairs)
                if _contrastive_elo_ok(p_elo, ref_elo, same_l2=same_l2, cand_c_score=p_c, has_shared_trap=True):
                    dom_bonus = 0.30 if same_l2 else (0.15 if servable_map[peer]["eff_primary_l1_module"] == norm_primary_l1 else 0.0)
                    _push_pool("CONTRASTIVE_TRAP", peer, 0.85 + dom_bonus + k_sim_r * 0.5, max(s_sim_r, k_sim_r), "共享核心易错陷阱排雷题")

    # 5. 多特征结构化候选召回（覆盖全量可服务池，支持跨模块分步铺垫、同子模型变式与双通道排雷）
    has_vectors = bool(skel_sim_map or surf_sim_map)
    rare_methods_hit = diag_methods & _RARE_THINKING_METHODS

    for uid, meta in servable_map.items():
        if uid in excluded_uids:
            continue
        q_elo = int(meta.get("eff_base_elo") or 1500)
        q_c = int(meta.get("eff_c_score") or 2)
        q_l1 = str(meta.get("eff_primary_l1_module") or "")
        sm = struct_by_uid[uid]
        cand_l2s: set[str] = sm["l2_pairs"]
        cand_l3s: set[str] = sm["l3_nids"]
        cand_traps: set[str] = sm["traps"]
        cand_motifs: set[str] = sm["motifs"]
        cand_methods: set[str] = sm["methods"]

        ksim = skel_sim_map.get(uid, 0.0)
        ssim = surf_sim_map.get(uid, 0.0)

        same_prim_l2 = bool(cand_l2s & primary_l2_pairs)
        same_step_l2 = bool(cand_l2s & step_l2_pairs)
        is_prereq_l2 = bool(cand_l2s & prereq_l2_pairs)
        shared_l3_cnt = len(cand_l3s & matched_l3_nids)
        shared_motif = bool(cand_motifs & diag_motifs)
        shared_rare_method = bool(cand_methods & rare_methods_hit)
        shared_traps = cand_traps & eff_traps

        # (A) 前置铺垫候选（支持按各步骤 L2 分拆召回低阶子技能题）
        if _scaffold_elo_ok(q_elo, ref_elo):
            sc_domain_ok = (
                same_step_l2
                or is_prereq_l2
                or shared_l3_cnt > 0
                or (not has_vectors and bool(norm_primary_l1) and q_l1 == norm_primary_l1 and not shared_traps)
            )
            if sc_domain_ok:
                is_cross_step_subskill = (len(non_tool_l1s) >= 2) and same_step_l2 and (q_l1 != norm_primary_l1)
                min_k = 0.66 if is_cross_step_subskill else (0.78 if shared_l3_cnt > 0 else 0.82)
                if (not has_vectors) or ksim >= min_k:
                    sc_score = (
                        ksim
                        + (0.22 if shared_l3_cnt > 0 else 0.0)
                        + (0.16 if same_step_l2 else 0.08 if is_prereq_l2 else 0.0)
                        + (0.14 if is_cross_step_subskill else 0.0)
                        - 0.03 * max(0, int(sm["step_count"]) - 2)
                    )
                    reason_sc = (
                        "跨模块前置子技能铺垫题"
                        if is_cross_step_subskill
                        else ("同原子算子降阶铺垫题" if shared_l3_cnt > 0 else "同专题基础模型铺垫题")
                    )
                    _push_pool("SCAFFOLD", uid, sc_score, ksim, reason_sc)

        # (B) 同构变式候选（严格要求同核心 L2 或稀缺思维原型命中；跨模块题须同时覆盖副模块或同子模型）
        high_iso_struct = shared_l3_cnt > 0 or (same_prim_l2 and ksim >= 0.90)
        if _isomorphic_elo_ok(q_elo, ref_elo, high_struct_match=high_iso_struct):
            cross_mod_ok = True
            if len(non_tool_l1s) >= 2:
                # 多模块融合原题（如几何周长×行程）：同构变式必须同样融合两模块或命中专属子算子
                cand_non_tool = {
                    x.split("/", 1)[0] for x in cand_l2s if "/" in x and x.split("/", 1)[0] in non_tool_l1s
                }
                cross_mod_ok = len(cand_non_tool) >= 2 or (uid in submodel_uids)
            iso_domain_ok = cross_mod_ok and (
                same_prim_l2
                or shared_l3_cnt > 0
                or shared_motif
                or shared_rare_method
                or (not has_vectors and bool(norm_primary_l1) and q_l1 == norm_primary_l1 and not shared_traps)
            )
            min_iso_k = 0.80 if (shared_l3_cnt > 0 or shared_rare_method) else 0.86
            if iso_domain_ok and ((not has_vectors) or ksim >= min_iso_k):
                iso_score = (
                    ksim
                    + (0.25 if shared_l3_cnt > 0 else 0.0)
                    + (0.15 if same_prim_l2 else 0.0)
                    + (0.12 if (shared_motif or shared_rare_method) else 0.0)
                    - 0.04 * (abs(q_elo - ref_elo) / 250.0)
                )
                reason_iso = "同子模型等量结构变式题" if shared_l3_cnt > 0 else "同专题核心模型变式题"
                _push_pool("ISOMORPHIC", uid, iso_score, ksim, reason_iso)

        # (C) 辨析/同坑排雷双通道候选（严禁无条件拿跨模块表皮词凑数，强制 Elo 上界保护）
        if _contrastive_elo_ok(
            q_elo,
            ref_elo,
            same_l2=same_prim_l2,
            cand_c_score=q_c,
            has_shared_trap=bool(shared_traps),
        ):
            # 子通道 C1：显式同坑排雷（按同 L2 > 同 L1 > 跨 L1 + 骨架相关性排序）
            if shared_traps:
                dom_ok = same_prim_l2 or (q_l1 == norm_primary_l1 and ksim >= 0.78) or (ksim >= 0.84)
                if (not has_vectors) or dom_ok:
                    tr_score = (
                        0.50 * ksim
                        + 0.25 * ssim
                        + (0.32 if same_prim_l2 else (0.16 if q_l1 == norm_primary_l1 else 0.0))
                        - 0.05 * (abs(q_elo - ref_elo) / 300.0)
                    )
                    _push_pool(
                        "CONTRASTIVE_TRAP",
                        uid,
                        tr_score,
                        max(ssim, ksim),
                        f"靶向排雷（共享易错点：{','.join(sorted(shared_traps))}）",
                    )
            # 子通道 C2：同模块条件分叉辨析 或 稀缺极值/统筹思维跨场景呼应
            elif has_vectors:
                has_divergent_kernel = (not matched_l3_nids) or (cand_l3s != matched_l3_nids) or bool(cand_traps)
                same_domain_contrast = (
                    (same_prim_l2 or (q_l1 == norm_primary_l1 and bool(cand_traps)))
                    and ssim >= 0.80
                    and ksim >= 0.74
                    and has_divergent_kernel
                )
                mindset_contrast = shared_rare_method and (ssim >= 0.75 or ksim >= 0.78)
                if same_domain_contrast or mindset_contrast:
                    ct_score = (
                        0.55 * ssim
                        + 0.30 * ksim
                        + (0.18 if bool(cand_traps) else 0.0)
                        + (0.20 if mindset_contrast else 0.0)
                    )
                    reason_ct = (
                        "跨场景瓶颈极值思维辨析题"
                        if mindset_contrast
                        else "同场景易混条件对比辨析题"
                    )
                    _push_pool("CONTRASTIVE_TRAP", uid, ct_score, ssim, reason_ct)

    # 6. 组装 Top-12 待精裁候选池，执行可选单次纯文本 judge_llm 批精裁
    trusted_uids = {u for grp in trusted_by_slot.values() for _, u, _, _ in grp}
    rerank_entries: list[dict[str, Any]] = []
    seen_in_rerank: set[str] = set()
    seen_digit_fps_rerank: set[str] = set()
    seen_clone_roots_rerank: set[str] = {clone_root.get(u, u) for u in trusted_uids}

    slot_map_order = (
        ("SCAFFOLD", 4),
        ("ISOMORPHIC", 4),
        ("CONTRASTIVE_TRAP", 4),
    )
    for slot_name, cap_k in slot_map_order:
        ranked_uids = sorted(
            pool_by_slot[slot_name].items(),
            key=lambda kv: (-kv[1][0], kv[0]),
        )
        picked_for_slot = 0
        for uid, (r_sc, d_sim, def_reason) in ranked_uids:
            if picked_for_slot >= cap_k or len(rerank_entries) >= 12:
                break
            if uid in trusted_uids or uid in seen_in_rerank:
                continue
            dfp = digit_fp_by_uid.get(uid, "")
            if dfp and dfp in seen_digit_fps_rerank:
                continue
            c_root_cand = clone_root.get(uid, uid)
            if c_root_cand in seen_clone_roots_rerank:
                continue
            seen_in_rerank.add(uid)
            if dfp:
                seen_digit_fps_rerank.add(dfp)
            seen_clone_roots_rerank.add(c_root_cand)
            picked_for_slot += 1
            cid = f"c{len(rerank_entries) + 1:02d}"
            meta = servable_map[uid]
            sm = struct_by_uid[uid]
            rerank_entries.append(
                {
                    "candidate_id": cid,
                    "uid": uid,
                    "proposed_slot": slot_name,
                    "rank_score": r_sc,
                    "disp_sim": d_sim,
                    "default_reason": def_reason,
                    "base_elo": int(meta.get("eff_base_elo") or 1500),
                    "c_score": int(meta.get("eff_c_score") or 2),
                    "tier": str(meta.get("eff_tier") or ""),
                    "primary_l1_module": str(meta.get("eff_primary_l1_module") or ""),
                    "l2_entities": sorted(sm["l2_pairs"]),
                    "l3_operators": list(sm["l3_labels"]),
                    "thinking_methods": sorted(sm["methods"]),
                    "trap_tags": sorted(sm["traps"]),
                    "surface_sim": surf_sim_map.get(uid, 0.0),
                    "skeleton_sim": skel_sim_map.get(uid, 0.0),
                    "question_text": str(meta.get("eff_question_text") or ""),
                    "abstract_skeleton": str(meta.get("eff_abstract_skeleton") or ""),
                }
            )

    judge_slot_cfg = settings.llm.judge_llm if settings.llm.judge_llm.model else settings.llm.text_llm
    judge_callable = use_mock or (
        bool(judge_slot_cfg.model)
        and (
            bool(judge_slot_cfg.api_key)
            or "localhost" in judge_slot_cfg.base_url
            or "127.0.0.1" in judge_slot_cfg.base_url
        )
    )

    reranked_decisions: dict[str, tuple[str, str]] = {}
    rerank_applied = False
    if settings.pipeline.enable_diagnose_llm_rerank and rerank_entries and judge_callable:
        mistake_summary = {
            "question_text": norm_text,
            "abstract_skeleton": skel_text,
            "primary_l1_module": norm_primary_l1,
            "matched_l2": sorted(primary_l2_pairs),
            "step_operators": [f"{s.raw_l1}/{s.raw_l2}:{s.raw_l3}" for s in diag.steps],
            "thinking_methods": sorted(diag_methods),
            "classic_motifs": sorted(diag_motifs),
            "trap_tags": sorted(eff_traps),
            "ref_elo": ref_elo,
            "ref_tier": ref_tier,
        }
        rerank_msgs = build_diagnose_rerank_messages(
            grade_subject=grade_subject,
            mistake_summary=mistake_summary,
            candidates=rerank_entries,
        )
        gw_settings = settings.model_copy(deep=True)
        gw_settings.pipeline.use_mock_llm = use_mock
        gw_rerank = LLMGateway(gw_settings, conn=None)
        try:
            rr_res = await gw_rerank.call_structured(
                slot="judge_llm",
                stage="DIAGNOSE_RERANK",
                messages=rerank_msgs,
                schema=DiagnoseRerankBatchOutput,
                prompt_version="diag_rerank_v1",
            )
            if rr_res.parsed.decisions:
                rerank_applied = True
                for dec in rr_res.parsed.decisions:
                    reranked_decisions[dec.candidate_id.strip()] = (
                        dec.verdict,
                        dec.pedagogical_reason.strip(),
                    )
        except LLMGatewayError as exc:
            if use_mock:
                raise
            warnings.append(f"DIAGNOSE_RERANK_DEGRADED: 推荐二级精裁调用降级，已回退结构阈值过滤 ({exc})")
        finally:
            await gw_rerank.aclose()

    # 7. 汇总受信 Stage 4 边与精裁通过项，强制校验 Elo 护栏 + CLONE 簇去重 + 宁缺毋滥截断
    final_by_slot: dict[str, list[tuple[float, str, float, str]]] = {
        k: list(v) for k, v in trusted_by_slot.items()
    }

    def _slot_elo_valid(target_slot: str, q_elo: int, q_c: int, same_l2: bool, has_sh_trap: bool) -> bool:
        if target_slot == "SCAFFOLD":
            return _scaffold_elo_ok(q_elo, ref_elo)
        if target_slot == "ISOMORPHIC":
            return _isomorphic_elo_ok(q_elo, ref_elo, high_struct_match=True)
        return _contrastive_elo_ok(
            q_elo,
            ref_elo,
            same_l2=same_l2,
            cand_c_score=q_c,
            has_shared_trap=has_sh_trap,
        )

    if rerank_applied:
        for entry in rerank_entries:
            cid = entry["candidate_id"]
            uid = entry["uid"]
            verdict_pair = reranked_decisions.get(cid)
            if verdict_pair is None or verdict_pair[0] == "REJECT":
                continue
            target_slot, custom_reason = verdict_pair
            q_elo = int(entry["base_elo"])
            q_c = int(entry["c_score"])
            same_l2 = bool(struct_by_uid[uid]["l2_pairs"] & primary_l2_pairs)
            has_sh_trap = bool(struct_by_uid[uid]["traps"] & eff_traps)
            if not _slot_elo_valid(target_slot, q_elo, q_c, same_l2, has_sh_trap):
                continue
            disp_sim = (
                entry["surface_sim"]
                if target_slot == "CONTRASTIVE_TRAP" and entry["surface_sim"] >= entry["skeleton_sim"]
                else entry["skeleton_sim"] or entry["disp_sim"]
            )
            final_by_slot[target_slot].append(
                (
                    float(entry["rank_score"]) + 1.0,
                    uid,
                    float(disp_sim),
                    custom_reason or str(entry["default_reason"]),
                )
            )
    else:
        # 未启用或 Mock 默认空决策时：按各栏独立池执行纯确定性严格门槛过滤（避免跨栏前置占用饿死）
        for slot_name in ("SCAFFOLD", "ISOMORPHIC", "CONTRASTIVE_TRAP"):
            ranked_pool = sorted(
                pool_by_slot[slot_name].items(),
                key=lambda kv: (-kv[1][0], kv[0]),
            )
            for uid, (r_sc, d_sim, def_reason) in ranked_pool[:8]:
                if uid in trusted_uids:
                    continue
                meta = servable_map[uid]
                q_elo = int(meta.get("eff_base_elo") or 1500)
                q_c = int(meta.get("eff_c_score") or 2)
                same_l2 = bool(struct_by_uid[uid]["l2_pairs"] & primary_l2_pairs)
                has_sh_trap = bool(struct_by_uid[uid]["traps"] & eff_traps)
                ksim = skel_sim_map.get(uid, 0.0)
                ssim = surf_sim_map.get(uid, 0.0)
                if has_vectors:
                    if slot_name == "SCAFFOLD" and ksim < 0.84 and len(non_tool_l1s) < 2:
                        continue
                    if slot_name == "ISOMORPHIC" and ksim < 0.88:
                        continue
                    if slot_name == "CONTRASTIVE_TRAP" and not has_sh_trap and ssim < 0.85:
                        continue
                if not _slot_elo_valid(slot_name, q_elo, q_c, same_l2, has_sh_trap):
                    continue
                disp_sim = (
                    ssim
                    if slot_name == "CONTRASTIVE_TRAP" and ssim >= ksim
                    else (ksim or d_sim)
                )
                final_by_slot[slot_name].append((float(r_sc), uid, float(disp_sim), str(def_reason)))

    used_uids_global: set[str] = set(excluded_uids)
    used_digit_fps_global: set[str] = set()
    used_clone_roots_global: set[str] = set()

    def _materialize_column(slot_key: str) -> list[dict[str, Any]]:
        ranked = sorted(final_by_slot[slot_key], key=lambda item: (-item[0], item[1]))
        out_cards: list[dict[str, Any]] = []
        for _, uid, sim_val, reason_txt in ranked:
            if len(out_cards) >= 3:
                break
            if uid in used_uids_global:
                continue
            dfp = digit_fp_by_uid.get(uid, "")
            if dfp and dfp in used_digit_fps_global:
                continue
            c_root = clone_root.get(uid, uid)
            if c_root in used_clone_roots_global:
                continue
            used_uids_global.add(uid)
            if dfp:
                used_digit_fps_global.add(dfp)
            used_clone_roots_global.add(c_root)
            out_cards.append(_card_item(uid, sim_val, reason_txt))
        return out_cards

    scaffolds = _materialize_column("SCAFFOLD")
    isomorphics = _materialize_column("ISOMORPHIC")
    contrastive_traps = _materialize_column("CONTRASTIVE_TRAP")

    recommendations = {
        "scaffold_foundations": scaffolds,
        "isomorphic_variants": isomorphics,
        "contrastive_and_traps": contrastive_traps,
    }
    recommendation_notes: dict[str, str] = {}
    for col_key, col_items in recommendations.items():
        if len(col_items) == 0:
            recommendation_notes[col_key] = (
                f"未召回满足置信下限的候选题（当前题库「{submodel_label}」细分模型仅收录 {submodel_total} 道匹配题，不再强凑低相关题）"
            )
        elif len(col_items) < 3:
            recommendation_notes[col_key] = (
                f"按置信下限精选 {len(col_items)} 题（当前题库「{submodel_label}」细分模型仅收录 {submodel_total} 道匹配题，已过滤跨模型噪音）"
            )

    return {
        "recommendation_engine_version": RECOMMENDATION_ENGINE_VERSION,
        "rerank_applied": rerank_applied,
        "effective_base_elo": ref_elo,
        "effective_tier": ref_tier,
        "effective_tier_rank": ref_tier_rank,
        "elo_calibration_source": elo_calibration_source,
        "matched_db_question": matched_db_question,
        "submodel_inventory": submodel_inventory,
        "duplicate_candidates": duplicate_candidates,
        "recommendations": recommendations,
        "recommendation_notes": recommendation_notes,
        "warnings": warnings,
    }


async def diagnose_external_mistake_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    question_text: str,
    grade_subject: str,
    image_paths: Sequence[str | Path] | None = None,
    known_answer: str | None = None,
    diagnosis_json: dict[str, Any] | str | None = None,
    config_dir: Path | str = Path("config"),
    vectors_dir: Path | str = Path("data/vectors"),
    drafts_dir: Path | str = Path("data/drafts"),
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """只读实时诊断外部新错题，生成 data/drafts/<draft_id>.json 并推荐关联题。"""
    gs_row = conn.execute(
        "SELECT grade_subject, slug FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if not gs_row:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科 '{grade_subject}' 尚未注册")
    scope_slug = str(gs_row["slug"])

    norm_text = normalize_text(question_text or "").normalized_text
    if not norm_text:
        raise PreconditionError("题干 text 不能为空")

    draft_root = Path(drafts_dir)
    _prune_expired_drafts(draft_root)
    draft_id = f"drf_{uuid.uuid4().hex[:12]}"
    draft_img_dir = draft_root / "images"
    draft_img_dir.mkdir(parents=True, exist_ok=True)

    raw_img_paths: list[Path] = []
    preprocessed_imgs: list[Path] = []
    for idx, raw_img in enumerate(image_paths or [], start=1):
        src_p = Path(raw_img)
        if not src_p.is_file():
            raise PreconditionError(f"找不到传入的错题图片: {src_p}")
        raw_img_paths.append(src_p)
        dst_p = draft_img_dir / f"{draft_id}_{idx}.jpg"
        prepare_external_image(src_p, dst_p, max_px=settings.pipeline.max_external_image_px)
        preprocessed_imgs.append(dst_p)

    # 1. 结构化解析（JSON 逃生通道 或 主路 VLM）
    eff_mock = settings.pipeline.use_mock_llm if use_mock is None else bool(use_mock)
    gw_settings = settings.model_copy(deep=True)
    gw_settings.pipeline.use_mock_llm = eff_mock
    if diagnosis_json is not None:
        raw_obj = json.loads(diagnosis_json) if isinstance(diagnosis_json, str) else diagnosis_json
        try:
            diag = ExternalDiagnosisOutput.model_validate(raw_obj)
        except ValidationError as exc:
            raise PreconditionError(f"诊断 JSON 校验失败: {exc}") from exc
    else:
        tax_enum = _build_taxonomy_enum_summary(conn, grade_subject)
        anchors_text = _build_anchors_prompt_text(conn, grade_subject, config_dir=config_dir)
        diag_ver = _resolve_diagnose_prompt_version(config_dir, grade_subject)
        msgs = build_external_diagnose_messages(
            grade_subject=grade_subject,
            question_text=norm_text,
            taxonomy_enum_text=tax_enum,
            preprocessed_image_paths=preprocessed_imgs,
            anchors_prompt_text=anchors_text,
            config_dir=Path(config_dir),
        )
        gw = LLMGateway(gw_settings, conn=None)
        try:
            res = await gw.call_structured(
                slot="vlm_primary",
                stage="DIAGNOSE",
                messages=msgs,
                schema=ExternalDiagnosisOutput,
                prompt_version=diag_ver,
            )
            diag = res.parsed
        finally:
            await gw.aclose()

    diag_parts = normalize_answer_parts(diag.final_answers)
    diff_res = difficulty.compute_difficulty(
        diag.difficulty.C_concept_count.score,
        diag.difficulty.S_chain_length.score,
        diag.difficulty.T_trickiness.score,
        diag.difficulty.L_computation_load.score,
    )

    # 2. 若提供了已知标准答案，执行一致性核对
    known_answer_match: bool | None = None
    if known_answer and known_answer.strip():
        known_parts = _parse_single_known_answer(known_answer, diag_parts)
        eq_res = await compare_answer_parts(
            diag_parts,
            known_parts,
            grade_subject=grade_subject,
            allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
            sympy_result=None,
            timeout_seconds=settings.pipeline.sympy_timeout_seconds,
            memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
        )
        known_answer_match = bool(eq_res.all_dual_equal)

    # 3. 内存 1 vs N 向量 + 结构化标签 + Stage 4 验真边复用 + 可选 judge_llm 二级精裁
    rec_bundle = await build_diagnose_recommendations_async(
        conn,
        settings,
        grade_subject=grade_subject,
        scope_slug=scope_slug,
        norm_text=norm_text,
        image_count=len(preprocessed_imgs),
        diag=diag,
        diag_parts=diag_parts,
        diff_res=diff_res,
        vectors_dir=vectors_dir,
        use_mock=eff_mock,
    )

    eff_elo = int(rec_bundle["effective_base_elo"])
    eff_tier = str(rec_bundle["effective_tier"])
    eff_tier_rank = int(rec_bundle["effective_tier_rank"])
    duplicate_candidates = rec_bundle["duplicate_candidates"]
    recommendations = rec_bundle["recommendations"]
    recommendation_notes = rec_bundle["recommendation_notes"]
    submodel_inventory = rec_bundle["submodel_inventory"]
    matched_db_question = rec_bundle["matched_db_question"]
    diag_warnings = rec_bundle["warnings"]

    content_hash = compute_diagnose_content_hash(grade_subject, norm_text, raw_img_paths)

    draft_payload = {
        "draft_id": draft_id,
        "recommendation_engine_version": RECOMMENDATION_ENGINE_VERSION,
        "rerank_applied": bool(rec_bundle["rerank_applied"]),
        "grade_subject": grade_subject,
        "question_text": norm_text,
        "preprocessed_images": [str(p.resolve()) for p in preprocessed_imgs],
        "known_answer": known_answer,
        "known_answer_match": known_answer_match,
        "content_hash": content_hash,
        "diagnosis": diag.model_dump(),
        "base_elo": eff_elo,
        "tier": eff_tier,
        "tier_rank": eff_tier_rank,
        "raw_draft_base_elo": diff_res.base_elo,
        "raw_draft_tier": diff_res.tier,
        "elo_calibration_source": rec_bundle["elo_calibration_source"],
        "matched_db_question": matched_db_question,
        "submodel_inventory": submodel_inventory,
        "duplicate_candidates": duplicate_candidates,
        "recommendations": recommendations,
        "recommendation_notes": recommendation_notes,
        "warnings": diag_warnings,
        "unverified_single_pass": True,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    draft_file = draft_root / f"{draft_id}.json"
    _atomic_write_text(draft_file, json.dumps(draft_payload, ensure_ascii=False, indent=2))

    return {
        "draft_id": draft_id,
        "draft_file": str(draft_file.resolve()),
        "recommendation_engine_version": RECOMMENDATION_ENGINE_VERSION,
        "rerank_applied": bool(rec_bundle["rerank_applied"]),
        "unverified_single_pass": True,
        "grade_subject": grade_subject,
        "base_elo": eff_elo,
        "tier": eff_tier,
        "raw_draft_base_elo": diff_res.base_elo,
        "raw_draft_tier": diff_res.tier,
        "elo_calibration_source": rec_bundle["elo_calibration_source"],
        "matched_db_question": matched_db_question,
        "submodel_inventory": submodel_inventory,
        "primary_l1_module": diag.primary_l1_module,
        "matched_l2": diag.matched_l2,
        "visual_facts": diag.visual_facts,
        "method_summary": diag.method_summary,
        "solution_markdown": diag.solution_markdown,
        "final_answers": [p.model_dump() for p in diag_parts],
        "abstract_skeleton": diag.abstract_skeleton,
        "thinking_methods": diag.thinking_methods,
        "classic_motifs": diag.classic_motifs,
        "trap_tags": diag.trap_tags,
        "known_answer_match": known_answer_match,
        "duplicate_candidates": duplicate_candidates,
        "recommendations": recommendations,
        "recommendation_notes": recommendation_notes,
        "warnings": diag_warnings,
    }


async def ingest_external_mistake_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    draft_id: str | None = None,
    question_text: str | None = None,
    grade_subject: str | None = None,
    image_paths: Sequence[str | Path] | None = None,
    known_answer: str | None = None,
    diagnosis_json: dict[str, Any] | str | None = None,
    secondary_json: dict[str, Any] | str | None = None,
    log_wrong: bool = False,
    learner_id: str = "default",
    user_note: str = "",
    dry_run: bool = False,
    assets_dir: Path | str = Path("data/assets/images"),
    config_dir: Path | str = Path("config"),
    vectors_dir: Path | str = Path("data/vectors"),
    drafts_dir: Path | str = Path("data/drafts"),
    human_dir: Path | str = Path("human"),
    lock_path: Path | str = Path("data/.compile.lock"),
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """将外部新错题一键写入题库、补全双盲/Stage2/S3/向量与五边拓扑并同步 5 份 JSONL。"""
    draft_data: dict[str, Any] | None = None
    if draft_id:
        df = Path(drafts_dir) / f"{draft_id}.json"
        if not df.exists():
            raise repository.NotFoundError(f"未找到诊断草稿 '{draft_id}'")
        draft_data = json.loads(df.read_text(encoding="utf-8"))

    eff_gs = grade_subject or (draft_data["grade_subject"] if draft_data else None)
    eff_text = normalize_text(
        question_text if question_text is not None else (draft_data["question_text"] if draft_data else "")
    ).normalized_text
    if not eff_gs or not eff_text:
        raise PreconditionError("ingest-mistake 需要有效的 grade_subject 与 question_text（或有效 --draft-id）")

    gs_row = conn.execute(
        "SELECT grade_subject, slug, taxonomy_status, taxonomy_version FROM grade_subjects WHERE grade_subject = ?;",
        (eff_gs,),
    ).fetchone()
    if not gs_row:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科 '{eff_gs}' 未注册")
    scope_slug = str(gs_row["slug"])

    if log_wrong:
        lr_row = conn.execute(
            "SELECT 1 FROM learners WHERE learner_id = ?;",
            (learner_id,),
        ).fetchone()
        if not lr_row:
            raise PreconditionError(f"PRECONDITION_FAILED: 学习者 '{learner_id}' 不存在，无法记录错题日志")

    eff_known_answer = known_answer if known_answer is not None else (draft_data.get("known_answer") if draft_data else None)
    eff_mock = settings.pipeline.use_mock_llm if use_mock is None else bool(use_mock)
    gw_settings = settings.model_copy(deep=True)
    gw_settings.pipeline.use_mock_llm = eff_mock

    if diagnosis_json is not None:
        raw_obj = json.loads(diagnosis_json) if isinstance(diagnosis_json, str) else diagnosis_json
        try:
            diag = ExternalDiagnosisOutput.model_validate(raw_obj)
        except ValidationError as exc:
            raise PreconditionError(f"诊断 JSON 校验失败: {exc}") from exc
    elif draft_data is not None:
        try:
            diag = ExternalDiagnosisOutput.model_validate(draft_data["diagnosis"])
        except ValidationError as exc:
            raise PreconditionError(f"草稿 '{draft_id}' 诊断数据校验失败（可能为旧版不完整草稿，请重新 diagnose）: {exc}") from exc
    else:
        diag_res = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text=eff_text,
            grade_subject=eff_gs,
            image_paths=image_paths,
            known_answer=eff_known_answer,
            config_dir=config_dir,
            vectors_dir=vectors_dir,
            drafts_dir=drafts_dir,
            use_mock=eff_mock,
        )
        df_new = Path(drafts_dir) / f"{diag_res['draft_id']}.json"
        draft_data = json.loads(df_new.read_text(encoding="utf-8"))
        diag = ExternalDiagnosisOutput.model_validate(draft_data["diagnosis"])

    raw_imgs: list[Path] = [Path(p) for p in (image_paths or [])]
    if not raw_imgs and draft_data:
        raw_imgs = [Path(p) for p in draft_data.get("preprocessed_images", [])]

    plain_fp = compute_plain_fingerprint(eff_text, len(raw_imgs))
    draft_chash = str(draft_data.get("content_hash") or "").strip() if draft_data else ""
    draft_unmodified = (
        draft_data is not None
        and bool(draft_chash)
        and not image_paths
        and eff_gs == draft_data.get("grade_subject")
        and eff_text == normalize_text(str(draft_data.get("question_text") or "")).normalized_text
    )
    if draft_unmodified:
        raw_content_hash = draft_chash
    else:
        for src_p in raw_imgs:
            if not src_p.is_file():
                raise PreconditionError(f"错题图片不存在: {src_p}")
        raw_content_hash = compute_diagnose_content_hash(eff_gs, eff_text, raw_imgs)
    uid = f"{scope_slug}:ext:{raw_content_hash[:12]}"
    source_id = f"ext_{raw_content_hash[:10]}"

    if dry_run:
        return {
            "dry_run": True,
            "question_uid": uid,
            "source_id": source_id,
            "grade_subject": eff_gs,
            "reused_draft_id": draft_id,
            "primary_l1_module": diag.primary_l1_module,
            "matched_l2": diag.matched_l2,
            "will_log_wrong": bool(log_wrong),
            "learner_id": learner_id,
        }

    # 1. CAS 归档图片（EXIF + 2048px）
    cas_names: list[str] = []
    if raw_imgs:
        tmp_img_dir = Path(drafts_dir) / "cas_prep"
        tmp_img_dir.mkdir(parents=True, exist_ok=True)
        for idx, src_p in enumerate(raw_imgs, start=1):
            if not src_p.is_file():
                raise PreconditionError(f"错题图片不存在: {src_p}")
            prepped = prepare_external_image(src_p, tmp_img_dir / f"{uid.replace(':', '_')}_{idx}.jpg", max_px=settings.pipeline.max_external_image_px)
            cas_names.append(archive_image(prepped, assets_dir))

    primary_parts = normalize_answer_parts(diag.final_answers)
    has_images = len(cas_names) > 0

    # 2. 补齐 SECONDARY 辅路双盲求解（逃生 JSON 或 LLMGateway）
    if secondary_json is not None:
        sec_obj = json.loads(secondary_json) if isinstance(secondary_json, str) else secondary_json
        sec_out = Stage1SecondaryOutput.model_validate(sec_obj)
    else:
        s1_inp = Stage1Input(
            question_uid=uid,
            grade_subject=eff_gs,
            question_text=eff_text,
            cas_images=cas_names,
            has_images=has_images,
        )
        s1_msgs = build_stage1_messages(
            s1_inp,
            role="SECONDARY",
            assets_dir=Path(assets_dir),
            config_dir=Path(config_dir),
        )
        gw = LLMGateway(gw_settings, conn=None)
        try:
            sec_res = await gw.call_structured(
                slot="vlm_secondary",
                stage="STAGE1_SECONDARY",
                messages=s1_msgs,
                schema=Stage1SecondaryOutput,
                question_uid=uid,
                prompt_version=STAGE1_PROMPT_VERSION,
            )
            sec_out = sec_res.parsed
        finally:
            await gw.aclose()

    sec_parts = normalize_answer_parts(sec_out.final_answers)

    # SymPy 沙盒验算（若有 SYMBOLIC 小问）
    sbx_res = None
    if diag.sympy_code:
        sbx_res = run_verify_script(
            diag.sympy_code,
            timeout_seconds=settings.pipeline.sympy_timeout_seconds,
            memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
        )

    gw_judge = LLMGateway(gw_settings, conn=conn)

    async def _qual_judge(lbl: str, a_str: str, b_str: str) -> QualitativeJudgeOutput:
        jmsgs = build_qualitative_judge_messages(
            lbl,
            a_str,
            b_str,
            question_text=eff_text,
            grade_subject=eff_gs,
            config_dir=Path(config_dir),
        )
        jr = await gw_judge.call_structured(
            slot="judge_llm",
            stage="STAGE1_JUDGE",
            messages=jmsgs,
            schema=QualitativeJudgeOutput,
            question_uid=uid,
            prompt_version="judge_v1",
        )
        return jr.parsed

    try:
        eq_dual = await compare_answer_parts(
            primary_parts,
            sec_parts,
            grade_subject=eff_gs,
            allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
            sympy_result=sbx_res,
            qualitative_judge=_qual_judge,
            timeout_seconds=settings.pipeline.sympy_timeout_seconds,
            memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
        )
        known_mismatch = False
        has_known = bool(eff_known_answer and eff_known_answer.strip())
        if has_known:
            assert eff_known_answer is not None
            k_parts = _parse_single_known_answer(eff_known_answer, primary_parts)
            eq_known = await compare_answer_parts(
                primary_parts,
                k_parts,
                grade_subject=eff_gs,
                allow_pi_314_subjects=settings.pipeline.allow_pi_314_subjects,
                qualitative_judge=_qual_judge,
                timeout_seconds=settings.pipeline.sympy_timeout_seconds,
                memory_limit_mb=settings.pipeline.sympy_memory_limit_mb,
            )
            known_mismatch = not eq_known.all_dual_equal
    finally:
        await gw_judge.aclose()

    review_reasons = list(eq_dual.review_reasons)
    if has_known and known_mismatch and "KNOWN_ANSWER_MISMATCH" not in review_reasons:
        review_reasons.append("KNOWN_ANSWER_MISMATCH")

    if has_known and not known_mismatch and eq_dual.all_dual_equal and eq_dual.all_sympy_ok:
        ver_status = "VERIFIED_CONSENSUS"
        human_verified_by_known = True
    elif not has_known and eq_dual.all_dual_equal and eq_dual.all_sympy_ok:
        ver_status = "VERIFIED_CONSENSUS"
        human_verified_by_known = False
    else:
        ver_status = "NEEDS_REVIEW"
        human_verified_by_known = False

    answers_json = json.dumps([p.model_dump() for p in primary_parts], ensure_ascii=False)
    diff_res = difficulty.compute_difficulty(
        diag.difficulty.C_concept_count.score,
        diag.difficulty.S_chain_length.score,
        diag.difficulty.T_trickiness.score,
        diag.difficulty.L_computation_load.score,
    )

    cas_images_str = json.dumps(cas_names, ensure_ascii=False)
    logged_id: int | None = None
    woven_edges = 0
    weave_warning: str | None = None
    is_servable = False

    with CompileLock(lock_path):
        with conn:
            # 0. 加载当前活跃词表（若有），提前归一题级主/次 L1 模块
            tax_status = str(gs_row["taxonomy_status"] or "NONE")
            tax_ver = str(gs_row["taxonomy_version"] or "draft-0")
            tax_spec = None
            tax_lookup = None
            try:
                _, tax_ver, tax_spec, tax_lookup = _load_active_taxonomy(conn, eff_gs)
            except PreconditionError:
                tax_spec = None
                tax_lookup = None

            norm_primary_l1 = diag.primary_l1_module.strip()
            norm_secondary_l1s: list[str] = []
            if tax_lookup is not None and tax_spec is not None:
                resolved_prim = tax_lookup.resolve_l1(norm_primary_l1)
                if resolved_prim is None:
                    for st in diag.steps:
                        pair_hit = tax_lookup.resolve_l2(st.raw_l1.strip(), st.raw_l2.strip())
                        if pair_hit is not None:
                            resolved_prim = pair_hit[0]
                            break
                        st_l1_hit = tax_lookup.resolve_l1(st.raw_l1.strip())
                        if st_l1_hit is not None:
                            resolved_prim = st_l1_hit
                            break
                if resolved_prim is None and tax_spec.modules:
                    resolved_prim = tax_spec.modules[0].name
                if resolved_prim:
                    norm_primary_l1 = resolved_prim
                for s_l1 in diag.secondary_l1_modules:
                    r_sec = tax_lookup.resolve_l1(s_l1.strip())
                    if r_sec and r_sec != norm_primary_l1 and r_sec not in norm_secondary_l1s:
                        norm_secondary_l1s.append(r_sec)
            else:
                for s_l1 in diag.secondary_l1_modules:
                    s_clean = s_l1.strip()
                    if s_clean and s_clean != norm_primary_l1 and s_clean not in norm_secondary_l1s:
                        norm_secondary_l1s.append(s_clean)

            # 1. 写入 raw_questions & questions
            conn.execute(
                """
                INSERT INTO raw_questions(
                    question_uid, grade_subject, source_type, source_collection, source_id,
                    raw_question, raw_images_json, cas_images_json, content_hash, plain_fingerprint, updated_at
                )
                VALUES (?, ?, 'EXTERNAL_MISTAKE', 'external_mistake', ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(question_uid) DO UPDATE SET
                    raw_question = excluded.raw_question,
                    raw_images_json = excluded.raw_images_json,
                    cas_images_json = excluded.cas_images_json,
                    content_hash = excluded.content_hash,
                    plain_fingerprint = excluded.plain_fingerprint,
                    updated_at = datetime('now');
                """,
                (
                    uid,
                    eff_gs,
                    source_id,
                    eff_text,
                    json.dumps([str(p) for p in raw_imgs], ensure_ascii=False),
                    cas_images_str,
                    raw_content_hash,
                    plain_fp,
                ),
            )
            conn.execute(
                """
                INSERT INTO questions(
                    question_uid, grade_subject, m_question_text, m_format, m_visual_facts,
                    m_verification_status, review_reason, m_final_answers_json,
                    m_abstract_skeleton, m_primary_l1_module, m_secondary_l1_modules_json,
                    m_c_score, m_s_score, m_t_score, m_l_score,
                    has_images, cas_images_json,
                    raw_l1_candidates_json, raw_l2_candidates_json,
                    raw_thinking_methods_json, raw_classic_motifs_json, raw_trap_tags_json,
                    difficulty_evidence_json
                )
                VALUES (?, ?, ?, '解答题', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(question_uid) DO UPDATE SET
                    m_question_text = excluded.m_question_text,
                    m_visual_facts = excluded.m_visual_facts,
                    m_verification_status = excluded.m_verification_status,
                    review_reason = excluded.review_reason,
                    m_final_answers_json = excluded.m_final_answers_json,
                    m_abstract_skeleton = excluded.m_abstract_skeleton,
                    m_primary_l1_module = excluded.m_primary_l1_module,
                    m_secondary_l1_modules_json = excluded.m_secondary_l1_modules_json,
                    m_c_score = excluded.m_c_score,
                    m_s_score = excluded.m_s_score,
                    m_t_score = excluded.m_t_score,
                    m_l_score = excluded.m_l_score,
                    has_images = excluded.has_images,
                    cas_images_json = excluded.cas_images_json,
                    updated_at = datetime('now');
                """,
                (
                    uid,
                    eff_gs,
                    eff_text,
                    diag.visual_facts,
                    ver_status,
                    ",".join(review_reasons),
                    answers_json,
                    diag.abstract_skeleton.strip(),
                    norm_primary_l1,
                    json.dumps(norm_secondary_l1s, ensure_ascii=False),
                    diag.difficulty.C_concept_count.score,
                    diag.difficulty.S_chain_length.score,
                    diag.difficulty.T_trickiness.score,
                    diag.difficulty.L_computation_load.score,
                    int(has_images),
                    cas_images_str,
                    json.dumps(sorted({s.raw_l1 for s in diag.steps}), ensure_ascii=False),
                    json.dumps(sorted({s.raw_l2 for s in diag.steps}), ensure_ascii=False),
                    json.dumps(diag.thinking_methods, ensure_ascii=False),
                    json.dumps(diag.classic_motifs, ensure_ascii=False),
                    json.dumps(diag.trap_tags, ensure_ascii=False),
                    json.dumps(diag.difficulty.model_dump(), ensure_ascii=False),
                ),
            )

            # 2. 写入双盲 stage1_vlm_runs + question_solutions
            diag_ver = _resolve_diagnose_prompt_version(config_dir, eff_gs)
            conn.execute(
                """
                INSERT INTO stage1_vlm_runs(
                    question_uid, role, model_name, prompt_version, input_hash, run_status,
                    visual_facts, method_summary, solution_markdown, final_answers_json,
                    sympy_code, sympy_status
                )
                VALUES (?, 'PRIMARY', 'external_diagnose', ?, ?, 'OK', ?, ?, ?, ?, ?, ?);
                """,
                (
                    uid,
                    diag_ver,
                    raw_content_hash,
                    diag.visual_facts,
                    diag.method_summary,
                    diag.solution_markdown,
                    answers_json,
                    diag.sympy_code,
                    eq_dual.sympy_status,
                ),
            )
            conn.execute(
                """
                INSERT INTO stage1_vlm_runs(
                    question_uid, role, model_name, prompt_version, input_hash, run_status,
                    visual_facts, method_summary, solution_markdown, final_answers_json
                )
                VALUES (?, 'SECONDARY', ?, ?, ?, 'OK', ?, ?, ?, ?);
                """,
                (
                    uid,
                    settings.llm.vlm_secondary.model or "mock-vlm-secondary",
                    STAGE1_PROMPT_VERSION,
                    raw_content_hash,
                    sec_out.visual_facts,
                    sec_out.method_summary,
                    sec_out.solution_markdown,
                    json.dumps([p.model_dump() for p in sec_parts], ensure_ascii=False),
                ),
            )
            conn.execute("DELETE FROM question_solutions WHERE question_uid = ?;", (uid,))
            conn.execute(
                """
                INSERT INTO question_solutions(
                    solution_id, question_uid, branch_id, is_optimal, source_origin,
                    method_summary, solution_markdown, sympy_code, sympy_verified
                )
                VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', ?, ?, ?, ?);
                """,
                (
                    f"{uid}::branch_1",
                    uid,
                    diag.method_summary,
                    diag.solution_markdown,
                    diag.sympy_code,
                    1 if eq_dual.sympy_status == "PASSED" else 0,
                ),
            )

            # 3. Stage 3c/3d 挂载：对齐当前词表（若有）并写入步骤 + 概念
            node_ids_mounted: list[str] = []
            conn.execute("DELETE FROM question_steps WHERE question_uid = ? AND branch_id = 'branch_1';", (uid,))
            for st in diag.steps:
                l1_m = st.raw_l1.strip() or norm_primary_l1
                l2_m = st.raw_l2.strip()
                if tax_lookup is not None and tax_spec is not None:
                    resolved_pair = tax_lookup.resolve_l2(l1_m, l2_m)
                    if resolved_pair:
                        l1_m, l2_m = resolved_pair
                    else:
                        resolved_l1 = tax_lookup.resolve_l1(l1_m)
                        if resolved_l1:
                            l1_m = resolved_l1
                        elif norm_primary_l1:
                            l1_m = norm_primary_l1
                        elif tax_spec.modules:
                            l1_m = tax_spec.modules[0].name
                        l2_fb = _best_fallback_l2(tax_lookup, l1_m, l2_m)
                        if tax_status == "LOCKED" and l2_m:
                            conn.execute(
                                """
                                INSERT INTO taxonomy_candidates(
                                    grade_subject, base_taxonomy_version, level, parent_l1, proposed_name, example_question_uids_json
                                )
                                VALUES (?, ?, 'L2', ?, ?, ?);
                                """,
                                (eff_gs, tax_ver, l1_m, l2_m, json.dumps([uid], ensure_ascii=False)),
                            )
                        l2_m = l2_fb

                l3_op = st.raw_l3.strip() or l2_m or "通用推导"
                nid_seed = f"kn:{scope_slug}:{tax_ver or 'v0'}:{l1_m}:{l2_m}:{l3_op}"
                nid = f"kn_{hashlib.sha256(nid_seed.encode()).hexdigest()[:16]}"
                row_kn = conn.execute(
                    """
                    SELECT node_id FROM knowledge_nodes
                    WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3'
                      AND l1_module = ? AND l2_entity = ? AND l3_operator = ? AND redirect_to_id IS NULL
                    LIMIT 1;
                    """,
                    (eff_gs, tax_ver or "draft-0", l1_m, l2_m, l3_op),
                ).fetchone()
                if row_kn:
                    nid = str(row_kn["node_id"])
                else:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_nodes(
                            node_id, grade_subject, taxonomy_version, node_level,
                            l1_module, l2_entity, l3_operator
                        )
                        VALUES (?, ?, ?, 'L3', ?, ?, ?);
                        """,
                        (nid, eff_gs, tax_ver or "draft-0", l1_m, l2_m, l3_op),
                    )
                canon_nid = find_canonical_node_id(conn, nid) or nid
                node_ids_mounted.append(canon_nid)
                conn.execute(
                    """
                    INSERT INTO question_steps(
                        step_id, question_uid, branch_id, step_index, sub_goal, core_formula, hint,
                        raw_l1, raw_l2, raw_l3, m_l1, m_l2, m_knowledge_node_id
                    )
                    VALUES (?, ?, 'branch_1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        f"{uid}::branch_1::{st.step_index}",
                        uid,
                        st.step_index,
                        st.sub_goal,
                        st.core_formula,
                        st.hint,
                        st.raw_l1,
                        st.raw_l2,
                        st.raw_l3,
                        l1_m,
                        l2_m,
                        canon_nid,
                    ),
                )

            conn.execute("DELETE FROM question_concepts WHERE question_uid = ? AND origin = 'MACHINE';", (uid,))
            for ckind, citems in (
                ("METHOD", diag.thinking_methods),
                ("MOTIF", diag.classic_motifs),
                ("TRAP", diag.trap_tags),
            ):
                for cname in citems:
                    if not cname.strip():
                        continue
                    cid = _get_or_create_concept(conn, kind=ckind, name=cname)
                    ccid = find_canonical_concept_id(conn, cid) or cid
                    conn.execute(
                        "INSERT OR IGNORE INTO question_concepts(question_uid, concept_id, kind, origin) VALUES (?, ?, ?, 'MACHINE');",
                        (uid, ccid, ckind),
                    )

            # 4. 若提供了 --answer 且与双盲一致，写入 HUMAN_VERIFIED PI_CLI 覆写
            if human_verified_by_known:
                bnd = f"bnd_{uuid.uuid4().hex[:12]}"
                for fname, fval in (
                    ("final_answers_json", answers_json),
                    ("verification_status", "HUMAN_VERIFIED"),
                ):
                    conn.execute(
                        "UPDATE human_overrides SET is_active = 0 WHERE question_uid = ? AND field_name = ? AND is_active = 1;",
                        (uid, fname),
                    )
                    conn.execute(
                        """
                        INSERT INTO human_overrides(
                            question_uid, field_name, override_value, reason, source, bundle_id, base_fingerprint
                        )
                        VALUES (?, ?, ?, 'KNOWN_ANSWER_MATCH', 'PI_CLI', ?, ?);
                        """,
                        (uid, fname, fval, bnd, raw_content_hash),
                    )

            # 5. compile_state(S1..S3D) + effective.recompute + S4 初始状态 + 不可服务清理旧机器边 + FTS5 + 可选 --log-wrong
            for stg in ("S1", "S2", "S3C", "S3D"):
                conn.execute(
                    """
                    INSERT INTO compile_state(question_uid, stage, status, input_hash, updated_at)
                    VALUES (?, ?, 'OK', ?, datetime('now'))
                    ON CONFLICT(question_uid, stage) DO UPDATE SET
                        status = excluded.status, input_hash = excluded.input_hash, updated_at = datetime('now');
                    """,
                    (uid, stg, raw_content_hash),
                )

            effective.recompute(conn, uid)
            q_check = conn.execute(
                "SELECT eff_is_servable, eff_question_text, eff_visual_facts, eff_abstract_skeleton FROM questions WHERE question_uid = ?;",
                (uid,),
            ).fetchone()
            is_servable = bool(q_check and q_check["eff_is_servable"] == 1)
            s4_init_status = "PENDING" if is_servable else "BLOCKED"
            conn.execute(
                """
                INSERT INTO compile_state(question_uid, stage, status, input_hash, updated_at)
                VALUES (?, 'S4', ?, ?, datetime('now'))
                ON CONFLICT(question_uid, stage) DO UPDATE SET
                    status = excluded.status, input_hash = excluded.input_hash, updated_at = datetime('now');
                """,
                (uid, s4_init_status, raw_content_hash),
            )
            if not is_servable:
                conn.execute(
                    """
                    DELETE FROM question_relations
                    WHERE (source_question_uid = ? OR target_question_uid = ?)
                      AND confidence_source != 'HUMAN_VERIFIED';
                    """,
                    (uid, uid),
                )

            repository.refresh_search_index(conn, uid)

            if log_wrong:
                cur = conn.execute(
                    """
                    INSERT INTO practice_log(learner_id, question_uid, outcome, score_ratio, user_note)
                    VALUES (?, ?, 'WRONG', 0.0, ?);
                    """,
                    (learner_id, uid, user_note),
                )
                logged_id = int(cur.lastrowid or 0)

        # 主事务提交后立即双写人工资产镜像，防止后续向量/织网异常导致 DB 与镜像漂移
        human_mirror.export_snapshot(
            conn,
            human_dir=human_dir,
            include_practice_log=settings.human_mirror.include_practice_log,
        )

        # 6. 若可出题，追加 .npz 向量与 weave_single_question 增量建五边，成功后再将 S4 置为 OK
        if is_servable and q_check is not None:
            store = VectorStore(vectors_dir)
            emb_client = EmbeddingClient(settings, conn=None, use_mock=eff_mock)
            try:
                s_txt = (str(q_check["eff_question_text"] or "") + "\n" + str(q_check["eff_visual_facts"] or "")).strip()
                k_txt = str(q_check["eff_abstract_skeleton"] or q_check["eff_question_text"] or "").strip()
                await store.upsert_vector("surface", uid, s_txt, emb_client, scope_slug=scope_slug)
                await store.upsert_vector("skeleton", uid, k_txt, emb_client, scope_slug=scope_slug)
            finally:
                await emb_client.aclose()

            if store.has_quantiles(scope_slug, "surface") and store.has_quantiles(scope_slug, "skeleton"):
                with conn:
                    w_res = weave_single_question(
                        conn, store, question_uid=uid, topology_cfg=settings.topology
                    )
                    conn.execute(
                        """
                        INSERT INTO compile_state(question_uid, stage, status, input_hash, updated_at)
                        VALUES (?, 'S4', 'OK', ?, datetime('now'))
                        ON CONFLICT(question_uid, stage) DO UPDATE SET
                            status = excluded.status, input_hash = excluded.input_hash, updated_at = datetime('now');
                        """,
                        (uid, raw_content_hash),
                    )
                woven_edges = int(w_res.get("written", 0))
            else:
                weave_warning = "向量分位表尚未初始化，已暂缓五边增量织网（可执行 reindex --quantiles 后重织）"

    return {
        "dry_run": False,
        "question_uid": uid,
        "source_id": source_id,
        "grade_subject": eff_gs,
        "verification_status": "HUMAN_VERIFIED" if human_verified_by_known else ver_status,
        "eff_is_servable": is_servable,
        "base_elo": diff_res.base_elo,
        "tier": diff_res.tier,
        "woven_edges": woven_edges,
        "log_id": logged_id,
        "warning": weave_warning,
    }
