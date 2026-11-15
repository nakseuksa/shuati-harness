# P8-1A 任务契约：测试日志隔离与 Pytest 配置告警消除

## 上下文
当前测试套件运行后会将行为日志写入真实 `./data/logs/harness.jsonl`（导致测试污染真实环境日志），且 `pyproject.toml` 未显式声明 `asyncio_default_fixture_loop_scope` 产生 `PytestDeprecationWarning`。

## 修改方案

### 1. 全局测试日志目录隔离与 Handler 清理（`tests/conftest.py`）
在 `tests/conftest.py` 新增 `autouse=True` fixture `_isolate_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]`：
- setup：
  - `monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")`
  - `monkeypatch.setattr(settings, "LOG_CONSOLE", False)`
- yield 后 teardown：
  - 清理 `logging.getLogger().handlers` 中所有带有 `getattr(h, "_shuati_harness_log", False)` 标记的 handler，先 `removeHandler(h)` 再 `h.close()`（`try/except Exception` 吞掉关闭异常），防止跨测试句柄泄漏或写入已失效的临时路径。
- 保留 `tests/conftest.py` 既有的 `tmp_db` 和 `cas_dir` fixture 不变。
- 注意：`tests/test_observability.py` 中的局部 `_env` 和 `_cleanup_logging` 可保留兼容（不需改动其他测试文件）。

### 2. 消除 `pytest-asyncio` 弃用告警（`pyproject.toml`）
在 `[tool.pytest.ini_options]` 下新增：
```toml
asyncio_default_fixture_loop_scope = "function"
```

## 文件白名单
- `tests/conftest.py`
- `pyproject.toml`

**禁止**修改白名单之外的任何文件。**禁止**调用 `run_command`（由 PM 统一复跑门禁验收），**禁止** git commit/push。
