"""轻量指标：OTD、平均订单周期、异常订单占比。

只读计算，不落表、不改任何状态。口径固定如下，页面会原样展示，
方便手工核对：

- OTD（准时交付率）= 准时到货笔数 / 已到货且有承诺交期的笔数 × 100
  · 准时 = actual_delivery_date <= promised_delivery_date
  · promised_delivery_date 为空的订单排除出分母（不惩罚未承诺交期）
- 平均订单周期 = 已到货订单的 (actual_delivery_date - order_date) 平均值，单位天
- 异常订单占比 = 统计期内曾标记过异常的订单数 / 统计期订单总数 × 100
  · 已取消订单不进分母也不进分子，避免出现超过 100%
"""

from __future__ import annotations

import sqlite3

from . import db


def _range_clause(date_from: str | None, date_to: str | None, alias: str = "o") -> tuple[str, list]:
    clause = ""
    params: list = []
    if date_from:
        clause += f" AND {alias}.order_date >= ?"
        params.append(date_from)
    if date_to:
        clause += f" AND {alias}.order_date <= ?"
        params.append(date_to)
    return clause, params


def delivery_detail(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """参与 OTD 与周期计算的订单明细，用于手工核对。

    limit 只影响页面展示，不影响 compute() 的统计口径——
    统计走 SQL 聚合，不依赖这个列表。
    """
    clause, params = _range_clause(date_from, date_to)
    sql = f"""
        SELECT o.id, o.order_no, s.name AS supplier_name, o.item_name_raw,
               o.order_date, o.promised_delivery_date, o.actual_delivery_date,
               CASE WHEN o.promised_delivery_date IS NULL THEN NULL
                    WHEN o.actual_delivery_date <= o.promised_delivery_date THEN 1
                    ELSE 0 END AS on_time,
               CAST(julianday(o.actual_delivery_date) - julianday(o.order_date) AS INTEGER) AS cycle_days
        FROM orders o
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.actual_delivery_date IS NOT NULL
          AND o.status <> '已取消'
          {clause}
        ORDER BY o.actual_delivery_date DESC, o.id DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return db.rows(sql, tuple(params), conn)


def exception_detail(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """统计期内曾标记过异常的订单。limit 只影响展示，不影响 compute()。"""
    clause, params = _range_clause(date_from, date_to)
    sql = f"""
        SELECT o.order_no, s.name AS supplier_name, o.status,
               COUNT(e.id) AS exception_times,
               MAX(e.created_at) AS last_exception_at,
               GROUP_CONCAT(DISTINCT e.category) AS categories
        FROM order_exceptions e
        JOIN orders o ON o.id = e.order_id
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.status <> '已取消'
          {clause}
        GROUP BY o.id
        ORDER BY last_exception_at DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return db.rows(sql, tuple(params), conn)


def compute(
    date_from: str | None = None,
    date_to: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """统计走 SQL 聚合，不把明细拉到 Python 里数——
    五万单时拉全量明细要 60ms 以上，聚合查询只要几毫秒。"""
    clause, params = _range_clause(date_from, date_to)

    row = db.row(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN o.actual_delivery_date IS NOT NULL THEN 1 ELSE 0 END) AS delivered,
            SUM(CASE WHEN o.actual_delivery_date IS NOT NULL
                      AND o.promised_delivery_date IS NOT NULL THEN 1 ELSE 0 END) AS promise_sample,
            SUM(CASE WHEN o.actual_delivery_date IS NOT NULL
                      AND o.promised_delivery_date IS NOT NULL
                      AND o.actual_delivery_date <= o.promised_delivery_date THEN 1 ELSE 0 END) AS on_time,
            SUM(CASE WHEN o.actual_delivery_date IS NOT NULL
                     THEN julianday(o.actual_delivery_date) - julianday(o.order_date)
                     ELSE 0 END) AS cycle_sum
        FROM orders o
        WHERE o.status <> '已取消' {clause}
        """,
        tuple(params),
        conn,
    ) or {}

    total_orders = int(row.get("total") or 0)
    delivered = int(row.get("delivered") or 0)
    promise_sample = int(row.get("promise_sample") or 0)
    on_time = int(row.get("on_time") or 0)
    cycle_sum = float(row.get("cycle_sum") or 0.0)

    exception_orders = int(
        db.scalar(
            f"""
            SELECT COUNT(DISTINCT o.id)
            FROM order_exceptions e
            JOIN orders o ON o.id = e.order_id
            WHERE o.status <> '已取消' {clause}
            """,
            tuple(params),
            conn,
        )
        or 0
    )

    return {
        "total_orders": total_orders,
        "delivered_orders": delivered,
        "otd_sample": promise_sample,
        "otd_on_time": on_time,
        "otd_excluded_no_promise": delivered - promise_sample,
        "otd_rate": (on_time / promise_sample * 100) if promise_sample else None,
        "cycle_sample": delivered,
        "avg_cycle_days": (cycle_sum / delivered) if delivered else None,
        "exception_orders": exception_orders,
        "exception_rate": (exception_orders / total_orders * 100) if total_orders else None,
    }
