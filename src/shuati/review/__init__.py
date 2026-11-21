"""极简本地键盘复核服务与单文件前端（DESIGN.md §4.3-5）。"""

from .server import create_review_server, serve_review

__all__ = ["create_review_server", "serve_review"]
