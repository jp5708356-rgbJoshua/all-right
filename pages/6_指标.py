"""轻量指标：OTD、平均订单周期、异常订单占比。只读，不触发任何状态变更。"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db, metrics

st.set_page_config(page_title="轻量指标", page_icon="📈", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _boot() -> None:
    db.init_db()


_boot()

st.title("轻量指标")
from core import workspace
workspace.banner(st)

st.caption("只读计算，不落表、不改任何订单状态。口径写在下面，方便你手工核对。")

c1, c2 = st.columns(2)
date_from = c1.date_input("下单日期起（可空 = 全部）", value=None)
date_to = c2.date_input("下单日期止（可空 = 全部）", value=None)

data = metrics.compute(
    date_from=str(date_from) if date_from else None,
    date_to=str(date_to) if date_to else None,
)

m1, m2, m3 = st.columns(3)
m1.metric(
    "准时交付率",
    "—" if data["otd_rate"] is None else f"{data['otd_rate']:.1f}%",
    help=f"分子 {data['otd_on_time']} 笔准时 / 分母 {data['otd_sample']} 笔有承诺交期的已到货订单",
)
m2.metric(
    "平均订单周期",
    "—" if data["avg_cycle_days"] is None else f"{data['avg_cycle_days']:.1f} 天",
    help=f"按 {data['cycle_sample']} 笔已到货订单计算，周期 = 实际到货日 - 下单日",
)
m3.metric(
    "异常订单占比",
    "—" if data["exception_rate"] is None else f"{data['exception_rate']:.1f}%",
    help=f"分子 {data['exception_orders']} 笔曾标记异常 / 分母 {data['total_orders']} 笔订单（不含已取消）",
)

st.info(
    f"口径：准时交付率分母只算「已到货且有承诺交期」的订单，本次排除了 {data['otd_excluded_no_promise']} 笔"
    f"无承诺交期的到货订单（不惩罚未承诺交期）。已取消订单不进异常占比的分母和分子。"
)

st.divider()

tab1, tab2 = st.tabs(["交付与周期明细", "异常订单明细"])

with tab1:
    detail_limit = 500
    detail = metrics.delivery_detail(
        date_from=str(date_from) if date_from else None,
        date_to=str(date_to) if date_to else None,
        limit=detail_limit,
    )
    if detail:
        st.dataframe(
            [
                {
                    "订单号": r["order_no"],
                    "供应商": r["supplier_name"],
                    "品名": r["item_name_raw"],
                    "下单日": r["order_date"],
                    "承诺交期": r["promised_delivery_date"] or "未定",
                    "实际到货": r["actual_delivery_date"],
                    "是否准时": "—" if r["on_time"] is None else ("准时" if r["on_time"] else "逾期"),
                    "周期(天)": r["cycle_days"] if r["cycle_days"] is not None else "—",
                }
                for r in detail
            ],
            use_container_width=True,
            hide_index=True,
        )
        if data["otd_sample"] and data["cycle_sample"]:
            st.caption(
                f"核对（全量统计，不限于下表）：{data['otd_sample']} 笔有承诺交期，"
                f"其中准时 {data['otd_on_time']} 笔 → 准时交付率 = {data['otd_on_time']}/{data['otd_sample']}；"
                f"{data['cycle_sample']} 笔有周期数据，平均 {data['avg_cycle_days']:.1f} 天"
            )
        else:
            st.caption("数据不足，无法核对。")

        if data["delivered_orders"] > len(detail):
            st.caption(
                f"共 {data['delivered_orders']} 笔已到货订单，下表只显示最近 {len(detail)} 笔；"
                "上面的指标按全量计算。"
            )
    else:
        st.info("所选范围内没有已到货的订单。")

with tab2:
    exc = metrics.exception_detail(
        date_from=str(date_from) if date_from else None,
        date_to=str(date_to) if date_to else None,
        limit=detail_limit,
    )
    if exc:
        st.dataframe(
            [
                {
                    "订单号": r["order_no"],
                    "供应商": r["supplier_name"],
                    "当前状态": r["status"],
                    "异常次数": r["exception_times"],
                    "最近一次": r["last_exception_at"],
                    "异常类别": r["categories"] or "—",
                }
                for r in exc
            ],
            use_container_width=True,
            hide_index=True,
        )
        if data["exception_rate"] is not None:
            st.caption(
                f"{len(exc)} 笔订单曾标记异常 / 统计期 {data['total_orders']} 笔订单 → "
                f"异常占比 {data['exception_rate']:.1f}%"
            )
        else:
            st.caption(f"{len(exc)} 笔订单曾标记异常")
    else:
        st.info("所选范围内没有异常记录。")
