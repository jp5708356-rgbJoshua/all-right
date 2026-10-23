"""供应商档案：列表、详情、编辑、新建。

列表页不展示联系人姓名 / 电话 / 银行账号，只在详情里显示。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config, db, repo_contracts, repo_suppliers as rs, scorecard

st.set_page_config(page_title="供应商档案", page_icon="🏭", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _boot() -> None:
    db.init_db()


_boot()

st.title("供应商档案")
from core import workspace
workspace.banner(st)


flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _form_payload(prefix: str, detail: dict | None) -> dict:
    """新建与编辑共用的字段渲染，返回待写入的数据。"""
    detail = detail or {}
    name = st.text_input("供应商名称 *", value=detail.get("name") or "", key=f"{prefix}_name")
    col1, col2 = st.columns(2)
    category = col1.text_input("类别", value=detail.get("category") or "", key=f"{prefix}_cat")
    credit_code = col2.text_input(
        "统一社会信用代码", value=detail.get("credit_code") or "", key=f"{prefix}_credit"
    )

    st.markdown("**联系方式**（仅本页详情可见，列表与导出都不带）")
    col3, col4 = st.columns(2)
    contact_name = col3.text_input("联系人", value=detail.get("contact_name") or "", key=f"{prefix}_cname")
    contact_phone = col4.text_input("联系电话", value=detail.get("contact_phone") or "", key=f"{prefix}_cphone")
    bank_account = st.text_input(
        "付款账号", value=detail.get("bank_account") or "", key=f"{prefix}_bank"
    )

    col5, col6, col7 = st.columns(3)
    expiry = col5.date_input(
        "资质到期日", value=_as_date(detail.get("qualification_expiry")), key=f"{prefix}_exp"
    )
    risk_level = col6.selectbox(
        "风险等级",
        rs.RISK_LEVELS,
        index=rs.RISK_LEVELS.index(detail.get("risk_level")) if detail.get("risk_level") in rs.RISK_LEVELS else 0,
        key=f"{prefix}_risk",
    )
    status = col7.selectbox(
        "状态",
        rs.STATUSES,
        index=rs.STATUSES.index(detail.get("status")) if detail.get("status") in rs.STATUSES else 0,
        key=f"{prefix}_status",
    )

    risk_notes = st.text_area("风险备注", value=detail.get("risk_notes") or "", height=80, key=f"{prefix}_notes")

    return {
        "name": name,
        "category": category,
        "credit_code": credit_code,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
        "bank_account": bank_account,
        "qualification_expiry": expiry,
        "risk_level": risk_level,
        "status": status,
        "risk_notes": risk_notes,
    }


tab_list, tab_new = st.tabs(["列表与详情", "新建供应商"])

# ---------------- 列表与详情 ----------------
with tab_list:
    f1, f2, f3 = st.columns([2, 1, 1])
    keyword = f1.text_input("搜索", placeholder="名称 / 类别 / 统一社会信用代码")
    risk_filter = f2.selectbox("风险等级", ["全部", *rs.RISK_LEVELS])
    status_filter = f3.selectbox("状态", ["全部", *rs.STATUSES])

    records = rs.list_suppliers(
        keyword=keyword,
        risk_level=None if risk_filter == "全部" else risk_filter,
        status=None if status_filter == "全部" else status_filter,
    )

    if not records:
        st.info("没有匹配的供应商。切到「新建供应商」建第一条。")
    else:
        st.dataframe(
            [
                {
                    "编号": r["id"],
                    "名称": r["name"],
                    "类别": r["category"] or "—",
                    "风险等级": r["risk_level"],
                    "状态": r["status"],
                    "资质到期日": r["qualification_expiry"] or "—",
                    "最近合作": r["last_order_date"] or "—",
                    "订单数": r["order_count"],
                    "进行中": r["active_order_count"],
                }
                for r in records
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"共 {len(records)} 条。联系方式与付款账号只在下方详情中显示。")

        st.divider()
        options = {f"#{r['id']}  {r['name']}": r["id"] for r in records}
        picked = st.selectbox("选择供应商查看详情", list(options))
        supplier_id = options[picked]
        detail = rs.get_supplier(supplier_id)

        if detail:
            top1, top2, top3 = st.columns(3)
            top1.metric("风险等级", detail["risk_level"])
            top2.metric("状态", detail["status"])
            top3.metric(
                "资质剩余天数",
                "未填"
                if not detail["qualification_expiry"]
                else (date.fromisoformat(detail["qualification_expiry"]) - date.today()).days,
            )

            with st.expander("基本信息", expanded=True):
                st.markdown(
                    f"- 统一社会信用代码：{detail['credit_code'] or '未填'}\n"
                    f"- 类别：{detail['category'] or '未填'}\n"
                    f"- 联系人：{detail['contact_name'] or '未填'}\n"
                    f"- 联系电话：{detail['contact_phone'] or '未填'}\n"
                    f"- 付款账号：{detail['bank_account'] or '未填'}\n"
                    f"- 风险备注：{detail['risk_notes'] or '无'}"
                )

            with st.expander("编辑档案"):
                with st.form(f"edit_supplier_{supplier_id}"):
                    payload = _form_payload(f"edit_{supplier_id}", detail)
                    saved = st.form_submit_button("保存修改", type="primary")
                if saved:
                    try:
                        rs.update_supplier(supplier_id, payload)
                        st.session_state["flash"] = f"已保存：{payload['name']}"
                        st.rerun()
                    except rs.ValidationError as exc:
                        st.error(str(exc))

            with st.expander("关联合同"):
                contracts = repo_contracts.list_contracts(supplier_id=supplier_id)
                if contracts:
                    st.dataframe(
                        [
                            {
                                "合同编号": c["contract_no"] or "—",
                                "标题": c["title"],
                                "开始": c["start_date"] or "—",
                                "结束": c["end_date"] or "—",
                                "价格有效期至": c["price_valid_until"] or "—",
                            }
                            for c in contracts
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )
                else:
                    st.caption("这个供应商还没有合同记录。")

                st.markdown("**新增合同**")
                with st.form(f"new_contract_{supplier_id}"):
                    c_title = st.text_input("合同标题 *", key=f"ct_title_{supplier_id}")
                    c_no = st.text_input("合同编号", key=f"ct_no_{supplier_id}")
                    cc1, cc2 = st.columns(2)
                    c_start = cc1.date_input("开始日期", value=None, key=f"ct_start_{supplier_id}")
                    c_end = cc2.date_input("结束日期", value=None, key=f"ct_end_{supplier_id}")
                    c_price = st.date_input("价格有效期至", value=None, key=f"ct_price_{supplier_id}")
                    c_saved = st.form_submit_button("添加合同")
                if c_saved:
                    try:
                        repo_contracts.create_contract(
                            {
                                "contract_no": c_no,
                                "supplier_id": supplier_id,
                                "title": c_title,
                                "start_date": c_start,
                                "end_date": c_end,
                                "price_valid_until": c_price,
                            }
                        )
                        st.session_state["flash"] = f"已为该供应商添加合同：{c_title}"
                        st.rerun()
                    except repo_contracts.ValidationError as exc:
                        st.error(str(exc))

            st.divider()
            st.subheader("绩效评分卡（只读）")

            def _recent_months(count: int = 6) -> list[str]:
                out = []
                year, month = date.today().year, date.today().month
                for _ in range(count):
                    out.append(f"{year:04d}-{month:02d}")
                    month -= 1
                    if month == 0:
                        month = 12
                        year -= 1
                return out

            period_options = {"全部": None, **{m: m for m in _recent_months()}}
            picked_period = st.selectbox("统计期间", list(period_options), key=f"period_{supplier_id}")
            card = scorecard.compute(supplier_id, period_options[picked_period])

            dcols = st.columns(4)
            for idx, key in enumerate(scorecard.DIMENSIONS):
                dim = card["dimensions"][key]
                dcols[idx].metric(
                    dim["label"],
                    "—" if dim["score"] is None else f"{dim['score']:.0f}",
                    help=f"{dim['detail']}｜权重 {dim['weight']:.2f}",
                )

            total = card["weighted_total"]
            grade = card["grade"]
            st.metric(
                "加权总分 / 等级",
                f"{'—' if total is None else f'{total:.1f}'}　{grade or ''}".strip(),
                help=f"参与计算的权重合计 {card['counted_weight']:.2f}",
            )

            st.dataframe(
                [
                    {
                        "维度": card["dimensions"][k]["label"],
                        "得分": "—" if card["dimensions"][k]["score"] is None
                        else round(card["dimensions"][k]["score"], 1),
                        "权重": card["dimensions"][k]["weight"],
                        "计算依据": card["dimensions"][k]["detail"],
                    }
                    for k in scorecard.DIMENSIONS
                ],
                use_container_width=True,
                hide_index=True,
            )

            if card["empty_dimensions"]:
                names = "、".join(card["dimensions"][k]["label"] for k in card["empty_dimensions"])
                st.caption(
                    f"{names} 没有可用数据，已从加权分母中剔除（空缺维度不扣分）。"
                    "服务分在 config.yaml 的 scorecard.service_manual 里配置，本页不提供编辑入口。"
                )
            else:
                st.caption("四个维度都有数据，按 config.yaml 的权重加权。")

            st.caption(
                f"权重与评级阈值来自 config.yaml；口径：交付=准时到货率，质量=1-质量异常占比，"
                f"成本=100-平均溢价%，服务=人工分。评级 S≥{config.grade_thresholds().get('S')}、"
                f"A≥{config.grade_thresholds().get('A')}、B≥{config.grade_thresholds().get('B')}、其余 C。"
            )

# ---------------- 新建 ----------------
with tab_new:
    st.caption("只有名称必填，其余可以留空后补。")
    with st.form("new_supplier", clear_on_submit=True):
        payload = _form_payload("new", None)
        created = st.form_submit_button("创建供应商", type="primary")

    if created:
        if not payload["name"].strip():
            st.error("供应商名称必填")
        else:
            dups = rs.duplicate_names(payload["name"])
            if dups:
                st.session_state["pending_supplier"] = payload
                st.session_state["pending_dups"] = dups
            else:
                try:
                    new_id = rs.create_supplier(payload)
                    st.session_state["flash"] = f"已创建供应商 #{new_id}：{payload['name']}"
                    st.rerun()
                except rs.ValidationError as exc:
                    st.error(str(exc))

    pending = st.session_state.get("pending_supplier")
    if pending:
        dups = st.session_state.get("pending_dups") or []
        st.warning(
            "已存在同名供应商："
            + "、".join(f"#{d['id']}（{d['status']}）" for d in dups)
            + "。允许重名，但请确认不是重复录入。"
        )
        col_a, col_b = st.columns(2)
        if col_a.button("确认仍然创建", type="primary"):
            try:
                new_id = rs.create_supplier(pending)
                st.session_state.pop("pending_supplier", None)
                st.session_state.pop("pending_dups", None)
                st.session_state["flash"] = f"已创建供应商 #{new_id}：{pending['name']}"
                st.rerun()
            except rs.ValidationError as exc:
                st.error(str(exc))
        if col_b.button("取消"):
            st.session_state.pop("pending_supplier", None)
            st.session_state.pop("pending_dups", None)
            st.rerun()
