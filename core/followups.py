"""沟通记录与跟进提醒，不代表对外承诺。"""
from datetime import date
from . import db

KINDS = ('催交期', '确认发货', '收货异常', '催发票', '付款进度', '询价跟进', '其他')


def create(order_id, kind, note, supplier_reply='', next_date=None, conn=None):
    if kind not in KINDS:
        raise ValueError('请选择有效的跟进类型')
    note = str(note or '').strip()
    if not note:
        raise ValueError('请填写本次跟进内容')
    if not db.row('SELECT id FROM orders WHERE id=?', (order_id,), conn):
        raise ValueError('订单不存在')
    next_date = date.fromisoformat(str(next_date)).isoformat() if next_date else None
    return db.execute('INSERT INTO followups(order_id,kind,note,supplier_reply,next_date) VALUES(?,?,?,?,?)',
                      (order_id, kind, note, str(supplier_reply or '').strip(), next_date), conn)


def list_rows(order_id=None, due_only=False, conn=None):
    sql = '''SELECT f.*, o.order_no, o.status, s.name AS supplier_name
             FROM followups f JOIN orders o ON o.id=f.order_id
             JOIN suppliers s ON s.id=o.supplier_id WHERE 1=1'''
    params = []
    if order_id is not None:
        sql += ' AND f.order_id=?'
        params.append(order_id)
    if due_only:
        sql += " AND f.closed=0 AND f.next_date<=? AND o.status NOT IN ('已完成','已取消')"
        params.append(date.today().isoformat())
    sql += ' ORDER BY f.next_date IS NULL, f.next_date, f.id DESC'
    return db.rows(sql, tuple(params), conn)


def close(record_id, conn=None):
    db.execute('UPDATE followups SET closed=1 WHERE id=?', (record_id,), conn)
