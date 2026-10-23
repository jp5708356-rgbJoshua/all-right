import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import db, followups, repo_orders, repo_suppliers

class FollowupTests(unittest.TestCase):
    def test_due_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = db.connect(Path(directory) / 'test.db')
            db.init_db(conn)
            sid = repo_suppliers.create_supplier({'name': '模拟供应商'}, conn)
            oid = repo_orders.create_order({'order_no':'FOLLOW-1','supplier_id':sid,'item_name_raw':'模拟物料','quantity':1,'order_date':str(date.today())}, conn)
            today = date.today()
            old = followups.create(oid,'催交期','询问发货时间','等待回复',today-timedelta(days=1),conn)
            followups.create(oid,'催发票','索取发票','',today+timedelta(days=1),conn)
            followups.create(oid,'其他','记录沟通',conn=conn)
            self.assertEqual(len(followups.list_rows(due_only=True,conn=conn)),1)
            followups.close(old,conn)
            self.assertFalse(followups.list_rows(due_only=True,conn=conn))
            self.assertEqual(len(followups.list_rows(oid,conn=conn)),3)
            self.assertEqual(repo_orders.get_order(oid,conn)['status'],'已下单')
            followups.create(oid,'催交期','再次催交','',today,conn)
            repo_orders.cancel_order(oid,conn=conn)
            self.assertFalse(followups.list_rows(due_only=True,conn=conn))
            db.init_db(conn)
            self.assertEqual(len(followups.list_rows(oid,conn=conn)),4)
            with self.assertRaises(ValueError):
                followups.create(oid,'催交期','',conn=conn)
            conn.close()
if __name__=='__main__':unittest.main()
