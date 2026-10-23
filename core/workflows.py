"""本地自动工作简报：只生成草稿，不发送、不变更业务。"""
from collections import defaultdict
from datetime import date
import os
import tempfile
from . import config, todos, followups


def generate(conn=None):
    items = todos.build_todos(conn)
    due = followups.list_rows(due_only=True, conn=conn)
    lines = [f'采购工作清单 · {date.today().isoformat()}', '内部草稿，请核对后使用。',
             f'业务提醒 {len(items)} 项；到期沟通 {len(due)} 项。', '']
    for row in items:
        lines.append(f"□ {row['kind']}｜{row['ref']}｜{row['supplier']}｜{row['action_hint']}")
    for row in due:
        lines.append(f"□ 到期跟进｜{row['order_no']}｜{row['kind']}｜{row['next_date']}")
    if not items and not due:
        lines.append('当前没有命中的待办。')
    groups = defaultdict(list)
    for row in items:
        # 付款进度和供应商风险属于内部协调，不写入外发沟通草稿。
        if row.get('order_id') and ('交期' in row['kind'] or '货到' in row['kind']):
            groups[row['supplier']].append(row)
    drafts = {}
    for supplier, rows in groups.items():
        text = [f'{supplier}，您好，请协助确认以下事项：']
        for row in rows:
            if '票到' in row['kind']:
                action = '发票已收，付款进度将由我方向财务核实后反馈。'
            elif '货到' in row['kind']:
                action = '请确认发票开具及寄送进度。'
            elif '交期' in row['kind']:
                action = '请回复当前进度、预计发货日期及是否存在延期风险。'
            else:
                action = '请确认当前执行进度及是否存在异常。'
            text.append(f"- {row['ref']}：{action}")
        text.append('谢谢。以上为待核对草稿。')
        drafts[supplier] = '\n'.join(text)
    return '\n'.join(lines), drafts


def daily_report(force=False):
    path = config.db_path()
    target = path.parent / 'daily_reports' / f'{path.stem}-{date.today().isoformat()}.txt'
    if target.exists() and not force:
        return target
    report, _ = generate()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix='.report-', suffix='.txt')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(report)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return target
