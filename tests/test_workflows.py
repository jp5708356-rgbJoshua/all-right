import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import db, workflows, repo_orders, repo_suppliers

class WorkflowTests(unittest.TestCase):
    def test_report_is_readonly_and_repeatable(self):
        previous = os.environ.get('PDESK_DB_PATH')
        with tempfile.TemporaryDirectory() as directory:
            os.environ['PDESK_DB_PATH'] = str(Path(directory) / 'work.db')
            try:
                db.close_conn()
                db.init_db()
                sid = repo_suppliers.create_supplier({'name':'模拟供应商','contact_phone':'SECRET-PHONE','bank_account':'SECRET-BANK'})
                oid = repo_orders.create_order({'order_no':'AUTO-1','supplier_id':sid,'item_name_raw':'物料','quantity':1,'order_date':'2020-01-01','promised_delivery_date':'2020-01-02'})
                before = repo_orders.get_order(oid)
                report, drafts = workflows.generate()
                self.assertIn('AUTO-1',report)
                self.assertIn('模拟供应商',drafts)
                self.assertNotIn('SECRET',report+str(drafts))
                self.assertEqual(repo_orders.get_order(oid),before)
                target = workflows.daily_report()
                saved = target.stat().st_mtime_ns
                self.assertEqual(workflows.daily_report().stat().st_mtime_ns,saved)
                self.assertEqual(target.read_text(),report)
            finally:
                db.close_conn()
                if previous is None: os.environ.pop('PDESK_DB_PATH',None)
                else: os.environ['PDESK_DB_PATH']=previous
if __name__=='__main__':unittest.main()
