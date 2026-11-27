"""外部新错题实时诊断与增量入库服务（DESIGN.md §5.3）。"""

from __future__ import annotations

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
from shuati.llm.gateway import LLMGateway
from shuati.llm.prompt_builder import (
    STAGE1_PROMPT_VERSION,
    build_external_diagnose_messages,
    build_qualitative_judge_messages,
    build_stage1_messages,
)
from shuati.llm.seam_loader import load_prompt_seams
from shuati.models.domain import AnswerPart
from shuati.models.schemas import (
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

    # 3. 内存 1 vs N 向量检索（完全只读，绝不修改 .npz）
    servable_rows = conn.execute(
        """
        SELECT q.question_uid, r.source_id, q.eff_question_text, q.eff_options_json, q.has_images,
               q.eff_base_elo, q.eff_tier, q.eff_primary_l1_module, q.eff_abstract_skeleton
        FROM questions q
        JOIN raw_questions r ON r.question_uid = q.question_uid
        WHERE q.grade_subject = ? AND q.is_retired = 0 AND q.eff_is_excluded = 0 AND q.eff_is_servable = 1;
        """,
        (grade_subject,),
    ).fetchall()
    servable_map = {str(r["question_uid"]): dict(r) for r in servable_rows}
    servable_uids = set(servable_map.keys())

    store = VectorStore(vectors_dir)
    surf_hits: list[tuple[str, float]] = []
    skel_hits: list[tuple[str, float]] = []

    surf_text = (norm_text + ("\n" + diag.visual_facts if diag.visual_facts else "")).strip()
    skel_text = diag.abstract_skeleton.strip() or norm_text

    if servable_uids and (
        store.npz_path("surface", scope_slug).exists()
        or store.npz_path("skeleton", scope_slug).exists()
    ):
        emb_client = EmbeddingClient(settings, conn=None, use_mock=eff_mock)
        try:
            q_vecs = await emb_client.embed_texts([surf_text, skel_text], stage="DIAGNOSE_EMBED")
        finally:
            await emb_client.aclose()
        surf_hits = store.query_top_k(
            "surface", q_vecs[0], scope_slug=scope_slug, top_k=30, allowed_uids=servable_uids
        )
        skel_hits = store.query_top_k(
            "skeleton", q_vecs[1], scope_slug=scope_slug, top_k=30, allowed_uids=servable_uids
        )

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

    # 归纳 3 类关联推荐
    scaffolds: list[dict[str, Any]] = []
    isomorphics: list[dict[str, Any]] = []
    contrastive_traps: list[dict[str, Any]] = []
    used_recs: set[str] = set()

    for uid, sim in skel_hits:
        q_elo = int(servable_map[uid]["eff_base_elo"] or 1500)
        if q_elo <= diff_res.base_elo - 200 and len(scaffolds) < 3:
            scaffolds.append(_card_item(uid, sim, "低 200+ Elo 骨架近似前置铺垫题"))
            used_recs.add(uid)
        elif abs(q_elo - diff_res.base_elo) <= 220 and uid not in used_recs and len(isomorphics) < 3:
            isomorphics.append(_card_item(uid, sim, "同骨架跨域同构变式题"))
            used_recs.add(uid)

    for uid, sim in surf_hits:
        if uid not in used_recs and len(contrastive_traps) < 3:
            contrastive_traps.append(_card_item(uid, sim, "表皮近邻辨析 / 同坑对照候选"))
            used_recs.add(uid)

    # 若有非笔误 TRAP 标签，补充库内同 TRAP 排雷题
    diag_traps = [t for t in diag.trap_tags if t != "TRAP_ARITHMETIC_SLIP"]
    if diag_traps and len(contrastive_traps) < 5:
        t_ph = ",".join("?" for _ in diag_traps)
        trap_rows = conn.execute(
            f"""
            SELECT DISTINCT qc.question_uid
            FROM question_concepts qc
            JOIN questions q ON q.question_uid = qc.question_uid
            JOIN concepts member ON member.concept_id = qc.concept_id
            JOIN concepts master
              ON (member.concept_id = master.concept_id OR member.redirect_to_id = master.concept_id)
            WHERE qc.kind = 'TRAP'
              AND master.kind = 'TRAP'
              AND master.redirect_to_id IS NULL
              AND master.name IN ({t_ph})
              AND q.grade_subject = ?
              AND q.is_retired = 0
              AND q.eff_is_excluded = 0
              AND q.eff_is_servable = 1
            ORDER BY qc.question_uid ASC
            LIMIT 20;
            """,
            [*diag_traps, grade_subject],
        ).fetchall()
        for tr in trap_rows:
            tuid = str(tr["question_uid"])
            if tuid in servable_map and tuid not in used_recs and len(contrastive_traps) < 5:
                contrastive_traps.append(_card_item(tuid, 0.0, f"共享高危陷阱 {','.join(diag_traps)}"))
                used_recs.add(tuid)

    duplicate_candidates = find_duplicate_candidates(
        conn,
        grade_subject=grade_subject,
        norm_text=norm_text,
        image_count=len(preprocessed_imgs),
        surf_hits=surf_hits,
        servable_map=servable_map,
    )

    content_hash = compute_diagnose_content_hash(grade_subject, norm_text, raw_img_paths)

    draft_payload = {
        "draft_id": draft_id,
        "grade_subject": grade_subject,
        "question_text": norm_text,
        "preprocessed_images": [str(p.resolve()) for p in preprocessed_imgs],
        "known_answer": known_answer,
        "known_answer_match": known_answer_match,
        "content_hash": content_hash,
        "diagnosis": diag.model_dump(),
        "base_elo": diff_res.base_elo,
        "tier": diff_res.tier,
        "tier_rank": diff_res.tier_rank,
        "duplicate_candidates": duplicate_candidates,
        "recommendations": {
            "scaffold_foundations": scaffolds,
            "isomorphic_variants": isomorphics,
            "contrastive_and_traps": contrastive_traps,
        },
        "unverified_single_pass": True,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    draft_file = draft_root / f"{draft_id}.json"
    _atomic_write_text(draft_file, json.dumps(draft_payload, ensure_ascii=False, indent=2))

    return {
        "draft_id": draft_id,
        "draft_file": str(draft_file.resolve()),
        "unverified_single_pass": True,
        "grade_subject": grade_subject,
        "base_elo": diff_res.base_elo,
        "tier": diff_res.tier,
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
        "recommendations": {
            "scaffold_foundations": scaffolds,
            "isomorphic_variants": isomorphics,
            "contrastive_and_traps": contrastive_traps,
        },
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
