"""合同 / 框架协议读写。

到期提醒看两个日期：end_date（合同终止）与 price_valid_until（价格有效期）。
两者任一落入提醒窗口都算待办。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import db

WRITABLE = (
    "contract_no",
    "supplier_id",
    "title",
    "start_date",
    "end_date",
    "price_valid_until",
    "auto_renewal_reminder_days",
    "file_path",
)


class ValidationError(ValueError):
    """录入内容不合法。"""


def _check_date(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field}需要 YYYY-MM-DD 格式，收到：{value}") from exc


def _clean(data: dict) -> dict:
    payload = {k: data[k] for k in WRITABLE if k in data}

    if "title" in payload:
        title = (payload["title"] or "").strip()
        if not title:
            raise ValidationError("合同标题必填")
        payload["title"] = title

    for key, label in (("start_date", "开始日期"), ("end_date", "结束日期"), ("price_valid_until", "价格有效期")):
        if key in payload:
            payload[key] = _check_date(payload[key], label)

    if "contract_no" in payload:
        payload["contract_no"] = (payload["contract_no"] or "").strip() or None

    if "file_path" in payload:
        payload["file_path"] = (payload["file_path"] or "").strip() or None

    if "supplier_id" in payload and payload["supplier_id"] in ("", None):
        payload["supplier_id"] = None
    elif "supplier_id" in payload:
        payload["supplier_id"] = int(payload["supplier_id"])

    if "auto_renewal_reminder_days" in payload:
        try:
            payload["auto_renewal_reminder_days"] = int(payload["auto_renewal_reminder_days"] or 30)
        except (TypeError, ValueError) as exc:
            raise ValidationError("提前提醒天数必须是整数") from exc

    return payload


_SELECT = """
    SELECT c.*, s.name AS supplier_name
    FROM contracts c
    LEFT JOIN suppliers s ON s.id = c.supplier_id
"""


def list_contracts(
    supplier_id: int | None = None,
    keyword: str = "",
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    sql = _SELECT + " WHERE 1 = 1"
    params: list = []
    if supplier_id:
        sql += " AND c.supplier_id = ?"
        params.append(supplier_id)
    if keyword:
        sql += " AND (c.title LIKE ? OR c.contract_no LIKE ?)"
        like = f"%{keyword.strip()}%"
        params += [like, like]
    sql += " ORDER BY COALESCE(c.end_date, '9999-12-31') ASC, c.id DESC"
    return db.rows(sql, tuple(params), conn)


def get_contract(contract_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    return db.row(f"{_SELECT} WHERE c.id = ?", (contract_id,), conn)


def create_contract(data: dict, conn: sqlite3.Connection | None = None) -> int:
    payload = _clean(data)
    if not payload.get("title"):
        raise ValidationError("合同标题必填")
    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    return db.execute(f"INSERT INTO contracts ({cols}) VALUES ({marks})", tuple(payload.values()), conn)


def update_contract(contract_id: int, data: dict, conn: sqlite3.Connection | None = None) -> None:
    payload = _clean(data)
    if not payload:
        return
    sets = ", ".join(f"{k} = ?" for k in payload)
    db.execute(f"UPDATE contracts SET {sets} WHERE id = ?", tuple(payload.values()) + (contract_id,), conn)


def expiring_contracts(days: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    """合同终止日或价格有效期在 N 天内（含已过期）。"""
    return db.rows(
        """
        SELECT c.id, c.contract_no, c.title, c.end_date, c.price_valid_until,
               s.name AS supplier_name,
               CASE
                 WHEN c.end_date IS NOT NULL
                      AND (c.price_valid_until IS NULL OR c.end_date <= c.price_valid_until)
                   THEN c.end_date ELSE c.price_valid_until
               END AS due_date,
               CASE
                 WHEN c.end_date IS NOT NULL
                      AND (c.price_valid_until IS NULL OR c.end_date <= c.price_valid_until)
                   THEN '合同到期' ELSE '价格有效期到期'
               END AS due_kind
        FROM contracts c
        LEFT JOIN suppliers s ON s.id = c.supplier_id
        WHERE (c.end_date IS NOT NULL OR c.price_valid_until IS NOT NULL)
          AND julianday(
                CASE
                  WHEN c.end_date IS NOT NULL
                       AND (c.price_valid_until IS NULL OR c.end_date <= c.price_valid_until)
                    THEN c.end_date ELSE c.price_valid_until
                END
              ) - julianday(date('now', 'localtime')) <= ?
        ORDER BY due_date ASC
        """,
        (days,),
        conn,
    )


def count_contracts(conn: sqlite3.Connection | None = None) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM contracts", (), conn) or 0)
