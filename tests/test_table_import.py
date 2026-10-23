import sys
import tempfile
import unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import db, table_import, repo_suppliers

class ImportTests(unittest.TestCase):
    def test_import_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = db.connect(Path(directory) / 'test.db')
            db.init_db(conn)
            repo_suppliers.create_supplier({'name': '测试供应商'}, conn)
            data = {'订单号': 'IMPORT-1', '供应商': '测试供应商', '品名': '测试物料',
                    '数量': '10', '单价': '2', '下单日期': '2026-10-23'}
            frame = pd.DataFrame([data])
            rows, errors = table_import.preview(frame, conn)
            self.assertFalse(errors)
            self.assertEqual(rows[0]['total_amount'], 20)
            self.assertEqual(db.scalar('SELECT COUNT(*) FROM orders', conn=conn), 0)
            duplicate = pd.DataFrame([data, data])
            with self.assertRaises(ValueError):
                table_import.commit(duplicate, conn)
            self.assertEqual(db.scalar('SELECT COUNT(*) FROM orders', conn=conn), 0)
            self.assertEqual(table_import.commit(frame, conn), 1)
            with self.assertRaises(ValueError):
                table_import.commit(frame, conn)
            self.assertEqual(db.scalar('SELECT COUNT(*) FROM orders', conn=conn), 1)
            conn.close()

if __name__ == '__main__':
    unittest.main()
