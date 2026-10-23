"""手动汇率。

规则（需求 §4.4）：
- 只维护一张手动汇率表，不接任何实时汇率 API
- 按「业务日期」取最近一条生效汇率（effective_date <= 业务日期 的最大者）
- 基准币种与自身换算恒为 1.0，不用录
- 折算结果写进 base_amount 作为快照，原始外币金额永不覆盖
- 查不到汇率时返回 None，由调用方拦截保存，不允许存半条脏数据
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import config, db

COMMON_CURRENCIES = ("USD", "EUR", "JPY", "GBP", "HKD", "AUD", "CAD", "SGD", "TWD")


class ValidationError(ValueError):
    """汇率录入不合法。"""


def base_currency() -> str:
    return str(config.get("fx.base_currency", "CNY") or "CNY").strip().upper()


def _check_date(value, field: str = "生效日期") -> str:
    if value in (None, ""):
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field}需要 YYYY-MM-DD 格式，收到：{value}") from exc


def get_rate(
    from_currency: str,
    to_currency: str | None = None,
    on_date: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> float | None:
    """取最近生效汇率。同币种恒为 1.0；查不到返回 None。"""
    src = (from_currency or "").strip().upper()
    dst = (to_currency or base_currency()).strip().upper()
    if not src:
        return None
    if src == dst:
        return 1.0

    day = _check_date(on_date, "业务日期")
    row = db.row(
        """
        SELECT rate FROM fx_rates
        WHERE from_currency = ? AND to_currency = ? AND effective_date <= ?
        ORDER BY effective_date DESC, id DESC
        LIMIT 1
        """,
        (src, dst, day),
        conn,
    )
    return float(row["rate"]) if row else None


def to_base(
    amount: float | None,
    currency: str | None,
    on_date: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> float | None:
    """折算成基准币种。缺汇率返回 None。"""
    if amount is None:
        return None
    rate = get_rate(currency or base_currency(), None, on_date, conn)
    if rate is None:
        return None
    return round(float(amount) * rate, 4)


def describe_missing(currency: str, on_date: str | None = None) -> str:
    """缺汇率时给用户看的提示，指明去哪补、补到哪天。"""
    day = on_date or date.today().isoformat()
    return (
        f"缺少 {currency.upper()} → {base_currency()} 在 {day} 及之前的汇率。"
        "请到「设置」页的汇率表里补一条生效日期不晚于该日的汇率。"
    )


def list_rates(conn: sqlite3.Connection | None = None) -> list[dict]:
    return db.rows(
        "SELECT * FROM fx_rates ORDER BY effective_date DESC, id DESC",
        (),
        conn,
    )


def upsert_rate(
    from_currency: str,
    rate: float,
    effective_date: str | None = None,
    to_currency: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    src = (from_currency or "").strip().upper()
    if not src:
        raise ValidationError("币种必填")
    if len(src) < 2 or len(src) > 6:
        raise ValidationError(f"币种代码看起来不对：{src}")
    if src == (to_currency or base_currency()).upper():
        raise ValidationError("同币种不需要录汇率")

    try:
        value = float(rate)
    except (TypeError, ValueError) as exc:
        raise ValidationError("汇率必须是数字") from exc
    if value <= 0:
        raise ValidationError("汇率必须大于 0")

    day = _check_date(effective_date)

    db.execute(
        """
        INSERT INTO fx_rates (from_currency, to_currency, rate, effective_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(from_currency, to_currency, effective_date)
        DO UPDATE SET rate = excluded.rate
        """,
        (src, (to_currency or base_currency()).upper(), value, day),
        conn,
    )
    row = db.row(
        "SELECT id FROM fx_rates WHERE from_currency = ? AND to_currency = ? AND effective_date = ?",
        (src, (to_currency or base_currency()).upper(), day),
        conn,
    )
    return int(row["id"]) if row else 0


def delete_rate(rate_id: int, conn: sqlite3.Connection | None = None) -> None:
    db.execute("DELETE FROM fx_rates WHERE id = ?", (rate_id,), conn)


def known_currencies(conn: sqlite3.Connection | None = None) -> list[str]:
    """汇率表里出现过的币种 + 常用币种，供页面下拉用。"""
    rows = db.rows("SELECT DISTINCT from_currency FROM fx_rates ORDER BY from_currency", (), conn)
    used = [r["from_currency"] for r in rows]
    return sorted(set(used) | set(COMMON_CURRENCIES) | {base_currency()})
