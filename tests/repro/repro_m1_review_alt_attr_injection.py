"""Repro: review.html 多图题注经 escapeHtml(未转义双引号)后拼入 alt="..." 属性 -> 属性注入/XSS。

image_captions 来自 OCR 语料（不可信）。题注含 `" onerror="..."` 时，渲染出的 <img> 带 onerror 处理器；
而 src 为不合规文件名时必然加载失败 -> 脚本执行，随后可同源调用 /api/* 写接口。
使用 node 执行页面真实的 escapeHtml 与 gallery 模板。期望：渲染结果中不得出现可执行的 onerror 属性。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

html = (Path(__file__).resolve().parents[2] / "src" / "shuati" / "review" / "review.html").read_text(
    encoding="utf-8"
)
fn = re.search(r"function escapeHtml\(str\) \{.*?\n\}", html, re.S)
tpl = re.search(r"gallery\.innerHTML = imgs\.map\(\(fn, idx\) => \{(.*?)\n    \}\)\.join\(\"\"\);", html, re.S)
assert fn and tpl, "未找到 escapeHtml 或 gallery 模板"

js = (
    fn.group(0)
    + r"""
const imgs = ["0123456789abcdef.png"];
const caps = ['x" onerror="window.__pwned=1'];
const out = imgs.map((fn, idx) => {"""
    + tpl.group(1)
    + r"""
}).join("");
console.log(out);
"""
)
node = shutil.which("node")
assert node, "需要 node"
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.js"
    p.write_text(js, encoding="utf-8")
    res = subprocess.run([node, str(p)], capture_output=True, text=True, timeout=30)
assert res.returncode == 0, res.stderr
out = res.stdout
print(out.strip())
# 在 <img ...> 标签内检测是否出现独立的 onerror= 属性（被引号截断后的注入）
img_tag = re.search(r"<img[^>]*>", out, re.S)
assert img_tag, "未渲染 img"
injected = re.search(r'"\s*onerror="', img_tag.group(0)) is not None
assert not injected, f"BUG: 题注注入出独立 onerror 属性: {img_tag.group(0)}"
print("PASS")
sys.exit(0)
