"""Stage 0：数据入库体检与清洗引擎（DESIGN.md §4.2 与 §7 M0）。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from shuati.core import effective, refresh
from shuati.core.image_store import (
    archive_image,
    compute_dhash,
    hamming_distance,
    validate_image,
)
from shuati.core.text_norm import (
    compute_content_hash,
    compute_plain_fingerprint,
    detect_and_extract_choice_options,
    normalize_text,
)
from shuati.db.connection import (
    CompileLock,
    PreconditionError,
    SingleWriterExecutor,
    get_connection,
)
from shuati.db.schema import init_db


@dataclass
class Stage0IngestReport:
    scan_id: str
    timestamp: str
    total_scanned: int = 0
    newly_ingested: list[str] = field(default_factory=list)
    skipped_unchanged: list[str] = field(default_factory=list)
    updated_source_changed: list[str] = field(default_factory=list)
    exact_duplicates: list[dict[str, str]] = field(default_factory=list)
    near_duplicate_candidates: list[dict[str, Any]] = field(default_factory=list)
    merged_near_duplicates: list[dict[str, str]] = field(default_factory=list)
    rejected_questions: list[dict[str, str]] = field(default_factory=list)
    missing_from_source: list[str] = field(default_factory=list)
    retired_questions: list[str] = field(default_factory=list)
    unmapped_collections: list[str] = field(default_factory=list)
    image_warnings: list[dict[str, Any]] = field(default_factory=list)
    report_json_path: str = ""
    report_md_path: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _posix_glob_to_regex(pattern: str) -> str:
    pat = pattern.replace("\\", "/").strip("/")
    i = 0
    n = len(pat)
    res: list[str] = ["^"]
    while i < n:
        if pat[i : i + 4] == "/**/":
            res.append("(?:/.+)?/")
            i += 4
        elif pat[i : i + 3] == "/**":
            if i + 3 == n:
                res.append("/.+")
                i += 3
            else:
                res.append("/.*")
                i += 3
        elif pat[i : i + 3] == "**/":
            res.append("(?:.+/)?")
            i += 3
        elif pat[i : i + 2] == "**":
            res.append(".*")
            i += 2
        elif pat[i] == "*":
            res.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            res.append("[^/]")
            i += 1
        else:
            res.append(re.escape(pat[i]))
            i += 1
    res.append("$")
    return "".join(res)


def _posix_glob_match(path_posix: str, pattern: str) -> bool:
    regex_str = _posix_glob_to_regex(pattern)
    path_clean = path_posix.replace("\\", "/").strip("/")
    return bool(re.match(regex_str, path_clean))


def _match_collection_rule(collection: str, rules: list[dict[str, str]]) -> str | None:
    """按顺序匹配 collections.yaml 中的 glob 规则。"""
    c_posix = collection.replace("\\", "/").strip("/")
    for rule in rules:
        pattern = rule.get("match", "")
        gs = rule.get("grade_subject", "")
        if pattern and _posix_glob_match(c_posix, pattern):
            return gs
    return None


def _promote_orphan(
    conn: sqlite3.Connection,
    orphan_row: sqlite3.Row | dict[str, Any],
    grade_subject: str,
) -> None:
    """将孤儿副本晋升为独立主题记录并补建 questions/compile_state 并重算。"""
    orphan_uid = orphan_row["question_uid"]
    conn.execute(
        "UPDATE raw_questions SET duplicate_of_uid = NULL, updated_at = datetime('now') WHERE question_uid = ?",
        (orphan_uid,),
    )
    o_raw_q = orphan_row["raw_question"]
    o_cas_imgs = json.loads(orphan_row["cas_images_json"] or "[]")
    o_captions = json.loads(orphan_row["image_captions_json"] or "[]")
    o_tags = json.loads(orphan_row["legacy_tags_json"] or "{}")

    o_norm_res = normalize_text(o_raw_q, num_images=len(o_cas_imgs))
    o_norm_q = o_norm_res.normalized_text
    if not o_captions:
        o_captions = o_norm_res.image_captions
    o_is_choice, o_opt_dict = detect_and_extract_choice_options(o_norm_q)
    o_raw_fmt = o_tags.get("format", "解答题") if isinstance(o_tags, dict) else "解答题"
    if o_raw_fmt == "操作构造题":
        o_raw_fmt = "作图操作题"

    if o_is_choice:
        o_fmt = "选择题"
        o_opts_j = json.dumps(o_opt_dict, ensure_ascii=False)
    else:
        if o_raw_fmt in (
            "选择题",
            "填空题",
            "判断题",
            "计算题",
            "解答题",
            "证明题",
            "作图操作题",
            "实验探究题",
        ):
            o_fmt = o_raw_fmt
        else:
            o_fmt = "解答题"
        o_opts_j = "{}"

    o_has_imgs = 1 if o_cas_imgs else 0

    conn.execute(
        """
        INSERT INTO questions(
            question_uid, grade_subject, cas_images_json,
            image_captions_json, has_images, is_retired,
            m_question_text, m_options_json, m_format
        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT(question_uid) DO UPDATE SET
            cas_images_json = excluded.cas_images_json,
            image_captions_json = excluded.image_captions_json,
            has_images = excluded.has_images,
            m_question_text = excluded.m_question_text,
            m_options_json = excluded.m_options_json,
            m_format = excluded.m_format,
            is_retired = CASE WHEN questions.retired_reason IN ('DUPLICATE', 'MERGED_NEAR_DUPLICATE', 'MISSING_FROM_SOURCE') THEN 0 ELSE questions.is_retired END,
            retired_reason = CASE WHEN questions.retired_reason IN ('DUPLICATE', 'MERGED_NEAR_DUPLICATE', 'MISSING_FROM_SOURCE') THEN '' ELSE questions.retired_reason END,
            updated_at = datetime('now')
        """,
        (
            orphan_uid,
            grade_subject,
            orphan_row["cas_images_json"],
            json.dumps(o_captions, ensure_ascii=False),
            o_has_imgs,
            o_norm_q,
            o_opts_j,
            o_fmt,
        ),
    )
    for stg in ("S1", "S2", "S3C", "S3D", "S4"):
        conn.execute(
            """
            INSERT INTO compile_state(question_uid, stage, status, updated_at)
            VALUES (?, ?, 'PENDING', datetime('now'))
            ON CONFLICT(question_uid, stage) DO UPDATE SET
                status = 'PENDING',
                updated_at = datetime('now')
            """,
            (orphan_uid, stg),
        )
    effective.recompute(conn, orphan_uid)


def _question_has_refs(conn: sqlite3.Connection, question_uid: str) -> bool:
    """检查题目是否被人工覆写、关系覆写、做题记录等引用（防止 FK RESTRICT 报错并守护人工数据不变量）。"""
    row = conn.execute(
        """
        SELECT 1 FROM human_overrides WHERE question_uid = ?
        UNION ALL
        SELECT 1 FROM relation_overrides WHERE source_question_uid = ? OR target_question_uid = ?
        UNION ALL
        SELECT 1 FROM practice_log WHERE question_uid = ?
        LIMIT 1
        """,
        (question_uid, question_uid, question_uid, question_uid),
    ).fetchone()
    return row is not None


def _compute_cas_filename_readonly(src_path: Path) -> str:
    h16 = hashlib.sha256(src_path.read_bytes()).hexdigest()[:16]
    suffix = src_path.suffix.lower() or ".jpg"
    return f"{h16}{suffix}"


def _run_stage0_dry_run(
    *,
    db_p: Path,
    assets_path: Path,
    mapped_files: list[tuple[Path, str, str]],
    dedup_near: bool,
    apply_retire: bool,
    report: Stage0IngestReport,
) -> Stage0IngestReport:
    """只读预览 Stage 0 入库变更：零写库、不持写锁、不拷贝 CAS 图片、不写磁盘报告。"""
    conn: sqlite3.Connection | None = None
    has_tables = False
    if db_p.exists():
        conn = get_connection(db_p, readonly=True)
        has_tables = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grade_subjects'"
            ).fetchone()
            is not None
        )

    try:
        slug_map: dict[str, str] = {}
        for _, _, gs in mapped_files:
            if gs in slug_map:
                continue
            if conn is not None and has_tables:
                row = conn.execute(
                    "SELECT slug FROM grade_subjects WHERE grade_subject = ?", (gs,)
                ).fetchone()
                if not row:
                    raise PreconditionError(
                        f"学段学科 '{gs}' 未在数据库中注册，请先使用 'shuati grade-subject add' 注册"
                    )
                slug_map[gs] = str(row["slug"])
            else:
                slug_map[gs] = "primary_olympiad" if gs == "小学奥数" else re.sub(r"[^A-Za-z0-9_]+", "_", gs).strip("_") or "default"

        existing_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        db_by_hash: dict[tuple[str, str], str] = {}
        db_by_plain: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
        existing_in_col: dict[tuple[str, str], set[str]] = {}

        if conn is not None and has_tables:
            for r in conn.execute(
                """
                SELECT question_uid, grade_subject, source_collection, source_id,
                       content_hash, plain_fingerprint, cas_images_json,
                       duplicate_of_uid, is_missing_from_source
                FROM raw_questions
                """
            ).fetchall():
                gs = str(r["grade_subject"])
                col = str(r["source_collection"])
                sid = str(r["source_id"])
                uid = str(r["question_uid"])
                chash = str(r["content_hash"])
                pfp = str(r["plain_fingerprint"] or "")
                cas = list(json.loads(r["cas_images_json"] or "[]"))
                existing_by_key[(gs, col, sid)] = {
                    "question_uid": uid,
                    "content_hash": chash,
                    "duplicate_of_uid": r["duplicate_of_uid"],
                }
                existing_in_col.setdefault((gs, col), set()).add(uid)
                if r["is_missing_from_source"] == 0:
                    master = str(r["duplicate_of_uid"] or uid)
                    db_by_hash.setdefault((gs, chash), master)
                    if r["duplicate_of_uid"] is None:
                        db_by_plain.setdefault((gs, pfp), []).append((uid, cas))

        batch_by_hash: dict[tuple[str, str], str] = {}
        batch_by_plain: dict[tuple[str, str], list[tuple[str, list[int]]]] = {}
        present_uids_by_col: dict[tuple[str, str], set[str]] = {}

        for pf, source_collection, grade_subject in mapped_files:
            slug = slug_map[grade_subject]
            col_key = (grade_subject, source_collection)
            present_uids_by_col.setdefault(col_key, set())

            with open(pf, encoding="utf-8") as f:
                for line_num, line in enumerate(f, start=1):
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        item = json.loads(line_str)
                    except Exception as e:
                        report.rejected_questions.append(
                            {"file": str(pf), "line": str(line_num), "error": f"JSON_PARSE_ERROR: {e}"}
                        )
                        continue
                    if not isinstance(item, dict):
                        report.rejected_questions.append(
                            {
                                "file": str(pf),
                                "line": str(line_num),
                                "error": f"MALFORMED_ROW: 行内容不是 JSON 对象 (dict), 而是 {type(item).__name__}",
                            }
                        )
                        continue
                    source_id_raw = item.get("id")
                    if source_id_raw is None or not str(source_id_raw).strip():
                        report.rejected_questions.append(
                            {"file": str(pf), "line": str(line_num), "error": "MISSING_ID: 题目缺少有效 id 字段"}
                        )
                        continue
                    source_id = str(source_id_raw).strip()
                    question_uid = f"{slug}:{source_collection}:{source_id}"
                    present_uids_by_col[col_key].add(question_uid)

                    raw_question_raw = item.get("question")
                    if not isinstance(raw_question_raw, str) or not raw_question_raw.strip():
                        report.rejected_questions.append(
                            {
                                "file": str(pf),
                                "line": str(line_num),
                                "question_id": source_id,
                                "error": "INVALID_QUESTION: 题干 question 缺失、为 null 或非字符串",
                            }
                        )
                        continue
                    raw_images_raw = item.get("images", [])
                    if not isinstance(raw_images_raw, list) or not all(
                        isinstance(img, str) for img in raw_images_raw
                    ):
                        report.rejected_questions.append(
                            {
                                "file": str(pf),
                                "line": str(line_num),
                                "question_id": source_id,
                                "error": "INVALID_IMAGES: images 字段必须为字符串列表",
                            }
                        )
                        continue

                    report.total_scanned += 1
                    cas_images: list[str] = []
                    dhashes: list[int] = []
                    image_corrupted = False
                    for img_rel in raw_images_raw:
                        img_src = pf.parent / img_rel
                        val_res = validate_image(img_src)
                        if not val_res.is_valid:
                            image_corrupted = True
                            report.rejected_questions.append(
                                {"question_uid": question_uid, "image": str(img_src), "error": val_res.error}
                            )
                            break
                        for w in val_res.warnings:
                            report.image_warnings.append({"question_uid": question_uid, "warning": w})
                        cas_images.append(_compute_cas_filename_readonly(img_src))
                        try:
                            dhashes.append(compute_dhash(img_src))
                        except Exception:
                            pass

                    if image_corrupted:
                        continue

                    norm_res = normalize_text(raw_question_raw, num_images=len(cas_images))
                    m_question_text = norm_res.normalized_text
                    image_captions = norm_res.image_captions
                    content_hash = compute_content_hash(m_question_text, cas_images, image_captions)
                    plain_fingerprint = compute_plain_fingerprint(m_question_text, len(cas_images))

                    existing = existing_by_key.get((grade_subject, source_collection, source_id))
                    if existing is not None:
                        if existing["content_hash"] == content_hash:
                            report.skipped_unchanged.append(existing["question_uid"])
                        else:
                            report.updated_source_changed.append(existing["question_uid"])
                        continue

                    dup_master = db_by_hash.get((grade_subject, content_hash)) or batch_by_hash.get(
                        (grade_subject, content_hash)
                    )
                    if dup_master:
                        report.exact_duplicates.append(
                            {"question_uid": question_uid, "duplicate_of_uid": dup_master}
                        )
                        report.newly_ingested.append(question_uid)
                        continue

                    near_dup_target: str | None = None
                    for cand_uid, cand_cas in db_by_plain.get((grade_subject, plain_fingerprint), []):
                        if len(cas_images) == 0 and len(cand_cas) == 0:
                            near_dup_target = cand_uid
                            report.near_duplicate_candidates.append(
                                {"question_uid": question_uid, "candidate_uid": cand_uid, "distance": 0}
                            )
                            break
                        if len(cas_images) > 0 and len(cas_images) == len(cand_cas):
                            cand_dhashes: list[int] = []
                            for ci in cand_cas:
                                try:
                                    cand_dhashes.append(compute_dhash(assets_path / ci))
                                except Exception:
                                    pass
                            if len(cand_dhashes) == len(dhashes) and dhashes:
                                max_d = max(hamming_distance(d1, d2) for d1, d2 in zip(dhashes, cand_dhashes))
                                if max_d <= 4:
                                    near_dup_target = cand_uid
                                    report.near_duplicate_candidates.append(
                                        {"question_uid": question_uid, "candidate_uid": cand_uid, "distance": max_d}
                                    )
                                    break

                    if near_dup_target is None:
                        for cand_uid, cand_dhashes in batch_by_plain.get((grade_subject, plain_fingerprint), []):
                            if len(dhashes) == 0 and len(cand_dhashes) == 0:
                                near_dup_target = cand_uid
                                report.near_duplicate_candidates.append(
                                    {"question_uid": question_uid, "candidate_uid": cand_uid, "distance": 0}
                                )
                                break
                            if len(dhashes) > 0 and len(dhashes) == len(cand_dhashes):
                                max_d = max(hamming_distance(d1, d2) for d1, d2 in zip(dhashes, cand_dhashes))
                                if max_d <= 4:
                                    near_dup_target = cand_uid
                                    report.near_duplicate_candidates.append(
                                        {"question_uid": question_uid, "candidate_uid": cand_uid, "distance": max_d}
                                    )
                                    break

                    if near_dup_target and dedup_near:
                        report.merged_near_duplicates.append(
                            {"question_uid": question_uid, "duplicate_of_uid": near_dup_target}
                        )
                        report.newly_ingested.append(question_uid)
                        continue

                    batch_by_hash[(grade_subject, content_hash)] = question_uid
                    batch_by_plain.setdefault((grade_subject, plain_fingerprint), []).append((question_uid, dhashes))
                    report.newly_ingested.append(question_uid)

        for col_key, present_uids in present_uids_by_col.items():
            for existing_uid in sorted(existing_in_col.get(col_key, set())):
                if existing_uid not in present_uids:
                    report.missing_from_source.append(existing_uid)
                    if apply_retire:
                        report.retired_questions.append(existing_uid)

        return report
    finally:
        if conn is not None:
            conn.close()


def run_stage0_ingest(
    db_path: Path | str = "data/shuati.db",
    root_dir: Path | str = "题库",
    config_dir: Path | str = "config",
    assets_dir: Path | str | None = None,
    reports_dir: Path | str | None = None,
    grade_subject_override: str | None = None,
    dedup_near: bool = False,
    apply_retire: bool = False,
    lock_path: Path | str | None = None,
    dry_run: bool = False,
) -> Stage0IngestReport:
    """执行 Stage 0 数据入库、校验与体检。

    1. Path.rglob("problems.jsonl") 递归发现；
    2. collections.yaml 映射 grade_subject（未匹配拒入，--grade-subject 显式覆盖仅限单集合）；
    3. 校验学段注册（未注册抛 PreconditionError）；
    4. normalize_text 规范化与多图题注绑定；
    5. 行首锚定选择题正则纠偏与选项提取；
    6. CAS 保序归档与 EXIF 校验；
    7. 幂等更新、content_hash 精确去重、plain_fingerprint + dHash 重扫预检；
    8. 源题消失检测与 --apply-retire；
    9. 输出体检报告。
    """
    root_path = Path(root_dir)
    cfg_dir = Path(config_dir)
    db_p = Path(db_path)

    if assets_dir is None:
        if db_p.name == "shuati.db" and db_p.parent.name == "data":
            assets_path = Path("data/assets/images")
        else:
            assets_path = db_p.parent / "assets" / "images"
    else:
        assets_path = Path(assets_dir)

    if reports_dir is None:
        if db_p.name == "shuati.db" and db_p.parent.name == "data":
            reports_path = Path("data/reports")
        else:
            reports_path = db_p.parent / "reports"
    else:
        reports_path = Path(reports_dir)

    if not dry_run:
        reports_path.mkdir(parents=True, exist_ok=True)
        assets_path.mkdir(parents=True, exist_ok=True)

    # 生成高精度且带随机因子的唯一 scan_id 与时间戳（防冻结时钟碰撞）
    now_ns = time.time_ns()
    rand_suffix = uuid.uuid4().hex[:6]
    try:
        base_ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    except Exception:
        base_ts = "20260101_000000"
    scan_ts = f"{base_ts}_{now_ns % 1_000_000:06d}_{rand_suffix}"
    scan_id = f"scan_{scan_ts}"
    report = Stage0IngestReport(scan_id=scan_id, timestamp=scan_ts, dry_run=dry_run)

    # 1. 递归发现 problems.jsonl
    if root_path.is_file():
        problems_files = [root_path]
    else:
        if not root_path.exists():
            raise PreconditionError(f"题库根目录 '{root_path}' 不存在")
        problems_files = sorted(
            list(root_path.rglob("problems.jsonl")),
            key=lambda p: str(p).replace("\\", "/"),
        )

    if not problems_files:
        raise PreconditionError(f"未在 '{root_path}' 下发现任何 problems.jsonl 文件")

    # 2. 读取 collections.yaml
    collections_yaml = cfg_dir / "collections.yaml"
    rules: list[dict[str, str]] = []
    if collections_yaml.exists():
        try:
            data = yaml.safe_load(collections_yaml.read_text(encoding="utf-8"))
            if data and "rules" in data:
                rules = data["rules"]
        except Exception:
            pass

    # 计算每个文件的 source_collection
    file_collections: list[tuple[Path, str]] = []
    for pf in problems_files:
        try:
            rel = pf.parent.relative_to(root_path).as_posix()
            if rel != ".":
                source_col = rel
            else:
                col_found = None
                curr_p = pf.parent.resolve()
                for ancestor in [curr_p, *curr_p.parents]:
                    if ancestor.name == "题库":
                        col_found = curr_p.relative_to(ancestor).as_posix()
                        break
                source_col = col_found if col_found and col_found != "." else pf.parent.name
        except Exception:
            source_col = pf.parent.name
        file_collections.append((pf, source_col))

    unique_collections = sorted(list({sc for _, sc in file_collections}))

    # 处理 --grade-subject 显式覆盖约束：仅限单集合导入
    if grade_subject_override:
        if len(unique_collections) > 1:
            raise PreconditionError(
                f"--grade-subject 显式覆盖仅限单集合导入，当前发现 {len(unique_collections)} 个集合: {unique_collections}"
            )

    # 建立映射
    mapped_files: list[tuple[Path, str, str]] = []  # (file_path, source_collection, grade_subject)
    for pf, sc in file_collections:
        gs_match: str | None
        if grade_subject_override:
            gs_match = grade_subject_override
        else:
            gs_match = _match_collection_rule(sc, rules)

        if not gs_match:
            if sc not in report.unmapped_collections:
                report.unmapped_collections.append(sc)
            continue
        mapped_files.append((pf, sc, gs_match))

    if dry_run:
        return _run_stage0_dry_run(
            db_p=db_p,
            assets_path=assets_path,
            mapped_files=mapped_files,
            dedup_near=dedup_near,
            apply_retire=apply_retire,
            report=report,
        )

    # 3. 校验学段注册与获取连接
    actual_lock_path = Path(lock_path) if lock_path else db_p.parent / ".compile.lock"
    compile_lock = CompileLock(actual_lock_path)

    with compile_lock, closing(get_connection(db_path)) as conn:
        writer = SingleWriterExecutor()

        # 确保 DB DDL 初始化完成
        with writer.transaction(conn):
            init_db(conn, config_dir=cfg_dir)

        effective.install_eff_write_guard(conn)

        # 检查各学科是否已在 grade_subjects 注册
        slug_map: dict[str, str] = {}
        for _, _, gs in mapped_files:
            if gs in slug_map:
                continue
            row = conn.execute(
                "SELECT slug FROM grade_subjects WHERE grade_subject = ?", (gs,)
            ).fetchone()
            if not row:
                raise PreconditionError(
                    f"学段学科 '{gs}' 未在数据库中注册，请先使用 'shuati grade-subject add' 注册"
                )
            slug_map[gs] = row["slug"]

        scanned_collections_by_gs: dict[str, set[str]] = {}
        present_uids_by_col: dict[tuple[str, str], set[str]] = {}

        for pf, source_collection, grade_subject in mapped_files:
            slug = slug_map[grade_subject]
            scanned_collections_by_gs.setdefault(grade_subject, set()).add(source_collection)
            key = (grade_subject, source_collection)
            present_uids_by_col.setdefault(key, set())
            try:
                with open(pf, encoding="utf-8") as f:
                    for line in f:
                        line_str = line.strip()
                        if not line_str:
                            continue
                        try:
                            it = json.loads(line_str)
                            if isinstance(it, dict) and it.get("id"):
                                sid = str(it.get("id")).strip()
                                present_uids_by_col[key].add(f"{slug}:{source_collection}:{sid}")
                        except Exception:
                            pass
            except Exception:
                pass

        # 4. 逐集合逐题解析处理
        with writer.transaction(conn):
            # 4.0 前置标记本轮源端缺失题并处理其下属活跃副本孤儿认领/晋升（防题号重编吞题 6B）
            for (gs, col), present_uids in present_uids_by_col.items():
                existing_in_col = conn.execute(
                    "SELECT question_uid FROM raw_questions WHERE grade_subject = ? AND source_collection = ?",
                    (gs, col),
                ).fetchall()
                for erow in existing_in_col:
                    e_uid = erow["question_uid"]
                    if e_uid not in present_uids:
                        conn.execute(
                            "UPDATE raw_questions SET is_missing_from_source = 1, updated_at = datetime('now') WHERE question_uid = ?",
                            (e_uid,),
                        )
                        # 若该缺失题曾是活跃题的主题，处理其活跃子副本
                        active_kids = conn.execute(
                            """
                            SELECT question_uid, grade_subject, raw_question, cas_images_json,
                                   image_captions_json, legacy_tags_json, content_hash
                            FROM raw_questions
                            WHERE duplicate_of_uid = ? AND is_missing_from_source = 0
                            ORDER BY ingested_at ASC, question_uid ASC
                            """,
                            (e_uid,),
                        ).fetchall()
                        for akid in active_kids:
                            ak_uid = akid["question_uid"]
                            ak_hash = akid["content_hash"]
                            new_m = conn.execute(
                                """
                                SELECT question_uid
                                FROM raw_questions
                                WHERE grade_subject = ? AND content_hash = ? AND duplicate_of_uid IS NULL
                                  AND is_missing_from_source = 0 AND question_uid != ?
                                ORDER BY ingested_at ASC, question_uid ASC LIMIT 1
                                """,
                                (gs, ak_hash, ak_uid),
                            ).fetchone()
                            if new_m:
                                conn.execute(
                                    "UPDATE raw_questions SET duplicate_of_uid = ?, updated_at = datetime('now') WHERE question_uid = ?",
                                    (new_m["question_uid"], ak_uid),
                                )
                            else:
                                _promote_orphan(conn, akid, gs)

            for pf, source_collection, grade_subject in mapped_files:
                slug = slug_map[grade_subject]
                scanned_collections_by_gs.setdefault(grade_subject, set()).add(source_collection)

                with open(pf, encoding="utf-8") as f:
                    for line_num, line in enumerate(f, start=1):
                        line_str = line.strip()
                        if not line_str:
                            continue

                        try:
                            item = json.loads(line_str)
                        except Exception as e:
                            report.rejected_questions.append(
                                {
                                    "file": str(pf),
                                    "line": str(line_num),
                                    "error": f"JSON_PARSE_ERROR: {e}",
                                }
                            )
                            continue

                        # 严格结构校验：防畸形非 dict 行
                        if not isinstance(item, dict):
                            report.rejected_questions.append(
                                {
                                    "file": str(pf),
                                    "line": str(line_num),
                                    "error": f"MALFORMED_ROW: 行内容不是 JSON 对象 (dict), 而是 {type(item).__name__}",
                                }
                            )
                            continue

                        source_id_raw = item.get("id")
                        if source_id_raw is None or not str(source_id_raw).strip():
                            report.rejected_questions.append(
                                {
                                    "file": str(pf),
                                    "line": str(line_num),
                                    "error": "MISSING_ID: 题目缺少有效 id 字段",
                                }
                            )
                            continue
                        source_id = str(source_id_raw).strip()

                        raw_question_raw = item.get("question")
                        if not isinstance(raw_question_raw, str) or not raw_question_raw.strip():
                            report.rejected_questions.append(
                                {
                                    "file": str(pf),
                                    "line": str(line_num),
                                    "question_id": source_id,
                                    "error": "INVALID_QUESTION: 题干 question 缺失、为 null 或非字符串",
                                }
                            )
                            continue
                        raw_question = raw_question_raw

                        raw_images_raw = item.get("images", [])
                        if not isinstance(raw_images_raw, list) or not all(
                            isinstance(img, str) for img in raw_images_raw
                        ):
                            report.rejected_questions.append(
                                {
                                    "file": str(pf),
                                    "line": str(line_num),
                                    "question_id": source_id,
                                    "error": "INVALID_IMAGES: images 字段必须为字符串列表",
                                }
                            )
                            continue
                        raw_images = raw_images_raw

                        legacy_tags_raw = item.get("tags", {})
                        legacy_tags = legacy_tags_raw if isinstance(legacy_tags_raw, dict) else {}
                        raw_chapter = str(item.get("chapter", "")) if item.get("chapter") is not None else ""

                        report.total_scanned += 1
                        try:
                            source_file_rel = pf.resolve().relative_to(Path.cwd().resolve()).as_posix()
                        except Exception:
                            source_file_rel = pf.as_posix()
                        question_uid = f"{slug}:{source_collection}:{source_id}"

                        # 图片校验与 CAS 归档
                        cas_images: list[str] = []
                        image_corrupted = False

                        for img_rel in raw_images:
                            img_src = pf.parent / img_rel
                            val_res = validate_image(img_src)
                            if not val_res.is_valid:
                                image_corrupted = True
                                report.rejected_questions.append(
                                    {
                                        "question_uid": question_uid,
                                        "image": str(img_src),
                                        "error": val_res.error,
                                    }
                                )
                                break
                            for w in val_res.warnings:
                                report.image_warnings.append(
                                    {
                                        "question_uid": question_uid,
                                        "warning": w,
                                    }
                                )

                            cas_fn = archive_image(img_src, assets_path)
                            cas_images.append(cas_fn)

                        if image_corrupted:
                            continue

                        # 文本规范化与选项提取
                        norm_res = normalize_text(raw_question, num_images=len(cas_images))
                        m_question_text = norm_res.normalized_text
                        image_captions = norm_res.image_captions
                        has_images = 1 if cas_images else 0

                        is_choice, options_dict = detect_and_extract_choice_options(m_question_text)

                        raw_format = legacy_tags.get("format", "解答题")
                        if raw_format == "操作构造题":
                            raw_format = "作图操作题"

                        if is_choice:
                            m_format = "选择题"
                            m_options_json = json.dumps(options_dict, ensure_ascii=False)
                        else:
                            if raw_format in (
                                "选择题",
                                "填空题",
                                "判断题",
                                "计算题",
                                "解答题",
                                "证明题",
                                "作图操作题",
                                "实验探究题",
                            ):
                                m_format = raw_format
                            else:
                                m_format = "解答题"
                            m_options_json = "{}"

                        content_hash = compute_content_hash(
                            m_question_text, cas_images, image_captions
                        )
                        plain_fingerprint = compute_plain_fingerprint(
                            m_question_text, len(cas_images)
                        )

                        # 计算用于近重题预检的配图 dHash
                        dhashes: list[int] = []
                        if cas_images:
                            for c_img in cas_images:
                                try:
                                    dh = compute_dhash(assets_path / c_img)
                                    dhashes.append(dh)
                                except Exception:
                                    pass

                        # 检查库内是否已有同 (grade_subject, source_collection, source_id)
                        existing = conn.execute(
                            """
                            SELECT question_uid, content_hash, duplicate_of_uid
                            FROM raw_questions
                            WHERE grade_subject = ? AND source_collection = ? AND source_id = ?
                            """,
                            (grade_subject, source_collection, source_id),
                        ).fetchone()

                        raw_images_json = json.dumps(raw_images, ensure_ascii=False)
                        cas_images_json = json.dumps(cas_images, ensure_ascii=False)
                        image_captions_json = json.dumps(image_captions, ensure_ascii=False)
                        legacy_tags_json = json.dumps(legacy_tags, ensure_ascii=False)

                        if existing:
                            # 6C: 源题回归自动解冻 MISSING_FROM_SOURCE
                            was_unretired = False
                            unretire_res = conn.execute(
                                """
                                UPDATE questions
                                SET is_retired = 0, retired_reason = '', updated_at = datetime('now')
                                WHERE question_uid = ? AND is_retired = 1 AND retired_reason = 'MISSING_FROM_SOURCE'
                                """,
                                (existing["question_uid"],),
                            )
                            if unretire_res.rowcount > 0:
                                was_unretired = True

                            # 存在同源题记录
                            if existing["content_hash"] == content_hash:
                                # 内容未变，幂等跳过
                                conn.execute(
                                    """
                                    UPDATE raw_questions
                                    SET last_seen_scan_id = ?, is_missing_from_source = 0, updated_at = datetime('now')
                                    WHERE question_uid = ?
                                    """,
                                    (scan_id, existing["question_uid"]),
                                )
                                if was_unretired:
                                    effective.recompute(conn, existing["question_uid"])
                                report.skipped_unchanged.append(existing["question_uid"])
                            else:
                                # 内容变更：重新核对是否还为副本
                                old_hash = existing["content_hash"]
                                new_dup = conn.execute(
                                    """
                                    SELECT question_uid, duplicate_of_uid
                                    FROM raw_questions
                                    WHERE grade_subject = ? AND content_hash = ? AND question_uid != ?
                                      AND COALESCE(duplicate_of_uid, '') != ? AND is_missing_from_source = 0
                                    ORDER BY ingested_at ASC LIMIT 1
                                    """,
                                    (
                                        grade_subject,
                                        content_hash,
                                        existing["question_uid"],
                                        existing["question_uid"],
                                    ),
                                ).fetchone()

                                target_dup_uid = (
                                    (new_dup["duplicate_of_uid"] or new_dup["question_uid"])
                                    if new_dup
                                    else None
                                )
                                if target_dup_uid == existing["question_uid"]:
                                    target_dup_uid = None

                                conn.execute(
                                    """
                                    UPDATE raw_questions
                                    SET
                                        raw_chapter = ?,
                                        source_file = ?,
                                        raw_question = ?,
                                        raw_images_json = ?,
                                        cas_images_json = ?,
                                        image_captions_json = ?,
                                        legacy_tags_json = ?,
                                        content_hash = ?,
                                        plain_fingerprint = ?,
                                        duplicate_of_uid = ?,
                                        last_seen_scan_id = ?,
                                        is_missing_from_source = 0,
                                        updated_at = datetime('now')
                                    WHERE question_uid = ?
                                    """,
                                    (
                                        raw_chapter,
                                        source_file_rel,
                                        raw_question,
                                        raw_images_json,
                                        cas_images_json,
                                        image_captions_json,
                                        legacy_tags_json,
                                        content_hash,
                                        plain_fingerprint,
                                        target_dup_uid,
                                        scan_id,
                                        existing["question_uid"],
                                    ),
                                )

                                q_row = conn.execute(
                                    "SELECT 1 FROM questions WHERE question_uid = ?",
                                    (existing["question_uid"],),
                                ).fetchone()

                                if target_dup_uid:
                                    # 变为了副本：若原先存在 questions 行，安全清理或软退场（防 FK RESTRICT）
                                    if q_row:
                                        if not _question_has_refs(conn, existing["question_uid"]):
                                            conn.execute(
                                                "DELETE FROM questions_fts WHERE question_uid = ?",
                                                (existing["question_uid"],),
                                            )
                                            conn.execute(
                                                "DELETE FROM compile_state WHERE question_uid = ?",
                                                (existing["question_uid"],),
                                            )
                                            conn.execute(
                                                "DELETE FROM questions WHERE question_uid = ?",
                                                (existing["question_uid"],),
                                            )
                                        else:
                                            conn.execute(
                                                """
                                                UPDATE questions
                                                SET is_retired = 1, retired_reason = 'DUPLICATE', updated_at = datetime('now')
                                                WHERE question_uid = ?
                                                """,
                                                (existing["question_uid"],),
                                            )
                                            conn.execute(
                                                "DELETE FROM compile_state WHERE question_uid = ?",
                                                (existing["question_uid"],),
                                            )
                                            effective.recompute(conn, existing["question_uid"])

                                    # 6A: 杜绝 duplicate_of_uid 二级断链
                                    children = conn.execute(
                                        """
                                        SELECT question_uid, grade_subject, raw_question, cas_images_json,
                                               image_captions_json, legacy_tags_json, content_hash, plain_fingerprint
                                        FROM raw_questions
                                        WHERE duplicate_of_uid = ?
                                        ORDER BY ingested_at ASC, question_uid ASC
                                        """,
                                        (existing["question_uid"],),
                                    ).fetchall()
                                    for ch in children:
                                        ch_uid = ch["question_uid"]
                                        ch_hash = ch["content_hash"]
                                        is_match = (ch_hash == content_hash)
                                        if not is_match and dedup_near:
                                            if ch["plain_fingerprint"] == plain_fingerprint:
                                                ch_cas = json.loads(ch["cas_images_json"] or "[]")
                                                if len(ch_cas) == 0 and len(cas_images) == 0:
                                                    is_match = True
                                                elif len(ch_cas) > 0 and len(ch_cas) == len(cas_images):
                                                    ch_dh = [compute_dhash(assets_path / x) for x in ch_cas if (assets_path / x).exists()]
                                                    if len(ch_dh) == len(dhashes):
                                                        dists = [hamming_distance(h1, h2) for h1, h2 in zip(dhashes, ch_dh)]
                                                        if dists and max(dists) <= 4:
                                                            is_match = True
                                        if is_match:
                                            conn.execute(
                                                "UPDATE raw_questions SET duplicate_of_uid = ?, updated_at = datetime('now') WHERE question_uid = ?",
                                                (target_dup_uid, ch_uid),
                                            )
                                        else:
                                            _promote_orphan(conn, ch, grade_subject)
                                else:
                                    # 不是副本：更新或补建 questions / compile_state
                                    if q_row:
                                        conn.execute(
                                            """
                                            UPDATE questions
                                            SET
                                                m_question_text = ?,
                                                m_options_json = ?,
                                                m_format = ?,
                                                cas_images_json = ?,
                                                image_captions_json = ?,
                                                has_images = ?,
                                                is_retired = CASE WHEN retired_reason IN ('DUPLICATE', 'MERGED_NEAR_DUPLICATE', 'MISSING_FROM_SOURCE') THEN 0 ELSE is_retired END,
                                                retired_reason = CASE WHEN retired_reason IN ('DUPLICATE', 'MERGED_NEAR_DUPLICATE', 'MISSING_FROM_SOURCE') THEN '' ELSE retired_reason END,
                                                updated_at = datetime('now')
                                            WHERE question_uid = ?
                                            """,
                                            (
                                                m_question_text,
                                                m_options_json,
                                                m_format,
                                                cas_images_json,
                                                image_captions_json,
                                                has_images,
                                                existing["question_uid"],
                                            ),
                                        )
                                        for stg in ("S1", "S2", "S3C", "S3D", "S4"):
                                            conn.execute(
                                                """
                                                INSERT INTO compile_state(question_uid, stage, status, updated_at)
                                                VALUES (?, ?, 'STALE', datetime('now'))
                                                ON CONFLICT(question_uid, stage) DO UPDATE SET
                                                    status = 'STALE',
                                                    updated_at = datetime('now')
                                                """,
                                                (existing["question_uid"], stg),
                                            )
                                        refresh.handle_source_content_changed(
                                            conn, existing["question_uid"], content_hash
                                        )
                                    else:
                                        # 原为副本，现内容修正后转为主记录，补建主表与编译状态
                                        conn.execute(
                                            """
                                            INSERT INTO questions(
                                                question_uid, grade_subject, cas_images_json,
                                                image_captions_json, has_images, is_retired,
                                                m_question_text, m_options_json, m_format
                                            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                                            """,
                                            (
                                                existing["question_uid"],
                                                grade_subject,
                                                cas_images_json,
                                                image_captions_json,
                                                has_images,
                                                m_question_text,
                                                m_options_json,
                                                m_format,
                                            ),
                                        )
                                        for stg in ("S1", "S2", "S3C", "S3D", "S4"):
                                            conn.execute(
                                                """
                                                INSERT INTO compile_state(question_uid, stage, status, updated_at)
                                                VALUES (?, ?, 'STALE', datetime('now'))
                                                ON CONFLICT(question_uid, stage) DO UPDATE SET
                                                    status = 'STALE',
                                                    updated_at = datetime('now')
                                                """,
                                                (existing["question_uid"], stg),
                                            )
                                        effective.recompute(conn, existing["question_uid"])

                                    # 6D: 精确区分精确副本与近重副本的孤儿判据（修复 6D 且完美保留 #20）
                                    children = conn.execute(
                                        """
                                        SELECT question_uid, grade_subject, raw_question, cas_images_json,
                                               image_captions_json, legacy_tags_json, content_hash, plain_fingerprint
                                        FROM raw_questions
                                        WHERE duplicate_of_uid = ?
                                        ORDER BY ingested_at ASC, question_uid ASC
                                        """,
                                        (existing["question_uid"],),
                                    ).fetchall()
                                    for ch in children:
                                        ch_uid = ch["question_uid"]
                                        ch_hash = ch["content_hash"]
                                        near_matched = False
                                        if ch["plain_fingerprint"] == plain_fingerprint:
                                            ch_cas = json.loads(ch["cas_images_json"] or "[]")
                                            if len(ch_cas) == 0 and len(cas_images) == 0:
                                                near_matched = True
                                            elif len(ch_cas) > 0 and len(ch_cas) == len(cas_images):
                                                ch_dh = [compute_dhash(assets_path / x) for x in ch_cas if (assets_path / x).exists()]
                                                if len(ch_dh) == len(dhashes):
                                                    dists = [hamming_distance(h1, h2) for h1, h2 in zip(dhashes, ch_dh)]
                                                    if dists and max(dists) <= 4:
                                                        near_matched = True

                                        if ch_hash == old_hash:
                                            # 原本是精确副本：仅当 dedup_near=True 且满足近重条件时保持归并，否则为孤儿
                                            is_still_merged = bool(dedup_near and near_matched)
                                        else:
                                            # 原本是近重副本（如 #20）：只要满足近重条件就继续保持归并
                                            is_still_merged = near_matched

                                        if not is_still_merged:
                                            new_master = conn.execute(
                                                """
                                                SELECT question_uid
                                                FROM raw_questions
                                                WHERE grade_subject = ? AND content_hash = ? AND duplicate_of_uid IS NULL
                                                  AND is_missing_from_source = 0 AND question_uid != ?
                                                ORDER BY ingested_at ASC, question_uid ASC LIMIT 1
                                                """,
                                                (grade_subject, ch_hash, ch_uid),
                                            ).fetchone()
                                            if new_master:
                                                conn.execute(
                                                    "UPDATE raw_questions SET duplicate_of_uid = ?, updated_at = datetime('now') WHERE question_uid = ?",
                                                    (new_master["question_uid"], ch_uid),
                                                )
                                            else:
                                                _promote_orphan(conn, ch, grade_subject)

                                report.updated_source_changed.append(existing["question_uid"])
                            continue

                        # 新题：检查同学段精确 content_hash 重复
                        exact_dup = conn.execute(
                            """
                            SELECT question_uid, duplicate_of_uid
                            FROM raw_questions
                            WHERE grade_subject = ? AND content_hash = ? AND is_missing_from_source = 0
                            ORDER BY ingested_at ASC LIMIT 1
                            """,
                            (grade_subject, content_hash),
                        ).fetchone()

                        if exact_dup:
                            target_uid = exact_dup["duplicate_of_uid"] or exact_dup["question_uid"]
                            conn.execute(
                                """
                                INSERT INTO raw_questions(
                                    question_uid, grade_subject, source_type, source_collection,
                                    source_file, source_id, raw_chapter, raw_question,
                                    raw_images_json, cas_images_json, image_captions_json,
                                    legacy_tags_json, content_hash, plain_fingerprint,
                                    duplicate_of_uid, is_missing_from_source, last_seen_scan_id
                                ) VALUES (?, ?, 'BATCH_JSONL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                                """,
                                (
                                    question_uid,
                                    grade_subject,
                                    source_collection,
                                    source_file_rel,
                                    source_id,
                                    raw_chapter,
                                    raw_question,
                                    raw_images_json,
                                    cas_images_json,
                                    image_captions_json,
                                    legacy_tags_json,
                                    content_hash,
                                    plain_fingerprint,
                                    target_uid,
                                    scan_id,
                                ),
                            )
                            report.exact_duplicates.append(
                                {
                                    "question_uid": question_uid,
                                    "duplicate_of_uid": target_uid,
                                }
                            )
                            report.newly_ingested.append(question_uid)
                            continue

                        # 检查同学段疑似同题重扫预检 (plain_fingerprint + dHash)
                        near_dup_target: str | None = None
                        cands = conn.execute(
                            """
                            SELECT question_uid, cas_images_json
                            FROM raw_questions
                            WHERE grade_subject = ? AND plain_fingerprint = ? AND duplicate_of_uid IS NULL
                              AND is_missing_from_source = 0
                            ORDER BY ingested_at ASC
                            """,
                            (grade_subject, plain_fingerprint),
                        ).fetchall()

                        for cand in cands:
                            cand_uid = cand["question_uid"]
                            cand_cas = json.loads(cand["cas_images_json"] or "[]")
                            if len(cas_images) == 0 and len(cand_cas) == 0:
                                near_dup_target = cand_uid
                                report.near_duplicate_candidates.append(
                                    {
                                        "question_uid": question_uid,
                                        "candidate_uid": cand_uid,
                                        "distance": 0,
                                    }
                                )
                                break
                            elif len(cas_images) > 0 and len(cas_images) == len(cand_cas):
                                cand_dhashes = []
                                for ci in cand_cas:
                                    try:
                                        cand_dhashes.append(compute_dhash(assets_path / ci))
                                    except Exception:
                                        pass
                                if len(cand_dhashes) == len(dhashes):
                                    dists = [
                                        hamming_distance(d1, d2)
                                        for d1, d2 in zip(dhashes, cand_dhashes)
                                    ]
                                    max_d = max(dists) if dists else 99
                                    if max_d <= 4:
                                        near_dup_target = cand_uid
                                        report.near_duplicate_candidates.append(
                                            {
                                                "question_uid": question_uid,
                                                "candidate_uid": cand_uid,
                                                "distance": max_d,
                                            }
                                        )
                                        break

                        if near_dup_target and dedup_near:
                            # 开启近重题归并，标记为副本
                            conn.execute(
                                """
                                INSERT INTO raw_questions(
                                    question_uid, grade_subject, source_type, source_collection,
                                    source_file, source_id, raw_chapter, raw_question,
                                    raw_images_json, cas_images_json, image_captions_json,
                                    legacy_tags_json, content_hash, plain_fingerprint,
                                    duplicate_of_uid, is_missing_from_source, last_seen_scan_id
                                ) VALUES (?, ?, 'BATCH_JSONL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                                """,
                                (
                                    question_uid,
                                    grade_subject,
                                    source_collection,
                                    source_file_rel,
                                    source_id,
                                    raw_chapter,
                                    raw_question,
                                    raw_images_json,
                                    cas_images_json,
                                    image_captions_json,
                                    legacy_tags_json,
                                    content_hash,
                                    plain_fingerprint,
                                    near_dup_target,
                                    scan_id,
                                ),
                            )
                            report.merged_near_duplicates.append(
                                {
                                    "question_uid": question_uid,
                                    "duplicate_of_uid": near_dup_target,
                                }
                            )
                            report.newly_ingested.append(question_uid)
                            continue

                        # 正常新题入库
                        conn.execute(
                            """
                            INSERT INTO raw_questions(
                                question_uid, grade_subject, source_type, source_collection,
                                source_file, source_id, raw_chapter, raw_question,
                                raw_images_json, cas_images_json, image_captions_json,
                                legacy_tags_json, content_hash, plain_fingerprint,
                                duplicate_of_uid, is_missing_from_source, last_seen_scan_id
                            ) VALUES (?, ?, 'BATCH_JSONL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                            """,
                            (
                                question_uid,
                                grade_subject,
                                source_collection,
                                source_file_rel,
                                source_id,
                                raw_chapter,
                                raw_question,
                                raw_images_json,
                                cas_images_json,
                                image_captions_json,
                                legacy_tags_json,
                                content_hash,
                                plain_fingerprint,
                                scan_id,
                            ),
                        )

                        # 写入 questions 机器列（严禁直写 eff_* 列）
                        conn.execute(
                            """
                            INSERT INTO questions(
                                question_uid, grade_subject, cas_images_json,
                                image_captions_json, has_images, is_retired,
                                m_question_text, m_options_json, m_format
                            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                            """,
                            (
                                question_uid,
                                grade_subject,
                                cas_images_json,
                                image_captions_json,
                                has_images,
                                m_question_text,
                                m_options_json,
                                m_format,
                            ),
                        )

                        # 插入初始 compile_state 状态
                        for stg in ("S1", "S2", "S3C", "S3D", "S4"):
                            conn.execute(
                                """
                                INSERT INTO compile_state(question_uid, stage, status, updated_at)
                                VALUES (?, ?, 'PENDING', datetime('now'))
                                """,
                                (question_uid, stg),
                            )

                        # 在同一事务尾部调用 effective.recompute 刷新生效列
                        effective.recompute(conn, question_uid)
                        report.newly_ingested.append(question_uid)

            # 5. 全局疑似同题重扫预检与近重题补跑归并 (plain_fingerprint + dHash)
            for gs in scanned_collections_by_gs:
                cand_pairs = conn.execute(
                    """
                    SELECT
                        r1.question_uid as uid1, r1.cas_images_json as cas1, r1.ingested_at as t1,
                        r2.question_uid as uid2, r2.cas_images_json as cas2, r2.ingested_at as t2
                    FROM raw_questions r1
                    JOIN raw_questions r2 ON r1.grade_subject = r2.grade_subject
                        AND r1.plain_fingerprint = r2.plain_fingerprint
                        AND r1.question_uid < r2.question_uid
                        AND r1.duplicate_of_uid IS NULL
                        AND r2.duplicate_of_uid IS NULL
                        AND r1.is_missing_from_source = 0
                        AND r2.is_missing_from_source = 0
                    WHERE r1.grade_subject = ?
                    """,
                    (gs,),
                ).fetchall()

                for pair in cand_pairs:
                    # 确定主副本顺序：先入库者为主，后入库者为待归并副本
                    if pair["t1"] <= pair["t2"]:
                        master_uid, dup_uid = pair["uid1"], pair["uid2"]
                        m_cas = json.loads(pair["cas1"] or "[]")
                        d_cas = json.loads(pair["cas2"] or "[]")
                    else:
                        master_uid, dup_uid = pair["uid2"], pair["uid1"]
                        m_cas = json.loads(pair["cas2"] or "[]")
                        d_cas = json.loads(pair["cas1"] or "[]")

                    is_near = False
                    dist = 0
                    if len(m_cas) == 0 and len(d_cas) == 0:
                        is_near = True
                        dist = 0
                    elif len(m_cas) > 0 and len(m_cas) == len(d_cas):
                        m_dhashes = [
                            compute_dhash(assets_path / ci)
                            for ci in m_cas
                            if (assets_path / ci).exists()
                        ]
                        d_dhashes = [
                            compute_dhash(assets_path / ci)
                            for ci in d_cas
                            if (assets_path / ci).exists()
                        ]
                        if len(m_dhashes) == len(d_dhashes) and m_dhashes:
                            dists = [
                                hamming_distance(h1, h2)
                                for h1, h2 in zip(m_dhashes, d_dhashes)
                            ]
                            dist = max(dists)
                            if dist <= 4:
                                is_near = True

                    if is_near:
                        if not any(
                            c["question_uid"] == dup_uid and c["candidate_uid"] == master_uid
                            for c in report.near_duplicate_candidates
                        ):
                            report.near_duplicate_candidates.append(
                                {
                                    "question_uid": dup_uid,
                                    "candidate_uid": master_uid,
                                    "distance": dist,
                                }
                            )

                        if dedup_near:
                            conn.execute(
                                "UPDATE raw_questions SET duplicate_of_uid = ? WHERE question_uid = ?",
                                (master_uid, dup_uid),
                            )
                            # 检查 dup_uid 是否有外键引用
                            if not _question_has_refs(conn, dup_uid):
                                conn.execute(
                                    "DELETE FROM questions_fts WHERE question_uid = ?",
                                    (dup_uid,),
                                )
                                conn.execute(
                                    "DELETE FROM compile_state WHERE question_uid = ?",
                                    (dup_uid,),
                                )
                                conn.execute(
                                    "DELETE FROM questions WHERE question_uid = ?",
                                    (dup_uid,),
                                )
                            else:
                                conn.execute(
                                    "UPDATE questions SET is_retired = 1, retired_reason = 'MERGED_NEAR_DUPLICATE' WHERE question_uid = ?",
                                    (dup_uid,),
                                )
                                effective.recompute(conn, dup_uid)

                            if not any(
                                m["question_uid"] == dup_uid
                                for m in report.merged_near_duplicates
                            ):
                                report.merged_near_duplicates.append(
                                    {
                                        "question_uid": dup_uid,
                                        "duplicate_of_uid": master_uid,
                                    }
                                )

            # 6. 源题消失检测
            for gs, colls in scanned_collections_by_gs.items():
                for col in colls:
                    conn.execute(
                        """
                        UPDATE raw_questions
                        SET is_missing_from_source = 1
                        WHERE grade_subject = ? AND source_collection = ? AND last_seen_scan_id != ?
                        """,
                        (gs, col, scan_id),
                    )

            missing_rows = conn.execute(
                """
                SELECT question_uid FROM raw_questions
                WHERE is_missing_from_source = 1
                """
            ).fetchall()
            report.missing_from_source = [r["question_uid"] for r in missing_rows]

            if apply_retire and report.missing_from_source:
                for uid in report.missing_from_source:
                    conn.execute(
                        """
                        UPDATE questions
                        SET is_retired = 1, retired_reason = 'MISSING_FROM_SOURCE', updated_at = datetime('now')
                        WHERE question_uid = ? AND is_retired = 0
                        """,
                        (uid,),
                    )
                    effective.recompute(conn, uid)
                    report.retired_questions.append(uid)

    # 7. 生成体检报告（.json 与 .md）
    json_path = reports_path / f"ingest_{scan_ts}.json"
    md_path = reports_path / f"ingest_{scan_ts}.md"

    report.report_json_path = str(json_path)
    report.report_md_path = str(md_path)

    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md_content = f"""# Stage 0 入库体检报告 ({scan_ts})

- **批次号 (scan_id)**: `{report.scan_id}`
- **扫描题目总数**: {report.total_scanned}
- **新入库题目数**: {len(report.newly_ingested)}
- **内容未变跳过数**: {len(report.skipped_unchanged)}
- **源题修改更新数**: {len(report.updated_source_changed)}
- **精确去重副本数**: {len(report.exact_duplicates)}
- **高置信疑似重扫题对**: {len(report.near_duplicate_candidates)}
- **近重题归并副本数**: {len(report.merged_near_duplicates)}
- **坏题拒入数**: {len(report.rejected_questions)}
- **源端消失题数**: {len(report.missing_from_source)}
- **下架处理题数**: {len(report.retired_questions)}
- **未匹配集合数**: {len(report.unmapped_collections)}
- **图片尺寸/EXIF告警数**: {len(report.image_warnings)}

## 1. 高置信疑似同题重扫对 (Near-duplicate Candidates)
"""
    if report.near_duplicate_candidates:
        for c in report.near_duplicate_candidates:
            md_content += (
                f"- `{c['question_uid']}` <--> `{c['candidate_uid']}` (dHash distance: {c.get('distance', 0)})\n"
            )
    else:
        md_content += "无\n"

    md_content += "\n## 2. 坏题与损坏图片拒入清单\n"
    if report.rejected_questions:
        for r in report.rejected_questions:
            md_content += f"- {r}\n"
    else:
        md_content += "无（所有题目图片解码正常且存在）\n"

    md_content += "\n## 3. 未匹配集合清单\n"
    if report.unmapped_collections:
        for u in report.unmapped_collections:
            md_content += f"- `{u}`\n"
    else:
        md_content += "无\n"

    md_content += "\n## 4. 图片尺寸与 EXIF 告警清单\n"
    if report.image_warnings:
        for iw in report.image_warnings:
            md_content += f"- {iw}\n"
    else:
        md_content += "无\n"

    md_path.write_text(md_content, encoding="utf-8")
    return report
