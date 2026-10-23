"""物料 / 服务读写。

historical_low_price / historical_high_price 不接受手工写入，
只由 recalc_historical_prices() 从 quotes 的 base_amount 重算。
"""

from __future__ import annotations

import sqlite3

from . import db

WRITABLE = ("code", "name", "spec", "unit")


class ValidationError(ValueError):
    """录入内容不合法。"""


def _clean(data: dict) -> dict:
    payload = {k: data[k] for k in WRITABLE if k in data}

    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            raise ValidationError("物料名称必填")
        payload["name"] = name

    for key in ("code", "spec", "unit"):
        if key in payload:
            value = (payload[key] or "").strip()
            payload[key] = value or None

    return payload


def list_items(keyword: str = "", conn: sqlite3.Connection | None = None) -> list[dict]:
    # 报价统计用一次 GROUP BY 聚合，避免每行跑两个相关子查询
    sql = """
        SELECT i.*,
               COALESCE(agg.quote_count, 0) AS quote_count,
               agg.last_quote_date
        FROM items i
        LEFT JOIN (
            SELECT item_id,
                   COUNT(*) AS quote_count,
                   MAX(quote_date) AS last_quote_date
            FROM quotes
            GROUP BY item_id
        ) agg ON agg.item_id = i.id
        WHERE 1 = 1
    """
    params: list = []
    if keyword:
        sql += " AND (i.name LIKE ? OR i.code LIKE ? OR i.spec LIKE ?)"
        like = f"%{keyword.strip()}%"
        params += [like, like, like]
    sql += " ORDER BY i.name"
    return db.rows(sql, tuple(params), conn)


def get_item(item_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    return db.row("SELECT * FROM items WHERE id = ?", (item_id,), conn)


def find_by_code(code: str, exclude_id: int | None = None, conn: sqlite3.Connection | None = None) -> dict | None:
    sql = "SELECT * FROM items WHERE code = ?"
    params: list = [code]
    if exclude_id:
        sql += " AND id <> ?"
        params.append(exclude_id)
    return db.row(sql, tuple(params), conn)


def create_item(data: dict, conn: sqlite3.Connection | None = None) -> int:
    payload = _clean(data)
    if not payload.get("name"):
        raise ValidationError("物料名称必填")
    if payload.get("code") and find_by_code(payload["code"], conn=conn):
        raise ValidationError(f"物料编码 {payload['code']} 已存在")

    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    return db.execute(f"INSERT INTO items ({cols}) VALUES ({marks})", tuple(payload.values()), conn)


def update_item(item_id: int, data: dict, conn: sqlite3.Connection | None = None) -> None:
    payload = _clean(data)
    if not payload:
        return
    if payload.get("code") and find_by_code(payload["code"], exclude_id=item_id, conn=conn):
        raise ValidationError(f"物料编码 {payload['code']} 已被其他物料占用")

    sets = ", ".join(f"{k} = ?" for k in payload)
    values = list(payload.values()) + [item_id]
    db.execute(
        f"UPDATE items SET {sets}, updated_at = datetime('now', 'localtime') WHERE id = ?",
        tuple(values),
        conn,
    )


def recalc_historical_prices(item_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    """从比价记录重算历史低/高价。比价为空则清成 NULL，不留旧值。"""
    stats = db.row(
        """
        SELECT MIN(COALESCE(base_amount, quoted_price)) AS low,
               MAX(COALESCE(base_amount, quoted_price)) AS high,
               COUNT(*) AS n
        FROM quotes WHERE item_id = ?
        """,
        (item_id,),
        conn,
    )
    low = stats["low"] if stats and stats["n"] else None
    high = stats["high"] if stats and stats["n"] else None
    db.execute(
        "UPDATE items SET historical_low_price = ?, historical_high_price = ?, "
        "updated_at = datetime('now', 'localtime') WHERE id = ?",
        (low, high, item_id),
        conn,
    )
    return {"historical_low_price": low, "historical_high_price": high}


def count_items(conn: sqlite3.Connection | None = None) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM items", (), conn) or 0)
