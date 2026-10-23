"""独立临时库验收，使用真实 Streamlit AppTest。"""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import db, repo_orders as orders, repo_suppliers as suppliers, repo_recon as recon
from streamlit.testing.v1 import AppTest
from core.auth import password_hash
os.environ['PDESK_LOGIN_USER'] = 'acceptance'
os.environ['PDESK_PASSWORD_HASH'] = password_hash('Acceptance-Only-Password')
def login(at):
    at.run(timeout=20)
    next(x for x in at.text_input if x.label == '账号').input('acceptance')
    next(x for x in at.text_input if x.label == '密码').input('Acceptance-Only-Password')
    next(x for x in at.button if x.label == '登录').click().run(timeout=20)
    return at
root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'acceptance.db'
    os.environ['PDESK_DB_PATH'] = str(path)
    conn = db.init_db()
    suppliers.create_supplier({'name': '验收模拟供应商'})
    def widget(group, label):
        return next(x for x in group if x.label == label)
    app = login(AppTest.from_file(str(root / 'pages/2_订单.py')))
    assert not app.exception, list(app.exception)
    assert widget(app.selectbox, '币种').value == 'CNY'
    widget(app.text_input, '订单号 *').input('ACCEPT-001')
    widget(app.text_input, '品名 *').input('模拟物料')
    widget(app.number_input, '数量 *').set_value(10.0)
    widget(app.number_input, '单价').set_value(12.5)
    widget(app.button, '创建订单').click().run()
    assert not app.exception, list(app.exception)
    print('ERRORS', [x.value for x in app.error])
    order = orders.list_orders()[0]
    assert order['total_amount'] == 125
    print('PASS 真实页面表单创建订单，数量×单价=125')
    for status in ['供应商已确认', '生产中', '已发货', '已到货']:
        orders.update_status(order['id'], status)
    orders.mark_exception(order['id'], '模拟单据异常', '单据')
    assert orders.restore_exception(order['id']) == '已到货'
    for flag in recon.FLAGS:
        recon.upsert(order['id'], {flag: True})
    assert recon.is_complete(recon.get_recon(order['id']))
    for status in ['已对账', '已完成']:
        orders.update_status(order['id'], status)
    print('PASS 到货、异常恢复、货票款记录与完成流程')
    for page in [root / 'app.py', *sorted((root / 'pages').glob('*.py'))]:
        at = login(AppTest.from_file(str(page)))
        assert not at.exception, (page.name, list(at.exception))
        print('PASS 真实框架页面', page.name)
    cancelled = orders.create_order({'order_no': 'CANCEL-001', 'supplier_id': order['supplier_id'], 'item_name_raw': '模拟取消', 'quantity': 1, 'order_date': '2026-10-23'})
    orders.cancel_order(cancelled, '模拟取消验收')
    assert orders.get_order(cancelled)['status'] == '已取消'
    assert cancelled not in {r['order_id'] for r in recon.list_rows()}
    print('PASS 取消订单退出对账清单')
    backup = db.snapshot(Path(directory) / 'restore.db')
    restored = db.connect(backup)
    assert orders.get_order(order['id'], restored)['status'] == '已完成'
    restored.close()
    print('PASS 备份恢复后订单和完成状态保留')
    db.close_conn()
