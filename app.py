"""个人采购工作台 · 仪表盘。

四张数字卡片 + 「需要你动手的订单」清单。
第 2 轮接入订单与对账前四类待办；第 4 轮补资质与合同两类。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config, db, repo_items, repo_orders, repo_suppliers, todos, workspace

st.set_page_config(page_title="个人采购工作台", page_icon="📋", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _bootstrap() -> str:
    db.init_db()
    return str(config.db_path())


DB_FILE = _bootstrap()

st.title("今日工作")
from core import followups
db.init_db()
followup_due = followups.list_rows(due_only=True)
if followup_due:
    st.warning(f"今天有 {len(followup_due)} 项沟通需要跟进，请到左侧「催交与跟进」处理。")
workspace.banner(st)
st.caption("先处理逾期和高优先级事项，再跟进临近交期、发票与付款。")

flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)

# ---------------- 四张数字卡片 ----------------
due_days = config.reminder_days("delivery_due_days")

c1, c2, c3, c4 = st.columns(4)
c1.metric("进行中订单", repo_orders.count_active())
c2.metric(f"{due_days} 天内到期交期", repo_orders.count_due_soon(due_days))
c3.metric("待对账订单", repo_orders.count_pending_recon())
c4.metric("高风险供应商进行中", repo_orders.count_high_risk_active())

st.divider()

# ---------------- 待办清单 ----------------
pending = todos.build_todos()


def _days_text(days) -> str:
    if days is None:
        return "—"
    if days > 0:
        return f"超 {days} 天"
    if days == 0:
        return "今天到期"
    return f"剩 {-days} 天"


st.subheader("需要你动手的订单")
if pending:
    # 待办按优先级排好序，只渲染前 100 条——一次性画一万多行表格会明显卡顿
    display_limit = 100
    shown = pending[:display_limit]

    st.dataframe(
        [
            {
                "优先级": t["priority"],
                "类型": t["kind"],
                "单号 / 对象": t["ref"],
                "供应商": t["supplier"],
                "当前状态": t["status"],
                "关键日期": t["due_date"] or "—",
                "逾期 / 剩余": _days_text(t["days"]),
                "建议动作": t["action_hint"],
            }
            for t in shown
        ],
        use_container_width=True,
        hide_index=True,
    )
    counts = {}
    for t in pending:
        counts[t["priority"]] = counts.get(t["priority"], 0) + 1
    summary = "、".join(f"第 {p} 类 {counts[p]} 条" for p in sorted(counts))
    if len(pending) > display_limit:
        st.caption(
            f"共 {len(pending)} 条（{summary}），只显示优先级最高的 {display_limit} 条。"
            "待办这么多说明积压严重，建议先按供应商集中处理。"
        )
    else:
        st.caption(f"共 {len(pending)} 条（{summary}）。同一订单可能命中多条，每条对应一个动作。")
else:
    st.success("暂无需要处理的订单。")

st.divider()

# ---------------- 基础数据 ----------------
st.subheader("基础数据")
b1, b2, b3 = st.columns(3)
b1.metric("供应商", repo_suppliers.count_suppliers())
b2.metric("物料 / 服务", repo_items.count_items())
b3.metric("订单总数", int(db.scalar("SELECT COUNT(*) FROM orders") or 0))

with st.expander("数据存放位置"):
    st.code(DB_FILE, language="text")
    st.caption("请到「设置」导出完整备份；请另存一份备份到手机或其他设备。")
