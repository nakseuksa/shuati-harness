"""逐小问等价判定引擎：LaTeX 转纯文本、单位剥离、SYMBOLIC（含比值/π≈3.14/多重集/映射）/EXACT/QUALITATIVE 判等。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from shuati.models.domain import AnswerPart
from shuati.models.schemas import QualitativeJudgeOutput
from shuati.verify.answer_parts import (
    normalize_choice_label,
    normalize_part_label,
    normalize_symbol_fill,
)
from shuati.verify.sympy_sandbox import SympySandboxResult, run_symbolic_check_in_sandbox

_FULLWIDTH_TRANS = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ（）［］｛｝＋－＊／＝＜＞：，．；",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz()[]{}+-*/=<>:,.;",
)

_IMPLICATION_SEPS = ("\\Rightarrow", "\\Longrightarrow", "\\implies", "⇒", "→")
_TEXT_CMD_RE = re.compile(r"\\(?:text|mathrm|mbox|operatorname)\s*\{[^{}]*\}")
_FRAC_RE = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SHORT_FRAC_RE = re.compile(
    r"\\frac\s*(?:\{([^{}]*)\}|([A-Za-z0-9]))\s*(?:\{([^{}]*)\}|([A-Za-z0-9]))"
)
_SQRT_NTH_RE = re.compile(r"\\sqrt\s*\[([^\[\]]+)\]\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_LEFT_RIGHT_RE = re.compile(r"\\(?:left|right)\b")
_SUB_BRACE_RE = re.compile(r"_\{([^{}]*)\}")
_SUP_BRACE_RE = re.compile(r"\^\{([^{}]*)\}")
_SUB_CHAR_RE = re.compile(r"_([A-Za-z0-9])")
_SPACING_RE = re.compile(r"\\[,;! ]")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\\?%")
_COMMAND_RE = re.compile(r"\\([A-Za-z]+)")

_CN_UNIT_PHRASES: tuple[str, ...] = (
    "千米每小时",
    "平方厘米",
    "平方分米",
    "平方毫米",
    "平方千米",
    "立方厘米",
    "立方分米",
    "立方毫米",
    "立方米",
    "平方米",
    "米每秒",
    "米/秒",
    "厘米",
    "毫米",
    "分米",
    "千米",
    "公里",
    "公顷",
    "分钟",
    "小时",
    "千克",
    "毫升",
    "米",
    "秒",
    "天",
    "克",
    "吨",
    "元",
    "角",
    "升",
    "度",
    "个",
    "只",
    "条",
    "种",
    "本",
    "张",
    "辆",
    "人",
    "岁",
    "名",
    "件",
    "块",
    "°",
)

_CN_UNIT_RE = re.compile(
    r"(?:" + "|".join(re.escape(p) for p in sorted(_CN_UNIT_PHRASES, key=len, reverse=True)) + r")[\s。，,.]*$"
)

_MULTI_LATIN_UNIT_RE = re.compile(
    r"(?<=[\d\s\)])\s*(?:cm\^2|cm\^3|mm\^2|mm\^3|dm\^2|dm\^3|km\^2|m\^3|m\^2|cm2|m2|m/s|km/h|min|mm|cm|dm|km|kg|ml|mL)[\s。，,.]*$"
)
_SINGLE_LATIN_UNIT_RE = re.compile(r"(?<=[\d\s])\s*(?:m|g|t|h|s|L)[\s。，,.]*$")

_QUANTITY_UNITS: dict[str, tuple[str, float]] = {
    "小时": ("time", 60.0),
    "时": ("time", 60.0),
    "分钟": ("time", 1.0),
    "分": ("time", 1.0),
    "min": ("time", 1.0),
    "秒": ("time", 1.0 / 60.0),
    "千米": ("length", 1000.0),
    "公里": ("length", 1000.0),
    "km": ("length", 1000.0),
    "米": ("length", 1.0),
    "分米": ("length", 0.1),
    "dm": ("length", 0.1),
    "厘米": ("length", 0.01),
    "cm": ("length", 0.01),
    "毫米": ("length", 0.001),
    "mm": ("length", 0.001),
    "吨": ("mass", 1_000_000.0),
    "千克": ("mass", 1000.0),
    "kg": ("mass", 1000.0),
    "克": ("mass", 1.0),
    "元": ("money", 1.0),
    "角": ("money", 0.1),
}
_QUANTITY_UNIT_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*("
    + "|".join(re.escape(k) for k in sorted(_QUANTITY_UNITS, key=len, reverse=True))
    + r")"
)


@dataclass
class PartVerdict:
    label: str
    verify_mode: str
    dual_equal: bool
    sympy_ok: bool | None  # 非 SYMBOLIC 小问为 None
    judge_note: str = ""


@dataclass
class EquivalenceSummary:
    all_dual_equal: bool
    all_sympy_ok: bool
    part_count_mismatch: bool
    review_reasons: list[str] = field(default_factory=list)
    part_verdicts: list[PartVerdict] = field(default_factory=list)
    sympy_status: Literal[
        "PASSED",
        "PARTIAL",
        "FAILED",
        "TIMEOUT",
        "AST_REJECTED",
        "SKIPPED_NO_SYMBOLIC",
        "NOT_RUN",
    ] = "NOT_RUN"
    sympy_output: dict[str, Any] = field(default_factory=dict)


def _fullwidth_to_halfwidth(text: str) -> str:
    return (text or "").translate(_FULLWIDTH_TRANS).replace("\u3000", " ")


def _is_pure_numeric(text: str) -> bool:
    return (
        bool(text)
        and "=" not in text
        and re.search(r"[A-Za-z]", text) is None
        and any(ch.isdigit() for ch in text)
    )


def _replace_short_frac(match: re.Match[str]) -> str:
    num = match.group(1) if match.group(1) is not None else match.group(2)
    den = match.group(3) if match.group(3) is not None else match.group(4)
    return f"(({num})/({den}))"


def latex_to_plain(text: str) -> str:
    """LaTeX 答案转纯表达式（兼容 \\frac, \\sqrt, \\pi, \\times, \\div, \\cdot, \\text, 全角等）。"""
    s = _fullwidth_to_halfwidth(text).strip()
    s = s.replace("$", "")
    # 保护显式转义集合花括号 \{ \}，避免被当作 LaTeX 分组括号转换为 ()
    s = s.replace(r"\{", "\x00LBRACE\x00").replace(r"\}", "\x00RBRACE\x00")
    s = re.sub(r"\^\s*\{\s*\\circ\s*\}|\^\s*\\circ", "", s)
    s = _TEXT_CMD_RE.sub("", s).replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac")
    for _ in range(8):
        expanded = _SQRT_NTH_RE.sub(r"((\2)**(1/(\1)))", s)
        expanded = _SQRT_RE.sub(r"sqrt(\1)", expanded)
        expanded = _FRAC_RE.sub(r"((\1)/(\2))", expanded)
        expanded = _SHORT_FRAC_RE.sub(_replace_short_frac, expanded)
        if expanded == s:
            break
        s = expanded
    s = _LEFT_RIGHT_RE.sub("", s)
    s = _SUB_BRACE_RE.sub(r"\1", s)
    s = _SUP_BRACE_RE.sub(r"^(\1)", s)
    s = _SUB_CHAR_RE.sub(r"\1", s)
    s = s.replace(r"\cdot", " * ").replace(r"\times", " * ").replace(r"\div", " / ")
    s = s.replace("×", " * ").replace("÷", " / ").replace("·", " * ")
    s = (
        s.replace(r"\leqslant", "<=")
        .replace(r"\leq", "<=")
        .replace(r"\le", "<=")
        .replace(r"\geqslant", ">=")
        .replace(r"\geq", ">=")
        .replace(r"\ge", ">=")
        .replace(r"\neq", "!=")
        .replace(r"\ne", "!=")
        .replace(r"\cup", " U ")
        .replace(r"\infty", "oo")
        .replace(r"\emptyset", "EmptySet")
        .replace(r"\pi", " pi ")
    )
    s = s.replace("π", " pi ").replace("≤", "<=").replace("≥", ">=").replace("≠", "!=")
    s = _PERCENT_RE.sub(r"((\1)/100)", s)
    s = _SPACING_RE.sub("", s)
    s = s.replace("~", " ")
    s = _COMMAND_RE.sub(r"\1", s)
    # 保护未转义的有限集花括号 {a, b, ...} 或空集 {}；仅将无逗号的残留分组花括号转为 ()
    s = re.sub(r"\{([^{}]*,[^{}]*)\}", "\x00LBRACE\x00\\1\x00RBRACE\x00", s)
    s = s.replace("{}", "\x00LBRACE\x00\x00RBRACE\x00")
    s = s.replace("{", "(").replace("}", ")").replace("\\", "").replace("_", "")
    s = s.replace("\x00LBRACE\x00", "{").replace("\x00RBRACE\x00", "}")
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"(?<![><!])\s*=\s*", " = ", s).strip()


def strip_units(text: str, *, allow_latin_unit: bool = False) -> str:
    """剥离尾部中文单位（及纯数字下的拉丁单位）。"""
    s = _fullwidth_to_halfwidth(text).strip()
    for _ in range(4):
        stripped = _CN_UNIT_RE.sub("", s).strip()
        if stripped == s:
            break
        s = stripped

    if allow_latin_unit:
        multi = _MULTI_LATIN_UNIT_RE.sub("", s).strip()
        if multi != s:
            s = multi
        m_single = _SINGLE_LATIN_UNIT_RE.search(s)
        if m_single is not None:
            prefix = s[: m_single.start()].strip()
            if _is_pure_numeric(prefix):
                s = prefix

    return s.strip()


def _parse_quantity(text: str) -> tuple[float, str] | None:
    s = _fullwidth_to_halfwidth(text).strip()
    if not s:
        return None
    for sep in _IMPLICATION_SEPS:
        if sep in s:
            s = s.split(sep)[-1].strip()
    s = re.sub(r"\\(?:text|mathrm|mbox|operatorname)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:qquad|quad|[,;:!])|\\ |~", " ", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip("。，,.")
    money_ctx = "元" in s or "角" in s
    pos = 0
    total = 0.0
    dim: str | None = None
    last_unit = ""
    matched = False
    while pos < len(s):
        m = _QUANTITY_UNIT_RE.match(s, pos)
        if m is not None:
            if matched and m.group(1).startswith("-"):
                return None
            num = float(m.group(1))
            unit = m.group(2)
            if unit == "分" and money_ctx:
                d, f = "money", 0.01
            else:
                d, f = _QUANTITY_UNITS[unit]
            if dim is None:
                dim = d
            elif d != dim:
                return None
            total += num * f
            last_unit = unit
            matched = True
            pos = m.end()
            while pos < len(s) and s[pos] == " ":
                pos += 1
            continue
        m2 = re.match(r"(\d+(?:\.\d+)?)$", s[pos:])
        if m2 is not None and matched and last_unit in ("小时", "时") and dim == "time":
            total += float(m2.group(1))
            pos = len(s)
            continue
        return None
    if not matched or dim is None:
        return None
    return total, dim


def _prepare_expr(text: str, *, allow_latin_unit: bool = True) -> str:
    s = _fullwidth_to_halfwidth(text).strip()
    for sep in _IMPLICATION_SEPS:
        if sep in s:
            s = s.split(sep)[-1].strip()
    s = latex_to_plain(s)
    s = strip_units(s, allow_latin_unit=allow_latin_unit)
    return s.strip()


async def _symbolic_equal(
    expr_a: str,
    expr_b: str,
    *,
    mode: Literal["EXPRESSION", "RATIO", "SET_OR_INTERVAL"],
    allow_pi_314: bool,
    timeout_seconds: float,
    memory_limit_mb: int,
) -> tuple[bool, str | None]:
    if mode == "EXPRESSION":
        qa = _parse_quantity(expr_a)
        qb = _parse_quantity(expr_b)
        if qa is not None and qb is not None:
            return (qa[1] == qb[1] and abs(qa[0] - qb[0]) <= 1e-9 * max(1.0, abs(qa[0]), abs(qb[0]))), None

    pa = _prepare_expr(expr_a, allow_latin_unit=True)
    pb = _prepare_expr(expr_b, allow_latin_unit=True)
    if not pa or not pb:
        return False, "BAD_INPUT"

    if mode != "SET_OR_INTERVAL" and pa == pb and (_is_pure_numeric(pa) or re.fullmatch(r"[A-Za-z0-9+\-*/^().\s]+", pa)):
        return True, None

    return await asyncio.to_thread(
        run_symbolic_check_in_sandbox,
        pa,
        pb,
        mode=mode,
        allow_pi_314=allow_pi_314,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    )


def _extract_item_info(item: Any) -> tuple[str, str]:
    if isinstance(item, dict):
        itype = str(item.get("item_type") or item.get("answer_type") or "NUMERIC").upper()
        if itype == "SYMBOL_FILL":
            val = str(item.get("canonical") or item.get("expr") or item.get("value") or item.get("answer") or "")
        else:
            val = str(
                item.get("expr")
                or item.get("sympy_expr")
                or item.get("value")
                or item.get("answer")
                or item.get("canonical")
                or ""
            )
        return itype, val
    return "NUMERIC", str(item)


def _extract_labeled_map(items: list[Any] | None) -> dict[str, tuple[str, str]] | None:
    if not items:
        return None
    mapping: dict[str, tuple[str, str]] = {}
    for item in items:
        if isinstance(item, dict):
            raw_key = item.get("label") or item.get("key") or item.get("name") or item.get("entity")
            if raw_key is not None:
                key = re.sub(r"\s+", "", str(raw_key))
                itype, val = _extract_item_info(item)
                if key in mapping:
                    return None
                mapping[key] = (itype, val)
                continue
            if len(item) == 1:
                k, v = next(iter(item.items()))
                key = re.sub(r"\s+", "", str(k))
                if key in mapping:
                    return None
                mapping[key] = ("NUMERIC", str(v))
                continue
            return None
        if isinstance(item, str) and (":" in item or "=" in item):
            sep = ":" if ":" in item else "="
            k, v = item.split(sep, 1)
            key = re.sub(r"\s+", "", k)
            if key in mapping:
                return None
            mapping[key] = ("NUMERIC", v.strip())
        else:
            return None
    return mapping or None


def _sandbox_mode_for_part(part: AnswerPart) -> Literal["EXPRESSION", "RATIO", "SET_OR_INTERVAL"]:
    if part.answer_type == "RATIO":
        return "RATIO"
    if part.answer_type == "SET_OR_INTERVAL":
        return "SET_OR_INTERVAL"
    return "EXPRESSION"


async def _items_pair_equal(
    item_a: tuple[str, str],
    item_b: tuple[str, str],
    *,
    mode: Literal["EXPRESSION", "RATIO", "SET_OR_INTERVAL"],
    allow_pi_314: bool,
    timeout_seconds: float,
    memory_limit_mb: int,
) -> bool:
    type_a, val_a = item_a
    type_b, val_b = item_b
    if type_a == "SYMBOL_FILL" or type_b == "SYMBOL_FILL":
        if type_a != type_b:
            return False
        return normalize_symbol_fill(val_a) == normalize_symbol_fill(val_b)
    eq, _ = await _symbolic_equal(
        val_a,
        val_b,
        mode=mode,
        allow_pi_314=allow_pi_314,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    )
    return eq


async def _compare_symbolic_parts(
    p: AnswerPart,
    s: AnswerPart,
    *,
    allow_pi_314: bool,
    timeout_seconds: float,
    memory_limit_mb: int,
) -> bool:
    if p.collection_mode != s.collection_mode:
        return False
    mode = _sandbox_mode_for_part(p)
    if p.collection_mode == "SINGLE":
        va = p.sympy_expr or p.answer
        vb = s.sympy_expr or s.answer
        eq, _ = await _symbolic_equal(
            va,
            vb,
            mode=mode,
            allow_pi_314=allow_pi_314,
            timeout_seconds=timeout_seconds,
            memory_limit_mb=memory_limit_mb,
        )
        return eq

    p_items = p.items or []
    s_items = s.items or []
    if len(p_items) != len(s_items) or not p_items:
        return False

    if p.collection_mode == "ORDERED_TUPLE":
        for ia, ib in zip(p_items, s_items, strict=True):
            if not await _items_pair_equal(
                _extract_item_info(ia),
                _extract_item_info(ib),
                mode=mode,
                allow_pi_314=allow_pi_314,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
            ):
                return False
        return True

    if p.collection_mode == "UNORDERED_MULTISET":
        used: set[int] = set()
        # Pass 1: 快速精确文本匹配，减少沙盒开销
        rem_a: list[tuple[str, str]] = []
        for ia in p_items:
            ta, va = _extract_item_info(ia)
            pa_norm = _prepare_expr(va, allow_latin_unit=True) if ta != "SYMBOL_FILL" else normalize_symbol_fill(va)
            found_idx = None
            for idx, ib in enumerate(s_items):
                if idx in used:
                    continue
                tb, vb = _extract_item_info(ib)
                if ta == tb:
                    pb_norm = _prepare_expr(vb, allow_latin_unit=True) if tb != "SYMBOL_FILL" else normalize_symbol_fill(vb)
                    if pa_norm and pa_norm == pb_norm:
                        found_idx = idx
                        break
            if found_idx is not None:
                used.add(found_idx)
            else:
                rem_a.append((ta, va))

        for info_a in rem_a:
            matched = False
            for idx, ib in enumerate(s_items):
                if idx in used:
                    continue
                if await _items_pair_equal(
                    info_a,
                    _extract_item_info(ib),
                    mode=mode,
                    allow_pi_314=allow_pi_314,
                    timeout_seconds=timeout_seconds,
                    memory_limit_mb=memory_limit_mb,
                ):
                    used.add(idx)
                    matched = True
                    break
            if not matched:
                return False
        return True

    if p.collection_mode == "LABELED_MAP":
        map_a = _extract_labeled_map(p_items)
        map_b = _extract_labeled_map(s_items)
        if map_a is None or map_b is None or set(map_a.keys()) != set(map_b.keys()):
            return False
        for k in map_a:
            if not await _items_pair_equal(
                map_a[k],
                map_b[k],
                mode=mode,
                allow_pi_314=allow_pi_314,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
            ):
                return False
        return True

    return False


async def _verify_part_against_sandbox(
    p: AnswerPart,
    sbx_val: str | list[str],
    *,
    allow_pi_314: bool,
    timeout_seconds: float,
    memory_limit_mb: int,
) -> bool:
    mode = _sandbox_mode_for_part(p)
    if p.collection_mode == "SINGLE":
        sbx_str = sbx_val[0] if isinstance(sbx_val, list) and len(sbx_val) == 1 else str(sbx_val)
        if isinstance(sbx_val, list) and len(sbx_val) != 1:
            return False
        claimed = p.sympy_expr or p.answer
        eq, _ = await _symbolic_equal(
            claimed,
            sbx_str,
            mode=mode,
            allow_pi_314=allow_pi_314,
            timeout_seconds=timeout_seconds,
            memory_limit_mb=memory_limit_mb,
        )
        return eq

    p_items = p.items or []
    if not p_items:
        return False

    if isinstance(sbx_val, list):
        sbx_list = sbx_val
    else:
        sbx_list = [x.strip() for x in str(sbx_val).split(",") if x.strip()]

    if p.collection_mode == "ORDERED_TUPLE":
        if len(p_items) != len(sbx_list):
            return False
        for ia, sb in zip(p_items, sbx_list, strict=True):
            if not await _items_pair_equal(
                _extract_item_info(ia),
                ("NUMERIC", sb),
                mode=mode,
                allow_pi_314=allow_pi_314,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
            ):
                # Also allow SYMBOL_FILL comparison if ia is SYMBOL_FILL
                ta, va = _extract_item_info(ia)
                if ta == "SYMBOL_FILL" and normalize_symbol_fill(va) == normalize_symbol_fill(sb):
                    continue
                return False
        return True

    if p.collection_mode == "UNORDERED_MULTISET":
        if len(p_items) != len(sbx_list):
            return False
        used: set[int] = set()
        for ia in p_items:
            info_a = _extract_item_info(ia)
            matched = False
            for idx, sb in enumerate(sbx_list):
                if idx in used:
                    continue
                if await _items_pair_equal(
                    info_a,
                    ("NUMERIC", sb),
                    mode=mode,
                    allow_pi_314=allow_pi_314,
                    timeout_seconds=timeout_seconds,
                    memory_limit_mb=memory_limit_mb,
                ):
                    used.add(idx)
                    matched = True
                    break
            if not matched:
                return False
        return True

    if p.collection_mode == "LABELED_MAP":
        map_a = _extract_labeled_map(p_items)
        if map_a is None or len(map_a) != len(sbx_list):
            return False
        if all(isinstance(x, str) and (":" in x or "=" in x) for x in sbx_list):
            map_b = _extract_labeled_map(sbx_list)
            if map_b is None or set(map_a.keys()) != set(map_b.keys()):
                return False
            for k in map_a:
                if not await _items_pair_equal(
                    map_a[k],
                    map_b[k],
                    mode=mode,
                    allow_pi_314=allow_pi_314,
                    timeout_seconds=timeout_seconds,
                    memory_limit_mb=memory_limit_mb,
                ):
                    return False
            return True
        # Fallback: order matches items
        for ia, sb in zip(p_items, sbx_list, strict=True):
            if not await _items_pair_equal(
                _extract_item_info(ia),
                ("NUMERIC", sb),
                mode=mode,
                allow_pi_314=allow_pi_314,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
            ):
                return False
        return True

    return False


def _is_parseable_numeric_option(option_text: str) -> str | None:
    """若选项文本剥离单位与 LaTeX 后不含汉字且为安全数学表达式，返回清洗后的表达式，否则返回 None。"""
    cleaned = _prepare_expr(option_text, allow_latin_unit=True)
    if not cleaned:
        return None
    if re.search(r"[\u4e00-\u9fff]", cleaned):
        return None
    # 多词英文句子（非数学函数）不视为数值选项
    words = re.findall(r"[A-Za-z]+", cleaned)
    allowed_words = {"pi", "sqrt", "sin", "cos", "tan", "log", "ln", "exp", "oo", "inf", "Abs", "gcd", "lcm"}
    for w in words:
        if len(w) > 2 and w not in allowed_words:
            return None
    if not any(ch.isdigit() for ch in cleaned) and "pi" not in words:
        return None
    return cleaned


def _normalize_qualitative_text(text: str) -> str:
    s = _fullwidth_to_halfwidth(text or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s.rstrip("。，,.;；")


def _add_reason(reasons: list[str], code: str) -> None:
    if code not in reasons:
        reasons.append(code)


async def _check_choice_option_vs_sympy(
    part: AnswerPart,
    options: dict[str, str] | None,
    *,
    allow_pi_314: bool,
    timeout_seconds: float,
    memory_limit_mb: int,
) -> bool | None:
    """当 CHOICE 题有 options 且对应选项可解析为数值/表达式且有 sympy_expr 时执行互校。"""
    if not options or not part.sympy_expr or not part.sympy_expr.strip():
        return None
    choice = normalize_choice_label(part.choice_label or part.answer)
    if len(choice) != 1 or choice not in options:
        return None
    opt_expr = _is_parseable_numeric_option(options[choice])
    if opt_expr is None:
        return None
    eq, err = await _symbolic_equal(
        opt_expr,
        part.sympy_expr,
        mode="EXPRESSION",
        allow_pi_314=allow_pi_314,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    )
    if err is not None:
        return None
    return eq


async def _compare_choice_and_numeric(
    choice_part: AnswerPart,
    other_part: AnswerPart,
    options: dict[str, str] | None,
    *,
    allow_pi_314: bool,
    timeout_seconds: float,
    memory_limit_mb: int,
) -> bool:
    if not options:
        return False
    choice = normalize_choice_label(choice_part.choice_label or choice_part.answer)
    if len(choice) != 1 or choice not in options:
        return False
    opt_raw = options[choice]
    opt_expr = _is_parseable_numeric_option(opt_raw)
    other_val = other_part.sympy_expr or other_part.answer
    if opt_expr is not None:
        eq, _ = await _symbolic_equal(
            opt_expr,
            other_val,
            mode="EXPRESSION",
            allow_pi_314=allow_pi_314,
            timeout_seconds=timeout_seconds,
            memory_limit_mb=memory_limit_mb,
        )
        if eq:
            return True
    return _normalize_qualitative_text(opt_raw) == _normalize_qualitative_text(other_val)


def _normalize_part_map(parts: list[AnswerPart]) -> tuple[dict[str, AnswerPart], bool]:
    mapping: dict[str, AnswerPart] = {}
    has_dup = False
    for part in parts:
        try:
            lbl = normalize_part_label(part.label)
        except ValueError:
            lbl = part.label.strip()
            has_dup = True
        if lbl in mapping:
            has_dup = True
        else:
            mapping[lbl] = part.model_copy(update={"label": lbl})
    return mapping, has_dup


async def compare_answer_parts(
    primary_parts: list[AnswerPart],
    secondary_parts: list[AnswerPart] | None,
    *,
    grade_subject: str,
    allow_pi_314_subjects: list[str],
    options: dict[str, str] | None = None,
    sympy_result: SympySandboxResult | None = None,
    qualitative_judge: Callable[[str, str, str], Awaitable[QualitativeJudgeOutput]] | None = None,
    timeout_seconds: float = 5.0,
    memory_limit_mb: int = 512,
) -> EquivalenceSummary:
    """逐小问双盲判等 + SymPy 校验。"""
    allow_pi_314 = grade_subject in allow_pi_314_subjects
    review_reasons: list[str] = []
    part_verdicts: list[PartVerdict] = []

    p_map, p_dup = _normalize_part_map(primary_parts)
    s_map: dict[str, AnswerPart] = {}
    s_dup = False
    part_count_mismatch = False

    if secondary_parts is not None:
        s_map, s_dup = _normalize_part_map(secondary_parts)
        if p_dup or s_dup or set(p_map.keys()) != set(s_map.keys()) or len(primary_parts) != len(secondary_parts):
            part_count_mismatch = True
            _add_reason(review_reasons, "PART_COUNT_MISMATCH")

    # 预处理沙盒返回字典（键也尝试做 label 归一）
    sbx_values_norm: dict[str, str | list[str]] = {}
    if sympy_result is not None and sympy_result.ok and sympy_result.values:
        for raw_k, v in sympy_result.values.items():
            try:
                nk = normalize_part_label(raw_k)
            except ValueError:
                nk = raw_k.strip()
            sbx_values_norm[nk] = v

    sympy_parts_out: list[dict[str, Any]] = []
    symbolic_checks_ok: list[bool] = []
    has_symbolic_part = any(p.verify_mode == "SYMBOLIC" for p in p_map.values())

    if sympy_result is not None and not sympy_result.ok:
        if sympy_result.status == "TIMEOUT":
            _add_reason(review_reasons, "SYMPY_TIMEOUT")
        else:
            _add_reason(review_reasons, "SYMPY_FAILED")

    for lbl, p in p_map.items():
        # 1. SymPy / 选项数值互校
        part_sympy_ok: bool | None = None
        if p.verify_mode == "SYMBOLIC":
            if sympy_result is None:
                part_sympy_ok = None
            elif not sympy_result.ok:
                part_sympy_ok = False
                symbolic_checks_ok.append(False)
                sympy_parts_out.append({"label": lbl, "value": None, "ok": False})
            elif lbl not in sbx_values_norm:
                part_sympy_ok = False
                symbolic_checks_ok.append(False)
                sympy_parts_out.append({"label": lbl, "value": None, "ok": False})
                _add_reason(review_reasons, "SYMPY_FAILED")
            else:
                sbx_val = sbx_values_norm[lbl]
                ok_sbx = await _verify_part_against_sandbox(
                    p,
                    sbx_val,
                    allow_pi_314=allow_pi_314,
                    timeout_seconds=timeout_seconds,
                    memory_limit_mb=memory_limit_mb,
                )
                part_sympy_ok = ok_sbx
                symbolic_checks_ok.append(ok_sbx)
                sympy_parts_out.append({"label": lbl, "value": sbx_val, "ok": ok_sbx})
                if not ok_sbx:
                    _add_reason(review_reasons, "SYMPY_FAILED")
        elif p.answer_type == "CHOICE":
            choice_ok = await _check_choice_option_vs_sympy(
                p,
                options,
                allow_pi_314=allow_pi_314,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
            )
            if choice_ok is not None:
                part_sympy_ok = choice_ok
                symbolic_checks_ok.append(choice_ok)
                sympy_parts_out.append({"label": lbl, "value": p.sympy_expr, "ok": choice_ok})
                if not choice_ok:
                    _add_reason(review_reasons, "SYMPY_FAILED")

        # 2. 双盲判等
        dual_equal = True
        judge_note = ""
        if secondary_parts is not None:
            s = s_map.get(lbl)
            if s is None:
                dual_equal = False
                judge_note = "PART_COUNT_MISMATCH"
            elif p.verify_mode == "SYMBOLIC" and s.verify_mode == "SYMBOLIC":
                dual_equal = await _compare_symbolic_parts(
                    p,
                    s,
                    allow_pi_314=allow_pi_314,
                    timeout_seconds=timeout_seconds,
                    memory_limit_mb=memory_limit_mb,
                )
                if not dual_equal:
                    _add_reason(review_reasons, "ANSWER_MISMATCH")
            elif p.answer_type == "CHOICE" and s.answer_type == "CHOICE":
                pa = normalize_choice_label(p.choice_label or p.answer)
                sa = normalize_choice_label(s.choice_label or s.answer)
                dual_equal = bool(pa and pa == sa)
                if not dual_equal:
                    _add_reason(review_reasons, "ANSWER_MISMATCH")
            elif p.answer_type == "CHOICE" and s.verify_mode == "SYMBOLIC":
                dual_equal = await _compare_choice_and_numeric(
                    p,
                    s,
                    options,
                    allow_pi_314=allow_pi_314,
                    timeout_seconds=timeout_seconds,
                    memory_limit_mb=memory_limit_mb,
                )
                if not dual_equal:
                    _add_reason(review_reasons, "ANSWER_MISMATCH")
            elif s.answer_type == "CHOICE" and p.verify_mode == "SYMBOLIC":
                dual_equal = await _compare_choice_and_numeric(
                    s,
                    p,
                    options,
                    allow_pi_314=allow_pi_314,
                    timeout_seconds=timeout_seconds,
                    memory_limit_mb=memory_limit_mb,
                )
                if not dual_equal:
                    _add_reason(review_reasons, "ANSWER_MISMATCH")
            elif p.answer_type == "SYMBOL_FILL" and s.answer_type == "SYMBOL_FILL":
                if p.collection_mode == "SINGLE" and s.collection_mode == "SINGLE":
                    pa_sym = normalize_symbol_fill(p.canonical or p.answer)
                    sa_sym = normalize_symbol_fill(s.canonical or s.answer)
                    dual_equal = bool(pa_sym and pa_sym == sa_sym)
                elif p.collection_mode == s.collection_mode and p.items and s.items and len(p.items) == len(s.items):
                    dual_equal = all(
                        normalize_symbol_fill(_extract_item_info(ia)[1])
                        == normalize_symbol_fill(_extract_item_info(ib)[1])
                        for ia, ib in zip(p.items, s.items, strict=True)
                    )
                else:
                    dual_equal = False
                if not dual_equal:
                    _add_reason(review_reasons, "ANSWER_MISMATCH")
            elif p.verify_mode == "QUALITATIVE" and s.verify_mode == "QUALITATIVE":
                norm_a = _normalize_qualitative_text(p.answer)
                norm_b = _normalize_qualitative_text(s.answer)
                if norm_a and norm_a == norm_b:
                    dual_equal = True
                    judge_note = "EXACT_TEXT_MATCH"
                elif qualitative_judge is not None:
                    jres = await qualitative_judge(lbl, p.answer, s.answer)
                    judge_note = jres.reason
                    if jres.equivalent and jres.confidence in ("HIGH", "MEDIUM"):
                        dual_equal = True
                    else:
                        dual_equal = False
                        if jres.confidence == "LOW":
                            _add_reason(review_reasons, "QUALITATIVE_UNCERTAIN")
                        else:
                            _add_reason(review_reasons, "ANSWER_MISMATCH")
                else:
                    dual_equal = False
                    _add_reason(review_reasons, "QUALITATIVE_UNCERTAIN")
            else:
                dual_equal = False
                _add_reason(review_reasons, "ANSWER_MISMATCH")

        part_verdicts.append(
            PartVerdict(
                label=lbl,
                verify_mode=p.verify_mode,
                dual_equal=dual_equal,
                sympy_ok=part_sympy_ok,
                judge_note=judge_note,
            )
        )

    # 计算整体 sympy_status 与 all_sympy_ok
    sympy_status: Literal[
        "PASSED",
        "PARTIAL",
        "FAILED",
        "TIMEOUT",
        "AST_REJECTED",
        "SKIPPED_NO_SYMBOLIC",
        "NOT_RUN",
    ]
    if sympy_result is not None:
        if not has_symbolic_part and not symbolic_checks_ok:
            sympy_status = "SKIPPED_NO_SYMBOLIC"
            all_sympy_ok = True
        elif sympy_result.status == "TIMEOUT":
            sympy_status = "TIMEOUT"
            all_sympy_ok = False
        elif sympy_result.status == "AST_REJECTED":
            sympy_status = "AST_REJECTED"
            all_sympy_ok = False
        elif not sympy_result.ok:
            sympy_status = "FAILED"
            all_sympy_ok = False
        elif all(symbolic_checks_ok):
            sympy_status = "PASSED"
            all_sympy_ok = True
        elif any(symbolic_checks_ok):
            sympy_status = "PARTIAL"
            all_sympy_ok = False
        else:
            sympy_status = "FAILED"
            all_sympy_ok = False
    else:
        if symbolic_checks_ok and not all(symbolic_checks_ok):
            sympy_status = "FAILED"
            all_sympy_ok = False
        elif not has_symbolic_part:
            sympy_status = "SKIPPED_NO_SYMBOLIC"
            all_sympy_ok = True
        else:
            sympy_status = "NOT_RUN"
            all_sympy_ok = False

    sympy_output: dict[str, Any] = {
        "parts": sympy_parts_out,
        "stderr_tail": sympy_result.stderr_tail if sympy_result is not None else "",
    }
    all_dual_equal = (not part_count_mismatch) and all(v.dual_equal for v in part_verdicts)

    return EquivalenceSummary(
        all_dual_equal=all_dual_equal,
        all_sympy_ok=all_sympy_ok,
        part_count_mismatch=part_count_mismatch,
        review_reasons=review_reasons,
        part_verdicts=part_verdicts,
        sympy_status=sympy_status,
        sympy_output=sympy_output,
    )
