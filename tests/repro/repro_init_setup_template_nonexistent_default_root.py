"""Repro: `skills/shuati-init/references/setup_templates.md` §2 retains gitignored `"题库"` alongside external paths, crashing cold-start `shuati ingest --dry-run` and failing `doctor` `checks.ingest_roots.ok` on any fresh `git clone`.

On a fresh `git clone`, `./题库` does not exist because `题库/` is in `.gitignore`.
`setup_templates.md` §2 instructs configuring `settings.yaml` with:
  ingest:
    roots:
      - "题库"
      - "D:/我的题库/初中物理"
When the user substitutes their real external directory for `"D:/..."` on a fresh clone (without a local `./题库` directory):
1. `shuati ingest --dry-run --dedup-near --json` fails with `EXIT_PRECONDITION` (`题库根目录 '题库' 不存在`);
2. `shuati doctor --mock --json` sets `data.checks.ingest_roots.ok == False`, blocking the Step 2 gate in `SKILL.md` line 86.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from shuati.cli.main import app  # noqa: E402


def _extract_section2_yaml(md_text: str) -> dict[str, object]:
    sec_match = re.search(
        r"##\s*2\.\s*`config/settings\.yaml`.*?```yaml\r?\n(.*?)```",
        md_text,
        re.DOTALL,
    )
    assert sec_match is not None, "setup_templates.md 缺少 §2 settings.yaml yaml 代码块"
    parsed = yaml.safe_load(sec_match.group(1))
    assert isinstance(parsed, dict) and "ingest" in parsed
    return parsed


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    templates_path = REPO_ROOT / "skills" / "shuati-init" / "references" / "setup_templates.md"
    snippet = _extract_section2_yaml(templates_path.read_text(encoding="utf-8"))
    ingest_block = snippet["ingest"]
    assert isinstance(ingest_block, dict)
    raw_roots = list(ingest_block.get("roots") or [])
    assert raw_roots, "setup_templates.md §2 ingest.roots 不能为空"

    runner = CliRunner()
    failures: list[str] = []
    orig_cwd = Path.cwd()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Simulate fresh git clone: config/ exists, `./题库` does NOT exist!
        os.chdir(tmp)
        try:
            cfg_dir = tmp / "config"
            shutil.copytree(REPO_ROOT / "config", cfg_dir)

            # Map any external placeholder paths (e.g. D:/... or /...) in the template to real external folders
            mapped_roots: list[str] = []
            for idx, r in enumerate(raw_roots):
                r_str = str(r)
                if ":" in r_str or r_str.startswith("/"):
                    ext_dir = tmp / f"mounted_external_{idx}"
                    (ext_dir / "ch01_demo").mkdir(parents=True, exist_ok=True)
                    (ext_dir / "ch01_demo" / "problems.jsonl").write_text(
                        json.dumps({"id": "q1", "question": "计算 1 + 2 等于多少？"}, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    mapped_roots.append(ext_dir.as_posix())
                else:
                    mapped_roots.append(r_str)

            settings_path = cfg_dir / "settings.yaml"
            txt = settings_path.read_text(encoding="utf-8")
            roots_yaml = "\n".join(f'    - "{p}"' for p in mapped_roots)
            old_block = 'ingest:\n  roots:\n    - "题库"\n  dedup_near_default: true'
            new_block = f"ingest:\n  roots:\n{roots_yaml}\n  dedup_near_default: true"
            assert old_block in txt
            settings_path.write_text(txt.replace(old_block, new_block), encoding="utf-8")

            db_path = tmp / "data" / "shuati.db"

            # Step 1.4 of SKILL.md: `shuati ingest --dry-run --dedup-near --json`
            r_ing = runner.invoke(
                app,
                [
                    "ingest",
                    "--dry-run",
                    "--dedup-near",
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            if r_ing.exit_code != 0:
                failures.append(
                    f"FAIL 1: Applying `setup_templates.md` §2 `ingest.roots={raw_roots}` on a fresh clone "
                    f"(where gitignored `./题库` does not exist) crashed `shuati ingest --dry-run` "
                    f"with exit_code={r_ing.exit_code}: {r_ing.stdout.strip()}"
                )

            # Step 2.3 of SKILL.md: `shuati doctor --mock --json` -> checks.ingest_roots.ok must be True
            r_doc = runner.invoke(
                app,
                [
                    "doctor",
                    "--mock",
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            assert r_doc.exit_code == 0, r_doc.output
            doc_data = json.loads(r_doc.stdout)["data"]
            if not doc_data["checks"]["ingest_roots"]["ok"]:
                failures.append(
                    f"FAIL 2: Applying `setup_templates.md` §2 `ingest.roots={raw_roots}` on a fresh clone "
                    f"caused `doctor` `checks.ingest_roots.ok == False`, blocking Step 2 gate in SKILL.md: "
                    f"{doc_data['checks']['ingest_roots']}"
                )
        finally:
            os.chdir(orig_cwd)

    if failures:
        for f in failures:
            print(f, file=sys.stderr)
        return 1

    print("PASS: setup_templates.md §2 ingest.roots works cleanly on a fresh git clone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
