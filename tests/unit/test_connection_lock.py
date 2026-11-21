"""测试数据库连接配置、单写者执行器与跨进程编译排他锁。"""

import threading

import pytest

from shuati.db.connection import (
    CompileLock,
    PreconditionError,
    SingleWriterExecutor,
    get_connection,
)


def test_get_connection_pragmas(tmp_path):
    db_file = tmp_path / "test_pragmas.db"
    conn = get_connection(db_file)

    fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    assert fk == 1

    busy = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    assert busy == 5000

    row = conn.execute("SELECT 1 AS val;").fetchone()
    assert row["val"] == 1


def test_compile_lock_reentrancy_and_mutual_exclusion(tmp_path):
    lock_file = tmp_path / ".compile.lock"

    # 1. 同一线程可重入
    lock1 = CompileLock(lock_file)
    with lock1:
        assert lock_file.exists()
        with lock1:
            pass  # 重入成功

    # 2. 模拟跨线程/跨进程互斥
    lock_a = CompileLock(lock_file)
    lock_b = CompileLock(lock_file)

    thread_result = []

    def try_lock_b():
        try:
            with lock_b:
                thread_result.append("ACQUIRED")
        except PreconditionError:
            thread_result.append("PRECONDITION_FAILED")

    with lock_a:
        t = threading.Thread(target=try_lock_b)
        t.start()
        t.join()

    # 在 lock_a 持有期间，lock_b 必须抛出 PreconditionError
    assert thread_result == ["PRECONDITION_FAILED"]


def test_single_writer_executor(tmp_path):
    db_file = tmp_path / "test_single_writer.db"
    conn = get_connection(db_file)
    conn.execute("CREATE TABLE counter (val INT);")
    conn.execute("INSERT INTO counter VALUES (0);")
    conn.commit()

    executor = SingleWriterExecutor()

    # 正常提交事务
    with executor.transaction(conn):
        conn.execute("UPDATE counter SET val = val + 1;")

    cur = conn.execute("SELECT val FROM counter;").fetchone()
    assert cur["val"] == 1

    # 异常回滚事务
    with pytest.raises(RuntimeError):
        with executor.transaction(conn):
            conn.execute("UPDATE counter SET val = val + 10;")
            raise RuntimeError("故意失败")

    cur2 = conn.execute("SELECT val FROM counter;").fetchone()
    assert cur2["val"] == 1  # 保持 1，未被修改为 11
