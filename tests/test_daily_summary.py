import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import db, daily_summary, repo_orders, repo_suppliers, followups, workflows

class SummaryTests(unittest.TestCase):
    def test_actual_records_and_internal_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = db.connect(Path(directory)/'summary.db')
            db.init_db(conn)
            sid = repo_suppliers.create_supplier({'name':'测试供应商','risk_level':'高'},conn)
            today = str(date.today())
            oid = repo_orders.create_order({'order_no':'SUMMARY-1','supplier_id':sid,'item_name_raw':'测试','quantity':2,'unit_price':5,'order_date':today},conn)
            followups.create(oid,'催交期','询问进度','',today,conn)
            result = daily_summary.build(today,conn)
            self.assertIn('新增录入订单：1 笔',result)
            self.assertIn('合计 10.00',result)
            self.assertIn('沟通记录：1 条',result)
            self.assertIn('催交期：1 条',result)
            self.assertIn('到期未处理跟进：1 项',result)
            report,drafts = workflows.generate(conn)
            self.assertIn('高风险',report)
            self.assertFalse(drafts)  # 内部风险不生成供应商外发内容
            self.assertEqual(repo_orders.get_order(oid,conn)['status'],'已下单')
            conn.close()
if __name__=='__main__':unittest.main()
