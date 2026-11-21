"""测试图片校验、CAS 归档与 dHash 汉明距离计算。"""

from pathlib import Path

from PIL import Image

from shuati.core.image_store import (
    archive_image,
    compute_dhash,
    hamming_distance,
    prepare_external_image,
    validate_image,
)


def test_validate_image_valid_and_missing(tmp_path: Path):
    # 1. 正常图片
    valid_img = tmp_path / "valid.png"
    im = Image.new("RGB", (200, 200), color="blue")
    im.save(valid_img)

    res = validate_image(valid_img)
    assert res.is_valid is True
    assert res.width == 200
    assert res.height == 200
    assert res.warnings == []

    # 2. 缺失文件
    missing_img = tmp_path / "missing.png"
    res_missing = validate_image(missing_img)
    assert res_missing.is_valid is False
    assert "FILE_NOT_FOUND" in res_missing.error

    # 3. 损坏文件
    corrupt_img = tmp_path / "corrupt.png"
    corrupt_img.write_bytes(b"not an image content")
    res_corrupt = validate_image(corrupt_img)
    assert res_corrupt.is_valid is False
    assert "CORRUPTED_IMAGE" in res_corrupt.error


def test_validate_image_dimension_warnings(tmp_path: Path):
    # 小于 120 像素告警
    small_img = tmp_path / "small.jpg"
    im = Image.new("RGB", (80, 150), color="red")
    im.save(small_img)

    res = validate_image(small_img)
    assert res.is_valid is True  # 告警不拒入
    assert any("IMAGE_DIMENSION_TOO_SMALL" in w for w in res.warnings)


def test_archive_image_cas_and_idempotence(tmp_path: Path):
    src_img = tmp_path / "origin.jpg"
    im = Image.new("RGB", (300, 300), color="green")
    im.save(src_img)

    cas_dir = tmp_path / "cas_store"
    fn1 = archive_image(src_img, target_dir=cas_dir)
    assert len(fn1) == 16 + 4  # 16 位哈希 + .jpg
    assert (cas_dir / fn1).exists()

    # 再次归档同一图片，文件名相同且幂等
    fn2 = archive_image(src_img, target_dir=cas_dir)
    assert fn1 == fn2


def test_prepare_external_image_resize_and_convert(tmp_path: Path):
    src_rgba = tmp_path / "external_large.png"
    im = Image.new("RGBA", (3000, 1500), color=(255, 0, 0, 128))
    im.save(src_rgba)

    dst_jpg = tmp_path / "processed.jpg"
    prepare_external_image(src_rgba, dst_jpg, max_px=1000)

    assert dst_jpg.exists()
    with Image.open(dst_jpg) as out:
        assert out.mode == "RGB"
        assert max(out.width, out.height) <= 1000


def test_dhash_and_hamming_distance(tmp_path: Path):
    img_a = tmp_path / "im_a.jpg"
    img_b = tmp_path / "im_b.jpg"
    img_diff = tmp_path / "im_diff.jpg"

    # 生成类似图片与差异图片
    im1 = Image.new("L", (100, 100), color=50)
    for i in range(50):
        for j in range(100):
            im1.putpixel((i, j), 200)
    im1.save(img_a)

    im2 = im1.copy()
    im2.putpixel((0, 0), 0)  # 微小差异
    im2.save(img_b)

    im3 = Image.new("L", (100, 100), color=10)
    im3.save(img_diff)

    ha = compute_dhash(img_a)
    hb = compute_dhash(img_b)
    hdiff = compute_dhash(img_diff)

    dist_ab = hamming_distance(ha, hb)
    dist_adiff = hamming_distance(ha, hdiff)

    assert dist_ab <= 4
    assert dist_adiff > dist_ab
