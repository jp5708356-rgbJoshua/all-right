"""SQLite 连接、事务与建表。

三个约定：

1. **每线程一个连接**。`check_same_thread=False` 只是关掉 sqlite3 模块的线程
   归属检查，它本身不提供任何线程安全保证。Streamlit 每个会话在自己的线程里
   跑脚本，共享一个 Connection 容易出现游标互相干扰。改成 threading.local 后
   每个线程拿自己的连接，写冲突交给 WAL + busy_timeout 处理。

2. **多步写操作走事务**。`db.execute` 默认写一次提交一次，但「插报价 + 取消
   同物料其它中选 + 重算历史价」这类组合如果中途失败，会留下不一致数据。
   用 `with db.transaction():` 包起来，嵌套时只由最外层提交。

3. **建表脚本幂等**，重复执行不报错、不丢数据。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import tempfile
from collections.abc import Iterator
from pathlib import Path

from . import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_JOURNAL_MODES = {"WAL", "MEMORY", "TRUNCATE", "PERSIST", "DELETE", "OFF"}

# 每线程独立连接；depth 记录事务嵌套层数，>0 时 execute 不自动提交
_local = threading.local()


def _default_journal_mode() -> str:
    """默认 WAL。放在网络盘 / 云同步目录上 WAL 会报 disk I/O error，
    这时可以设环境变量 PDESK_JOURNAL_MODE=MEMORY 应急。"""
    mode = os.environ.get("PDESK_JOURNAL_MODE", "WAL").upper()
    return mode if mode in _JOURNAL_MODES else "WAL"


def connect(path: Path, journal_mode: str | None = None) -> sqlite3.Connection:
    """按指定路径建连接，测试和临时库用这个。

    journal_mode 默认读环境变量或 WAL（正常使用）；沙箱或只读介质下可传 MEMORY / OFF。
    """
    mode = (journal_mode or _default_journal_mode()).upper()
    if mode not in _JOURNAL_MODES:
        raise ValueError(f"不支持的 journal_mode：{mode}")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA journal_mode = {mode}")
    conn.execute("PRAGMA busy_timeout = 5000")
    # 写多的场景下 NORMAL 已经够安全，比 FULL 少一次 fsync
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def get_conn() -> sqlite3.Connection:
    """当前线程的连接。切换数据库路径时自动重连。"""
    key = str(config.db_path())
    conn = getattr(_local, "conn", None)

    if conn is not None and getattr(_local, "key", None) == key:
        return conn

    if conn is not None:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    conn = connect(Path(key))
    _local.conn = conn
    _local.key = key
    _local.depth = 0
    return conn


def close_conn() -> None:
    """关掉当前线程的连接。主要给脚本和测试收尾用。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    _local.conn = None
    _local.key = None
    _local.depth = 0


def _depth() -> int:
    return int(getattr(_local, "depth", 0))


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """把多条写操作包成一个原子事务。

    嵌套时只有最外层提交或回滚；内层抛异常会一路冒泡到最外层统一回滚。
    """
    c = conn or get_conn()
    outer_depth = _depth()
    _local.depth = outer_depth + 1
    try:
        yield c
    except Exception:
        _local.depth = outer_depth
        if outer_depth == 0:
            c.rollback()
        raise
    else:
        _local.depth = outer_depth
        if outer_depth == 0:
            c.commit()


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """建表（幂等）。启动时调一次即可。"""
    c = conn or get_conn()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.commit()
    return c


# ---- 查询辅助：全部返回 dict 友好的结构 ----

def rows(sql: str, params: tuple | dict = (), conn: sqlite3.Connection | None = None) -> list[dict]:
    cur = (conn or get_conn()).execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def row(sql: str, params: tuple | dict = (), conn: sqlite3.Connection | None = None) -> dict | None:
    cur = (conn or get_conn()).execute(sql, params)
    found = cur.fetchone()
    return dict(found) if found else None


def scalar(sql: str, params: tuple | dict = (), conn: sqlite3.Connection | None = None):
    cur = (conn or get_conn()).execute(sql, params)
    found = cur.fetchone()
    return found[0] if found else None


def execute(sql: str, params: tuple | dict = (), conn: sqlite3.Connection | None = None) -> int:
    """执行写操作，返回 lastrowid。

    在 `with transaction()` 内部调用时不提交，交给最外层统一提交。
    """
    c = conn or get_conn()
    cur = c.execute(sql, params)
    if _depth() == 0:
        c.commit()
    return int(cur.lastrowid or 0)


def get_meta(key: str, default: str | None = None) -> str | None:
    value = scalar("SELECT value FROM schema_meta WHERE key = ?", (key,))
    return value if value is not None else default


def set_meta(key: str, value: str) -> None:
    execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---- 维护与备份 ----

def db_size(path: Path | None = None) -> int:
    """数据库主文件字节数。WAL 里未落盘的部分不算在内。"""
    target = Path(path) if path else config.db_path()
    return target.stat().st_size if target.exists() else 0


def maintenance(conn: sqlite3.Connection | None = None, path: Path | None = None) -> dict:
    """整理数据库：optimize 更新统计信息，VACUUM 回收空间。

    两者都不能在事务里执行，所以直接走连接。
    path 只在统计文件大小时用，默认取当前配置的库路径。
    """
    c = conn or get_conn()
    target = Path(path) if path else config.db_path()
    before = db_size(target)

    c.execute("PRAGMA optimize")
    c.commit()
    c.execute("VACUUM")
    c.commit()

    after = db_size(target)
    return {"size_before": before, "size_after": after}


def snapshot(target: Path, conn: sqlite3.Connection | None = None) -> Path:
    """导出一致性快照。

    WAL 模式下直接复制 .db 可能漏掉还在 -wal 文件里的数据，
    用 VACUUM INTO 生成一个自包含的单文件，复制走就能用。
    """
    c = conn or get_conn()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if c.in_transaction:
        raise ValueError("请先提交或回滚当前事务，再导出快照")
    for database in c.execute("PRAGMA database_list"):
        if database[2] and Path(database[2]).resolve() == target.resolve():
            raise ValueError("快照不能覆盖正在使用的数据库")
    # 先在同一目录生成并验证，再原子替换，失败时保留上一次备份。
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".db", dir=target.parent)
    os.close(fd)
    staging = Path(temporary)
    try:
        staging.unlink()
        c.execute("VACUUM INTO ?", (str(staging),))
        with contextlib.closing(sqlite3.connect(str(staging))) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("快照完整性检查失败")
            if check.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("快照存在外键错误")
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return target
