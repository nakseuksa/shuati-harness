"""Stage 2 独立多模态深度拆解与四维打标流水线（D-Audit-4）。

支持三通道执行（mode = api / export / import）：
1. 前置门禁：校验 grade_subjects.anchors_ready == 1 与锚点 YAML 存在；
2. 候选筛选：仅处理已验真（VERIFIED_CONSENSUS / HUMAN_VERIFIED）且未退库/未排除题目；
3. D-Audit-4 强制原图与人工权威最高优先：
   - 查询激活且未挂起的 human_overrides（visual_facts / final_answers_json），注入最高权威提示；
   - has_images == 1 题目在 API 模式强制附带 EXIF 矫正原图并路由至多模态槽位 "vlm_primary"；
   - 纯文字题（has_images == 0）不传图并路由至 "text_llm"；
4. 确定性校验与落库：经 verify.draft_validator.validate_draft_payload(..., mode="STAGE2_ONLY", run_sympy=False)
   严格校验后，通过单写者事务写入 questions 机器列、question_steps、question_concepts、
   compile_state(stage='S2', status='OK')，调用 effective.recompute 与 refresh.propagate_stage_stale(..., 'S2')。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from shuati.config import Settings, resolve_subagent_model, validate_subagent_model_id
from shuati.core.taxonomy import format_taxonomy_enum, load_taxonomy_spec
from shuati.db import repository
from shuati.db.connection import CompileLock, PreconditionError, SingleWriterExecutor
from shuati.llm.gateway import LLMCallResult, LLMGateway, LLMGatewayError, SlotName
from shuati.llm.mocks import MOCK_RESPONSES
from shuati.llm.prompt_builder import STAGE2_PROMPT_VERSION, build_stage2_messages
from shuati.llm.seam_loader import load_prompt_seams
from shuati.models.schemas import Stage2OnlyOutput
from shuati.pipeline.subagent_bridge import (
    _atomic_write_text,
    export_task_batches,
    load_and_validate_results,
)
from shuati.pipeline.unified_import import (
    _format_sanitized_anchors_text,
    _load_blacklist_and_traps,
    _write_stage2_db,
    is_compile_locked,
)
from shuati.verify.draft_validator import validate_draft_payload


class Stage2BatchOutput(BaseModel):
    """Stage 2 脱敏批处理导出/回灌包装模型（items 内每题 Stage2OnlyOutput 携带 task_id）。"""

    items: list[Stage2OnlyOutput] = Field(default_factory=list)


def compute_stage2_input_hash(
    *,
    eff_question_text: str,
    eff_visual_facts: str,
    eff_final_answers_json: str,
    eff_options_json: str,
    cas_images: list[str],
    image_captions: list[str],
    has_images: bool,
    has_human_visual_override: bool,
    has_human_answer_override: bool,
    verified_method_summary: str,
    verified_solution_markdown: str,
    anchors_digest: str,
    model_name: str,
    prompt_version: str,
) -> str:
    """计算 Stage 2 输入指纹（严禁包含 question_uid / source_collection / raw_chapter）。"""
    payload = json.dumps(
        {
            "eff_question_text": eff_question_text,
            "eff_visual_facts": eff_visual_facts,
            "eff_final_answers_json": eff_final_answers_json,
            "eff_options_json": eff_options_json,
            "cas_images": cas_images,
            "image_captions": image_captions,
            "has_images": has_images,
            "has_human_visual_override": has_human_visual_override,
            "has_human_answer_override": has_human_answer_override,
            "verified_method_summary": verified_method_summary,
            "verified_solution_markdown": verified_solution_markdown,
            "anchors_digest": anchors_digest,
            "model_name": model_name,
            "prompt_version": prompt_version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_runtime_paths(
    conn: sqlite3.Connection,
    *,
    assets_dir: Path | str | None,
    lock_path: Path | str | None,
) -> tuple[Path, Path, Path]:
    row = conn.execute("PRAGMA database_list;").fetchone()
    db_file_str = str(row["file"]) if row is not None and row["file"] else ""
    db_file = Path(db_file_str) if db_file_str else Path("data/shuati.db")

    resolved_lock = Path(lock_path) if lock_path is not None else (db_file.parent / ".compile.lock")
    if assets_dir is not None:
        resolved_assets = Path(assets_dir)
    elif db_file.name == "shuati.db" and db_file.parent.name == "data":
        resolved_assets = Path("data/assets/images")
    else:
        resolved_assets = db_file.parent / "assets" / "images"

    if db_file.name == "shuati.db" and db_file.parent.name == "data":
        resolved_llm_raw = Path("data/llm_raw")
    else:
        resolved_llm_raw = db_file.parent / "llm_raw"

    return resolved_lock, resolved_assets, resolved_llm_raw


def _resolve_stage2_seam_meta(
    config_dir: Path,
    grade_subject: str,
    prompt_profile: str | None,
) -> tuple[str, str]:
    if (config_dir / "prompts" / "default.yaml").is_file() or (
        prompt_profile is not None and prompt_profile.strip()
    ):
        bundle = load_prompt_seams(
            config_dir=config_dir,
            grade_subject=grade_subject,
            profile=prompt_profile,
        )
        return (
            bundle.prompt_versions.get("stage2", STAGE2_PROMPT_VERSION),
            bundle.prompt_digest,
        )
    return STAGE2_PROMPT_VERSION, ""


def _build_stage2_taxonomy_enum(conn: sqlite3.Connection, gs_row: sqlite3.Row, grade_subject: str) -> str:
    tax_file_raw = str(gs_row["taxonomy_file"] or "").strip()
    tax_status = str(gs_row["taxonomy_status"] or "NONE")
    if tax_file_raw and tax_status in ("DRAFT", "LOCKED"):
        tax_path = Path(tax_file_raw)
        if tax_path.is_file():
            try:
                spec = load_taxonomy_spec(tax_path)
                return format_taxonomy_enum(spec)
            except Exception:
                pass

    rows = conn.execute(
        """
        SELECT DISTINCT l1_module, l2_entity
        FROM knowledge_nodes
        WHERE grade_subject = ? AND redirect_to_id IS NULL
        ORDER BY l1_module, l2_entity
        """,
        (grade_subject,),
    ).fetchall()
    if not rows:
        return ""
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(str(r["l1_module"]), []).append(str(r["l2_entity"]))
    return "\n".join(f"- {l1}: {', '.join(l2s)}" for l1, l2s in grouped.items())


def _fetch_human_override_flags(conn: sqlite3.Connection, uid: str) -> tuple[bool, bool]:
    rows = conn.execute(
        """
        SELECT field_name
        FROM human_overrides
        WHERE question_uid = ?
          AND field_name IN ('visual_facts', 'final_answers_json')
          AND is_active = 1
          AND is_suspended = 0
        """,
        (uid,),
    ).fetchall()
    fields = {str(r["field_name"]) for r in rows}
    return ("visual_facts" in fields, "final_answers_json" in fields)


def _fetch_optimal_solution(conn: sqlite3.Connection, uid: str) -> tuple[str, str]:
    row = conn.execute(
        """
        SELECT method_summary, solution_markdown
        FROM question_solutions
        WHERE question_uid = ?
        ORDER BY is_optimal DESC, solution_id ASC
        LIMIT 1
        """,
        (uid,),
    ).fetchone()
    if row is None:
        return "", ""
    return str(row["method_summary"] or ""), str(row["solution_markdown"] or "")


def _ensure_solution_branch1(conn: sqlite3.Connection, uid: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM question_solutions WHERE question_uid = ? AND branch_id = 'branch_1' LIMIT 1",
        (uid,),
    ).fetchone()
    if exists is not None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO question_solutions(
            solution_id, question_uid, branch_id, is_optimal, source_origin,
            method_summary, solution_markdown
        ) VALUES (?, ?, 'branch_1', 1, 'HUMAN', '', '')
        """,
        (f"{uid}::branch_1", uid),
    )


def _parse_json_dict(raw: str | None) -> dict[str, str]:
    try:
        obj = json.loads(raw or "{}")
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k): str(v) for k, v in obj.items()}


def _parse_json_list(raw: str | None) -> list[Any]:
    try:
        obj = json.loads(raw or "[]")
    except Exception:
        return []
    return obj if isinstance(obj, list) else []


def _per_item_authority_directive(
    *,
    task_id: str,
    has_human_visual_override: bool,
    has_human_answer_override: bool,
) -> str:
    """构造绑定至单题 task_id 作用域的 D-Audit-4 权威指令（避免同批多题互相污染）。"""
    lines: list[str] = []
    if has_human_visual_override:
        lines.append(
            f"[task_id={task_id}] 【D-Audit-4 人工视觉事实最高权威】本题的 `verified_visual_facts` 已由人工复核核定；"
            "若原图印刷模糊、阴影边界或标注细节与已核定 `verified_visual_facts` 存在歧义冲突，"
            "**必须无条件以人工核定的 `verified_visual_facts` 为准，严禁自行推翻重读**。"
        )
    if has_human_answer_override:
        lines.append(
            f"[task_id={task_id}] 【D-Audit-4 人工标准答案最高权威】本题的 `verified_final_answers` 已由人工复核核定；"
            "步骤拆解、核心公式与提示**必须无条件服从该人工标准答案**，严禁篡改答案或得出相反结论。"
        )
    if not lines:
        lines.append(
            f"[task_id={task_id}] 【已验真事实约束】本题无人工视觉/答案覆写，所有拆解步骤与难度评估必须忠于已核定的参考解答与标准答案，不得自行推翻终值。"
        )
    return "\n".join(lines)


def _build_batch_system_prompt(chunk_items: list[dict[str, Any]]) -> str:
    """构造批级 system prompt：多题批剥离首题私有覆写声明，保留通用规则与批级 D-Audit-4 总原则。"""
    base_system = str(chunk_items[0]["messages"][0]["content"])
    if len(chunk_items) > 1:
        # 剥离首题私有权威段落（第 2 行起至空行前的单题权威说明），避免误传染同批其它题目
        head_sep = "\n\n请严格输出一个合法的 JSON 对象"
        if head_sep in base_system:
            first_line = base_system.split("\n", 1)[0]
            rest = base_system.split(head_sep, 1)[1]
            any_vis = any(bool(it.get("has_human_visual_override")) for it in chunk_items)
            any_ans = any(bool(it.get("has_human_answer_override")) for it in chunk_items)
            batch_rules: list[str] = [
                "【逐题 D-Audit-4 权威隔离总原则】本批次各题的人工覆写状态相互独立，"
                "必须严格遵守每道题目块（`task_id` 作用域）内各自声明的权威指令，严禁跨题混淆。"
            ]
            if any_vis:
                batch_rules.append(
                    "【批级 D-Audit-4 人工视觉事实最高权威】凡在对应题目块内标注有人工视觉事实最高权威的题目，"
                    "若原图印刷模糊、阴影边界或标注细节与已核定 `verified_visual_facts` 存在歧义冲突，"
                    "**必须无条件以人工核定的 `verified_visual_facts` 为准，严禁自行推翻重读**。"
                )
            if any_ans:
                batch_rules.append(
                    "【批级 D-Audit-4 人工标准答案最高权威】凡在对应题目块内标注有人工标准答案最高权威的题目，"
                    "步骤拆解、核心公式与提示**必须无条件服从该人工标准答案**，严禁篡改答案或得出相反结论。"
                )
            base_system = f"{first_line}\n" + "\n".join(batch_rules) + head_sep + rest

    return (
        f"{base_system}\n\n"
        "【批处理包装要求（Stage2BatchOutput）】\n"
        "本批次包含一道或多道独立题目（每题以 `task_id` 唯一标识）。\n"
        '请严格输出单个合法 JSON 对象：`{"items": [ <每题对应的 Stage2OnlyOutput 对象，原样回填 task_id> ]}`，'
        "确保本批次全部 `task_id` 100% 覆盖且无任何遗漏。"
    )


def _build_batch_messages(chunk_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将同批多题的单题脱敏 Stage 2 消息组合为 Stage2BatchOutput 批处理消息（逐题权威隔离）。"""
    batch_system = _build_batch_system_prompt(chunk_items)

    if len(chunk_items) == 1:
        item0 = chunk_items[0]
        directive = _per_item_authority_directive(
            task_id=str(item0["task_id"]),
            has_human_visual_override=bool(item0.get("has_human_visual_override")),
            has_human_answer_override=bool(item0.get("has_human_answer_override")),
        )
        content0 = item0["messages"][1]["content"]
        if isinstance(content0, list):
            user_content: Any = [{"type": "text", "text": directive}, *content0]
        else:
            user_content = f"{directive}\n\n{content0}"
        return [
            {"role": "system", "content": batch_system},
            {"role": "user", "content": user_content},
        ]

    has_multimodal_part = any(
        isinstance(item["messages"][1]["content"], list) for item in chunk_items
    )
    if not has_multimodal_part:
        blocks: list[str] = []
        for idx, item in enumerate(chunk_items, start=1):
            directive = _per_item_authority_directive(
                task_id=str(item["task_id"]),
                has_human_visual_override=bool(item.get("has_human_visual_override")),
                has_human_answer_override=bool(item.get("has_human_answer_override")),
            )
            blocks.append(
                f"### 批次题目 {idx}（task_id={item['task_id']}）\n{directive}\n\n{item['messages'][1]['content']}"
            )
        return [
            {"role": "system", "content": batch_system},
            {"role": "user", "content": "\n\n========================================\n\n".join(blocks)},
        ]

    merged_parts: list[dict[str, Any]] = []
    for idx, item in enumerate(chunk_items, start=1):
        directive = _per_item_authority_directive(
            task_id=str(item["task_id"]),
            has_human_visual_override=bool(item.get("has_human_visual_override")),
            has_human_answer_override=bool(item.get("has_human_answer_override")),
        )
        merged_parts.append(
            {
                "type": "text",
                "text": f"### 批次题目 {idx}（task_id={item['task_id']}）\n{directive}",
            }
        )
        content = item["messages"][1]["content"]
        if isinstance(content, list):
            merged_parts.extend(content)
        else:
            merged_parts.append({"type": "text", "text": str(content)})

    return [
        {"role": "system", "content": batch_system},
        {"role": "user", "content": merged_parts},
    ]


def _build_mock_stage2_fallback(task_id: str) -> Stage2OnlyOutput:
    return Stage2OnlyOutput.model_validate(
        {
            "task_id": task_id,
            "steps": [
                {
                    "step_index": 1,
                    "sub_goal": "提取题目已知量与核心数量关系",
                    "core_formula": "y = f(x)",
                    "hint": "先梳理已知条件之间的等价约束关系",
                    "raw_l1": "平面几何",
                    "raw_l2": "等高模型",
                    "raw_l3": "数量关系建立模型",
                }
            ],
            "abstract_skeleton": "根据已知条件建立核心等量关系并推演目标量",
            "primary_l1_module": "平面几何",
            "secondary_l1_modules": [],
            "thinking_methods": ["数形结合"],
            "classic_motifs": ["等高模型"],
            "trap_tags": [],
            "difficulty": {
                "C_concept_count": {"evidence": "单一核心模型直接推理", "score": 2},
                "S_chain_length": {"evidence": "两步条件转化推演", "score": 1},
                "T_trickiness": {"evidence": "条件表述清晰无隐藏陷阱", "score": 1},
                "L_computation_load": {"evidence": "常规基础算术演算", "score": 1},
            },
        }
    )


async def run_stage2_decompose_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    grade_subject: str = "小学奥数",
    mode: Literal["api", "export", "import"] = "api",
    only_uids: list[str] | None = None,
    force: bool = False,
    batch_size: int = 10,
    tasks_dir: Path | str = Path("data/unified/stage2"),
    results_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    config_dir: Path | str = Path("config"),
    assets_dir: Path | str | None = None,
    subagent_model: str | None = None,
    prompt_profile: str | None = None,
    overwrite_pending: bool = False,
    use_mock: bool = False,
) -> dict[str, Any]:
    """独立多模态 Stage 2 深度拆解与四维打标流水线（支持 api / export / import）。"""
    norm_mode = mode.strip().lower()
    if norm_mode not in ("api", "export", "import"):
        raise PreconditionError(f"PRECONDITION_FAILED: 不支持的 Stage 2 执行模式: {mode!r}")

    cfg_dir = Path(config_dir)
    resolved_lock, resolved_assets, resolved_llm_raw = _resolve_runtime_paths(
        conn, assets_dir=assets_dir, lock_path=lock_path
    )

    gs_row = conn.execute(
        """
        SELECT grade_subject, slug, anchors_file, anchors_ready, taxonomy_status, taxonomy_file
        FROM grade_subjects
        WHERE grade_subject = ?
        """,
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        raise PreconditionError(f"PRECONDITION_FAILED: 未注册学段学科 '{grade_subject}'")
    if gs_row["anchors_ready"] != 1 or not gs_row["anchors_file"]:
        raise PreconditionError(
            f"PRECONDITION_FAILED: '{grade_subject}' 的 Stage 1.5 黄金锚点尚未就绪，请先运行 calibrate-anchors"
        )
    anchors_path = Path(str(gs_row["anchors_file"]))
    if not anchors_path.is_file():
        raise PreconditionError(f"PRECONDITION_FAILED: 锚点文件不存在: {anchors_path}")

    blacklist, seed_traps = _load_blacklist_and_traps(cfg_dir)
    anchors_prompt_text = _format_sanitized_anchors_text(conn, anchors_path, blacklist, seed_traps)
    anchors_digest = hashlib.sha256(anchors_prompt_text.encode("utf-8")).hexdigest()[:16]
    taxonomy_enum_text = _build_stage2_taxonomy_enum(conn, gs_row, grade_subject)
    p_ver, p_digest = _resolve_stage2_seam_meta(cfg_dir, grade_subject, prompt_profile)

    target_uids: list[str] | None = None
    if only_uids is not None:
        target_uids = list(dict.fromkeys(repository.resolve_question_uid(conn, u) for u in only_uids))

    where_clauses = [
        "q.is_retired = 0",
        "q.eff_is_excluded = 0",
        "q.eff_verification_status IN ('VERIFIED_CONSENSUS', 'HUMAN_VERIFIED')",
        "q.grade_subject = ?",
    ]
    params: list[Any] = [grade_subject]
    if target_uids is not None:
        if not target_uids:
            return {
                "mode": norm_mode,
                "grade_subject": grade_subject,
                "total_candidates": 0,
                "processed": 0,
                "succeeded": 0,
                "skipped_locked": 0,
                "failed": 0,
            }
        placeholders = ",".join("?" for _ in target_uids)
        where_clauses.append(f"q.question_uid IN ({placeholders})")
        params.extend(target_uids)

    rows = conn.execute(
        f"""
        SELECT
            q.question_uid, q.grade_subject, q.cas_images_json, q.image_captions_json,
            q.has_images, q.eff_question_text, q.eff_options_json, q.eff_format,
            q.eff_visual_facts, q.eff_verification_status, q.eff_final_answers_json,
            rq.content_hash,
            cs2.status AS s2_status, cs2.model_name AS s2_model, cs2.input_hash AS s2_hash,
            cs2.attempts AS s2_attempts
        FROM questions q
        JOIN raw_questions rq ON rq.question_uid = q.question_uid
        LEFT JOIN compile_state cs2
            ON cs2.question_uid = q.question_uid AND cs2.stage = 'S2'
        WHERE {' AND '.join(where_clauses)}
        ORDER BY q.question_uid ASC
        """,
        params,
    ).fetchall()

    if norm_mode == "export":
        try:
            resolved_model = resolve_subagent_model(settings, "stage2", subagent_model)
        except ValueError as exc:
            raise PreconditionError(f"PRECONDITION_FAILED: {exc}") from exc

        skipped_locked = 0
        export_items: list[dict[str, Any]] = []
        task_items_meta: dict[str, dict[str, Any]] = {}

        for row in rows:
            uid = str(row["question_uid"])
            content_hash = str(row["content_hash"])
            cas_images = [str(x) for x in _parse_json_list(row["cas_images_json"])]
            image_captions = [str(x) for x in _parse_json_list(row["image_captions_json"])]
            q_has_images = bool(row["has_images"] or len(cas_images) > 0)
            options = _parse_json_dict(row["eff_options_json"])
            verified_final_answers = _parse_json_list(row["eff_final_answers_json"])
            eff_question_text = str(row["eff_question_text"] or "")
            eff_visual_facts = str(row["eff_visual_facts"] or "")
            has_human_visual_ov, has_human_answer_ov = _fetch_human_override_flags(conn, uid)
            method_summary, solution_markdown = _fetch_optimal_solution(conn, uid)

            s2_input_hash = compute_stage2_input_hash(
                eff_question_text=eff_question_text,
                eff_visual_facts=eff_visual_facts,
                eff_final_answers_json=str(row["eff_final_answers_json"] or "[]"),
                eff_options_json=str(row["eff_options_json"] or "{}"),
                cas_images=cas_images,
                image_captions=image_captions,
                has_images=q_has_images,
                has_human_visual_override=has_human_visual_ov,
                has_human_answer_override=has_human_answer_ov,
                verified_method_summary=method_summary,
                verified_solution_markdown=solution_markdown,
                anchors_digest=anchors_digest,
                model_name=f"subagent:{resolved_model}",
                prompt_version=p_ver,
            )

            locked = is_compile_locked(
                row["s2_status"], row["s2_model"], row["s2_hash"], content_hash, force=force
            )
            already_ok = not force and row["s2_status"] == "OK" and row["s2_hash"] == s2_input_hash
            if locked or already_ok:
                skipped_locked += 1
                continue

            seq = len(export_items) + 1
            task_id = f"s2_{seq:04d}"
            export_image_paths = [
                (resolved_assets / h).as_posix() if "/" not in h and "\\" not in h else h
                for h in cas_images
            ]
            msgs = build_stage2_messages(
                grade_subject=grade_subject,
                question_text=eff_question_text,
                visual_facts=eff_visual_facts,
                verified_final_answers=verified_final_answers,
                verified_method_summary=method_summary,
                verified_solution_markdown=solution_markdown,
                anchors_prompt_text=anchors_prompt_text,
                taxonomy_enum_text=taxonomy_enum_text,
                options=options or None,
                has_images=q_has_images,
                image_paths=export_image_paths,
                image_captions=image_captions,
                assets_dir=resolved_assets,
                embed_images=False,
                has_human_visual_override=has_human_visual_ov,
                has_human_answer_override=has_human_answer_ov,
                task_id=task_id,
                config_dir=cfg_dir,
                prompt_profile=prompt_profile,
            )
            export_items.append(
                {
                    "task_id": task_id,
                    "messages": msgs,
                    "has_human_visual_override": has_human_visual_ov,
                    "has_human_answer_override": has_human_answer_ov,
                }
            )
            task_items_meta[task_id] = {
                "question_uid": uid,
                "content_hash": content_hash,
                "stage2_input_hash": s2_input_hash,
                "question_text": eff_question_text,
                "options": options,
                "verified_final_answers": verified_final_answers,
                "has_images": q_has_images,
                "has_human_visual_override": has_human_visual_ov,
                "has_human_answer_override": has_human_answer_ov,
            }

        eff_bs = max(1, int(batch_size))
        batches: list[dict[str, Any]] = []
        for b_idx, start in enumerate(range(0, len(export_items), eff_bs), start=1):
            chunk = export_items[start : start + eff_bs]
            batches.append(
                {
                    "batch_id": f"stage2_batch_{b_idx:04d}",
                    "messages": _build_batch_messages(chunk),
                    "task_item_ids": [item["task_id"] for item in chunk],
                }
            )

        task_map = {
            "grade_subject": grade_subject,
            "prompt_version": p_ver,
            "subagent_model": resolved_model,
            "items": task_items_meta,
        }
        manifest = export_task_batches(
            "stage2",
            batches,
            task_map,
            tasks_dir,
            schema_cls=Stage2BatchOutput,
            subagent_model=resolved_model,
            prompt_version=p_ver,
            prompt_digest=p_digest,
            prompt_profile=prompt_profile,
            max_parallel_workers=settings.subagent.max_parallel_workers,
            overwrite_pending=overwrite_pending,
        )
        return {
            "mode": "export",
            "grade_subject": grade_subject,
            "total_candidates": len(rows),
            "exported_questions": len(export_items),
            "skipped_locked": skipped_locked,
            "batch_count": len(batches),
            "subagent_model": resolved_model,
            "manifest": manifest,
        }

    if norm_mode == "import":
        if results_path is None:
            raise PreconditionError("PRECONDITION_FAILED: --mode import 必须提供 --results")
        tasks_base = Path(tasks_dir)
        task_map_file = tasks_base / "stage2_task_map.json"
        manifest_file = tasks_base / "stage2_manifest.json"

        valid_by_batch, loaded_map, bridge_errs = load_and_validate_results(
            "stage2",
            results_path,
            task_map_file,
            Stage2BatchOutput,
            retry_dir=tasks_base,
            verify_item_completeness=True,
            id_extractor=lambda out: {item.task_id for item in out.items},
        )

        manifest_data: dict[str, Any] = {}
        if manifest_file.is_file():
            try:
                loaded_mf = json.loads(manifest_file.read_text(encoding="utf-8"))
                if isinstance(loaded_mf, dict):
                    manifest_data = loaded_mf
            except Exception:
                manifest_data = {}

        if subagent_model is not None and subagent_model.strip():
            try:
                resolved_model = validate_subagent_model_id(subagent_model)
            except ValueError as exc:
                raise PreconditionError(f"PRECONDITION_FAILED: {exc}") from exc
        elif str(manifest_data.get("subagent_model") or "").strip():
            try:
                resolved_model = validate_subagent_model_id(str(manifest_data["subagent_model"]))
            except ValueError as exc:
                raise PreconditionError(f"PRECONDITION_FAILED: {exc}") from exc
        else:
            resolved_model = resolve_subagent_model(settings, "stage2", None)

        model_name_to_write = f"subagent:{resolved_model}"
        prompt_version_to_write = str(manifest_data.get("prompt_version") or p_ver)
        export_model_for_hash = str(
            loaded_map.get("subagent_model") or manifest_data.get("subagent_model") or resolved_model
        )
        export_pver_for_hash = str(
            loaded_map.get("prompt_version") or manifest_data.get("prompt_version") or p_ver
        )
        items_meta = loaded_map.get("items") or {}

        ingested = 0
        skipped_locked = 0
        item_errors: list[dict[str, Any]] = []
        writer = SingleWriterExecutor()

        with CompileLock(resolved_lock):
            for batch_id, batch_out in valid_by_batch.items():
                for out_s2 in batch_out.items:
                    meta = items_meta.get(out_s2.task_id)
                    if not isinstance(meta, dict):
                        item_errors.append(
                            {
                                "batch_id": batch_id,
                                "task_id": out_s2.task_id,
                                "error_code": "UNKNOWN_TASK_ID",
                                "error": f"task_map 中不存在 task_id={out_s2.task_id!r}",
                            }
                        )
                        continue

                    uid = str(meta["question_uid"])
                    expected_hash = str(meta.get("content_hash") or "")
                    db_row = conn.execute(
                        """
                        SELECT rq.content_hash,
                               q.is_retired, q.eff_is_excluded, q.eff_verification_status,
                               q.cas_images_json, q.image_captions_json, q.has_images,
                               q.eff_question_text, q.eff_options_json,
                               q.eff_visual_facts, q.eff_final_answers_json,
                               cs2.status AS s2_status, cs2.model_name AS s2_model, cs2.input_hash AS s2_hash
                        FROM raw_questions rq
                        JOIN questions q ON q.question_uid = rq.question_uid
                        LEFT JOIN compile_state cs2 ON cs2.question_uid = rq.question_uid AND cs2.stage = 'S2'
                        WHERE rq.question_uid = ?
                        """,
                        (uid,),
                    ).fetchone()
                    if db_row is None:
                        item_errors.append(
                            {
                                "batch_id": batch_id,
                                "task_id": out_s2.task_id,
                                "question_uid": uid,
                                "error_code": "QUESTION_NOT_FOUND",
                                "error": f"题目不存在: {uid}",
                            }
                        )
                        continue

                    if (
                        int(db_row["is_retired"]) != 0
                        or int(db_row["eff_is_excluded"]) != 0
                        or str(db_row["eff_verification_status"])
                        not in ("VERIFIED_CONSENSUS", "HUMAN_VERIFIED")
                    ):
                        item_errors.append(
                            {
                                "batch_id": batch_id,
                                "task_id": out_s2.task_id,
                                "question_uid": uid,
                                "error_code": "INELIGIBLE_POST_EXPORT",
                                "error": (
                                    f"导出后题目状态已不可用 (is_retired={db_row['is_retired']}, "
                                    f"eff_is_excluded={db_row['eff_is_excluded']}, "
                                    f"eff_verification_status={db_row['eff_verification_status']})"
                                ),
                            }
                        )
                        continue

                    current_hash = str(db_row["content_hash"])
                    if expected_hash and current_hash != expected_hash:
                        item_errors.append(
                            {
                                "batch_id": batch_id,
                                "task_id": out_s2.task_id,
                                "question_uid": uid,
                                "error_code": "STALE_CONTENT_HASH",
                                "error": "源题指纹已变化，请重新导出",
                            }
                        )
                        continue

                    expected_s2_hash = str(meta.get("stage2_input_hash") or "")
                    if expected_s2_hash:
                        cur_cas = [str(x) for x in _parse_json_list(db_row["cas_images_json"])]
                        cur_caps = [str(x) for x in _parse_json_list(db_row["image_captions_json"])]
                        cur_has_img = bool(db_row["has_images"] or len(cur_cas) > 0)
                        cur_vis_ov, cur_ans_ov = _fetch_human_override_flags(conn, uid)
                        cur_ms, cur_sm = _fetch_optimal_solution(conn, uid)
                        current_s2_hash = compute_stage2_input_hash(
                            eff_question_text=str(db_row["eff_question_text"] or ""),
                            eff_visual_facts=str(db_row["eff_visual_facts"] or ""),
                            eff_final_answers_json=str(db_row["eff_final_answers_json"] or "[]"),
                            eff_options_json=str(db_row["eff_options_json"] or "{}"),
                            cas_images=cur_cas,
                            image_captions=cur_caps,
                            has_images=cur_has_img,
                            has_human_visual_override=cur_vis_ov,
                            has_human_answer_override=cur_ans_ov,
                            verified_method_summary=cur_ms,
                            verified_solution_markdown=cur_sm,
                            anchors_digest=anchors_digest,
                            model_name=f"subagent:{export_model_for_hash}",
                            prompt_version=export_pver_for_hash,
                        )
                        if current_s2_hash != expected_s2_hash:
                            item_errors.append(
                                {
                                    "batch_id": batch_id,
                                    "task_id": out_s2.task_id,
                                    "question_uid": uid,
                                    "error_code": "STALE_STAGE2_INPUT_HASH",
                                    "error": "导出后有效输入（覆写/解答/锚点）已变更，请重新导出",
                                }
                            )
                            continue

                    if is_compile_locked(
                        db_row["s2_status"],
                        db_row["s2_model"],
                        db_row["s2_hash"],
                        current_hash,
                        force=force,
                    ):
                        skipped_locked += 1
                        continue

                    v_res = validate_draft_payload(
                        mode="STAGE2_ONLY",
                        candidate=out_s2.model_dump(),
                        question_text=str(meta.get("question_text") or ""),
                        grade_subject=grade_subject,
                        options=meta.get("options") if isinstance(meta.get("options"), dict) else None,
                        verified_final_answers=meta.get("verified_final_answers")
                        if isinstance(meta.get("verified_final_answers"), list)
                        else None,
                        run_sympy=False,
                        config_dir=cfg_dir,
                    )
                    if not v_res["ok"]:
                        item_errors.append(
                            {
                                "batch_id": batch_id,
                                "task_id": out_s2.task_id,
                                "question_uid": uid,
                                "error_code": "DRAFT_VALIDATION_FAILED",
                                "validation_errors": v_res["errors"],
                            }
                        )
                        continue

                    with writer.transaction(conn):
                        _ensure_solution_branch1(conn, uid)
                        _write_stage2_db(
                            conn,
                            uid=uid,
                            content_hash=current_hash,
                            out=out_s2,
                            model_name=model_name_to_write,
                            prompt_version=prompt_version_to_write,
                        )
                    ingested += 1

        if item_errors:
            retry_path = tasks_base / "retry_tasks.jsonl"
            existing_lines = (
                [ln for ln in retry_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if retry_path.is_file()
                else []
            )
            extra_lines = [json.dumps({"step_kind": "stage2", **e}, ensure_ascii=False) for e in item_errors]
            _atomic_write_text(retry_path, "\n".join([*existing_lines, *extra_lines]) + "\n")
            if manifest_data:
                manifest_data["status"] = "PENDING_IMPORT"
                manifest_data.pop("imported_at", None)
                _atomic_write_text(manifest_file, json.dumps(manifest_data, ensure_ascii=False, indent=2))

        final_manifest_status = ""
        if manifest_file.is_file():
            try:
                final_manifest_status = str(
                    json.loads(manifest_file.read_text(encoding="utf-8")).get("status") or ""
                )
            except Exception:
                final_manifest_status = ""

        retry_file_path = tasks_base / "retry_tasks.jsonl"
        all_errors = [*bridge_errs, *item_errors]
        return {
            "mode": "import",
            "grade_subject": grade_subject,
            "total_candidates": len(rows),
            "ingested": ingested,
            "skipped_locked": skipped_locked,
            "failed": len(item_errors),
            "batch_errors": len(bridge_errs),
            "errors": all_errors,
            "manifest_status": final_manifest_status,
            "retry_file": retry_file_path.as_posix() if retry_file_path.is_file() else None,
        }

    # mode == "api"
    effective_settings = settings.model_copy(deep=True)
    if use_mock:
        effective_settings.pipeline.use_mock_llm = True

    writer = SingleWriterExecutor()
    vlm_model = effective_settings.llm.vlm_primary.model
    text_model = effective_settings.llm.text_llm.model

    summary: dict[str, Any] = {
        "mode": "api",
        "grade_subject": grade_subject,
        "total_candidates": len(rows),
        "processed": 0,
        "succeeded": 0,
        "skipped_locked": 0,
        "failed": 0,
        "errors": [],
    }

    with CompileLock(resolved_lock):
        async with writer.async_transaction(conn):
            conn.execute(
                """
                UPDATE compile_state
                SET status = 'PENDING', updated_at = datetime('now')
                WHERE stage = 'S2' AND status = 'RUNNING'
                """
            )

        work_items: list[dict[str, Any]] = []
        for row in rows:
            uid = str(row["question_uid"])
            content_hash = str(row["content_hash"])
            cas_images = [str(x) for x in _parse_json_list(row["cas_images_json"])]
            image_captions = [str(x) for x in _parse_json_list(row["image_captions_json"])]
            q_has_images = bool(row["has_images"] or len(cas_images) > 0)
            slot: SlotName = "vlm_primary" if q_has_images else "text_llm"
            slot_model = vlm_model if slot == "vlm_primary" else text_model

            options = _parse_json_dict(row["eff_options_json"])
            verified_final_answers = _parse_json_list(row["eff_final_answers_json"])
            eff_question_text = str(row["eff_question_text"] or "")
            eff_visual_facts = str(row["eff_visual_facts"] or "")
            has_human_visual_ov, has_human_answer_ov = _fetch_human_override_flags(conn, uid)
            method_summary, solution_markdown = _fetch_optimal_solution(conn, uid)

            s2_input_hash = compute_stage2_input_hash(
                eff_question_text=eff_question_text,
                eff_visual_facts=eff_visual_facts,
                eff_final_answers_json=str(row["eff_final_answers_json"] or "[]"),
                eff_options_json=str(row["eff_options_json"] or "{}"),
                cas_images=cas_images,
                image_captions=image_captions,
                has_images=q_has_images,
                has_human_visual_override=has_human_visual_ov,
                has_human_answer_override=has_human_answer_ov,
                verified_method_summary=method_summary,
                verified_solution_markdown=solution_markdown,
                anchors_digest=anchors_digest,
                model_name=slot_model,
                prompt_version=p_ver,
            )

            locked = is_compile_locked(
                row["s2_status"], row["s2_model"], row["s2_hash"], content_hash, force=force
            )
            already_ok = not force and row["s2_status"] == "OK" and row["s2_hash"] == s2_input_hash
            if locked or already_ok:
                summary["skipped_locked"] += 1
                continue

            prev_attempts = int(row["s2_attempts"] or 0)
            reset_attempts = force or (str(row["s2_hash"] or "") != s2_input_hash)
            next_attempt = 1 if reset_attempts else (prev_attempts + 1)

            work_items.append(
                {
                    "uid": uid,
                    "task_id": f"s2_{len(work_items) + 1:04d}",
                    "slot": slot,
                    "slot_model": slot_model,
                    "s2_input_hash": s2_input_hash,
                    "next_attempt": next_attempt,
                    "question_text": eff_question_text,
                    "visual_facts": eff_visual_facts,
                    "options": options,
                    "verified_final_answers": verified_final_answers,
                    "method_summary": method_summary,
                    "solution_markdown": solution_markdown,
                    "has_images": q_has_images,
                    "cas_images": cas_images,
                    "image_captions": image_captions,
                    "has_human_visual_override": has_human_visual_ov,
                    "has_human_answer_override": has_human_answer_ov,
                }
            )

        if not work_items:
            return summary

        async with writer.async_transaction(conn):
            for item in work_items:
                conn.execute(
                    """
                    INSERT INTO compile_state(
                        question_uid, stage, status, input_hash, prompt_version,
                        model_name, attempts, last_error, updated_at
                    ) VALUES (?, 'S2', 'RUNNING', ?, ?, ?, ?, '', datetime('now'))
                    ON CONFLICT(question_uid, stage) DO UPDATE SET
                        status = 'RUNNING',
                        input_hash = excluded.input_hash,
                        prompt_version = excluded.prompt_version,
                        model_name = excluded.model_name,
                        attempts = excluded.attempts,
                        last_error = '',
                        updated_at = datetime('now')
                    """,
                    (
                        item["uid"],
                        item["s2_input_hash"],
                        p_ver,
                        item["slot_model"],
                        item["next_attempt"],
                    ),
                )

        gateway = LLMGateway(
            effective_settings,
            conn=conn,
            writer=writer,
            llm_raw_dir=resolved_llm_raw,
        )
        max_conc = max(
            1,
            min(
                effective_settings.llm.vlm_primary.max_concurrency,
                effective_settings.llm.text_llm.max_concurrency,
            ),
            4,
        )
        task_sem = asyncio.Semaphore(max_conc)

        async def _process_item(item: dict[str, Any]) -> None:
            uid = str(item["uid"])
            slot: SlotName = item["slot"]
            async with task_sem:
                summary["processed"] += 1
                try:
                    msgs = build_stage2_messages(
                        grade_subject=grade_subject,
                        question_text=item["question_text"],
                        visual_facts=item["visual_facts"],
                        verified_final_answers=item["verified_final_answers"],
                        verified_method_summary=item["method_summary"],
                        verified_solution_markdown=item["solution_markdown"],
                        anchors_prompt_text=anchors_prompt_text,
                        taxonomy_enum_text=taxonomy_enum_text,
                        options=item["options"] or None,
                        has_images=item["has_images"],
                        image_paths=item["cas_images"],
                        image_captions=item["image_captions"],
                        assets_dir=resolved_assets,
                        embed_images=True,
                        has_human_visual_override=item["has_human_visual_override"],
                        has_human_answer_override=item["has_human_answer_override"],
                        task_id=item["task_id"],
                        config_dir=cfg_dir,
                        prompt_profile=prompt_profile,
                    )

                    call_res: LLMCallResult[Stage2OnlyOutput] | None = None
                    try:
                        call_res = await gateway.call_structured(
                            slot=slot,
                            stage="S2",
                            messages=msgs,
                            schema=Stage2OnlyOutput,
                            question_uid=uid,
                            prompt_version=p_ver,
                        )
                    except LLMGatewayError as exc:
                        if (
                            effective_settings.pipeline.use_mock_llm
                            and exc.error_kind == "MOCK_KEY_UNREGISTERED"
                            and not any(
                                k in MOCK_RESPONSES
                                for k in (f"{uid}:{slot}", f"S2:{slot}", "S2", "Stage2OnlyOutput")
                            )
                        ):
                            out_s2 = _build_mock_stage2_fallback(item["task_id"])
                            used_model = item["slot_model"] or f"mock-{slot}"
                        else:
                            raise
                    else:
                        out_s2 = call_res.parsed
                        used_model = call_res.model_name

                    v_res = validate_draft_payload(
                        mode="STAGE2_ONLY",
                        candidate=out_s2.model_dump(),
                        question_text=item["question_text"],
                        grade_subject=grade_subject,
                        options=item["options"] or None,
                        verified_final_answers=item["verified_final_answers"],
                        run_sympy=False,
                        config_dir=cfg_dir,
                    )
                    if not v_res["ok"]:
                        err_msg = json.dumps(v_res["errors"], ensure_ascii=False)
                        raise ValueError(f"STAGE2_VALIDATION_FAILED: {err_msg}")

                    async with writer.async_transaction(conn):
                        _ensure_solution_branch1(conn, uid)
                        _write_stage2_db(
                            conn,
                            uid=uid,
                            content_hash=item["s2_input_hash"],
                            out=out_s2,
                            model_name=used_model,
                            prompt_version=p_ver,
                        )
                    summary["succeeded"] += 1
                except Exception as exc:
                    err_str = f"{type(exc).__name__}: {exc}"
                    summary["failed"] += 1
                    summary["errors"].append({"question_uid": uid, "slot": slot, "error": err_str})
                    async with writer.async_transaction(conn):
                        conn.execute(
                            """
                            UPDATE compile_state
                            SET status = 'FAILED',
                                last_error = ?,
                                updated_at = datetime('now')
                            WHERE question_uid = ? AND stage = 'S2'
                            """,
                            (err_str[:500], uid),
                        )

        try:
            await asyncio.gather(*(_process_item(it) for it in work_items))
        finally:
            await gateway.aclose()

    return summary
