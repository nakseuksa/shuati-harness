"""Stage 3a/3b 词表规范管理、DRAFT → LOCKED 锁表迁移与 CHANGELOG 增量迁移。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from shuati.core import effective, refresh
from shuati.db import human_mirror
from shuati.db.connection import CompileLock, PreconditionError


def normalize_label(text: str) -> str:
    """基础规范化：去首尾空白、折叠内部空白并转小写。"""
    return " ".join((text or "").strip().split()).lower()


def loose_label_key(text: str) -> str:
    """宽松匹配键：剥离常见分隔符与全半角括号后转小写。"""
    return re.sub(r"[\s·\-_/、，,()（）\[\]【】]+", "", (text or "").strip()).lower()


class TaxonomyL2Entry(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class TaxonomyL1Entry(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    l2_entities: list[TaxonomyL2Entry] = Field(default_factory=list)


class TaxonomyChangeAction(BaseModel):
    op: Literal["add", "rename", "merge"]
    level: Literal["L1", "L2"]
    parent_l1: str = ""
    target: str = ""
    new_name: str = ""
    merge_into: str = ""
    aliases: list[str] = Field(default_factory=list)


class TaxonomyChangelogEntry(BaseModel):
    from_version: str
    to_version: str
    actions: list[TaxonomyChangeAction] = Field(default_factory=list)


class TaxonomySpec(BaseModel):
    schema_version: int = 1
    grade_subject: str
    slug: str
    status: Literal["DRAFT", "LOCKED"] = "DRAFT"
    version: str = "draft-1"
    modules: list[TaxonomyL1Entry] = Field(default_factory=list)
    changelog: list[TaxonomyChangelogEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_uniqueness(self) -> TaxonomySpec:
        if not self.modules:
            raise ValueError("taxonomy.modules 不能为空")
        seen_l1: set[str] = set()
        for mod in self.modules:
            k1 = normalize_label(mod.name)
            if not k1:
                raise ValueError("L1 模块名不能为空")
            if k1 in seen_l1:
                raise ValueError(f"存在重复 L1 模块名: {mod.name!r}")
            seen_l1.add(k1)
            seen_l2: set[str] = set()
            for l2 in mod.l2_entities:
                k2 = normalize_label(l2.name)
                if not k2:
                    raise ValueError(f"L1={mod.name!r} 下存在空 L2 实体名")
                if k2 in seen_l2:
                    raise ValueError(f"L1={mod.name!r} 下存在重复 L2 实体名: {l2.name!r}")
                seen_l2.add(k2)
        return self


@dataclass
class TaxonomyLookup:
    """零 LLM 成本精确/别名查表索引。"""

    spec: TaxonomySpec
    l1_exact: dict[str, str]
    l1_loose: dict[str, str]
    l2_by_l1_exact: dict[tuple[str, str], str]
    l2_by_l1_loose: dict[tuple[str, str], str]
    l2_unique_global: dict[str, tuple[str, str]]
    l2_unique_global_loose: dict[str, tuple[str, str]]

    def resolve_l1(self, raw_l1: str) -> str | None:
        k = normalize_label(raw_l1)
        if not k:
            return None
        if k in self.l1_exact:
            return self.l1_exact[k]
        lk = loose_label_key(raw_l1)
        return self.l1_loose.get(lk)

    def resolve_l2(self, raw_l1: str, raw_l2: str) -> tuple[str, str] | None:
        c_l1 = self.resolve_l1(raw_l1)
        k2 = normalize_label(raw_l2)
        lk2 = loose_label_key(raw_l2)
        if not k2:
            return None
        if c_l1 is not None:
            hit = self.l2_by_l1_exact.get((c_l1, k2)) or self.l2_by_l1_loose.get((c_l1, lk2))
            if hit is not None:
                return (c_l1, hit)
        # 若 raw_l2 在全词表中唯一归属某个 (L1, L2)，跨 L1 自动纠偏命中
        if k2 in self.l2_unique_global:
            return self.l2_unique_global[k2]
        if lk2 in self.l2_unique_global_loose:
            return self.l2_unique_global_loose[lk2]
        return None

    def has_pair(self, l1: str, l2: str) -> bool:
        return (l1, normalize_label(l2)) in self.l2_by_l1_exact and self.l2_by_l1_exact[(l1, normalize_label(l2))] == l2

    def l1_names(self) -> list[str]:
        return [m.name for m in self.spec.modules]

    def l2_names(self, l1: str) -> list[str]:
        for m in self.spec.modules:
            if m.name == l1:
                return [e.name for e in m.l2_entities]
        return []


def build_lookup_index(spec: TaxonomySpec) -> TaxonomyLookup:
    l1_exact: dict[str, str] = {}
    l1_loose: dict[str, str] = {}
    l2_by_l1_exact: dict[tuple[str, str], str] = {}
    l2_by_l1_loose: dict[tuple[str, str], str] = {}
    global_hits: dict[str, set[tuple[str, str]]] = {}
    global_hits_loose: dict[str, set[tuple[str, str]]] = {}

    for mod in spec.modules:
        for term in [mod.name, *mod.aliases]:
            nk = normalize_label(term)
            if nk:
                l1_exact.setdefault(nk, mod.name)
            lk = loose_label_key(term)
            if lk:
                l1_loose.setdefault(lk, mod.name)

        for l2 in mod.l2_entities:
            for term in [l2.name, *l2.aliases]:
                nk2 = normalize_label(term)
                if nk2:
                    l2_by_l1_exact.setdefault((mod.name, nk2), l2.name)
                    global_hits.setdefault(nk2, set()).add((mod.name, l2.name))
                lk2 = loose_label_key(term)
                if lk2:
                    l2_by_l1_loose.setdefault((mod.name, lk2), l2.name)
                    global_hits_loose.setdefault(lk2, set()).add((mod.name, l2.name))

    l2_unique_global = {k: next(iter(v)) for k, v in global_hits.items() if len(v) == 1}
    l2_unique_global_loose = {k: next(iter(v)) for k, v in global_hits_loose.items() if len(v) == 1}
    return TaxonomyLookup(
        spec=spec,
        l1_exact=l1_exact,
        l1_loose=l1_loose,
        l2_by_l1_exact=l2_by_l1_exact,
        l2_by_l1_loose=l2_by_l1_loose,
        l2_unique_global=l2_unique_global,
        l2_unique_global_loose=l2_unique_global_loose,
    )


def format_taxonomy_enum(spec: TaxonomySpec) -> str:
    """序列化紧凑 L1/L2 标准词表供 Prompt 内联约束。"""
    lines: list[str] = []
    for mod in spec.modules:
        l2_list = "、".join(e.name for e in mod.l2_entities)
        lines.append(f"- {mod.name}: {l2_list}")
    return "\n".join(lines)


def default_taxonomy_path(
    config_dir: Path | str,
    slug: str,
    status: Literal["DRAFT", "LOCKED"],
) -> Path:
    base = Path(config_dir) / "taxonomies"
    fname = f"taxonomy_{slug}.draft.yaml" if status == "DRAFT" else f"taxonomy_{slug}.yaml"
    return base / fname


def load_taxonomy_spec(yaml_path: Path | str) -> TaxonomySpec:
    p = Path(yaml_path)
    if not p.exists():
        raise PreconditionError(f"PRECONDITION_FAILED: 词表文件不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return TaxonomySpec.model_validate(raw)


def save_taxonomy_spec(spec: TaxonomySpec, yaml_path: Path | str) -> Path:
    p = Path(yaml_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = spec.model_dump(mode="json")
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def make_node_id(
    slug: str,
    taxonomy_version: str,
    node_level: Literal["L2", "L3"],
    l1_module: str,
    l2_entity: str,
    l3_operator: str = "",
) -> str:
    """生成确定性 ASCII node_id（严格限定于学段与版本作用域内）。"""
    raw = f"{slug}|{taxonomy_version}|{node_level}|{l1_module}|{l2_entity}|{l3_operator}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    safe_ver = re.sub(r"[^A-Za-z0-9_.-]+", "_", taxonomy_version)
    return f"node_{slug}_{safe_ver}_{node_level}_{digest}"


def _ensure_l2_and_l3_node(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    slug: str,
    taxonomy_version: str,
    l1_module: str,
    l2_entity: str,
    l3_operator: str,
) -> str:
    l2_id = make_node_id(slug, taxonomy_version, "L2", l1_module, l2_entity, "")
    conn.execute(
        """
        INSERT OR IGNORE INTO knowledge_nodes(
            node_id, grade_subject, taxonomy_version, node_level,
            l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
        )
        VALUES (?, ?, ?, 'L2', ?, ?, '', '[]', NULL);
        """,
        (l2_id, grade_subject, taxonomy_version, l1_module, l2_entity),
    )
    l3_id = make_node_id(slug, taxonomy_version, "L3", l1_module, l2_entity, l3_operator)
    row = conn.execute(
        """
        SELECT node_id FROM knowledge_nodes
        WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3'
          AND l1_module = ? AND l2_entity = ? AND l3_operator = ? AND redirect_to_id IS NULL
        LIMIT 1;
        """,
        (grade_subject, taxonomy_version, l1_module, l2_entity, l3_operator),
    ).fetchone()
    if row is not None:
        return str(row["node_id"])
    conn.execute(
        """
        INSERT OR IGNORE INTO knowledge_nodes(
            node_id, grade_subject, taxonomy_version, node_level,
            l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
        )
        VALUES (?, ?, ?, 'L3', ?, ?, ?, '[]', NULL);
        """,
        (l3_id, grade_subject, taxonomy_version, l1_module, l2_entity, l3_operator),
    )
    return l3_id


def _build_changelog_renames(spec: TaxonomySpec, from_version: str) -> tuple[dict[str, str], dict[tuple[str, str], tuple[str, str]]]:
    l1_rename: dict[str, str] = {}
    l2_rename: dict[tuple[str, str], tuple[str, str]] = {}
    for entry in spec.changelog:
        if from_version and entry.from_version != from_version and not entry.from_version.startswith("draft"):
            continue
        for act in entry.actions:
            dst = act.new_name if act.op == "rename" else act.merge_into
            if not dst or not act.target:
                continue
            if act.level == "L1":
                l1_rename[normalize_label(act.target)] = dst
            elif act.level == "L2" and act.parent_l1:
                l2_rename[(normalize_label(act.parent_l1), normalize_label(act.target))] = (act.parent_l1, dst)
    return l1_rename, l2_rename


_STAGE3C_DEFAULT_PV = "s3c_v2"
_STAGE3D_DEFAULT_PV = "s3d_v2"


def _sha256_json(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_s3_prompt_versions(config_dir: Path | str, grade_subject: str) -> tuple[str, str]:
    cfg = Path(config_dir)
    if not (cfg / "prompts" / "default.yaml").is_file():
        return _STAGE3C_DEFAULT_PV, _STAGE3D_DEFAULT_PV
    from shuati.llm.seam_loader import load_prompt_seams

    bundle = load_prompt_seams(config_dir=cfg, grade_subject=grade_subject, profile=None)
    return (
        bundle.prompt_versions.get("stage3c", _STAGE3C_DEFAULT_PV),
        bundle.prompt_versions.get("stage3d", _STAGE3D_DEFAULT_PV),
    )


def _resolve_l2_pair_in_new_spec(
    old_l1: str,
    old_l2: str,
    lookup: TaxonomyLookup,
    l1_renames: dict[str, str],
    l2_renames: dict[tuple[str, str], tuple[str, str]],
) -> tuple[str, str] | None:
    if not old_l1.strip() or not old_l2.strip():
        return None
    norm_l1 = normalize_label(old_l1)
    norm_l2 = normalize_label(old_l2)
    renamed_l1 = l1_renames.get(norm_l1, old_l1)
    mapped_pair = (
        l2_renames.get((norm_l1, norm_l2))
        or l2_renames.get((normalize_label(renamed_l1), norm_l2))
    )
    if mapped_pair is not None:
        cand_l1 = l1_renames.get(normalize_label(mapped_pair[0]), mapped_pair[0])
        cand_l2 = mapped_pair[1]
    else:
        cand_l1 = renamed_l1
        cand_l2 = old_l2
    if lookup.has_pair(cand_l1, cand_l2):
        return (cand_l1, cand_l2)
    return lookup.resolve_l2(cand_l1, cand_l2)


def _migrate_override_value(
    field_name: str,
    override_value: str,
    lookup: TaxonomyLookup,
    l1_renames: dict[str, str],
    l2_renames: dict[tuple[str, str], tuple[str, str]],
    target_version: str,
) -> tuple[bool, str, str]:
    """尝试将单条人工覆写迁移到新词表，返回 (ok, new_override_value, reason)。"""
    if field_name == "primary_l1_module":
        raw = override_value.strip()
        cand = l1_renames.get(normalize_label(raw), raw)
        hit = lookup.resolve_l1(cand)
        if hit is None:
            return False, override_value, f"L1 {raw!r} 无法映射到新词表"
        return True, hit, ""

    if field_name == "secondary_l1_modules_json":
        try:
            vals = json.loads(override_value)
        except ValueError:
            return False, override_value, "secondary_l1_modules_json 非法 JSON"
        if not isinstance(vals, list):
            return False, override_value, "secondary_l1_modules_json 非数组"
        mapped: list[str] = []
        for v in vals:
            raw = str(v).strip()
            cand = l1_renames.get(normalize_label(raw), raw)
            hit = lookup.resolve_l1(cand)
            if hit is None:
                return False, override_value, f"次要 L1 {raw!r} 无法映射到新词表"
            if hit not in mapped:
                mapped.append(hit)
        return True, json.dumps(mapped, ensure_ascii=False), ""

    if field_name == "taxonomy_override_json":
        try:
            obj = json.loads(override_value)
        except ValueError:
            return False, override_value, "taxonomy_override_json 非法 JSON"
        if not isinstance(obj, dict) or not isinstance(obj.get("nodes"), list):
            return False, override_value, "taxonomy_override_json 缺少 nodes 数组"
        new_nodes: list[dict[str, Any]] = []
        for nd in obj["nodes"]:
            if not isinstance(nd, dict):
                return False, override_value, "节点项非对象"
            raw_l1 = str(nd.get("l1") or "")
            raw_l2 = str(nd.get("l2") or "")
            raw_l3 = str(nd.get("l3") or "")
            pair_hit = _resolve_l2_pair_in_new_spec(raw_l1, raw_l2, lookup, l1_renames, l2_renames)
            if pair_hit is None or not raw_l3.strip():
                return False, override_value, f"考点 ({raw_l1!r}, {raw_l2!r}, {raw_l3!r}) 无法映射"
            new_l1, new_l2 = pair_hit
            new_nodes.append({**nd, "l1": new_l1, "l2": new_l2, "l3": raw_l3.strip()})
        new_obj = {**obj, "taxonomy_version": target_version, "nodes": new_nodes}
        return True, json.dumps(new_obj, ensure_ascii=False), ""

    return True, override_value, ""


def _follow_old_redirect_target(
    old_id: str,
    old_redirect_map: dict[str, str | None],
    node_id_map: dict[str, str],
) -> str | None:
    curr: str | None = old_id
    visited: set[str] = set()
    while curr and curr not in visited:
        if curr in node_id_map:
            return node_id_map[curr]
        visited.add(curr)
        curr = old_redirect_map.get(curr)
    return None


def _migrate_l3_vectors_npz(
    conn: sqlite3.Connection,
    *,
    vectors_dir: Path | str,
    grade_subject: str,
    slug: str,
    target_version: str,
    node_id_map: dict[str, str],
) -> int:
    npz_file = Path(vectors_dir) / f"l3_nodes_{slug}.npz"
    if not npz_file.exists():
        return 0

    import numpy as np

    from shuati.vectors.store import VectorStore

    active_l3_ids = {
        str(r["node_id"])
        for r in conn.execute(
            """
            SELECT node_id
            FROM knowledge_nodes
            WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3' AND redirect_to_id IS NULL;
            """,
            (grade_subject, target_version),
        ).fetchall()
    }

    store = VectorStore(vectors_dir)
    with store._lock():
        if not npz_file.exists():
            return 0
        old_ids, old_hashes, old_vecs = store._load_raw_npz(npz_file)
        if not (len(old_ids) == len(old_hashes) == len(old_vecs)):
            return 0
        new_ids: list[str] = []
        new_hashes: list[str] = []
        keep_indices: list[int] = []
        seen_new: set[str] = set()
        for idx, oid in enumerate(old_ids):
            nid = node_id_map.get(oid)
            if nid is None or nid not in active_l3_ids or nid in seen_new:
                continue
            seen_new.add(nid)
            new_ids.append(nid)
            new_hashes.append(old_hashes[idx])
            keep_indices.append(idx)
        dim = int(old_vecs.shape[1]) if old_vecs.ndim == 2 else 0
        new_vecs = (
            old_vecs[keep_indices]
            if keep_indices
            else np.zeros((0, dim), dtype=np.float32)
        )
        store._save_npz(npz_file, new_ids, new_hashes, new_vecs)
        return len(new_ids)


def transition_taxonomy(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    yaml_path: Path | str,
    target_version: str,
    target_status: Literal["DRAFT", "LOCKED"],
    allow_stale_overrides: bool = False,
    dry_run: bool = False,
    force_recompile: bool = False,
    vectors_dir: Path | str = Path("data/vectors"),
    config_dir: Path | str = Path("config"),
    backups_dir: Path | str = Path("data/backups"),
    human_dir: Path | str = Path("human"),
    reports_dir: Path | str = Path("data/reports"),
    lock_path: Path | str = Path("data/.compile.lock"),
) -> dict[str, Any]:
    """执行 DRAFT 换版或 DRAFT → LOCKED 1.0 事务（默认智能无损晋升，--force-recompile 全清重跑）。"""
    gs_row = conn.execute(
        "SELECT grade_subject, slug, taxonomy_status, taxonomy_version FROM grade_subjects WHERE grade_subject = ?",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科不存在: {grade_subject!r}")

    slug = str(gs_row["slug"])
    old_version = str(gs_row["taxonomy_version"] or "")
    spec = load_taxonomy_spec(yaml_path)
    if spec.grade_subject != grade_subject:
        raise PreconditionError(
            f"PRECONDITION_FAILED: YAML grade_subject={spec.grade_subject!r} 与目标 {grade_subject!r} 不一致"
        )

    updated_spec = spec.model_copy(update={"status": target_status, "version": target_version})
    lookup = build_lookup_index(updated_spec)
    l1_renames, l2_renames = _build_changelog_renames(updated_spec, old_version)

    # 预解析旧版本所有 (old_l1, old_l2) 在新词表中的映射关系
    old_pairs_raw: set[tuple[str, str]] = set()
    for r in conn.execute(
        """
        SELECT DISTINCT l1_module, l2_entity
        FROM knowledge_nodes
        WHERE grade_subject = ? AND taxonomy_version != ?;
        """,
        (grade_subject, target_version),
    ).fetchall():
        l1v, l2v = str(r["l1_module"] or ""), str(r["l2_entity"] or "")
        if l1v.strip() or l2v.strip():
            old_pairs_raw.add((l1v, l2v))
    for r in conn.execute(
        """
        SELECT DISTINCT s.m_l1, s.m_l2
        FROM question_steps s
        JOIN questions q ON q.question_uid = s.question_uid
        WHERE q.grade_subject = ? AND (s.m_l1 != '' OR s.m_l2 != '');
        """,
        (grade_subject,),
    ).fetchall():
        old_pairs_raw.add((str(r["m_l1"] or ""), str(r["m_l2"] or "")))

    mapped_l2_pairs: dict[tuple[str, str], tuple[str, str]] = {}
    dropped_l2_pairs: set[tuple[str, str]] = set()
    for p_l1, p_l2 in sorted(old_pairs_raw):
        resolved_pair = _resolve_l2_pair_in_new_spec(p_l1, p_l2, lookup, l1_renames, l2_renames)
        if resolved_pair is not None:
            mapped_l2_pairs[(p_l1, p_l2)] = resolved_pair
        else:
            dropped_l2_pairs.add((p_l1, p_l2))

    # 1. 预检激活人工覆写迁移
    ov_rows = conn.execute(
        """
        SELECT o.override_id, o.question_uid, o.field_name, o.override_value, o.taxonomy_version
        FROM human_overrides o
        JOIN questions q ON q.question_uid = o.question_uid
        WHERE q.grade_subject = ?
          AND o.is_active = 1
          AND o.is_suspended = 0
          AND o.field_name IN ('primary_l1_module', 'secondary_l1_modules_json', 'taxonomy_override_json');
        """,
        (grade_subject,),
    ).fetchall()

    migrated: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for r in ov_rows:
        ok, new_val, reason = _migrate_override_value(
            str(r["field_name"]),
            str(r["override_value"]),
            lookup,
            l1_renames,
            l2_renames,
            target_version,
        )
        if ok:
            migrated.append(
                {
                    "override_id": int(r["override_id"]),
                    "question_uid": str(r["question_uid"]),
                    "field_name": str(r["field_name"]),
                    "new_value": new_val,
                }
            )
        else:
            unmapped.append(
                {
                    "override_id": int(r["override_id"]),
                    "question_uid": str(r["question_uid"]),
                    "field_name": str(r["field_name"]),
                    "old_value": str(r["override_value"]),
                    "reason": reason,
                }
            )

    if dry_run:
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM questions WHERE grade_subject = ?",
            (grade_subject,),
        ).fetchone()
        return {
            "dry_run": True,
            "grade_subject": grade_subject,
            "from_version": old_version,
            "target_status": target_status,
            "target_version": target_version,
            "promotion_mode": "smart_preserve" if not force_recompile else "force_recompile",
            "preserved_l2_pairs": len(mapped_l2_pairs),
            "dropped_l2_pairs": len(dropped_l2_pairs),
            "total_overrides_checked": len(ov_rows),
            "migrated_count": len(migrated),
            "unmapped_count": len(unmapped),
            "unmapped": unmapped,
            "affected_questions": int(q_count["c"]) if q_count else 0,
        }

    report_path = Path(reports_dir) / "taxonomy_override_migration.json"
    if unmapped:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "grade_subject": grade_subject,
                    "from_version": old_version,
                    "target_version": target_version,
                    "unmapped": unmapped,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if not allow_stale_overrides:
            raise PreconditionError(
                f"PRECONDITION_FAILED: 存在 {len(unmapped)} 条人工覆写无法自动迁移，"
                f"已导出 {report_path}；请先处理或加 --allow-stale-overrides 挂起后继续。"
            )

    old_kn_cnt_row = conn.execute(
        "SELECT COUNT(*) AS c FROM knowledge_nodes WHERE grade_subject = ? AND taxonomy_version != ?;",
        (grade_subject, target_version),
    ).fetchone()
    has_old_nodes = int(old_kn_cnt_row["c"] if old_kn_cnt_row else 0) > 0
    use_smart_promote = (
        (not force_recompile)
        and bool(old_version)
        and (old_version != target_version)
        and has_old_nodes
    )

    target_yaml_path = default_taxonomy_path(config_dir, slug, target_status)
    backup_path = human_mirror.vacuum_backup(conn, backups_dir, "taxonomy_lock")

    node_id_map: dict[str, str] = {}
    migrated_practice_logs = 0
    nulled_practice_logs = 0
    stale_count_out = 0
    preserved_count_out = 0

    with CompileLock(lock_path):
        conn.execute("BEGIN IMMEDIATE;")
        try:
            old_node_where = "grade_subject = ? AND taxonomy_version != ?"
            old_node_params: tuple[Any, ...] = (grade_subject, target_version)

            if not use_smart_promote:
                # 原有全清重跑路径（force_recompile=True 或 old_version == '' 或无旧节点）
                for item in migrated:
                    if item["field_name"] == "taxonomy_override_json":
                        tax_obj = json.loads(item["new_value"])
                        for nd in tax_obj["nodes"]:
                            nd["node_id"] = _ensure_l2_and_l3_node(
                                conn,
                                grade_subject=grade_subject,
                                slug=slug,
                                taxonomy_version=target_version,
                                l1_module=nd["l1"],
                                l2_entity=nd["l2"],
                                l3_operator=nd["l3"],
                            )
                        item["new_value"] = json.dumps(tax_obj, ensure_ascii=False)
                    conn.execute(
                        """
                        UPDATE human_overrides
                        SET override_value = ?, taxonomy_version = ?
                        WHERE override_id = ?;
                        """,
                        (item["new_value"], target_version, item["override_id"]),
                    )

                for item in unmapped:
                    conn.execute(
                        """
                        UPDATE human_overrides
                        SET is_suspended = 1, suspended_reason = 'TAXONOMY_UNMAPPED'
                        WHERE override_id = ?;
                        """,
                        (item["override_id"],),
                    )

                conn.execute(
                    """
                    UPDATE question_steps
                    SET m_l1 = '', m_l2 = '', m_knowledge_node_id = NULL
                    WHERE question_uid IN (SELECT question_uid FROM questions WHERE grade_subject = ?);
                    """,
                    (grade_subject,),
                )
                conn.execute(
                    """
                    UPDATE questions
                    SET m_primary_l1_module = '', m_secondary_l1_modules_json = '[]'
                    WHERE grade_subject = ?;
                    """,
                    (grade_subject,),
                )

                for mod in updated_spec.modules:
                    for l2 in mod.l2_entities:
                        l2_id = make_node_id(slug, target_version, "L2", mod.name, l2.name, "")
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO knowledge_nodes(
                                node_id, grade_subject, taxonomy_version, node_level,
                                l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                            )
                            VALUES (?, ?, ?, 'L2', ?, ?, '', ?, NULL);
                            """,
                            (
                                l2_id,
                                grade_subject,
                                target_version,
                                mod.name,
                                l2.name,
                                json.dumps(l2.aliases, ensure_ascii=False),
                            ),
                        )

                pl_rows = conn.execute(
                    f"""
                    SELECT p.log_id, kn.l1_module, kn.l2_entity
                    FROM practice_log p
                    JOIN knowledge_nodes kn ON kn.node_id = p.stuck_node_id
                    WHERE kn.{old_node_where};
                    """,
                    old_node_params,
                ).fetchall()
                for pr in pl_rows:
                    raw_l1 = str(pr["l1_module"] or "")
                    raw_l2 = str(pr["l2_entity"] or "")
                    pair_hit = _resolve_l2_pair_in_new_spec(raw_l1, raw_l2, lookup, l1_renames, l2_renames)
                    if pair_hit is not None:
                        new_l1, new_l2 = pair_hit
                        new_l2_id = make_node_id(slug, target_version, "L2", new_l1, new_l2, "")
                        conn.execute(
                            "UPDATE practice_log SET stuck_node_id = ? WHERE log_id = ?;",
                            (new_l2_id, pr["log_id"]),
                        )
                        migrated_practice_logs += 1
                    else:
                        conn.execute(
                            "UPDATE practice_log SET stuck_node_id = NULL WHERE log_id = ?;",
                            (pr["log_id"],),
                        )
                        nulled_practice_logs += 1

                conn.execute(
                    f"""
                    DELETE FROM knowledge_edges
                    WHERE grade_subject = ?
                      AND (
                          source_node_id IN (SELECT node_id FROM knowledge_nodes WHERE {old_node_where})
                          OR target_node_id IN (SELECT node_id FROM knowledge_nodes WHERE {old_node_where})
                      );
                    """,
                    (grade_subject, *old_node_params, *old_node_params),
                )
                conn.execute(
                    f"DELETE FROM knowledge_nodes WHERE {old_node_where} AND redirect_to_id IS NOT NULL;",
                    old_node_params,
                )
                conn.execute(
                    f"DELETE FROM knowledge_nodes WHERE {old_node_where};",
                    old_node_params,
                )
                ledger_rows = conn.execute(
                    "SELECT merge_id, scope_json FROM compaction_audit_ledger WHERE target_kind = 'KNOWLEDGE_NODE' AND is_reverted = 0;"
                ).fetchall()
                for lr in ledger_rows:
                    try:
                        sc = json.loads(lr["scope_json"])
                    except ValueError:
                        continue
                    if sc.get("grade_subject") == grade_subject and sc.get("taxonomy_version") != target_version:
                        conn.execute(
                            "UPDATE compaction_audit_ledger SET is_reverted = 1, reverted_at = datetime('now') WHERE merge_id = ?;",
                            (lr["merge_id"],),
                        )

                conn.execute(
                    """
                    UPDATE grade_subjects
                    SET taxonomy_status = ?, taxonomy_version = ?, taxonomy_file = ?
                    WHERE grade_subject = ?;
                    """,
                    (target_status, target_version, str(target_yaml_path), grade_subject),
                )
                q_uids = [
                    str(r["question_uid"])
                    for r in conn.execute(
                        "SELECT question_uid FROM questions WHERE grade_subject = ?;",
                        (grade_subject,),
                    ).fetchall()
                ]
                for uid in q_uids:
                    refresh.mark_stages_stale(conn, uid, ["S3C", "S3D", "S4"])
                stale_count_out = len(q_uids)
            else:
                # Smart In-Place Promotion（智能无损晋升）
                # Step A: 创建新版本 L2、活跃主 L3 与重定向子 L3 节点
                for mod in updated_spec.modules:
                    for l2 in mod.l2_entities:
                        l2_id = make_node_id(slug, target_version, "L2", mod.name, l2.name, "")
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO knowledge_nodes(
                                node_id, grade_subject, taxonomy_version, node_level,
                                l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                            )
                            VALUES (?, ?, ?, 'L2', ?, ?, '', ?, NULL);
                            """,
                            (
                                l2_id,
                                grade_subject,
                                target_version,
                                mod.name,
                                l2.name,
                                json.dumps(l2.aliases, ensure_ascii=False),
                            ),
                        )

                old_kn_rows = conn.execute(
                    f"""
                    SELECT node_id, node_level, l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                    FROM knowledge_nodes
                    WHERE {old_node_where}
                    ORDER BY node_id;
                    """,
                    old_node_params,
                ).fetchall()

                old_redirect_map: dict[str, str | None] = {}
                new_node_meta: dict[str, tuple[str, str, str]] = {}
                reverse_l3_sources: dict[str, list[str]] = {}
                created_redirect_new_ids: set[str] = set()
                new_redirect_target: dict[str, str | None] = {}

                for kn in old_kn_rows:
                    oid = str(kn["node_id"])
                    old_redirect_map[oid] = str(kn["redirect_to_id"]) if kn["redirect_to_id"] else None
                    if kn["node_level"] == "L2":
                        p_hit = mapped_l2_pairs.get((str(kn["l1_module"]), str(kn["l2_entity"])))
                        if p_hit is not None:
                            node_id_map[oid] = make_node_id(slug, target_version, "L2", p_hit[0], p_hit[1], "")

                # 1) 活跃主 L3 节点（redirect_to_id IS NULL）
                for kn in old_kn_rows:
                    if kn["node_level"] != "L3" or kn["redirect_to_id"] is not None:
                        continue
                    oid = str(kn["node_id"])
                    ol1, ol2 = str(kn["l1_module"]), str(kn["l2_entity"])
                    p_hit = mapped_l2_pairs.get((ol1, ol2))
                    if p_hit is None:
                        continue
                    nl1, nl2 = p_hit
                    l3_op = str(kn["l3_operator"])
                    new_id = make_node_id(slug, target_version, "L3", nl1, nl2, l3_op)
                    old_al = json.loads(kn["aliases_json"] or "[]")
                    ex_kn = conn.execute(
                        "SELECT node_id, aliases_json FROM knowledge_nodes WHERE node_id = ?;",
                        (new_id,),
                    ).fetchone()
                    if ex_kn is not None:
                        cur_al = json.loads(ex_kn["aliases_json"] or "[]")
                        merged_al = [
                            a for a in dict.fromkeys([*cur_al, *old_al]) if a and a != l3_op
                        ][:32]
                        conn.execute(
                            "UPDATE knowledge_nodes SET aliases_json = ? WHERE node_id = ?;",
                            (json.dumps(merged_al, ensure_ascii=False), new_id),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO knowledge_nodes(
                                node_id, grade_subject, taxonomy_version, node_level,
                                l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                            )
                            VALUES (?, ?, ?, 'L3', ?, ?, ?, ?, NULL);
                            """,
                            (
                                new_id,
                                grade_subject,
                                target_version,
                                nl1,
                                nl2,
                                l3_op,
                                json.dumps(old_al, ensure_ascii=False),
                            ),
                        )
                    node_id_map[oid] = new_id
                    new_node_meta[new_id] = (nl1, nl2, l3_op)
                    new_redirect_target[new_id] = None
                    reverse_l3_sources.setdefault(new_id, []).append(oid)

                # 2) 重定向子 L3 节点（redirect_to_id IS NOT NULL，保持跳数 <= 1）
                for kn in old_kn_rows:
                    if kn["node_level"] != "L3" or kn["redirect_to_id"] is None:
                        continue
                    oid = str(kn["node_id"])
                    old_tgt = str(kn["redirect_to_id"])
                    new_target_id = _follow_old_redirect_target(old_tgt, old_redirect_map, node_id_map)
                    if new_target_id is None:
                        continue
                    ol1, ol2 = str(kn["l1_module"]), str(kn["l2_entity"])
                    p_hit = mapped_l2_pairs.get((ol1, ol2))
                    if p_hit is not None:
                        nl1, nl2 = p_hit
                    elif new_target_id in new_node_meta:
                        nl1, nl2 = new_node_meta[new_target_id][0], new_node_meta[new_target_id][1]
                    else:
                        continue
                    l3_op = str(kn["l3_operator"])
                    new_child_id = make_node_id(slug, target_version, "L3", nl1, nl2, l3_op)
                    if new_child_id == new_target_id:
                        node_id_map[oid] = new_target_id
                        reverse_l3_sources.setdefault(new_target_id, []).append(oid)
                        continue
                    active_conflict = conn.execute(
                        """
                        SELECT node_id FROM knowledge_nodes
                        WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3'
                          AND l1_module = ? AND l2_entity = ? AND l3_operator = ? AND redirect_to_id IS NULL
                        LIMIT 1;
                        """,
                        (grade_subject, target_version, nl1, nl2, l3_op),
                    ).fetchone()
                    ex_child = conn.execute(
                        "SELECT node_id, redirect_to_id FROM knowledge_nodes WHERE node_id = ?;",
                        (new_child_id,),
                    ).fetchone()
                    if active_conflict is None and ex_child is None:
                        conn.execute(
                            """
                            INSERT INTO knowledge_nodes(
                                node_id, grade_subject, taxonomy_version, node_level,
                                l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                            )
                            VALUES (?, ?, ?, 'L3', ?, ?, ?, ?, ?);
                            """,
                            (
                                new_child_id,
                                grade_subject,
                                target_version,
                                nl1,
                                nl2,
                                l3_op,
                                kn["aliases_json"] or "[]",
                                new_target_id,
                            ),
                        )
                        node_id_map[oid] = new_child_id
                        new_node_meta[new_child_id] = (nl1, nl2, l3_op)
                        created_redirect_new_ids.add(new_child_id)
                        new_redirect_target[new_child_id] = new_target_id
                        reverse_l3_sources.setdefault(new_child_id, []).append(oid)
                    elif ex_child is not None and ex_child["redirect_to_id"] == new_target_id:
                        node_id_map[oid] = new_child_id
                        reverse_l3_sources.setdefault(new_child_id, []).append(oid)
                    else:
                        # 命名冲突无法独立保留重定向壳时，严格追随其真实归并目标主节点 new_target_id
                        node_id_map[oid] = new_target_id
                        reverse_l3_sources.setdefault(new_target_id, []).append(oid)

                # Step B: 迁移引用指针
                # B1. human_overrides
                for item in migrated:
                    if item["field_name"] == "taxonomy_override_json":
                        tax_obj = json.loads(item["new_value"])
                        for nd in tax_obj["nodes"]:
                            nd["node_id"] = _ensure_l2_and_l3_node(
                                conn,
                                grade_subject=grade_subject,
                                slug=slug,
                                taxonomy_version=target_version,
                                l1_module=nd["l1"],
                                l2_entity=nd["l2"],
                                l3_operator=nd["l3"],
                            )
                        item["new_value"] = json.dumps(tax_obj, ensure_ascii=False)
                    conn.execute(
                        """
                        UPDATE human_overrides
                        SET override_value = ?, taxonomy_version = ?
                        WHERE override_id = ?;
                        """,
                        (item["new_value"], target_version, item["override_id"]),
                    )

                for item in unmapped:
                    conn.execute(
                        """
                        UPDATE human_overrides
                        SET is_suspended = 1, suspended_reason = 'TAXONOMY_UNMAPPED'
                        WHERE override_id = ?;
                        """,
                        (item["override_id"],),
                    )

                # B2. practice_log.stuck_node_id（优先精确保住 L3 级卡点）
                pl_rows = conn.execute(
                    f"""
                    SELECT p.log_id, p.stuck_node_id, kn.l1_module, kn.l2_entity
                    FROM practice_log p
                    JOIN knowledge_nodes kn ON kn.node_id = p.stuck_node_id
                    WHERE kn.{old_node_where};
                    """,
                    old_node_params,
                ).fetchall()
                for pr in pl_rows:
                    old_snid = str(pr["stuck_node_id"] or "")
                    if old_snid in node_id_map:
                        conn.execute(
                            "UPDATE practice_log SET stuck_node_id = ? WHERE log_id = ?;",
                            (node_id_map[old_snid], pr["log_id"]),
                        )
                        migrated_practice_logs += 1
                    else:
                        pair_hit = _resolve_l2_pair_in_new_spec(
                            str(pr["l1_module"] or ""),
                            str(pr["l2_entity"] or ""),
                            lookup,
                            l1_renames,
                            l2_renames,
                        )
                        if pair_hit is not None:
                            new_l2_id = make_node_id(slug, target_version, "L2", pair_hit[0], pair_hit[1], "")
                            conn.execute(
                                "UPDATE practice_log SET stuck_node_id = ? WHERE log_id = ?;",
                                (new_l2_id, pr["log_id"]),
                            )
                            migrated_practice_logs += 1
                        else:
                            conn.execute(
                                "UPDATE practice_log SET stuck_node_id = NULL WHERE log_id = ?;",
                                (pr["log_id"],),
                            )
                            nulled_practice_logs += 1

                # B3. question_steps 与 questions 机器分类列
                q_all_rows = conn.execute(
                    """
                    SELECT q.question_uid, q.m_primary_l1_module, q.m_secondary_l1_modules_json,
                           q.raw_l1_candidates_json,
                           cs3c.status AS s3c_status, cs3c.prompt_version AS s3c_pv,
                           cs3d.status AS s3d_status, cs3d.prompt_version AS s3d_pv
                    FROM questions q
                    LEFT JOIN compile_state cs3c ON cs3c.question_uid = q.question_uid AND cs3c.stage = 'S3C'
                    LEFT JOIN compile_state cs3d ON cs3d.question_uid = q.question_uid AND cs3d.stage = 'S3D'
                    WHERE q.grade_subject = ?
                    ORDER BY q.question_uid;
                    """,
                    (grade_subject,),
                ).fetchall()

                steps_by_q: dict[str, list[sqlite3.Row]] = {}
                for sr in conn.execute(
                    """
                    SELECT s.step_id, s.question_uid, s.branch_id, s.step_index,
                           s.sub_goal, s.raw_l1, s.raw_l2, s.raw_l3,
                           s.m_l1, s.m_l2, s.m_knowledge_node_id
                    FROM question_steps s
                    JOIN questions q ON q.question_uid = s.question_uid
                    WHERE q.grade_subject = ?
                    ORDER BY s.question_uid, s.branch_id, s.step_index;
                    """,
                    (grade_subject,),
                ).fetchall():
                    steps_by_q.setdefault(str(sr["question_uid"]), []).append(sr)

                stale_uids: list[str] = []
                preserved_q_meta: list[dict[str, Any]] = []

                for qr in q_all_rows:
                    uid = str(qr["question_uid"])
                    q_steps = steps_by_q.get(uid, [])
                    raw_prim = str(qr["m_primary_l1_module"] or "")
                    s3c_st = str(qr["s3c_status"] or "")
                    s3d_st = str(qr["s3d_status"] or "")
                    has_any_machine_tax = bool(raw_prim.strip()) or any(
                        bool(s["m_l1"] or s["m_l2"] or s["m_knowledge_node_id"]) for s in q_steps
                    )

                    if not has_any_machine_tax and s3c_st != "OK" and s3d_st != "OK":
                        continue

                    cand_prim = l1_renames.get(normalize_label(raw_prim), raw_prim)
                    new_prim = lookup.resolve_l1(cand_prim)

                    sec_ok = True
                    new_secs: list[str] = []
                    try:
                        raw_secs = json.loads(qr["m_secondary_l1_modules_json"] or "[]")
                        if not isinstance(raw_secs, list):
                            sec_ok = False
                            raw_secs = []
                    except ValueError:
                        sec_ok = False
                        raw_secs = []
                    for s_val in raw_secs:
                        s_str = str(s_val).strip()
                        if not s_str:
                            continue
                        c_sec = l1_renames.get(normalize_label(s_str), s_str)
                        h_sec = lookup.resolve_l1(c_sec)
                        if h_sec is None:
                            sec_ok = False
                            break
                        if h_sec != new_prim and h_sec not in new_secs:
                            new_secs.append(h_sec)

                    steps_ok = True
                    step_updates: list[tuple[str, str, str | None, str]] = []
                    for s in q_steps:
                        old_ml1 = str(s["m_l1"] or "")
                        old_ml2 = str(s["m_l2"] or "")
                        old_nid = s["m_knowledge_node_id"]
                        p_hit = mapped_l2_pairs.get((old_ml1, old_ml2)) or _resolve_l2_pair_in_new_spec(
                            old_ml1, old_ml2, lookup, l1_renames, l2_renames
                        )
                        if p_hit is None:
                            steps_ok = False
                            break
                        if old_nid is not None:
                            mapped_nid = node_id_map.get(str(old_nid))
                            if mapped_nid is None:
                                steps_ok = False
                                break
                        else:
                            if s3d_st == "OK":
                                steps_ok = False
                                break
                            mapped_nid = None
                        step_updates.append((p_hit[0], p_hit[1], mapped_nid, str(s["step_id"])))

                    if new_prim is not None and sec_ok and steps_ok:
                        for nl1, nl2, mnid, sid in step_updates:
                            conn.execute(
                                "UPDATE question_steps SET m_l1 = ?, m_l2 = ?, m_knowledge_node_id = ? WHERE step_id = ?;",
                                (nl1, nl2, mnid, sid),
                            )
                        conn.execute(
                            """
                            UPDATE questions
                            SET m_primary_l1_module = ?, m_secondary_l1_modules_json = ?
                            WHERE question_uid = ?;
                            """,
                            (new_prim, json.dumps(new_secs, ensure_ascii=False), uid),
                        )
                        preserved_q_meta.append(
                            {
                                "uid": uid,
                                "new_prim": new_prim,
                                "raw_l1s": qr["raw_l1_candidates_json"],
                                "s3c_status": s3c_st,
                                "s3c_pv": str(qr["s3c_pv"] or ""),
                                "s3d_status": s3d_st,
                                "s3d_pv": str(qr["s3d_pv"] or ""),
                                "q_steps": q_steps,
                                "step_updates": step_updates,
                            }
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE question_steps
                            SET m_l1 = '', m_l2 = '', m_knowledge_node_id = NULL
                            WHERE question_uid = ?;
                            """,
                            (uid,),
                        )
                        conn.execute(
                            """
                            UPDATE questions
                            SET m_primary_l1_module = '', m_secondary_l1_modules_json = '[]'
                            WHERE question_uid = ?;
                            """,
                            (uid,),
                        )
                        stale_uids.append(uid)

                # B4. knowledge_edges 迁移
                old_edges = conn.execute(
                    f"""
                    SELECT source_node_id, target_node_id, edge_type, weight, evidence_json
                    FROM knowledge_edges
                    WHERE grade_subject = ?
                      AND (
                          source_node_id IN (SELECT node_id FROM knowledge_nodes WHERE {old_node_where})
                          OR target_node_id IN (SELECT node_id FROM knowledge_nodes WHERE {old_node_where})
                      )
                    ORDER BY weight DESC, source_node_id, target_node_id;
                    """,
                    (grade_subject, *old_node_params, *old_node_params),
                ).fetchall()

                prereq_candidates: list[dict[str, Any]] = []
                co_edges_to_insert: list[tuple[str, str, str, float, str]] = []
                seen_edge_keys: set[tuple[str, str, str]] = set()
                for er in old_edges:
                    nsrc = node_id_map.get(str(er["source_node_id"]), str(er["source_node_id"]))
                    ndst = node_id_map.get(str(er["target_node_id"]), str(er["target_node_id"]))
                    if (
                        str(er["source_node_id"]) not in node_id_map
                        or str(er["target_node_id"]) not in node_id_map
                        or nsrc == ndst
                    ):
                        continue
                    etype = str(er["edge_type"])
                    ekey = (nsrc, ndst, etype)
                    if ekey in seen_edge_keys:
                        continue
                    seen_edge_keys.add(ekey)
                    w_val = float(er["weight"])
                    ev_json = str(er["evidence_json"] or "{}")
                    if etype == "PREREQUISITE":
                        prereq_candidates.append(
                            {
                                "source_node_id": nsrc,
                                "target_node_id": ndst,
                                "weight": w_val,
                                "evidence_json": ev_json,
                            }
                        )
                    else:
                        co_edges_to_insert.append((nsrc, ndst, etype, w_val, ev_json))

                if prereq_candidates and len(set(node_id_map.values())) < len(node_id_map):
                    from shuati.core.knowledge_graph import check_dag_and_break_cycles

                    prereq_candidates, _ = check_dag_and_break_cycles(prereq_candidates)

                for pe in prereq_candidates:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_edges(
                            source_node_id, target_node_id, grade_subject, edge_type, weight, evidence_json
                        )
                        VALUES (?, ?, ?, 'PREREQUISITE', ?, ?);
                        """,
                        (
                            pe["source_node_id"],
                            pe["target_node_id"],
                            grade_subject,
                            pe["weight"],
                            pe["evidence_json"],
                        ),
                    )
                for nsrc, ndst, etype, w_val, ev_json in co_edges_to_insert:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_edges(
                            source_node_id, target_node_id, grade_subject, edge_type, weight, evidence_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?);
                        """,
                        (nsrc, ndst, grade_subject, etype, w_val, ev_json),
                    )

                conn.execute(
                    f"""
                    DELETE FROM knowledge_edges
                    WHERE grade_subject = ?
                      AND (
                          source_node_id IN (SELECT node_id FROM knowledge_nodes WHERE {old_node_where})
                          OR target_node_id IN (SELECT node_id FROM knowledge_nodes WHERE {old_node_where})
                      );
                    """,
                    (grade_subject, *old_node_params, *old_node_params),
                )

                # B5. compaction_audit_ledger 迁移（严格校验 1-to-1 重定向拓扑可逆不变量）
                def _new_root(nid: str) -> str:
                    nxt = new_redirect_target.get(nid)
                    return nxt if nxt is not None else nid

                ledger_rows = conn.execute(
                    """
                    SELECT merge_id, batch_id, scope_json, from_id, to_id, affected_child_ids_json
                    FROM compaction_audit_ledger
                    WHERE target_kind = 'KNOWLEDGE_NODE' AND is_reverted = 0
                    ORDER BY merge_id ASC;
                    """
                ).fetchall()

                scoped_entries: list[dict[str, Any]] = []
                for lr in ledger_rows:
                    try:
                        sc = json.loads(lr["scope_json"] or "{}")
                    except ValueError:
                        continue
                    if not isinstance(sc, dict):
                        continue
                    if sc.get("grade_subject") != grade_subject or sc.get("taxonomy_version") == target_version:
                        continue
                    old_from = str(lr["from_id"])
                    old_to = str(lr["to_id"])
                    try:
                        raw_ch = json.loads(lr["affected_child_ids_json"] or "[]")
                        old_children = [str(c) for c in raw_ch] if isinstance(raw_ch, list) else []
                    except ValueError:
                        old_children = []
                    old_cm = str(sc["created_master_id"]) if sc.get("created_master_id") else ""
                    old_set = {old_from, old_to, *old_children}
                    if old_cm:
                        old_set.add(old_cm)
                    new_set = {node_id_map[x] for x in old_set if x in node_id_map}

                    row_ok = (
                        all(x in node_id_map for x in old_set)
                        and all(len(reverse_l3_sources.get(node_id_map[x], ())) == 1 for x in old_set)
                        and node_id_map.get(old_from) != node_id_map.get(old_to)
                        and node_id_map.get(old_from, "") in created_redirect_new_ids
                        and all(node_id_map.get(c, "") in created_redirect_new_ids for c in old_children)
                        and _new_root(node_id_map.get(old_from, "")) == _new_root(node_id_map.get(old_to, ""))
                    )
                    scoped_entries.append(
                        {
                            "merge_id": int(lr["merge_id"]),
                            "batch_id": str(lr["batch_id"]),
                            "sc": sc,
                            "old_from": old_from,
                            "old_to": old_to,
                            "old_children": old_children,
                            "old_set": old_set,
                            "new_set": new_set,
                            "row_ok": row_ok,
                        }
                    )

                n_ent = len(scoped_entries)
                uf_parent = list(range(n_ent))

                def _uf_find(x: int) -> int:
                    while uf_parent[x] != x:
                        uf_parent[x] = uf_parent[uf_parent[x]]
                        x = uf_parent[x]
                    return x

                def _uf_union(a: int, b: int) -> None:
                    ra, rb = _uf_find(a), _uf_find(b)
                    if ra != rb:
                        uf_parent[rb] = ra

                by_batch_idx: dict[str, int] = {}
                by_old_node_idx: dict[str, int] = {}
                by_new_node_idx: dict[str, int] = {}
                for idx_e, ent in enumerate(scoped_entries):
                    bid = ent["batch_id"]
                    if bid in by_batch_idx:
                        _uf_union(idx_e, by_batch_idx[bid])
                    else:
                        by_batch_idx[bid] = idx_e
                    for onid in ent["old_set"]:
                        if onid in by_old_node_idx:
                            _uf_union(idx_e, by_old_node_idx[onid])
                        else:
                            by_old_node_idx[onid] = idx_e
                    for nnid in ent["new_set"]:
                        if nnid in by_new_node_idx:
                            _uf_union(idx_e, by_new_node_idx[nnid])
                        else:
                            by_new_node_idx[nnid] = idx_e

                comp_ok: dict[int, bool] = {}
                for idx_e, ent in enumerate(scoped_entries):
                    root_i = _uf_find(idx_e)
                    comp_ok[root_i] = comp_ok.get(root_i, True) and bool(ent["row_ok"])

                for idx_e, ent in enumerate(scoped_entries):
                    if comp_ok[_uf_find(idx_e)]:
                        new_from = node_id_map[ent["old_from"]]
                        new_to = node_id_map[ent["old_to"]]
                        new_children = list(
                            dict.fromkeys(
                                node_id_map[c]
                                for c in ent["old_children"]
                                if c in node_id_map and node_id_map[c] not in (new_from, new_to)
                            )
                        )
                        new_sc = dict(ent["sc"])
                        new_sc["taxonomy_version"] = target_version
                        if new_to in new_node_meta:
                            nl1, nl2, _ = new_node_meta[new_to]
                            new_sc["l1_module"] = nl1
                            new_sc["l2_entity"] = nl2
                            if "l1" in new_sc:
                                new_sc["l1"] = nl1
                            if "l2" in new_sc:
                                new_sc["l2"] = nl2
                        if (
                            "created_master_id" in new_sc
                            and str(new_sc["created_master_id"]) in node_id_map
                        ):
                            new_sc["created_master_id"] = node_id_map[str(new_sc["created_master_id"])]
                        conn.execute(
                            """
                            UPDATE compaction_audit_ledger
                            SET from_id = ?, to_id = ?, affected_child_ids_json = ?, scope_json = ?
                            WHERE merge_id = ?;
                            """,
                            (
                                new_from,
                                new_to,
                                json.dumps(new_children, ensure_ascii=False),
                                json.dumps(new_sc, ensure_ascii=False),
                                ent["merge_id"],
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE compaction_audit_ledger SET is_reverted = 1, reverted_at = datetime('now') WHERE merge_id = ?;",
                            (ent["merge_id"],),
                        )

                # Step C: 安全删除旧版本节点
                conn.execute(
                    f"DELETE FROM knowledge_nodes WHERE {old_node_where} AND redirect_to_id IS NOT NULL;",
                    old_node_params,
                )
                conn.execute(
                    f"DELETE FROM knowledge_nodes WHERE {old_node_where};",
                    old_node_params,
                )

                # Step D: 精确更新 grade_subjects 与 compile_state（刷新保留题 S3C/S3D input_hash）
                conn.execute(
                    """
                    UPDATE grade_subjects
                    SET taxonomy_status = ?, taxonomy_version = ?, taxonomy_file = ?
                    WHERE grade_subject = ?;
                    """,
                    (target_status, target_version, str(target_yaml_path), grade_subject),
                )

                for uid in stale_uids:
                    refresh.mark_stages_stale(conn, uid, ["S3C", "S3D", "S4"])

                def_s3c_pv, def_s3d_pv = _resolve_s3_prompt_versions(config_dir, grade_subject)
                ov_tax_by_q: dict[str, str] = {}
                for orow in conn.execute(
                    """
                    SELECT o.question_uid, o.override_value
                    FROM human_overrides o
                    JOIN questions q ON q.question_uid = o.question_uid
                    WHERE q.grade_subject = ? AND o.field_name = 'taxonomy_override_json'
                      AND o.is_active = 1 AND o.is_suspended = 0;
                    """,
                    (grade_subject,),
                ).fetchall():
                    ov_tax_by_q[str(orow["question_uid"])] = str(orow["override_value"])

                for pm in preserved_q_meta:
                    uid = pm["uid"]
                    q_steps = pm["q_steps"]
                    step_updates = pm["step_updates"]
                    if pm["s3c_status"] == "OK":
                        pv3c = pm["s3c_pv"] or def_s3c_pv
                        new_s3c_hash = _sha256_json(
                            {
                                "pv": pv3c,
                                "tv": target_version,
                                "prim": pm["new_prim"],
                                "raw_l1s": pm["raw_l1s"],
                                "steps": [
                                    (
                                        str(s["step_id"]),
                                        str(s["raw_l1"]),
                                        str(s["raw_l2"]),
                                        str(s["sub_goal"]),
                                    )
                                    for s in q_steps
                                ],
                            }
                        )
                        conn.execute(
                            """
                            UPDATE compile_state
                            SET input_hash = ?, prompt_version = ?
                            WHERE question_uid = ? AND stage = 'S3C';
                            """,
                            (new_s3c_hash, pv3c, uid),
                        )
                    if pm["s3d_status"] == "OK":
                        pv3d = pm["s3d_pv"] or def_s3d_pv
                        new_s3d_hash = _sha256_json(
                            {
                                "pv": pv3d,
                                "tv": target_version,
                                "steps": [
                                    (
                                        str(s["step_id"]),
                                        str(upd[0]),
                                        str(upd[1]),
                                        str(s["raw_l3"]),
                                    )
                                    for s, upd in zip(q_steps, step_updates, strict=True)
                                ],
                                "ov": ov_tax_by_q.get(uid, ""),
                            }
                        )
                        conn.execute(
                            """
                            UPDATE compile_state
                            SET input_hash = ?, prompt_version = ?
                            WHERE question_uid = ? AND stage = 'S3D';
                            """,
                            (new_s3d_hash, pv3d, uid),
                        )

                stale_count_out = len(stale_uids)
                preserved_count_out = len(preserved_q_meta)

            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        # 事务提交成功后才原子写盘 YAML，杜绝回滚撕裂磁盘文件
        save_taxonomy_spec(updated_spec, target_yaml_path)

        # 事务提交后通过 effective.recompute 刷新生效列并导出 human/ 镜像
        effective.recompute_all(conn, grade_subject=grade_subject)
        conn.commit()
        human_mirror.export_snapshot(conn, human_dir=human_dir)

    migrated_l3_vectors = 0
    if use_smart_promote:
        migrated_l3_vectors = _migrate_l3_vectors_npz(
            conn,
            vectors_dir=vectors_dir,
            grade_subject=grade_subject,
            slug=slug,
            target_version=target_version,
            node_id_map=node_id_map,
        )

    return {
        "dry_run": False,
        "grade_subject": grade_subject,
        "from_version": old_version,
        "target_status": target_status,
        "target_version": target_version,
        "promotion_mode": "smart_preserve" if use_smart_promote else "force_recompile",
        "preserved_l2_pairs": len(mapped_l2_pairs) if use_smart_promote else 0,
        "dropped_l2_pairs": len(dropped_l2_pairs) if use_smart_promote else 0,
        "taxonomy_file": str(target_yaml_path),
        "backup_path": str(backup_path),
        "migrated_overrides": len(migrated),
        "suspended_overrides": len(unmapped),
        "migrated_practice_logs": migrated_practice_logs,
        "nulled_practice_logs": nulled_practice_logs,
        "preserved_questions": preserved_count_out,
        "stale_questions": stale_count_out,
        "migrated_l3_vectors": migrated_l3_vectors,
    }


def get_taxonomy_maintenance_status(conn: sqlite3.Connection, grade_subject: str) -> dict[str, Any]:
    """检测学段是否处于词表维护降级期（LOCKED/DRAFT 且存在活跃 S2=OK 但 S3C/S3D 为 STALE/PENDING 题目）。"""
    gs_row = conn.execute(
        "SELECT taxonomy_status, taxonomy_version FROM grade_subjects WHERE grade_subject = ?;",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        return {
            "grade_subject": grade_subject,
            "taxonomy_status": "NONE",
            "taxonomy_version": "",
            "degraded": False,
            "code": "",
            "reason": "",
            "stale_s3c_count": 0,
            "stale_s3d_count": 0,
            "message": "",
        }

    tax_status = str(gs_row["taxonomy_status"] or "NONE")
    tax_version = str(gs_row["taxonomy_version"] or "")
    stale_s3c_count = 0
    stale_s3d_count = 0

    if tax_status in ("LOCKED", "DRAFT"):
        counts = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN cs3c.status IN ('STALE', 'PENDING') THEN 1 ELSE 0 END), 0) AS s3c_cnt,
                COALESCE(SUM(CASE WHEN cs3d.status IN ('STALE', 'PENDING') THEN 1 ELSE 0 END), 0) AS s3d_cnt
            FROM questions q
            JOIN compile_state cs2
              ON cs2.question_uid = q.question_uid AND cs2.stage = 'S2' AND cs2.status = 'OK'
            LEFT JOIN compile_state cs3c
              ON cs3c.question_uid = q.question_uid AND cs3c.stage = 'S3C'
            LEFT JOIN compile_state cs3d
              ON cs3d.question_uid = q.question_uid AND cs3d.stage = 'S3D'
            WHERE q.grade_subject = ?
              AND q.is_retired = 0
              AND q.eff_is_excluded = 0;
            """,
            (grade_subject,),
        ).fetchone()
        if counts is not None:
            stale_s3c_count = int(counts["s3c_cnt"] or 0)
            stale_s3d_count = int(counts["s3d_cnt"] or 0)

    degraded = tax_status in ("LOCKED", "DRAFT") and (stale_s3c_count > 0 or stale_s3d_count > 0)
    message = (
        f"MAINTENANCE_DEGRADED: 学段 {grade_subject} 处于词表重编维护降级期"
        f"（S3C 待重编 {stale_s3c_count} 题，S3D 待重编 {stale_s3d_count} 题），"
        "请完成 Stage 3c/3d 重编以恢复完整知识树与节点检索。"
        if degraded
        else ""
    )
    return {
        "grade_subject": grade_subject,
        "taxonomy_status": tax_status,
        "taxonomy_version": tax_version,
        "degraded": degraded,
        "code": "MAINTENANCE_DEGRADED" if degraded else "",
        "reason": "MAINTENANCE_DEGRADED" if degraded else "",
        "stale_s3c_count": stale_s3c_count,
        "stale_s3d_count": stale_s3d_count,
        "message": message,
    }


def migrate_taxonomy_incremental(
    conn: sqlite3.Connection,
    *,
    grade_subject: str,
    new_yaml_path: Path | str,
    dry_run: bool = False,
    config_dir: Path | str = Path("config"),
    backups_dir: Path | str = Path("data/backups"),
    human_dir: Path | str = Path("human"),
    lock_path: Path | str = Path("data/.compile.lock"),
) -> dict[str, Any]:
    """按 LOCKED YAML 中的 changelog（add/rename/merge）精准增量迁移（如 1.0 → 1.1）。"""
    gs_row = conn.execute(
        "SELECT slug, taxonomy_status, taxonomy_version FROM grade_subjects WHERE grade_subject = ?",
        (grade_subject,),
    ).fetchone()
    if gs_row is None:
        raise PreconditionError(f"PRECONDITION_FAILED: 学段学科不存在: {grade_subject!r}")
    if gs_row["taxonomy_status"] != "LOCKED":
        raise PreconditionError("PRECONDITION_FAILED: 仅 LOCKED 状态支持 changelog 增量迁移，DRAFT 请直接修订并锁表。")

    slug = str(gs_row["slug"])
    cur_ver = str(gs_row["taxonomy_version"])
    spec = load_taxonomy_spec(new_yaml_path)
    if spec.grade_subject != grade_subject:
        raise PreconditionError("PRECONDITION_FAILED: YAML grade_subject 不匹配")
    target_ver = spec.version
    if target_ver == cur_ver:
        raise PreconditionError(f"PRECONDITION_FAILED: 新版本号 {target_ver!r} 与当前版本相同")

    actions: list[TaxonomyChangeAction] = []
    for entry in spec.changelog:
        if entry.from_version == cur_ver or (entry.from_version > cur_ver and entry.to_version <= target_ver):
            actions.extend(entry.actions)

    l1_map: dict[str, str] = {}
    l2_map: dict[tuple[str, str], tuple[str, str]] = {}
    for act in actions:
        dst = act.new_name if act.op == "rename" else act.merge_into
        if act.op in ("rename", "merge") and dst and act.target:
            if act.level == "L1":
                l1_map[act.target] = dst
            elif act.level == "L2" and act.parent_l1:
                l2_map[(act.parent_l1, act.target)] = (act.parent_l1, dst)

    if dry_run:
        return {
            "dry_run": True,
            "grade_subject": grade_subject,
            "from_version": cur_ver,
            "to_version": target_ver,
            "actions_count": len(actions),
            "l1_rewrites": l1_map,
            "l2_rewrites": {f"{k[0]}::{k[1]}": f"{v[0]}::{v[1]}" for k, v in l2_map.items()},
        }

    backup_path = human_mirror.vacuum_backup(conn, backups_dir, "taxonomy_migrate")
    affected_uids: set[str] = set()
    lookup = build_lookup_index(spec)

    with CompileLock(lock_path):
        conn.execute("BEGIN IMMEDIATE;")
        try:
            # 迁移受影响步骤并收集受影响 question_uid
            for old_l1, new_l1 in l1_map.items():
                rows = conn.execute(
                    """
                    SELECT DISTINCT s.question_uid
                    FROM question_steps s
                    JOIN questions q ON q.question_uid = s.question_uid
                    WHERE q.grade_subject = ? AND s.m_l1 = ?;
                    """,
                    (grade_subject, old_l1),
                ).fetchall()
                affected_uids.update(str(r["question_uid"]) for r in rows)
                conn.execute(
                    """
                    UPDATE question_steps SET m_l1 = ?
                    WHERE question_uid IN (SELECT question_uid FROM questions WHERE grade_subject = ?)
                      AND m_l1 = ?;
                    """,
                    (new_l1, grade_subject, old_l1),
                )
                q_prim = conn.execute(
                    "SELECT question_uid, m_secondary_l1_modules_json FROM questions WHERE grade_subject = ?;",
                    (grade_subject,),
                ).fetchall()
                for qr in q_prim:
                    sec = json.loads(qr["m_secondary_l1_modules_json"] or "[]")
                    changed = False
                    new_sec = []
                    for s_l1 in sec:
                        nv = l1_map.get(s_l1, s_l1)
                        if nv != s_l1:
                            changed = True
                        if nv not in new_sec:
                            new_sec.append(nv)
                    if changed:
                        affected_uids.add(str(qr["question_uid"]))
                        conn.execute(
                            "UPDATE questions SET m_secondary_l1_modules_json = ? WHERE question_uid = ?;",
                            (json.dumps(new_sec, ensure_ascii=False), qr["question_uid"]),
                        )
                conn.execute(
                    "UPDATE questions SET m_primary_l1_module = ? WHERE grade_subject = ? AND m_primary_l1_module = ?;",
                    (new_l1, grade_subject, old_l1),
                )

            for (p_l1, old_l2), (new_p_l1, new_l2) in l2_map.items():
                rows = conn.execute(
                    """
                    SELECT DISTINCT s.question_uid
                    FROM question_steps s
                    JOIN questions q ON q.question_uid = s.question_uid
                    WHERE q.grade_subject = ? AND s.m_l1 = ? AND s.m_l2 = ?;
                    """,
                    (grade_subject, p_l1, old_l2),
                ).fetchall()
                affected_uids.update(str(r["question_uid"]) for r in rows)
                conn.execute(
                    """
                    UPDATE question_steps SET m_l1 = ?, m_l2 = ?, m_knowledge_node_id = NULL
                    WHERE question_uid IN (SELECT question_uid FROM questions WHERE grade_subject = ?)
                      AND m_l1 = ? AND m_l2 = ?;
                    """,
                    (new_p_l1, new_l2, grade_subject, p_l1, old_l2),
                )

            # 更新知识节点表版本号并补齐新增 L2 节点
            conn.execute(
                "UPDATE knowledge_nodes SET taxonomy_version = ? WHERE grade_subject = ? AND taxonomy_version = ?;",
                (target_ver, grade_subject, cur_ver),
            )
            for (p_l1, old_l2), (new_p_l1, new_l2) in l2_map.items():
                # 合并/重命名旧 L3 到新 L2 下，避免与已有 L3 唯一索引冲突
                old_l3s = conn.execute(
                    """
                    SELECT node_id, l3_operator FROM knowledge_nodes
                    WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3'
                      AND l1_module = ? AND l2_entity = ?;
                    """,
                    (grade_subject, target_ver, p_l1, old_l2),
                ).fetchall()
                for r in old_l3s:
                    existing = conn.execute(
                        """
                        SELECT node_id FROM knowledge_nodes
                        WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L3'
                          AND l1_module = ? AND l2_entity = ? AND l3_operator = ? AND redirect_to_id IS NULL;
                        """,
                        (grade_subject, target_ver, new_p_l1, new_l2, r["l3_operator"]),
                    ).fetchone()
                    if existing and existing["node_id"] != r["node_id"]:
                        conn.execute(
                            "UPDATE question_steps SET m_knowledge_node_id = ? WHERE m_knowledge_node_id = ?;",
                            (existing["node_id"], r["node_id"]),
                        )
                        conn.execute(
                            "UPDATE knowledge_nodes SET redirect_to_id = ? WHERE node_id = ?;",
                            (existing["node_id"], r["node_id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE knowledge_nodes SET l1_module = ?, l2_entity = ? WHERE node_id = ?;",
                            (new_p_l1, new_l2, r["node_id"]),
                        )
                # 清理旧 L2 汇总行
                conn.execute(
                    """
                    DELETE FROM knowledge_nodes
                    WHERE grade_subject = ? AND taxonomy_version = ? AND node_level = 'L2'
                      AND l1_module = ? AND l2_entity = ?;
                    """,
                    (grade_subject, target_ver, p_l1, old_l2),
                )

            for mod in spec.modules:
                for l2 in mod.l2_entities:
                    l2_id = make_node_id(slug, target_ver, "L2", mod.name, l2.name, "")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_nodes(
                            node_id, grade_subject, taxonomy_version, node_level,
                            l1_module, l2_entity, l3_operator, aliases_json, redirect_to_id
                        )
                        VALUES (?, ?, ?, 'L2', ?, ?, '', ?, NULL);
                        """,
                        (l2_id, grade_subject, target_ver, mod.name, l2.name, json.dumps(l2.aliases, ensure_ascii=False)),
                    )

            # 迁移受影响 human_overrides
            ov_rows = conn.execute(
                """
                SELECT o.override_id, o.question_uid, o.field_name, o.override_value
                FROM human_overrides o
                JOIN questions q ON q.question_uid = o.question_uid
                WHERE q.grade_subject = ? AND o.is_active = 1 AND o.is_suspended = 0
                  AND o.field_name IN ('primary_l1_module', 'secondary_l1_modules_json', 'taxonomy_override_json');
                """,
                (grade_subject,),
            ).fetchall()
            l1_ren = {normalize_label(k): v for k, v in l1_map.items()}
            l2_ren = {(normalize_label(k[0]), normalize_label(k[1])): v for k, v in l2_map.items()}
            for r in ov_rows:
                ok, new_val, _ = _migrate_override_value(
                    str(r["field_name"]), str(r["override_value"]), lookup, l1_ren, l2_ren, target_ver
                )
                if ok:
                    conn.execute(
                        "UPDATE human_overrides SET override_value = ?, taxonomy_version = ? WHERE override_id = ?;",
                        (new_val, target_ver, r["override_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE human_overrides SET is_suspended = 1, suspended_reason = 'TAXONOMY_UNMAPPED' WHERE override_id = ?;",
                        (r["override_id"],),
                    )
                affected_uids.add(str(r["question_uid"]))

            target_yaml = default_taxonomy_path(config_dir, slug, "LOCKED")
            locked_spec = spec.model_copy(update={"status": "LOCKED"})
            conn.execute(
                "UPDATE grade_subjects SET taxonomy_version = ?, taxonomy_file = ? WHERE grade_subject = ?;",
                (target_ver, str(target_yaml), grade_subject),
            )

            for uid in affected_uids:
                refresh.mark_stages_stale(conn, uid, ["S3D", "S4"])

            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        save_taxonomy_spec(locked_spec, target_yaml)

        for uid in affected_uids:
            effective.recompute(conn, uid)
        if affected_uids:
            conn.commit()
        human_mirror.export_snapshot(conn, human_dir=human_dir)

    return {
        "dry_run": False,
        "grade_subject": grade_subject,
        "from_version": cur_ver,
        "to_version": target_ver,
        "backup_path": str(backup_path),
        "affected_questions": len(affected_uids),
    }
