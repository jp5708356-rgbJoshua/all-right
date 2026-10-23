"""对账视图：货 / 票 / 款三列对勾，支持三种缺口筛选。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db, repo_recon as rr

st.set_page_config(page_title="对账", page_icon="✅", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _boot() -> None:
    db.init_db()


_boot()

st.title("对账")
from core import workspace
workspace.banner(st)

st.caption("货到、票到、款付三个勾。打勾会自动记当天日期，取消打勾会一并清掉日期。")

flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)


def _as_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _money(amount, currency: str = "CNY") -> str:
    if amount is None:
        return "—"
    symbol = "¥" if currency == "CNY" else f"{currency} "
    return f"{symbol}{amount:,.2f}"


mode = st.radio("筛选", rr.MODES, horizontal=True)
page_limit = 200
total = rr.count_rows(mode)
rows = rr.list_rows(mode, limit=page_limit)

if not rows:
    st.info("没有符合条件的订单。")
    st.stop()

st.dataframe(
    [
        {
            "订单号": r["order_no"],
            "供应商": r["supplier_name"],
            "品名": r["item_name_raw"],
            "金额": _money(r["total_amount"], r["currency"]),
            "状态": r["status"],
            "货": "✅" if r["goods_received"] else "⬜",
            "票": "✅" if r["invoice_received"] else "⬜",
            "款": "✅" if r["payment_done"] else "⬜",
            "发票号": r["invoice_no"] or "—",
            "三单齐": "是" if r["complete"] else "否",
        }
        for r in rows
    ],
    use_container_width=True,
    hide_index=True,
)

incomplete = sum(1 for r in rows if not r["complete"])
if total > len(rows):
    st.caption(
        f"共 {total} 条，只显示最近 {len(rows)} 条（本页 {incomplete} 条三单未齐）。"
        "用上面的筛选缩小范围，或按日期分段查看。"
    )
else:
    st.caption(f"共 {total} 条，其中 {incomplete} 条三单未齐。")

st.divider()
st.subheader("登记对账")

options = {f"{r['order_no']}  ·  {r['supplier_name']}  ·  {r['item_name_raw']}": r["order_id"] for r in rows}
picked = st.selectbox("选择订单", list(options))
order_id = options[picked]
row = next(r for r in rows if r["order_id"] == order_id)

if row["complete"]:
    st.success("这单货 / 票 / 款都齐了。可以到「订单」页把它推进到「已对账」。")

with st.form(f"recon_form_{order_id}"):
    c1, c2, c3 = st.columns(3)

    goods = c1.checkbox("货已到", value=bool(row["goods_received"]), key=f"goods_{order_id}")
    goods_date = c1.date_input("到货日期", value=_as_date(row["goods_received_date"]), key=f"goods_d_{order_id}")

    invoice = c2.checkbox("票已收", value=bool(row["invoice_received"]), key=f"inv_{order_id}")
    invoice_date = c2.date_input(
        "收票日期", value=_as_date(row["invoice_received_date"]), key=f"inv_d_{order_id}"
    )

    payment = c3.checkbox("款已付", value=bool(row["payment_done"]), key=f"pay_{order_id}")
    payment_date = c3.date_input(
        "付款日期", value=_as_date(row["payment_date"]), key=f"pay_d_{order_id}"
    )

    invoice_no = st.text_input("发票号（可留空）", value=row["invoice_no"] or "")

    saved = st.form_submit_button("保存对账状态", type="primary")

if saved:
    try:
        result = rr.upsert(
            order_id,
            {
                "goods_received": goods,
                "goods_received_date": goods_date,
                "invoice_received": invoice,
                "invoice_received_date": invoice_date,
                "payment_done": payment,
                "payment_date": payment_date,
                "invoice_no": invoice_no,
            },
        )
        if rr.is_complete(result):
            st.session_state["flash"] = f"{row['order_no']} 三单已齐，可到「订单」页推进到「已对账」"
        else:
            st.session_state["flash"] = f"{row['order_no']} 对账状态已保存"
        st.rerun()
    except (ValueError, TypeError) as exc:
        st.error(str(exc))
