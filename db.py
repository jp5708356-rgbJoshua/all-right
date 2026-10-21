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
            connection.executescript(QUOTE_SCHEMA)
            connection.executescript(RECONCILIATION_SCHEMA)
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
                raise ValueError("该记录已被订单或报价引用，不能删除") from None
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


QUOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    quoted_price INTEGER NOT NULL CHECK (typeof(quoted_price)='integer' AND quoted_price >= 0),
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency='CNY'),
    quote_date TEXT NOT NULL CHECK (quote_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    valid_until TEXT CHECK (valid_until IS NULL OR valid_until GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    lead_time_days INTEGER CHECK (lead_time_days IS NULL OR (typeof(lead_time_days)='integer' AND lead_time_days >= 0)),
    is_selected INTEGER NOT NULL DEFAULT 0 CHECK (is_selected IN (0,1)),
    selection_reason TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_quotes_item_date ON quotes(item_id, quote_date);
CREATE INDEX IF NOT EXISTS idx_quotes_supplier ON quotes(supplier_id);
"""
QUOTE_FIELDS = ("item_id", "supplier_id", "quoted_price", "currency", "quote_date", "valid_until",
                "lead_time_days", "is_selected", "selection_reason", "note")
QUOTE_COLUMNS = "q.id,q.item_id,q.supplier_id,q.quoted_price,q.currency,q.quote_date,q.valid_until,q.lead_time_days,q.is_selected,q.selection_reason,q.note"


class QuoteError(OrderError):
    """报价字段校验错误。"""


def quote_settings():
    """每次调用重读；纯配置逻辑，不发出页面警告。"""
    config = load_config()
    missing = "quotes" not in config or (isinstance(config["quotes"], dict) and "min_suppliers" not in config["quotes"])
    if missing:
        return 3, True
    if not isinstance(config["quotes"], dict):
        raise QuoteError("config", "quotes.min_suppliers 必须为正整数，请检查 config.yaml")
    value = config["quotes"]["min_suppliers"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QuoteError("config", "quotes.min_suppliers 必须为正整数，请检查 config.yaml")
    return value, False


def _decode_quote(row):
    result = dict(row)
    result["quoted_price"] = format_fixed(result["quoted_price"], SCALE)
    return result


def get_quote(quote_id):
    connection = get_connection()
    try:
        row = connection.execute(f"SELECT {QUOTE_COLUMNS} FROM quotes q WHERE q.id=?", (quote_id,)).fetchone()
        return _decode_quote(row) if row else None
    finally:
        connection.close()


def save_quote(values, quote_id=None):
    """编辑可部分提交；引用ID创建后不可变；所有校验和写入在同一事务。"""
    if set(values) - set(QUOTE_FIELDS):
        raise QuoteError("form", "包含不可编辑的报价字段")
    connection = get_connection()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = None
            if quote_id is not None:
                previous = connection.execute(f"SELECT {QUOTE_COLUMNS} FROM quotes q WHERE q.id=?", (quote_id,)).fetchone()
                if previous is None:
                    raise QuoteError("form", "报价不存在，请刷新列表")
            prepared = _decode_quote(previous) if previous else {}
            prepared.pop("id", None)
            prepared.update(values)
            for field in ("item_id", "supplier_id"):
                try:
                    prepared[field] = _reference_id(prepared.get(field), field)
                except OrderError as error:
                    raise QuoteError(error.field, str(error)) from None
                if previous and prepared[field] != previous[field]:
                    raise QuoteError(field, "报价创建后不能修改关联物料或供应商，请删除后重建")
            _check_references(connection, prepared)
            if prepared.get("quoted_price") is None or prepared.get("quoted_price") == "":
                raise QuoteError("quoted_price", "报价单价必填")
            try:
                price = parse_decimal(prepared["quoted_price"], "unit_price")
            except OrderError as error:
                raise QuoteError("quoted_price", "报价单价不能小于 0" if str(error).startswith("数量必须") else str(error)) from None
            with localcontext() as ctx:
                ctx.prec = 160
                prepared["quoted_price"] = int(price * SCALE)
            prepared["currency"] = prepared.get("currency") or "CNY"
            if prepared["currency"] != "CNY":
                raise QuoteError("currency", "本轮报价仅支持 CNY")
            if not prepared.get("quote_date"):
                raise QuoteError("quote_date", "报价日期必填")
            for field in ("quote_date", "valid_until"):
                prepared[field] = _date_value(prepared.get(field), field)
            lead = prepared.get("lead_time_days")
            if lead is None or lead == "":
                prepared["lead_time_days"] = None
            else:
                if (isinstance(lead, bool) or not isinstance(lead, (str, int))
                        or not re.fullmatch(r"[0-9]+", str(lead), re.ASCII) or len(str(lead)) > 19):
                    raise QuoteError("lead_time_days", "交期天数必须为非负整数或留空")
                lead = int(lead)
                if lead > INT64_MAX:
                    raise QuoteError("lead_time_days", "交期天数超出 SQLite 64 位整数范围")
                prepared["lead_time_days"] = lead
            selected = prepared.get("is_selected", 0)
            if not isinstance(selected, (bool, int)) or selected not in (0, 1):
                raise QuoteError("is_selected", "选中标记必须为是或否")
            prepared["is_selected"] = int(selected)
            for field in ("selection_reason", "note"):
                value = prepared.get(field)
                if value is not None and not isinstance(value, str):
                    raise QuoteError(field, "请输入文本或留空")
                prepared[field] = value.strip() or None if value is not None else None
            try:
                if quote_id is None:
                    cursor = connection.execute(
                        f"INSERT INTO quotes ({','.join(QUOTE_FIELDS)}) VALUES ({','.join('?' for _ in QUOTE_FIELDS)})",
                        [prepared[field] for field in QUOTE_FIELDS])
                    return cursor.lastrowid
                mutable = [field for field in QUOTE_FIELDS if field not in ("item_id", "supplier_id")]
                connection.execute(f"UPDATE quotes SET {','.join(field+'=?' for field in mutable)} WHERE id=?",
                                   [*[prepared[field] for field in mutable], quote_id])
                return quote_id
            except sqlite3.IntegrityError:
                raise QuoteError("form", "报价未保存，请检查物料、供应商及字段约束") from None
    finally:
        connection.close()


def delete_quote(quote_id, confirmed=False, selected_confirmed=False):
    connection = get_connection()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT is_selected FROM quotes WHERE id=?", (quote_id,)).fetchone()
            if row is None:
                raise QuoteError("delete", "报价不存在，请刷新列表")
            if confirmed is not True:
                raise QuoteError("delete", "请先确认永久删除这条报价")
            if row["is_selected"] and selected_confirmed is not True:
                raise QuoteError("delete", "该报价已标记为选中，删除后选择理由将一并丢失，确定删除？")
            connection.execute("DELETE FROM quotes WHERE id=?", (quote_id,))
    finally:
        connection.close()


def quote_comparison(item_id, today: Optional[date] = None):
    """仅报价与供应商；不访问订单状态。价格使用定点整数比较。"""
    today = today or date.today()
    connection = get_connection()
    try:
        rows = connection.execute(
            f"SELECT {QUOTE_COLUMNS},s.name AS supplier_name, "
            "(q.valid_until IS NULL OR q.valid_until>=?) AS is_valid "
            "FROM quotes q JOIN suppliers s ON s.id=q.supplier_id "
            "WHERE q.item_id=? ORDER BY q.quote_date DESC,q.id DESC", (today.isoformat(), item_id)).fetchall()
        valid = [row for row in rows if row["is_valid"]]
        minimum = min((row["quoted_price"] for row in valid), default=None)
        suppliers = {row["supplier_id"] for row in valid}
        tied = {row["supplier_id"] for row in valid if row["quoted_price"] == minimum}
        quotes = []
        for row in rows:
            decoded = _decode_quote(row)
            decoded["is_lowest"] = bool(row["is_valid"] and row["quoted_price"] == minimum)
            quotes.append(decoded)
        return {"quotes": quotes, "valid_supplier_count": len(suppliers),
                "lowest_price": None if minimum is None else format_fixed(minimum, SCALE),
                "tied_supplier_count": len(tied)}
    finally:
        connection.close()


def quotes_for_order(order_id, today: Optional[date] = None):
    connection = get_connection()
    try:
        row = connection.execute("SELECT item_id FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise QuoteError("order_id", "订单不存在")
        item_id = row["item_id"]
    finally:
        connection.close()
    return None if item_id is None else quote_comparison(item_id, today)


RECONCILIATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS reconciliation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
    goods_received INTEGER NOT NULL DEFAULT 0 CHECK (goods_received IN (0,1)),
    invoice_received INTEGER NOT NULL DEFAULT 0 CHECK (invoice_received IN (0,1)),
    payment_done INTEGER NOT NULL DEFAULT 0 CHECK (payment_done IN (0,1)),
    invoice_no TEXT,
    payment_date TEXT CHECK (payment_date IS NULL OR payment_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
);
"""
RECONCILIATION_FIELDS = ("goods_received", "invoice_received", "payment_done", "invoice_no", "payment_date")
RECONCILIATION_COLUMNS = "id,order_id,goods_received,invoice_received,payment_done,invoice_no,payment_date"
RECONCILIATION_FILTERS = {
    "全部": "",
    "货到未收票": "COALESCE(r.goods_received,0)=1 AND COALESCE(r.invoice_received,0)=0",
    "票到未付款": "COALESCE(r.invoice_received,0)=1 AND COALESCE(r.payment_done,0)=0",
    "三单未齐": "(COALESCE(r.goods_received,0)=0 OR COALESCE(r.invoice_received,0)=0 OR COALESCE(r.payment_done,0)=0)",
}


class ReconciliationError(OrderError):
    """对账字段校验错误，不暴露数据库原始错误。"""


def get_reconciliation(order_id):
    connection = get_connection()
    try:
        row = connection.execute(f"SELECT {RECONCILIATION_COLUMNS} FROM reconciliation WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_reconciliation(filter_name="全部"):
    """订单为行左连接。只读订单识别信息，不查询订单状态。"""
    if filter_name not in RECONCILIATION_FILTERS:
        raise ReconciliationError("filter", "请选择有效的对账筛选条件")
    connection = get_connection()
    try:
        query = """SELECT o.id AS order_id,o.order_no,s.name AS supplier_name,o.item_name_raw,o.order_date,
                          r.id AS reconciliation_id,
                          COALESCE(r.goods_received,0) AS goods_received,
                          COALESCE(r.invoice_received,0) AS invoice_received,
                          COALESCE(r.payment_done,0) AS payment_done,r.invoice_no,r.payment_date
                   FROM orders o JOIN suppliers s ON s.id=o.supplier_id
                   LEFT JOIN reconciliation r ON r.order_id=o.id"""
        clause = RECONCILIATION_FILTERS[filter_name]
        if clause:
            query += " WHERE " + clause
        return [dict(row) for row in connection.execute(query + " ORDER BY o.id DESC")]
    finally:
        connection.close()


def save_reconciliation(order_id, values):
    """order_id是定位参数；values仅五个业务字段。首次保存按需创建，含全0。"""
    if isinstance(order_id, bool) or not isinstance(order_id, int) or not 0 < order_id <= INT64_MAX:
        raise ReconciliationError("order_id", "请选择有效订单")
    if not isinstance(values, dict) or set(values) - set(RECONCILIATION_FIELDS):
        raise ReconciliationError("form", "只允许提交货、票、款标记、发票号和付款日期；关联订单不可编辑")
    connection = get_connection()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT id FROM orders WHERE id=?", (order_id,)).fetchone():
                raise ReconciliationError("order_id", "订单不存在，请刷新列表")
            previous = connection.execute(f"SELECT {RECONCILIATION_COLUMNS} FROM reconciliation WHERE order_id=?", (order_id,)).fetchone()
            prepared = {"goods_received": 0, "invoice_received": 0, "payment_done": 0,
                        "invoice_no": None, "payment_date": None}
            if previous:
                prepared.update({field: previous[field] for field in RECONCILIATION_FIELDS})
            prepared.update(values)
            for field in ("goods_received", "invoice_received", "payment_done"):
                value = prepared[field]
                if not isinstance(value, (bool, int)) or value not in (0, 1):
                    raise ReconciliationError(field, "货、票、款标记必须为是或否")
                prepared[field] = int(value)
            invoice = prepared["invoice_no"]
            if invoice is not None and not isinstance(invoice, str):
                raise ReconciliationError("invoice_no", "发票号请输入文本或留空")
            prepared["invoice_no"] = invoice.strip() or None if invoice is not None else None
            try:
                prepared["payment_date"] = _date_value(prepared["payment_date"], "payment_date")
            except OrderError as error:
                raise ReconciliationError(error.field, str(error)) from None
            try:
                if previous:
                    connection.execute(
                        f"UPDATE reconciliation SET {','.join(field+'=?' for field in RECONCILIATION_FIELDS)} WHERE order_id=?",
                        [*[prepared[field] for field in RECONCILIATION_FIELDS], order_id])
                    return previous["id"]
                cursor = connection.execute(
                    f"INSERT INTO reconciliation (order_id,{','.join(RECONCILIATION_FIELDS)}) "
                    f"VALUES ({','.join('?' for _ in range(len(RECONCILIATION_FIELDS)+1))})",
                    [order_id, *[prepared[field] for field in RECONCILIATION_FIELDS]])
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                raise ReconciliationError("form", "对账未保存，请检查字段约束并刷新订单列表") from None
    finally:
        connection.close()
