"""基于实际记录生成日报，金额按币种分别列示。"""
from datetime import date
from . import db


def build(on_date=None, conn=None):
    day = date.fromisoformat(str(on_date or date.today())).isoformat()
    new = db.rows('''SELECT currency, COUNT(*) AS count, SUM(total_amount) AS amount,
                     SUM(total_amount IS NULL) AS missing FROM orders
                     WHERE substr(created_at,1,10)=? GROUP BY currency ORDER BY currency''', (day,), conn)
    contacts = db.rows('''SELECT f.kind, COUNT(*) AS count FROM followups f
                          WHERE substr(f.created_at,1,10)=? GROUP BY f.kind ORDER BY f.kind''', (day,), conn)
    received = db.scalar('SELECT COUNT(*) FROM orders WHERE actual_delivery_date=? AND status<>?', (day,'已取消'), conn)
    overdue = db.scalar("SELECT COUNT(*) FROM orders WHERE promised_delivery_date<? AND status NOT IN ('已到货','已对账','已完成','已取消')", (day,), conn)
    exceptions = db.scalar("SELECT COUNT(*) FROM orders WHERE status='异常'", (), conn)
    due = db.scalar("""SELECT COUNT(*) FROM followups f JOIN orders o ON o.id=f.order_id
                       WHERE f.closed=0 AND f.next_date<=? AND o.status NOT IN ('已完成','已取消')""", (day,), conn)
    lines = [f'采购日报草稿｜{day}', '', '一、当日记录']
    count = sum(r['count'] for r in new)
    lines.append(f'新增录入订单：{count} 笔（按系统录入日期，含补录及后续取消）。')
    for row in new:
        amount = '未填' if row['amount'] is None else f"{row['amount']:,.2f}"
        lines.append(f"- {row['currency']}：{row['count']} 笔，已填金额合计 {amount}；{row['missing']} 笔金额未填。")
    lines.append(f'记录到货：{received} 笔（按实际到货日期）。')
    lines.append(f"沟通记录：{sum(r['count'] for r in contacts)} 条。")
    for row in contacts:
        lines.append(f"- {row['kind']}：{row['count']} 条。")
    lines += ['', '二、当前待办与风险', f'逾期未到货：{overdue} 笔；异常订单：{exceptions} 笔；到期未处理跟进：{due} 项。',
              '', '三、需人工补充', '需协调事项：________', '下一步安排：________', '',
              '说明：本日报为系统记录草稿，不代表对外承诺或付款确认。各币种金额不相加。风险为查询时当前状态，不是历史快照。']
    return '\n'.join(lines)
