"""cas_store 单元测试：图片全部用 Pillow 在测试内合成，不依赖二进制 fixture。"""

import hashlib
import io
import random
import re
import uuid
from pathlib import Path

import pytest
from PIL import Image, UnidentifiedImageError

from backend.config import settings
from backend.services.cas_store import (
    ingest_image_bytes,
    normalize_markdown_with_frontmatter,
    resolve_cas_uri,
)

_URI_RE = re.compile(r"cas://([0-9a-f]{64})")


def _make_png(
    size: tuple[int, int],
    mode: str = "RGB",
    color: int | tuple[int, ...] = (120, 30, 200),
) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _noise_png(width: int, height: int) -> bytes:
    data = random.Random(42).randbytes(width * height * 3)
    img = Image.frombytes("RGB", (width, height), data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _cas_path(cas_dir: Path, uri: str) -> Path:
    digest = uri.removeprefix("cas://")
    return cas_dir / digest[:2] / digest[2:4] / f"{digest}.webp"


# ---------------------------------------------------------------- ingest


def test_ingest_downscale_webp_and_bucket(cas_dir: Path) -> None:
    raw = _make_png((2000, 1000))
    uri = ingest_image_bytes(raw)

    assert _URI_RE.fullmatch(uri)
    digest = uri.removeprefix("cas://")
    path = _cas_path(cas_dir, uri)
    assert path.is_file()
    # 哈希针对编码后字节而非原始输入
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert digest != hashlib.sha256(raw).hexdigest()
    with Image.open(path) as img:
        assert img.format == "WEBP"
        assert img.mode == "RGB"
        assert img.size == (1536, 768)
        assert max(img.size) <= settings.CAS_MAX_IMAGE_SIDE


def test_ingest_idempotent_single_file(cas_dir: Path) -> None:
    raw = _noise_png(1600, 1200)
    uri1 = ingest_image_bytes(raw)
    uri2 = ingest_image_bytes(raw)

    assert uri1 == uri2
    files = [p for p in cas_dir.rglob("*") if p.is_file()]
    assert files == [_cas_path(cas_dir, uri1)]  # 只有一份，且无残留临时文件


@pytest.mark.parametrize("size", [(800, 600), (1536, 1000), (1000, 1536)])
def test_small_or_boundary_image_not_upscaled(cas_dir: Path, size: tuple[int, int]) -> None:
    uri = ingest_image_bytes(_make_png(size))
    with Image.open(_cas_path(cas_dir, uri)) as img:
        assert img.size == size


def test_explicit_max_side(cas_dir: Path) -> None:
    uri = ingest_image_bytes(_make_png((2000, 1000)), max_side=512)
    with Image.open(_cas_path(cas_dir, uri)) as img:
        assert img.size == (512, 256)


def test_default_max_side_from_settings(cas_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CAS_MAX_IMAGE_SIDE", 300)
    uri = ingest_image_bytes(_make_png((2000, 1000)))
    with Image.open(_cas_path(cas_dir, uri)) as img:
        assert img.size == (300, 150)


@pytest.mark.parametrize(
    ("mode", "color"),
    [("RGBA", (255, 0, 0, 128)), ("P", 3), ("L", 77)],
)
def test_mode_conversion(cas_dir: Path, mode: str, color: int | tuple[int, ...]) -> None:
    uri = ingest_image_bytes(_make_png((2000, 1000), mode=mode, color=color))
    with Image.open(_cas_path(cas_dir, uri)) as img:
        assert img.format == "WEBP"
        assert img.mode == "RGB"
        assert max(img.size) <= 1536


def test_ingest_rejects_garbage_bytes(cas_dir: Path) -> None:
    with pytest.raises(UnidentifiedImageError):
        ingest_image_bytes(b"definitely not an image")


def test_ingest_rejects_nonpositive_max_side(cas_dir: Path) -> None:
    with pytest.raises(ValueError):
        ingest_image_bytes(_make_png((10, 10)), max_side=0)


# ---------------------------------------------------------------- resolve


def test_resolve_valid_uri(cas_dir: Path) -> None:
    uri = ingest_image_bytes(_make_png((64, 48)))
    path = resolve_cas_uri(uri)
    assert path == _cas_path(cas_dir, uri)
    assert path.is_file()


@pytest.mark.parametrize(
    "bad_uri",
    [
        "",
        "cas://",
        "cas://" + "a" * 63,
        "cas://" + "a" * 65,
        "cas://" + "A" * 64,  # 大写 hex 不合法
        "cas://" + "g" * 64,  # 非 hex 字符
        "cas://" + "a" * 64 + ".webp",
        "cas://" + "a" * 64 + "\n",
        "http://" + "a" * 64,
        "cas:/" + "a" * 64,
    ],
)
def test_resolve_invalid_uri(cas_dir: Path, bad_uri: str) -> None:
    with pytest.raises(ValueError):
        resolve_cas_uri(bad_uri)


def test_resolve_missing_file(cas_dir: Path) -> None:
    digest = hashlib.sha256(b"never ingested").hexdigest()
    with pytest.raises(FileNotFoundError):
        resolve_cas_uri(f"cas://{digest}")


# ---------------------------------------------------------------- normalize


def test_uid_injection_deterministic_and_idempotent() -> None:
    md = "# 题干\n\n一个质量为 $m$ 的滑块。\n"
    out, uid, stem_hash = normalize_markdown_with_frontmatter(md, None, "初二物理")

    assert out.startswith("---\n")
    assert f"question_uid: {uid}\n" in out
    assert "target_grade_and_subject: 初二物理\n" in out
    assert out.endswith("# 题干\n\n一个质量为 $m$ 的滑块。\n")

    assert stem_hash == hashlib.sha256("# 题干\n\n一个质量为 $m$ 的滑块。".encode()).hexdigest()
    assert uid == str(uuid.uuid5(uuid.NAMESPACE_URL, "shuati-harness:" + stem_hash))
    assert uuid.UUID(uid).version == 5

    # 对规范化输出再次规范化：完全幂等
    out2, uid2, hash2 = normalize_markdown_with_frontmatter(out, None, "初二物理")
    assert (out2, uid2, hash2) == (out, uid, stem_hash)


def test_explicit_uid_used_when_absent() -> None:
    out, uid, _ = normalize_markdown_with_frontmatter("正文", "my-uid-123", "高一数学")
    assert uid == "my-uid-123"
    assert "question_uid: my-uid-123\n" in out


def test_existing_uid_immutable_and_grade_updated() -> None:
    md = "---\nquestion_uid: existing-uid\ntarget_grade_and_subject: 旧年级\n---\n\n正文内容\n"
    out, uid, stem_hash = normalize_markdown_with_frontmatter(md, "conflicting-uid", "新年级")

    assert uid == "existing-uid"  # 冲突时以已有为准
    assert "question_uid: existing-uid\n" in out
    assert "target_grade_and_subject: 新年级\n" in out
    assert "旧年级" not in out
    assert stem_hash == hashlib.sha256("正文内容".encode()).hexdigest()


def test_stem_hash_stable_across_frontmatter_and_whitespace() -> None:
    body = "同一道题干。\n\n![图](cas://" + "b" * 64 + ")\n"
    md_a = body
    md_b = f"---\nquestion_uid: u1\ntarget_grade_and_subject: A\n---\n\n{body}\n\n\n"
    md_c = f"---\nquestion_uid: u2\ntarget_grade_and_subject: B\n---\n{body}"

    _, uid_a, hash_a = normalize_markdown_with_frontmatter(md_a, None, "A")
    _, uid_b, hash_b = normalize_markdown_with_frontmatter(md_b, None, "B")
    _, uid_c, hash_c = normalize_markdown_with_frontmatter(md_c, None, "C")
    assert hash_a == hash_b == hash_c
    # 已有 uid 保留，无 uid 时由正文确定性生成
    assert uid_b == "u1"
    assert uid_c == "u2"
    assert uid_a not in (uid_b, uid_c)


def test_crlf_input_normalized_stably() -> None:
    lf = "第一行\r\n第二行\r\n".replace("\r\n", "\n")
    crlf = "第一行\r\n第二行\r\n"
    out_a, uid_a, hash_a = normalize_markdown_with_frontmatter(lf, None, "G")
    out_b, uid_b, hash_b = normalize_markdown_with_frontmatter(crlf, None, "G")
    assert (out_a, uid_a, hash_a) == (out_b, uid_b, hash_b)


def test_local_image_rewritten_and_stable(cas_dir: Path, tmp_path: Path) -> None:
    base_dir = tmp_path / "md"
    (base_dir / "pics").mkdir(parents=True)
    (base_dir / "pics" / "fig.png").write_bytes(_make_png((2000, 1000)))
    md = '题干\n\n![配图](pics/fig.png)\n\n![带标题](pics/fig.png "说明")\n'

    out, _, stem_hash = normalize_markdown_with_frontmatter(md, None, "G", base_dir=base_dir)
    uris = re.findall(r"!\[[^\]]*\]\((cas://[0-9a-f]{64})", out)
    assert len(uris) == 2
    assert uris[0] == uris[1]  # 同一文件内容寻址到同一 URI
    assert "pics/fig.png" not in out
    assert '![带标题](' + uris[1] + ' "说明")' in out  # alt 与 title 保留
    assert resolve_cas_uri(uris[0]).is_file()
    body_out = re.sub(r"^---\n.*?\n---\n\n", "", out, flags=re.S).rstrip("\n")
    assert stem_hash == hashlib.sha256(body_out.encode()).hexdigest()

    # 二次规范化：cas:// 不再改动，输出完全一致
    out2, _, hash2 = normalize_markdown_with_frontmatter(out, None, "G", base_dir=base_dir)
    assert out2 == out
    assert hash2 == stem_hash


def test_remote_and_cas_uris_untouched(cas_dir: Path, tmp_path: Path) -> None:
    md = (
        "![a](cas://" + "c" * 64 + ")\n"
        "![b](https://example.com/x.png)\n"
        "![c](http://example.com/y.jpg)\n"
    )
    out, _, _ = normalize_markdown_with_frontmatter(md, None, "G", base_dir=tmp_path)
    assert md in out


def test_missing_local_path_untouched(cas_dir: Path, tmp_path: Path) -> None:
    md = "![缺图](nope/missing.png)\n"
    out, _, _ = normalize_markdown_with_frontmatter(md, None, "G", base_dir=tmp_path)
    assert md in out
    assert list(cas_dir.rglob("*")) == []  # 未发生任何入库


def test_no_base_dir_leaves_local_paths() -> None:
    md = "![本地图](fig.png)\n"
    out, _, _ = normalize_markdown_with_frontmatter(md, None, "G")
    assert md in out


def test_absolute_local_path_untouched(cas_dir: Path, tmp_path: Path) -> None:
    img = tmp_path / "abs.png"
    img.write_bytes(_make_png((32, 32)))
    md = f"![绝对路径]({img})\n"
    out, _, _ = normalize_markdown_with_frontmatter(md, None, "G", base_dir=tmp_path)
    assert md in out


def test_extra_frontmatter_fields_preserved() -> None:
    md = "---\nquestion_uid: keep-me\ntags: physics\n---\n正文\n"
    out, uid, _ = normalize_markdown_with_frontmatter(md, None, "G")
    assert uid == "keep-me"
    assert "tags: physics\n" in out
