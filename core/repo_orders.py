"""订单读写与状态流转。

铁律：orders.status 只能通过本文件的
    update_status / mark_exception / restore_exception / cancel_order
四个函数变更。页面与其它模块一律不许直接 UPDATE status。
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime

from . import db, repo_fx, state_machine as sm

WRITABLE_NEW = (
    "order_no",
    "supplier_id",
    "item_id",
    "item_name_raw",
    "quantity",
    "unit_price",
    "total_amount",
    "currency",
    "base_amount",
    "order_date",
    "promised_delivery_date",
    "contract_id",
    "is_urgent",
)

# 编辑时不允许碰状态相关字段，状态只走专用函数
WRITABLE_EDIT = (
    "item_id",
    "item_name_raw",
    "quantity",
    "unit_price",
    "total_amount",
    "currency",
    "base_amount",
    "order_date",
    "promised_delivery_date",
    "actual_delivery_date",
    "contract_id",
    "is_urgent",
    "exception_note",
)

EXCEPTION_CATEGORIES = ("质量", "交期", "单据", "其他")


class ValidationError(ValueError):
    """录入内容不合法。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


def _check_date(value, field: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValidationError(f"{field}必填")
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field}需要 YYYY-MM-DD 格式，收到：{value}") from exc


def _to_float(value, field: str, minimum: float | None = None) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field}必须是数字，收到：{value}") from exc
    if not math.isfinite(number):
        raise ValidationError(f"{field}必须是有限数字")
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field}不能小于 {minimum}")
    return number


# ---------------------------------------------------------------- 写入

def _clean_new(data: dict, conn: sqlite3.Connection | None = None) -> dict:
    payload = {k: data[k] for k in WRITABLE_NEW if k in data}

    order_no = (payload.get("order_no") or "").strip()
    if not order_no:
        raise ValidationError("订单号必填")
    payload["order_no"] = order_no

    supplier_id = payload.get("supplier_id")
    if not supplier_id:
        raise ValidationError("必须选择供应商")
    if not db.row("SELECT id FROM suppliers WHERE id = ?", (int(supplier_id),), conn):
        raise ValidationError(f"供应商 #{supplier_id} 不存在")

    payload["supplier_id"] = int(supplier_id)

    if payload.get("item_id") in ("", None):
        payload["item_id"] = None
    else:
        payload["item_id"] = int(payload["item_id"])
        if not db.row("SELECT id FROM items WHERE id = ?", (payload["item_id"],), conn):
            raise ValidationError(f"物料 #{payload['item_id']} 不存在")

    # 品名：没填但选了物料，就用物料名兜住
    item_name = (payload.get("item_name_raw") or "").strip()
    if not item_name and payload.get("item_id"):
        found = db.row("SELECT name FROM items WHERE id = ?", (payload["item_id"],), conn)
        item_name = found["name"] if found else ""
    if not item_name:
        raise ValidationError("品名必填（没选物料时请手填）")
    payload["item_name_raw"] = item_name

    quantity = _to_float(payload.get("quantity"), "数量", minimum=0.000001)
    if quantity is None:
        raise ValidationError("数量必填")
    payload["quantity"] = quantity

    unit_price = _to_float(payload.get("unit_price"), "单价", minimum=0)
    payload["unit_price"] = unit_price

    total = _to_float(payload.get("total_amount"), "总价", minimum=0)
    if total is None and unit_price is not None:
        total = round(quantity * unit_price, 4)
    payload["total_amount"] = total

    payload["order_date"] = _check_date(payload.get("order_date"), "下单日期", required=True)
    payload["promised_delivery_date"] = _check_date(payload.get("promised_delivery_date"), "承诺交期")

    # 外币按订单日最近生效汇率折算成 base_amount 快照；缺汇率直接拦下
    payload["currency"] = (payload.get("currency") or repo_fx.base_currency()).strip().upper()
    base = _to_float(payload.get("base_amount"), "折算金额", minimum=0)
    if base is None and total is not None:
        base = repo_fx.to_base(total, payload["currency"], payload["order_date"], conn)
        if base is None:
            raise ValidationError(
                repo_fx.describe_missing(payload["currency"], payload["order_date"])
            )
    payload["base_amount"] = base

    if payload.get("contract_id") in ("", None):
        payload["contract_id"] = None

    payload["is_urgent"] = 1 if payload.get("is_urgent") in (1, True, "1", "是") else 0

    if db.row("SELECT id FROM orders WHERE order_no = ?", (order_no,), conn):
        raise ValidationError(f"订单号 {order_no} 已存在")

    return payload


def create_order(data: dict, conn: sqlite3.Connection | None = None) -> int:
    payload = _clean_new(data, conn)
    payload["status"] = "已下单"
    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    return db.execute(f"INSERT INTO orders ({cols}) VALUES ({marks})", tuple(payload.values()), conn)


def update_order(order_id: int, data: dict, conn: sqlite3.Connection | None = None) -> None:
    payload = {k: data[k] for k in WRITABLE_EDIT if k in data}
    if not payload:
        return

    if "quantity" in payload:
        qty = _to_float(payload["quantity"], "数量", minimum=0.000001)
        if qty is None:
            raise ValidationError("数量必填")
        payload["quantity"] = qty
    if "unit_price" in payload:
        payload["unit_price"] = _to_float(payload["unit_price"], "单价", minimum=0)
    if "total_amount" in payload:
        payload["total_amount"] = _to_float(payload["total_amount"], "总价", minimum=0)
    if "order_date" in payload:
        payload["order_date"] = _check_date(payload["order_date"], "下单日期", required=True)
    if "promised_delivery_date" in payload:
        payload["promised_delivery_date"] = _check_date(payload["promised_delivery_date"], "承诺交期")
    if "actual_delivery_date" in payload:
        payload["actual_delivery_date"] = _check_date(payload["actual_delivery_date"], "实际到货日")
    if "item_name_raw" in payload:
        name = (payload["item_name_raw"] or "").strip()
        if not name:
            raise ValidationError("品名不能为空")
        payload["item_name_raw"] = name
    if "is_urgent" in payload:
        payload["is_urgent"] = 1 if payload["is_urgent"] in (1, True, "1", "是") else 0

    # 只有币种或总价真的变了才重算折算快照，避免只改品名也把历史汇率冲掉
    current = db.row(
        "SELECT currency, total_amount, order_date FROM orders WHERE id = ?", (order_id,), conn
    )
    if not current:
        raise ValidationError(f"订单 #{order_id} 不存在")

    total_changed = (
        "total_amount" in payload and payload["total_amount"] != current["total_amount"]
    )
    currency_changed = (
        "currency" in payload
        and (payload["currency"] or "").strip().upper() != (current["currency"] or "").upper()
    )

    if total_changed or currency_changed:
        currency = (payload.get("currency") or current["currency"]).strip().upper()
        payload["currency"] = currency
        total = payload.get("total_amount", current["total_amount"])
        order_date = payload.get("order_date") or current["order_date"]
        if total is not None:
            base = repo_fx.to_base(total, currency, order_date, conn)
            if base is None:
                raise ValidationError(repo_fx.describe_missing(currency, order_date))
            payload["base_amount"] = base
        else:
            payload["base_amount"] = None

    payload["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in payload)
    db.execute(f"UPDATE orders SET {sets} WHERE id = ?", tuple(payload.values()) + (order_id,), conn)


# ---------------------------------------------------------------- 状态流转

def _load_for_change(order_id: int, conn: sqlite3.Connection | None) -> dict:
    order = db.row("SELECT * FROM orders WHERE id = ?", (order_id,), conn)
    if not order:
        raise ValidationError(f"订单 #{order_id} 不存在")
    return order


def update_status(order_id: int, target: str, conn: sqlite3.Connection | None = None) -> dict:
    """唯一允许的正常流转入口。跳级、倒流、从异常直转，全部拒绝。"""
    order = _load_for_change(order_id, conn)
    current = order["status"]

    if not sm.can_transition(current, target):
        allowed = sm.next_states(current) or ["（无，终态）"]
        raise sm.StateError(
            f"订单 {order['order_no']} 当前为「{current}」，不能直接流转到「{target}」。"
            f"允许的下一步：{'、'.join(allowed)}"
        )

    extra: dict = {}
    # 到货时若没填实际到货日，按当天记，否则 OTD 永远算不出来
    if target == "已到货" and not order["actual_delivery_date"]:
        extra["actual_delivery_date"] = _today()

    extra["status"] = target
    extra["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in extra)
    db.execute(f"UPDATE orders SET {sets} WHERE id = ?", tuple(extra.values()) + (order_id,), conn)
    return {"from": current, "to": target, "auto_filled_delivery": "actual_delivery_date" in extra}


def mark_exception(
    order_id: int,
    reason: str,
    category: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    order = _load_for_change(order_id, conn)
    if not sm.can_mark_exception(order["status"]):
        raise sm.StateError(f"订单 {order['order_no']} 当前为「{order['status']}」，不能再标记异常")

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("标记异常必须填写原因")
    if category not in EXCEPTION_CATEGORIES:
        raise ValidationError(f"异常类别只能是 {'/'.join(EXCEPTION_CATEGORIES)}")

    # 异常历史与订单状态必须一起落地，否则会出现「状态是异常但没有记录」
    with db.transaction(conn):
        db.execute(
            "INSERT INTO order_exceptions (order_id, from_status, category, reason) VALUES (?, ?, ?, ?)",
            (order_id, order["status"], category, reason),
            conn,
        )
        db.execute(
            "UPDATE orders SET status = ?, status_before_exception = ?, exception_note = ?, "
            "exception_category = ?, updated_at = ? WHERE id = ?",
            (sm.EXCEPTION, order["status"], reason, category, _now(), order_id),
            conn,
        )


def restore_exception(order_id: int, conn: sqlite3.Connection | None = None) -> str:
    """异常恢复：回到标记前状态，并关掉未结的异常记录。"""
    order = _load_for_change(order_id, conn)
    if not sm.can_restore(order["status"]):
        raise sm.StateError(f"订单 {order['order_no']} 当前不是异常状态，无需恢复")

    target = sm.restore_target(order)
    with db.transaction(conn):
        db.execute(
            "UPDATE order_exceptions SET resolved_at = ? WHERE order_id = ? AND resolved_at IS NULL",
            (_now(), order_id),
            conn,
        )
        db.execute(
            "UPDATE orders SET status = ?, status_before_exception = NULL, exception_note = NULL, "
            "exception_category = NULL, updated_at = ? WHERE id = ?",
            (target, _now(), order_id),
            conn,
        )
    return target


def cancel_order(order_id: int, reason: str = "", conn: sqlite3.Connection | None = None) -> None:
    order = _load_for_change(order_id, conn)
    if not sm.can_cancel(order["status"]):
        raise sm.StateError(f"订单 {order['order_no']} 当前为「{order['status']}」，不能取消")

    with db.transaction(conn):
        if order["status"] == sm.EXCEPTION:
            db.execute(
                "UPDATE order_exceptions SET resolved_at = ? WHERE order_id = ? AND resolved_at IS NULL",
                (_now(), order_id),
                conn,
            )

        db.execute(
            "UPDATE orders SET status = ?, status_before_exception = NULL, updated_at = ? WHERE id = ?",
            (sm.CANCELLED, _now(), order_id),
            conn,
        )
        if reason.strip():
            db.execute(
                "UPDATE orders SET exception_note = ? WHERE id = ?",
                (f"取消原因：{reason.strip()}", order_id),
                conn,
            )


# ---------------------------------------------------------------- 读取

_BASE_SELECT = """
    SELECT o.*,
           s.name AS supplier_name,
           s.risk_level AS supplier_risk_level,
           i.code AS item_code,
           i.name AS item_name,
           i.unit AS item_unit
    FROM orders o
    JOIN suppliers s ON s.id = o.supplier_id
    LEFT JOIN items i ON i.id = o.item_id
"""


def get_order(order_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    return db.row(f"{_BASE_SELECT} WHERE o.id = ?", (order_id,), conn)


def _filters(
    keyword: str = "",
    supplier_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_urgent: bool = False,
    only_active: bool = False,
) -> tuple[str, list]:
    """列表与计数共用同一套筛选条件，避免两边写岔。"""
    clause = ""
    params: list = []

    if keyword:
        clause += " AND (o.order_no LIKE ? OR o.item_name_raw LIKE ?)"
        like = f"%{keyword.strip()}%"
        params += [like, like]
    if supplier_id:
        clause += " AND o.supplier_id = ?"
        params.append(supplier_id)
    if status:
        clause += " AND o.status = ?"
        params.append(status)
    if date_from:
        clause += " AND o.order_date >= ?"
        params.append(date_from)
    if date_to:
        clause += " AND o.order_date <= ?"
        params.append(date_to)
    if only_urgent:
        clause += " AND o.is_urgent = 1"
    if only_active:
        clause += " AND o.status NOT IN ('已完成', '已取消')"

    return clause, params


def list_orders(
    keyword: str = "",
    supplier_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_urgent: bool = False,
    only_active: bool = False,
    limit: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    clause, params = _filters(
        keyword, supplier_id, status, date_from, date_to, only_urgent, only_active
    )
    sql = _BASE_SELECT + " WHERE 1 = 1" + clause + " ORDER BY o.order_date DESC, o.id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return db.rows(sql, tuple(params), conn)


def count_orders(
    keyword: str = "",
    supplier_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_urgent: bool = False,
    only_active: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    clause, params = _filters(
        keyword, supplier_id, status, date_from, date_to, only_urgent, only_active
    )
    return int(
        db.scalar(
            f"SELECT COUNT(*) FROM orders o WHERE 1 = 1 {clause}",
            tuple(params),
            conn,
        )
        or 0
    )


def list_exceptions(order_id: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    return db.rows(
        "SELECT * FROM order_exceptions WHERE order_id = ? ORDER BY id DESC",
        (order_id,),
        conn,
    )


def get_reconciliation(order_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    return db.row("SELECT * FROM reconciliation WHERE order_id = ?", (order_id,), conn)


# ---------------------------------------------------------------- 仪表盘数字

def count_active(conn: sqlite3.Connection | None = None) -> int:
    return int(
        db.scalar("SELECT COUNT(*) FROM orders WHERE status NOT IN ('已完成', '已取消')", (), conn) or 0
    )


def count_due_soon(days: int, conn: sqlite3.Connection | None = None) -> int:
    return int(
        db.scalar(
            """
            SELECT COUNT(*) FROM orders
            WHERE status NOT IN ('已到货', '已对账', '已完成', '已取消')
              AND promised_delivery_date IS NOT NULL
              AND promised_delivery_date BETWEEN date('now', 'localtime')
                                             AND date('now', 'localtime', ?)
            """,
            (f"+{int(days)} days",),
            conn,
        )
        or 0
    )


def count_pending_recon(conn: sqlite3.Connection | None = None) -> int:
    """已到货但货/票/款没齐的订单数。"""
    return int(
        db.scalar(
            """
            SELECT COUNT(*) FROM orders o
            LEFT JOIN reconciliation r ON r.order_id = o.id
            WHERE o.status = '已到货'
              AND COALESCE(r.goods_received, 0) + COALESCE(r.invoice_received, 0)
                  + COALESCE(r.payment_done, 0) < 3
            """,
            (),
            conn,
        )
        or 0
    )


def count_high_risk_active(conn: sqlite3.Connection | None = None) -> int:
    return int(
        db.scalar(
            """
            SELECT COUNT(*) FROM orders o
            JOIN suppliers s ON s.id = o.supplier_id
            WHERE s.risk_level = '高' AND o.status NOT IN ('已完成', '已取消')
            """,
            (),
            conn,
        )
        or 0
    )
