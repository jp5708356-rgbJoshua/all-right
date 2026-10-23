"""每日催交与沟通记录。"""
import streamlit as st
from core import db, workspace, followups, repo_orders

st.set_page_config(page_title='催交与跟进', page_icon='📞', layout='wide')

from core.auth import require_login
require_login(st)
db.init_db()
st.title('催交与跟进')
workspace.banner(st)
st.caption('记录你已完成的沟通和供应商答复，不会发送消息，也不会自动修改订单交期。')
due = followups.list_rows(due_only=True)
st.subheader(f'今天需跟进 · {len(due)} 项')
for row in due:
    with st.expander(f"{row['next_date']} · {row['order_no']} · {row['supplier_name']} · {row['kind']}"):
        st.write(row['note'])
        st.write('供应商答复：' + (row['supplier_reply'] or '尚未记录'))
        if st.button('本项跟进已处理', key=f"close_followup_{row['id']}"):
            followups.close(row['id'])
            st.rerun()
if not due:
    st.success('没有到期的跟进提醒。')

st.subheader('记录本次沟通')
keyword = st.text_input('搜索订单号或品名')
orders = repo_orders.list_orders(keyword=keyword, limit=200)
if orders:
    choices = {f"{r['order_no']} · {r['supplier_name']} · {r['item_name_raw']}": r['id'] for r in orders}
    picked = st.selectbox('关联订单', list(choices))
    order_id = choices[picked]
    with st.form('followup_record', clear_on_submit=True):
        kind = st.selectbox('跟进类型', followups.KINDS)
        note = st.text_area('本次跟进内容 *', placeholder='例如：询问本周能否发货，请对方提供物流单号')
        reply = st.text_area('供应商答复', placeholder='如实记录，未回复可留空')
        next_date = st.date_input('下次跟进日期', value=None)
        saved = st.form_submit_button('保存沟通记录')
    if saved:
        try:
            followups.create(order_id, kind, note, reply, next_date)
            st.success('沟通记录已保存')
        except ValueError as exc:
            st.error(str(exc))
    st.subheader('该订单沟通历史')
    for row in followups.list_rows(order_id):
        with st.expander(f"{row['created_at']} · {row['kind']} · {'已处理' if row['closed'] else '已记录'}"):
            st.write(row['note'])
            st.write('供应商答复：' + (row['supplier_reply'] or '未回复'))
            st.caption('下次跟进：' + (row['next_date'] or '未设置'))
else:
    st.info('没有匹配订单，请先建立订单，或调整搜索条件。')
