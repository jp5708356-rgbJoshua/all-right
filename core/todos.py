"""仪表盘「需要你动手的订单」取数，六类。

    1 交期已过但未到货
    2 货到超 N 天票未到
    3 票到超 N 天款未付
    4 高风险供应商的进行中订单
    5 供应商资质到期前 N 天
    6 合同 / 价格有效期到期前 N 天

统一返回结构：
    {priority, kind, order_id, ref, supplier, status, due_date, days, action_hint, sort_key}
days：已逾期天数，正数=已超，负数=还剩。第 5/6 类由「剩余天数」取负得到。
sort_key 只用于排序，页面不展示。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import config, db, repo_contracts, repo_suppliers

TODAY = "date('now', 'localtime')"


def _with_meta(rows: list[dict], priority: int, kind: str, hint: str) -> list[dict]:
    for row in rows:
        row["priority"] = priority
        row["kind"] = kind
        row["action_hint"] = hint
        row["sort_key"] = -(row.get("days") or 0)
    return rows


def _delivery_overdue(conn: sqlite3.Connection | None) -> list[dict]:
    rows = db.rows(
        f"""
        SELECT o.id AS order_id, o.order_no AS ref, s.name AS supplier, o.status,
               o.promised_delivery_date AS due_date,
               CAST(julianday({TODAY}) - julianday(o.promised_delivery_date) AS INTEGER) AS days
        FROM orders o
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.promised_delivery_date IS NOT NULL
          AND o.promised_delivery_date < {TODAY}
          AND o.status NOT IN ('已到货', '已对账', '已完成', '已取消')
        """,
        (),
        conn,
    )
    return _with_meta(rows, 1, "交期已过未到货", "联系供应商确认发货时间，或标记异常")


def _goods_without_invoice(conn: sqlite3.Connection | None, days: int) -> list[dict]:
    rows = db.rows(
        f"""
        SELECT o.id AS order_id, o.order_no AS ref, s.name AS supplier, o.status,
               COALESCE(r.goods_received_date, o.actual_delivery_date) AS due_date,
               CAST(julianday({TODAY})
                    - julianday(COALESCE(r.goods_received_date, o.actual_delivery_date)) AS INTEGER) AS days
        FROM orders o
        JOIN suppliers s ON s.id = o.supplier_id
        JOIN reconciliation r ON r.order_id = o.id
        WHERE r.goods_received = 1
          AND r.invoice_received = 0
          AND o.status <> '已取消'
          AND COALESCE(r.goods_received_date, o.actual_delivery_date) IS NOT NULL
          AND julianday({TODAY})
              - julianday(COALESCE(r.goods_received_date, o.actual_delivery_date)) > ?
        """,
        (days,),
        conn,
    )
    return _with_meta(rows, 2, f"货到超 {days} 天未收票", "催供应商开票")


def _invoice_without_payment(conn: sqlite3.Connection | None, days: int) -> list[dict]:
    rows = db.rows(
        f"""
        SELECT o.id AS order_id, o.order_no AS ref, s.name AS supplier, o.status,
               r.invoice_received_date AS due_date,
               CAST(julianday({TODAY}) - julianday(r.invoice_received_date) AS INTEGER) AS days
        FROM orders o
        JOIN suppliers s ON s.id = o.supplier_id
        JOIN reconciliation r ON r.order_id = o.id
        WHERE r.invoice_received = 1
          AND r.payment_done = 0
          AND o.status <> '已取消'
          AND r.invoice_received_date IS NOT NULL
          AND julianday({TODAY}) - julianday(r.invoice_received_date) > ?
        """,
        (days,),
        conn,
    )
    return _with_meta(rows, 3, f"票到超 {days} 天未付款", "走付款流程")


def _high_risk_active(conn: sqlite3.Connection | None) -> list[dict]:
    rows = db.rows(
        """
        SELECT o.id AS order_id, o.order_no AS ref, s.name AS supplier, o.status,
               o.promised_delivery_date AS due_date,
               NULL AS days
        FROM orders o
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE s.risk_level = '高'
          AND o.status NOT IN ('已完成', '已取消')
        """,
        (),
        conn,
    )
    return _with_meta(rows, 4, "高风险供应商进行中", "加密跟单频次，确认产能与账期")


def _qualification_expiring(conn: sqlite3.Connection | None, days: int) -> list[dict]:
    rows = repo_suppliers.expiring_qualifications(days, conn)
    items = []
    for row in rows:
        items.append(
            {
                "order_id": None,
                "ref": row["name"],
                "supplier": row["name"],
                "status": "资质已过期" if row["days_left"] < 0 else "资质有效",
                "due_date": row["qualification_expiry"],
                # 统一成「已逾期天数」语义：剩 10 天 = -10
                "days": -int(row["days_left"]),
            }
        )
    return _with_meta(items, 5, f"供应商资质 {days} 天内到期", "催换证；过期则暂停下单")


def _contract_expiring(conn: sqlite3.Connection | None) -> list[dict]:
    window = max(config.reminder_days("contract_days"), config.reminder_days("price_valid_days"))
    rows = repo_contracts.expiring_contracts(window, conn)
    today = date.today()
    items = []
    for row in rows:
        try:
            remaining = (date.fromisoformat(str(row["due_date"])[:10]) - today).days
        except (TypeError, ValueError):
            remaining = None
        items.append(
            {
                "order_id": None,
                "ref": row["contract_no"] or row["title"],
                "supplier": row["supplier_name"] or "—",
                "status": row["due_kind"],
                "due_date": row["due_date"],
                "days": -remaining if remaining is not None else None,
            }
        )
    return _with_meta(items, 6, "合同 / 价格有效期临近", "启动续签或重新议价")


def build_todos(conn: sqlite3.Connection | None = None) -> list[dict]:
    goods_days = config.reminder_days("goods_to_invoice_days")
    invoice_days = config.reminder_days("invoice_to_payment_days")
    qualification_days = config.reminder_days("qualification_days")

    items = (
        _delivery_overdue(conn)
        + _goods_without_invoice(conn, goods_days)
        + _invoice_without_payment(conn, invoice_days)
        + _high_risk_active(conn)
        + _qualification_expiring(conn, qualification_days)
        + _contract_expiring(conn)
    )
    items.sort(key=lambda x: (x["priority"], x["sort_key"]))
    return items
