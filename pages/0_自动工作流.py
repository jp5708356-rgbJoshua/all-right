"""本地草稿自动化。"""
import streamlit as st
from core import db, workspace, workflows
st.set_page_config(page_title='自动工作流', page_icon='🔁', layout='wide')

from core.auth import require_login
require_login(st)
db.init_db()
st.title('自动工作流')
workspace.banner(st)
st.caption('首次打开自动保存当日清单。下面实时预览最新数据；网页关闭时不会后台运行。')
st.subheader('每日工作清单')
try:
    report, drafts = workflows.generate()
    st.text(report)
    st.download_button('下载最新工作清单', report, file_name='采购工作清单.txt', mime='text/plain')
    if st.button('更新今日已保存清单'):
        workflows.daily_report(force=True)
        st.success('今日清单已更新')
    st.subheader('按供应商整理沟通草稿')
    st.caption('仅供复制核对，不会自动发送；付款信息需向财务确认。')
    for supplier, draft in drafts.items():
        with st.expander(supplier):
            st.code(draft, language=None)
    if not drafts:
        st.info('当前没有需要生成供应商沟通草稿的订单。')
except Exception as exc:
    st.error(f'生成失败：{exc}')
