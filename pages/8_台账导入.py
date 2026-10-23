"""台账预览与确认导入。"""
import pandas as pd
import streamlit as st
from core import db, table_import

st.set_page_config(page_title='台账导入', page_icon='📥', layout='wide')

from core.auth import require_login
require_login(st)
db.init_db()
st.title('台账导入')
from core import workspace
workspace.banner(st)

st.caption('支持 Excel 和逗号分隔表格。仅新增订单，不覆盖历史记录；供应商须已存在且名称唯一。')
st.write('必填列：订单号、供应商、品名、数量、下单日期。可选列：单价、总价、币种、承诺交期。日期请用 年-月-日 格式。')
file = st.file_uploader('选择订单台账', type=['xlsx', 'csv'])
if file:
    try:
        if file.size > 10 * 1024 * 1024:
            raise ValueError('文件超过 10 兆字节，请拆分后导入')
        frame = (pd.read_excel(file, dtype=str, keep_default_na=False) if file.name.endswith('.xlsx')
                 else pd.read_csv(file, dtype=str, keep_default_na=False))
        if len(frame) > 5000:
            raise ValueError('每次最多导入 5000 行')
        st.dataframe(frame.head(100), hide_index=True)
        rows, errors = table_import.preview(frame)
        if errors:
            st.error('存在问题，请修正文件后重新上传。未写入数据库。')
            st.dataframe(errors, hide_index=True)
        elif rows:
            st.success(f'检查通过，可新增 {len(rows)} 笔订单')
            confirmed = st.checkbox('我已核对预览，确认新增这些订单')
            if st.button('确认导入', disabled=not confirmed):
                count = table_import.commit(frame)
                st.success(f'已导入 {count} 笔订单')
    except Exception as exc:
        st.error(f'无法导入：{exc}')
