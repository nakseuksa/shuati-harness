"""SQLite 连接工厂、单写者锁与跨进程编译排他锁。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any


class PreconditionError(Exception):
    """前置条件未满足异常（对应退出码 EXIT_PRECONDITION=2）。"""
    pass


def get_connection(db_path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    """创建并配置 SQLite 连接。

    统一开启：
    - PRAGMA foreign_keys = ON
    - PRAGMA journal_mode = WAL
    - PRAGMA synchronous = NORMAL
    - PRAGMA busy_timeout = 5000
    - sqlite3.Row 结果集字典映射
    """
    path_str = str(db_path)
    if not readonly:
        os.makedirs(os.path.dirname(os.path.abspath(path_str)), exist_ok=True)

    if readonly:
        uri = f"file:{Path(path_str).resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(path_str, timeout=5.0)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    if not readonly:
        from shuati.core.effective import install_eff_write_guard

        install_eff_write_guard(conn)
    return conn


class CompileLock:
    """跨进程独占编译锁（data/.compile.lock，NFR-2 & DESIGN.md §4.1）。

    Windows 使用 msvcrt.locking，Unix 使用 fcntl.flock。
    记录 PID 与启动时间，支持同线程可重入。获取失败抛 PreconditionError("PRECONDITION_FAILED")。
    """

    _thread_local = threading.local()

    def __init__(self, lock_path: Path | str = "data/.compile.lock") -> None:
        self.lock_path = Path(lock_path)
        self._file_obj: Any = None

    def _get_depths(self) -> dict[str, int]:
        if not hasattr(self._thread_local, "depths"):
            self._thread_local.depths = {}
        depths: dict[str, int] = self._thread_local.depths
        return depths

    def acquire(self) -> None:
        key = str(self.lock_path.resolve())
        depths = self._get_depths()
        depth = depths.get(key, 0)
        if depth > 0:
            depths[key] = depth + 1
            return

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 打开文件用于读写
            f = open(self.lock_path, "a+b")
        except OSError as e:
            raise PreconditionError(f"PRECONDITION_FAILED: 无法打开编译锁文件 {self.lock_path}: {e}") from e

        if sys.platform == "win32":
            import msvcrt

            try:
                f.seek(0)
                # 尝试以非阻塞模式锁住首字节
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                f.close()
                raise PreconditionError("PRECONDITION_FAILED: 另一进程正在执行编译任务，编译锁已被占用") from e
        else:
            import fcntl

            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                f.close()
                raise PreconditionError("PRECONDITION_FAILED: 另一进程正在执行编译任务，编译锁已被占用") from e

        # 写入 PID 与启动时间
        f.seek(0)
        f.truncate()
        info = f"pid={os.getpid()}\nstart_time={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f.write(info.encode("utf-8"))
        f.flush()

        self._file_obj = f
        depths[key] = 1

    def release(self) -> None:
        key = str(self.lock_path.resolve())
        depths = self._get_depths()
        depth = depths.get(key, 0)
        if depth > 1:
            depths[key] = depth - 1
            return

        if self._file_obj is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self._file_obj.seek(0)
                    msvcrt.locking(self._file_obj.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_UN)
                self._file_obj.close()
            except OSError:
                pass
            finally:
                self._file_obj = None
        depths[key] = 0

    def __enter__(self) -> CompileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


class SingleWriterExecutor:
    """进程内单写者锁/执行器，串行化 SQLite 写事务，杜绝 database is locked。"""

    def __init__(self) -> None:
        self._sync_lock = threading.Lock()
        self._async_lock = asyncio.Lock()

    @contextmanager
    def transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """获取写锁并开启 IMMEDIATE 事务。"""
        with self._sync_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @asynccontextmanager
    async def async_transaction(self, conn: sqlite3.Connection) -> AsyncIterator[sqlite3.Connection]:
        """异步协程上下文写事务。"""
        async with self._async_lock:
            self._sync_lock.acquire()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            finally:
                self._sync_lock.release()
