"""仅本机运行：参见 README 的启动参数。"""
from datetime import date
import sqlite3
import yaml
import streamlit as st
from db import (init_db, load_config, list_records, get_record, count_name, save_record, delete_record,
                list_orders, get_order, save_order, delete_order, perform_order_action,
                dashboard_counts, OrderError, quote_settings, quote_comparison, get_quote, save_quote,
                delete_quote, quotes_for_order, RECONCILIATION_FILTERS,
                list_reconciliation, get_reconciliation, save_reconciliation)
from state_machine import allowed_transitions, get_available_actions


def profile_page(page):
    table = "suppliers" if page == "供应商档案" else "items"
    label = "供应商" if table == "suppliers" else "物料"
    st.header(page)
    if message := st.session_state.pop("notice", None):
        st.success(message)

    records = list_records(table)
    headings = {"id": "编号", "name": "名称", "risk_level": "风险等级", "status": "状态",
                "qualification_expiry": "资质到期日", "code": "编码", "spec": "规格",
                "unit": "单位", "historical_low_price": "历史最低价", "historical_high_price": "历史最高价"}
    st.dataframe([{headings[key]: value for key, value in row.items()} for row in records],
                 hide_index=True, width="stretch")
    if not records:
        st.info(f"暂无{label}，可在下方新增。")
    record_id = st.selectbox("新增或打开详情", [None] + [row["id"] for row in records],
                             format_func=lambda value: "新增记录" if value is None else
                             f"记录 #{value}",
                             key=f"select_{table}")
    record = get_record(table, record_id) if record_id is not None else {}
    if record_id is not None:
        st.subheader(f"详情 · #{record_id}")
        detail_labels = {**headings, "credit_code": "统一社会信用代码", "category": "分类",
                         "contact_name": "联系人", "contact_phone": "电话", "bank_account": "付款账号",
                         "risk_notes": "风险备注"}
        # 不使用 dataframe，避免详情区提供表格下载。
        for key, value in record.items():
            st.text(f"{detail_labels[key]}：{value if value is not None else '—'}")
    st.subheader("新增" if record_id is None else "编辑")
    prefix = f"{table}_{record_id}"
    values = {}
    # 使用普通组件以便名称变动后、点击保存前即可显示重复警告。
    st.session_state.setdefault(f"{prefix}_name", record.get("name") or "")
    values["name"] = st.text_input("名称（必填）", key=f"{prefix}_name").strip()
    name_changed = record_id is None or values["name"] != record["name"]
    if values["name"] and name_changed and count_name(table, values["name"]) > 0:
        st.warning("已有同名记录，仍可保存；保存后会保留独立编号。")
    fields = ({"credit_code": "统一社会信用代码", "category": "分类", "contact_name": "联系人",
               "contact_phone": "电话", "bank_account": "付款账号", "risk_notes": "风险备注"}
              if table == "suppliers" else {"code": "编码", "spec": "规格", "unit": "单位"})
    for key, title in fields.items():
        st.session_state.setdefault(f"{prefix}_{key}", record.get(key) or "")
        values[key] = st.text_input(title, key=f"{prefix}_{key}")
    if table == "suppliers":
        raw_date = record.get("qualification_expiry")
        expiry = st.date_input("资质到期日（可空）", value=date.fromisoformat(raw_date) if raw_date else None,
                               key=f"{prefix}_expiry")
        values["qualification_expiry"] = expiry.isoformat() if expiry else None
        for key, title, options, default in [
            ("risk_level", "风险等级", ["低", "中", "高"], "低"),
            ("status", "状态", ["活跃", "暂停", "淘汰"], "活跃")]:
            values[key] = st.selectbox(title, options, index=options.index(record.get(key) or default),
                                      key=f"{prefix}_{key}")
    else:
        st.caption("旧物料价格列保持只读；报价记录请到比价视图查看，本轮不回写旧物料价格列。")
    if st.button("保存", key=f"{prefix}_save"):
        saved_id = save_record(table, values, record_id)
        st.session_state["notice"] = f"已保存 {label} #{saved_id}"
        st.rerun()
    if record_id is not None:
        st.divider()
        st.caption("物理删除仅用于清理测试数据；正式淘汰供应商请将状态改为“淘汰”。")
        confirmed = st.checkbox("确认永久删除这条测试记录", key=f"{prefix}_confirm")
        def delete_selected():
            try:
                delete_record(table, record_id)
            except ValueError as error:
                st.session_state["failure"] = str(error)
                return
            st.session_state[f"select_{table}"] = None
            st.session_state["notice"] = "已删除测试记录"

        st.button("删除测试记录", disabled=not confirmed, key=f"{prefix}_delete",
                  on_click=delete_selected)


def clear_order_date(widget_key):
    st.session_state[widget_key] = None


def reset_order_filters():
    for key in ("order_filter_supplier", "order_filter_status", "order_filter_start",
                "order_filter_end", "order_filter_urgent"):
        st.session_state[key] = None


def orders_page():
    st.header("订单")
    suppliers = list_records("suppliers")
    items = list_records("items")
    supplier_labels = {row["id"]: f"#{row['id']} · {row['name']}" for row in suppliers}
    item_labels = {row["id"]: f"#{row['id']} · {row['name']}" for row in items}
    item_names = {row["id"]: row["name"] for row in items}
    with st.expander("筛选订单"):
        filter_supplier = st.selectbox("筛选供应商", [None, *supplier_labels],
                                        format_func=lambda value: supplier_labels.get(value, "全部"), key="order_filter_supplier")
        st.selectbox("筛选状态", [None, *allowed_transitions],
                     format_func=lambda value: value or "全部", key="order_filter_status")
        date_start = st.date_input("下单日期从", value=None, key="order_filter_start")
        date_end = st.date_input("下单日期至", value=None, key="order_filter_end")
        urgent = st.selectbox("筛选紧急", [None, 1, 0],
                              format_func=lambda value: {None: "全部", 1: "紧急", 0: "非紧急"}[value], key="order_filter_urgent")
        st.button("重置筛选", on_click=reset_order_filters)
    if date_start and date_end and date_start > date_end:
        st.error("起始日期不能晚于结束日期")
    records = list_orders(filter_supplier, st.session_state["order_filter_status"], date_start, date_end, urgent)
    fields = {"id": "编号", "order_no": "订单号", "supplier_name": "供应商", "item_name_raw": "物料快照",
              "quantity": "数量", "unit_price": "单价", "total_amount": "总额", "currency": "币种",
              "order_date": "下单日期", "promised_delivery_date": "承诺交期", "status": "状态",
              "is_urgent": "紧急(1/0)", "is_exception": "异常(1/0)"}
    st.dataframe([{title: row[key] for key, title in fields.items()} for row in records],
                 hide_index=True, width="stretch")
    if not records:
        st.info("当前筛选下暂无订单")
    # 详情选择独立于筛选，保证修改后仍可看到刚保存的记录。
    all_ids = [row["id"] for row in list_orders()]
    if "pending_order_id" in st.session_state:
        st.session_state["select_order"] = st.session_state.pop("pending_order_id")
    order_id = st.selectbox("新增或打开订单", [None, *all_ids],
                            format_func=lambda value: "新增订单" if value is None else f"订单 #{value}",
                            key="select_order")
    record = get_order(order_id) if order_id is not None else {}
    if order_id is not None:
        st.subheader(f"订单详情 · #{order_id}")
        st.text(f"订单号：{record['order_no']}")
        st.text(f"状态：{record['status']}")
        st.caption("对账标记不改变此处状态")
        st.text(f"异常标记（1=是，0=否）：{record['is_exception']}")
        st.text(f"异常原因：{record['exception_reason'] or '—'}")
        st.text(f"异常来源：{record['status_before_exception'] or '—'}")
        st.text(f"异常时间（世界协调时）：{record['exception_marked_at'] or '—'}")
        st.text(f"总额：{record['total_amount'] if record['total_amount'] is not None else '—'} {record['currency']}")
        st.subheader("状态与异常操作")
        for action in get_available_actions(record["status"], record["is_exception"], record["status_before_exception"]):
            reason = None
            if action.needs_reason:
                reason = st.text_input("异常原因（标记时必填）", key=f"reason_{order_id}")
            if st.button(action.label, key=f"action_{order_id}_{action.action}_{action.target_status}"):
                ok, message = perform_order_action(order_id, action.action, action.target_status, reason)
                if ok:
                    st.session_state["notice"] = message
                    st.rerun()
                st.error(message)
        st.caption("按钮由状态机提供；无可用动作时不会显示按钮。日期不会随状态自动补填。")
        st.subheader("关联历史报价（只读）")
        linked = quotes_for_order(order_id)
        if linked is None:
            st.info("本订单未关联物料，无法按物料关联报价")
        else:
            show_quote_table(linked)
    st.subheader("新增订单" if order_id is None else "编辑普通字段")
    prefix = f"order_{order_id}_{st.session_state.get('order_revision', 0)}"
    values, errors = {}, {}

    def input_text(field, label, default=""):
        st.session_state.setdefault(f"{prefix}_{field}", record.get(field) or default)
        value = st.text_input(label, key=f"{prefix}_{field}")
        errors[field] = st.empty()
        return value

    def input_date(field, label):
        st.session_state.setdefault(f"{prefix}_{field}", date.fromisoformat(record[field]) if record.get(field) else None)
        value = st.date_input(label, min_value=date.min, max_value=date.max, key=f"{prefix}_{field}")
        st.button("清空" + label.split("（")[0], key=f"{prefix}_{field}_clear",
                  on_click=clear_order_date, args=(f"{prefix}_{field}",))
        errors[field] = st.empty()
        return value

    values["order_no"] = input_text("order_no", "订单号（必填）")
    st.session_state.setdefault(f"{prefix}_supplier_id", record.get("supplier_id"))
    values["supplier_id"] = st.selectbox("供应商（必选）", [None, *supplier_labels],
                                        format_func=lambda value: supplier_labels.get(value, "请选择"),
                                        key=f"{prefix}_supplier_id")
    errors["supplier_id"] = st.empty()
    st.session_state.setdefault(f"{prefix}_item_id", record.get("item_id"))
    values["item_id"] = st.selectbox("关联物料（可空）", [None, *item_labels],
                                    format_func=lambda value: item_labels.get(value, "不关联，使用手填名称"),
                                    key=f"{prefix}_item_id")
    errors["item_id"] = st.empty()
    values["item_name_raw"] = input_text("item_name_raw", "物料名称快照（未关联物料时必填）")
    snapshot_choice = None
    if (order_id is not None and values["item_id"] != record["item_id"]
            and values["item_id"] is not None and values["item_name_raw"].strip()
            and values["item_name_raw"].strip() != item_names[values["item_id"]]):
        st.warning("已更换关联物料。当前快照名称与新物料名称不同，请选择处理方式。")
        snapshot_choice = st.selectbox("物料快照处理（必须选择）", [None, "sync", "keep"],
                                       format_func=lambda value: {None: "请选择", "sync": "同步为新物料名称",
                                                                  "keep": "保留当前快照名称"}[value],
                                       key=f"{prefix}_snapshot_{values['item_id']}")
    st.caption("关联物料且快照为空时，保存会自动填写物料名称；档案改名不追溯覆盖快照。")
    values["quantity"] = input_text("quantity", "数量（必填，最多六位小数）")
    values["unit_price"] = input_text("unit_price", "单价（可空，最多六位小数）")
    st.caption("总额只读，保存时按数量 × 单价计算；单价为空时总额为空。")
    errors["total_amount"] = st.empty()
    values["currency"] = input_text("currency", "币种（未填默认 CNY）", "CNY")
    values["order_date"] = input_date("order_date", "下单日期（必填）")
    if values["order_date"] and values["order_date"] > date.today():
        st.info("下单日期在未来，仍可保存，请确认符合你的录单意图。")
    values["promised_delivery_date"] = input_date("promised_delivery_date", "承诺交期（可空）")
    values["actual_delivery_date"] = input_date("actual_delivery_date", "实际到货日期（可空）")
    st.session_state.setdefault(f"{prefix}_is_urgent", bool(record.get("is_urgent", False)))
    values["is_urgent"] = st.checkbox("紧急订单", key=f"{prefix}_is_urgent")
    values["exception_note"] = input_text("exception_note", "普通备注（不是异常原因）")
    st.caption("异常及已完成订单可纠正普通字段；金额按当前值重算，本轮不保留修改历史。")
    save_error = st.empty()
    if st.button("保存订单", key=f"{prefix}_save"):
        try:
            saved = save_order(values, order_id, snapshot_choice)
        except OrderError as error:
            save_error.error(f"订单未保存：{error}")
            if error.field in errors:
                errors[error.field].error(str(error))
        else:
            st.session_state["pending_order_id"] = saved
            st.session_state["order_revision"] = st.session_state.get("order_revision", 0) + 1
            st.session_state["notice"] = f"已保存订单 #{saved}"
            st.rerun()
    if order_id is not None:
        st.divider()
        st.caption("物理删除仅用于清理测试数据，删除后订单号可以复用。")
        st.warning("该订单的对账记录将一并删除")
        confirmed = st.checkbox("确认永久删除这条测试订单", key=f"delete_confirm_{order_id}")
        def delete_selected_order():
            try:
                delete_order(order_id)
            except sqlite3.Error:
                st.session_state["failure"] = "订单删除失败，请检查数据库文件"
                return
            st.session_state["select_order"] = None
            st.session_state["notice"] = "已删除测试订单"

        st.button("删除测试订单", disabled=not confirmed, key=f"delete_order_{order_id}",
                  on_click=delete_selected_order)


def dashboard_page():
    st.header("仪表盘")
    numbers = dashboard_counts()
    days = load_config()["reminders"]["delivery_due_days"]
    columns = st.columns(3)
    columns[0].metric("进行中订单数", numbers["active"])
    columns[1].metric(f"{days} 天内到期交期数", numbers["due"])
    columns[2].metric("标记异常订单数", numbers["exceptions"])
    st.caption("按本机本地日期计算，临近交期含今天和窗口最后一天，空交期与逾期不计入。三个数字可重叠。")


def quote_price_text(value):
    return value.rstrip("0").rstrip(".") if "." in value else value


def show_quote_table(comparison):
    """同一只读组件用于比价页和订单详情，不读取订单状态。"""
    import pandas as pd
    records = comparison["quotes"]
    if not records:
        st.info("暂无该物料的报价记录")
        return
    displayed = [{"编号": row["id"], "供应商": f"#{row['supplier_id']} · {row['supplier_name']}",
                  "单价": quote_price_text(row["quoted_price"]), "币种": row["currency"],
                  "报价日期": row["quote_date"], "有效期至": row["valid_until"] or "—",
                  "有效性": "有效" if row["is_valid"] else "已过期",
                  "价格标记": "最低价" if row["is_lowest"] else "—",
                  "交期天数": str(row["lead_time_days"]) if row["lead_time_days"] is not None else "—",
                  "选中": "✓" if row["is_selected"] else "—",
                  "选择理由": row["selection_reason"] or "—", "备注": row["note"] or "—"} for row in records]
    frame = pd.DataFrame(displayed)
    lowest_ids = {row["id"] for row in records if row["is_lowest"]}
    styled = frame.style.apply(lambda row: ["background-color: #e1f3e5" if row["编号"] in lowest_ids else ""] * len(row), axis=1)
    st.dataframe(styled, hide_index=True, width="stretch")


def quotes_page():
    st.header("比价视图")
    try:
        minimum_suppliers, missing = quote_settings()
    except OrderError as error:
        st.error(str(error))
        return
    if missing:
        st.warning("config.yaml 缺少 quotes.min_suppliers，已使用默认值 3")
    items = {row["id"]: row["name"] for row in list_records("items")}
    suppliers = {row["id"]: row["name"] for row in list_records("suppliers")}
    if not items:
        st.info("请先在物料档案新增物料")
        return
    item_id = st.selectbox("选择比价物料", list(items), format_func=lambda value: f"#{value} · {items[value]}", key="quote_item")
    comparison = quote_comparison(item_id)
    if comparison["valid_supplier_count"] < minimum_suppliers:
        st.warning(f"有效报价不足 {minimum_suppliers} 家，请确认是否符合比价要求")
    if comparison["lowest_price"] is None:
        st.info("暂无有效报价")
    else:
        st.success(f"最低价 {quote_price_text(comparison['lowest_price'])}（{comparison['tied_supplier_count']} 家并列）")
    show_quote_table(comparison)
    if not suppliers:
        st.info("请先在供应商档案新增供应商")
        return
    if st.session_state.pop("pending_quote_reset", None) == item_id:
        st.session_state[f"select_quote_{item_id}"] = None
    pending = st.session_state.pop("pending_quote", None)
    if pending is not None:
        st.session_state[f"select_quote_{item_id}"] = pending
    quote_id = st.selectbox("新增或打开报价", [None, *[row["id"] for row in comparison["quotes"]]],
                            format_func=lambda value: "新增报价" if value is None else f"报价 #{value}", key=f"select_quote_{item_id}")
    record = get_quote(quote_id) if quote_id is not None else {}
    st.subheader("新增报价" if quote_id is None else f"报价详情 · #{quote_id}")
    st.caption("历史报价修改会影响历史比价视图；多条报价可以同时标记选中。")
    prefix = f"quote_{item_id}_{quote_id}_{st.session_state.get('quote_revision', 0)}"
    values, errors = {"item_id": item_id, "currency": "CNY"}, {}
    st.text(f"物料：#{item_id} · {items[item_id]}（创建后不可修改）")
    if quote_id is None:
        values["supplier_id"] = st.selectbox("报价供应商（必选）", [None, *suppliers],
                                            format_func=lambda value: "请选择" if value is None else f"#{value} · {suppliers[value]}", key=f"{prefix}_supplier")
    else:
        values["supplier_id"] = record["supplier_id"]
        st.text(f"供应商：#{record['supplier_id']} · {suppliers[record['supplier_id']]}（创建后不可修改）")
    errors["supplier_id"] = st.empty()
    st.text("币种：人民币（CNY）")
    for field, label in (("quoted_price", "报价单价（必填，最多六位小数）"), ("lead_time_days", "报价交期天数（可空）"),
                         ("selection_reason", "选择理由（可空）"), ("note", "报价备注（可空）")):
        raw = record.get(field)
        st.session_state.setdefault(f"{prefix}_{field}", "" if raw is None else str(raw))
        values[field] = st.text_input(label, key=f"{prefix}_{field}")
        errors[field] = st.empty()
    for field, label in (("quote_date", "报价日期（必填）"), ("valid_until", "报价有效期至（可空）")):
        initial = date.fromisoformat(record[field]) if record.get(field) else (date.today() if field == "quote_date" and quote_id is None else None)
        st.session_state.setdefault(f"{prefix}_{field}", initial)
        values[field] = st.date_input(label, min_value=date.min, max_value=date.max, key=f"{prefix}_{field}")
        st.button("清空" + label.split("（")[0], key=f"{prefix}_{field}_clear", on_click=clear_order_date, args=(f"{prefix}_{field}",))
        errors[field] = st.empty()
    st.session_state.setdefault(f"{prefix}_is_selected", bool(record.get("is_selected", 0)))
    values["is_selected"] = st.checkbox("标记为选中报价", key=f"{prefix}_is_selected")
    summary = st.empty()
    if st.button("保存报价", key=f"{prefix}_save"):
        try:
            saved = save_quote(values, quote_id)
        except OrderError as error:
            summary.error(f"报价未保存：{error}")
            if error.field in errors:
                errors[error.field].error(str(error))
        else:
            st.session_state["pending_quote"] = saved
            st.session_state["quote_revision"] = st.session_state.get("quote_revision", 0) + 1
            st.session_state["notice"] = f"已保存报价 #{saved}"
            st.rerun()
    if quote_id is not None:
        st.divider()
        st.caption("物理删除仅用于清理测试数据，删除后无法在本工具中恢复。")
        confirmed = st.checkbox("确认永久删除这条报价", key=f"{prefix}_delete_confirm")
        selected_confirmed = False
        if record["is_selected"]:
            selected_confirmed = st.checkbox("该报价已标记为选中，删除后选择理由将一并丢失，确定删除？", key=f"{prefix}_selected_confirm")
        delete_error = st.empty()
        if st.button("删除报价", disabled=not confirmed or bool(record["is_selected"] and not selected_confirmed), key=f"{prefix}_delete"):
            try:
                delete_quote(quote_id, confirmed, selected_confirmed)
            except OrderError as error:
                delete_error.error(str(error))
            else:
                st.session_state["pending_quote_reset"] = item_id
                st.session_state["notice"] = "已删除报价"
                st.rerun()


def reconciliation_page():
    st.header("对账视图")
    st.info("对账标记不改变订单状态")
    filter_name = st.selectbox("对账筛选", list(RECONCILIATION_FILTERS), key="reconciliation_filter")
    records = list_reconciliation(filter_name)
    if records:
        import pandas as pd
        shown = []
        for row in records:
            displayed = {"订单号": row["order_no"], "供应商": row["supplier_name"],
                         "物料": row["item_name_raw"] or "—", "下单日期": row["order_date"]}
            for field, label in (("goods_received", "货"), ("invoice_received", "票"), ("payment_done", "款")):
                displayed[label] = "未标记" if row["reconciliation_id"] is None else ("✓" if row[field] else "未完成")
            shown.append(displayed)
        frame = pd.DataFrame(shown)
        styled = frame.style.apply(lambda column: ["color: #16803d" if value == "✓" else "color: #6b7280" for value in column], subset=["货", "票", "款"])
        st.dataframe(styled, hide_index=True, width="stretch")
    else:
        st.info("当前筛选下暂无订单")
    # 详情独立于筛选；保存后即使不再符合筛选，仍能看到刚保存的标记。
    all_orders = list_reconciliation()
    if not all_orders:
        st.info("暂无订单，请先在订单页创建订单")
        return
    choices = {row["order_id"]: row for row in all_orders}
    order_id = st.selectbox("打开对账订单", list(choices),
                            format_func=lambda value: f"订单 #{value} · {choices[value]['order_no']}", key="reconciliation_order")
    row = choices[order_id]
    st.subheader(f"对账详情 · {row['order_no']}")
    st.caption("首次保存才建立对账记录；全0也会保存。各标记可独立修改，发票号、付款日期可空。")
    record = get_reconciliation(order_id) or {}
    prefix = f"reconciliation_{order_id}_{st.session_state.get('reconciliation_revision', 0)}"
    values, errors = {}, {}
    for field, label in (("goods_received", "货已收到"), ("invoice_received", "票已收到"), ("payment_done", "款已付清")):
        st.session_state.setdefault(f"{prefix}_{field}", bool(record.get(field, 0)))
        values[field] = st.checkbox(label, key=f"{prefix}_{field}")
        errors[field] = st.empty()
    st.session_state.setdefault(f"{prefix}_invoice_no", record.get("invoice_no") or "")
    values["invoice_no"] = st.text_input("发票号（可空）", key=f"{prefix}_invoice_no")
    errors["invoice_no"] = st.empty()
    st.session_state.setdefault(f"{prefix}_payment_date", date.fromisoformat(record["payment_date"]) if record.get("payment_date") else None)
    values["payment_date"] = st.date_input("付款日期（可空）", min_value=date.min, max_value=date.max, key=f"{prefix}_payment_date")
    st.button("清空付款日期", key=f"{prefix}_payment_date_clear", on_click=clear_order_date, args=(f"{prefix}_payment_date",))
    errors["payment_date"] = st.empty()
    summary = st.empty()
    if st.button("保存对账标记", key=f"{prefix}_save"):
        try:
            save_reconciliation(order_id, values)
        except OrderError as error:
            summary.error(f"对账未保存：{error}")
            if error.field in errors:
                errors[error.field].error(str(error))
        else:
            st.session_state["reconciliation_revision"] = st.session_state.get("reconciliation_revision", 0) + 1
            st.session_state["notice"] = "已保存对账标记"
            st.rerun()


st.set_page_config(page_title="个人采购工作台", layout="wide")
st.title("个人采购工作台")
try:
    init_db()
    if message := st.session_state.pop("notice", None):
        st.success(message)
    if message := st.session_state.pop("failure", None):
        st.error(message)
    page = st.sidebar.radio("页面", ["仪表盘", "订单", "供应商档案", "物料档案", "比价视图", "对账视图"])
    pages = {"仪表盘": dashboard_page, "订单": orders_page, "比价视图": quotes_page, "对账视图": reconciliation_page,
             "供应商档案": lambda: profile_page("供应商档案"), "物料档案": lambda: profile_page("物料档案")}
    pages[page]()
except ValueError as error:
    st.error(str(error))
except (OSError, sqlite3.Error, yaml.YAMLError):
    st.error("操作失败，请检查本地配置、数据库路径和文件权限。")
