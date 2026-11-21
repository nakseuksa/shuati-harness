"""极简本地键盘复核 HTTP Server（仅 stdlib http.server，强制绑定 127.0.0.1）。"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from shuati.config import Settings, load_settings
from shuati.db.connection import PreconditionError, SingleWriterExecutor, get_connection
from shuati.db.repository import AmbiguousUidError, NotFoundError
from shuati.db.schema import init_db
from shuati.llm.gateway import LLMGatewayError
from shuati.services import facade

_ASSET_FILENAME_RE = re.compile(r"^[0-9a-f]{16}\.(jpg|jpeg|png|webp)$")
_REVIEW_HTML_PATH = Path(__file__).with_name("review.html")


class ReviewServerApp:
    """复核 HTTP Server 共享运行时上下文。"""

    def __init__(
        self,
        *,
        db_path: Path,
        config_dir: Path,
        assets_dir: Path,
        human_dir: Path,
        llm_raw_dir: Path,
        settings: Settings,
        default_grade_subject: str | None = None,
        default_audit_mode: bool = False,
    ) -> None:
        self.db_path = db_path
        self.config_dir = config_dir
        self.assets_dir = assets_dir
        self.human_dir = human_dir
        self.llm_raw_dir = llm_raw_dir
        self.settings = settings
        self.default_grade_subject = default_grade_subject
        self.default_audit_mode = default_audit_mode
        self.writer = SingleWriterExecutor()


def _make_handler(app_ctx: ReviewServerApp) -> type[BaseHTTPRequestHandler]:
    class _ReviewRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # 保持 stdout 干净，仅必要时打印到 stderr
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _send_error_json(self, status: int, code: str, message: str) -> None:
            self._send_json(status, {"ok": False, "code": code, "error": message})

        def _read_json_body(self) -> dict[str, Any]:
            length_hdr = self.headers.get("Content-Length", "0")
            try:
                length = int(length_hdr)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if length < 0 or length > 8 * 1024 * 1024:
                raise ValueError("Invalid request body size")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == "/" or path == "/index.html":
                if not _REVIEW_HTML_PATH.exists():
                    self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "review.html missing")
                    return
                body = _REVIEW_HTML_PATH.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path.startswith("/assets/"):
                raw_name = urllib.parse.unquote(path[len("/assets/") :])
                if not _ASSET_FILENAME_RE.fullmatch(raw_name):
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST,
                        "INVALID_ASSET_NAME",
                        "非法图片路径或文件名",
                    )
                    return
                assets_root = app_ctx.assets_dir.resolve()
                target = (assets_root / raw_name).resolve()
                if target.parent != assets_root or not target.is_file():
                    self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "图片文件不存在")
                    return
                mime, _ = mimetypes.guess_type(target.name)
                data = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/api/queue":
                qs = urllib.parse.parse_qs(parsed.query)
                gs_param = qs.get("grade_subject", [None])[0]
                grade_subject = gs_param if gs_param is not None else app_ctx.default_grade_subject
                audit_param = qs.get("audit", [None])[0]
                if audit_param is None:
                    audit_mode = app_ctx.default_audit_mode
                else:
                    audit_mode = audit_param.lower() in ("1", "true", "yes")
                try:
                    limit = int(qs.get("limit", ["100"])[0])
                except ValueError:
                    limit = 100

                conn = get_connection(app_ctx.db_path)
                try:
                    items = facade.list_review_queue(
                        conn,
                        grade_subject=grade_subject or None,
                        audit_mode=audit_mode,
                        limit=limit,
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "audit_mode": audit_mode,
                            "grade_subject": grade_subject,
                            "total": len(items),
                            "items": items,
                        },
                    )
                except Exception as exc:
                    self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", str(exc))
                finally:
                    conn.close()
                return

            if path.startswith("/api/item/"):
                raw_uid = urllib.parse.unquote(path[len("/api/item/") :])
                if not raw_uid:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "PRECONDITION_FAILED", "缺少题目 ID")
                    return
                conn = get_connection(app_ctx.db_path)
                try:
                    item = facade.get_review_item(conn, raw_uid)
                    self._send_json(HTTPStatus.OK, {"ok": True, "item": item})
                except NotFoundError as exc:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", str(exc))
                except AmbiguousUidError as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "AMBIGUOUS_UID", str(exc))
                except Exception as exc:
                    self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", str(exc))
                finally:
                    conn.close()
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Unknown route: {path}")

        def _validate_post_security(self) -> bool:
            """校验 POST 写请求的 Content-Type、Host 与 Origin，防御 CSRF 与 DNS rebinding。"""
            raw_ct = self.headers.get("Content-Type", "")
            media_type = raw_ct.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                self._send_error_json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "UNSUPPORTED_MEDIA_TYPE",
                    "POST 接口仅接受 Content-Type: application/json",
                )
                return False

            server_port = cast(ThreadingHTTPServer, self.server).server_address[1]
            allowed_hosts = {f"127.0.0.1:{server_port}", f"localhost:{server_port}"}

            host_hdr = (self.headers.get("Host") or "").strip().lower()
            if host_hdr and host_hdr not in allowed_hosts:
                self._send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "FORBIDDEN_HOST",
                    f"非法 Host 头: {host_hdr}",
                )
                return False

            origin_hdr = (self.headers.get("Origin") or "").strip()
            if origin_hdr:
                parsed_origin = urllib.parse.urlparse(origin_hdr)
                origin_host = (parsed_origin.netloc or "").lower()
                if parsed_origin.scheme != "http" or origin_host not in allowed_hosts:
                    self._send_error_json(
                        HTTPStatus.FORBIDDEN,
                        "FORBIDDEN_ORIGIN",
                        f"拒绝跨站 Origin: {origin_hdr}",
                    )
                    return False

            return True

        def do_POST(self) -> None:  # noqa: N802
            if not self._validate_post_security():
                return

            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "BAD_JSON", str(exc))
                return

            conn = get_connection(app_ctx.db_path)
            try:
                if path == "/api/adopt":
                    uid = str(body.get("question_uid") or "")
                    role = str(body.get("role") or "")
                    reason = str(body.get("reason") or "")
                    res = facade.adopt_run(
                        conn,
                        uid,
                        role=role,  # type: ignore[arg-type]
                        reason=reason,
                        source="REVIEW_UI",
                        human_dir=app_ctx.human_dir,
                        writer=app_ctx.writer,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "result": res})
                    return

                if path == "/api/edit":
                    uid = str(body.get("question_uid") or "")
                    raw_vf = body.get("visual_facts", "")
                    raw_md = body.get("solution_markdown", "")
                    raw_ms = body.get("method_summary", "")
                    raw_reason = body.get("reason", "")
                    raw_answers = body.get("final_answers")
                    raw_sympy = body.get("sympy_code")
                    if not isinstance(raw_vf, str) or not isinstance(raw_md, str) or not isinstance(raw_ms, str) or not isinstance(raw_reason, str):
                        raise PreconditionError("visual_facts / solution_markdown / method_summary / reason 必须为字符串")
                    if not isinstance(raw_answers, list) or len(raw_answers) == 0:
                        raise PreconditionError("final_answers 必须为非空数组")
                    if raw_sympy is not None and not isinstance(raw_sympy, str):
                        raise PreconditionError("sympy_code 必须为字符串或 null")
                    res = facade.submit_manual_review(
                        conn,
                        uid,
                        visual_facts=raw_vf,
                        final_answers=raw_answers,
                        solution_markdown=raw_md,
                        method_summary=raw_ms,
                        sympy_code=raw_sympy,
                        reason=raw_reason,
                        source="REVIEW_UI",
                        human_dir=app_ctx.human_dir,
                        writer=app_ctx.writer,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "result": res})
                    return

                if path == "/api/resolve":
                    uid = str(body.get("question_uid") or "")
                    draft = facade.resolve_review_draft(
                        conn,
                        uid,
                        visual_facts=str(body.get("visual_facts") or ""),
                        settings=app_ctx.settings,
                        llm_raw_dir=app_ctx.llm_raw_dir,
                        writer=app_ctx.writer,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "draft": draft})
                    return

                if path == "/api/exclude":
                    uid = str(body.get("question_uid") or "")
                    res = facade.exclude_question(
                        conn,
                        uid,
                        reason=str(body.get("reason") or ""),
                        source="REVIEW_UI",
                        human_dir=app_ctx.human_dir,
                        writer=app_ctx.writer,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "result": res})
                    return

                if path == "/api/reconfirm":
                    uid = str(body.get("question_uid") or "")
                    res = facade.reconfirm_stale_overrides(
                        conn,
                        uid,
                        reason=str(body.get("reason") or ""),
                        source="REVIEW_UI",
                        human_dir=app_ctx.human_dir,
                        writer=app_ctx.writer,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "result": res})
                    return

                if path == "/api/undo":
                    bundle_id = str(body.get("bundle_id") or "")
                    res = facade.undo_override_bundle(
                        conn,
                        bundle_id,
                        human_dir=app_ctx.human_dir,
                        writer=app_ctx.writer,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "result": res})
                    return

                self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Unknown route: {path}")
            except NotFoundError as exc:
                self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", str(exc))
            except (PreconditionError, AmbiguousUidError, ValueError) as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "PRECONDITION_FAILED", str(exc))
            except LLMGatewayError as exc:
                self._send_error_json(HTTPStatus.BAD_GATEWAY, "LLM_ERROR", str(exc))
            except Exception as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", str(exc))
            finally:
                conn.close()

    return _ReviewRequestHandler


def create_review_server(
    *,
    db_path: Path,
    config_dir: Path = Path("config"),
    assets_dir: Path | None = None,
    human_dir: Path | None = None,
    grade_subject: str | None = None,
    audit_mode: bool = False,
    port: int = 8765,
    use_mock: bool | None = None,
) -> ThreadingHTTPServer:
    """构建绑定到 127.0.0.1 的 ThreadingHTTPServer 实例。"""
    db_p = Path(db_path)
    cfg_dir = Path(config_dir)
    settings = load_settings(cfg_dir / "settings.yaml")
    if use_mock is not None:
        settings.pipeline.use_mock_llm = use_mock

    if assets_dir is None:
        if db_p.name == "shuati.db" and db_p.parent.name == "data":
            resolved_assets = Path("data/assets/images")
        else:
            resolved_assets = db_p.parent / "assets" / "images"
    else:
        resolved_assets = Path(assets_dir)

    if db_p.name == "shuati.db" and db_p.parent.name == "data":
        resolved_llm_raw = Path("data/llm_raw")
    else:
        resolved_llm_raw = db_p.parent / "llm_raw"

    resolved_human = Path(human_dir) if human_dir is not None else Path(settings.human_mirror.dir)

    conn = get_connection(db_p)
    try:
        init_db(conn, config_dir=cfg_dir)
    finally:
        conn.close()

    ctx = ReviewServerApp(
        db_path=db_p,
        config_dir=cfg_dir,
        assets_dir=resolved_assets,
        human_dir=resolved_human,
        llm_raw_dir=resolved_llm_raw,
        settings=settings,
        default_grade_subject=grade_subject,
        default_audit_mode=audit_mode,
    )
    handler_cls = _make_handler(ctx)
    # 强制绑定 127.0.0.1（DESIGN.md §4.3-5）
    return ThreadingHTTPServer(("127.0.0.1", port), handler_cls)


def serve_review(
    *,
    db_path: Path,
    config_dir: Path = Path("config"),
    assets_dir: Path | None = None,
    human_dir: Path | None = None,
    grade_subject: str | None = None,
    audit_mode: bool = False,
    port: int = 8765,
) -> None:
    """阻塞运行本地复核页 HTTP 服务，按 Ctrl+C 优雅退出。"""
    server = create_review_server(
        db_path=db_path,
        config_dir=config_dir,
        assets_dir=assets_dir,
        human_dir=human_dir,
        grade_subject=grade_subject,
        audit_mode=audit_mode,
        port=port,
    )
    actual_port = server.server_address[1]
    print(f"复核页已启动: http://127.0.0.1:{actual_port}/ (Ctrl+C 退出)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
