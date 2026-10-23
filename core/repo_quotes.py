"""比价记录读写 + 有效报价判定。

判定口径（写进 README，避免以后自己对不上）：
- 单条有效性：valid_until 为空 = 长期有效；valid_until >= 今天 = 有效；否则已过期。
- 有效报价家数：按 supplier_id 去重，同一家报三次只算 1 家。
- 最低价比较：一律用 base_amount（折算后金额）。缺汇率的外币报价不进比较，单独列出。
- 同一供应商多轮报价：只取 quote_date 最新的一条参与最低价比较。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import db, repo_fx, repo_items

WRITABLE = (
    "item_id",
    "supplier_id",
    "quoted_price",
    "currency",
    "quote_date",
    "valid_until",
    "lead_time_days",
    "is_selected",
    "selection_reason",
    "note",
    "import_source",
    "raw_payload",
    "edited_by_user",
)


class ValidationError(ValueError):
    """录入内容不合法。"""


def _check_date(value, field: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValidationError(f"{field}必填")
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field}需要 YYYY-MM-DD 格式，收到：{value}") from exc


def _to_float(value, field: str, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field}必须是数字，收到：{value}") from exc
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field}不能小于 {minimum}")
    return number


# ---------------------------------------------------------------- 写入

def create_quote(data: dict, conn: sqlite3.Connection | None = None) -> int:
    payload = {k: data[k] for k in WRITABLE if k in data}

    item_id = payload.get("item_id")
    if not item_id:
        raise ValidationError("必须选择物料")
    if not db.row("SELECT id FROM items WHERE id = ?", (int(item_id),), conn):
        raise ValidationError(f"物料 #{item_id} 不存在")
    payload["item_id"] = int(item_id)

    supplier_id = payload.get("supplier_id")
    if not supplier_id:
        raise ValidationError("必须选择供应商")
    if not db.row("SELECT id FROM suppliers WHERE id = ?", (int(supplier_id),), conn):
        raise ValidationError(f"供应商 #{supplier_id} 不存在")
    payload["supplier_id"] = int(supplier_id)

    price = _to_float(payload.get("quoted_price"), "报价", minimum=0.000001)
    payload["quoted_price"] = price

    currency = (payload.get("currency") or repo_fx.base_currency()).strip().upper()
    payload["currency"] = currency

    payload["quote_date"] = _check_date(payload.get("quote_date"), "报价日期", required=True)
    payload["valid_until"] = _check_date(payload.get("valid_until"), "有效期至")
    if payload["valid_until"] and payload["valid_until"] < payload["quote_date"]:
        raise ValidationError("有效期不能早于报价日期")

    # 外币报价按报价日的最近生效汇率折算；缺汇率直接拦下，不存半条脏数据
    base = repo_fx.to_base(price, currency, payload["quote_date"], conn)
    if base is None:
        raise ValidationError(repo_fx.describe_missing(currency, payload["quote_date"]))
    payload["base_amount"] = base

    if payload.get("lead_time_days") in ("", None):
        payload["lead_time_days"] = None
    else:
        payload["lead_time_days"] = int(payload["lead_time_days"])

    payload["is_selected"] = 1 if payload.get("is_selected") in (1, True, "1", "是") else 0
    payload["import_source"] = payload.get("import_source") or "manual"
    payload["edited_by_user"] = 1 if payload.get("edited_by_user") in (1, True) else 0

    for key in ("selection_reason", "note"):
        if key in payload:
            payload[key] = (payload[key] or "").strip() or None

    with db.transaction(conn):
        quote_id = db.execute(
            f"INSERT INTO quotes ({', '.join(payload)}) VALUES ({', '.join('?' for _ in payload)})",
            tuple(payload.values()),
            conn,
        )
        if payload["is_selected"]:
            _exclusive_select(payload["item_id"], quote_id, conn)
        repo_items.recalc_historical_prices(payload["item_id"], conn)

    return quote_id


def _exclusive_select(item_id: int, quote_id: int, conn: sqlite3.Connection | None) -> None:
    """同一物料只保留一条「选中」，避免比价页出现多个中选标。"""
    db.execute(
        "UPDATE quotes SET is_selected = 0 WHERE item_id = ? AND id <> ?",
        (item_id, quote_id),
        conn,
    )


def select_quote(quote_id: int, reason: str = "", conn: sqlite3.Connection | None = None) -> None:
    quote = db.row("SELECT * FROM quotes WHERE id = ?", (quote_id,), conn)
    if not quote:
        raise ValidationError(f"报价 #{quote_id} 不存在")
    with db.transaction(conn):
        db.execute(
            "UPDATE quotes SET is_selected = 1, selection_reason = ? WHERE id = ?",
            ((reason or "").strip() or None, quote_id),
            conn,
        )
        _exclusive_select(quote["item_id"], quote_id, conn)


def delete_quote(quote_id: int, conn: sqlite3.Connection | None = None) -> None:
    quote = db.row("SELECT item_id FROM quotes WHERE id = ?", (quote_id,), conn)
    if not quote:
        return
    with db.transaction(conn):
        db.execute("DELETE FROM quotes WHERE id = ?", (quote_id,), conn)
        repo_items.recalc_historical_prices(quote["item_id"], conn)


# ---------------------------------------------------------------- 读取

_SELECT = """
    SELECT q.*,
           s.name AS supplier_name,
           s.risk_level AS supplier_risk_level,
           i.name AS item_name,
           i.unit AS item_unit
    FROM quotes q
    JOIN suppliers s ON s.id = q.supplier_id
    JOIN items i ON i.id = q.item_id
"""


def list_quotes(
    item_id: int | None = None,
    supplier_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    sql = _SELECT + " WHERE 1 = 1"
    params: list = []
    if item_id:
        sql += " AND q.item_id = ?"
        params.append(item_id)
    if supplier_id:
        sql += " AND q.supplier_id = ?"
        params.append(supplier_id)
    sql += " ORDER BY q.quote_date DESC, q.id DESC"
    return db.rows(sql, tuple(params), conn)


def list_quotes_by_items(
    item_ids: list[int], conn: sqlite3.Connection | None = None
) -> list[dict]:
    """一次取多个物料的报价，给评分卡这类批量场景用，避免按物料逐条查。"""
    if not item_ids:
        return []
    marks = ", ".join("?" for _ in item_ids)
    return db.rows(
        f"{_SELECT} WHERE q.item_id IN ({marks}) ORDER BY q.quote_date DESC, q.id DESC",
        tuple(int(i) for i in item_ids),
        conn,
    )


def quote_validity(quote: dict, today: str | None = None) -> str:
    today = today or date.today().isoformat()
    if not quote.get("valid_until"):
        return "有效"
    return "有效" if quote["valid_until"] >= today else "已过期"


def analyze_quotes(rows: list[dict], today: str | None = None) -> dict:
    """纯计算：给定一批报价行，算有效 / 过期 / 各家最新 / 当前最低价。

    不查库，所以既能服务单个物料，也能服务批量场景（评分卡）。
    """
    today = today or date.today().isoformat()
    rows = [{**row, "state": quote_validity(row, today)} for row in rows]

    valid = [r for r in rows if r["state"] == "有效"]
    expired = [r for r in rows if r["state"] == "已过期"]

    # 同一供应商只取最新一条参与比价
    latest: dict[int, dict] = {}
    for row in sorted(valid, key=lambda r: (r["quote_date"], r["id"])):
        latest[row["supplier_id"]] = row

    priced, missing_fx = [], []
    for row in latest.values():
        base = row["base_amount"]
        if base is None:
            if row["currency"] == repo_fx.base_currency():
                base = row["quoted_price"]
            else:
                missing_fx.append(row)
                continue
        priced.append({**row, "compare_price": base})

    lowest = min(priced, key=lambda r: r["compare_price"]) if priced else None
    return {
        "all": rows,
        "valid": valid,
        "expired": expired,
        "latest_by_supplier": latest,
        "priced": sorted(priced, key=lambda r: r["compare_price"]),
        "lowest": lowest,
        "supplier_count": len(latest),
        "need_more": len(latest) < 3,
        "missing_fx": missing_fx,
    }


def effective_quotes(item_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """一个物料的比价全貌。"""
    return analyze_quotes(list_quotes(item_id=item_id, conn=conn))


def latest_quote_per_supplier(item_id: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    """给评分卡 cost 维度用：每个供应商在该物料上的最新有效报价。"""
    data = effective_quotes(item_id, conn)
    return data["priced"]
