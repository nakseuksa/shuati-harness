"""物理零 DB 依赖的无状态草稿自检核与 SymPy 验算内核（Step 2a，契约 §1.4）。

铁律：
- 绝对禁止 import sqlite3 或读写 data/shuati.db / task_map.json；
- 仅只读 config/method_blacklist.yaml（及可选 config/settings.yaml 中的
  allow_pi_314_subjects / sympy_timeout_seconds / sympy_memory_limit_mb）。
"""

from __future__ import annotations

import asyncio
import copy
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ValidationError

from shuati.core.difficulty import compute_difficulty
from shuati.models.domain import AnswerPart
from shuati.models.schemas import (
    ExternalDiagnosisOutput,
    Stage2OnlyOutput,
    UnifiedS1S2Output,
)
from shuati.verify.answer_parts import derive_verification_mode, normalize_answer_parts
from shuati.verify.equivalence import EquivalenceSummary, compare_answer_parts
from shuati.verify.sympy_sandbox import SympySandboxResult, run_verify_script

_EXEMPT_ANGLE_NUMS = frozenset({"45", "60", "90", "180", "360"})
_SPOILER_PHRASE_RE = re.compile(
    r"(?:答案|最终结果|解得结果|所以等于|故选|选)\s*[:：=是为]?\s*(?:\$?\d|[A-D]\b)"
)
_DIGIT_RE = re.compile(r"\d")
_MULTI_DIGIT_RE = re.compile(r"(?<![A-Za-z_])\d{2,}(?![A-Za-z_])")

_DEFAULT_SEED_TRAPS: frozenset[str] = frozenset(
    {
        "TRAP_DOMAIN_BOUNDARY",
        "TRAP_SIGN_DIRECTION",
        "TRAP_EXTRANEOUS_ROOT",
        "TRAP_FRAME_SHIFT",
        "TRAP_APPROX_CONDITION",
        "TRAP_ARITHMETIC_SLIP",
        "TRAP_AVERAGE_HARMONIC",
        "TRAP_EXTENDED_BODY_LENGTH",
        "TRAP_ASYNC_SYNC_CONFUSION",
        "TRAP_DIRECTION_REF_POINT",
    }
)

_MODE_TO_SCHEMA: dict[str, type[BaseModel]] = {
    "UNIFIED_S1_S2": UnifiedS1S2Output,
    "STAGE2_ONLY": Stage2OnlyOutput,
    "DIAGNOSE": ExternalDiagnosisOutput,
}


def _run_coro_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _load_pipeline_verify_settings(config_dir: Path) -> tuple[list[str], float, int]:
    """仅读取 settings.yaml 的 SymPy 验算相关只读配置（不触及 DB 或修改环境变量）。"""
    allow_pi_314_subjects: list[str] = ["小学奥数"]
    timeout_seconds = 5.0
    memory_limit_mb = 512

    settings_path = config_dir / "settings.yaml"
    if settings_path.exists():
        try:
            raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
            pipe = raw.get("pipeline") if isinstance(raw, dict) else None
            if isinstance(pipe, dict):
                raw_subjects = pipe.get("allow_pi_314_subjects")
                if isinstance(raw_subjects, list):
                    allow_pi_314_subjects = [str(s) for s in raw_subjects if str(s).strip()]
                raw_timeout = pipe.get("sympy_timeout_seconds")
                if raw_timeout is not None:
                    timeout_seconds = float(raw_timeout)
                raw_mem = pipe.get("sympy_memory_limit_mb")
                if raw_mem is not None:
                    memory_limit_mb = int(raw_mem)
        except Exception:
            pass

    return allow_pi_314_subjects, timeout_seconds, memory_limit_mb


def _load_blacklist_and_traps(config_dir: Path) -> tuple[set[str], set[str]]:
    cfg_path = config_dir / "method_blacklist.yaml"
    if not cfg_path.exists():
        return set(), set(_DEFAULT_SEED_TRAPS)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    blacklist = {str(t).strip() for t in data.get("blacklist_terms", []) if str(t).strip()}
    traps = {str(item["code"]).strip() for item in data.get("seed_traps", []) if item.get("code")}
    if not traps:
        traps = set(_DEFAULT_SEED_TRAPS)
    return blacklist, traps


def _inline_refs(node: Any, defs: dict[str, Any], active_refs: frozenset[str]) -> Any:
    if isinstance(node, list):
        return [_inline_refs(item, defs, active_refs) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node and isinstance(node["$ref"], str):
        ref = node["$ref"]
        if ref.startswith("#/$defs/"):
            def_name = ref.split("#/$defs/", 1)[1]
            if def_name in defs and def_name not in active_refs:
                resolved = copy.deepcopy(defs[def_name])
                merged = _inline_refs(resolved, defs, active_refs | {def_name})
                if isinstance(merged, dict):
                    for k, v in node.items():
                        if k != "$ref":
                            merged[k] = _inline_refs(v, defs, active_refs)
                return merged

    out: dict[str, Any] = {}
    for k, v in node.items():
        if k == "$defs":
            continue
        out[k] = _inline_refs(v, defs, active_refs)
    return out


def _tighten_difficulty_bounds(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _tighten_difficulty_bounds(item)
        return
    if not isinstance(node, dict):
        return

    props = node.get("properties")
    if isinstance(props, dict):
        for dim_name, upper in (
            ("C_concept_count", 4),
            ("S_chain_length", 3),
            ("T_trickiness", 3),
            ("L_computation_load", 3),
        ):
            dim_schema = props.get(dim_name)
            if isinstance(dim_schema, dict):
                dim_props = dim_schema.get("properties")
                if isinstance(dim_props, dict) and isinstance(dim_props.get("score"), dict):
                    dim_props["score"]["minimum"] = 1
                    dim_props["score"]["maximum"] = upper

    for val in node.values():
        _tighten_difficulty_bounds(val)


def flatten_strict_schema(schema_cls: type[BaseModel]) -> dict[str, Any]:
    """展平 Pydantic JSON Schema 的 $defs/$ref 并收紧 C∈[1,4]、S/T/L∈[1,3]。"""
    raw_schema = schema_cls.model_json_schema()
    defs = raw_schema.get("$defs")
    defs_map = defs if isinstance(defs, dict) else {}
    flattened = _inline_refs(raw_schema, defs_map, frozenset())
    if isinstance(flattened, dict):
        flattened.pop("$defs", None)
        _tighten_difficulty_bounds(flattened)
        return flattened
    return {}


def _extract_spoiler_tokens(parts: list[AnswerPart], question_text: str) -> list[str]:
    tokens: list[str] = []
    for p in parts:
        for cand in (p.answer, p.sympy_expr, p.canonical):
            if not cand:
                continue
            s = cand.strip()
            if not s or s.upper() in {"A", "B", "C", "D", "E"}:
                continue
            is_fraction_or_ratio = "/" in s or ":" in s
            if (len(s) >= 2 and s not in question_text) or len(s) >= 3 or is_fraction_or_ratio:
                tokens.append(s)
    return list(dict.fromkeys(tokens))


def _build_sympy_fix_hints(
    sympy_result: SympySandboxResult | None,
    equiv: EquivalenceSummary,
    norm_parts: list[AnswerPart],
) -> list[str]:
    hints: list[str] = []
    if sympy_result is not None:
        if sympy_result.status == "AST_REJECTED":
            hints.append(
                "仅允许导入 sympy / math / fractions；严禁调用 sympify / parse_expr / lambdify / S(<字符串>) / eval / exec / open。"
            )
            hints.append(
                "必须定义 def verify(): 并通过真实符号或算术计算得出结果，严禁硬编码常量返回。"
            )
        elif sympy_result.status == "TIMEOUT":
            hints.append("SymPy 脚本执行超时，请化简方程或减少枚举规模后重试。")
        elif not sympy_result.ok:
            hints.append("请检查 sympy_code 语法、变量定义与 def verify(): 返回值类型（dict[str, str | list[str]]）。")

    expected_labels = [p.label for p in norm_parts]
    if sympy_result is not None and sympy_result.ok:
        for pv in equiv.part_verdicts:
            if pv.sympy_ok is False:
                hints.append(
                    f"小问 '{pv.label}' SymPy 核对失败：请确认 def verify() 返回键名与归一标签 {expected_labels} 一致，"
                    "且 answer / sympy_expr 与 verify() 计算值数学等价（未要求 π≈3.14 时请保留精确符号 pi）。"
                )
    return list(dict.fromkeys(hints))


def verify_sympy_payload(
    *,
    sympy_code: str | None,
    final_answers: list[dict[str, Any]],
    grade_subject: str = "小学奥数",
    options: dict[str, str] | None = None,
    config_dir: Path | str = Path("config"),
) -> dict[str, Any]:
    """无状态执行 AnswerPart 归一 + AST/沙盒 SymPy 验算 + 逐小问数学等价核对。"""
    cfg_dir = Path(config_dir)
    allow_pi_314_subjects, timeout_seconds, memory_limit_mb = _load_pipeline_verify_settings(cfg_dir)

    raw_parts: list[AnswerPart] = []
    for idx, item in enumerate(final_answers):
        try:
            raw_parts.append(item if isinstance(item, AnswerPart) else AnswerPart.model_validate(item))
        except ValidationError as exc:
            return {
                "ok": False,
                "sympy_status": "FAILED",
                "computed_values": None,
                "part_verdicts": [],
                "errors": [
                    {
                        "code": "ANSWER_PART_SCHEMA_ERROR",
                        "field": f"final_answers[{idx}]",
                        "message": str(exc),
                    }
                ],
                "fix_hints": ["请检查 final_answers 元素是否符合 AnswerPart 契约（answer_type × verify_mode 匹配）。"],
                "stderr_tail": "",
            }

    if not raw_parts:
        return {
            "ok": False,
            "sympy_status": "FAILED",
            "computed_values": None,
            "part_verdicts": [],
            "errors": [{"code": "EMPTY_FINAL_ANSWERS", "field": "final_answers", "message": "final_answers 不能为空"}],
            "fix_hints": ["请至少提供一个合法的 AnswerPart。"],
            "stderr_tail": "",
        }

    try:
        norm_parts = normalize_answer_parts(raw_parts)
    except ValueError as exc:
        return {
            "ok": False,
            "sympy_status": "FAILED",
            "computed_values": None,
            "part_verdicts": [],
            "errors": [
                {
                    "code": "ANSWER_PART_NORMALIZE_ERROR",
                    "field": "final_answers",
                    "message": str(exc),
                }
            ],
            "fix_hints": ["小问 label 仅允许 '主问' 或 '(1)', '(2)'...，且不得重复或自造 '(1)-1' 子标签。"],
            "stderr_tail": "",
        }

    has_symbolic = any(p.verify_mode == "SYMBOLIC" for p in norm_parts)
    sympy_result: SympySandboxResult | None = None
    if has_symbolic:
        if not isinstance(sympy_code, str) or not sympy_code.strip():
            sympy_result = SympySandboxResult(
                ok=False,
                status="FAILED",
                values=None,
                stderr_tail="Missing sympy_code for SYMBOLIC AnswerPart",
            )
        else:
            sympy_result = run_verify_script(
                sympy_code,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
            )

    equiv: EquivalenceSummary = _run_coro_sync(
        compare_answer_parts(
            norm_parts,
            None,
            grade_subject=grade_subject,
            allow_pi_314_subjects=allow_pi_314_subjects,
            options=options,
            sympy_result=sympy_result,
            timeout_seconds=timeout_seconds,
            memory_limit_mb=memory_limit_mb,
        )
    )

    errors: list[dict[str, Any]] = []
    if not equiv.all_sympy_ok:
        err_code = "SYMPY_FAILED"
        if equiv.sympy_status == "AST_REJECTED":
            err_code = "SYMPY_AST_REJECTED"
        elif equiv.sympy_status == "TIMEOUT":
            err_code = "SYMPY_TIMEOUT"
        elif equiv.sympy_status == "PARTIAL":
            err_code = "SYMPY_PARTIAL"
        detail = sympy_result.stderr_tail if sympy_result is not None else ""
        if not detail:
            failed_labels = [pv.label for pv in equiv.part_verdicts if pv.sympy_ok is False]
            detail = f"小问核对不一致: {failed_labels}" if failed_labels else f"sympy_status={equiv.sympy_status}"
        errors.append(
            {
                "code": err_code,
                "field": "sympy_code",
                "message": detail,
            }
        )

    fix_hints = _build_sympy_fix_hints(sympy_result, equiv, norm_parts) if errors else []

    return {
        "ok": bool(equiv.all_sympy_ok),
        "sympy_status": equiv.sympy_status,
        "computed_values": sympy_result.values if sympy_result is not None else None,
        "part_verdicts": [asdict(pv) for pv in equiv.part_verdicts],
        "errors": errors,
        "fix_hints": fix_hints,
        "stderr_tail": sympy_result.stderr_tail if sympy_result is not None else "",
    }


def _validate_stage2_deterministic(
    out: Stage2OnlyOutput | UnifiedS1S2Output | ExternalDiagnosisOutput,
    *,
    question_text: str,
    answer_parts: list[AnswerPart],
    blacklist_terms: set[str],
    seed_traps: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    diff = out.difficulty
    for name, dim, upper in (
        ("C_concept_count", diff.C_concept_count, 4),
        ("S_chain_length", diff.S_chain_length, 3),
        ("T_trickiness", diff.T_trickiness, 3),
        ("L_computation_load", diff.L_computation_load, 3),
    ):
        if not 1 <= dim.score <= upper:
            errors.append(
                {
                    "code": "DIFFICULTY_RANGE_ERROR",
                    "field": f"difficulty.{name}.score",
                    "message": f"{name}.score={dim.score} 超出 [1, {upper}]",
                }
            )

    step_indices = [s.step_index for s in out.steps]
    if step_indices != list(range(1, len(out.steps) + 1)):
        errors.append(
            {
                "code": "STEP_INDEX_NON_CONTIGUOUS",
                "field": "steps",
                "message": f"step_index 必须从 1 起连续递增，收到 {step_indices}",
            }
        )

    spoiler_tokens = _extract_spoiler_tokens(answer_parts, question_text)
    for idx, s in enumerate(out.steps):
        if _DIGIT_RE.search(s.raw_l3):
            errors.append(
                {
                    "code": "RAW_L3_CONTAINS_DIGIT",
                    "field": f"steps[{idx}].raw_l3",
                    "message": f"step {s.step_index} raw_l3='{s.raw_l3}' 不得包含数字",
                }
            )
        l3_len = len(s.raw_l3.strip())
        if l3_len < 4 or l3_len > 14:
            warnings.append(
                f"steps[{idx}].raw_l3='{s.raw_l3}' 长度为 {l3_len} 字（建议 4~14 字脱壳原子转化算子）"
            )

        for field_key, field_val in (
            ("raw_l1", s.raw_l1),
            ("raw_l2", s.raw_l2),
            ("raw_l3", s.raw_l3),
        ):
            if field_val.strip() in blacklist_terms:
                errors.append(
                    {
                        "code": "BLACKLIST_TERM_HIT",
                        "field": f"steps[{idx}].{field_key}",
                        "message": f"step {s.step_index} {field_key} 命中黑名单泛词 '{field_val}'",
                    }
                )

        if _SPOILER_PHRASE_RE.search(s.hint):
            errors.append(
                {
                    "code": "HINT_SPOILER_PHRASE",
                    "field": f"steps[{idx}].hint",
                    "message": f"step {s.step_index} hint='{s.hint}' 疑似直接剧透答案",
                }
            )
        for tok in spoiler_tokens:
            if re.search(rf"(?<![\d.]){re.escape(tok)}(?![\d.])", s.hint):
                errors.append(
                    {
                        "code": "HINT_LEAKS_ANSWER",
                        "field": f"steps[{idx}].hint",
                        "message": f"step {s.step_index} hint 泄露终值 '{tok}'",
                    }
                )

    if out.primary_l1_module.strip() in blacklist_terms:
        errors.append(
            {
                "code": "BLACKLIST_TERM_HIT",
                "field": "primary_l1_module",
                "message": f"primary_l1_module 命中黑名单泛词 '{out.primary_l1_module}'",
            }
        )
    for idx, mod in enumerate(out.secondary_l1_modules):
        if mod.strip() in blacklist_terms:
            errors.append(
                {
                    "code": "BLACKLIST_TERM_HIT",
                    "field": f"secondary_l1_modules[{idx}]",
                    "message": f"secondary_l1_modules 包含黑名单泛词 '{mod}'",
                }
            )
    for idx, m in enumerate(out.thinking_methods):
        if m.strip() in blacklist_terms:
            errors.append(
                {
                    "code": "BLACKLIST_TERM_HIT",
                    "field": f"thinking_methods[{idx}]",
                    "message": f"thinking_methods 包含黑名单泛词 '{m}'",
                }
            )
    for idx, m in enumerate(out.classic_motifs):
        if m.strip() in blacklist_terms:
            errors.append(
                {
                    "code": "BLACKLIST_TERM_HIT",
                    "field": f"classic_motifs[{idx}]",
                    "message": f"classic_motifs 包含黑名单泛词 '{m}'",
                }
            )

    for idx, trap in enumerate(out.trap_tags):
        if trap not in seed_traps:
            errors.append(
                {
                    "code": "INVALID_TRAP_TAG",
                    "field": f"trap_tags[{idx}]",
                    "message": f"'{trap}' 不在允许的 seed_traps 枚举 {sorted(seed_traps)} 中",
                }
            )

    q_specific_nums = {
        m.group(0)
        for m in _MULTI_DIGIT_RE.finditer(question_text)
        if m.group(0) not in _EXEMPT_ANGLE_NUMS
    }
    for num_tok in sorted(q_specific_nums):
        if re.search(rf"(?<!\d){re.escape(num_tok)}(?!\d)", out.abstract_skeleton):
            errors.append(
                {
                    "code": "SKELETON_LEAKS_NUMBER",
                    "field": "abstract_skeleton",
                    "message": f"abstract_skeleton 夹带题干具体数值 '{num_tok}'",
                }
            )

    return errors, warnings


def validate_draft_payload(
    *,
    mode: Literal["UNIFIED_S1_S2", "STAGE2_ONLY", "DIAGNOSE"],
    candidate: dict[str, Any],
    question_text: str,
    grade_subject: str = "小学奥数",
    options: dict[str, str] | None = None,
    verified_final_answers: list[dict[str, Any]] | None = None,
    run_sympy: bool = True,
    config_dir: Path | str = Path("config"),
) -> dict[str, Any]:
    """零 DB 依赖的草稿全量校验（Schema + AnswerPart + Stage 2 规则 + 可选 SymPy 验算）。"""
    schema_cls = _MODE_TO_SCHEMA.get(mode)
    if schema_cls is None:
        return {
            "ok": False,
            "mode": mode,
            "errors": [{"code": "INVALID_MODE", "field": "mode", "message": f"不支持的校验模式: {mode!r}"}],
            "warnings": [],
            "sympy_check": None,
            "normalized_preview": None,
        }

    cfg_dir = Path(config_dir)
    blacklist_terms, seed_traps = _load_blacklist_and_traps(cfg_dir)

    try:
        validated = schema_cls.model_validate(candidate)
    except ValidationError as exc:
        schema_errors: list[dict[str, Any]] = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            schema_errors.append(
                {
                    "code": "SCHEMA_ERROR",
                    "field": loc or "root",
                    "message": str(err.get("msg", "")),
                }
            )
        return {
            "ok": False,
            "mode": mode,
            "errors": schema_errors,
            "warnings": [],
            "sympy_check": None,
            "normalized_preview": None,
        }

    assert isinstance(validated, (UnifiedS1S2Output, Stage2OnlyOutput, ExternalDiagnosisOutput))

    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    norm_parts: list[AnswerPart] = []

    if isinstance(validated, (UnifiedS1S2Output, ExternalDiagnosisOutput)):
        try:
            norm_parts = normalize_answer_parts(validated.final_answers)
        except ValueError as exc:
            errors.append(
                {
                    "code": "ANSWER_PART_NORMALIZE_ERROR",
                    "field": "final_answers",
                    "message": str(exc),
                }
            )
    else:
        if verified_final_answers:
            try:
                raw_verified = [
                    x if isinstance(x, AnswerPart) else AnswerPart.model_validate(x)
                    for x in verified_final_answers
                ]
                norm_parts = normalize_answer_parts(raw_verified)
            except (ValidationError, ValueError) as exc:
                errors.append(
                    {
                        "code": "VERIFIED_ANSWERS_INVALID",
                        "field": "verified_final_answers",
                        "message": str(exc),
                    }
                )

    s2_errors, s2_warnings = _validate_stage2_deterministic(
        validated,
        question_text=question_text,
        answer_parts=norm_parts,
        blacklist_terms=blacklist_terms,
        seed_traps=seed_traps,
    )
    errors.extend(s2_errors)
    warnings.extend(s2_warnings)

    sympy_check: dict[str, Any] | None = None
    if isinstance(validated, (UnifiedS1S2Output, ExternalDiagnosisOutput)):
        has_symbolic = any(p.verify_mode == "SYMBOLIC" for p in norm_parts)
        if run_sympy and norm_parts:
            sympy_check = verify_sympy_payload(
                sympy_code=validated.sympy_code,
                final_answers=[p.model_dump() for p in norm_parts],
                grade_subject=grade_subject,
                options=options,
                config_dir=cfg_dir,
            )
            if not sympy_check["ok"]:
                errors.extend(sympy_check["errors"])
        elif not run_sympy and has_symbolic and (not validated.sympy_code or not validated.sympy_code.strip()):
            warnings.append("存在 verify_mode=='SYMBOLIC' 小问但 sympy_code 为空（当前 run_sympy=False 未执行沙盒验算）")

    diff = validated.difficulty
    diff_res = compute_difficulty(
        diff.C_concept_count.score,
        diff.S_chain_length.score,
        diff.T_trickiness.score,
        diff.L_computation_load.score,
    )

    normalized_preview: dict[str, Any] = {
        "grade_subject": grade_subject,
        "base_elo": diff_res.base_elo,
        "tier": diff_res.tier,
        "tier_rank": diff_res.tier_rank,
        "primary_l1_module": validated.primary_l1_module.strip(),
        "secondary_l1_modules": [x.strip() for x in validated.secondary_l1_modules if x.strip()],
        "steps_count": len(validated.steps),
        "trap_tags": [t.strip() for t in validated.trap_tags if t.strip()],
    }
    if norm_parts:
        normalized_preview["final_answers"] = [p.model_dump() for p in norm_parts]
        normalized_preview["verification_mode"] = derive_verification_mode(norm_parts)

    return {
        "ok": len(errors) == 0,
        "mode": mode,
        "errors": errors,
        "warnings": warnings,
        "sympy_check": sympy_check,
        "normalized_preview": normalized_preview,
    }
