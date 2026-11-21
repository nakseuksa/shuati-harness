"""测试文本规范化、选项正则提取与重扫指纹（对照题库 122 题全量真实样本）。"""

import json
from pathlib import Path

import pytest

from shuati.core.text_norm import (
    compute_content_hash,
    compute_plain_fingerprint,
    detect_and_extract_choice_options,
    normalize_text,
)


def _load_all_problems():
    problems = []
    base_dir = Path("题库")
    if not base_dir.exists():
        pytest.skip("题库目录不存在，跳过真实题库测试")
    for jsonl_file in base_dir.rglob("problems.jsonl"):
        with open(jsonl_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    problems.append(json.loads(line))
    return problems


def test_text_norm_ocr_broken_words_and_captions():
    # 1. 汉字断词修复（ch28_010: "面\\n\\n积" -> "面积"）
    raw_010 = "图中两个边长为 8 的正方形如图摆放, A、B 分别为两个正方形的中心。那么, 正方形 CDEF 的面\n\n积是____。"
    norm_010 = normalize_text(raw_010, num_images=1)
    assert "面积是____。" in norm_010.normalized_text
    assert "面\n\n积" not in norm_010.normalized_text

    # 2. 汉字断词修复且不吞句末标点分段（ch28_030: "929500 平方米。小\\n\\n叶和小峰"）
    raw_030 = "929500 平方米。小\n\n叶和小峰的步行速度相同"
    norm_030 = normalize_text(raw_030, num_images=1)
    assert "小叶和小峰" in norm_030.normalized_text
    assert "平方米。小叶" in norm_030.normalized_text

    # 3. 保护正常分段（ch01_016 句末标点后的分段）
    raw_016 = "请用“积不变”的性质计算下列各式。\n\n(1) 1.25 \\times 80 ;\n\n(2) 4.4×620;"
    norm_016 = normalize_text(raw_016, num_images=0)
    assert "(1) 1.25" in norm_016.normalized_text
    assert "\n\n(2) 4.4" in norm_016.normalized_text

    # 4. 多图题注提取（ch01_012: 末尾提取 ["A", "B"]）
    raw_012 = "王师傅要给这两个房屋的内部铺满木地板, 那么两个房屋所铺木地板的面积相差____平方米。\n\nA\n\nB"
    norm_012 = normalize_text(raw_012, num_images=2)
    assert norm_012.image_captions == ["A", "B"]
    assert not norm_012.normalized_text.endswith("B")

    # 5. 多图题注提取（ch28_083: 末尾提取 ["图1", "图2"]）
    raw_083 = "如果AM=2BM,那么 $\\triangle ABC$ 的面积是多少?\n\n图1\n\n图2"
    norm_083 = normalize_text(raw_083, num_images=2)
    assert norm_083.image_captions == ["图1", "图2"]
    assert not norm_083.normalized_text.endswith("图2")

    # 6. 非多图题注末尾不被误提（ch28_050, ch28_091）
    raw_050 = "其单层纸片的面积是 ____ 平方厘米。"
    norm_050 = normalize_text(raw_050, num_images=1)
    assert norm_050.image_captions == []

    raw_091 = "如果正八边形的边长为18厘米，那么该“木铎”图形的面积是____平方厘米。"
    norm_091 = normalize_text(raw_091, num_images=2)
    assert norm_091.image_captions == []


def test_choice_option_extraction_across_all_122_questions():
    problems = _load_all_problems()
    assert len(problems) == 122, f"当前题库应为 122 题，实得 {len(problems)}"

    choice_uids = []
    for p in problems:
        is_choice, opts = detect_and_extract_choice_options(p["question"])
        if is_choice:
            choice_uids.append((p["id"], opts))

    # 必须精确命中 ch28_003 和 ch28_016，且对其余 120 题（包括 76 道含 A/B 几何字母题）零误伤
    matched_ids = [cid for cid, _ in choice_uids]
    assert matched_ids == ["ch28_003", "ch28_016"], f"选择题识别结果不符: {matched_ids}"

    # 验证提取的选项内容
    opts_003 = [opts for cid, opts in choice_uids if cid == "ch28_003"][0]
    assert opts_003["A"] == "1000 米"
    assert opts_003["D"] == "1700 米"

    opts_016 = [opts for cid, opts in choice_uids if cid == "ch28_016"][0]
    assert opts_016["A"] == "3"
    assert opts_016["D"] == "6"


def test_plain_fingerprint_and_content_hash():
    problems = {p["id"]: p for p in _load_all_problems()}

    # 同题重扫对 ch28_015 与 ch28_057 拥有相同 plain_fingerprint
    q15 = problems["ch28_015"]
    q57 = problems["ch28_057"]

    fp15 = compute_plain_fingerprint(q15["question"], len(q15.get("images", [])))
    fp57 = compute_plain_fingerprint(q57["question"], len(q57.get("images", [])))
    assert fp15 == fp57

    # content_hash 保留配图顺序（修改顺序应产生不同哈希）
    h1 = compute_content_hash("题干", ["img1.jpg", "img2.jpg"], ["图1", "图2"])
    h2 = compute_content_hash("题干", ["img2.jpg", "img1.jpg"], ["图1", "图2"])
    assert h1 != h2
