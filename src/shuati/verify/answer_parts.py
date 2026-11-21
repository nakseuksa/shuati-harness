"""显式小问标签归一、选择题与符号填空归一、多值校验与题目级验真模式派生。"""

from __future__ import annotations

import re
from typing import Any, Literal

from shuati.models.domain import AnswerPart

_FULLWIDTH_TRANS = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ（）",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz()",
)

_CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_CIRCLED_MAP = {ch: str(idx + 1) for idx, ch in enumerate(_CIRCLED_DIGITS)}

_SUB_LABEL_PATTERNS = (
    re.compile(r"^\(\d+\)\s*[-._－—]"),
    re.compile(r"^\(\d+\)\s*[A-Za-z0-9]+$"),
    re.compile(r"^\d+\s*[-_]\s*\d+$"),
    re.compile(r"^\d+[).]\s*[-._－—]"),
    re.compile(r"^[①-⑳]\s*[-._－—]"),
)

_SYMBOL_FILL_EXACT_MAP: dict[str, str] = {
    ">": ">",
    "＞": ">",
    "大于": ">",
    "\\gt": ">",
    "<": "<",
    "＜": "<",
    "小于": "<",
    "\\lt": "<",
    "=": "=",
    "＝": "=",
    "等于": "=",
    "相等": "=",
    ">=": ">=",
    "≥": ">=",
    "≧": ">=",
    "大于等于": ">=",
    "大于或等于": ">=",
    "不小于": ">=",
    "\\ge": ">=",
    "\\geq": ">=",
    "\\geqslant": ">=",
    "<=": "<=",
    "≤": "<=",
    "≦": "<=",
    "小于等于": "<=",
    "小于或等于": "<=",
    "不大于": "<=",
    "\\le": "<=",
    "\\leq": "<=",
    "\\leqslant": "<=",
    "!=": "!=",
    "≠": "!=",
    "≢": "!=",
    "不等于": "!=",
    "不等": "!=",
    "\\ne": "!=",
    "\\neq": "!=",
    "+": "+",
    "＋": "+",
    "加": "+",
    "加上": "+",
    "加号": "+",
    "-": "-",
    "－": "-",
    "—": "-",
    "−": "-",
    "–": "-",
    "减": "-",
    "减去": "-",
    "减号": "-",
    "*": "*",
    "＊": "*",
    "×": "*",
    "✕": "*",
    "✖": "*",
    "·": "*",
    "•": "*",
    "乘": "*",
    "乘以": "*",
    "乘号": "*",
    "\\times": "*",
    "\\cdot": "*",
    "\\ast": "*",
    "/": "/",
    "／": "/",
    "÷": "/",
    "除": "/",
    "除以": "/",
    "除号": "/",
    "\\div": "/",
}


def normalize_part_label(raw_label: str) -> str:
    """显式标签归一：'（1）'/'①'/'1)'/'1.' -> '(1)'；'主问'/'' -> '主问'；拒绝 '(1)-1' 等自造子标签（抛 ValueError）。"""
    if not isinstance(raw_label, str):
        raise ValueError(f"小问标签必须为字符串，收到: {type(raw_label).__name__}")

    s = raw_label.strip().translate(_FULLWIDTH_TRANS)
    if not s or s == "主问":
        return "主问"

    for pat in _SUB_LABEL_PATTERNS:
        if pat.match(s):
            raise ValueError(f"不合法的自造子小问标签 '{raw_label}'：严禁拆分出子标签")

    if s in _CIRCLED_MAP:
        return f"({_CIRCLED_MAP[s]})"

    # (1) / 1) / 1. / 1、 / 1 / 第1问 / 第(1)问 / 问1
    m = re.fullmatch(
        r"(?:\((\d+)\)|(\d+)[).、]?|第\s*\(?(\d+)\)?\s*问|问\s*\(?(\d+)\)?)",
        s,
    )
    if m:
        num_str = next(g for g in m.groups() if g is not None)
        num = int(num_str)
        if num <= 0:
            raise ValueError(f"小问序号必须为正整数: '{raw_label}'")
        return f"({num})"

    raise ValueError(f"无法归一的小问标签 '{raw_label}'：仅允许 '主问' 或显式数字编号 '(1)', '(2)' ...")


def normalize_choice_label(raw: str) -> str:
    """选择题字母大写排序去重：' c, a ' -> 'AC'。"""
    if not isinstance(raw, str):
        raise ValueError(f"选择题标签必须为字符串，收到: {type(raw).__name__}")
    s = raw.strip().translate(_FULLWIDTH_TRANS).upper()
    letters = sorted({ch for ch in s if "A" <= ch <= "Z"})
    return "".join(letters)


def normalize_symbol_fill(raw: str) -> str:
    """符号填空归一：全角/中文词 -> 标准半角符号（'>','<','=','>=','<=','!=','+','-','*','/'）。"""
    if not isinstance(raw, str):
        raise ValueError(f"符号填空值必须为字符串，收到: {type(raw).__name__}")
    s = raw.strip().replace("$", "").strip()
    if s in _SYMBOL_FILL_EXACT_MAP:
        return _SYMBOL_FILL_EXACT_MAP[s]
    s_half = s.translate(_FULLWIDTH_TRANS).strip()
    if s_half in _SYMBOL_FILL_EXACT_MAP:
        return _SYMBOL_FILL_EXACT_MAP[s_half]
    # 折叠内部空白（如 "大于 等于"）
    s_compact = re.sub(r"\s+", "", s_half)
    if s_compact in _SYMBOL_FILL_EXACT_MAP:
        return _SYMBOL_FILL_EXACT_MAP[s_compact]
    return s_half


def _normalize_items(items: list[Any] | None) -> list[Any] | None:
    if items is None:
        return None
    normalized: list[Any] = []
    for item in items:
        if isinstance(item, dict):
            new_item = dict(item)
            if new_item.get("item_type") == "SYMBOL_FILL" and isinstance(new_item.get("canonical"), str):
                new_item["canonical"] = normalize_symbol_fill(new_item["canonical"])
            normalized.append(new_item)
        else:
            normalized.append(item)
    return normalized


def normalize_answer_parts(parts: list[AnswerPart]) -> list[AnswerPart]:
    """批量归一每个 AnswerPart 的 label、choice_label、canonical，校验无重复 label。"""
    result: list[AnswerPart] = []
    seen_labels: set[str] = set()

    for part in parts:
        norm_label = normalize_part_label(part.label)
        if norm_label in seen_labels:
            raise ValueError(f"存在重复的小问标签: '{norm_label}'")
        seen_labels.add(norm_label)

        updates: dict[str, Any] = {"label": norm_label}
        if part.answer_type == "CHOICE" and part.choice_label is not None:
            norm_choice = normalize_choice_label(part.choice_label)
            if not norm_choice:
                raise ValueError(f"CHOICE 小问 '{norm_label}' 的 choice_label 归一后为空")
            updates["choice_label"] = norm_choice

        if part.answer_type == "SYMBOL_FILL" and part.canonical is not None:
            norm_sym = normalize_symbol_fill(part.canonical)
            if not norm_sym:
                raise ValueError(f"SYMBOL_FILL 小问 '{norm_label}' 的 canonical 归一后为空")
            updates["canonical"] = norm_sym

        if part.items is not None:
            updates["items"] = _normalize_items(part.items)

        result.append(part.model_copy(update=updates))

    return result


def derive_verification_mode(parts: list[AnswerPart]) -> Literal["DETERMINISTIC", "QUALITATIVE", "MIXED"]:
    """派生题目级 verification_mode。"""
    if not parts:
        return "DETERMINISTIC"
    vmodes = {p.verify_mode for p in parts}
    if vmodes.issubset({"SYMBOLIC", "EXACT"}):
        return "DETERMINISTIC"
    if vmodes == {"QUALITATIVE"}:
        return "QUALITATIVE"
    return "MIXED"
