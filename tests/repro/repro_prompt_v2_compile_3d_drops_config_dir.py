"""Repro: `shuati compile --stage 3d` calls run_stage3d_mount without config_dir/prompt_profile.

Hence a custom prompt_versions.stage3d in --config-dir (or --prompt-profile) is silently ignored
and the hard-coded constant (s3d_v2) is used for s3d_hash / compile_state / skip decision.

Safe: all state in a temp dir; human_mirror.dir redirected to tmp/human.
Exit 0 = fixed, != 0 = bug present.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from shuati.cli.main import app  # noqa: E402
from shuati.pipeline import stage3_taxonomy  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="repro_3d_cfg_"))
    try:
        cfg = tmp / "config"
        shutil.copytree(ROOT / "config", cfg)
        st = (cfg / "settings.yaml").read_text(encoding="utf-8")
        st = st.replace('dir: "human/"', f'dir: "{(tmp / "human").as_posix()}"')
        (cfg / "settings.yaml").write_text(st, encoding="utf-8")
        dy = cfg / "prompts" / "default.yaml"
        txt = dy.read_text(encoding="utf-8")
        assert 'stage3d: "s3d_v2"' in txt, "default.yaml stage3d key not found"
        dy.write_text(txt.replace('stage3d: "s3d_v2"', 'stage3d: "s3d_custom_99"'), encoding="utf-8")

        seen: dict[str, Any] = {}

        def fake_mount(conn: Any, **kw: Any) -> dict[str, Any]:
            seen.update(kw)
            return {"mounted": 0}

        stage3_taxonomy.run_stage3d_mount = fake_mount  # type: ignore[assignment]
        r = CliRunner().invoke(
            app,
            ["compile", "--stage", "3d", "--db", str(tmp / "t.db"), "--config-dir", str(cfg), "--json"],
        )
        print("exit_code:", r.exit_code, "| kwargs seen by run_stage3d_mount:", sorted(seen))
        ok = Path(str(seen.get("config_dir", ""))) == cfg
        assert ok, f"BUG: run_stage3d_mount did not receive custom config_dir={cfg}; got {seen.get('config_dir')!r}"
        print("PASS")
        return 0
    except AssertionError as e:
        print("FAIL:", e)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
