"""Run on a Docker host: build, restart/recreate on same disk, restore backup.
This script has not been run in the delivered environment (no Docker daemon).
"""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.auth import password_hash
suffix=secrets.token_hex(4)
name='pdesk-check-'+suffix
volume=name+'-data'
image=name+':test'
env=os.environ.copy()
env['PDESK_LOGIN_USER']='container-test'
env['PDESK_PASSWORD_HASH']=password_hash(secrets.token_urlsafe(24))
def docker(*args,capture=False):
    return subprocess.run(['docker',*args],cwd=ROOT,env=env,check=True,text=True,capture_output=capture)
def start():
    docker('run','-d','--name',name,'-e','PDESK_LOGIN_USER','-e','PDESK_PASSWORD_HASH','-p','127.0.0.1::8501','-v',volume+':/data',image)
    endpoint=docker('port',name,'8501',capture=True).stdout.strip()
    for _ in range(120):
        try:
            with urllib.request.urlopen('http://'+endpoint+'/_stcore/health',timeout=1) as response:
                if response.status==200:break
        except OSError:pass
        time.sleep(.5)
    else:raise RuntimeError('Container never became healthy')
    return endpoint
def execute(code):return docker('exec','--user','10001:10001',name,'python','-c',code)
try:
    docker('build','-t',image,'.')
    endpoint=start()
    from websockets.sync.client import connect
    with connect('ws://'+endpoint+'/_stcore/stream',origin='http://'+endpoint,subprotocols=['streamlit'],open_timeout=5):pass
    execute("import os; assert os.geteuid()==10001; status=open('/proc/1/status').read(); uid_line=next(line for line in status.splitlines() if line.startswith('Uid:')); assert all(int(uid)==10001 for uid in uid_line.split()[1:]); from core import db,config,repo_suppliers; db.init_db(); repo_suppliers.create_supplier({'name':'container-restart-check'}); config.update_yaml_scalar('reminders','delivery_due_days',17)")
    docker('restart',name)
    # Remove the whole container to verify the volume survives recreation.
    docker('rm','-f',name)
    start()
    execute("from core import db,config; db.init_db(); assert db.scalar(\"SELECT count(*) FROM suppliers WHERE name='container-restart-check'\")==1; assert config.reminder_days('delivery_due_days')==17")
    execute("from core import db,backups; import sqlite3; from pathlib import Path; db.init_db(); p=backups.create_bundle(); backups.restore_bundle(p,Path('/tmp/restore-check')); c=sqlite3.connect('/tmp/restore-check/procurement.db'); assert c.execute(\"SELECT count(*) FROM suppliers WHERE name='container-restart-check'\").fetchone()[0]==1")
    docker('exec','--user','10001:10001',name,'python','tests/test_deployment.py')
    print('PASS container build, non-root runtime, WebSocket, recreation, persistence and backup restore')
except Exception:
    subprocess.run(['docker','logs',name],check=False)
    raise
finally:
    subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
