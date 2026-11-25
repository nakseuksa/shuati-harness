"""Repro: text_norm.py single-space horizontal choice fallback (1b) corrupts vertical math options and false-positives."""

from __future__ import annotations

from shuati.core.text_norm import detect_and_extract_choice_options, normalize_text


def test_repro_vertical_latex_options_not_split_inside_math() -> None:
    raw = (
        "下列式子恒成立的是：\n"
        "A. $(A + B) = 1$\n"
        "B. $(A - B) = 1$\n"
        "C. $A = 1$\n"
        "D. $B = 1$"
    )
    norm = normalize_text(raw, num_images=0).normalized_text
    is_choice, opts = detect_and_extract_choice_options(norm)
    assert is_choice is True
    assert opts == {
        "A": "$(A + B) = 1$",
        "B": "$(A - B) = 1$",
        "C": "$A = 1$",
        "D": "$B = 1$",
    }


def test_repro_vertical_coordinate_and_set_options_not_corrupted() -> None:
    raw_coords = (
        "若点 P 满足条件，则：\n"
        "A. (x, B) 在第一象限\n"
        "B. (x, C) 在第二象限\n"
        "C. (x, D) 在第三象限\n"
        "D. (x, E) 在第四象限"
    )
    norm_coords = normalize_text(raw_coords, num_images=0).normalized_text
    is_choice, opts = detect_and_extract_choice_options(norm_coords)
    assert is_choice is True
    assert opts == {
        "A": "(x, B) 在第一象限",
        "B": "(x, C) 在第二象限",
        "C": "(x, D) 在第三象限",
        "D": "(x, E) 在第四象限",
    }

    raw_set = (
        "下列命题正确的是：\n"
        "A. (A ∩ B) ≠ ∅\n"
        "B. (A ∪ B) = U\n"
        "C. A ⊆ B\n"
        "D. B ⊆ A"
    )
    norm_set = normalize_text(raw_set, num_images=0).normalized_text
    is_choice_s, opts_s = detect_and_extract_choice_options(norm_set)
    assert is_choice_s is True
    assert opts_s == {
        "A": "(A ∩ B) ≠ ∅",
        "B": "(A ∪ B) = U",
        "C": "A ⊆ B",
        "D": "B ⊆ A",
    }


def test_repro_single_line_interval_not_false_positive_choice() -> None:
    raw = "A. 区间 (a, B) 上单调递增"
    norm = normalize_text(raw, num_images=0).normalized_text
    is_choice, opts = detect_and_extract_choice_options(norm)
    assert is_choice is False
    assert opts == {}
