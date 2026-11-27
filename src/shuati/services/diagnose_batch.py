"""批量外部错题 Markdown/纯文本列表解析、草稿复用断点续跑与双产物汇总报告生成。"""

from __future__ import annotations

import asyncio
import json
import os
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
from shuati.core import difficulty
from shuati.core.text_norm import normalize_text
from shuati.db.connection import PreconditionError
from shuati.db.schema import SEED_TRAPS
from shuati.llm.gateway import LLMGatewayError
from shuati.models.schemas import ExternalDiagnosisOutput
from shuati.services.mistake import (
    RECOMMENDATION_ENGINE_VERSION,
    _prune_expired_drafts,
    _resolve_diagnose_prompt_version,
    build_diagnose_recommendations_async,
    build_servable_orig_match_index,
    compute_diagnose_content_hash,
    diagnose_external_mistake_async,
    find_duplicate_candidates,
    find_fast_matched_orig_uid,
)
from shuati.verify.answer_parts import normalize_answer_parts

_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_HEADING_RE = re.compile(r"^\s*#{2,3}\s+(.+?)\s*$")
_HEADING_PARTS_RE = re.compile(r"^(\s*#{2,3})\s+(.+?)\s*$")
_HEADING_NO_SPACE_RE = re.compile(r"^(\s*#{2,3})([^\s#].*)$")
_INLINE_BODY_HEADING_RE = re.compile(r"^((?:第\s*\d+\s*题(?:\s*[：:.、])?|\d+\s*[.、)．]))\s*(.+)$")
_Q_NUM_PREFIX_RE = re.compile(r"^(?:第\s*\d+\s*题(?:\s*[：:.、])?|\d+\s*[.、)．）]?)\s*")
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$")
_MD_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\((\s*<[^>\r\n]+>(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*|(?:[^()\r\n]|\([^()\r\n]*\))+)\)"
)
_EXPLICIT_IMAGE_LINE_RE = re.compile(r"^\s*(?:图片|配图|图|image)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_EXPLICIT_IMAGE_PREFIX_RE = re.compile(r"^(\s*(?:图片|配图|图|image)\s*[:：]\s*)(.+?)\s*$", re.IGNORECASE)


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


def _atomic_write_text(target: Path, content: str) -> None:
    """通过同目录 pid+uuid 临时文件 + os.replace 原子落盘，异常时清理临时文件。"""
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
            draft_id_val = payload.get("draft_id")
            tier_val = payload.get("tier")
            raw_elo = payload.get("base_elo")
            if (
                not isinstance(draft_id_val, str)
                or not draft_id_val.strip()
                or not isinstance(tier_val, str)
                or not tier_val.strip()
                or isinstance(raw_elo, bool)
                or raw_elo is None
            ):
                continue
            int(raw_elo)
            # 确认草稿诊断体满足当前严格 Schema（过滤历史不完整草稿）
            ExternalDiagnosisOutput.model_validate(payload.get("diagnosis"))
            index[chash] = (df, payload)
        except (OSError, ValueError, TypeError, ValidationError):
            continue
    return index


def _first_nonempty(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = mapping.get(k)
        if v is not None:
            s = str(v).strip()
            if s:
                return s
    return ""


def _extract_item_display(item: Any, *, labeled: bool = False) -> str:
    if isinstance(item, dict):
        val = _first_nonempty(item, ("canonical", "choice_label", "sympy_expr", "expr", "answer", "value"))
        if labeled:
            key = _first_nonempty(item, ("label", "key", "name", "entity"))
            if key:
                return f"{key}={val}" if val else key
            if len(item) == 1:
                k0, v0 = next(iter(item.items()))
                return f"{str(k0).strip()}={str(v0).strip()}"
        return val
    return str(item).strip()


def _format_answers_inline(final_answers: list[dict[str, Any]]) -> str:
    if not final_answers:
        return "（无）"
    parts: list[str] = []
    for p in final_answers:
        lbl = str(p.get("label") or "主问")
        ans = _first_nonempty(p, ("answer",))
        if not ans:
            collection_mode = str(p.get("collection_mode") or "SINGLE").upper()
            raw_items = p.get("items")
            if collection_mode == "SINGLE":
                ans = _first_nonempty(p, ("choice_label", "canonical", "sympy_expr"))
            if not ans and isinstance(raw_items, list) and raw_items:
                is_labeled = collection_mode == "LABELED_MAP"
                rendered_items = [
                    s for s in (_extract_item_display(it, labeled=is_labeled) for it in raw_items) if s
                ]
                ans = ", ".join(rendered_items)
            if not ans:
                ans = _first_nonempty(p, ("choice_label", "canonical", "sympy_expr"))
        unit = str(p.get("unit") or "").strip()
        val = f"{ans}{unit}" if ans and unit and not ans.endswith(unit) else ans
        parts.append(val if lbl == "主问" and len(final_answers) == 1 else f"{lbl}: {val}")
    return "；".join(parts)


def _escape_md_cell(val: Any, max_len: int | None = None) -> str:
    """规范化 GFM 表格单元格：先折叠换行为空格、再可选截断、最后转义 `|` 避免切出孤立 `\\`。"""
    text = re.sub(r"[\r\n]+", " ", str(val if val is not None else "")).strip()
    if max_len is not None and max_len >= 0 and len(text) > max_len:
        text = text[:max_len]
    return text.replace("|", "\\|")


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
        idx = _escape_md_cell(it["index"])
        title = _escape_md_cell(it["title"])
        st_raw = it["status"]
        st = _escape_md_cell(st_raw)
        if st_raw == "FAILED":
            err_msg = _escape_md_cell((it.get("error") or {}).get("message") or "未知错误", max_len=60)
            lines.append(f"| {idx} | {title} | `{st}` | — | — | — | {err_msg} | — |")
            continue

        diag = it["result"]
        elo_tier = _escape_md_cell(f"{diag.get('tier', '')} ({diag.get('base_elo', '')})")
        l1 = _escape_md_cell(diag.get("primary_l1_module") or "")
        raw_ans_str = _format_answers_inline(diag.get("final_answers") or [])
        matched_db_row = diag.get("matched_db_question")
        if isinstance(matched_db_row, dict) and matched_db_row.get("answer_conflict"):
            raw_ans_str = f"⚠️ {raw_ans_str} (库内验真: {matched_db_row.get('db_answer_display', '')})"
        ans_str = _escape_md_cell(raw_ans_str)
        dups = diag.get("duplicate_candidates") or []
        dup_str = (
            ", ".join(
                f"`{_escape_md_cell(d['source_id'] or d['question_uid'])}` ({_escape_md_cell(d['similarity'])})"
                for d in dups[:2]
            )
            if dups
            else "无"
        )
        draft_id = _escape_md_cell(diag.get("draft_id") or "")
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
        cal_src = str(diag.get("elo_calibration_source") or "")
        raw_elo = diag.get("raw_draft_base_elo")
        raw_tier = diag.get("raw_draft_tier")
        matched_db = diag.get("matched_db_question") or {}
        if (
            cal_src == "DB_VERIFIED_MATCH"
            and raw_elo is not None
            and int(raw_elo) != int(diag.get("base_elo") or 0)
        ):
            lines.append(
                f"- **难度定级**：`{diag.get('tier')}` ｜ `base_elo = {diag.get('base_elo')}`"
                f"（已借用库内已验真原题 `{matched_db.get('source_id', '')}` 定级定窗；单路初评 `{raw_tier}` / {raw_elo} Elo）"
            )
        elif cal_src == "KNN_ANCHOR_BLEND" and raw_elo is not None and int(raw_elo) != int(diag.get("base_elo") or 0):
            lines.append(
                f"- **难度定级**：`{diag.get('tier')}` ｜ `base_elo = {diag.get('base_elo')}`"
                f"（结合库内同子模型近邻校准；单路初评 {raw_elo} Elo）"
            )
        else:
            lines.append(f"- **难度定级**：`{diag.get('tier')}` ｜ `base_elo = {diag.get('base_elo')}`")
        l2_txt = "、".join(diag.get("matched_l2") or []) or "（无）"
        lines.append(f"- **知识定位**：{diag.get('primary_l1_module')} / {l2_txt}")
        sub_inv = diag.get("submodel_inventory")
        if isinstance(sub_inv, dict) and sub_inv.get("submodel_label"):
            tot_db = int(sub_inv.get("total_in_db") or 0)
            compat_iso = sub_inv.get("compatible_isomorphic_peers")
            if tot_db > 3 and compat_iso is not None:
                lines.append(
                    f"- **细分模型题库覆盖**：当前题库「{sub_inv['submodel_label']}」算子共关联 **{tot_db}** 道题"
                    f"（同物理子模型有效非原题变式 **{int(compat_iso)}** 道）"
                )
            else:
                lines.append(
                    f"- **细分模型题库覆盖**：当前题库「{sub_inv['submodel_label']}」细分模型共收录 **{tot_db}** 道匹配题"
                )
        lines.append(f"- **参考答案**：{_format_answers_inline(diag.get('final_answers') or [])}")
        if isinstance(matched_db, dict) and matched_db.get("answer_conflict"):
            lines.append(
                f"- **⚠️ 库内验真答案冲突告警**：单路草稿答案与库内已验真原题 `{matched_db.get('source_id')}`"
                f"（`{matched_db.get('verification_status')}`）不一致！库内标准答案为 **{matched_db.get('db_answer_display')}**，"
                "请警惕单路视觉/建模偏差并以库内验真结果为准。"
            )
        for w_msg in diag.get("warnings") or []:
            w_str = str(w_msg).strip()
            if w_str and not w_str.startswith("DUPLICATE_ANSWER_CONFLICT"):
                lines.append(f"- **⚠️ 系统诊断/精裁告警**：{w_str}")
        if diag.get("method_summary"):
            lines.append(f"- **核心思路**：{diag['method_summary']}")
        eff_trap_list = diag.get("effective_trap_tags") or diag.get("trap_tags") or []
        if eff_trap_list:
            traps_cn = _format_trap_tags_cn(list(eff_trap_list), trap_label_map)
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
        rec_notes = diag.get("recommendation_notes") if isinstance(diag.get("recommendation_notes"), dict) else {}
        for label, key in (
            ("前置铺垫题", "scaffold_foundations"),
            ("同构变式题", "isomorphic_variants"),
            ("辨析/同坑排雷题", "contrastive_and_traps"),
        ):
            rec_list = recs.get(key) or []
            col_note = str(rec_notes.get(key) or "").strip()
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
                    reason_txt = str(r.get("match_reason") or "").strip()
                    if reason_txt:
                        lines.append(f"     - *推荐理由：{reason_txt}*")
                if col_note and len(rec_list[:3]) < 3:
                    lines.append(f"  - *注：{col_note}*")
            elif col_note:
                lines.append(f"- **推荐{label}**：*（{col_note}）*")
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

    cur_diag_ver = _resolve_diagnose_prompt_version(config_dir, grade_subject)
    reusable_index = {} if force else _load_reusable_draft_index(draft_root, grade_subject)
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    total = len(parsed_items)

    # 预扫描整批题目对应的库内原题主键，为每道错题注入同批次其他原题互斥集合（消除同批错题互推自指循环）
    servable_match_idx = build_servable_orig_match_index(conn, grade_subject)
    batch_orig_by_index: dict[int, str] = {}
    for p_it in parsed_items:
        p_norm = normalize_text(p_it.question_text or "").normalized_text
        if not p_norm:
            continue
        m_uid = find_fast_matched_orig_uid(
            conn,
            grade_subject=grade_subject,
            norm_text=p_norm,
            image_count=len(p_it.image_paths),
            servable_index=servable_match_idx,
        )
        if m_uid:
            batch_orig_by_index[p_it.index] = m_uid

    async def _run_one(item: ParsedQuestionItem) -> dict[str, Any]:
        async with sem:
            t_item = time.perf_counter()
            norm_text = item.question_text
            peer_batch_uids = {
                u for idx_other, u in batch_orig_by_index.items() if idx_other != item.index
            }
            print(f"[diagnose {item.index}/{total}] 开始诊断: {item.title}", file=sys.stderr, flush=True)
            try:
                # 1. 图片存在性预检（单题失败不阻断整批）
                missing_imgs = [str(p) for p in item.image_paths if not p.is_file()]
                if missing_imgs:
                    raise PreconditionError(f"找不到传入的错题图片: {', '.join(missing_imgs)}")

                norm_text = normalize_text(item.question_text or "").normalized_text
                if not norm_text:
                    raise PreconditionError("题干 text 不能为空")

                chash = compute_diagnose_content_hash(grade_subject, norm_text, item.image_paths)

                # 2. 断点续跑复用已有未过期草稿（若旧版草稿 Prompt 版本落后或存在旧版答案冲突则重跑 VLM；推荐引擎落后、同批互斥集合变化或曾发生精裁超时降级则原位重算推荐）
                can_reuse_draft = False
                if not force and chash in reusable_index:
                    _, cand_payload = reusable_index[chash]
                    saved_pver = str(cand_payload.get("diagnose_prompt_version") or "").strip()
                    has_old_conflict = bool((cand_payload.get("matched_db_question") or {}).get("answer_conflict"))
                    if (not saved_pver or saved_pver == cur_diag_ver) and not (has_old_conflict and saved_pver != cur_diag_ver):
                        can_reuse_draft = True

                if can_reuse_draft:
                    df_path, payload = reusable_index[chash]
                    diag_dict = payload["diagnosis"]
                    eff_mock_reuse = settings.pipeline.use_mock_llm if use_mock is None else bool(use_mock)
                    had_rerank_degraded = any(
                        str(w).startswith("DIAGNOSE_RERANK_DEGRADED") for w in (payload.get("warnings") or [])
                    )
                    saved_batch_excl = payload.get("batch_excluded_uids")
                    batch_excl_changed = (
                        not isinstance(saved_batch_excl, list)
                        or saved_batch_excl != sorted(peer_batch_uids)
                    )
                    if (
                        payload.get("recommendation_engine_version") != RECOMMENDATION_ENGINE_VERSION
                        or had_rerank_degraded
                        or batch_excl_changed
                    ):
                        diag_model = ExternalDiagnosisOutput.model_validate(diag_dict)
                        diag_parts = normalize_answer_parts(diag_model.final_answers)
                        diff_res = difficulty.compute_difficulty(
                            diag_model.difficulty.C_concept_count.score,
                            diag_model.difficulty.S_chain_length.score,
                            diag_model.difficulty.T_trickiness.score,
                            diag_model.difficulty.L_computation_load.score,
                        )
                        rec_bundle = await build_diagnose_recommendations_async(
                            conn,
                            settings,
                            grade_subject=grade_subject,
                            scope_slug=str(gs_row["slug"]),
                            norm_text=norm_text,
                            image_count=len(item.image_paths),
                            diag=diag_model,
                            diag_parts=diag_parts,
                            diff_res=diff_res,
                            vectors_dir=vectors_dir,
                            use_mock=eff_mock_reuse,
                            batch_excluded_uids=peer_batch_uids,
                        )
                        payload = {
                            **payload,
                            "recommendation_engine_version": RECOMMENDATION_ENGINE_VERSION,
                            "rerank_applied": bool(rec_bundle["rerank_applied"]),
                            "base_elo": int(rec_bundle["effective_base_elo"]),
                            "tier": str(rec_bundle["effective_tier"]),
                            "tier_rank": int(rec_bundle["effective_tier_rank"]),
                            "raw_draft_base_elo": diff_res.base_elo,
                            "raw_draft_tier": diff_res.tier,
                            "elo_calibration_source": rec_bundle["elo_calibration_source"],
                            "matched_db_question": rec_bundle["matched_db_question"],
                            "submodel_inventory": rec_bundle["submodel_inventory"],
                            "duplicate_candidates": rec_bundle["duplicate_candidates"],
                            "recommendations": rec_bundle["recommendations"],
                            "recommendation_notes": rec_bundle["recommendation_notes"],
                            "effective_trap_tags": rec_bundle["effective_trap_tags"],
                            "batch_excluded_uids": rec_bundle["batch_excluded_uids"],
                            "warnings": rec_bundle["warnings"],
                        }
                        _atomic_write_text(df_path, json.dumps(payload, ensure_ascii=False, indent=2))

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
                        "recommendation_engine_version": payload.get("recommendation_engine_version", ""),
                        "unverified_single_pass": bool(payload.get("unverified_single_pass", True)),
                        "grade_subject": grade_subject,
                        "base_elo": int(payload["base_elo"]),
                        "tier": payload["tier"],
                        "raw_draft_base_elo": payload.get("raw_draft_base_elo"),
                        "raw_draft_tier": payload.get("raw_draft_tier"),
                        "elo_calibration_source": payload.get("elo_calibration_source", ""),
                        "matched_db_question": payload.get("matched_db_question"),
                        "submodel_inventory": payload.get("submodel_inventory"),
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
                        "effective_trap_tags": payload.get("effective_trap_tags"),
                        "batch_excluded_uids": payload.get("batch_excluded_uids", []),
                        "known_answer_match": payload.get("known_answer_match"),
                        "duplicate_candidates": dups,
                        "recommendations": payload.get(
                            "recommendations",
                            {"scaffold_foundations": [], "isomorphic_variants": [], "contrastive_and_traps": []},
                        ),
                        "recommendation_notes": payload.get("recommendation_notes", {}),
                        "warnings": payload.get("warnings", []),
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
                    batch_excluded_uids=peer_batch_uids,
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

    summary_json_text = json.dumps(summary, ensure_ascii=False, indent=2)
    summary_md_text = render_batch_markdown_report(summary, conn=conn, config_dir=config_dir)
    _atomic_write_text(batch_json_path, summary_json_text)
    _atomic_write_text(batch_md_path, summary_md_text)

    # 若全军覆没（0 成功且 0 复用），仍保留已落盘的双汇总报告，但返回非 0 退出码
    if succeeded + reused == 0 and failed > 0:
        err_codes = {(r.get("error") or {}).get("code") for r in results}
        msg = f"批量诊断全部 {failed} 道题均失败，详情已写入 {batch_md_path}"
        if err_codes == {"LLM_ERROR"}:
            raise LLMGatewayError(msg, error_kind="BATCH_ALL_FAILED")
        raise PreconditionError(msg)

    return summary


def _is_image_only_line(line: str) -> bool:
    if _EXPLICIT_IMAGE_LINE_RE.match(line):
        return True
    if "![" in line and "](" in line:
        return _MD_IMAGE_RE.sub("", line).strip() == ""
    return False


def _absolutize_image_line(line: str, base_dir: Path, fixes_applied: list[str]) -> str:
    m_exp = _EXPLICIT_IMAGE_PREFIX_RE.match(line)
    if m_exp:
        prefix, raw_val = m_exp.group(1), m_exp.group(2)
        ref = _clean_explicit_image_ref(raw_val)
        if ref and not Path(ref).is_absolute():
            abs_p = (base_dir / ref).resolve()
            fixes_applied.append(f"相对图片路径转绝对路径: {ref} -> {abs_p.as_posix()}")
            return f"{prefix}{abs_p.as_posix()}"
        return line

    if "![" in line and "](" in line:
        def _repl(m: re.Match[str]) -> str:
            full = m.group(0)
            inside = m.group(1)
            ref = _extract_md_image_target(inside)
            if not ref or Path(ref).is_absolute():
                return full
            abs_p = (base_dir / ref).resolve()
            abs_posix = abs_p.as_posix()
            fixes_applied.append(f"相对图片路径转绝对路径: {ref} -> {abs_posix}")
            s_inside = inside.strip()
            if s_inside.startswith("<") and ">" in s_inside:
                end_gt = s_inside.find(">")
                suffix = s_inside[end_gt + 1 :]
                new_inside = f"<{abs_posix}>{suffix}"
            else:
                replacement = (
                    f"<{abs_posix}>"
                    if any(ch in abs_posix for ch in (" ", "\t", "(", ")"))
                    else abs_posix
                )
                if (
                    (s_inside.startswith('"') and s_inside.endswith('"'))
                    or (s_inside.startswith("'") and s_inside.endswith("'"))
                ) and s_inside[1:-1].strip() == ref:
                    new_inside = replacement
                else:
                    new_inside = inside.replace(ref, replacement, 1)
            prefix_md = full[: full.index("](") + 2]
            return f"{prefix_md}{new_inside})"

        return _MD_IMAGE_RE.sub(_repl, line)
    return line


def _block_has_body_text(lines: list[str], start_idx: int) -> bool:
    for idx in range(start_idx, len(lines)):
        ln = lines[idx]
        if _HR_RE.match(ln) or _HEADING_RE.match(ln):
            break
        if not ln.strip():
            continue
        if _is_image_only_line(ln):
            continue
        return True
    return False


def preflight_question_list_markdown(
    file_path: Path | str,
    *,
    out_normalized: Path | str | None = None,
    drafts_dir: Path | str = Path("data/drafts"),
) -> dict[str, Any]:
    """严格只读源文件，修复常见排版坑位并输出规范化副本，运行预检统计。

    修复项：
    1. `##12.` / `###2.` 等井号后漏空格补齐；
    2. `---` 分隔线后孤立/错位的 `图片:` / `配图:` 行归位至前一道题末尾；
    3. 标题与题干正文同行（如 `##19. 一列火车...`）拆分题号标题与正文，防止题干丢失；
    4. 相对图片路径按源文件所在目录转为绝对路径。
    """
    fp = Path(file_path)
    if not fp.exists() or not fp.is_file():
        raise PreconditionError(f"找不到题目列表文件: {fp}")

    if out_normalized is not None:
        norm_path = Path(out_normalized)
        if norm_path.resolve() == fp.resolve():
            raise PreconditionError(f"PRECONDITION_FAILED: out_normalized 不得指向源文件本身（源文件严格只读）: {fp}")
    else:
        ts_slug, _ = _now_ts_str("Asia/Shanghai")
        norm_path = Path(drafts_dir) / "preflight" / f"{ts_slug}_{fp.stem}.normalized.md"

    raw_text = fp.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    src_base_dir = fp.resolve().parent
    fixes_applied: list[str] = []

    # 1. 修复 `##12.` / `###2.` 标题漏空格
    pass1_lines: list[str] = []
    for line_no, raw_line in enumerate(raw_text.split("\n"), start=1):
        m_no_space = _HEADING_NO_SPACE_RE.match(raw_line)
        if m_no_space:
            fixed = f"{m_no_space.group(1)} {m_no_space.group(2).lstrip()}"
            pass1_lines.append(fixed)
            fixes_applied.append(f"L{line_no}: 标题补空格 '{raw_line.strip()}' -> '{fixed.strip()}'")
        else:
            pass1_lines.append(raw_line)

    # 2. 修复 `---` 分隔线后孤立错位的 `图片:` 行归位
    pass2_lines: list[str] = []
    i = 0
    n_lines = len(pass1_lines)
    while i < n_lines:
        cur = pass1_lines[i]
        if _HR_RE.match(cur):
            has_prev_content = any(ln.strip() and not _HR_RE.match(ln) for ln in pass2_lines)
            j = i + 1
            collected_imgs: list[str] = []
            while j < n_lines:
                nxt = pass1_lines[j]
                if not nxt.strip():
                    j += 1
                    continue
                if _is_image_only_line(nxt):
                    collected_imgs.append(nxt)
                    j += 1
                    continue
                break
            at_boundary = j >= n_lines or bool(_HEADING_RE.match(pass1_lines[j])) or bool(_HR_RE.match(pass1_lines[j]))
            if has_prev_content and collected_imgs and at_boundary:
                for img_line in collected_imgs:
                    pass2_lines.append(img_line)
                    fixes_applied.append(f"错位图片行归位至分隔线前: '{img_line.strip()}'")
                pass2_lines.append(cur)
                i = j
                continue
        pass2_lines.append(cur)
        i += 1

    # 3. 拆分标题与正文同行（避免丢弃题干）
    pass3_lines: list[str] = []
    auto_q_idx = 0
    for idx, line in enumerate(pass2_lines):
        m_head = _HEADING_PARTS_RE.match(line)
        if m_head:
            auto_q_idx += 1
            hashes = m_head.group(1).strip()
            content = m_head.group(2).strip()
            has_body = _block_has_body_text(pass2_lines, idx + 1)
            m_inline = _INLINE_BODY_HEADING_RE.match(content)
            if m_inline:
                title_part = m_inline.group(1).strip()
                rest_part = m_inline.group(2).strip()
                if not has_body:
                    looks_like_prose = True
                else:
                    looks_like_prose = (
                        len(rest_part) >= 24
                        or any(ch in rest_part for ch in "。？！?!，,")
                    )
                if rest_part and looks_like_prose:
                    pass3_lines.append(f"{hashes} {title_part}")
                    pass3_lines.append(rest_part)
                    fixes_applied.append(f"拆分同行标题与题干: '{content[:30]}'")
                    continue
            else:
                stripped_stem = _Q_NUM_PREFIX_RE.sub("", content).strip()
                if not has_body and stripped_stem and (
                    len(stripped_stem) >= 12
                    or any(ch in stripped_stem for ch in "。？！?!，,;；：:")
                    or bool(re.search(r"\d", stripped_stem))
                ):
                    pass3_lines.append(f"{hashes} 第 {auto_q_idx} 题")
                    pass3_lines.append(stripped_stem)
                    fixes_applied.append(f"无正文标题降级为题干: '{stripped_stem[:30]}'")
                    continue
        pass3_lines.append(line)

    # 4. 相对图片路径转绝对路径
    pass4_lines = [_absolutize_image_line(ln, src_base_dir, fixes_applied) for ln in pass3_lines]
    normalized_text = "\n".join(pass4_lines).rstrip() + "\n"

    _atomic_write_text(norm_path, normalized_text)

    parsed_items = parse_question_list_markdown(norm_path)
    missing_images: list[dict[str, Any]] = []
    warnings: list[str] = []
    items_out: list[dict[str, Any]] = []

    if not parsed_items:
        warnings.append("未能从规范化副本中解析出任何有效题目。")

    for item in parsed_items:
        item_missing: list[str] = []
        for p, raw_ref in zip(item.image_paths, item.raw_image_refs, strict=False):
            if not p.is_file():
                p_str = str(p)
                item_missing.append(p_str)
                missing_images.append(
                    {
                        "index": item.index,
                        "title": item.title,
                        "image_path": p_str,
                        "raw_ref": raw_ref,
                    }
                )
        if not item.question_text.strip():
            warnings.append(f"第 {item.index} 题（{item.title}）题干文本为空。")
        items_out.append(
            {
                "index": item.index,
                "title": item.title,
                "char_count": len(item.question_text),
                "image_count": len(item.image_paths),
                "missing_images": item_missing,
                "preview": item.question_text[:80].replace("\n", " "),
            }
        )

    if missing_images:
        warnings.append(f"共有 {len(missing_images)} 张配图文件不存在，请检查路径。")

    return {
        "source_file": str(fp.resolve()),
        "normalized_file": str(norm_path.resolve()),
        "fixes_applied": fixes_applied,
        "total_questions": len(parsed_items),
        "missing_images": missing_images,
        "warnings": warnings,
        "items": items_out,
    }
