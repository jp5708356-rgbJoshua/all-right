"""物料 / 服务档案。

历史低/高价只读，由比价记录自动重算，不接受手工填写。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db, repo_items as ri

st.set_page_config(page_title="物料 / 服务", page_icon="📦", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _boot() -> None:
    db.init_db()


_boot()

st.title("物料 / 服务")
from core import workspace
workspace.banner(st)

st.caption("比价、订单、对账都以这里的物料为锚点。历史高低价由比价记录自动算出，不能手填。")

flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)


def _fields(prefix: str, detail: dict | None) -> dict:
    detail = detail or {}
    col1, col2 = st.columns([1, 2])
    code = col1.text_input("物料编码", value=detail.get("code") or "", key=f"{prefix}_code")
    name = col2.text_input("物料名称 *", value=detail.get("name") or "", key=f"{prefix}_name")
    col3, col4 = st.columns(2)
    spec = col3.text_input("规格型号", value=detail.get("spec") or "", key=f"{prefix}_spec")
    unit = col4.text_input("单位", value=detail.get("unit") or "", key=f"{prefix}_unit")
    return {"code": code, "name": name, "spec": spec, "unit": unit}


tab_list, tab_new = st.tabs(["列表与详情", "新建物料"])

with tab_list:
    keyword = st.text_input("搜索", placeholder="名称 / 编码 / 规格")
    records = ri.list_items(keyword=keyword)

    if not records:
        st.info("还没有物料。切到「新建物料」建第一条。")
    else:
        st.dataframe(
            [
                {
                    "编号": r["id"],
                    "编码": r["code"] or "—",
                    "名称": r["name"],
                    "规格": r["spec"] or "—",
                    "单位": r["unit"] or "—",
                    "历史低价": r["historical_low_price"] if r["historical_low_price"] is not None else "—",
                    "历史高价": r["historical_high_price"] if r["historical_high_price"] is not None else "—",
                    "报价条数": r["quote_count"],
                }
                for r in records
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"共 {len(records)} 条。历史高低价来自比价记录，第 3 轮接入比价后自动填充。")

        st.divider()
        options = {f"#{r['id']}  {r['name']}": r["id"] for r in records}
        picked = st.selectbox("选择物料查看 / 编辑", list(options))
        item_id = options[picked]
        detail = ri.get_item(item_id)

        if detail:
            with st.form(f"edit_item_{item_id}"):
                payload = _fields(f"edit_{item_id}", detail)
                saved = st.form_submit_button("保存修改", type="primary")
            if saved:
                try:
                    ri.update_item(item_id, payload)
                    st.session_state["flash"] = f"已保存：{payload['name']}"
                    st.rerun()
                except ri.ValidationError as exc:
                    st.error(str(exc))

with tab_new:
    st.caption("只有名称必填。编码留空也可以，之后随时补。")
    with st.form("new_item", clear_on_submit=True):
        payload = _fields("new", None)
        created = st.form_submit_button("创建物料", type="primary")

    if created:
        if not payload["name"].strip():
            st.error("物料名称必填")
        else:
            try:
                new_id = ri.create_item(payload)
                st.session_state["flash"] = f"已创建物料 #{new_id}：{payload['name']}"
                st.rerun()
            except ri.ValidationError as exc:
                st.error(str(exc))
