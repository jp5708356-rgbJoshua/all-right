"""比价视图：历史报价、当前有效最低价、有效报价家数提示、手动录入。"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import ai_import, db, repo_fx, repo_items, repo_quotes as rq, repo_suppliers

st.set_page_config(page_title="比价", page_icon="⚖️", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _boot() -> None:
    db.init_db()


_boot()

st.title("比价")
from core import workspace
workspace.banner(st)


flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)

items = repo_items.list_items()
suppliers = repo_suppliers.list_suppliers()

if not items:
    st.warning("还没有物料。先去「物料」页建一个，比价是围绕物料展开的。")
    st.stop()
if not suppliers:
    st.warning("还没有供应商。先去「供应商」页建一个。")
    st.stop()

item_options = {
    f"{i['name']}{' · ' + i['spec'] if i['spec'] else ''}"
    f"{'（' + i['code'] + '）' if i['code'] else ''}": i["id"]
    for i in items
}
supplier_options = {f"{s['name']}（{s['risk_level']}风险）": s["id"] for s in suppliers}


def _money(value) -> str:
    return "—" if value is None else f"¥{value:,.4f}".rstrip("0").rstrip(".")


def _amount(value, currency: str = "CNY") -> str:
    if value is None:
        return "—"
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"¥{text}" if currency == "CNY" else f"{currency} {text}"


def _as_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


tab_view, tab_new = st.tabs(["比价视图", "新增报价"])
tab_ai = None
if ai_import.is_enabled():
    tab_view, tab_new, tab_ai = st.tabs(["比价视图", "新增报价", "智能导入"])

# ---------------------------------------------------------------- 比价视图
with tab_view:
    picked_item = st.selectbox("选择物料", list(item_options))
    item_id = item_options[picked_item]
    item = repo_items.get_item(item_id)
    data = rq.effective_quotes(item_id)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("历史最低价", _money(item["historical_low_price"]))
    m2.metric("历史最高价", _money(item["historical_high_price"]))
    m3.metric("有效报价家数", data["supplier_count"])
    m4.metric("报价总条数", len(data["all"]))

    if data["lowest"]:
        low = data["lowest"]
        st.success(
            f"当前有效最低价：**{_money(low['compare_price'])}** ／ {low['supplier_name']}"
            f"（报价日 {low['quote_date']}"
            + (f"，交期 {low['lead_time_days']} 天" if low["lead_time_days"] else "")
            + "）"
        )
    elif data["all"]:
        st.warning("当前没有有效报价（全部已过期）。")
    else:
        st.info("这个物料还没有任何报价记录。")

    if data["need_more"] and data["all"]:
        st.warning(
            f"有效报价只有 {data['supplier_count']} 家，不足 3 家，最低价参考价值有限。"
        )
    if data["missing_fx"]:
        st.warning(
            f"有 {len(data['missing_fx'])} 条外币报价缺少汇率，未参与最低价比较。"
            "多币种在第 8 轮接入。"
        )

    if data["all"]:
        lowest_id = data["lowest"]["id"] if data["lowest"] else None
        st.dataframe(
            [
                {
                    "标记": "★ 最低" if row["id"] == lowest_id else ("已过期" if row["state"] == "已过期" else ""),
                    "报价日期": row["quote_date"],
                    "供应商": row["supplier_name"],
                    "单价": _amount(row["quoted_price"], row["currency"]),
                    "折算价": _amount(row["base_amount"]),
                    "有效期至": row["valid_until"] or "长期",
                    "状态": row["state"],
                    "交期(天)": row["lead_time_days"] if row["lead_time_days"] is not None else "—",
                    "中选": "是" if row["is_selected"] else "",
                    "备注": row["note"] or "",
                }
                for row in data["all"]
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "同一供应商有多条有效报价时，只有最新一条参与最低价比较；"
            "有效报价家数按供应商去重，不按条数。"
        )

        st.divider()
        st.subheader("标记中选报价")
        quote_options = {
            f"#{q['id']} {q['supplier_name']} · {_money(q['quoted_price'])} · {q['quote_date']}": q["id"]
            for q in data["all"]
        }
        c1, c2 = st.columns([2, 2])
        picked_quote = c1.selectbox("选择一条报价", list(quote_options))
        reason = c2.text_input("中选理由（可留空）", placeholder="如：价格最低且交期可接受")
        if st.button("标记为中选", type="primary"):
            rq.select_quote(quote_options[picked_quote], reason)
            st.session_state["flash"] = "已标记中选报价（同物料其他报价自动取消中选）"
            st.rerun()

    with st.expander("重算历史高低价"):
        st.caption("历史高低价由全部报价（含已过期）自动计算，不手工填。数据对不上时可以手动触发一次。")
        if st.button("立即重算", key="recalc_prices"):
            result = repo_items.recalc_historical_prices(item_id)
            st.session_state["flash"] = (
                f"已重算：低 {_money(result['historical_low_price'])} / "
                f"高 {_money(result['historical_high_price'])}"
            )
            st.rerun()

# ---------------------------------------------------------------- 新增报价
with tab_new:
    st.caption("只有物料、供应商、单价、报价日期必填。外币按报价日最近生效的汇率折算成基准币种。")

    currencies = repo_fx.known_currencies()
    base_cur = repo_fx.base_currency()
    cur_index = currencies.index(base_cur) if base_cur in currencies else 0

    h1, h2 = st.columns(2)
    currency = h1.selectbox("币种", currencies, index=cur_index, key="new_quote_currency")
    quote_date = h2.date_input("报价日期 *", value=date.today(), key="new_quote_date")

    rate_preview = None
    if currency != base_cur:
        rate_preview = repo_fx.get_rate(currency, None, str(quote_date))
        if rate_preview is None:
            st.warning(
                f"缺少 {currency} 在 {quote_date} 及之前的汇率，保存会被拦下。"
                "请先到「设置」页的汇率表补一条。"
            )
        else:
            st.caption(f"汇率 1 {currency} = {rate_preview:.4f} {base_cur}（按报价日取最近生效）")

    with st.form("new_quote", clear_on_submit=True):
        q1, q2 = st.columns(2)
        item_label = q1.selectbox("物料 *", list(item_options), key="new_quote_item")
        supplier_label = q2.selectbox("供应商 *", list(supplier_options), key="new_quote_supplier")

        q3, q4 = st.columns(2)
        price = q3.number_input("单价 *", min_value=0.0001, value=1.0, step=0.01, format="%.4f")
        valid_until = q4.date_input("有效期至（可空）", value=None)

        q5, q6 = st.columns(2)
        lead_time = q5.number_input("交期（天，可空）", min_value=0, value=None, step=1)
        is_selected = q6.checkbox("直接标为中选")

        selection_reason = st.text_input("中选理由（可留空）")
        note = st.text_area("备注（可留空）", height=70)

        created = st.form_submit_button("保存报价", type="primary")

    if created:
        try:
            new_id = rq.create_quote(
                {
                    "item_id": item_options[item_label],
                    "supplier_id": supplier_options[supplier_label],
                    "quoted_price": price,
                    "currency": currency,
                    "quote_date": quote_date,
                    "valid_until": valid_until,
                    "lead_time_days": lead_time,
                    "is_selected": is_selected,
                    "selection_reason": selection_reason,
                    "note": note,
                }
            )
            st.session_state["flash"] = f"已保存报价 #{new_id}，历史高低价已自动重算"
            st.rerun()
        except rq.ValidationError as exc:
            st.error(str(exc))

# ---------------------------------------------------------------- AI 导入
if tab_ai is not None:
    with tab_ai:
        st.caption(
            "上传报价单（Excel / PDF / Word / 文本）。模型只抽字段，不折算、不算总价、不判断最低价，"
            "抽出来的结果必须你确认才入库。"
        )

        a1, a2 = st.columns(2)
        ai_item_label = a1.selectbox("报价归属物料 *", list(item_options), key="ai_item")
        ai_quote_date = a2.date_input("报价日期 *", value=date.today(), key="ai_quote_date")

        uploaded = st.file_uploader(
            "选择报价文件",
            type=["xlsx", "xlsm", "pdf", "docx", "txt", "md"],
            key="ai_upload",
        )

        if uploaded is not None and st.button("解析文件", type="primary"):
            try:
                text = ai_import.read_source(uploaded, uploaded.name)
                with st.spinner("正在解析，稍等…"):
                    result = ai_import.extract_quotes(text, uploaded.name)
                st.session_state["ai_result"] = result
                st.session_state["ai_filename"] = uploaded.name
            except (ai_import.ImportError_, ValueError) as exc:
                st.error(str(exc))

        result = st.session_state.get("ai_result")
        if result:
            origin = result["rows"]
            st.success(
                f"从 {st.session_state.get('ai_filename') or '文件'} 抽出 {len(origin)} 行"
                f"（模型 {result.get('model')}）。下表可以直接改。"
            )

            preview = pd.DataFrame(
                [
                    {
                        "供应商": r["supplier_name"] or "",
                        "品名": r["item_name"] or "",
                        "数量": r["quantity"],
                        "单位": r["unit"] or "",
                        "单价": r["unit_price"],
                        "币种": r["currency"] or "CNY",
                        "交期天数": r["lead_time_days"],
                        "有效期至": r["valid_until"] or "",
                        "备注": r["note"] or "",
                    }
                    for r in origin
                ]
            )

            edited = st.data_editor(
                preview, num_rows="dynamic", use_container_width=True, key="ai_editor"
            )

            st.caption("确认后按「供应商」列匹配档案。匹配不到、或没有单价的行会被跳过并列出来。")

            if st.button("确认写入报价", type="primary"):

                def _text(value) -> str:
                    if value is None:
                        return ""
                    try:
                        if pd.isna(value):
                            return ""
                    except (TypeError, ValueError):
                        pass
                    return str(value).strip()

                def _num(value):
                    if value is None:
                        return None
                    try:
                        if pd.isna(value):
                            return None
                    except (TypeError, ValueError):
                        pass
                    try:
                        return float(str(value).replace(",", "").strip())
                    except ValueError:
                        return None

                created, skipped, failed = 0, [], []

                for idx, row in edited.iterrows():
                    supplier_name = _text(row.get("供应商"))
                    supplier_id = ai_import.match_supplier(supplier_name, suppliers)
                    price = _num(row.get("单价"))

                    if not supplier_id:
                        skipped.append(f"{supplier_name or '（空供应商）'}：档案里没有这家")
                        continue
                    if price is None:
                        skipped.append(f"{supplier_name}：没有可用单价")
                        continue

                    note_parts = []
                    if _text(row.get("备注")):
                        note_parts.append(_text(row.get("备注")))
                    qty = _num(row.get("数量"))
                    if qty is not None:
                        note_parts.append(f"原单数量 {qty:g}{_text(row.get('单位'))}")

                    original = origin[idx] if idx < len(origin) else {}
                    changed = (
                        _text(original.get("supplier_name")) != supplier_name
                        or _num(original.get("unit_price")) != price
                    )

                    try:
                        rq.create_quote(
                            {
                                "item_id": item_options[ai_item_label],
                                "supplier_id": supplier_id,
                                "quoted_price": price,
                                "currency": (_text(row.get("币种")) or "CNY").upper(),
                                "quote_date": ai_quote_date,
                                "valid_until": _text(row.get("有效期至")) or None,
                                "lead_time_days": _num(row.get("交期天数")),
                                "note": "；".join(note_parts) or None,
                                "import_source": "ai",
                                "raw_payload": json.dumps(original, ensure_ascii=False),
                                "edited_by_user": 1 if changed else 0,
                            }
                        )
                        created += 1
                    except (rq.ValidationError, ValueError) as exc:
                        failed.append(f"第 {idx + 1} 行：{exc}")

                if created:
                    st.session_state["flash"] = f"智能导入完成：写入 {created} 条报价"
                    st.session_state.pop("ai_result", None)
                    st.rerun()
                if skipped:
                    st.warning(
                        f"跳过 {len(skipped)} 行：\n\n" + "\n".join(f"- {item}" for item in skipped)
                    )
                if failed:
                    st.error(
                        f"失败 {len(failed)} 行：\n\n" + "\n".join(f"- {item}" for item in failed)
                    )

            with st.expander("模型原始返回"):
                st.code(result.get("raw") or "", language="json")
                st.caption("系统会保存每条报价的原始识别结果，方便日后核对。")
