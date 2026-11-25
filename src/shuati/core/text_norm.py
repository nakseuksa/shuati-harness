"""文本规范化、选项提取与指纹计算（DESIGN.md §4.2-3 与 §4.2-4）。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

# 全角字母与数字转半角
_FULLWIDTH_TO_HALFWIDTH = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)

# 排除包含几何描述的词，防止将几何顶点声明行误识为选项
_GEOMETRIC_NON_OPTION_SUBSTRINGS = (
    "如图",
)
_GEOMETRIC_NON_OPTION_RE = re.compile(r"点\s*[A-E][.、．)）]")

# 横向选项行检测与分隔符正则（保护 A. ...   B. ... 不被空白折叠压扁）
_HORIZ_OPT_LINE_START_RE = re.compile(r"^[ \t]*[A-E][.、．)）]")
_HORIZ_OPT_SEP_RE = re.compile(r"[ \t]{2,}(?=[B-E][.、．)）])")

_OPT_LETTER_MAP: dict[str, str] = {
    "A": "A",
    "B": "B",
    "C": "C",
    "D": "D",
    "E": "E",
    "Ａ": "A",
    "Ｂ": "B",
    "Ｃ": "C",
    "Ｄ": "D",
    "Ｅ": "E",
}
_OPT_DELIM_FAMILY: dict[str, str] = {
    ".": "dot",
    "．": "dot",
    "、": "dun",
    ")": "paren",
    "）": "paren",
}


def _is_unescaped(text: str, idx: int) -> bool:
    backslashes = 0
    j = idx - 1
    while j >= 0 and text[j] == "\\":
        backslashes += 1
        j -= 1
    return (backslashes % 2) == 0


def _extract_single_space_horizontal_options(line: str) -> list[tuple[str, str]]:
    """安全提取行首起始、单空格分隔的严格连续横向选项。

    仅在满足全部条件时返回候选列表：
    1. 行首为 [A-EＡ-Ｅ][.、．)）]；
    2. 后续选项标记位于空白之后且处于顶层（不在 $...$/$$...$$ 数学模式，也不在未闭合的 ()/（）/[]/【】/{} 内）；
    3. 同一行内所有选项标记使用同一种规范分隔符族（`.`/`．` vs `、` vs `)`/`）`）；
    4. 选项字母严格连续（ord(next) == ord(prev) + 1）、>= 2 项、每项非空且通过几何非选项过滤。
    """
    n = len(line)
    if n < 3:
        return []
    first_letter = _OPT_LETTER_MAP.get(line[0])
    first_family = _OPT_DELIM_FAMILY.get(line[1])
    if first_letter is None or first_family is None:
        return []

    markers: list[tuple[int, int, str]] = [(0, 2, first_letter)]
    math_delim: str | None = None
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0

    i = 2
    while i < n:
        ch = line[i]
        # 1. 追踪 $...$ / $$...$$ 数学跨度，跨度内不切分且不计普通括号深度（兼容半开区间 $[0, 1)$）
        if ch == "$" and _is_unescaped(line, i):
            if math_delim is None:
                if i + 1 < n and line[i + 1] == "$":
                    math_delim = "$$"
                    i += 2
                    continue
                math_delim = "$"
                i += 1
                continue
            if math_delim == "$$" and i + 1 < n and line[i + 1] == "$":
                math_delim = None
                i += 2
                continue
            if math_delim == "$":
                math_delim = None
                i += 1
                continue
            i += 1
            continue

        if math_delim is not None:
            i += 1
            continue

        # 2. 仅在顶层且前置空白时识别同分隔符族的后续选项标记
        if (
            paren_depth == 0
            and bracket_depth == 0
            and brace_depth == 0
            and line[i - 1] in (" ", "\t")
            and i + 1 < n
        ):
            letter = _OPT_LETTER_MAP.get(ch)
            delim_family = _OPT_DELIM_FAMILY.get(line[i + 1])
            if letter is not None and delim_family == first_family:
                markers.append((i, i + 2, letter))
                i += 2
                continue

        # 3. 追踪普通括号深度
        if ch in ("(", "（"):
            paren_depth += 1
        elif ch in (")", "）"):
            paren_depth = max(0, paren_depth - 1)
        elif ch in ("[", "【"):
            bracket_depth += 1
        elif ch in ("]", "】"):
            bracket_depth = max(0, bracket_depth - 1)
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)

        i += 1

    if len(markers) < 2:
        return []

    candidates: list[tuple[str, str]] = []
    for idx, (_m_start, m_end, letter) in enumerate(markers):
        val_end = markers[idx + 1][0] if idx + 1 < len(markers) else n
        val = line[m_end:val_end].strip()
        if not val:
            return []
        if any(sub in val for sub in _GEOMETRIC_NON_OPTION_SUBSTRINGS) or _GEOMETRIC_NON_OPTION_RE.search(val):
            return []
        candidates.append((letter, val))

    if not all(
        ord(candidates[idx][0]) == ord(candidates[idx - 1][0]) + 1
        for idx in range(1, len(candidates))
    ):
        return []

    return candidates


@dataclass(frozen=True)
class NormalizeResult:
    normalized_text: str
    image_captions: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def normalize_text(raw_text: str, num_images: int = 0) -> NormalizeResult:
    """确定性文本规范化与多图题注提取。

    规则（DESIGN.md §4.2-3）：
    ① $...$ 内的 LaTeX 原样保留；
    ② 精确修复 OCR 断词而不吞段落：仅当换行左侧为汉字（或非句末标点 ，、）、右侧为汉字，
       且右侧行不构成独立图注（如 ^图\\s*[0-9一二三四五六七八九十]+$）时直接合并换行；
       句末/分句标点（。？！；：）后的换行完整保留；
    ③ 多图子图题注绑定 vs 单图残留字母清理：当 num_images >= 2 且题干末尾连续孤立短行数量恰好等于图片数、
       匹配子图标签序列（如 ch01_012 末尾 A / B、ch28_083 末尾 图1 / 图2）时，
       将其提取为保序的 image_captions 并从题干末尾剥离；
    ④ 行首尾去空白、连续空白折叠为单空格、多个空行折叠为一个；
    ⑤ 全角数字与字母转半角（Ａ→A），中文标点保持不变。
    """
    stats: dict[str, Any] = {
        "cjk_broken_words_joined": 0,
        "captions_extracted": False,
        "stray_letters_stripped": False,
    }

    # 0. 抽取并保护 $$...$$ 块级 LaTeX 与 $...$ 内联 LaTeX 片段（DESIGN §4.2-3①）
    latex_spans: list[str] = []

    def _save_latex(m: re.Match[str]) -> str:
        latex_spans.append(m.group(0))
        return f"__SHUATI_LATEX_{len(latex_spans) - 1}__"

    block_latex_pattern = re.compile(r"(?<!\\)\$\$(?:\\\$|[\s\S])+?(?<!\\)\$\$")
    text = block_latex_pattern.sub(_save_latex, raw_text)

    inline_latex_pattern = re.compile(r"(?<!\\)\$(?:\\\$|[^$\r\n])+?(?<!\\)\$")
    text = inline_latex_pattern.sub(_save_latex, text)

    # 1. 全角字母与数字转半角
    text = text.translate(_FULLWIDTH_TO_HALFWIDTH)

    # 2. 检查末尾的多图子图题注序列
    image_captions: list[str] = []
    lines = [line.strip() for line in text.split("\n")]

    if num_images >= 2:
        non_empty = [l for l in lines if l]
        if len(non_empty) >= num_images:
            candidate_lines = non_empty[-num_images:]
            is_alpha = candidate_lines == [chr(ord("A") + i) for i in range(num_images)]
            is_fig = candidate_lines == [f"图{i + 1}" for i in range(num_images)]
            if is_alpha or is_fig:
                image_captions = candidate_lines
                stats["captions_extracted"] = True
                match_count = 0
                new_lines: list[str] = []
                for l in reversed(lines):
                    if match_count < num_images and l == candidate_lines[num_images - 1 - match_count]:
                        match_count += 1
                        continue
                    new_lines.append(l)
                lines = list(reversed(new_lines))

    # 单图或无图清理末尾孤立的大写字母（非选项行）
    if num_images < 2:
        non_empty = [l for l in lines if l]
        if non_empty and re.match(r"^[A-Z]$", non_empty[-1]):
            last_val = non_empty[-1]
            new_lines = []
            stripped = False
            for l in reversed(lines):
                if not stripped and l == last_val:
                    stripped = True
                    stats["stray_letters_stripped"] = True
                    continue
                new_lines.append(l)
            lines = list(reversed(new_lines))

    joined_text = "\n".join(lines)

    # 3. 修复汉字间 OCR 断词（右侧为独立图注行 ^图\s*[0-9一二三四五六七八九十]+$ 时绝不合并）
    pattern = re.compile(r"([\u4e00-\u9fff，、])\n+(?!图\s*[0-9一二三四五六七八九十]+(?:\r?\n|$))([\u4e00-\u9fff])")

    def _replace_cjk_newline(m: re.Match[str]) -> str:
        left = m.group(1)
        right = m.group(2)
        stats["cjk_broken_words_joined"] += 1
        return left + right

    prev = None
    curr = joined_text
    while curr != prev:
        prev = curr
        curr = pattern.sub(_replace_cjk_newline, curr)
    joined_text = curr

    # 4. 折叠空白与换行（保留横向选项行中相邻选项间的双空格分隔）
    norm_lines: list[str] = []
    for line in joined_text.split("\n"):
        if _HORIZ_OPT_LINE_START_RE.match(line) and _HORIZ_OPT_SEP_RE.search(line):
            parts = _HORIZ_OPT_SEP_RE.split(line)
            norm_lines.append("  ".join(re.sub(r"[ \t]+", " ", p) for p in parts))
        else:
            norm_lines.append(re.sub(r"[ \t]+", " ", line))
    joined_text = "\n".join(norm_lines)
    joined_text = re.sub(r"\n{3,}", "\n\n", joined_text)
    joined_text = joined_text.strip()

    # 5. 还原受保护的 LaTeX 片段
    for i, span in enumerate(latex_spans):
        joined_text = joined_text.replace(f"__SHUATI_LATEX_{i}__", span)

    return NormalizeResult(
        normalized_text=joined_text,
        image_captions=image_captions,
        stats=stats,
    )


def detect_and_extract_choice_options(text: str) -> tuple[bool, dict[str, str]]:
    r"""行首锚定选择题纠偏正则与结构化选项表提取（DESIGN.md §4.2-4）。

    仅当满足全部条件时判定为选择题：
    1. 存在行首匹配的选项 A：^[ \t]*[AＡ][.、．)）]\s*\S+
    2. 在其后存在行首匹配的选项 B：^[ \t]*[BＢ][.、．)）]\s*\S+
    3. 选项行内容非纯几何顶点声明（不含“如图”“点 A. ”等句式）。
    同时支持单行内或多行网格（如 2x2）以 2 个及以上空格分隔的横向连续选项完整提取。
    """
    line_opt_re = re.compile(r"^[ \t]*([A-EＡ-Ｅ])[.、．)）]\s*(\S.*?)$")
    horizontal_re = re.compile(
        r"([A-EＡ-Ｅ])[.、．)）]\s*(\S.*?)(?=(?:[ \t]{2,}[A-EＡ-Ｅ][.、．)）]|$))"
    )

    norm_letter_map = {"Ａ": "A", "Ｂ": "B", "Ｃ": "C", "Ｄ": "D", "Ｅ": "E"}

    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]

    found_keys: list[str] = []
    temp_opts: dict[str, str] = {}

    for line_idx, line in enumerate(non_empty_lines):
        # 1. 尝试匹配横向多个选项（>= 2 个，2 个及以上空白分隔）
        h_matches = horizontal_re.findall(line)
        if len(h_matches) >= 2:
            row_candidates: list[tuple[str, str]] = []
            valid_row = True
            for raw_k, raw_v in h_matches:
                k = norm_letter_map.get(raw_k.upper(), raw_k.upper())
                v = raw_v.strip()
                if any(sub in v for sub in _GEOMETRIC_NON_OPTION_SUBSTRINGS) or _GEOMETRIC_NON_OPTION_RE.search(v):
                    valid_row = False
                    break
                row_candidates.append((k, v))

            if valid_row and len(row_candidates) >= 2:
                for k, v in row_candidates:
                    if k == "A" and not found_keys:
                        found_keys.append(k)
                        temp_opts[k] = v
                    elif found_keys and ord(k) == ord(found_keys[-1]) + 1:
                        found_keys.append(k)
                        temp_opts[k] = v
                continue

        # 1b. 安全兜底：行首以选项标签起头且单空格分隔的严格连续横向选项（如人工覆写直接输入 "A. 10 B. 20 C. 30 D. 40"）
        m_cur = line_opt_re.match(line)
        if m_cur:
            cur_k = norm_letter_map.get(m_cur.group(1).upper(), m_cur.group(1).upper())
            next_is_consecutive_vertical = False
            if line_idx + 1 < len(non_empty_lines):
                m_next = line_opt_re.match(non_empty_lines[line_idx + 1])
                if m_next:
                    next_k = norm_letter_map.get(m_next.group(1).upper(), m_next.group(1).upper())
                    if ord(next_k) == ord(cur_k) + 1:
                        next_is_consecutive_vertical = True

            if not next_is_consecutive_vertical:
                s_candidates = _extract_single_space_horizontal_options(line)
                if s_candidates:
                    for k, v in s_candidates:
                        if k == "A" and not found_keys:
                            found_keys.append(k)
                            temp_opts[k] = v
                        elif found_keys and ord(k) == ord(found_keys[-1]) + 1:
                            found_keys.append(k)
                            temp_opts[k] = v
                    continue

        # 2. 单行选项匹配
        if m_cur:
            k = m_cur.group(1).upper()
            k = norm_letter_map.get(k, k)
            val = m_cur.group(2).strip()
            # 过滤几何声明
            if any(sub in val for sub in _GEOMETRIC_NON_OPTION_SUBSTRINGS) or _GEOMETRIC_NON_OPTION_RE.search(val):
                continue
            if k == "A" and not found_keys:
                found_keys.append(k)
                temp_opts[k] = val
            elif found_keys and ord(k) == ord(found_keys[-1]) + 1:
                found_keys.append(k)
                temp_opts[k] = val

    if len(found_keys) >= 2 and found_keys[:2] == ["A", "B"]:
        return True, temp_opts

    return False, {}


def compute_content_hash(
    normalized_text: str,
    cas_images: list[str],
    image_captions: list[str] | None = None,
) -> str:
    """保序计算 content_hash（严禁排序 cas_images，保证配图顺序修改能触发重算）。"""
    captions = image_captions or []
    payload = f"{normalized_text}|{','.join(cas_images)}|{','.join(captions)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_plain_fingerprint(normalized_text: str, num_images: int) -> str:
    """计算去格式重扫预检纯文本指纹（用于检出 ch28_015 / ch28_057 等同题重扫对）。"""
    t = normalized_text.replace("$", "")
    t = t.replace("\\triangle", "△")
    t = re.sub(r"\s+", "", t)
    t = t.lower()
    payload = f"{t}|{num_images}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
