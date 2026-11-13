"""运行时结构化行为日志（P5 观测层，与 DB 审计账本互补）。

唯一权威规格：docs/tasks/P5-observability.md 架构规格 + 事件目录。

- ``setup_logging``：幂等挂载控制台（人类可读）与 RotatingFileHandler
  （``{LOG_DIR}/harness.jsonl``，JSON Lines）；重复调用不重复挂 handler。
- ``log_event``：结构化行为事件入口。record 附加 ``event`` 名与 ``fields``，
  自动附带 contextvar 中的 ``request_id``（如有）。
- ``request_id_var``：请求上下文（main.py 中间件每请求写入 uuid4）。

安全红线（契约 §1）：
- 绝不记录 LLM_API_KEY / Authorization 头 / 完整请求体：密钥字段名一律脱敏，
  字符串值中出现的真实密钥与 Bearer 令牌一律替换为 ``***REDACTED***``；
- 题干等内容字段截断 <= 120 字符（全文在 DB）；
- 任何序列化异常不得击穿业务：未知对象降级为 repr 截断，log_event 全程兜底。
"""

from __future__ import annotations

import json
import logging
import math
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from backend.config import settings

__all__ = [
    "CONTENT_MAX_CHARS",
    "ConsoleFormatter",
    "JsonFormatter",
    "log_event",
    "request_id_var",
    "setup_logging",
]

# 请求上下文：main.py 中间件每请求生成 uuid4 写入，响应头回传 X-Request-ID。
request_id_var: ContextVar[str] = ContextVar("harness_request_id", default="")

# 内容字段截断上限（契约安全红线：题干等 <= 120 字符，全文在 DB）。
CONTENT_MAX_CHARS = 120

_REDACTED = "***REDACTED***"
# 密钥类字段名：无论值是什么一律脱敏（防调用方误传）。
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "llm_api_key",
        "authorization",
        "auth",
        "auth_header",
        "token",
        "access_token",
        "password",
        "secret",
        "client_secret",
    }
)
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)
# JSON 保留键：fields 不得覆盖格式化器骨架字段。
_RESERVED_KEYS = frozenset({"ts", "level", "logger", "event", "request_id"})
# 本模块挂载的 handler 标记（幂等重装时按标记摘除旧 handler）。
_HANDLER_MARKER = "_harness_handler"


def _redact_secrets(text: str) -> str:
    """替换文本中出现的真实 API Key 与 Bearer 令牌（纵深防御，绝不记录密钥）。"""
    key = settings.LLM_API_KEY
    if key and key in text:
        text = text.replace(key, _REDACTED)
    return _BEARER_RE.sub(f"Bearer {_REDACTED}", text)


def _sanitize(value: Any) -> Any:
    """递归净化字段值：字符串截断 + 密钥脱敏；未知对象降级为 repr 截断。

    输出仅含 JSON 原生类型（str/int/float/bool/None/list/dict），
    非有限浮点转字符串，保证 json.dumps(allow_nan=False) 必然成功。
    """
    if isinstance(value, str):
        return _redact_secrets(value)[:CONTENT_MAX_CHARS]
    if isinstance(value, dict):
        return {str(k)[:CONTENT_MAX_CHARS]: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    try:
        return _redact_secrets(repr(value))[:CONTENT_MAX_CHARS]
    except Exception:
        return "<unserializable>"


def _json_default(obj: Any) -> str:
    """json.dumps 兜底：不可序列化对象降级为 repr 截断（契约防御性要求）。"""
    try:
        return _redact_secrets(repr(obj))[:CONTENT_MAX_CHARS]
    except Exception:
        return "<unserializable>"


def log_event(logger: logging.Logger, event: str, /, **fields: Any) -> None:
    """记录一条结构化行为事件（INFO 级）。

    record 附加 ``event`` / ``fields`` / ``request_id``（contextvar，如有）；
    字段值经 ``_sanitize`` 净化（截断 + 脱敏 + repr 降级）。
    任何异常不得击穿业务：最坏情况降级为无字段的事件名日志。
    """
    try:
        safe_fields: dict[str, Any] = {}
        for name, value in fields.items():
            if name.lower() in _SECRET_FIELD_NAMES:
                safe_fields[name] = _REDACTED
            else:
                safe_fields[name] = _sanitize(value)
        extra: dict[str, Any] = {"event": event, "fields": safe_fields}
        rid = request_id_var.get()
        if rid:
            extra["request_id"] = rid
        logger.info(event, extra=extra)
    except Exception:
        try:
            logger.info(event)
        except Exception:
            pass  # 日志绝不影响业务


class JsonFormatter(logging.Formatter):
    """JSON Lines 格式化器：ts, level, logger, event, request_id, ...fields。

    非事件记录输出 message；序列化失败降级为最小骨架 + repr 截断，绝不抛异常。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = str(event)
        rid = getattr(record, "request_id", None)
        if rid:
            payload["request_id"] = str(rid)
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key not in _RESERVED_KEYS:
                    payload[key] = value
        else:
            try:
                payload["message"] = record.getMessage()
            except Exception:
                payload["message"] = "<unprintable message>"
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[:2000]
        try:
            return json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_json_default)
        except Exception:
            fallback: dict[str, Any] = {
                "ts": payload["ts"],
                "level": record.levelname,
                "logger": record.name,
                "message": _json_default(record.getMessage()),
            }
            if event is not None:
                fallback["event"] = str(event)
            try:
                return json.dumps(fallback, ensure_ascii=False)
            except Exception:
                return '{"level": "ERROR", "message": "<log serialization failed>"}'


class ConsoleFormatter(logging.Formatter):
    """人类可读控制台格式：时间 级别 模块 事件 摘要（非事件记录退化为消息）。"""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        event = getattr(record, "event", None)
        if event is not None:
            fields = getattr(record, "fields", None) or {}
            summary = " ".join(f"{key}={value}" for key, value in fields.items())
            line = f"{ts} {record.levelname:<7} {record.name} {event} {summary}"
        else:
            try:
                message = record.getMessage()
            except Exception:
                message = "<unprintable message>"
            line = f"{ts} {record.levelname:<7} {record.name} {message}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging() -> None:
    """幂等挂载根日志 handler（重复调用不重复挂）。

    - 控制台：人类可读，``settings.LOG_CONSOLE`` 开关；
    - 文件：``{LOG_DIR}/harness.jsonl`` JSON Lines，``LOG_MAX_BYTES`` /
      ``LOG_BACKUP_COUNT`` 滚动；磁盘不可用时静默跳过（不阻塞启动）。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    root.setLevel(settings.LOG_LEVEL.upper())

    if settings.LOG_CONSOLE:
        console = logging.StreamHandler()
        console.setFormatter(ConsoleFormatter())
        setattr(console, _HANDLER_MARKER, True)
        root.addHandler(console)

    try:
        settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.LOG_DIR / "harness.jsonl",
            maxBytes=settings.LOG_MAX_BYTES,
            backupCount=settings.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        setattr(file_handler, _HANDLER_MARKER, True)
        root.addHandler(file_handler)
    except OSError:
        pass  # 日志盘不可用不得击穿业务启动

    # 第三方库降噪：httpx 的 INFO 请求流水与本观测层重复且可能携带 URL 细节。
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
