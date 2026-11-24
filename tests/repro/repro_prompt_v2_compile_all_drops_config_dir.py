"""Repro: `shuati compile --stage all` drops --config-dir/--prompt-profile for 3c / 3d / 3e / 4.

Each pipeline stage falls back to its default config_dir=Path("config") (cwd-relative) and
prompt_profile=None, so custom prompt_versions (stage3c/3d/3e/4) in --config-dir are ignored.

Safe: temp dir only; human_mirror.dir redirected. Exit 0 = fixed, != 0 = bug present.
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
from shuati.pipeline import stage3_taxonomy, stage4_weave  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="repro_all_cfg_"))
    try:
        cfg = tmp / "config"
        shutil.copytree(ROOT / "config", cfg)
        st = (cfg / "settings.yaml").read_text(encoding="utf-8")
        st = st.replace('dir: "human/"', f'dir: "{(tmp / "human").as_posix()}"')
        (cfg / "settings.yaml").write_text(st, encoding="utf-8")

        seen: dict[str, dict[str, Any]] = {}

        def make_sync(name: str) -> Any:
            def f(conn: Any, *a: Any, **kw: Any) -> dict[str, Any]:
                seen[name] = kw
                return {}

            return f

        def make_async(name: str) -> Any:
            async def f(conn: Any, *a: Any, **kw: Any) -> dict[str, Any]:
                seen[name] = kw
                return {}

            return f

        stage3_taxonomy.run_stage3c_async = make_async("3c")  # type: ignore[assignment]
        stage3_taxonomy.run_stage3d_mount = make_sync("3d")  # type: ignore[assignment]
        stage3_taxonomy.run_compaction_async = make_async("compact")  # type: ignore[assignment]
        stage3_taxonomy.run_stage3e_async = make_async("3e")  # type: ignore[assignment]
        stage4_weave.run_stage4_weave_async = make_async("4")  # type: ignore[assignment]

        r = CliRunner().invoke(
            app,
            [
                "compile", "--stage", "all", "--mode", "api", "--mock",
                "--db", str(tmp / "t.db"), "--config-dir", str(cfg),
                "--prompt-profile", "default", "--json",
            ],
        )
        print("exit_code:", r.exit_code, "| stages reached:", sorted(seen))
        bad = [
            n for n in ("3c", "3d", "3e", "4")
            if n in seen and Path(str(seen[n].get("config_dir", ""))) != cfg
        ]
        for n in bad:
            print(f"stage {n}: config_dir={seen[n].get('config_dir')!r} prompt_profile={seen[n].get('prompt_profile')!r}")
        assert seen, "no stage reached; repro inconclusive"
        assert not bad, f"BUG: --stage all dropped custom config_dir for stages {bad}"
        print("PASS")
        return 0
    except AssertionError as e:
        print("FAIL:", e)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
