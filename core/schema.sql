-- 个人采购工作台 · 全量表结构
-- 建表顺序即外键依赖顺序：suppliers/items -> contracts -> orders -> quotes/reconciliation/order_exceptions -> fx_rates
-- 所有语句 IF NOT EXISTS，可重复执行。

CREATE TABLE IF NOT EXISTS schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- 1. 供应商
CREATE TABLE IF NOT EXISTS suppliers (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    credit_code           TEXT,
    category              TEXT,
    contact_name          TEXT,
    contact_phone         TEXT,
    bank_account          TEXT,
    qualification_expiry  TEXT,
    risk_level            TEXT NOT NULL DEFAULT '低' CHECK (risk_level IN ('低', '中', '高')),
    status                TEXT NOT NULL DEFAULT '活跃' CHECK (status IN ('活跃', '暂停', '淘汰')),
    risk_notes            TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at            TEXT
);

-- 2. 物料 / 服务
CREATE TABLE IF NOT EXISTS items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    code                  TEXT UNIQUE,
    name                  TEXT NOT NULL,
    spec                  TEXT,
    unit                  TEXT,
    historical_low_price  REAL,
    historical_high_price REAL,
    created_at            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at            TEXT
);

-- 3. 合同 / 框架协议（依赖 suppliers）
CREATE TABLE IF NOT EXISTS contracts (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no                TEXT,
    supplier_id                INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    title                      TEXT NOT NULL,
    start_date                 TEXT,
    end_date                   TEXT,
    price_valid_until          TEXT,
    auto_renewal_reminder_days INTEGER NOT NULL DEFAULT 30,
    file_path                  TEXT,
    created_at                 TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 4. 订单（核心表，依赖 suppliers / items / contracts）
CREATE TABLE IF NOT EXISTS orders (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no                TEXT NOT NULL UNIQUE,
    supplier_id             INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    item_id                 INTEGER REFERENCES items(id) ON DELETE SET NULL,
    item_name_raw           TEXT NOT NULL,
    quantity                REAL NOT NULL,
    unit_price              REAL,
    total_amount            REAL,
    currency                TEXT NOT NULL DEFAULT 'CNY',
    base_amount             REAL,
    order_date              TEXT NOT NULL,
    promised_delivery_date  TEXT,
    actual_delivery_date    TEXT,
    status                  TEXT NOT NULL DEFAULT '已下单'
                            CHECK (status IN ('已下单', '供应商已确认', '生产中', '已发货',
                                              '已到货', '已对账', '已完成', '异常', '已取消')),
    is_urgent               INTEGER NOT NULL DEFAULT 0 CHECK (is_urgent IN (0, 1)),
    exception_note          TEXT,
    exception_category      TEXT CHECK (exception_category IS NULL OR exception_category IN ('质量', '交期', '单据', '其他')),
    status_before_exception TEXT,
    contract_id             INTEGER REFERENCES contracts(id) ON DELETE SET NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at              TEXT
);

-- 5. 比价记录（依赖 items / suppliers）
-- 供应商与物料用 RESTRICT：有历史报价就不许硬删，避免把比价历史一起带走
CREATE TABLE IF NOT EXISTS quotes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    supplier_id      INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    quoted_price     REAL NOT NULL,
    currency         TEXT NOT NULL DEFAULT 'CNY',
    base_amount      REAL,
    quote_date       TEXT NOT NULL,
    valid_until      TEXT,
    lead_time_days   INTEGER,
    is_selected      INTEGER NOT NULL DEFAULT 0 CHECK (is_selected IN (0, 1)),
    selection_reason TEXT,
    note             TEXT,
    import_source    TEXT NOT NULL DEFAULT 'manual' CHECK (import_source IN ('manual', 'ai')),
    raw_payload      TEXT,
    edited_by_user   INTEGER NOT NULL DEFAULT 0 CHECK (edited_by_user IN (0, 1)),
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 6. 对账标记（依赖 orders）
CREATE TABLE IF NOT EXISTS reconciliation (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id              INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
    goods_received        INTEGER NOT NULL DEFAULT 0 CHECK (goods_received IN (0, 1)),
    goods_received_date   TEXT,
    invoice_received      INTEGER NOT NULL DEFAULT 0 CHECK (invoice_received IN (0, 1)),
    invoice_received_date TEXT,
    invoice_no            TEXT,
    payment_done          INTEGER NOT NULL DEFAULT 0 CHECK (payment_done IN (0, 1)),
    payment_date          TEXT,
    updated_at            TEXT
);

-- 7. 订单异常历史（依赖 orders）
CREATE TABLE IF NOT EXISTS order_exceptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    from_status TEXT NOT NULL,
    category    TEXT,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at TEXT
);

-- 8. 手动汇率
CREATE TABLE IF NOT EXISTS fx_rates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    from_currency  TEXT NOT NULL,
    to_currency    TEXT NOT NULL DEFAULT 'CNY',
    rate           REAL NOT NULL CHECK (rate > 0),
    effective_date TEXT NOT NULL,
    UNIQUE (from_currency, to_currency, effective_date)
);

-- 索引：按实际查询形态设计，等值列在前、范围列在后
-- 单列 idx_orders_status / idx_quotes_item 被复合索引前缀覆盖，删掉避免重复维护

DROP INDEX IF EXISTS idx_orders_status;
DROP INDEX IF EXISTS idx_quotes_item;

CREATE INDEX IF NOT EXISTS idx_orders_supplier       ON orders (supplier_id);
CREATE INDEX IF NOT EXISTS idx_orders_order_date     ON orders (order_date);
CREATE INDEX IF NOT EXISTS idx_orders_promised       ON orders (promised_delivery_date);
-- 待办第 1 类：status 过滤 + 交期范围
CREATE INDEX IF NOT EXISTS idx_orders_status_promised ON orders (status, promised_delivery_date);
-- 列表按状态 + 下单日期筛选
CREATE INDEX IF NOT EXISTS idx_orders_status_date     ON orders (status, order_date);
-- 指标页按实际到货日过滤并排序
CREATE INDEX IF NOT EXISTS idx_orders_actual         ON orders (actual_delivery_date);

CREATE INDEX IF NOT EXISTS idx_quotes_supplier       ON quotes (supplier_id);
CREATE INDEX IF NOT EXISTS idx_quotes_quote_date     ON quotes (quote_date);
-- 比价按物料取最新报价
CREATE INDEX IF NOT EXISTS idx_quotes_item_date      ON quotes (item_id, quote_date);

-- 质量分统计按订单 + 异常类别
CREATE INDEX IF NOT EXISTS idx_exceptions_order      ON order_exceptions (order_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_order_cat  ON order_exceptions (order_id, category);

-- 对账待办：货到未收票 / 票到未付款
CREATE INDEX IF NOT EXISTS idx_recon_goods           ON reconciliation (goods_received, invoice_received);
CREATE INDEX IF NOT EXISTS idx_recon_invoice         ON reconciliation (invoice_received, payment_done);

-- 高风险供应商筛选
CREATE INDEX IF NOT EXISTS idx_suppliers_risk        ON suppliers (risk_level);

CREATE INDEX IF NOT EXISTS idx_contracts_supplier    ON contracts (supplier_id);
-- 合同到期检查按两个日期扫
CREATE INDEX IF NOT EXISTS idx_contracts_end         ON contracts (end_date);
CREATE INDEX IF NOT EXISTS idx_contracts_price       ON contracts (price_valid_until);

CREATE INDEX IF NOT EXISTS idx_fx_lookup             ON fx_rates (from_currency, to_currency, effective_date);

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', '1');

-- 日常跟进记录：仅记录沟通，不自动更改交期或订单状态。
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    note TEXT NOT NULL,
    supplier_reply TEXT,
    next_date TEXT,
    closed INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(closed, next_date);
CREATE INDEX IF NOT EXISTS idx_followups_order ON followups(order_id, id);
