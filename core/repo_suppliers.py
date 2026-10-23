"""供应商档案读写。

安全约定：列表查询不 SELECT 联系人姓名 / 电话 / 银行账号，
敏感字段只在 get_supplier() 详情里返回。这样页面就算写错也漏不出去。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import db

# 允许写入的字段白名单，防止调用方塞进未知列
WRITABLE = (
    "name",
    "credit_code",
    "category",
    "contact_name",
    "contact_phone",
    "bank_account",
    "qualification_expiry",
    "risk_level",
    "status",
    "risk_notes",
)

RISK_LEVELS = ("低", "中", "高")
STATUSES = ("活跃", "暂停", "淘汰")

# 列表页只取这些列，敏感字段一律不在其中
_LIST_COLUMNS = """
    s.id, s.name, s.category, s.credit_code,
    s.qualification_expiry, s.risk_level, s.status, s.risk_notes
"""


class ValidationError(ValueError):
    """录入内容不合法。"""


def _check_date(value: str | None, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationError(f"{field} 需要 YYYY-MM-DD 格式，收到：{value}") from exc
    return str(value)[:10]


def _clean(data: dict) -> dict:
    payload = {k: data[k] for k in WRITABLE if k in data}

    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            raise ValidationError("供应商名称必填")
        payload["name"] = name

    if "risk_level" in payload and payload["risk_level"] not in RISK_LEVELS:
        raise ValidationError(f"risk_level 只能是 {'/'.join(RISK_LEVELS)}")

    if "status" in payload and payload["status"] not in STATUSES:
        raise ValidationError(f"status 只能是 {'/'.join(STATUSES)}")

    if "qualification_expiry" in payload:
        payload["qualification_expiry"] = _check_date(payload["qualification_expiry"], "资质到期日")

    # 其余文本字段统一去空白，空串存 NULL，保持"非关键字段可为空"
    for key in ("credit_code", "category", "contact_name", "contact_phone", "bank_account", "risk_notes"):
        if key in payload:
            value = (payload[key] or "").strip()
            payload[key] = value or None

    return payload


def duplicate_names(name: str, exclude_id: int | None = None, conn: sqlite3.Connection | None = None) -> list[dict]:
    """同名供应商提示用。不强制唯一，只提示。"""
    sql = "SELECT id, name, status FROM suppliers WHERE name = ?"
    params: list = [name.strip()]
    if exclude_id:
        sql += " AND id <> ?"
        params.append(exclude_id)
    return db.rows(sql, tuple(params), conn)


def list_suppliers(
    keyword: str = "",
    risk_level: str | None = None,
    status: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    # 订单统计用一次 GROUP BY 聚合再 LEFT JOIN，避免每行跑三个相关子查询
    sql = f"""
        SELECT {_LIST_COLUMNS},
               agg.last_order_date,
               COALESCE(agg.order_count, 0) AS order_count,
               COALESCE(agg.active_order_count, 0) AS active_order_count
        FROM suppliers s
        LEFT JOIN (
            SELECT supplier_id,
                   MAX(order_date) AS last_order_date,
                   COUNT(*) AS order_count,
                   SUM(CASE WHEN status NOT IN ('已完成', '已取消') THEN 1 ELSE 0 END)
                       AS active_order_count
            FROM orders
            GROUP BY supplier_id
        ) agg ON agg.supplier_id = s.id
        WHERE 1 = 1
    """
    params: list = []
    if keyword:
        sql += " AND (s.name LIKE ? OR s.category LIKE ? OR s.credit_code LIKE ?)"
        like = f"%{keyword.strip()}%"
        params += [like, like, like]
    if risk_level:
        sql += " AND s.risk_level = ?"
        params.append(risk_level)
    if status:
        sql += " AND s.status = ?"
        params.append(status)
    sql += " ORDER BY CASE s.risk_level WHEN '高' THEN 0 WHEN '中' THEN 1 ELSE 2 END, s.name"
    return db.rows(sql, tuple(params), conn)


def get_supplier(supplier_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    """详情用，含敏感字段。"""
    return db.row("SELECT * FROM suppliers WHERE id = ?", (supplier_id,), conn)


def create_supplier(data: dict, conn: sqlite3.Connection | None = None) -> int:
    payload = _clean(data)
    if not payload.get("name"):
        raise ValidationError("供应商名称必填")
    payload.setdefault("risk_level", "低")
    payload.setdefault("status", "活跃")

    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    return db.execute(f"INSERT INTO suppliers ({cols}) VALUES ({marks})", tuple(payload.values()), conn)


def update_supplier(supplier_id: int, data: dict, conn: sqlite3.Connection | None = None) -> None:
    payload = _clean(data)
    if not payload:
        return
    payload["updated_at"] = datetime_now()
    sets = ", ".join(f"{k} = ?" for k in payload)
    values = list(payload.values()) + [supplier_id]
    db.execute(f"UPDATE suppliers SET {sets} WHERE id = ?", tuple(values), conn)


def datetime_now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def count_suppliers(conn: sqlite3.Connection | None = None) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM suppliers", (), conn) or 0)


def expiring_qualifications(days: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    """资质在 N 天内到期（含已过期），仪表盘第 5 类待办用。"""
    return db.rows(
        """
        SELECT id, name, qualification_expiry, risk_level,
               CAST(julianday(qualification_expiry) - julianday(date('now', 'localtime')) AS INTEGER) AS days_left
        FROM suppliers
        WHERE qualification_expiry IS NOT NULL
          AND status <> '淘汰'
          AND julianday(qualification_expiry) - julianday(date('now', 'localtime')) <= ?
        ORDER BY qualification_expiry
        """,
        (days,),
        conn,
    )
