"""Offline authentication, full-bundle restore, and real process-restart checks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zipfile
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core import auth, config, db, backups, repo_suppliers
from streamlit.testing.v1 import AppTest

class DeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoded=auth.password_hash('Testing-Password-2026')
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.data=Path(self.tmp.name)/'data'
        self.data.mkdir()
        self.env=patch.dict(os.environ,{
            'PDESK_DB_PATH':str(self.data/'procurement.db'),
            'PDESK_LOGIN_USER':'owner', 'PDESK_PASSWORD_HASH':self.encoded})
        self.env.start()
        self.cfg=patch.object(config,'CONFIG_PATH',self.data/'config.yaml')
        self.cfg.start()
        shutil.copy(ROOT/'config.yaml',config.CONFIG_PATH)
        config.reload()
    def tearDown(self):
        db.close_conn()
        self.cfg.stop()
        self.env.stop()
        config.reload()
        self.tmp.cleanup()
    def login(self,at):
        next(w for w in at.text_input if w.label=='账号').input('owner')
        next(w for w in at.text_input if w.label=='密码').input('Testing-Password-2026')
        next(w for w in at.button if w.label=='登录').click().run(timeout=20)
        self.assertFalse(at.exception,list(at.exception))
        return at
    def test_every_page_denies_unauthenticated_access(self):
        for page in [ROOT/'app.py',*sorted((ROOT/'pages').glob('*.py'))]:
            with self.subTest(page=page.name):
                at=AppTest.from_file(str(page)).run(timeout=20)
                self.assertFalse(at.exception,list(at.exception))
                self.assertEqual([w.label for w in at.text_input],['账号','密码'])
                self.assertEqual([w.label for w in at.button],['登录'])
                self.assertFalse((self.data/'procurement.db').exists())
    def test_login_wrong_password_logout_expiry_rotation(self):
        at=AppTest.from_file(str(ROOT/'app.py')).run(timeout=20)
        next(w for w in at.text_input if w.label=='密码').input('wrong')
        next(w for w in at.button if w.label=='登录').click().run(timeout=20)
        self.assertTrue(at.error)
        self.assertFalse((self.data/'procurement.db').exists())
        self.login(at)
        self.assertTrue((self.data/'procurement.db').exists())
        next(w for w in at.button if w.label=='退出登录').click().run()
        self.assertEqual([w.label for w in at.text_input],['账号','密码'])
        self.login(at)
        fingerprint=at.session_state['_pdesk_auth'][0]
        at.session_state['_pdesk_auth']=(fingerprint,time.time()-auth.TTL-1)
        at.run()
        self.assertEqual([w.label for w in at.text_input],['账号','密码'])
        self.login(at)
        with patch.dict(os.environ,PDESK_PASSWORD_HASH=auth.password_hash('A-Different-Password')):
            at.run()
            self.assertEqual([w.label for w in at.text_input],['账号','密码'])
    def test_fail_closed_and_persistent_lockout(self):
        with patch.dict(os.environ,PDESK_PASSWORD_HASH=''):
            at=AppTest.from_file(str(ROOT/'app.py')).run()
            self.assertTrue(at.error)
            self.assertFalse(at.button)
            self.assertFalse(auth.authenticate('owner','Testing-Password-2026'))
        for _ in range(10):
            self.assertFalse(auth.authenticate('owner','bad',now=100))
        self.assertFalse(auth.authenticate('owner','Testing-Password-2026',now=101))
        check=subprocess.run([sys.executable,'-c',"from core.auth import authenticate; assert not authenticate('owner','Testing-Password-2026',now=101)"],cwd=ROOT,env=os.environ.copy(),capture_output=True,text=True)
        self.assertEqual(check.returncode,0,check.stderr)
        self.assertTrue(auth.authenticate('owner','Testing-Password-2026',now=1001))
    def test_backup_restore_database_settings_reports(self):
        db.init_db()
        sid=repo_suppliers.create_supplier({'name':'恢复验收供应商'})
        config.update_yaml_scalar('reminders','delivery_due_days',11)
        (self.data/'daily_reports').mkdir()
        (self.data/'daily_reports'/'check.txt').write_text('采购日报验收')
        bundle=backups.create_bundle()
        target=Path(self.tmp.name)/'restored'
        backups.restore_bundle(bundle,target)
        with sqlite3.connect(target/'procurement.db') as c:
            self.assertEqual(c.execute('SELECT name FROM suppliers WHERE id=?',(sid,)).fetchone()[0],'恢复验收供应商')
        self.assertEqual((target/'config.yaml').read_bytes(),config.CONFIG_PATH.read_bytes())
        self.assertEqual((target/'daily_reports'/'check.txt').read_text(),'采购日报验收')
        with self.assertRaises(ValueError):
            backups.restore_bundle(bundle,target)
        with zipfile.ZipFile(bundle) as source:
            payload={n:source.read(n) for n in source.namelist()}
        payload['config.yaml']=b'tampered: true'
        bad=Path(self.tmp.name)/'bad.zip'
        with zipfile.ZipFile(bad,'w') as z:
            for n,v in payload.items():z.writestr(n,v)
        with self.assertRaises(ValueError):backups.restore_bundle(bad,Path(self.tmp.name)/'bad-restore')
        with zipfile.ZipFile(bad,'w') as z:z.writestr('../escape.txt','x')
        with self.assertRaises(ValueError):backups.restore_bundle(bad,Path(self.tmp.name)/'bad-path')
    def test_real_service_restart(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port=s.getsockname()[1]
        env=os.environ.copy()
        env.update(PORT=str(port),PDESK_CONFIG_PATH=str(config.CONFIG_PATH),PATH=str(Path(sys.executable).parent)+os.pathsep+env['PATH'])
        def start():
            log=open(Path(self.tmp.name)/'service.log','a')
            proc=subprocess.Popen([sys.executable,'scripts/container_start.py'],cwd=ROOT,env=env,stdout=log,stderr=log)
            log.close()
            try:
                for _ in range(150):
                    if proc.poll() is not None:raise AssertionError((Path(self.tmp.name)/'service.log').read_text())
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/_stcore/health',timeout=.3) as response:
                            if response.status==200:return proc
                    except OSError:pass
                    time.sleep(.1)
                raise AssertionError('Service did not become healthy')
            except BaseException:
                proc.terminate();proc.wait(timeout=15);raise
        proc=start()
        try:
            from websockets.sync.client import connect
            with connect(f'ws://127.0.0.1:{port}/_stcore/stream',
                         origin=f'http://127.0.0.1:{port}', subprotocols=['streamlit'],
                         open_timeout=5) as websocket:
                self.assertEqual(websocket.subprotocol,'streamlit')
            db.init_db()
            repo_suppliers.create_supplier({'name':'重启保留验收'})
            config.update_yaml_scalar('reminders','delivery_due_days',13)
            db.close_conn()
        finally:
            proc.terminate();proc.wait(timeout=20)
        proc=start()
        try:
            check=subprocess.run([sys.executable,'-c',"from core import db, config; db.init_db(); assert db.scalar(\"SELECT count(*) FROM suppliers WHERE name='重启保留验收'\")==1; assert config.reminder_days('delivery_due_days')==13"],cwd=ROOT,env=env,capture_output=True,text=True)
            self.assertEqual(check.returncode,0,check.stderr)
            for _ in range(50):
                if list((self.data/'backups').glob('full-*.zip')):break
                time.sleep(.1)
            self.assertTrue(list((self.data/'backups').glob('full-*.zip')))
        finally:
            proc.terminate();proc.wait(timeout=20)
        self.assertEqual(proc.returncode,0)

if __name__=='__main__':unittest.main(verbosity=2)
