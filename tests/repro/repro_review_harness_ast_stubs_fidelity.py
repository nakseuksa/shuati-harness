"""Repro #2: AST stub extractor syntax safety, decorator/dunder fidelity, and TYPE_CHECKING imports."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_harness as rh  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_repro_docstring_trailing_quote_or_backslash_produces_valid_python_ast(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "src/shuati/__init__.py", "")
    _write(
        tmp_path / "src/shuati/helper.py",
        "def norm_label(s: str) -> str:\n"
        '    \'\'\'将单问标签统一归一为 "主问"\'\'\'\n'
        "    return s\n\n"
        "class TexBox:\n"
        '    """剥离末尾反斜杠 \\\\"""\n'
        "    raw: str = ''\n",
    )
    _write(
        tmp_path / "src/shuati/caller.py",
        "from shuati.helper import TexBox, norm_label\n\n"
        "def run(b: TexBox) -> str:\n"
        "    return norm_label(b.raw)\n",
    )

    stubs = rh.extract_dependency_stubs(tmp_path, [{"path": "src/shuati/caller.py"}])
    assert "def norm_label" in stubs
    assert "class TexBox:" in stubs
    # 生成的 §5 Python 代码块必须是合法可解析的 Python 语法，不能产生 """..." """ -> """" SyntaxError
    ast.parse(stubs)


def test_repro_semantic_decorators_and_context_manager_dunders_preserved(tmp_path: Path) -> None:
    # 1. 验证真实仓库 B14 中 SingleWriterExecutor 与 CompileLock 的上下文管理器契约不丢失
    plan = rh.load_plan_data(REPO_ROOT)
    b14 = next(b for b in plan["batches"] if b["id"] == "B14")
    b14_stubs = rh.extract_dependency_stubs(REPO_ROOT, b14["targets"])
    assert "@contextmanager" in b14_stubs
    assert "@asynccontextmanager" in b14_stubs
    assert "def __enter__(" in b14_stubs
    assert "def __exit__(" in b14_stubs

    # 2. 验证真实仓库 B01 中 DifficultyResult (若被引用) 及合成仓库中 @dataclass/@classmethod/@staticmethod/@property 保留
    _write(tmp_path / "src/shuati/__init__.py", "")
    _write(
        tmp_path / "src/shuati/defs.py",
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True)\n"
        "class ScoreCard:\n"
        "    elo: int\n"
        "    tier: str\n"
        "    @classmethod\n"
        "    def default(cls) -> 'ScoreCard':\n"
        "        return cls(1200, 'L2')\n"
        "    @staticmethod\n"
        "    def clamp(v: int) -> int:\n"
        "        return max(800, v)\n"
        "    @property\n"
        "    def is_high(self) -> bool:\n"
        "        return self.elo >= 1800\n",
    )
    _write(
        tmp_path / "src/shuati/consumer.py",
        "from shuati.defs import ScoreCard\n\n"
        "def check() -> bool:\n"
        "    card = ScoreCard(1500, 'L3')\n"
        "    return card.is_high or ScoreCard.clamp(card.elo) > 0\n",
    )

    syn_stubs = rh.extract_dependency_stubs(tmp_path, [{"path": "src/shuati/consumer.py"}])
    ast.parse(syn_stubs)
    assert "@dataclass(frozen=True)" in syn_stubs
    assert "@classmethod" in syn_stubs
    assert "@staticmethod" in syn_stubs
    assert "@property" in syn_stubs


def test_repro_type_checking_imports_resolved_for_partial_slice_and_two_part_import(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "src/shuati/__init__.py", "APP_VERSION = '2.0'\n")
    _write(
        tmp_path / "src/shuati/models.py",
        "class ItemDTO:\n"
        "    uid: str\n",
    )
    _write(
        tmp_path / "src/shuati/sliced_worker.py",
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "import shuati\n"
        "if TYPE_CHECKING:\n"
        "    from shuati.models import ItemDTO\n"
        "\n"
        "def part_one() -> int:\n"
        "    return 1\n"
        "\n"
        "def part_two(item: ItemDTO) -> str:\n"
        "    return f'{shuati.APP_VERSION}:{item.uid}'\n",
    )

    # 仅审查 L10-L11（不含 L4-L5 的 if TYPE_CHECKING 块）
    stubs = rh.extract_dependency_stubs(
        tmp_path,
        [{"path": "src/shuati/sliced_worker.py", "ranges": [[10, 11]]}],
    )
    assert "class ItemDTO:" in stubs
    assert "APP_VERSION" in stubs


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
