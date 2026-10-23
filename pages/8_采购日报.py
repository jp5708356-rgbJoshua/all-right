import streamlit as st
from datetime import date
from core import db, workspace, daily_summary
st.set_page_config(page_title='采购日报', page_icon='📝', layout='wide')

from core.auth import require_login
require_login(st)
db.init_db()
st.title('采购日报')
workspace.banner(st)
st.caption('根据已录入数据生成草稿，核对后复制到工作群或汇报文档。不会发送消息。')
day = st.date_input('汇报日期', value=date.today())
text = daily_summary.build(day)
edited = st.text_area('日报草稿（可修改后复制）', value=text, height=480, key=f'summary_{day}')
st.download_button('下载日报', edited, file_name=f'采购日报-{day}.txt', mime='text/plain')
st.caption('下载包含上方已修改的内容。')
