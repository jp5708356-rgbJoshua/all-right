"""供应商评分卡（QCDS，只读计算）。

数据只来自 orders / order_exceptions / quotes 已有字段，不新增手填 KPI。
权重与评级阈值全部来自 config.yaml，代码里不出现 90 / 80 / 70 这类字面量。

口径：
- delivery（交付）= 准时到货笔数 / 已到货且有承诺交期笔数 × 100
- quality（质量）= (1 - 质量类异常订单数 / 统计期订单数) × 100
- cost（成本）  = 100 - 平均溢价%，溢价 = (本家报价 - 同物料最低有效报价) / 最低有效报价 × 100
- service（服务）= config.scorecard.service_manual 里的人工分，可留空

空缺维度不扣分：weighted_total = Σ(score×w) / Σ(w)，分母只累计有值的维度。
否则没填 service 的供应商永远拿不到 S，评分卡会变成「谁被打过分谁高」。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import config, db, repo_quotes

DIMENSIONS = ("quality", "cost", "delivery", "service")
DIMENSION_LABELS = {
    "quality": "质量 Q",
    "cost": "成本 C",
    "delivery": "交付 D",
    "service": "服务 S",
}


def _period_clause(period: str | None, alias: str = "o") -> tuple[str, list]:
    if not period:
        return "", []
    if len(period) != 7 or period[4] != "-":
        raise ValueError("period 需要 YYYY-MM 格式")
    return f" AND {alias}.order_date LIKE ?", [f"{period}%"]


def _delivery_score(supplier_id: int, period: str | None, conn) -> dict:
    clause, params = _period_clause(period)
    row = db.row(
        f"""
        SELECT
            SUM(CASE WHEN o.promised_delivery_date IS NOT NULL THEN 1 ELSE 0 END) AS sample,
            SUM(CASE WHEN o.promised_delivery_date IS NOT NULL
                      AND o.actual_delivery_date <= o.promised_delivery_date THEN 1 ELSE 0 END) AS on_time,
            SUM(CASE WHEN o.actual_delivery_date IS NOT NULL THEN 1 ELSE 0 END) AS delivered
        FROM orders o
        WHERE o.supplier_id = ?
          AND o.actual_delivery_date IS NOT NULL
          AND o.status <> '已取消'
          {clause}
        """,
        tuple([supplier_id] + params),
        conn,
    )
    sample = int(row["sample"] or 0)
    on_time = int(row["on_time"] or 0)
    score = (on_time / sample * 100) if sample else None
    return {
        "score": score,
        "detail": f"{on_time} / {sample} 笔准时（已到货 {int(row['delivered'] or 0)} 笔）",
        "sample": sample,
    }


def _quality_score(supplier_id: int, period: str | None, conn) -> dict:
    clause, params = _period_clause(period)
    total = int(
        db.scalar(
            f"SELECT COUNT(*) FROM orders o WHERE o.supplier_id = ? AND o.status <> '已取消' {clause}",
            tuple([supplier_id] + params),
            conn,
        )
        or 0
    )
    if total == 0:
        return {"score": None, "detail": "统计期内没有订单", "sample": 0}

    bad = int(
        db.scalar(
            f"""
            SELECT COUNT(DISTINCT o.id)
            FROM order_exceptions e
            JOIN orders o ON o.id = e.order_id
            WHERE o.supplier_id = ?
              AND o.status <> '已取消'
              AND e.category = '质量'
              {clause}
            """,
            tuple([supplier_id] + params),
            conn,
        )
        or 0
    )
    score = max(0.0, (1 - bad / total) * 100)
    return {"score": score, "detail": f"质量类异常 {bad} 笔 / 订单 {total} 笔", "sample": total}


def _cost_score(supplier_id: int, period: str | None, conn) -> dict:
    """按物料比较：本家最新有效报价 vs 同物料所有供应商的最低有效报价。

    期间只决定「哪些物料的报价参与统计」（按 quote_date 落期），
    基准价仍用当前有效最低价——那是时点概念，翻旧账没有意义。

    实现上一次性取齐所有相关物料的报价再分组计算，不按物料逐条查库。
    """
    sql = "SELECT DISTINCT item_id FROM quotes WHERE supplier_id = ?"
    params: list = [supplier_id]
    if period:
        sql += " AND quote_date LIKE ?"
        params.append(f"{period}%")

    item_ids = [int(row["item_id"]) for row in db.rows(sql, tuple(params), conn)]
    if not item_ids:
        return {"score": None, "detail": "统计期内没有可比的报价", "sample": 0}

    grouped: dict[int, list[dict]] = {item_id: [] for item_id in item_ids}
    for row in repo_quotes.list_quotes_by_items(item_ids, conn):
        grouped.setdefault(row["item_id"], []).append(row)

    premiums: list[float] = []
    compared_items = 0

    for item_id in item_ids:
        data = repo_quotes.analyze_quotes(grouped.get(item_id, []))
        if not data["lowest"]:
            continue
        mine = next((q for q in data["priced"] if q["supplier_id"] == supplier_id), None)
        if not mine:
            continue
        lowest_price = data["lowest"]["compare_price"]
        if not lowest_price:
            continue
        premiums.append((mine["compare_price"] - lowest_price) / lowest_price * 100)
        compared_items += 1

    if not premiums:
        return {"score": None, "detail": "统计期内没有可比的报价", "sample": 0}

    avg_premium = sum(premiums) / len(premiums)
    score = max(0.0, min(100.0, 100 - avg_premium))
    return {
        "score": score,
        "detail": f"{compared_items} 个物料可比，平均溢价 {avg_premium:+.1f}%",
        "sample": compared_items,
    }


def _service_score(supplier_id: int) -> dict:
    manual = config.get("scorecard.service_manual") or {}
    if not isinstance(manual, dict):
        return {"score": None, "detail": "配置格式不对", "sample": 0}

    raw = manual.get(supplier_id, manual.get(str(supplier_id)))
    if raw is None:
        return {"score": None, "detail": "未配置人工分", "sample": 0}
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return {"score": None, "detail": "人工分不是数字", "sample": 0}

    score = max(0.0, min(100.0, score))
    return {"score": score, "detail": "来自 config.yaml 人工分", "sample": 1}


def grade_of(total: float | None) -> str | None:
    """按 config 的阈值评级。低于最低阈值为 C。"""
    if total is None:
        return None
    thresholds = config.grade_thresholds()
    for name in sorted(thresholds, key=lambda k: -thresholds[k]):
        if total >= thresholds[name]:
            return name
    return "C"


def compute(supplier_id: int, period: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    supplier = db.row("SELECT id, name FROM suppliers WHERE id = ?", (supplier_id,), conn)
    if not supplier:
        raise ValueError(f"供应商 #{supplier_id} 不存在")

    weights = config.scorecard_weights()

    raw = {
        "delivery": _delivery_score(supplier_id, period, conn),
        "quality": _quality_score(supplier_id, period, conn),
        "cost": _cost_score(supplier_id, period, conn),
        "service": _service_score(supplier_id),
    }

    dimensions = {}
    weighted_sum = 0.0
    weight_sum = 0.0
    for key in DIMENSIONS:
        item = raw[key]
        weight = float(weights.get(key, 0.0))
        score = item["score"]
        dimensions[key] = {
            "label": DIMENSION_LABELS[key],
            "score": score,
            "weight": weight,
            "detail": item["detail"],
            "sample": item["sample"],
        }
        if score is not None and weight > 0:
            weighted_sum += score * weight
            weight_sum += weight

    total = (weighted_sum / weight_sum) if weight_sum > 0 else None
    return {
        "supplier_id": supplier_id,
        "supplier_name": supplier["name"],
        "period": period or "全部",
        "dimensions": dimensions,
        "weighted_total": total,
        "grade": grade_of(total),
        "counted_weight": weight_sum,
        "empty_dimensions": [k for k, v in dimensions.items() if v["score"] is None],
        "computed_at": date.today().isoformat(),
    }
