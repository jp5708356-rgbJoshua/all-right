"""本地采购工作台：配置、连接及第一轮 CRUD。"""
from pathlib import Path
import sqlite3
import yaml
import re
from datetime import date
from typing import Optional
from decimal import Decimal, localcontext, ROUND_HALF_UP
from state_machine import (allowed_transitions, NORMAL_STATUSES, ACTIVE_STATUSES,
                           PRE_ARRIVAL_STATUSES, initial_fields, action_fields)

ROOT = Path(__file__).resolve().parent
FIELDS = {
    "suppliers": ("name", "credit_code", "category", "contact_name", "contact_phone",
                  "bank_account", "qualification_expiry", "risk_level", "status", "risk_notes"),
    "items": ("code", "name", "spec", "unit"),
}


def load_config():
    with (ROOT / "config.yaml").open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict) or not isinstance(config.get("db_path"), str) or not config["db_path"].strip():
        raise ValueError("config.yaml 必须包含非空 db_path")
    return config


def get_connection():
    path = Path(load_config()["db_path"]).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    connection = get_connection()
    try:
        with connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
                    credit_code TEXT, category TEXT, contact_name TEXT,
                    contact_phone TEXT, bank_account TEXT, qualification_expiry TEXT,
                    risk_level TEXT DEFAULT '低' CHECK(risk_level IN ('低','中','高')),
                    status TEXT DEFAULT '活跃' CHECK(status IN ('活跃','暂停','淘汰')),
                    risk_notes TEXT
                );
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT, name TEXT NOT NULL CHECK(length(trim(name)) > 0),
                    spec TEXT, unit TEXT,
                    historical_low_price REAL, historical_high_price REAL
                );
                CREATE INDEX IF NOT EXISTS idx_suppliers_name ON suppliers(name);
                CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
            """)
            connection.executescript(ORDER_SCHEMA)
    finally:
        connection.close()


def _fields(table):
    if table not in FIELDS:
        raise ValueError("不支持的数据表")
    return FIELDS[table]


def list_records(table):
    _fields(table)
    connection = get_connection()
    try:
        # 列表查询不读取供应商联系人、电话、付款账号。
        columns = ("id,name,risk_level,status,qualification_expiry" if table == "suppliers"
                   else "id,code,name,spec,unit,historical_low_price,historical_high_price")
        return [dict(row) for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY id DESC")]
    finally:
        connection.close()


def get_record(table, record_id):
    _fields(table)
    connection = get_connection()
    try:
        row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def count_name(table, name):
    _fields(table)
    connection = get_connection()
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE name = ?", (name.strip(),)).fetchone()[0]
    finally:
        connection.close()


def save_record(table, values, record_id=None):
    allowed = _fields(table)
    if set(values) - set(allowed):
        raise ValueError("包含不可编辑字段")
    values = dict(values)
    values["name"] = str(values.get("name") or "").strip()
    if not values["name"]:
        raise ValueError("名称不能为空")
    values = {key: (None if value == "" else value) for key, value in values.items()}
    for key in ("risk_level", "status"):
        if table == "suppliers" and key in values and values[key] is None:
            values.pop(key)
    columns = list(values)
    connection = get_connection()
    try:
        with connection:
            if record_id is None:
                placeholders = ",".join("?" for _ in columns)
                cursor = connection.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", list(values.values()))
                return cursor.lastrowid
            cursor = connection.execute(
                f"UPDATE {table} SET {','.join(key + ' = ?' for key in columns)} WHERE id = ?",
                [*values.values(), record_id])
            if cursor.rowcount != 1:
                raise ValueError("记录不存在")
            return record_id
    finally:
        connection.close()


def delete_record(table, record_id):
    _fields(table)
    connection = get_connection()
    try:
        with connection:
            try:
                connection.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            except sqlite3.IntegrityError:
                raise ValueError("该记录已被订单引用，不能删除；正式淘汰供应商请修改其状态") from None
    finally:
        connection.close()

# 数据编码常量，不是业务阈值；三个字段均不用 REAL。
SCALE = 1_000_000
MONEY_SCALE = 100
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
DECIMAL_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", re.ASCII)
DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
ORDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE CHECK (TRIM(order_no) != ''),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    item_id INTEGER REFERENCES items(id) ON DELETE RESTRICT,
    item_name_raw TEXT,
    quantity INTEGER NOT NULL CHECK (typeof(quantity)='integer' AND quantity > 0),
    unit_price INTEGER CHECK (unit_price IS NULL OR (typeof(unit_price)='integer' AND unit_price >= 0)),
    total_amount INTEGER CHECK (total_amount IS NULL OR (typeof(total_amount)='integer' AND total_amount >= 0)),
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (TRIM(currency) != ''),
    order_date TEXT NOT NULL CHECK (order_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    promised_delivery_date TEXT CHECK (promised_delivery_date IS NULL OR promised_delivery_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    actual_delivery_date TEXT CHECK (actual_delivery_date IS NULL OR actual_delivery_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    status TEXT NOT NULL DEFAULT '已下单'
        CHECK (status IN ('已下单','供应商已确认','生产中','已发货','已到货','已对账','已完成')),
    is_urgent INTEGER NOT NULL DEFAULT 0 CHECK (is_urgent IN (0,1)),
    exception_note TEXT,
    contract_id INTEGER CHECK (contract_id IS NULL),
    is_exception INTEGER NOT NULL DEFAULT 0 CHECK (is_exception IN (0,1)),
    exception_reason TEXT,
    status_before_exception TEXT,
    exception_marked_at TEXT,
    CHECK (item_id IS NOT NULL OR (item_name_raw IS NOT NULL AND TRIM(item_name_raw) != ''))
);
CREATE INDEX IF NOT EXISTS idx_orders_supplier ON orders(supplier_id);
CREATE INDEX IF NOT EXISTS idx_orders_item ON orders(item_id);
CREATE INDEX IF NOT EXISTS idx_orders_status_due ON orders(status, promised_delivery_date);
"""
ORDER_EDITABLE = ("order_no", "supplier_id", "item_id", "item_name_raw", "quantity", "unit_price",
                  "currency", "order_date", "promised_delivery_date", "actual_delivery_date",
                  "is_urgent", "exception_note")
NUMERIC_FIELDS = {"quantity": SCALE, "unit_price": SCALE, "total_amount": MONEY_SCALE}


class OrderError(ValueError):
    """可定位到页面字段的业务错误，不包含原始数据库异常。"""
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def parse_decimal(value, field, optional=False):
    if value is None or value == "":
        if optional:
            return None
        raise OrderError(field, "数量必填")
    if not isinstance(value, str) or len(value) > 128 or not DECIMAL_PATTERN.fullmatch(value):
        raise OrderError(field, "请输入标准十进制数字，不允许空格、逗号、科学计数法或货币符号")
    number = Decimal(value)
    if not number.is_finite():
        raise OrderError(field, "数字必须是有限值")
    with localcontext() as ctx:
        ctx.prec = 160
        scaled = number * SCALE
        if scaled != scaled.to_integral_value():
            raise OrderError(field, "最多允许六位有效小数")
        if (field == "quantity" and number <= 0) or (field == "unit_price" and number < 0):
            raise OrderError(field, "数量必须大于 0，单价不能小于 0")
        if not INT64_MIN <= scaled <= INT64_MAX:
            raise OrderError(field, "数值超出 SQLite 64 位定点整数范围")
    return number


def encode_numbers(quantity, unit_price):
    q = parse_decimal(quantity, "quantity")
    p = parse_decimal(unit_price, "unit_price", optional=True)
    with localcontext() as ctx:
        ctx.prec = 160
        # 全程 Decimal；不计算 Q_int * P_int，也不在 SQL 中相乘。
        total = None if p is None else (q * p).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * MONEY_SCALE
        if total is not None and not INT64_MIN <= total <= INT64_MAX:
            raise OrderError("total_amount", "总额超出 SQLite 64 位整数范围，请调整数量或单价")
        return {"quantity": int(q * SCALE), "unit_price": None if p is None else int(p * SCALE),
                "total_amount": None if total is None else int(total)}


def format_fixed(value, scale):
    if value is None:
        return ""
    with localcontext() as ctx:
        ctx.prec = 160
        return format(Decimal(value) / scale, ".2f" if scale == MONEY_SCALE else "f")


def _decode_order(row):
    result = dict(row)
    for key, scale in NUMERIC_FIELDS.items():
        result[key] = None if result[key] is None else format_fixed(result[key], scale)
    return result


def _date_value(value, field, required=False):
    if value is None or value == "":
        if required:
            raise OrderError(field, "下单日期必填")
        return None
    if isinstance(value, date):
        value = value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise OrderError(field, "日期必须为 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise OrderError(field, "请输入真实有效的日期") from None
    return value


def _reference_id(value, field, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= INT64_MAX:
        raise OrderError(field, "请选择有效的供应商或物料")
    return value


def _check_references(connection, values):
    if not connection.execute("SELECT 1 FROM suppliers WHERE id = ?", (values["supplier_id"],)).fetchone():
        raise OrderError("supplier_id", "所选供应商不存在，请重新选择")
    if values["item_id"] is not None and not connection.execute(
            "SELECT 1 FROM items WHERE id = ?", (values["item_id"],)).fetchone():
        raise OrderError("item_id", "所选物料不存在，请重新选择")


def _normalise_order(connection, values, previous, snapshot_choice):
    if set(values) - set(ORDER_EDITABLE):
        raise OrderError("form", "包含不可编辑字段；状态与异常字段只能通过操作按钮修改")
    values = dict(values)
    # 编辑接口接收完整表单，避免隐式清空未提交字段。
    required = {"order_no", "supplier_id", "quantity", "order_date"}
    if required - set(values):
        raise OrderError("form", "请完整提交订单号、供应商、数量和下单日期")
    values["order_no"] = str(values.get("order_no") or "").strip()
    if not values["order_no"]:
        raise OrderError("order_no", "订单号必填")
    values["supplier_id"] = _reference_id(values.get("supplier_id"), "supplier_id")
    values["item_id"] = _reference_id(values.get("item_id"), "item_id", optional=True)
    _check_references(connection, values)
    values["item_name_raw"] = str(values.get("item_name_raw") or "").strip()
    item = connection.execute("SELECT name FROM items WHERE id = ?", (values["item_id"],)).fetchone()
    if previous and values["item_id"] != previous["item_id"] and item and values["item_name_raw"] and values["item_name_raw"] != item["name"]:
        if snapshot_choice not in ("sync", "keep"):
            raise OrderError("item_name_raw", "已更换关联物料，请明确选择同步为新物料名称或保留当前快照")
        if snapshot_choice == "sync":
            values["item_name_raw"] = item["name"]
    if not values["item_name_raw"] and item:
        values["item_name_raw"] = item["name"]
    if values["item_id"] is None and not values["item_name_raw"]:
        raise OrderError("item_name_raw", "请选择物料或填写物料名称")
    values.update(encode_numbers(values["quantity"], values.get("unit_price")))
    values["currency"] = str(values.get("currency") or "CNY").strip().upper() or "CNY"
    for field in ("order_date", "promised_delivery_date", "actual_delivery_date"):
        values[field] = _date_value(values.get(field), field, required=field == "order_date")
    values["is_urgent"] = values.get("is_urgent", 0)
    if values["is_urgent"] not in (0, 1):
        raise OrderError("is_urgent", "紧急标记必须为是或否")
    values["exception_note"] = values.get("exception_note") or None
    return values


def save_order(values, order_id=None, snapshot_choice=None):
    connection = get_connection()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = None
            if order_id is not None:
                previous = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if previous is None:
                    raise OrderError("form", "订单不存在")
            prepared = _normalise_order(connection, values, previous, snapshot_choice)
            try:
                if order_id is None:
                    prepared.update(initial_fields())
                    columns = list(prepared)
                    cursor = connection.execute(
                        f"INSERT INTO orders ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        list(prepared.values()))
                    return cursor.lastrowid
                columns = list(prepared)
                connection.execute(f"UPDATE orders SET {','.join(key + ' = ?' for key in columns)} WHERE id = ?",
                                   [*prepared.values(), order_id])
                return order_id
            except sqlite3.IntegrityError:
                _check_references(connection, prepared)
                duplicate = connection.execute("SELECT id FROM orders WHERE order_no = ?", (prepared["order_no"],)).fetchone()
                if duplicate and duplicate["id"] != order_id:
                    raise OrderError("order_no", "订单号已存在，请使用其他订单号") from None
                raise OrderError("form", "订单未保存，请检查字段约束") from None
    finally:
        connection.close()


def get_order(order_id):
    connection = get_connection()
    try:
        row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return _decode_order(row) if row else None
    finally:
        connection.close()


def list_orders(supplier_id=None, status_filter=None, date_start=None, date_end=None, urgent=None):
    clauses, params = ["(? IS NULL OR o.status = ?)"], [status_filter, status_filter]
    for column, value, operator in (("o.supplier_id", supplier_id, "="),
                                    ("o.order_date", date_start, ">="), ("o.order_date", date_end, "<="),
                                    ("o.is_urgent", urgent, "=")):
        if value is not None:
            clauses.append(f"{column} {operator} ?")
            params.append(value.isoformat() if isinstance(value, date) else value)
    connection = get_connection()
    try:
        query = """SELECT o.*, s.name AS supplier_name FROM orders o
                   JOIN suppliers s ON s.id = o.supplier_id"""
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return [_decode_order(row) for row in connection.execute(query + " ORDER BY o.id DESC", params)]
    finally:
        connection.close()


def perform_order_action(order_id, action, target_status=None, exception_reason=None):
    connection = get_connection()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                return False, "订单不存在"
            try:
                changes = action_fields(row, action, target_status, exception_reason)
            except ValueError as error:
                return False, str(error)
            connection.execute(f"UPDATE orders SET {','.join(key + ' = ?' for key in changes)} WHERE id = ?",
                               [*changes.values(), order_id])
        return True, "操作成功"
    finally:
        connection.close()


def delete_order(order_id):
    connection = get_connection()
    try:
        with connection:
            connection.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    finally:
        connection.close()


def _count_in_statuses(connection, statuses, extra_sql="", extra_params=()):
    if not statuses:
        return 0
    placeholders = ",".join("?" for _ in statuses)
    # 只拼占位符和内部固定 SQL，状态值全部参数绑定。
    return connection.execute(
        f"SELECT COUNT(*) FROM orders WHERE status IN ({placeholders})" + extra_sql,
        [*statuses, *extra_params]).fetchone()[0]


def count_due_within_window(today: Optional[date] = None) -> int:
    from datetime import timedelta
    today = today or date.today()
    days = load_config()["reminders"]["delivery_due_days"]
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("交期提醒天数必须为非负整数")
    end = today + timedelta(days=days)
    connection = get_connection()
    try:
        return _count_in_statuses(connection, PRE_ARRIVAL_STATUSES,
                                  " AND promised_delivery_date IS NOT NULL AND promised_delivery_date BETWEEN ? AND ?",
                                  (today.isoformat(), end.isoformat()))
    finally:
        connection.close()


def dashboard_counts(today: Optional[date] = None) -> dict:
    today = today or date.today()
    connection = get_connection()
    try:
        return {"active": _count_in_statuses(connection, ACTIVE_STATUSES),
                "due": count_due_within_window(today),
                "exceptions": connection.execute("SELECT COUNT(*) FROM orders WHERE is_exception = ?", (1,)).fetchone()[0]}
    finally:
        connection.close()
