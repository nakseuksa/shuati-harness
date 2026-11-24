"""Repro: Cold-start `shuati human reset` (Step 0) loses configured learner identity and violates precondition atomicity.

Covers three concrete defects in the cold-start `human reset` flow:
1. Omitting `--human-dir` ignores `config/settings.yaml -> human_mirror.dir` (unlike `human restore` and `human verify`).
2. Following `skills/shuati-init/SKILL.md` Step 0 -> Step 1 -> Step 3 on a fresh clone (`data/shuati.db` absent):
   - Step 0 `shuati human reset --learner-id alice --learner-name "Alice" --subject "初中物理"` writes `alice` only to `human/learners.jsonl`;
   - Step 1 (`grade-subject add` + `ingest`) initializes `data/shuati.db` with only `default` (`小学奥数`), leaving `alice` absent from `learners` and breaking `shuati human verify`;
   - Step 3 (`shuati unified calibrate-anchors`) calls `human_mirror.export_snapshot`, overwriting `human/learners.jsonl` with `default` and permanently wiping out `alice`.
3. When `data/shuati.db` already exists and `--subject` is unregistered, `human reset` creates `<archive_dir>/seed_human_<ts>/` on disk BEFORE failing `PreconditionError`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from shuati.cli.main import app  # noqa: E402
from shuati.models.domain import EXIT_PRECONDITION  # noqa: E402


def _prepare_cold_clone_workspace(tmp: Path, ext_bank: Path) -> Path:
    cfg_dir = tmp / "config"
    shutil.copytree(REPO_ROOT / "config", cfg_dir)

    custom_human = tmp / "custom_human"
    custom_human.mkdir(parents=True, exist_ok=True)
    (custom_human / "learners.jsonl").write_text(
        json.dumps(
            {
                "created_at": "2026-01-01 00:00:00",
                "default_grade_subject": "小学奥数",
                "display_name": "原仓作者",
                "is_current": 1,
                "learner_id": "repo_owner",
                "notes": "",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    settings_path = cfg_dir / "settings.yaml"
    txt = settings_path.read_text(encoding="utf-8")
    txt = txt.replace('dir: "human/"', f'dir: "{custom_human.as_posix()}"')
    txt = txt.replace('- "题库"', f'- "{ext_bank.as_posix()}"')
    settings_path.write_text(txt, encoding="utf-8")
    return cfg_dir


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    runner = CliRunner()
    failures: list[str] = []
    orig_cwd = Path.cwd()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.chdir(tmp)
        try:
            ext_bank = tmp / "external_physics"
            (ext_bank / "初中物理" / "ch01_motion").mkdir(parents=True)
            rows = [
                {"id": f"p{i}", "question": f"某物体在 {i} 秒内匀速移动 {i * 10} 米，求速度是多少米每秒？"}
                for i in range(1, 6)
            ]
            (ext_bank / "初中物理" / "ch01_motion" / "problems.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )

            cfg_dir = _prepare_cold_clone_workspace(tmp, ext_bank)
            custom_human = tmp / "custom_human"
            db_path = tmp / "data" / "shuati.db"
            arch_dir = tmp / "data" / "backups"

            # Check 1: Omitting --human-dir should honor settings.human_mirror.dir (like restore & verify)
            r_reset = runner.invoke(
                app,
                [
                    "human",
                    "reset",
                    "--learner-id",
                    "alice",
                    "--learner-name",
                    "Alice",
                    "--subject",
                    "初中物理",
                    "--archive-dir",
                    str(arch_dir),
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            assert r_reset.exit_code == 0, f"human reset failed: {r_reset.output}"
            custom_learners_txt = (custom_human / "learners.jsonl").read_text(encoding="utf-8")
            if '"alice"' not in custom_learners_txt:
                failures.append(
                    "FAIL 1: `shuati human reset` ignored `settings.human_mirror.dir` when `--human-dir` was omitted "
                    f"(custom_human/learners.jsonl still has: {custom_learners_txt.strip()})"
                )
                r_reset2 = runner.invoke(
                    app,
                    [
                        "human",
                        "reset",
                        "--learner-id",
                        "alice",
                        "--learner-name",
                        "Alice",
                        "--subject",
                        "初中物理",
                        "--human-dir",
                        str(custom_human),
                        "--archive-dir",
                        str(arch_dir),
                        "--db",
                        str(db_path),
                        "--config-dir",
                        str(cfg_dir),
                        "--json",
                    ],
                )
                assert r_reset2.exit_code == 0, r_reset2.output

            # Step 1 of shuati-init: grade-subject add -> ingest
            r_gs = runner.invoke(
                app,
                [
                    "grade-subject",
                    "add",
                    "--name",
                    "初中物理",
                    "--slug",
                    "junior_physics",
                    "--family",
                    "PHYSICS",
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            assert r_gs.exit_code == 0, r_gs.output

            r_ing = runner.invoke(
                app,
                [
                    "ingest",
                    "--dedup-near",
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            assert r_ing.exit_code == 0, r_ing.output

            # Check 2a: After Step 1, `shuati learner list` and `shuati human verify` must have `alice`
            r_ll = runner.invoke(
                app,
                ["learner", "list", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
            )
            assert r_ll.exit_code == 0, r_ll.output
            learners_in_db = [it["learner_id"] for it in json.loads(r_ll.stdout)["data"]["learners"]]
            if "alice" not in learners_in_db:
                failures.append(
                    f"FAIL 2a: After Step 0 `human reset` + Step 1 `grade-subject add` & `ingest`, "
                    f"learner 'alice' is missing from DB `learners` table (got {learners_in_db})"
                )

            r_ver = runner.invoke(
                app,
                ["human", "verify", "--db", str(db_path), "--config-dir", str(cfg_dir), "--json"],
            )
            if r_ver.exit_code != 0:
                failures.append(
                    f"FAIL 2b: `shuati human verify` failed after Step 1 with exit_code={r_ver.exit_code}: "
                    f"{r_ver.stdout.strip()}"
                )

            # Step 3 of shuati-init: Stage 1 export/import 4 anchors -> unified calibrate-anchors
            s1_dir = tmp / "data" / "unified" / "stage3_4" / "stage1"
            anchor_uids = [f"junior_physics:初中物理/ch01_motion:p{i}" for i in range(1, 5)]
            r_exp = runner.invoke(
                app,
                [
                    "compile",
                    "--stage",
                    "1",
                    "--mode",
                    "export",
                    "--grade-subject",
                    "初中物理",
                    "--only-uids",
                    ",".join(anchor_uids),
                    "--tasks-dir",
                    str(s1_dir),
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            assert r_exp.exit_code == 0, r_exp.output
            tmap = json.loads((s1_dir / "stage1_task_map.json").read_text(encoding="utf-8"))
            s1_items = [
                {
                    "visual_facts": "",
                    "visual_ambiguity_flag": False,
                    "visual_ambiguity_note": "",
                    "method_summary": "速度公式",
                    "solution_markdown": "v = s / t = 10。",
                    "final_answers": [
                        {
                            "label": "主问",
                            "answer_type": "NUMERIC",
                            "verify_mode": "SYMBOLIC",
                            "collection_mode": "SINGLE",
                            "answer": "10",
                            "sympy_expr": "10",
                        }
                    ],
                    "sympy_code": "def verify():\n    return {'主问': str(20 // 2)}\n",
                    "task_item_id": tid,
                }
                for tid in tmap["items"]
            ]
            res_f = s1_dir / "results.jsonl"
            res_f.write_text(
                json.dumps({"batch_id": "stage1_batch_0001", "result": {"items": s1_items}}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            r_imp = runner.invoke(
                app,
                [
                    "compile",
                    "--stage",
                    "1",
                    "--mode",
                    "import",
                    "--tasks-dir",
                    str(s1_dir),
                    "--results",
                    str(res_f),
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            assert r_imp.exit_code == 0, r_imp.output

            anchors_yaml = cfg_dir / "anchors" / "anchors_junior_physics.yaml"
            anchors_yaml.parent.mkdir(parents=True, exist_ok=True)
            tiers = [
                ("L1_基础直达", 1, 1, 1, 1, 1120),
                ("L2_常规巩固", 1, 2, 1, 1, 1225),
                ("L3_综合中档", 2, 2, 2, 1, 1510),
                ("L4-A_进阶拔高(繁)", 2, 3, 2, 3, 1745),
            ]
            lines = ["grade_subject: 初中物理", 'version: "1.0"', "anchors:"]
            for uid, (tier, c, s, t, l_val, elo) in zip(anchor_uids, tiers, strict=True):
                lines.extend(
                    [
                        f'  - question_uid: "{uid}"',
                        f'    target_tier: "{tier}"',
                        "    scores:",
                        f"      C: {c}",
                        f"      S: {s}",
                        f"      T: {t}",
                        f"      L: {l_val}",
                        f"    base_elo: {elo}",
                        f'    rationale: "{tier}"',
                    ]
                )
            anchors_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")

            r_cal = runner.invoke(
                app,
                [
                    "unified",
                    "calibrate-anchors",
                    "--grade-subject",
                    "初中物理",
                    "--anchors-file",
                    str(anchors_yaml),
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            assert r_cal.exit_code == 0, r_cal.output

            after_cal_learners = (custom_human / "learners.jsonl").read_text(encoding="utf-8").strip()
            if '"alice"' not in after_cal_learners:
                failures.append(
                    "FAIL 2c: Step 3 `shuati unified calibrate-anchors` overwrote `learners.jsonl` and "
                    f"permanently wiped out cold-start learner 'alice'! Current learners.jsonl: {after_cal_learners}"
                )

            # Check 3: Precondition failure atomicity when DB exists and --subject is unregistered
            clean_arch = tmp / "clean_arch_on_fail"
            r_bad_subj = runner.invoke(
                app,
                [
                    "human",
                    "reset",
                    "--learner-id",
                    "bob",
                    "--subject",
                    "未注册高中化学",
                    "--human-dir",
                    str(custom_human),
                    "--archive-dir",
                    str(clean_arch),
                    "--db",
                    str(db_path),
                    "--config-dir",
                    str(cfg_dir),
                    "--json",
                ],
            )
            assert r_bad_subj.exit_code == EXIT_PRECONDITION
            if clean_arch.exists() and any(clean_arch.iterdir()):
                leaked = [p.name for p in clean_arch.iterdir()]
                failures.append(
                    f"FAIL 3: `shuati human reset` failed with EXIT_PRECONDITION for unregistered subject, "
                    f"but still created side-effect archive directory in {clean_arch}: {leaked}"
                )
        finally:
            os.chdir(orig_cwd)

    if failures:
        for msg in failures:
            print(msg, file=sys.stderr)
        return 1

    print("PASS: Cold-start human reset preserves learner identity and precondition atomicity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
