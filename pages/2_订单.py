"""订单列表、详情、状态流转。

状态按钮一律来自 state_machine.next_states()，本页不写任何状态判断分支。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import (
    db,
    repo_fx,
    repo_items,
    repo_orders as ro,
    repo_suppliers,
    state_machine as sm,
)

st.set_page_config(page_title="订单", page_icon="🧾", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _boot() -> None:
    db.init_db()


_boot()

st.title("订单")
from core import workspace
workspace.banner(st)


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


suppliers = repo_suppliers.list_suppliers()
items = repo_items.list_items()
supplier_options = {f"{s['name']}（{s['risk_level']}风险）": s["id"] for s in suppliers}
item_options = {"不指定": None, **{f"{i['name']}{' · ' + i['spec'] if i['spec'] else ''}": i["id"] for i in items}}

tab_list, tab_new = st.tabs(["订单列表与详情", "新建订单"])

# ---------------------------------------------------------------- 列表
with tab_list:
    f1, f2, f3 = st.columns([2, 1, 1])
    keyword = f1.text_input("搜索", placeholder="订单号 / 品名")
    supplier_filter = f2.selectbox("供应商", ["全部", *supplier_options.keys()])
    status_filter = f3.selectbox("状态", ["全部", *sm.ALL_STATUSES])

    f4, f5, f6 = st.columns([1, 1, 1])
    date_from = f4.date_input("下单日期起", value=None)
    date_to = f5.date_input("下单日期止", value=None)
    only_urgent = f6.checkbox("只看紧急")
    only_active = st.checkbox("只看进行中")

    filters = {
        "keyword": keyword,
        "supplier_id": None if supplier_filter == "全部" else supplier_options[supplier_filter],
        "status": None if status_filter == "全部" else status_filter,
        "date_from": str(date_from) if date_from else None,
        "date_to": str(date_to) if date_to else None,
        "only_urgent": only_urgent,
        "only_active": only_active,
    }
    page_limit = 200
    total = ro.count_orders(**filters)
    records = ro.list_orders(**filters, limit=page_limit)

    if not records:
        st.info("没有匹配的订单。切到「新建订单」建第一笔。")
    else:
        st.dataframe(
            [
                {
                    "编号": r["id"],
                    "订单号": r["order_no"],
                    "供应商": r["supplier_name"],
                    "品名": r["item_name_raw"],
                    "数量": r["quantity"],
                    "总价": _money(r["total_amount"], r["currency"]),
                    "下单日": r["order_date"],
                    "承诺交期": r["promised_delivery_date"] or "—",
                    "状态": r["status"],
                    "紧急": "是" if r["is_urgent"] else "",
                }
                for r in records
            ],
            use_container_width=True,
            hide_index=True,
        )
        if total > len(records):
            st.caption(
                f"共 {total} 条，当前只显示最近 {len(records)} 条。"
                "用上面的筛选或日期范围缩小，或按月份分段查看。"
            )
        else:
            st.caption(f"共 {total} 条。")

        st.divider()
        options = {f"#{r['id']}  {r['order_no']}  ·  {r['item_name_raw']}": r["id"] for r in records}
        picked = st.selectbox("选择订单查看详情", list(options))
        order_id = options[picked]
        order = ro.get_order(order_id)

        if order:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("当前状态", order["status"])
            m2.metric("数量", f"{order['quantity']:g} {order['item_unit'] or ''}".strip())
            m3.metric("总价", _money(order["total_amount"], order["currency"]))
            m4.metric("供应商风险", order["supplier_risk_level"])

            st.markdown(
                f"**订单号** {order['order_no']}　|　**供应商** {order['supplier_name']}　|　"
                f"**品名** {order['item_name_raw']}　|　**下单日** {order['order_date']}　|　"
                f"**承诺交期** {order['promised_delivery_date'] or '未定'}　|　"
                f"**实际到货** {order['actual_delivery_date'] or '未到'}　|　"
                f"**紧急** {'是' if order['is_urgent'] else '否'}"
            )

            if order["status"] == sm.EXCEPTION:
                st.error(
                    f"当前处于异常：{order['exception_note'] or '（无原因记录）'}"
                    f"　类别：{order['exception_category'] or '未分类'}"
                    f"　标记前状态：{order['status_before_exception'] or '未知'}"
                )

            # ---- 状态流转 ----
            st.subheader("状态流转")
            nexts = sm.next_states(order["status"])

            if nexts:
                cols = st.columns(len(nexts) + 2)
                for idx, target in enumerate(nexts):
                    if cols[idx].button(f"推进到「{target}」", type="primary", key=f"adv_{order_id}_{target}"):
                        try:
                            result = ro.update_status(order_id, target)
                            msg = f"{order['order_no']}：{result['from']} → {result['to']}"
                            if result["auto_filled_delivery"]:
                                msg += "（已按今天记实际到货日，可在下方编辑修改）"
                            st.session_state["flash"] = msg
                            st.rerun()
                        except (sm.StateError, ro.ValidationError) as exc:
                            st.error(str(exc))
            else:
                st.info(f"「{order['status']}」是终态，没有可流转的下一步。")

            # 只有异常态才渲染恢复按钮（and 短路，非异常态时 button 不会被调用）
            if sm.can_restore(order["status"]) and st.button("从异常恢复", key=f"restore_{order_id}"):
                try:
                    target = ro.restore_exception(order_id)
                    st.session_state["flash"] = f"{order['order_no']} 已恢复到「{target}」"
                    st.rerun()
                except (sm.StateError, ro.ValidationError) as exc:
                    st.error(str(exc))

            if sm.can_mark_exception(order["status"]):
                with st.expander("标记异常"):
                    with st.form(f"exception_form_{order_id}"):
                        category = st.selectbox("异常类别 *", ro.EXCEPTION_CATEGORIES)
                        reason = st.text_area("异常原因 *", height=80)
                        marked = st.form_submit_button("确认标记异常")
                    if marked:
                        try:
                            ro.mark_exception(order_id, reason, category)
                            st.session_state["flash"] = f"{order['order_no']} 已标记异常"
                            st.rerun()
                        except (sm.StateError, ro.ValidationError) as exc:
                            st.error(str(exc))

            if sm.can_cancel(order["status"]):
                with st.expander("取消订单"):
                    with st.form(f"cancel_form_{order_id}"):
                        cancel_reason = st.text_input("取消原因（可留空）")
                        cancelled = st.form_submit_button("确认取消")
                    if cancelled:
                        try:
                            ro.cancel_order(order_id, cancel_reason)
                            st.session_state["flash"] = f"{order['order_no']} 已取消"
                            st.rerun()
                        except (sm.StateError, ro.ValidationError) as exc:
                            st.error(str(exc))

            # ---- 对账状态 ----
            st.subheader("对账状态")
            recon = ro.get_reconciliation(order_id)
            if recon:
                r1, r2, r3 = st.columns(3)
                r1.metric("货到", "✅" if recon["goods_received"] else "⬜", recon["goods_received_date"] or "无日期")
                r2.metric("票到", "✅" if recon["invoice_received"] else "⬜", recon["invoice_received_date"] or "无日期")
                r3.metric("款付", "✅" if recon["payment_done"] else "⬜", recon["payment_date"] or "无日期")
                if recon["invoice_no"]:
                    st.caption(f"发票号：{recon['invoice_no']}")
            else:
                st.caption("还没有对账记录。第 3 轮接入对账页后可在此录入货 / 票 / 款。")

            # ---- 异常历史 ----
            history = ro.list_exceptions(order_id)
            if history:
                st.subheader("异常历史")
                st.dataframe(
                    [
                        {
                            "标记时间": h["created_at"],
                            "标记前状态": h["from_status"],
                            "类别": h["category"] or "—",
                            "原因": h["reason"],
                            "恢复时间": h["resolved_at"] or "未恢复",
                        }
                        for h in history
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

            # ---- 编辑 ----
            with st.expander("编辑订单"):
                with st.form(f"edit_order_{order_id}"):
                    e1, e2, e3 = st.columns(3)
                    qty = e1.number_input("数量 *", min_value=0.0001, value=float(order["quantity"]), step=1.0)
                    price = e2.number_input(
                        "单价", min_value=0.0, value=order["unit_price"], step=0.01, format="%.4f"
                    )
                    total = e3.number_input(
                        "总价", min_value=0.0, value=order["total_amount"], step=1.0, format="%.2f"
                    )
                    e4, e5 = st.columns(2)
                    promised = e4.date_input("承诺交期", value=_as_date(order["promised_delivery_date"]))
                    actual = e5.date_input("实际到货日", value=_as_date(order["actual_delivery_date"]))
                    item_name = st.text_input("品名 *", value=order["item_name_raw"])
                    urgent = st.checkbox("紧急订单", value=bool(order["is_urgent"]))
                    saved = st.form_submit_button("保存修改", type="primary")
                if saved:
                    try:
                        # 改了数量/单价但没填总价，按 数量×单价 重算，避免总价与明细脱节
                        if total is None and price is not None:
                            total = round(qty * price, 4)
                        ro.update_order(
                            order_id,
                            {
                                "quantity": qty,
                                "unit_price": price,
                                "total_amount": total,
                                "promised_delivery_date": promised,
                                "actual_delivery_date": actual,
                                "item_name_raw": item_name,
                                "is_urgent": urgent,
                            },
                        )
                        st.session_state["flash"] = f"{order['order_no']} 已保存"
                        st.rerun()
                    except ro.ValidationError as exc:
                        st.error(str(exc))

# ---------------------------------------------------------------- 新建
with tab_new:
    if not suppliers:
        st.warning("还没有供应商，先去「供应商」页建一个再下单。")
    else:
        st.caption("只有订单号、供应商、品名、数量、下单日期必填，其余可以留空后补。外币按订单日汇率折算。")

        currencies = repo_fx.known_currencies()
        base_cur = repo_fx.base_currency()
        cur_index = currencies.index(base_cur) if base_cur in currencies else 0

        t1, t2 = st.columns(2)
        order_date = t1.date_input("下单日期 *", value=date.today(), key="new_order_date")
        currency = t2.selectbox("币种", currencies, index=cur_index, key="new_order_currency")

        if currency != base_cur:
            rate = repo_fx.get_rate(currency, None, str(order_date))
            if rate is None:
                st.warning(
                    f"缺少 {currency} 在 {order_date} 及之前的汇率，保存会被拦下。"
                    "请先到「设置」页的汇率表补一条。"
                )
            else:
                st.caption(f"汇率 1 {currency} = {rate:.4f} {base_cur}（按下单日取最近生效）")

        with st.form("new_order", clear_on_submit=True):
            n1, n2 = st.columns(2)
            order_no = n1.text_input("订单号 *", placeholder="PO-20261023-001")
            supplier_label = n2.selectbox("供应商 *", list(supplier_options))

            n3, n4 = st.columns(2)
            item_label = n3.selectbox("物料", list(item_options))
            item_name = n4.text_input("品名 *", placeholder="选了物料可留空，自动带出物料名")

            n5, n6, n7 = st.columns(3)
            quantity = n5.number_input("数量 *", min_value=0.0001, value=1.0, step=1.0)
            unit_price = n6.number_input("单价", min_value=0.0, value=None, step=0.01, format="%.4f")
            total_amount = n7.number_input(
                "总价（留空按 数量×单价 自动算）", min_value=0.0, value=None, step=1.0, format="%.2f"
            )

            n8, n9 = st.columns(2)
            promised = n8.date_input("承诺交期", value=None)
            urgent = n9.checkbox("紧急订单")

            created = st.form_submit_button("创建订单", type="primary")

        if created:
            try:
                new_id = ro.create_order(
                    {
                        "order_no": order_no,
                        "supplier_id": supplier_options[supplier_label],
                        "item_id": item_options[item_label],
                        "item_name_raw": item_name,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "total_amount": total_amount,
                        "currency": currency,
                        "order_date": order_date,
                        "promised_delivery_date": promised,
                        "is_urgent": urgent,
                    }
                )
                st.session_state["flash"] = f"已创建订单 #{new_id}：{order_no}"
                st.rerun()
            except ro.ValidationError as exc:
                st.error(str(exc))
