"""Repro: preflight 将“仅有题号、无题干”的空题标题与章节标题伪造成题目，total_questions 虚高。

preflight 第 3 步 `elif not has_body and (含数字 ...)` 把任何无正文标题“降级为题干”：
  `## 1. `（空题，仅题号）-> 生成题干 "1." 的幻影题。
而基准解析器 parse_question_list_markdown 对该块丢弃（无题干无图）。
结果：预检报告 total_questions 比真实题目多，且仅含题号的幻影题会被送入批量诊断。

运行：.venv\\Scripts\\python.exe tests/repro/repro_seam_preflight_phantom_questions.py
期望修复后 Exit Code == 0。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from shuati.services.diagnose_batch import (  # noqa: E402
    parse_question_list_markdown,
    preflight_question_list_markdown,
)

CASES = {
    "空题(仅题号)": "## 1. \n\n---\n## 2. 有题\n这里是真正的题干文字\n",
    "空题(仅题号, 末尾)": "## 1. 有题\n这里是真正的题干文字\n\n---\n## 2.\n",
}


def main() -> int:
    bad = 0
    for label, raw in CASES.items():
        d = Path(tempfile.mkdtemp(prefix="repro_pf_"))
        src = d / "q.md"
        src.write_text(raw, encoding="utf-8")
        real = len(parse_question_list_markdown(src))
        rep = preflight_question_list_markdown(src, drafts_dir=d / "drafts")
        previews = [it["preview"] for it in rep["items"]]
        ok = rep["total_questions"] == real
        print(f"[{'OK ' if ok else 'BUG'}] {label}: parser={real} preflight={rep['total_questions']} previews={previews}")
        bad += 0 if ok else 1
    if bad:
        print(f"REPRO FAILED: {bad}/{len(CASES)} 场景 preflight 虚增题目")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
