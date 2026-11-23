"""外部新错题实时诊断与增量入库服务（DESIGN.md §5.3）。"""

from __future__ import annotations

import hashlib
import json
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
from shuati.verify.sympy_sandbox import run_verify_script

_DRAFT_TTL_SECONDS = 7 * 86400


def _prune_expired_drafts(drafts_dir: Path) -> None:
    if not drafts_dir.exists():
        return
    now = time.time()
    for p in drafts_dir.glob("*.json"):
        try:
            if now - p.stat().st_mtime > _DRAFT_TTL_SECONDS:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _build_taxonomy_enum_summary(conn: sqlite3.Connection, grade_subject: str) -> str:
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


def _parse_single_known_answer(raw_answer: str, reference_parts: list[AnswerPart]) -> list[AnswerPart]:
    """将命令行 --answer 解析为与 reference_parts 对齐的 AnswerPart 列表用于一致性比对。"""
    ref0 = reference_parts[0]
    cleaned = raw_answer.strip()
    if len(reference_parts) == 1:
        if ref0.answer_type == "CHOICE":
            return [
                AnswerPart(
                    label=ref0.label,
                    answer_type="CHOICE",
                    verify_mode="EXACT",
                    collection_mode="SINGLE",
                    answer=cleaned,
                    choice_label=normalize_choice_label(cleaned) or cleaned.upper(),
                )
            ]
        if ref0.answer_type == "SYMBOL_FILL" and ref0.collection_mode == "SINGLE":
            return [
                AnswerPart(
                    label=ref0.label,
                    answer_type="SYMBOL_FILL",
                    verify_mode="EXACT",
                    collection_mode="SINGLE",
                    answer=cleaned,
                    canonical=normalize_symbol_fill(cleaned) or cleaned,
                )
            ]
        if ref0.verify_mode == "SYMBOLIC" and ref0.collection_mode == "SINGLE":
            return [
                AnswerPart(
                    label=ref0.label,
                    answer_type=ref0.answer_type,
                    verify_mode="SYMBOLIC",
                    collection_mode="SINGLE",
                    answer=cleaned,
                    sympy_expr=cleaned,
                )
            ]
        if ref0.verify_mode == "QUALITATIVE":
            return [
                AnswerPart(
                    label=ref0.label,
                    answer_type=ref0.answer_type,
                    verify_mode="QUALITATIVE",
                    collection_mode="SINGLE",
                    answer=cleaned,
                )
            ]

    # 多小问分号切分兜底
    segs = [s.strip() for s in cleaned.replace("；", ";").split(";") if s.strip()]
    out: list[AnswerPart] = []
    for idx, ref in enumerate(reference_parts):
        val = segs[idx] if idx < len(segs) else cleaned
        if ref.answer_type == "CHOICE":
            out.append(
                AnswerPart(
                    label=ref.label,
                    answer_type="CHOICE",
                    verify_mode="EXACT",
                    collection_mode="SINGLE",
                    answer=val,
                    choice_label=normalize_choice_label(val) or val.upper(),
                )
            )
        elif ref.verify_mode == "SYMBOLIC" and ref.collection_mode == "SINGLE":
            out.append(
                AnswerPart(
                    label=ref.label,
                    answer_type=ref.answer_type,
                    verify_mode="SYMBOLIC",
                    collection_mode="SINGLE",
                    answer=val,
                    sympy_expr=val,
                )
            )
        else:
            out.append(
                AnswerPart(
                    label=ref.label,
                    answer_type="TEXT",
                    verify_mode="QUALITATIVE",
                    collection_mode="SINGLE",
                    answer=val,
                )
            )
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
        if not src_p.exists():
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
        msgs = build_external_diagnose_messages(
            grade_subject=grade_subject,
            question_text=norm_text,
            taxonomy_enum_text=tax_enum,
            preprocessed_image_paths=preprocessed_imgs,
            anchors_prompt_text=anchors_text,
        )
        gw = LLMGateway(gw_settings, conn=None)
        try:
            res = await gw.call_structured(
                slot="vlm_primary",
                stage="DIAGNOSE",
                messages=msgs,
                schema=ExternalDiagnosisOutput,
                prompt_version="diag_v2",
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
            JOIN concepts c ON c.concept_id = qc.concept_id
            WHERE qc.kind = 'TRAP' AND c.name IN ({t_ph})
            LIMIT 10;
            """,
            diag_traps,
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
    draft_file.write_text(json.dumps(draft_payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
    raw_content_hash = hashlib.sha256(
        json.dumps({"text": eff_text, "gs": eff_gs, "imgs": [str(p.name) for p in raw_imgs]}, sort_keys=True).encode("utf-8")
    ).hexdigest()
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
            if not src_p.exists():
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
        s1_msgs = build_stage1_messages(s1_inp, role="SECONDARY", assets_dir=Path(assets_dir))
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
        jmsgs = build_qualitative_judge_messages(lbl, a_str, b_str, question_text=eff_text)
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

    with CompileLock(lock_path):
        with conn:
            # 1. 写入 raw_questions & questions
            conn.execute(
                """
                INSERT INTO raw_questions(
                    question_uid, grade_subject, source_type, source_collection, source_id,
                    raw_question, raw_images_json, content_hash, plain_fingerprint
                )
                VALUES (?, ?, 'EXTERNAL_MISTAKE', 'external_mistake', ?, ?, ?, ?, ?)
                ON CONFLICT(question_uid) DO UPDATE SET
                    raw_question = excluded.raw_question,
                    raw_images_json = excluded.raw_images_json,
                    content_hash = excluded.content_hash;
                """,
                (
                    uid,
                    eff_gs,
                    source_id,
                    eff_text,
                    json.dumps([str(p) for p in raw_imgs], ensure_ascii=False),
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
                    diag.primary_l1_module.strip(),
                    json.dumps(diag.secondary_l1_modules, ensure_ascii=False),
                    diag.difficulty.C_concept_count.score,
                    diag.difficulty.S_chain_length.score,
                    diag.difficulty.T_trickiness.score,
                    diag.difficulty.L_computation_load.score,
                    int(has_images),
                    json.dumps(cas_names, ensure_ascii=False),
                    json.dumps(sorted({s.raw_l1 for s in diag.steps}), ensure_ascii=False),
                    json.dumps(sorted({s.raw_l2 for s in diag.steps}), ensure_ascii=False),
                    json.dumps(diag.thinking_methods, ensure_ascii=False),
                    json.dumps(diag.classic_motifs, ensure_ascii=False),
                    json.dumps(diag.trap_tags, ensure_ascii=False),
                    json.dumps(diag.difficulty.model_dump(), ensure_ascii=False),
                ),
            )

            # 2. 写入双盲 stage1_vlm_runs + question_solutions
            conn.execute(
                """
                INSERT INTO stage1_vlm_runs(
                    question_uid, role, model_name, prompt_version, input_hash, run_status,
                    visual_facts, method_summary, solution_markdown, final_answers_json,
                    sympy_code, sympy_status
                )
                VALUES (?, 'PRIMARY', 'external_diagnose', 'diag_v1', ?, 'OK', ?, ?, ?, ?, ?, ?);
                """,
                (
                    uid,
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
            tax_status = str(gs_row["taxonomy_status"] or "NONE")
            tax_ver = str(gs_row["taxonomy_version"] or "draft-0")
            tax_spec = None
            tax_lookup = None
            try:
                _, tax_ver, tax_spec, tax_lookup = _load_active_taxonomy(conn, eff_gs)
            except PreconditionError:
                tax_spec = None
                tax_lookup = None
            node_ids_mounted: list[str] = []
            conn.execute("DELETE FROM question_steps WHERE question_uid = ? AND branch_id = 'branch_1';", (uid,))
            for st in diag.steps:
                l1_m = st.raw_l1.strip() or diag.primary_l1_module.strip()
                l2_m = st.raw_l2.strip()
                if tax_lookup is not None and tax_spec is not None:
                    resolved_pair = tax_lookup.resolve_l2(l1_m, l2_m)
                    if resolved_pair:
                        l1_m, l2_m = resolved_pair
                    else:
                        resolved_l1 = tax_lookup.resolve_l1(l1_m)
                        if resolved_l1:
                            l1_m = resolved_l1
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

            # 5. compile_state 全阶段写入 + effective.recompute + FTS5 刷新
            for stg in ("S1", "S2", "S3C", "S3D", "S4"):
                stg_status = "OK" if (stg != "S4" or ver_status == "VERIFIED_CONSENSUS" or human_verified_by_known) else "BLOCKED"
                conn.execute(
                    """
                    INSERT INTO compile_state(question_uid, stage, status, input_hash, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(question_uid, stage) DO UPDATE SET
                        status = excluded.status, input_hash = excluded.input_hash, updated_at = datetime('now');
                    """,
                    (uid, stg, stg_status, raw_content_hash),
                )

            effective.recompute(conn, uid)
            repository.refresh_search_index(conn, uid)

        # 6. 若可出题，追加 .npz 向量与 weave_single_question 增量建五边
        q_check = conn.execute(
            "SELECT eff_is_servable, eff_question_text, eff_visual_facts, eff_abstract_skeleton FROM questions WHERE question_uid = ?;",
            (uid,),
        ).fetchone()
        is_servable = bool(q_check and q_check["eff_is_servable"] == 1)
        woven_edges = 0
        weave_warning: str | None = None

        if is_servable:
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
                woven_edges = int(w_res.get("written", 0))
            else:
                weave_warning = "向量分位表尚未初始化，已暂缓五边增量织网（可执行 reindex --quantiles 后重织）"

        # 7. 可选 --log-wrong
        logged_id: int | None = None
        if log_wrong:
            cur = conn.execute(
                """
                INSERT INTO practice_log(learner_id, question_uid, outcome, score_ratio, user_note)
                VALUES (?, ?, 'WRONG', 0.0, ?);
                """,
                (learner_id, uid, user_note),
            )
            logged_id = int(cur.lastrowid or 0)
            conn.commit()

        human_mirror.export_snapshot(
            conn,
            human_dir=human_dir,
            include_practice_log=settings.human_mirror.include_practice_log,
        )

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
