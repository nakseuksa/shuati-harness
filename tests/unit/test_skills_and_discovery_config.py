"""Unit tests for AGY skill discovery config, Skill frontmatter/links, gitignore, and docs."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "skills"
EXPECTED_SKILLS = ("shuati", "shuati-init", "shuati-compiler")

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#?\s]+)(?:#[^)\s]+)?\)")


def _parse_frontmatter(md_path: Path) -> tuple[dict[str, object], str]:
    text = md_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    assert match is not None, f"{md_path} 缺少合法 YAML frontmatter"
    parsed = yaml.safe_load(match.group(1))
    assert isinstance(parsed, dict), f"{md_path} frontmatter 必须为 YAML 映射"
    return parsed, text[match.end() :]


def test_agy_skills_json_discovers_all_three_skills() -> None:
    cfg_path = REPO_ROOT / ".agents" / "skills.json"
    assert cfg_path.is_file(), "缺少 .agents/skills.json"

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data == {"entries": [{"path": "skills"}]}

    discovered: set[str] = set()
    for entry in data["entries"]:
        base_dir = (REPO_ROOT / entry["path"]).resolve()
        assert base_dir.is_dir(), f"skills entry 目录不存在: {base_dir}"
        for child in base_dir.iterdir():
            if child.is_dir() and (child / "SKILL.md").is_file():
                discovered.add(child.name)

    assert set(EXPECTED_SKILLS).issubset(discovered)


@pytest.mark.parametrize("skill_name", EXPECTED_SKILLS)
def test_skill_frontmatter_and_relative_links(skill_name: str) -> None:
    skill_dir = SKILLS_DIR / skill_name
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"缺少 {skill_md}"

    meta, body = _parse_frontmatter(skill_md)
    assert meta.get("name") == skill_name
    desc = meta.get("description")
    assert isinstance(desc, str) and len(desc.strip()) >= 20

    for raw_target in _MD_LINK_RE.findall(body):
        if raw_target.startswith(("http://", "https://", "file://", "mailto:")):
            continue
        resolved = (skill_dir / raw_target).resolve()
        assert resolved.is_relative_to(SKILLS_DIR.resolve()), (
            f"{skill_md} 外链越界出 skills/: {raw_target!r}"
        )
        assert resolved.is_file(), f"{skill_md} 引用文件不存在: {raw_target!r} -> {resolved}"


def test_shuati_init_and_compiler_key_contracts() -> None:
    init_text = (SKILLS_DIR / "shuati-init" / "SKILL.md").read_text(encoding="utf-8")
    for required in (
        "Step 0",
        "Step 1",
        "Step 2",
        "Step 3",
        "Step 4",
        "Step 5",
        "shuati human reset",
        "shuati human restore",
        "shuati doctor",
        "shuati anchors sample",
        "shuati compile --stage 1",
        "shuati unified calibrate-anchors",
        "references/setup_templates.md",
    ):
        assert required in init_text, f"shuati-init/SKILL.md 缺少关键项: {required}"

    templates_path = SKILLS_DIR / "shuati-init" / "references" / "setup_templates.md"
    assert templates_path.is_file()
    templates_text = templates_path.read_text(encoding="utf-8")
    for section in ("EMBEDDING_BASE_URL", "ingest:", "rules:", "schema_version: 1", "anchors:"):
        assert section in templates_text, f"setup_templates.md 缺少模板片段: {section}"

    # Repro #4 回归断言：setup_templates.md §2 的 ingest.roots 必须使用外部目录示例，不得保留 gitignored 的 "题库"
    sec2_match = re.search(
        r"##\s*2\.\s*`config/settings\.yaml`.*?```yaml\r?\n(.*?)```",
        templates_text,
        re.DOTALL,
    )
    assert sec2_match is not None, "setup_templates.md 缺少 §2 settings.yaml yaml 代码块"
    sec2_yaml = yaml.safe_load(sec2_match.group(1))
    assert isinstance(sec2_yaml, dict) and isinstance(sec2_yaml.get("ingest"), dict)
    sec2_roots = list(sec2_yaml["ingest"].get("roots") or [])
    assert sec2_roots and "题库" not in [str(r).strip() for r in sec2_roots], (
        f"setup_templates.md §2 不得保留未跟踪占位项 '题库': {sec2_roots}"
    )
    assert "替换" in init_text and "替换" in templates_text

    compiler_text = (SKILLS_DIR / "shuati-compiler" / "SKILL.md").read_text(encoding="utf-8")
    for required in (
        "shuati status",
        "shuati ingest --dry-run",
        "路由 A",
        "路由 B",
        "路由 C",
        "路由 D",
        "gemini-4-neon-medium",
        "--role worker",
        "reindex --vectors --quantiles",
        "taxonomy lock",
    ):
        assert required in compiler_text, f"shuati-compiler/SKILL.md 缺少关键项: {required}"


def test_gitignore_allows_skills_json_and_rules_but_ignores_teamwork() -> None:
    git_bin = shutil.which("git")
    if git_bin is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("git 不可用或当前不在 Git 仓库内")

    def _is_ignored(rel_path: str) -> bool:
        proc = subprocess.run(
            [git_bin, "check-ignore", "--quiet", rel_path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise AssertionError(f"git check-ignore 异常退出 {proc.returncode}: {proc.stderr}")

    assert _is_ignored(".agents/skills.json") is False
    assert _is_ignored(".agents/rules/shuati_ops_lessons.md") is False
    assert _is_ignored(".agents/teamwork/foo.md") is True


def test_no_claude_md_and_docs_routing_present() -> None:
    assert not (REPO_ROOT / "CLAUDE.md").exists(), "按用户裁决禁止在根目录创建 CLAUDE.md"

    agents_text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for idx_marker in (
        "1. **权威文档（主从双层架构）**:",
        "2. **历史归档保护**:",
        "3. **确认优先**:",
        "4. **提交纪律**:",
        "5. **汇报、裁决与执行记录三件套落盘规范**:",
        "6. **批处理编排与 Windows 工程经验沉淀**:",
        "shuati-init",
        "shuati-compiler",
        "`shuati`",
    ):
        assert idx_marker in agents_text, f"AGENTS.md 缺少必要内容: {idx_marker}"

    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for marker in (
        ".agents/skills.json",
        "shuati skill install --all",
        "shuati-init",
        "shuati-compiler",
        "shuati doctor",
    ):
        assert marker in readme_text, f"README.md 缺少必要挂载/使用说明: {marker}"
