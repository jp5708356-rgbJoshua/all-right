"""对账标记：货到 / 票到 / 款付 三个勾 + 对应日期。

打勾时自动填当天日期，取消打勾时清掉日期——日期是判断「货到超 7 天未收票」
这类待办的前提，不能留一个勾着但没有日期的状态。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from . import db

FLAGS = ("goods_received", "invoice_received", "payment_done")

# 勾选状态 -> 对应日期字段
DATE_FIELD = {
    "goods_received": "goods_received_date",
    "invoice_received": "invoice_received_date",
    "payment_done": "payment_date",
}

MODES = ("全部", "货到未收票", "票到未付款", "三单未齐")

_MODE_SQL = {
    "货到未收票": "AND COALESCE(r.goods_received, 0) = 1 AND COALESCE(r.invoice_received, 0) = 0",
    "票到未付款": "AND COALESCE(r.invoice_received, 0) = 1 AND COALESCE(r.payment_done, 0) = 0",
    "三单未齐": (
        "AND COALESCE(r.goods_received, 0) + COALESCE(r.invoice_received, 0) "
        "+ COALESCE(r.payment_done, 0) < 3"
    ),
}

_SELECT = """
    SELECT o.id AS order_id, o.order_no, o.status, o.order_date, o.currency,
           o.total_amount, o.base_amount, o.item_name_raw,
           s.name AS supplier_name,
           COALESCE(r.goods_received, 0)    AS goods_received,
           r.goods_received_date,
           COALESCE(r.invoice_received, 0)  AS invoice_received,
           r.invoice_received_date,
           COALESCE(r.payment_done, 0)      AS payment_done,
           r.payment_date,
           r.invoice_no
    FROM orders o
    JOIN suppliers s ON s.id = o.supplier_id
    LEFT JOIN reconciliation r ON r.order_id = o.id
    WHERE o.status <> '已取消'
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


def get_recon(order_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    return db.row("SELECT * FROM reconciliation WHERE order_id = ?", (order_id,), conn)


def is_complete(row: dict) -> bool:
    return all(int(row.get(flag) or 0) == 1 for flag in FLAGS)


def upsert(order_id: int, data: dict, conn: sqlite3.Connection | None = None) -> dict:
    """写入对账标记。data 里没给的勾保持原样。"""
    if not db.row("SELECT id FROM orders WHERE id = ?", (order_id,), conn):
        raise ValueError(f"订单 #{order_id} 不存在")

    current = get_recon(order_id, conn) or {
        "goods_received": 0,
        "goods_received_date": None,
        "invoice_received": 0,
        "invoice_received_date": None,
        "payment_done": 0,
        "payment_date": None,
        "invoice_no": None,
    }

    payload: dict = {}
    for flag in FLAGS:
        date_field = DATE_FIELD[flag]
        if flag not in data:
            payload[flag] = int(current[flag] or 0)
            payload[date_field] = current[date_field]
            continue

        new_value = 1 if data[flag] in (1, True, "1", "是") else 0
        payload[flag] = new_value

        if new_value == 0:
            # 取消打勾就把日期一起清掉，不留「勾没了但日期还在」的脏状态
            payload[date_field] = None
            continue

        explicit = data.get(date_field)
        if explicit:
            try:
                payload[date_field] = date.fromisoformat(str(explicit)[:10]).isoformat()
            except ValueError as exc:
                raise ValueError(f"{date_field} 需要 YYYY-MM-DD 格式") from exc
        else:
            # 刚打勾且没有日期 -> 记今天；已有日期则保留
            payload[date_field] = current[date_field] or _today()

    if "invoice_no" in data:
        payload["invoice_no"] = (data.get("invoice_no") or "").strip() or None
    else:
        payload["invoice_no"] = current.get("invoice_no")

    db.execute(
        """
        INSERT INTO reconciliation
            (order_id, goods_received, goods_received_date, invoice_received,
             invoice_received_date, payment_done, payment_date, invoice_no, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            goods_received = excluded.goods_received,
            goods_received_date = excluded.goods_received_date,
            invoice_received = excluded.invoice_received,
            invoice_received_date = excluded.invoice_received_date,
            payment_done = excluded.payment_done,
            payment_date = excluded.payment_date,
            invoice_no = excluded.invoice_no,
            updated_at = excluded.updated_at
        """,
        (
            order_id,
            payload["goods_received"],
            payload["goods_received_date"],
            payload["invoice_received"],
            payload["invoice_received_date"],
            payload["payment_done"],
            payload["payment_date"],
            payload["invoice_no"],
            _now(),
        ),
        conn,
    )
    return get_recon(order_id, conn)


def list_rows(
    mode: str = "全部",
    limit: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    if mode not in _MODE_SQL and mode != "全部":
        raise ValueError(f"未知筛选：{mode}")

    sql = _SELECT
    if mode in _MODE_SQL:
        sql += " " + _MODE_SQL[mode]
    sql += " ORDER BY o.order_date DESC, o.id DESC"
    if limit:
        sql += " LIMIT ?"

    params: tuple = (int(limit),) if limit else ()
    rows = db.rows(sql, params, conn)
    for row in rows:
        row["complete"] = is_complete(row)
        row["marks"] = "".join(
            "✅" if int(row[flag] or 0) else "⬜" for flag in FLAGS
        )
    return rows


def count_rows(mode: str = "全部", conn: sqlite3.Connection | None = None) -> int:
    if mode not in _MODE_SQL and mode != "全部":
        raise ValueError(f"未知筛选：{mode}")

    sql = "SELECT COUNT(*) FROM orders o LEFT JOIN reconciliation r ON r.order_id = o.id WHERE o.status <> '已取消'"
    if mode in _MODE_SQL:
        sql += " " + _MODE_SQL[mode]
    return int(db.scalar(sql, (), conn) or 0)


def count_incomplete(conn: sqlite3.Connection | None = None) -> int:
    return int(
        db.scalar(
            """
            SELECT COUNT(*) FROM orders o
            LEFT JOIN reconciliation r ON r.order_id = o.id
            WHERE o.status <> '已取消'
              AND COALESCE(r.goods_received, 0) + COALESCE(r.invoice_received, 0)
                  + COALESCE(r.payment_done, 0) < 3
            """,
            (),
            conn,
        )
        or 0
    )
