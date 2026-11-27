"""自包含双模式静态 HTML 练习单渲染器（DESIGN.md §5.5）。

特点：
- 默认打包内联离线 KaTeX（CSS / JS / auto-render / .woff2 base64），完全断网单文件可用，并内置纯 DOM 数学符号降级兜底；
- 默认将 CAS 配图以 data:image/...;base64 内联，多图按序并排带题注；
- 屏幕模式：三级默认折叠 <details>（逐步提示 -> 参考答案 -> 完整解析与知识卡片含多解 Tab）；
- 打印模式（@media print）：隐藏 <details> 与工具栏，break-inside: avoid，按题型留白 8cm/7cm/3.5cm，可选末页集中参考答案。
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
import sqlite3
from pathlib import Path
from typing import Any

from shuati.db import repository

_KATEX_ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "katex"
_FONT_URL_RE = re.compile(r"url\((?:['\"]?)fonts/([^)'\"]+\.woff2)(?:['\"]?)\)")
_SAFE_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_IMAGE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


def _load_inline_katex_assets() -> tuple[str, str, str]:
    """读取本地打包 KaTeX CSS（内联 woff2 base64）、katex.min.js 与 auto-render.min.js。"""
    css_path = _KATEX_ASSETS_DIR / "katex.min.css"
    js_path = _KATEX_ASSETS_DIR / "katex.min.js"
    auto_path = _KATEX_ASSETS_DIR / "contrib" / "auto-render.min.js"
    if not (css_path.exists() and js_path.exists() and auto_path.exists()):
        return "", "", ""

    css_text = css_path.read_text(encoding="utf-8")

    def _replace_font(match: re.Match[str]) -> str:
        fn = match.group(1)
        fp = _KATEX_ASSETS_DIR / "fonts" / fn
        if not fp.exists():
            return match.group(0)
        b64 = base64.b64encode(fp.read_bytes()).decode("ascii")
        return f"url(data:font/woff2;base64,{b64})"

    # 将 @font-face 中 .woff/.ttf 冗余声明简化并把 .woff2 内联为 data URI
    inlined_css = _FONT_URL_RE.sub(_replace_font, css_text)
    katex_js = js_path.read_text(encoding="utf-8")
    auto_js = auto_path.read_text(encoding="utf-8")
    return inlined_css, katex_js, auto_js


def _encode_cas_image(img_name: str, assets_dir: Path, embed_images: bool) -> str:
    """安全加载 assets_dir 内的单段图片文件；拒绝越界、符号链接、目录及非图片扩展名。"""
    raw_name = (img_name or "").strip()
    if (
        not raw_name
        or raw_name in (".", "..")
        or "/" in raw_name
        or "\\" in raw_name
        or "\x00" in raw_name
        or not _SAFE_IMAGE_NAME_RE.fullmatch(raw_name)
    ):
        return ""

    p_name = Path(raw_name)
    if (
        p_name.is_absolute()
        or p_name.drive
        or p_name.name != raw_name
        or p_name.suffix.lower() not in _ALLOWED_IMAGE_EXTS
    ):
        return ""

    try:
        assets_root = assets_dir.resolve()
        candidate = assets_root / raw_name
        if candidate.is_symlink():
            return ""
        resolved = candidate.resolve()
        if (
            resolved.parent != assets_root
            or not resolved.is_relative_to(assets_root)
            or not resolved.is_file()
        ):
            return ""
    except OSError:
        return ""

    if not embed_images:
        return resolved.as_uri()

    mime, _ = mimetypes.guess_type(str(resolved))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    try:
        raw_bytes = resolved.read_bytes()
    except OSError:
        return ""
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _is_escaped(text: str, pos: int) -> bool:
    """判断 text[pos] 前是否紧跟奇数个反斜杠。"""
    bs = 0
    j = pos - 1
    while j >= 0 and text[j] == "\\":
        bs += 1
        j -= 1
    return (bs % 2) == 1


def _nl2br_preserving_math(text: str) -> str:
    """HTML 转义并将非数学区换行转为 <br/>，保留 $...$ 与 $$...$$ 内的原始换行。"""
    norm = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    n = len(norm)
    i = 0
    plain_start = 0

    while i < n:
        if norm[i] == "$" and not _is_escaped(norm, i):
            delim = "$$" if (i + 1 < n and norm[i + 1] == "$") else "$"
            dlen = len(delim)
            search_pos = i + dlen
            close_pos = -1
            while search_pos < n:
                idx = norm.find(delim, search_pos)
                if idx == -1:
                    break
                if not _is_escaped(norm, idx):
                    close_pos = idx
                    break
                search_pos = idx + 1
            if close_pos != -1:
                if i > plain_start:
                    out.append(html.escape(norm[plain_start:i]).replace("\n", "<br/>"))
                span_end = close_pos + dlen
                out.append(html.escape(norm[i:span_end]))
                i = span_end
                plain_start = span_end
                continue
            i += dlen
            continue
        i += 1

    if plain_start < n:
        out.append(html.escape(norm[plain_start:]).replace("\n", "<br/>"))
    return "".join(out)


def _extract_item_display_value(item: dict[str, Any]) -> str:
    """按展示优先级从 AnswerPart.items 的字典元素中提取可读答案值。"""
    for field in ("answer", "canonical", "choice_label", "sympy_expr", "expr", "value"):
        raw = item.get(field)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _format_answer_part_display(p: dict[str, Any]) -> str:
    """按不变量 I4 格式化单个 AnswerPart 展示文本（支持 items 多值与 sympy_expr 回退）。"""
    val = str(p.get("answer") or "").strip()

    if not val:
        raw_items = p.get("items")
        if isinstance(raw_items, list) and raw_items:
            collection_mode = str(p.get("collection_mode") or "SINGLE").strip().upper()
            formatted: list[str] = []
            for it in raw_items:
                if collection_mode == "LABELED_MAP":
                    if isinstance(it, dict):
                        raw_key: Any = None
                        for k_field in ("key", "label", "name", "entity"):
                            cand_k = it.get(k_field)
                            if cand_k is not None and str(cand_k).strip():
                                raw_key = cand_k
                                break
                        if raw_key is not None:
                            iv = _extract_item_display_value(it)
                            k_str = str(raw_key).strip()
                            formatted.append(f"{k_str}={iv}" if iv else k_str)
                        elif len(it) == 1:
                            k_only, v_only = next(iter(it.items()))
                            formatted.append(f"{str(k_only).strip()}={str(v_only).strip()}")
                        else:
                            iv = _extract_item_display_value(it)
                            if iv:
                                formatted.append(iv)
                    else:
                        s_it = str(it).strip()
                        if s_it:
                            formatted.append(s_it)
                else:
                    if isinstance(it, dict):
                        iv = _extract_item_display_value(it)
                        if not iv and len(it) == 1:
                            k_only, v_only = next(iter(it.items()))
                            iv = f"{str(k_only).strip()}={str(v_only).strip()}"
                        if iv:
                            formatted.append(iv)
                    else:
                        s_it = str(it).strip()
                        if s_it:
                            formatted.append(s_it)
            val = ", ".join(formatted)

    if not val:
        for field in ("choice_label", "canonical", "sympy_expr"):
            cand = str(p.get(field) or "").strip()
            if cand:
                val = cand
                break

    unit = str(p.get("unit") or "").strip()
    if unit and val and unit not in val:
        return f"{val} {unit}"
    return val


def _print_blank_cm(fmt: str, has_images: bool) -> float:
    f = (fmt or "").strip()
    if f == "作图操作题":
        return 8.0
    if has_images and ("解答" in f or "证明" in f or "几何" in f):
        return 7.0
    return 3.5


def _safe_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _load_sheet_items(
    conn: sqlite3.Connection,
    *,
    uids: list[str],
    reasons_by_uid: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for uid in uids:
        q = conn.execute(
            """
            SELECT q.*, r.source_id
            FROM questions q
            JOIN raw_questions r ON r.question_uid = q.question_uid
            WHERE q.question_uid = ?;
            """,
            (uid,),
        ).fetchone()
        if not q:
            continue

        sols = conn.execute(
            """
            SELECT branch_id, is_optimal, method_summary, solution_markdown, sympy_verified
            FROM question_solutions
            WHERE question_uid = ?
            ORDER BY is_optimal DESC, branch_id ASC;
            """,
            (uid,),
        ).fetchall()

        branches: list[dict[str, Any]] = []
        for s in sols:
            bid = str(s["branch_id"])
            st_rows = conn.execute(
                """
                SELECT step_index, sub_goal, core_formula, hint, m_l1, m_l2
                FROM question_steps
                WHERE question_uid = ? AND branch_id = ?
                ORDER BY step_index ASC;
                """,
                (uid, bid),
            ).fetchall()
            branches.append(
                {
                    "branch_id": bid,
                    "is_optimal": bool(s["is_optimal"]),
                    "method_summary": str(s["method_summary"] or ""),
                    "solution_markdown": str(s["solution_markdown"] or ""),
                    "steps": [dict(st) for st in st_rows],
                }
            )

        node_ids = _safe_json(q["eff_node_ids_json"], [])
        l2_set: list[str] = []
        l3_set: list[str] = []
        if node_ids:
            ph = ",".join("?" for _ in node_ids)
            for nr in conn.execute(
                f"SELECT l2_entity, l3_operator FROM knowledge_nodes WHERE node_id IN ({ph});",
                node_ids,
            ).fetchall():
                if nr["l2_entity"] and nr["l2_entity"] not in l2_set:
                    l2_set.append(str(nr["l2_entity"]))
                if nr["l3_operator"] and nr["l3_operator"] not in l3_set:
                    l3_set.append(str(nr["l3_operator"]))

        motifs: list[str] = []
        traps: list[str] = []
        for cr in conn.execute(
            """
            SELECT qc.kind, c.name
            FROM question_concepts qc
            JOIN concepts c ON c.concept_id = qc.concept_id
            WHERE qc.question_uid = ?;
            """,
            (uid,),
        ).fetchall():
            if cr["kind"] == "MOTIF":
                motifs.append(str(cr["name"]))
            elif cr["kind"] == "TRAP":
                traps.append(str(cr["name"]))

        rmeta = reasons_by_uid.get(uid, {})
        items.append(
            {
                "question_uid": uid,
                "source_id": str(q["source_id"] or ""),
                "format": str(q["eff_format"] or ""),
                "question_text": str(q["eff_question_text"] or ""),
                "options": _safe_json(q["eff_options_json"], {}),
                "has_images": bool(q["has_images"]),
                "cas_images": _safe_json(q["cas_images_json"], []),
                "image_captions": _safe_json(q["image_captions_json"], []),
                "base_elo": int(q["eff_base_elo"] or 1500),
                "tier": str(q["eff_tier"] or ""),
                "primary_l1_module": str(q["eff_primary_l1_module"] or ""),
                "final_answers": _safe_json(q["eff_final_answers_json"], []),
                "branches": branches,
                "l2_entities": l2_set,
                "l3_operators": l3_set,
                "motifs": motifs,
                "traps": traps,
                "reason_public": rmeta.get("reason_public", ""),
            }
        )
    return items


def render_practice_sheet(
    conn: sqlite3.Connection,
    *,
    quiz_batch_id: str | None = None,
    question_uids: list[str] | None = None,
    assets_dir: Path | str = Path("data/assets/images"),
    sheets_dir: Path | str = Path("data/sheets"),
    out_path: Path | str | None = None,
    title: str = "智学练习单",
    embed_images: bool = True,
    print_answers_at_end: bool = True,
) -> dict[str, Any]:
    """渲染双模式自包含静态 HTML 练习单并落盘，返回文件路径与渲染摘要。"""
    resolved_uids: list[str] = []
    reasons_by_uid: dict[str, dict[str, str]] = {}
    batch_title = title

    if quiz_batch_id:
        qb = conn.execute(
            "SELECT * FROM quiz_batches WHERE quiz_batch_id = ?;",
            (quiz_batch_id,),
        ).fetchone()
        if not qb:
            raise repository.NotFoundError(f"组卷批次 '{quiz_batch_id}' 不存在")
        batch_title = f"专题练习单 · {qb['grade_subject']} ({quiz_batch_id})"
        for it in _safe_json(qb["items_json"], []):
            if isinstance(it, dict) and it.get("question_uid"):
                uid = str(it["question_uid"])
                resolved_uids.append(uid)
                reasons_by_uid[uid] = {
                    "reason_public": str(it.get("reason_public") or ""),
                    "reason_full": str(it.get("reason_full") or ""),
                }
    elif question_uids:
        for raw_id in question_uids:
            uid = repository.resolve_question_uid(conn, raw_id)
            resolved_uids.append(uid)
    else:
        raise ValueError("必须提供 quiz_batch_id 或 question_uids 之一")

    items = _load_sheet_items(conn, uids=resolved_uids, reasons_by_uid=reasons_by_uid)
    if not items:
        raise repository.NotFoundError("未加载到任何有效待渲染题目")

    target_dir = Path(sheets_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    if out_path:
        final_out = Path(out_path)
        final_out.parent.mkdir(parents=True, exist_ok=True)
    else:
        stem = quiz_batch_id or f"sheet_{len(items)}q"
        final_out = target_dir / f"{stem}.html"

    katex_css, katex_js, auto_js = _load_inline_katex_assets()
    img_base = Path(assets_dir)

    cards_html: list[str] = []
    appendix_html: list[str] = []

    for idx, item in enumerate(items, start=1):
        esc_text = _nl2br_preserving_math(item["question_text"])
        meta_badges = (
            f"<span class='badge'>{html.escape(item['source_id'] or item['question_uid'])}</span>"
            f"<span class='badge tier'>{html.escape(item['tier'])}</span>"
            f"<span class='badge mod'>{html.escape(item['primary_l1_module'])}</span>"
            f"<span class='badge fmt'>{html.escape(item['format'])}</span>"
        )
        reason_line = (
            f"<div class='reason-pub no-print'>{html.escape(item['reason_public'])}</div>"
            if item["reason_public"]
            else ""
        )

        # 选择题选项
        opts_html = ""
        if isinstance(item["options"], dict) and item["options"]:
            opt_lis = "".join(
                f"<div class='opt-item'><strong>{html.escape(str(k))}.</strong> {html.escape(str(v))}</div>"
                for k, v in sorted(item["options"].items())
            )
            opts_html = f"<div class='options-grid'>{opt_lis}</div>"

        # 配图并排渲染
        imgs_html = ""
        if item["cas_images"]:
            figs: list[str] = []
            captions = item["image_captions"]
            for i_img, cas_img in enumerate(item["cas_images"], start=1):
                src_uri = _encode_cas_image(str(cas_img), img_base, embed_images)
                cap_txt = captions[i_img - 1] if i_img - 1 < len(captions) else ""
                cap_label = f"配图 {i_img}" + (f" · {cap_txt}" if cap_txt else "")
                if src_uri:
                    img_body = f"<img src='{html.escape(src_uri, quote=True)}' alt='{html.escape(cap_label)}' />"
                else:
                    img_body = "<div class='fig-missing'>（配图缺失或路径不可用）</div>"
                figs.append(
                    f"<figure class='fig-box'>{img_body}"
                    f"<figcaption>{html.escape(cap_label)}</figcaption></figure>"
                )
            imgs_html = f"<div class='fig-row'>{''.join(figs)}</div>"

        # ① 逐步提示（严格不含 sub_goal 与 core_formula）
        opt_branch = next((b for b in item["branches"] if b["is_optimal"]), item["branches"][0] if item["branches"] else None)
        hints_lis = ""
        if opt_branch and opt_branch["steps"]:
            hints_lis = "".join(
                f"<li><span class='step-idx'>提示 {int(st['step_index'])}：</span>{html.escape(str(st['hint'] or ''))}</li>"
                for st in opt_branch["steps"]
            )
        else:
            hints_lis = "<li>暂无分步提示</li>"

        # ② 标准答案按小问分列
        ans_rows = []
        ans_compact = []
        for p in item["final_answers"]:
            if not isinstance(p, dict):
                continue
            lbl = str(p.get("label") or "主问")
            full_val = _format_answer_part_display(p)
            ans_rows.append(f"<li><strong>{html.escape(lbl)}：</strong>{html.escape(full_val)}</li>")
            ans_compact.append(f"{lbl}: {full_val}")
        ans_html = f"<ul>{''.join(ans_rows) or '<li>暂无答案</li>'}</ul>"

        # ③ 完整解析与知识卡片（多解分支切换）
        tab_btns: list[str] = []
        tab_panels: list[str] = []
        for b_idx, br in enumerate(item["branches"]):
            active_cls = "active" if b_idx == 0 else ""
            b_title = f"解法 {b_idx + 1}" + ("（最优）" if br["is_optimal"] else "")
            tab_btns.append(
                f"<button type='button' class='tab-btn {active_cls}' onclick=\"switchTab(this, 'q{idx}-b{b_idx}')\">{html.escape(b_title)}</button>"
            )
            steps_detail = "".join(
                "<li>"
                f"<strong>Step {int(st['step_index'])} · {html.escape(str(st['sub_goal']))}</strong>"
                + (
                    f"<div class='formula'>核心公式：{html.escape(str(st['core_formula']))}</div>"
                    if st.get("core_formula")
                    else ""
                )
                + "</li>"
                for st in br["steps"]
            )
            sol_md = _nl2br_preserving_math(br["solution_markdown"])
            tab_panels.append(
                f"<div id='q{idx}-b{b_idx}' class='tab-panel {active_cls}'>"
                f"<div class='method-sum'><em>思路概述：</em>{html.escape(br['method_summary'])}</div>"
                f"<div class='sol-body'>{sol_md}</div>"
                f"<ol class='step-list'>{steps_detail}</ol>"
                "</div>"
            )

        tag_card = (
            "<div class='knowledge-card'>"
            f"<div><strong>L2：</strong>{html.escape('、'.join(item['l2_entities']) or '—')}</div>"
            f"<div><strong>L3：</strong>{html.escape('、'.join(item['l3_operators']) or '—')}</div>"
            f"<div><strong>母题：</strong>{html.escape('、'.join(item['motifs']) or '—')}</div>"
            f"<div><strong>陷阱：</strong>{html.escape('、'.join(item['traps']) or '—')}</div>"
            "</div>"
        )

        details_block = (
            "<div class='screen-details no-print'>"
            f"<details><summary>① 逐步思路提示</summary><ol class='hint-list'>{hints_lis}</ol></details>"
            f"<details><summary>② 标准答案</summary>{ans_html}</details>"
            "<details><summary>③ 完整解析与知识卡片</summary>"
            f"<div class='tabs-bar'>{''.join(tab_btns)}</div>"
            f"{''.join(tab_panels)}"
            f"{tag_card}"
            "</details>"
            "</div>"
        )

        blank_cm = _print_blank_cm(item["format"], item["has_images"])
        cards_html.append(
            f"<article class='q-card'>"
            f"<div class='q-hdr'><span class='q-num'>第 {idx} 题</span><div class='badges'>{meta_badges}</div></div>"
            f"{reason_line}"
            f"<div class='q-stem'>{esc_text}</div>"
            f"{opts_html}"
            f"{imgs_html}"
            f"{details_block}"
            f"<div class='print-scratchpad' style='height: {blank_cm}cm;'>"
            "<span class='scratch-label'>草稿 / 作图演算区</span>"
            "</div>"
            "</article>"
        )

        key_goal = (
            opt_branch["steps"][0]["sub_goal"]
            if opt_branch and opt_branch["steps"]
            else (opt_branch["method_summary"] if opt_branch else "")
        )
        appendix_html.append(
            f"<div class='app-row'><strong>第 {idx} 题 ({html.escape(item['source_id'])})：</strong>"
            f"{html.escape('；'.join(ans_compact) or '—')}"
            + (f" <span class='app-hint'>｜核心：{html.escape(str(key_goal))}</span>" if key_goal else "")
            + "</div>"
        )

    appendix_section = ""
    if print_answers_at_end:
        appendix_section = (
            "<section class='print-answer-appendix'>"
            "<h2>参考答案与核心步骤速查（打印附页）</h2>"
            f"{''.join(appendix_html)}"
            "</section>"
        )

    katex_style_block = f"<style>{katex_css}</style>" if katex_css else ""
    katex_script_block = (
        f"<script>{katex_js}</script><script>{auto_js}</script>"
        if katex_js and auto_js
        else ""
    )

    doc_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{html.escape(batch_title)}</title>
{katex_style_block}
<style>
  :root {{
    --bg: #f8fafc;
    --card: #ffffff;
    --border: #cbd5e1;
    --text: #0f172a;
    --muted: #475569;
    --accent: #0284c7;
    --soft: #f1f5f9;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 24px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    line-height: 1.65;
  }}
  .container {{ max-width: 920px; margin: 0 auto; }}
  .toolbar {{
    display: flex; align-items: center; justify-content: space-between;
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 18px; margin-bottom: 18px;
  }}
  .toolbar h1 {{ margin: 0; font-size: 18px; }}
  .toolbar button {{
    background: var(--accent); color: #fff; border: 0; border-radius: 6px;
    padding: 8px 14px; cursor: pointer; font-weight: 600;
  }}
  .q-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 18px 20px; margin-bottom: 18px; break-inside: avoid; page-break-inside: avoid;
  }}
  .q-hdr {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }}
  .q-num {{ font-weight: 700; font-size: 16px; color: var(--accent); }}
  .badges {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .badge {{ font-size: 12px; padding: 2px 8px; border-radius: 999px; background: var(--soft); color: var(--muted); border: 1px solid var(--border); }}
  .reason-pub {{ font-size: 12px; color: var(--muted); margin-bottom: 8px; }}
  .q-stem {{ font-size: 15px; margin-bottom: 10px; }}
  .options-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 14px; margin-bottom: 10px; }}
  .fig-row {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 10px 0; }}
  .fig-box {{ margin: 0; border: 1px solid var(--border); padding: 6px; border-radius: 6px; background: #fff; text-align: center; }}
  .fig-box img {{ max-width: 340px; max-height: 240px; display: block; margin: 0 auto; }}
  .fig-box figcaption {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .fig-missing {{ font-size: 12px; color: var(--muted); padding: 12px 16px; background: var(--soft); border-radius: 4px; }}
  .screen-details details {{
    margin-top: 8px; border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 12px; background: var(--soft);
  }}
  .screen-details summary {{ cursor: pointer; font-weight: 600; color: var(--text); }}
  .tabs-bar {{ display: flex; gap: 6px; margin: 8px 0; }}
  .tab-btn {{ border: 1px solid var(--border); background: #fff; padding: 4px 10px; border-radius: 4px; cursor: pointer; }}
  .tab-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  .knowledge-card {{
    margin-top: 10px; padding: 8px 10px; border-radius: 6px; background: #fff;
    border: 1px dashed var(--border); font-size: 13px; color: var(--muted);
  }}
  .print-scratchpad {{ display: none; border: 1px dashed #94a3b8; border-radius: 6px; margin-top: 10px; position: relative; }}
  .scratch-label {{ position: absolute; top: 6px; left: 10px; font-size: 11px; color: #94a3b8; }}
  .print-answer-appendix {{
    display: none;
    margin-top: 24px; background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px 20px;
  }}
  .app-row {{ font-size: 13px; margin: 4px 0; }}
  .app-hint {{ color: var(--muted); }}
  @media print {{
    body {{ background: #fff; padding: 0; font-size: 13px; }}
    .container {{ max-width: 100%; }}
    .no-print {{ display: none !important; }}
    .q-card {{ border: 1px solid #cbd5e1; box-shadow: none; margin-bottom: 12px; padding: 12px; }}
    .print-scratchpad {{ display: block; }}
    .print-answer-appendix {{ display: block; page-break-before: always; border: 0; padding: 0; }}
  }}
</style>
</head>
<body>
<div class="container">
  <header class="toolbar no-print">
    <div>
      <h1>{html.escape(batch_title)}</h1>
      <div style="font-size:12px;color:var(--muted);">共 {len(items)} 题 · 离线单文件自包含双模式</div>
    </div>
    <button type="button" onclick="window.print()">打印纸笔练习单</button>
  </header>
  <main>
    {''.join(cards_html)}
  </main>
  {appendix_section}
</div>
{katex_script_block}
<script>
function switchTab(btn, panelId) {{
  var container = btn.closest('details');
  container.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  container.querySelectorAll('.tab-panel').forEach(function(p) {{ p.classList.remove('active'); }});
  btn.classList.add('active');
  var target = document.getElementById(panelId);
  if (target) target.classList.add('active');
}}
document.addEventListener("DOMContentLoaded", function() {{
  if (typeof renderMathInElement === "function") {{
    renderMathInElement(document.body, {{
      delimiters: [
        {{left: "$$", right: "$$", display: true}},
        {{left: "$", right: "$", display: false}}
      ],
      throwOnError: false
    }});
  }}
}});
</script>
</body>
</html>
"""
    final_out.write_text(doc_html, encoding="utf-8")

    if quiz_batch_id:
        conn.execute(
            "UPDATE quiz_batches SET sheet_path = ? WHERE quiz_batch_id = ?;",
            (str(final_out), quiz_batch_id),
        )
        conn.commit()

    return {
        "sheet_path": str(final_out.resolve()),
        "quiz_batch_id": quiz_batch_id,
        "question_count": len(items),
        "embed_images": embed_images,
        "katex_inlined": bool(katex_js),
        "print_answers_at_end": print_answers_at_end,
    }
