"""CAS 内容寻址资产库与 Markdown 规范化服务。

设计文档 §3.1 第 1 条（分区剪贴板导入与 SHA-256 CAS 归档）与 Target File #5：
- 图片统一本地预处理（长边 <= settings.CAS_MAX_IMAGE_SIDE，转码 WebP）后对编码字节
  计算 SHA-256，落盘 {CAS_ASSETS_DIR}/{h[:2]}/{h[2:4]}/{h}.webp，Markdown 内引用 cas://{h}；
- Markdown 规范化提取/注入不可变 question_uid Frontmatter，将本地配图改写为 CAS URI，
  并计算 stem_hash 用于识别二次编辑增量更新（不引入 PyYAML，简单行解析）。
"""

import contextlib
import hashlib
import io
import os
import re
import tempfile
import uuid
from pathlib import Path

from PIL import Image

from backend.config import settings

# "cas://" + 64 位小写 hex
_CAS_URI_RE = re.compile(r"cas://([0-9a-f]{64})")
# 任意 URI scheme（cas://、http(s):// 等）一律不做本地文件解析
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
# ![alt](path) / ![alt](path "title")
_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s]+)(\s+"[^"]*")?\s*\)')

_UID_FIELD = "question_uid"
_GRADE_FIELD = "target_grade_and_subject"
_UID_NAMESPACE_PREFIX = "shuati-harness:"


def ingest_image_bytes(raw_bytes: bytes, max_side: int | None = None) -> str:
    """解码图片 → 长边等比缩放 → 转 RGB → WebP 编码 → SHA-256 CAS 归档。

    幂等：同内容重复入库返回同 URI 且不重复写盘；写盘用临时文件 + rename 原子替换。
    max_side 缺省取 settings.CAS_MAX_IMAGE_SIDE。返回 "cas://{sha256}"。
    """
    limit = settings.CAS_MAX_IMAGE_SIDE if max_side is None else max_side
    if limit < 1:
        raise ValueError(f"max_side 必须 >= 1，收到 {limit}")

    with Image.open(io.BytesIO(raw_bytes)) as src:
        src.load()
        width, height = src.size
        long_side = max(width, height)
        img: Image.Image = src
        if long_side > limit:
            scale = limit / long_side
            new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            img = src.resize(new_size, Image.Resampling.LANCZOS)
        rgb = img.convert("RGB")

    buffer = io.BytesIO()
    rgb.save(buffer, format="WEBP")
    payload = buffer.getvalue()

    digest = hashlib.sha256(payload).hexdigest()
    dest = settings.CAS_ASSETS_DIR / digest[:2] / digest[2:4] / f"{digest}.webp"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{digest}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp_name, dest)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    return f"cas://{digest}"


def resolve_cas_uri(uri: str) -> Path:
    """校验 CAS URI 并返回对应资产文件路径（Phase 4 /assets/{hash} 静态路由复用）。

    非法 URI 抛 ValueError；文件不存在抛 FileNotFoundError。
    """
    match = _CAS_URI_RE.fullmatch(uri)
    if match is None:
        raise ValueError(f"非法 CAS URI（要求 cas:// + 64 位小写 hex）: {uri!r}")
    digest = match.group(1)
    path = settings.CAS_ASSETS_DIR / digest[:2] / digest[2:4] / f"{digest}.webp"
    if not path.is_file():
        raise FileNotFoundError(f"CAS 资产不存在: {uri}")
    return path


def _parse_frontmatter(text: str) -> tuple[dict[str, str], list[str], str]:
    """简单行解析 `---` 包裹的头部，返回 (已知字段, 其余原样保留行, 正文)。

    无 frontmatter（或未闭合）时整体视为正文。
    """
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, [], text
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            fields: dict[str, str] = {}
            extras: list[str] = []
            for line in lines[1:idx]:
                key, sep, value = line.partition(":")
                if sep and key.strip() in (_UID_FIELD, _GRADE_FIELD):
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    fields[key.strip()] = value
                elif line.strip():
                    extras.append(line)
            return fields, extras, "\n".join(lines[idx + 1 :])
    return {}, [], text


def _rewrite_local_images(body: str, base_dir: Path | None) -> str:
    """将 base_dir 下可找到的本地相对路径配图改写为 cas:// URI。

    已带 scheme（cas://、http(s):// 等）、绝对路径、找不到的本地路径一律保持原样。
    """
    if base_dir is None:
        return body

    def _sub(match: re.Match[str]) -> str:
        alt, target, title = match.group(1), match.group(2), match.group(3) or ""
        if _SCHEME_RE.match(target) or Path(target).is_absolute():
            return match.group(0)
        candidate = base_dir / target
        if not candidate.is_file():
            return match.group(0)
        uri = ingest_image_bytes(candidate.read_bytes())
        return f"![{alt}]({uri}{title})"

    return _IMAGE_RE.sub(_sub, body)


def normalize_markdown_with_frontmatter(
    stem_md: str,
    question_uid: str | None,
    target_grade: str,
    base_dir: Path | None = None,
) -> tuple[str, str, str]:
    """规范化题干 Markdown，返回 (规范化后的完整 Markdown, question_uid, stem_hash)。

    - 已有 question_uid 一律保留（不可变），与传入 uid 冲突时以已有为准；
    - 无 frontmatter 且 question_uid=None 时生成确定性 uid：
      uuid5(NAMESPACE_URL, "shuati-harness:" + sha256(规范化正文))，同内容重导入幂等；
    - stem_hash 为剥离 frontmatter 后的规范化正文（含图片改写）的 SHA-256 hex。
    """
    text = stem_md.replace("\r\n", "\n").replace("\r", "\n")
    fields, extras, raw_body = _parse_frontmatter(text)
    body = _rewrite_local_images(raw_body.strip(), base_dir)
    stem_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    uid = fields.get(_UID_FIELD) or question_uid
    if not uid:
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, _UID_NAMESPACE_PREFIX + stem_hash))

    fm_lines = [f"{_UID_FIELD}: {uid}", f"{_GRADE_FIELD}: {target_grade}", *extras]
    normalized = "---\n" + "\n".join(fm_lines) + "\n---\n"
    if body:
        normalized += "\n" + body + "\n"
    return normalized, uid, stem_hash
