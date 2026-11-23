"""统一防剧透裁剪引擎（DESIGN.md §5.4-1 & §5.4-2）。

提供四档确定性裁剪：
- quiz（默认）：仅题面、配图、粗粒度 L1、公开选拔理由；默认隐藏 visual_facts，严禁出现答案/解法/L2/L3/标签；
- hint：透出 visual_facts 与最优解前 k 步防剧透 hint（绝不含 sub_goal 与 core_formula）；
- full：透出完整答案、多解法分支、步法骨架与全部细粒度标签；
- check：仅允许逐小问 verdict 与数量统计，物理剥离答案、解法与 judge_llm.reason。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

RevealMode = Literal["quiz", "hint", "full"]

_SAFE_QUIZ_FIELDS: tuple[str, ...] = (
    "question_uid",
    "source_id",
    "grade_subject",
    "format",
    "question_text",
    "options",
    "has_images",
    "cas_images",
    "image_paths",
    "image_captions",
    "base_elo",
    "tier",
    "tier_rank",
    "primary_l1_module",
    "verification_status",
    "is_servable",
    "reason_public",
)

_FULL_EXTRA_FIELDS: tuple[str, ...] = (
    "visual_facts",
    "abstract_skeleton",
    "secondary_l1_modules",
    "final_answers",
    "solutions",
    "difficulty_evidence",
    "c_score",
    "s_score",
    "t_score",
    "l_score",
    "l2_entities",
    "l3_nodes",
    "thinking_methods",
    "classic_motifs",
    "trap_tags",
    "pedagogical_note",
    "reason_full",
)

_CHECK_ALLOWLIST: tuple[str, ...] = (
    "question_uid",
    "source_id",
    "all_correct",
    "parts_correct",
    "parts_total",
    "missing_labels",
    "extra_labels",
    "part_results",
    "logged",
    "log_id",
    "outcome",
    "score_ratio",
)


def resolve_cas_image_paths(cas_images: list[str], assets_dir: Path | str = Path("data/assets/images")) -> list[str]:
    """将 CAS 图片文件名解析为绝对本地路径数组。"""
    base = Path(assets_dir).resolve()
    return [str((base / img).resolve()) for img in cas_images if img]


def _extract_hints_up_to(question: dict[str, Any], hint_level: int) -> list[dict[str, Any]]:
    """从最优解步骤中提取前 k 步纯 hint，严格剔除 sub_goal 与 core_formula。"""
    k = max(1, int(hint_level))
    raw_hints = question.get("hints")
    if isinstance(raw_hints, list) and raw_hints:
        cleaned = [
            {"step_index": int(h["step_index"]), "hint": str(h["hint"])}
            for h in sorted(raw_hints, key=lambda x: int(x.get("step_index", 0)))
            if isinstance(h, dict) and h.get("hint") is not None
        ]
        return cleaned[:k]

    solutions = question.get("solutions")
    if not isinstance(solutions, list) or not solutions:
        return []
    optimal = next((s for s in solutions if isinstance(s, dict) and s.get("is_optimal")), solutions[0])
    steps = optimal.get("steps") if isinstance(optimal, dict) else None
    if not isinstance(steps, list):
        return []
    ordered = sorted(steps, key=lambda s: int(s.get("step_index", 0)))
    return [
        {"step_index": int(st.get("step_index", idx)), "hint": str(st.get("hint") or "")}
        for idx, st in enumerate(ordered[:k], start=1)
    ]


def _count_total_hints(question: dict[str, Any]) -> int:
    if isinstance(question.get("total_hints"), int):
        return int(question["total_hints"])
    raw_hints = question.get("hints")
    if isinstance(raw_hints, list) and raw_hints:
        return len(raw_hints)
    solutions = question.get("solutions")
    if isinstance(solutions, list) and solutions:
        optimal = next((s for s in solutions if isinstance(s, dict) and s.get("is_optimal")), solutions[0])
        steps = optimal.get("steps") if isinstance(optimal, dict) else None
        if isinstance(steps, list):
            return len(steps)
    return 0


def apply_reveal(
    question: dict[str, Any],
    *,
    reveal: RevealMode = "quiz",
    hint_level: int = 1,
    include_visual_facts: bool = False,
) -> dict[str, Any]:
    """按 reveal 档位对题目字典执行确定性防剧透裁剪。"""
    if reveal not in ("quiz", "hint", "full"):
        raise ValueError(f"不支持的 reveal 档位: {reveal!r}")

    out: dict[str, Any] = {}
    for key in _SAFE_QUIZ_FIELDS:
        if key in question:
            out[key] = question[key]

    if reveal == "quiz":
        if include_visual_facts and "visual_facts" in question:
            out["visual_facts"] = question["visual_facts"]
        return out

    if reveal == "hint":
        if "visual_facts" in question:
            out["visual_facts"] = question["visual_facts"]
        out["hints"] = _extract_hints_up_to(question, hint_level)
        out["hint_level_shown"] = len(out["hints"])
        out["total_hints"] = _count_total_hints(question)
        return out

    # reveal == "full"
    for key in _FULL_EXTRA_FIELDS:
        if key in question:
            out[key] = question[key]
    out["hints"] = _extract_hints_up_to(question, max(100, _count_total_hints(question)))
    out["total_hints"] = _count_total_hints(question)
    return out


def apply_related_reveal(
    edge_item: dict[str, Any],
    *,
    reveal: RevealMode = "quiz",
    hint_level: int = 1,
    include_visual_facts: bool = False,
) -> dict[str, Any]:
    """对 related 返回的拓扑关联项同步执行嵌套题面裁剪与教学说明隔离。"""
    out: dict[str, Any] = {
        k: v
        for k, v in edge_item.items()
        if k not in ("question", "pedagogical_note")
    }
    if reveal == "full" and "pedagogical_note" in edge_item:
        out["pedagogical_note"] = edge_item["pedagogical_note"]
    if isinstance(edge_item.get("question"), dict):
        out["question"] = apply_reveal(
            edge_item["question"],
            reveal=reveal,
            hint_level=hint_level,
            include_visual_facts=include_visual_facts,
        )
    return out


def apply_check_reveal(check_result: dict[str, Any]) -> dict[str, Any]:
    """shuati check 强制裁剪档：只许输出 verdict 统计，绝不回传答案或定性 reason。"""
    out: dict[str, Any] = {}
    for key in _CHECK_ALLOWLIST:
        if key not in check_result:
            continue
        if key == "part_results" and isinstance(check_result[key], list):
            out["part_results"] = [
                {
                    "label": str(item.get("label", "")),
                    "verdict": str(item.get("verdict", "uncertain")),
                }
                for item in check_result[key]
                if isinstance(item, dict)
            ]
        else:
            out[key] = check_result[key]
    return out
