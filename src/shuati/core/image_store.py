"""图片校验、EXIF 规范化与保序 CAS 归档（DESIGN.md §4.2-2）。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps


@dataclass
class ImageValidationResult:
    is_valid: bool
    path: Path
    width: int = 0
    height: int = 0
    format: str = ""
    exif_orientation: int = 1
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def validate_image(path: Path | str) -> ImageValidationResult:
    """校验图片存在性、Pillow 解码、宽高与 EXIF Orientation。

    - 损坏或缺失：is_valid = False；
    - min(w, h) < 120：添加告警 IMAGE_DIMENSION_TOO_SMALL（不拒入）；
    - 非正向 EXIF Orientation（!= 1）：添加告警 NON_NORMAL_EXIF_ORIENTATION（不拒入）。
    """
    p = Path(path)
    if not p.is_file():
        return ImageValidationResult(
            is_valid=False,
            path=p,
            error=f"FILE_NOT_FOUND: 图片文件不存在 {p}",
        )

    try:
        with Image.open(p) as img:
            w, h = img.size
            fmt = img.format or ""
            orientation = 1
            exif = img.getexif()
            if exif:
                orientation = exif.get(0x0112, 1)

            warnings: list[str] = []
            if min(w, h) < 120:
                warnings.append(
                    f"IMAGE_DIMENSION_TOO_SMALL: 图片尺寸过小 ({w}x{h} < 120px) {p.name}"
                )
            if orientation not in (1, 0, None):
                warnings.append(
                    f"NON_NORMAL_EXIF_ORIENTATION: 包含非正向 EXIF 旋转标记 (orientation={orientation}) {p.name}"
                )

            return ImageValidationResult(
                is_valid=True,
                path=p,
                width=w,
                height=h,
                format=fmt,
                exif_orientation=orientation or 1,
                warnings=warnings,
            )
    except Exception as e:
        return ImageValidationResult(
            is_valid=False,
            path=p,
            error=f"CORRUPTED_IMAGE: 无法解码图片 {p}: {e}",
        )


def archive_image(
    src_path: Path | str,
    target_dir: Path | str = "data/assets/images",
) -> str:
    """以原字节 SHA-256 前 16 位保序归档至 CAS 仓库。

    只读原图字节，不修改原始文件。
    返回保序文件名，如 'a1b2c3d4e5f67890.jpg'。
    """
    src = Path(src_path)
    raw_bytes = src.read_bytes()
    h16 = hashlib.sha256(raw_bytes).hexdigest()[:16]

    suffix = src.suffix.lower()
    if not suffix:
        suffix = ".jpg"

    cas_filename = f"{h16}{suffix}"
    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / cas_filename

    if not out_path.exists():
        # 写入临时文件后 replace，确保原子写入
        tmp_path = out_path.with_suffix(f"{out_path.suffix}.tmp")
        tmp_path.write_bytes(raw_bytes)
        os.replace(tmp_path, out_path)

    return cas_filename


def prepare_external_image(
    src: Path | str,
    dst: Path | str,
    max_px: int = 2048,
) -> Path:
    """外部拍照错题图片预处理（ImageOps.exif_transpose + 转 RGB + 限制长边 max_px）。"""
    src_p = Path(src)
    dst_p = Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src_p) as raw_img:
        # 1. 矫正 EXIF 方向
        img: Image.Image = ImageOps.exif_transpose(raw_img)

        # 2. 颜色模式转 RGB
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        # 3. 限制长边像素上限
        w, h = img.size
        if max(w, h) > max_px:
            img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)

        # 4. 保存
        img.save(dst_p, quality=92, optimize=True)

    return dst_p


def compute_dhash(path: Path | str) -> int:
    """先 ImageOps.exif_transpose() 再计算 8x8 dHash（返回 64 位整数）。"""
    with Image.open(path) as raw_img:
        img: Image.Image = ImageOps.exif_transpose(raw_img)
        # 转灰度并缩放为 9x8
        gray = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        hash_val = 0
        for y in range(8):
            for x in range(8):
                p1 = int(gray.getpixel((x, y)))  # type: ignore[arg-type]
                p2 = int(gray.getpixel((x + 1, y)))  # type: ignore[arg-type]
                diff = p1 > p2
                hash_val = (hash_val << 1) | int(diff)
        return hash_val


def hamming_distance(a: int, b: int) -> int:
    """计算两个整数哈希值的汉明距离。"""
    return bin(a ^ b).count("1")
