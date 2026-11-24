"""三层提示词接缝引擎：default.yaml → subjects/<slug>.yaml → profiles/<name>.yaml。

职责边界：
- 管理可演进的领域措辞、示例与分段引导（blocks / stages / prompt_versions）；
- 严禁覆盖或削弱 prompt_builder.py 中锁死的硬契约（防泄漏白名单、AnswerPart、SymPy AST、
  visual_facts 首位、evidence 先于 score、D-Audit-4 人工最高权威与 D-Audit-5 方向）；
- 严格校验外链片段（file:/append_file:）不得越界出 config/prompts/ 目录。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REQUIRED_BLOCKS: tuple[str, ...] = (
    "persona",
    "visual_extraction",
    "solving_conventions",
    "steps_and_socratic_hint",
    "abstraction_and_tagging",
    "difficulty_rubric_notes",
)

REQUIRED_STAGES: tuple[str, ...] = (
    "stage1_judge",
    "stage3_propose",
    "stage3c_mapping",
    "stage3d_cluster_assign",
    "stage3e_prerequisite",
    "stage4_refine",
)

REQUIRED_PROMPT_VERSIONS: tuple[str, ...] = (
    "stage1",
    "stage2",
    "unified",
    "stage3a",
    "stage3c",
    "stage3d",
    "stage3e",
    "stage4",
    "diagnose",
)

KNOWN_SUBJECT_SLUGS: dict[str, str] = {
    "小学奥数": "primary_olympiad",
    "初中数学": "junior_math",
    "高中数学": "senior_math",
    "初中物理": "junior_physics",
    "高中物理": "senior_physics",
    "初中化学": "junior_chemistry",
    "高中化学": "senior_chemistry",
}

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class ResolvedSeamBundle:
    """三层合并后的不可变提示词接缝包与版本/摘要追踪。"""

    grade_subject: str
    slug: str
    profile: str | None
    chain: list[str]
    prompt_versions: dict[str, str]
    blocks: dict[str, str]
    stages: dict[str, str]
    sources: dict[str, list[str]] = field(default_factory=dict)
    prompt_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade_subject": self.grade_subject,
            "slug": self.slug,
            "profile": self.profile,
            "chain": list(self.chain),
            "prompt_versions": dict(self.prompt_versions),
            "blocks": dict(self.blocks),
            "stages": dict(self.stages),
            "sources": {k: list(v) for k, v in self.sources.items()},
            "prompt_digest": self.prompt_digest,
        }


def _prompts_root(config_dir: Path | str) -> Path:
    return (Path(config_dir) / "prompts").resolve()


def _validate_segment(name: str, kind: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or ".." in cleaned or "/" in cleaned or "\\" in cleaned or not _SAFE_SEGMENT_RE.match(cleaned):
        raise ValueError(f"非法的 {kind} 名称: {name!r}")
    return cleaned


def _resolve_child_yaml(parent_dir: Path, filename: str, kind: str) -> Path:
    parent_resolved = parent_dir.resolve()
    target = (parent_resolved / filename).resolve()
    if not target.is_relative_to(parent_resolved):
        raise ValueError(f"安全拦截：{kind} 路径越界: {filename!r}")
    return target


def _resolve_snippet_path(prompts_root: Path, rel_path: str) -> Path:
    raw = (rel_path or "").strip()
    if not raw:
        raise ValueError("外链片段路径不能为空")
    p = Path(raw)
    if p.is_absolute():
        raise ValueError(f"安全拦截：外链片段禁止使用绝对路径: {raw!r}")
    candidate = (prompts_root / p).resolve()
    if not candidate.is_relative_to(prompts_root):
        raise ValueError(f"安全拦截：外链片段路径越界出 prompts 目录: {raw!r}")
    if not candidate.is_file():
        raise FileNotFoundError(f"引用的 Prompt 片段文件不存在: {raw!r}")
    return candidate


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    raw_text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw_text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Prompt 配置文件根节点必须为映射 (dict): {path.as_posix()}")
    return loaded


def _apply_slot_directive(
    *,
    previous: str,
    directive: Any,
    prompts_root: Path,
    slot_label: str,
    referenced_snippets: set[str] | None = None,
) -> str:
    if isinstance(directive, str):
        return directive.strip()
    if not isinstance(directive, dict):
        raise ValueError(
            f"槽位 {slot_label} 语法非法（仅支持 str 或 replace/append/file/append_file 字典），实际类型: {type(directive).__name__}"
        )

    allowed_keys = {"replace", "append", "file", "append_file"}
    unknown = set(directive.keys()) - allowed_keys
    if unknown:
        raise ValueError(f"槽位 {slot_label} 存在未知指令键: {sorted(unknown)}")
    if len(directive) != 1:
        raise ValueError(f"槽位 {slot_label} 指令字典必须恰好包含 1 个操作键，实际: {sorted(directive.keys())}")

    if "replace" in directive:
        val = directive["replace"]
        if not isinstance(val, str):
            raise ValueError(f"槽位 {slot_label}.replace 必须为字符串")
        return val.strip()

    if "append" in directive:
        val = directive["append"]
        if not isinstance(val, str):
            raise ValueError(f"槽位 {slot_label}.append 必须为字符串")
        addition = val.strip()
        if not addition:
            return previous
        return f"{previous}\n\n{addition}" if previous else addition

    if "file" in directive:
        rel = directive["file"]
        if not isinstance(rel, str):
            raise ValueError(f"槽位 {slot_label}.file 必须为路径字符串")
        snippet = _resolve_snippet_path(prompts_root, rel)
        if referenced_snippets is not None:
            referenced_snippets.add(snippet.relative_to(prompts_root).as_posix())
        return snippet.read_text(encoding="utf-8").strip()

    # append_file
    rel = directive["append_file"]
    if not isinstance(rel, str):
        raise ValueError(f"槽位 {slot_label}.append_file 必须为路径字符串")
    snippet = _resolve_snippet_path(prompts_root, rel)
    if referenced_snippets is not None:
        referenced_snippets.add(snippet.relative_to(prompts_root).as_posix())
    addition = snippet.read_text(encoding="utf-8").strip()
    if not addition:
        return previous
    return f"{previous}\n\n{addition}" if previous else addition


def _compute_prompt_digest(blocks: dict[str, str], stages: dict[str, str]) -> str:
    canonical = json.dumps(
        {"blocks": blocks, "stages": stages},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def resolve_slug_for_subject(grade_subject: str, config_dir: Path = Path("config")) -> str:
    """将学段学科名称解析为 subjects/<slug>.yaml 的 slug。"""
    gs = (grade_subject or "").strip()
    if not gs:
        return "default"

    prompts_root = _prompts_root(config_dir)
    subjects_dir = prompts_root / "subjects"
    if subjects_dir.is_dir():
        for yaml_path in sorted(subjects_dir.glob("*.yaml")):
            try:
                data = _read_yaml_mapping(yaml_path)
            except Exception:
                continue
            declared_gs = str(data.get("grade_subject") or "").strip()
            if declared_gs and declared_gs == gs:
                declared_slug = str(data.get("slug") or yaml_path.stem).strip()
                return _validate_segment(declared_slug, "subject slug")
        direct_candidate = subjects_dir / f"{gs}.yaml"
        if _SAFE_SEGMENT_RE.match(gs) and direct_candidate.is_file():
            return gs

    if gs in KNOWN_SUBJECT_SLUGS:
        return KNOWN_SUBJECT_SLUGS[gs]

    fallback = re.sub(r"[^a-z0-9_]+", "_", gs.lower()).strip("_")
    return fallback or "default"


def _load_layer_into_state(
    *,
    layer_name: str,
    layer_data: dict[str, Any],
    prompts_root: Path,
    prompt_versions: dict[str, str],
    blocks: dict[str, str],
    stages: dict[str, str],
    sources: dict[str, list[str]],
    referenced_snippets: set[str] | None = None,
) -> None:
    pv_raw = layer_data.get("prompt_versions")
    if pv_raw is not None:
        if not isinstance(pv_raw, dict):
            raise ValueError(f"{layer_name}: prompt_versions 必须为字典")
        for k, v in pv_raw.items():
            key = str(k).strip()
            if not key or not isinstance(v, str) or not v.strip():
                raise ValueError(f"{layer_name}: prompt_versions.{key} 必须为非空字符串")
            prompt_versions[key] = v.strip()
            sources.setdefault(f"prompt_versions.{key}", []).append(layer_name)

    for section_name, target in (("blocks", blocks), ("stages", stages)):
        sec_raw = layer_data.get(section_name)
        if sec_raw is None:
            continue
        if not isinstance(sec_raw, dict):
            raise ValueError(f"{layer_name}: {section_name} 必须为字典")
        for raw_key, directive in sec_raw.items():
            slot_key = str(raw_key).strip()
            if not slot_key:
                raise ValueError(f"{layer_name}: {section_name} 存在空键名")
            prev = target.get(slot_key, "")
            merged = _apply_slot_directive(
                previous=prev,
                directive=directive,
                prompts_root=prompts_root,
                slot_label=f"{layer_name}:{section_name}.{slot_key}",
                referenced_snippets=referenced_snippets,
            )
            target[slot_key] = merged
            sources.setdefault(f"{section_name}.{slot_key}", []).append(layer_name)


def load_prompt_seams(
    *,
    config_dir: Path = Path("config"),
    grade_subject: str = "小学奥数",
    slug: str | None = None,
    profile: str | None = None,
) -> ResolvedSeamBundle:
    """按三层继承（default → subject → profile）加载、校验并合并提示词接缝。"""
    prompts_root = _prompts_root(config_dir)
    default_path = prompts_root / "default.yaml"
    if not default_path.is_file():
        raise FileNotFoundError(f"缺少基础 Prompt 接缝配置: {default_path.as_posix()}")

    resolved_slug = (
        _validate_segment(slug, "subject slug")
        if slug is not None and slug.strip()
        else resolve_slug_for_subject(grade_subject, config_dir=config_dir)
    )
    resolved_profile = (
        _validate_segment(profile, "prompt profile")
        if profile is not None and profile.strip()
        else None
    )

    prompt_versions: dict[str, str] = {}
    blocks: dict[str, str] = {}
    stages: dict[str, str] = {}
    sources: dict[str, list[str]] = {}
    chain: list[str] = []

    # Layer 1: default.yaml
    chain.append("default.yaml")
    _load_layer_into_state(
        layer_name="default.yaml",
        layer_data=_read_yaml_mapping(default_path),
        prompts_root=prompts_root,
        prompt_versions=prompt_versions,
        blocks=blocks,
        stages=stages,
        sources=sources,
    )

    # Layer 2: subjects/<slug>.yaml (optional if subject has no dedicated overrides)
    subjects_dir = prompts_root / "subjects"
    if subjects_dir.exists():
        subject_path = _resolve_child_yaml(subjects_dir, f"{resolved_slug}.yaml", "subject")
        if subject_path.is_file():
            layer_rel = f"subjects/{resolved_slug}.yaml"
            chain.append(layer_rel)
            _load_layer_into_state(
                layer_name=layer_rel,
                layer_data=_read_yaml_mapping(subject_path),
                prompts_root=prompts_root,
                prompt_versions=prompt_versions,
                blocks=blocks,
                stages=stages,
                sources=sources,
            )

    # Layer 3: profiles/<name>.yaml (required if explicitly requested)
    if resolved_profile is not None:
        profiles_dir = prompts_root / "profiles"
        profile_path = _resolve_child_yaml(profiles_dir, f"{resolved_profile}.yaml", "profile")
        if not profile_path.is_file():
            raise FileNotFoundError(f"指定的 Prompt profile 不存在: {profile_path.as_posix()}")
        layer_rel = f"profiles/{resolved_profile}.yaml"
        chain.append(layer_rel)
        _load_layer_into_state(
            layer_name=layer_rel,
            layer_data=_read_yaml_mapping(profile_path),
            prompts_root=prompts_root,
            prompt_versions=prompt_versions,
            blocks=blocks,
            stages=stages,
            sources=sources,
        )

    # 校验必须槽位完整性
    missing_pv = [k for k in REQUIRED_PROMPT_VERSIONS if not prompt_versions.get(k, "").strip()]
    missing_blocks = [k for k in REQUIRED_BLOCKS if not blocks.get(k, "").strip()]
    missing_stages = [k for k in REQUIRED_STAGES if not stages.get(k, "").strip()]
    if missing_pv or missing_blocks or missing_stages:
        parts: list[str] = []
        if missing_pv:
            parts.append(f"prompt_versions={missing_pv}")
        if missing_blocks:
            parts.append(f"blocks={missing_blocks}")
        if missing_stages:
            parts.append(f"stages={missing_stages}")
        raise ValueError(f"Prompt 接缝缺失必填槽位或内容为空: {'; '.join(parts)}")

    digest = _compute_prompt_digest(blocks, stages)
    return ResolvedSeamBundle(
        grade_subject=grade_subject,
        slug=resolved_slug,
        profile=resolved_profile,
        chain=chain,
        prompt_versions=prompt_versions,
        blocks=blocks,
        stages=stages,
        sources=sources,
        prompt_digest=digest,
    )


def list_prompt_seams(config_dir: Path = Path("config")) -> dict[str, Any]:
    """列出 prompts 目录下的 default、学科覆写、profile 与片段清单。"""
    prompts_root = _prompts_root(config_dir)
    default_path = prompts_root / "default.yaml"

    subjects: list[dict[str, Any]] = []
    subjects_dir = prompts_root / "subjects"
    if subjects_dir.is_dir():
        for sp in sorted(subjects_dir.glob("*.yaml")):
            info: dict[str, Any] = {
                "slug": sp.stem,
                "file": sp.relative_to(prompts_root).as_posix(),
                "grade_subject": "",
                "override_blocks": [],
                "override_stages": [],
            }
            try:
                data = _read_yaml_mapping(sp)
                info["grade_subject"] = str(data.get("grade_subject") or "")
                if isinstance(data.get("blocks"), dict):
                    info["override_blocks"] = sorted(str(k) for k in data["blocks"].keys())
                if isinstance(data.get("stages"), dict):
                    info["override_stages"] = sorted(str(k) for k in data["stages"].keys())
            except Exception as exc:
                info["parse_error"] = str(exc)
            subjects.append(info)

    profiles: list[dict[str, Any]] = []
    profiles_dir = prompts_root / "profiles"
    if profiles_dir.is_dir():
        for pp in sorted(profiles_dir.glob("*.yaml")):
            pinfo: dict[str, Any] = {
                "name": pp.stem,
                "file": pp.relative_to(prompts_root).as_posix(),
                "description": "",
                "override_blocks": [],
                "override_stages": [],
            }
            try:
                data = _read_yaml_mapping(pp)
                pinfo["description"] = str(data.get("description") or "")
                if isinstance(data.get("blocks"), dict):
                    pinfo["override_blocks"] = sorted(str(k) for k in data["blocks"].keys())
                if isinstance(data.get("stages"), dict):
                    pinfo["override_stages"] = sorted(str(k) for k in data["stages"].keys())
            except Exception as exc:
                pinfo["parse_error"] = str(exc)
            profiles.append(pinfo)

    snippets: list[dict[str, Any]] = []
    snippets_dir = prompts_root / "snippets"
    if snippets_dir.is_dir():
        for sn in sorted(snippets_dir.rglob("*.md")):
            snippets.append(
                {
                    "path": sn.relative_to(prompts_root).as_posix(),
                    "size_bytes": sn.stat().st_size,
                }
            )

    default_versions: dict[str, str] = {}
    if default_path.is_file():
        try:
            ddata = _read_yaml_mapping(default_path)
            if isinstance(ddata.get("prompt_versions"), dict):
                default_versions = {str(k): str(v) for k, v in ddata["prompt_versions"].items()}
        except Exception:
            pass

    return {
        "prompts_root": prompts_root.as_posix(),
        "default_exists": default_path.is_file(),
        "default_prompt_versions": default_versions,
        "subjects": subjects,
        "profiles": profiles,
        "snippets": snippets,
        "required_blocks": list(REQUIRED_BLOCKS),
        "required_stages": list(REQUIRED_STAGES),
        "required_prompt_versions": list(REQUIRED_PROMPT_VERSIONS),
    }


def _check_unknown_slot_keys(
    layer_name: str,
    data: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> None:
    allowed_top = {"schema_version", "grade_subject", "slug", "description", "prompt_versions", "blocks", "stages"}
    unknown_top = sorted(set(data.keys()) - allowed_top)
    if unknown_top:
        warnings.append({"file": layer_name, "code": "UNKNOWN_TOP_KEYS", "message": f"未知顶层键: {unknown_top}"})

    blocks = data.get("blocks")
    if isinstance(blocks, dict):
        unknown_b = sorted(set(str(k) for k in blocks.keys()) - set(REQUIRED_BLOCKS))
        if unknown_b:
            warnings.append({"file": layer_name, "code": "UNKNOWN_BLOCK_KEYS", "message": f"未知 blocks 键: {unknown_b}"})

    stages = data.get("stages")
    if isinstance(stages, dict):
        unknown_s = sorted(set(str(k) for k in stages.keys()) - set(REQUIRED_STAGES))
        if unknown_s:
            warnings.append({"file": layer_name, "code": "UNKNOWN_STAGE_KEYS", "message": f"未知 stages 键: {unknown_s}"})

    pv = data.get("prompt_versions")
    if isinstance(pv, dict):
        unknown_pv = sorted(set(str(k) for k in pv.keys()) - set(REQUIRED_PROMPT_VERSIONS))
        if unknown_pv:
            warnings.append({"file": layer_name, "code": "UNKNOWN_VERSION_KEYS", "message": f"未知 prompt_versions 键: {unknown_pv}"})


def lint_prompt_seams(config_dir: Path = Path("config")) -> dict[str, Any]:
    """静态校验所有提示词接缝层、必填槽位、外链片段路径安全性与可合并性。"""
    prompts_root = _prompts_root(config_dir)
    checked_files: list[str] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    referenced_snippets: set[str] = set()

    default_path = prompts_root / "default.yaml"
    if not default_path.is_file():
        errors.append(
            {
                "file": "default.yaml",
                "code": "MISSING_DEFAULT_YAML",
                "message": f"缺少基础配置文件: {default_path.as_posix()}",
            }
        )
        return {"ok": False, "checked_files": checked_files, "errors": errors, "warnings": warnings}

    checked_files.append("default.yaml")
    try:
        default_data = _read_yaml_mapping(default_path)
        _check_unknown_slot_keys("default.yaml", default_data, warnings)
        pv: dict[str, str] = {}
        blk: dict[str, str] = {}
        stg: dict[str, str] = {}
        src: dict[str, list[str]] = {}
        _load_layer_into_state(
            layer_name="default.yaml",
            layer_data=default_data,
            prompts_root=prompts_root,
            prompt_versions=pv,
            blocks=blk,
            stages=stg,
            sources=src,
            referenced_snippets=referenced_snippets,
        )
        load_prompt_seams(config_dir=config_dir, grade_subject="__lint_base__", slug="lint-base-only")
    except Exception as exc:
        errors.append({"file": "default.yaml", "code": "INVALID_DEFAULT_LAYER", "message": str(exc)})

    subjects_dir = prompts_root / "subjects"
    if subjects_dir.is_dir():
        for sp in sorted(subjects_dir.glob("*.yaml")):
            rel = sp.relative_to(prompts_root).as_posix()
            checked_files.append(rel)
            try:
                sdata = _read_yaml_mapping(sp)
                _check_unknown_slot_keys(rel, sdata, warnings)
                # 追踪片段引用
                pv_tmp: dict[str, str] = {}
                blk_tmp: dict[str, str] = {}
                stg_tmp: dict[str, str] = {}
                src_tmp: dict[str, list[str]] = {}
                _load_layer_into_state(
                    layer_name=rel,
                    layer_data=sdata,
                    prompts_root=prompts_root,
                    prompt_versions=pv_tmp,
                    blocks=blk_tmp,
                    stages=stg_tmp,
                    sources=src_tmp,
                    referenced_snippets=referenced_snippets,
                )
                gs_val = str(sdata.get("grade_subject") or sp.stem)
                load_prompt_seams(config_dir=config_dir, grade_subject=gs_val, slug=sp.stem)
            except Exception as exc:
                errors.append({"file": rel, "code": "INVALID_SUBJECT_LAYER", "message": str(exc)})

    profiles_dir = prompts_root / "profiles"
    if profiles_dir.is_dir():
        for pp in sorted(profiles_dir.glob("*.yaml")):
            rel = pp.relative_to(prompts_root).as_posix()
            checked_files.append(rel)
            try:
                pdata = _read_yaml_mapping(pp)
                _check_unknown_slot_keys(rel, pdata, warnings)
                pv_tmp2: dict[str, str] = {}
                blk_tmp2: dict[str, str] = {}
                stg_tmp2: dict[str, str] = {}
                src_tmp2: dict[str, list[str]] = {}
                _load_layer_into_state(
                    layer_name=rel,
                    layer_data=pdata,
                    prompts_root=prompts_root,
                    prompt_versions=pv_tmp2,
                    blocks=blk_tmp2,
                    stages=stg_tmp2,
                    sources=src_tmp2,
                    referenced_snippets=referenced_snippets,
                )
                load_prompt_seams(
                    config_dir=config_dir,
                    grade_subject="__lint_profile__",
                    slug="lint-base-only",
                    profile=pp.stem,
                )
            except Exception as exc:
                errors.append({"file": rel, "code": "INVALID_PROFILE_LAYER", "message": str(exc)})

    snippets_dir = prompts_root / "snippets"
    if snippets_dir.is_dir():
        for sn in sorted(snippets_dir.rglob("*.md")):
            rel = sn.relative_to(prompts_root).as_posix()
            checked_files.append(rel)
            if not sn.read_text(encoding="utf-8").strip():
                warnings.append({"file": rel, "code": "EMPTY_SNIPPET", "message": "片段文件为空"})
            if rel not in referenced_snippets:
                warnings.append({"file": rel, "code": "UNREFERENCED_SNIPPET", "message": "未被任何 YAML 引用的片段文件"})

    return {
        "ok": len(errors) == 0,
        "checked_files": checked_files,
        "errors": errors,
        "warnings": warnings,
    }
