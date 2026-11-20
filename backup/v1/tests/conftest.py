"""共享测试夹具：临时数据库 / 临时 CAS 目录（与真实 ./data 完全隔离）。"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """将行为日志重定向到临时目录，并在测试结束后摘除并关闭挂载的 handler。"""
    from backend.config import settings

    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(settings, "LOG_CONSOLE", False)
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_harness_handler", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """初始化一个临时 SQLite 数据库并返回路径。"""
    from backend.db.schema import init_db

    db_path = tmp_path / "harness.db"
    init_db(db_path)
    return db_path


@pytest.fixture()
def cas_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """将 CAS 资产目录指向临时路径，避免污染真实 ./data/assets。"""
    from backend.config import settings

    assets = tmp_path / "assets"
    monkeypatch.setattr(settings, "CAS_ASSETS_DIR", assets)
    return assets
