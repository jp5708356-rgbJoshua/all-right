"""订单表预览与原子导入；不创建供应商。"""
from . import db, repo_orders, repo_suppliers, repo_fx

HEADERS = {'订单号': 'order_no', '供应商': 'supplier_id', '品名': 'item_name_raw',
           '数量': 'quantity', '单价': 'unit_price', '总价': 'total_amount',
           '币种': 'currency', '下单日期': 'order_date', '承诺交期': 'promised_delivery_date'}


def preview(frame, conn=None):
    required = ['订单号', '供应商', '品名', '数量', '下单日期']
    missing = [x for x in required if x not in frame.columns]
    if missing:
        raise ValueError('缺少列：' + '、'.join(missing))
    suppliers = {}
    for row in repo_suppliers.list_suppliers(conn=conn):
        suppliers.setdefault(row['name'], []).append(row['id'])
    seen, rows, errors = set(), [], []
    for number, record in enumerate(frame.to_dict('records'), 2):
        try:
            data = {field: record.get(label) for label, field in HEADERS.items()}
            ids = suppliers.get(str(data['supplier_id']).strip(), [])
            if len(ids) != 1:
                raise ValueError('供应商不存在或重名，请先整理供应商档案')
            data['supplier_id'] = ids[0]
            data['currency'] = data.get('currency') or repo_fx.base_currency()
            cleaned = repo_orders._clean_new(data, conn)
            if cleaned['order_no'] in seen:
                raise ValueError('文件内订单号重复')
            seen.add(cleaned['order_no'])
            rows.append(cleaned)
        except (ValueError, TypeError) as exc:
            errors.append({'表格行': number, '问题': str(exc)})
    return rows, errors


def commit(frame, conn=None):
    with db.transaction(conn):
        rows, errors = preview(frame, conn)
        if errors:
            raise ValueError('检查未通过，未导入任何订单')
        for row in rows:
            repo_orders.create_order(row, conn)
    return len(rows)
