"""批量外部错题 Markdown/纯文本列表解析、草稿复用断点续跑与双产物汇总报告生成。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from shuati.config import Settings
from shuati.core.text_norm import normalize_text
from shuati.db.connection import PreconditionError
from shuati.db.schema import SEED_TRAPS
from shuati.llm.gateway import LLMGatewayError
from shuati.models.schemas import ExternalDiagnosisOutput
from shuati.services.mistake import (
    _prune_expired_drafts,
    compute_diagnose_content_hash,
    diagnose_external_mistake_async,
    find_duplicate_candidates,
)

_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_HEADING_RE = re.compile(r"^\s*#{2,3}\s+(.+?)\s*$")
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_EXPLICIT_IMAGE_LINE_RE = re.compile(r"^\s*(?:图片|配图|图|image)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedQuestionItem:
    """从 Markdown / 纯文本题目列表中解析出的单道题目。"""

    index: int
    title: str
    question_text: str
    image_paths: list[Path]
    raw_image_refs: list[str]


def _extract_md_image_target(raw_inside_parens: str) -> str:
    """提取 Markdown `![](target "optional title")` 中的路径部分。"""
    s = raw_inside_parens.strip()
    if s.startswith("<"):
        end_gt = s.find(">")
        if end_gt != -1:
            return s[1:end_gt].strip()
    # 去除尾部的可选引号标题（如 `path/a.png "图1"` 或 `path/a.png '图1'`）
    m_title = re.match(r'^(.+?)\s+["\'].*["\']\s*$', s)
    if m_title:
        s = m_title.group(1).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def _clean_explicit_image_ref(raw_val: str) -> str:
    s = raw_val.strip()
    if (
        (s.startswith('"') and s.endswith('"'))
        or (s.startswith("'") and s.endswith("'"))
        or (s.startswith("`") and s.endswith("`"))
        or (s.startswith("<") and s.endswith(">"))
    ):
        s = s[1:-1].strip()
    return s


def _resolve_image_path(raw_ref: str, base_dir: Path) -> Path:
    p = Path(raw_ref)
    if p.is_absolute():
        return p
    return (base_dir / p).resolve()


def _parse_single_block(
    lines: list[str],
    *,
    title: str | None,
    index: int,
    base_dir: Path,
) -> ParsedQuestionItem | None:
    cleaned_lines: list[str] = []
    raw_refs: list[str] = []
    img_paths: list[Path] = []

    for line in lines:
        # 1. 显式 `图片: path` 行
        m_exp = _EXPLICIT_IMAGE_LINE_RE.match(line)
        if m_exp:
            ref = _clean_explicit_image_ref(m_exp.group(1))
            if ref:
                raw_refs.append(ref)
                img_paths.append(_resolve_image_path(ref, base_dir))
            continue

        # 2. 行内或独立行的 Markdown 图片 `![...](path)`
        if "![" in line and "](" in line:
            for m_md in _MD_IMAGE_RE.finditer(line):
                ref = _extract_md_image_target(m_md.group(1))
                if ref:
                    raw_refs.append(ref)
                    img_paths.append(_resolve_image_path(ref, base_dir))
            stripped_line = _MD_IMAGE_RE.sub("", line).strip()
            if stripped_line:
                cleaned_lines.append(stripped_line)
            continue

        cleaned_lines.append(line.rstrip())

    # 去除首尾空行，并把连续 3+ 空行压成 2 行
    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    # 若整个块只剩 H1 文档总标题且无图片，跳过
    if not img_paths and len(cleaned_lines) == 1 and _H1_RE.match(cleaned_lines[0]):
        return None

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()
    if not text and not img_paths:
        return None

    return ParsedQuestionItem(
        index=index,
        title=(title or "").strip() or f"第 {index} 题",
        question_text=text,
        image_paths=img_paths,
        raw_image_refs=raw_refs,
    )


def parse_question_list_markdown(file_path: Path | str) -> list[ParsedQuestionItem]:
    """解析纯文本 / Markdown 题目列表文件。

    分题规则：按 `---` / `***` / `___` 水平分隔线或 `## ` / `### ` 标题分题；
    图片规则：识别标准 Markdown `![...](路径)` 或独立行 `图片: 路径` / `图片：路径`，
    相对路径以文件所在目录为基准解析，并从题干中剥离图片路径行。
    """
    fp = Path(file_path)
    if not fp.exists() or not fp.is_file():
        raise PreconditionError(f"找不到题目列表文件: {fp}")

    raw_text = fp.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    base_dir = fp.resolve().parent

    raw_blocks: list[tuple[str | None, list[str]]] = []
    cur_title: str | None = None
    cur_lines: list[str] = []

    for line in raw_text.split("\n"):
        if _HR_RE.match(line):
            if cur_lines or cur_title:
                raw_blocks.append((cur_title, cur_lines))
                cur_title = None
                cur_lines = []
            continue
        m_head = _HEADING_RE.match(line)
        if m_head:
            if cur_lines or cur_title:
                raw_blocks.append((cur_title, cur_lines))
            cur_title = m_head.group(1).strip()
            cur_lines = []
            continue
        cur_lines.append(line)

    if cur_lines or cur_title:
        raw_blocks.append((cur_title, cur_lines))

    items: list[ParsedQuestionItem] = []
    for blk_title, blk_lines in raw_blocks:
        parsed = _parse_single_block(
            blk_lines,
            title=blk_title,
            index=len(items) + 1,
            base_dir=base_dir,
        )
        if parsed is not None:
            items.append(parsed)

    return items


def _load_reusable_draft_index(
    drafts_dir: Path,
    grade_subject: str,
) -> dict[str, tuple[Path, dict[str, Any]]]:
    """扫描 drafts_dir 中未过期且通过当前 Schema 校验的草稿，按 content_hash 建索引。"""
    index: dict[str, tuple[Path, dict[str, Any]]] = {}
    if not drafts_dir.exists():
        return index
    # 按修改时间新到旧排序，同 hash 优先复用最新草稿
    draft_files = sorted(
        drafts_dir.glob("drf_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for df in draft_files:
        try:
            payload = json.loads(df.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            if payload.get("grade_subject") != grade_subject:
                continue
            chash = str(payload.get("content_hash") or "").strip()
            if not chash or chash in index:
                continue
            # 确认草稿诊断体满足当前严格 Schema（过滤历史不完整草稿）
            ExternalDiagnosisOutput.model_validate(payload.get("diagnosis"))
            index[chash] = (df, payload)
        except (OSError, ValueError, ValidationError):
            continue
    return index


def _format_answers_inline(final_answers: list[dict[str, Any]]) -> str:
    if not final_answers:
        return "（无）"
    parts: list[str] = []
    for p in final_answers:
        lbl = str(p.get("label") or "主问")
        ans = str(p.get("answer") or "")
        unit = str(p.get("unit") or "").strip()
        val = f"{ans}{unit}" if unit and not ans.endswith(unit) else ans
        parts.append(val if lbl == "主问" and len(final_answers) == 1 else f"{lbl}: {val}")
    return "；".join(parts)


def _build_trap_label_map(config_dir: Path | str | None = None) -> dict[str, str]:
    """构建 TRAP code -> 中文名称映射（以 SEED_TRAPS 为底表，支持 method_blacklist.yaml 覆盖）。"""
    mapping: dict[str, str] = {str(t["code"]): str(t["name"]) for t in SEED_TRAPS}
    if config_dir is not None:
        cfg_path = Path(config_dir) / "method_blacklist.yaml"
        if cfg_path.exists():
            try:
                loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                for item in loaded.get("seed_traps") or []:
                    if isinstance(item, dict) and item.get("code") and item.get("name"):
                        mapping[str(item["code"])] = str(item["name"])
            except Exception:
                pass
    return mapping


def _format_trap_tags_cn(trap_tags: list[str], trap_label_map: dict[str, str]) -> str:
    """将易错陷阱代码列表转换为中文名称行内串（去重保序，未知代码原样回退）。"""
    labels: list[str] = []
    seen: set[str] = set()
    for raw in trap_tags:
        code = str(raw).strip()
        if not code:
            continue
        label = trap_label_map.get(code, code)
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return "、".join(labels)


def _fetch_rec_question_meta(
    conn: sqlite3.Connection | None,
    uids: set[str],
) -> dict[str, dict[str, Any]]:
    """按 question_uid 批量反查库内原题文本、选择题选项与配图标记（兼容历史草稿/JSON 重渲染）。"""
    if conn is None or not uids:
        return {}
    uid_list = sorted(uids)
    ph = ",".join("?" for _ in uid_list)
    rows = conn.execute(
        f"""
        SELECT question_uid, eff_question_text, eff_options_json, has_images
        FROM questions
        WHERE question_uid IN ({ph});
        """,
        uid_list,
    ).fetchall()
    meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_opts = row["eff_options_json"] or "{}"
        try:
            opts = json.loads(raw_opts) if isinstance(raw_opts, str) else dict(raw_opts)
        except ValueError:
            opts = {}
        meta[str(row["question_uid"])] = {
            "question_text": str(row["eff_question_text"] or "").strip(),
            "options": opts if isinstance(opts, dict) else {},
            "has_images": bool(row["has_images"]),
        }
    return meta


def _format_rec_quote_lines(
    question_text: str,
    options: dict[str, Any],
    indent: str = "     ",
) -> list[str]:
    """将推荐原题的纯文本题干与选项渲染为嵌套引用块行。"""
    out: list[str] = []
    body = question_text.strip()
    if body:
        for line in body.splitlines():
            out.append(f"{indent}> {line}" if line.strip() else f"{indent}>")
    if options:
        if body:
            out.append(f"{indent}>")
        for key in sorted(options.keys()):
            val = str(options[key] or "").strip()
            if val:
                out.append(f"{indent}> {key}. {val}")
    return out


def render_batch_markdown_report(
    summary: dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
    config_dir: Path | str | None = Path("config"),
) -> str:
    """渲染可读 Markdown 批量诊断汇总报告。"""
    trap_label_map = _build_trap_label_map(config_dir)

    rec_uids: set[str] = set()
    for it in summary.get("items") or []:
        if it.get("status") == "FAILED":
            continue
        recs = (it.get("result") or {}).get("recommendations") or {}
        for key in ("scaffold_foundations", "isomorphic_variants", "contrastive_and_traps"):
            for r in (recs.get(key) or [])[:3]:
                uid = str(r.get("question_uid") or "").strip()
                if uid:
                    rec_uids.add(uid)
    rec_db_meta = _fetch_rec_question_meta(conn, rec_uids)

    lines: list[str] = [
        "# 外部错题批量诊断汇总报告",
        "",
        f"- **批次 ID**：`{summary['batch_id']}`",
        f"- **源文件**：`{summary['source_file']}`",
        f"- **学段学科**：{summary['grade_subject']}",
        f"- **生成时间**：{summary['created_at']}",
        (
            f"- **统计概览**：共 **{summary['total']}** 题 ｜ "
            f"新诊断成功 **{summary['succeeded']}** ｜ "
            f"复用草稿 **{summary['reused']}** ｜ "
            f"失败 **{summary['failed']}**"
        ),
        "",
        "## 一、总览表",
        "",
        "| # | 标题 | 状态 | 难度 / Elo | 一级模块 | 参考答案 | 重复提示 | 草稿 ID |",
        "| :---: | :--- | :---: | :---: | :--- | :--- | :--- | :--- |",
    ]

    for it in summary["items"]:
        idx = it["index"]
        title = str(it["title"]).replace("|", "\\|")
        st = it["status"]
        if st == "FAILED":
            err_msg = str((it.get("error") or {}).get("message") or "未知错误").replace("|", "\\|")[:60]
            lines.append(f"| {idx} | {title} | `FAILED` | — | — | — | {err_msg} | — |")
            continue

        diag = it["result"]
        elo_tier = f"{diag.get('tier', '')} ({diag.get('base_elo', '')})"
        l1 = str(diag.get("primary_l1_module") or "")
        ans_str = _format_answers_inline(diag.get("final_answers") or []).replace("|", "\\|")
        dups = diag.get("duplicate_candidates") or []
        dup_str = (
            ", ".join(f"`{d['source_id'] or d['question_uid']}` ({d['similarity']})" for d in dups[:2])
            if dups
            else "无"
        )
        draft_id = str(diag.get("draft_id") or "")
        lines.append(f"| {idx} | {title} | `{st}` | {elo_tier} | {l1} | {ans_str} | {dup_str} | `{draft_id}` |")

    lines.extend(["", "## 二、逐题诊断明细", ""])
    for it in summary["items"]:
        idx = it["index"]
        title = it["title"]
        st = it["status"]
        lines.append(f"### {idx}. {title}（`{st}`）")
        lines.append("")
        q_preview = str(it.get("question_text") or "").strip()
        if q_preview:
            lines.append(f"> {q_preview.replace(chr(10), chr(10) + '> ')}")
            lines.append("")

        if st == "FAILED":
            err = it.get("error") or {}
            lines.append(f"- **错误码**：`{err.get('code', 'ERROR')}`")
            lines.append(f"- **错误信息**：{err.get('message', '')}")
            lines.append("")
            continue

        diag = it["result"]
        lines.append(f"- **草稿 ID**：`{diag.get('draft_id')}`（可执行 `shuati ingest-mistake --draft-id {diag.get('draft_id')} --log-wrong` 入库）")
        lines.append(f"- **难度定级**：`{diag.get('tier')}` ｜ `base_elo = {diag.get('base_elo')}`")
        l2_txt = "、".join(diag.get("matched_l2") or []) or "（无）"
        lines.append(f"- **知识定位**：{diag.get('primary_l1_module')} / {l2_txt}")
        lines.append(f"- **参考答案**：{_format_answers_inline(diag.get('final_answers') or [])}")
        if diag.get("method_summary"):
            lines.append(f"- **核心思路**：{diag['method_summary']}")
        if diag.get("trap_tags"):
            traps_cn = _format_trap_tags_cn(list(diag["trap_tags"]), trap_label_map)
            if traps_cn:
                lines.append(f"- **易错陷阱**：{traps_cn}")

        dups = diag.get("duplicate_candidates") or []
        if dups:
            lines.append("- **库内重复题提示**：")
            for d in dups:
                lines.append(
                    f"  - `{d['source_id'] or d['question_uid']}`（{d['match_type']}，sim={d['similarity']}，库内 `{d['tier']}` / {d['base_elo']} Elo）"
                )

        recs = diag.get("recommendations") or {}
        for label, key in (
            ("前置铺垫题", "scaffold_foundations"),
            ("同构变式题", "isomorphic_variants"),
            ("辨析/同坑排雷题", "contrastive_and_traps"),
        ):
            rec_list = recs.get(key) or []
            if rec_list:
                lines.append(f"- **推荐{label}**：")
                for rec_idx, r in enumerate(rec_list[:3], start=1):
                    uid = str(r.get("question_uid") or "").strip()
                    db_info = rec_db_meta.get(uid, {})
                    q_txt = str(r.get("question_text") or db_info.get("question_text") or "").strip()
                    opts_raw = r.get("options") if isinstance(r.get("options"), dict) and r.get("options") else db_info.get("options") or {}
                    opts = opts_raw if isinstance(opts_raw, dict) else {}
                    has_img = bool(r["has_images"]) if "has_images" in r else bool(db_info.get("has_images", False))
                    img_badge = " [含配图]" if has_img else ""
                    sid = r.get("source_id") or uid
                    lines.append(
                        f"  {rec_idx}. `{sid}`（{r.get('tier', '')} / {r.get('base_elo', '')} Elo，sim={r.get('similarity', '')}）{img_badge}"
                    )
                    lines.extend(_format_rec_quote_lines(q_txt, opts))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_batch_markdown_report(
    summary: dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
    config_dir: Path | str | None = Path("config"),
) -> str:
    return render_batch_markdown_report(summary, conn=conn, config_dir=config_dir)


def _now_ts_str(tz_name: str) -> tuple[str, str]:
    try:
        dt = datetime.now(ZoneInfo(tz_name))
    except Exception:
        dt = datetime.now(UTC)
    return dt.strftime("%Y%m%d_%H%M%S"), dt.strftime("%Y-%m-%d %H:%M:%S %Z")


async def diagnose_external_file_async(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    file_path: Path | str,
    grade_subject: str,
    concurrency: int = 1,
    force: bool = False,
    config_dir: Path | str = Path("config"),
    vectors_dir: Path | str = Path("data/vectors"),
    drafts_dir: Path | str = Path("data/drafts"),
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """读取纯文本 / Markdown 题目列表文件，逐题诊断并输出 JSON + Markdown 批次汇总报告。"""
    if concurrency < 1:
        raise PreconditionError(f"--concurrency 必须 >= 1，收到 {concurrency}")

    gs_row = conn.execute(
        "SELECT grade_subject, slug FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if not gs_row:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科 '{grade_subject}' 尚未注册")

    src_file = Path(file_path).resolve()
    parsed_items = parse_question_list_markdown(src_file)
    if not parsed_items:
        raise PreconditionError(f"题目列表文件 '{src_file}' 中未解析出任何有效题目")

    draft_root = Path(drafts_dir)
    draft_root.mkdir(parents=True, exist_ok=True)
    _prune_expired_drafts(draft_root)

    reusable_index = {} if force else _load_reusable_draft_index(draft_root, grade_subject)
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    total = len(parsed_items)

    async def _run_one(item: ParsedQuestionItem) -> dict[str, Any]:
        async with sem:
            t_item = time.perf_counter()
            print(f"[diagnose {item.index}/{total}] 开始诊断: {item.title}", file=sys.stderr, flush=True)

            # 1. 图片存在性预检（单题失败不阻断整批）
            missing_imgs = [str(p) for p in item.image_paths if not p.exists()]
            if missing_imgs:
                msg = f"找不到传入的错题图片: {', '.join(missing_imgs)}"
                print(f"[diagnose {item.index}/{total}] 失败: {msg}", file=sys.stderr, flush=True)
                return {
                    "index": item.index,
                    "title": item.title,
                    "question_text": item.question_text,
                    "image_paths": [str(p) for p in item.image_paths],
                    "status": "FAILED",
                    "duration_ms": int((time.perf_counter() - t_item) * 1000),
                    "error": {"code": "PRECONDITION_FAILED", "message": msg},
                }

            norm_text = normalize_text(item.question_text or "").normalized_text
            if not norm_text:
                msg = "题干 text 不能为空"
                print(f"[diagnose {item.index}/{total}] 失败: {msg}", file=sys.stderr, flush=True)
                return {
                    "index": item.index,
                    "title": item.title,
                    "question_text": item.question_text,
                    "image_paths": [str(p) for p in item.image_paths],
                    "status": "FAILED",
                    "duration_ms": int((time.perf_counter() - t_item) * 1000),
                    "error": {"code": "PRECONDITION_FAILED", "message": msg},
                }

            chash = compute_diagnose_content_hash(grade_subject, norm_text, item.image_paths)

            # 2. 断点续跑复用已有未过期草稿
            if not force and chash in reusable_index:
                df_path, payload = reusable_index[chash]
                diag_dict = payload["diagnosis"]
                dups = payload.get("duplicate_candidates")
                if dups is None:
                    dups = find_duplicate_candidates(
                        conn,
                        grade_subject=grade_subject,
                        norm_text=norm_text,
                        image_count=len(item.image_paths),
                        surf_hits=[],
                        servable_map={},
                    )
                reused_res = {
                    "draft_id": payload["draft_id"],
                    "draft_file": str(df_path.resolve()),
                    "unverified_single_pass": bool(payload.get("unverified_single_pass", True)),
                    "grade_subject": grade_subject,
                    "base_elo": int(payload["base_elo"]),
                    "tier": payload["tier"],
                    "primary_l1_module": diag_dict.get("primary_l1_module", ""),
                    "matched_l2": diag_dict.get("matched_l2", []),
                    "visual_facts": diag_dict.get("visual_facts", ""),
                    "method_summary": diag_dict.get("method_summary", ""),
                    "solution_markdown": diag_dict.get("solution_markdown", ""),
                    "final_answers": diag_dict.get("final_answers", []),
                    "abstract_skeleton": diag_dict.get("abstract_skeleton", ""),
                    "thinking_methods": diag_dict.get("thinking_methods", []),
                    "classic_motifs": diag_dict.get("classic_motifs", []),
                    "trap_tags": diag_dict.get("trap_tags", []),
                    "known_answer_match": payload.get("known_answer_match"),
                    "duplicate_candidates": dups,
                    "recommendations": payload.get(
                        "recommendations",
                        {"scaffold_foundations": [], "isomorphic_variants": [], "contrastive_and_traps": []},
                    ),
                }
                print(
                    f"[diagnose {item.index}/{total}] 复用草稿: {payload['draft_id']} ({payload['tier']} / {payload['base_elo']} Elo)",
                    file=sys.stderr,
                    flush=True,
                )
                return {
                    "index": item.index,
                    "title": item.title,
                    "question_text": norm_text,
                    "image_paths": [str(p) for p in item.image_paths],
                    "status": "REUSED",
                    "duration_ms": int((time.perf_counter() - t_item) * 1000),
                    "result": reused_res,
                }

            # 3. 执行单题实时诊断
            try:
                res = await diagnose_external_mistake_async(
                    conn,
                    settings,
                    question_text=norm_text,
                    grade_subject=grade_subject,
                    image_paths=item.image_paths,
                    config_dir=config_dir,
                    vectors_dir=vectors_dir,
                    drafts_dir=drafts_dir,
                    use_mock=use_mock,
                )
                print(
                    f"[diagnose {item.index}/{total}] 完成: {res['draft_id']} ({res['tier']} / {res['base_elo']} Elo)",
                    file=sys.stderr,
                    flush=True,
                )
                return {
                    "index": item.index,
                    "title": item.title,
                    "question_text": norm_text,
                    "image_paths": [str(p) for p in item.image_paths],
                    "status": "OK",
                    "duration_ms": int((time.perf_counter() - t_item) * 1000),
                    "result": res,
                }
            except PreconditionError as exc:
                print(f"[diagnose {item.index}/{total}] 失败(PRECONDITION_FAILED): {exc}", file=sys.stderr, flush=True)
                return {
                    "index": item.index,
                    "title": item.title,
                    "question_text": norm_text,
                    "image_paths": [str(p) for p in item.image_paths],
                    "status": "FAILED",
                    "duration_ms": int((time.perf_counter() - t_item) * 1000),
                    "error": {"code": "PRECONDITION_FAILED", "message": str(exc)},
                }
            except LLMGatewayError as exc:
                print(f"[diagnose {item.index}/{total}] 失败(LLM_ERROR): {exc}", file=sys.stderr, flush=True)
                return {
                    "index": item.index,
                    "title": item.title,
                    "question_text": norm_text,
                    "image_paths": [str(p) for p in item.image_paths],
                    "status": "FAILED",
                    "duration_ms": int((time.perf_counter() - t_item) * 1000),
                    "error": {"code": "LLM_ERROR", "message": str(exc)},
                }
            except Exception as exc:
                print(f"[diagnose {item.index}/{total}] 失败(INTERNAL_ERROR): {exc}", file=sys.stderr, flush=True)
                return {
                    "index": item.index,
                    "title": item.title,
                    "question_text": norm_text,
                    "image_paths": [str(p) for p in item.image_paths],
                    "status": "FAILED",
                    "duration_ms": int((time.perf_counter() - t_item) * 1000),
                    "error": {"code": "INTERNAL_ERROR", "message": str(exc)},
                }

    results = list(await asyncio.gather(*(_run_one(it) for it in parsed_items)))
    results.sort(key=lambda r: int(r["index"]))

    succeeded = sum(1 for r in results if r["status"] == "OK")
    reused = sum(1 for r in results if r["status"] == "REUSED")
    failed = sum(1 for r in results if r["status"] == "FAILED")

    ts_slug, created_at_human = _now_ts_str(settings.timezone)
    batch_id = f"batch_{ts_slug}_{uuid.uuid4().hex[:6]}"
    batch_json_path = (draft_root / f"{batch_id}.json").resolve()
    batch_md_path = (draft_root / f"{batch_id}.md").resolve()

    warnings: list[str] = []
    if failed > 0:
        warnings.append(f"批量诊断中有 {failed}/{total} 道题失败，详见汇总报告: {batch_md_path}")

    summary: dict[str, Any] = {
        "mode": "batch",
        "batch_id": batch_id,
        "source_file": str(src_file),
        "grade_subject": grade_subject,
        "created_at": created_at_human,
        "total": total,
        "succeeded": succeeded,
        "reused": reused,
        "failed": failed,
        "batch_json_file": str(batch_json_path),
        "batch_markdown_file": str(batch_md_path),
        "warnings": warnings,
        "items": results,
    }

    batch_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    batch_md_path.write_text(
        render_batch_markdown_report(summary, conn=conn, config_dir=config_dir),
        encoding="utf-8",
    )

    # 若全军覆没（0 成功且 0 复用），仍保留已落盘的双汇总报告，但返回非 0 退出码
    if succeeded + reused == 0 and failed > 0:
        err_codes = {(r.get("error") or {}).get("code") for r in results}
        msg = f"批量诊断全部 {failed} 道题均失败，详情已写入 {batch_md_path}"
        if err_codes == {"LLM_ERROR"}:
            raise LLMGatewayError(msg, error_kind="BATCH_ALL_FAILED")
        raise PreconditionError(msg)

    return summary
