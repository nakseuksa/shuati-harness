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

    # 0. 抽取并保护 $...$ 内联 LaTeX 片段（DESIGN §4.2-3①）
    latex_spans: list[str] = []

    def _save_latex(m: re.Match[str]) -> str:
        latex_spans.append(m.group(0))
        return f"__SHUATI_LATEX_{len(latex_spans) - 1}__"

    latex_pattern = re.compile(r"(?<!\\)\$(?:\\\$|[^$\r\n])+?(?<!\\)\$")
    text = latex_pattern.sub(_save_latex, raw_text)

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

    # 4. 折叠空白与换行
    joined_text = re.sub(r"[ \t]+", " ", joined_text)
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
    同时支持单行内以 2 个及以上空格分隔的横向选项。
    """
    line_opt_re = re.compile(r"^[ \t]*([A-EＡ-Ｅ])[.、．)）]\s*(\S.*?)$")
    horizontal_re = re.compile(
        r"([A-EＡ-Ｅ])[.、．)）]\s*(\S.*?)(?=(?:[ \t]{2,}[A-EＡ-Ｅ][.、．)）]|$))"
    )

    norm_letter_map = {"Ａ": "A", "Ｂ": "B", "Ｃ": "C", "Ｄ": "D", "Ｅ": "E"}

    lines = [line.strip() for line in text.splitlines()]

    found_keys: list[str] = []
    temp_opts: dict[str, str] = {}

    for line in lines:
        if not line:
            continue
        m = line_opt_re.match(line)
        if m:
            k = m.group(1).upper()
            k = norm_letter_map.get(k, k)
            val = m.group(2).strip()
            # 过滤几何声明
            if any(sub in val for sub in _GEOMETRIC_NON_OPTION_SUBSTRINGS) or _GEOMETRIC_NON_OPTION_RE.search(val):
                continue
            if k == "A" and not found_keys:
                found_keys.append(k)
                temp_opts[k] = val
            elif found_keys and ord(k) == ord(found_keys[-1]) + 1:
                found_keys.append(k)
                temp_opts[k] = val

    if len(found_keys) >= 2 and "A" in found_keys and "B" in found_keys:
        return True, temp_opts

    # 检查横向选项
    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        matches = horizontal_re.findall(line_clean)
        if matches:
            keys = [m[0].upper() for m in matches]
            norm_keys = [norm_letter_map.get(k, k) for k in keys]
            if len(norm_keys) >= 2 and norm_keys[:2] == ["A", "B"]:
                # 检查连续性
                is_seq = True
                for i in range(1, len(norm_keys)):
                    if ord(norm_keys[i]) != ord(norm_keys[i - 1]) + 1:
                        is_seq = False
                        break
                if is_seq:
                    opts = {k: v.strip() for k, v in zip(norm_keys, [m[1] for m in matches])}
                    if any(
                        any(sub in v for sub in _GEOMETRIC_NON_OPTION_SUBSTRINGS) or _GEOMETRIC_NON_OPTION_RE.search(v)
                        for v in opts.values()
                    ):
                        continue
                    return True, opts

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
