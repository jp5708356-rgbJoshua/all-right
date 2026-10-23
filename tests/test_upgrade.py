"""升级回归：真实临时 SQLite 库，不触碰业务数据库。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import db, repo_orders


class UpgradeTests(unittest.TestCase):
    def test_nonfinite(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(repo_orders.ValidationError):
                repo_orders._to_float(value, "金额", minimum=0)

    def test_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            target = Path(directory) / "backup.db"
            conn = db.connect(source)
            try:
                conn.execute("CREATE TABLE evidence (value TEXT)")
                conn.execute("INSERT INTO evidence VALUES ('已提交')")
                conn.commit()
                db.snapshot(target, conn)
                with sqlite3.connect(target) as restored:
                    self.assertEqual(restored.execute("SELECT value FROM evidence").fetchone()[0], "已提交")
                with self.assertRaises(ValueError):
                    db.snapshot(source, conn)
                previous = target.read_bytes()
                with patch("core.db.os.replace", side_effect=OSError("模拟替换失败")):
                    with self.assertRaises(OSError):
                        db.snapshot(target, conn)
                self.assertEqual(target.read_bytes(), previous)
                self.assertEqual(list(Path(directory).glob(".snapshot-*")), [])
                conn.execute("INSERT INTO evidence VALUES ('未提交')")
                with self.assertRaises(ValueError):
                    db.snapshot(target, conn)
                self.assertEqual(target.read_bytes(), previous)
                conn.rollback()
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
