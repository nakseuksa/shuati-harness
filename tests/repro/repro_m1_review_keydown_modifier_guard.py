"""Repro: review.html 全局 keydown 处理器不检查 ctrl/meta/alt 修饰键。

Ctrl+R(刷新)→触发 [R] 付费 text_llm 调用；Ctrl+U(查看源码)→触发 U 撤销；Ctrl+X(剪切)→触发 X 下架；
Ctrl+S/Ctrl+B/Ctrl+K/Ctrl+1/2 同理。期望：带 ctrl/meta/alt 的组合键不触发任何业务动作。
使用 node 的 vm 抽取并执行真实 keydown 监听器（需要 node 在 PATH）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

html = (Path(__file__).resolve().parents[2] / "src" / "shuati" / "review" / "review.html").read_text(
    encoding="utf-8"
)
m = re.search(r'document\.addEventListener\("keydown",\s*\((ev)\)\s*=>\s*\{(.*?)\n\}\);', html, re.S)
assert m, "未找到 keydown 监听器"
body = m.group(2)

js = r"""
const calls = [];
const doAdopt = (r) => calls.push("adopt:" + r);
const toggleEditor = () => calls.push("toggleEditor");
const doResolve = () => calls.push("doResolve");
const doExclude = () => calls.push("doExclude");
const doSkip = () => calls.push("doSkip");
const doReconfirm = () => calls.push("doReconfirm");
const doUndo = () => calls.push("doUndo");
const doSaveEdit = () => calls.push("doSaveEdit");
const document = { activeElement: { tagName: "BODY", blur() {} } };
function handler(ev) {
__BODY__
}
const bad = [];
for (const key of ["r", "u", "x", "s", "b", "k", "e", "1", "2"]) {
  for (const mod of ["ctrlKey", "metaKey", "altKey"]) {
    calls.length = 0;
    handler({ key, [mod]: true, preventDefault() {} });
    if (calls.length) bad.push(mod + "+" + key + " -> " + calls.join(","));
  }
}
console.log(JSON.stringify(bad));
""".replace("__BODY__", body)

node = shutil.which("node")
assert node, "需要 node"
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.js"
    p.write_text(js, encoding="utf-8")
    out = subprocess.run([node, str(p)], capture_output=True, text=True, timeout=30)
assert out.returncode == 0, out.stderr
bad = json.loads(out.stdout.strip().splitlines()[-1])
print("modifier combos that trigger actions:", bad)
assert not bad, f"BUG: {len(bad)} 个修饰键组合误触发业务动作，例如 {bad[:4]}"
print("PASS")
sys.exit(0)
