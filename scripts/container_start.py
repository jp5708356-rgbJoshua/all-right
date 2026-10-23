"""Fail closed, drop privileges, supervise Streamlit and hourly backups."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, db
from core.auth import configured
if not configured():
    raise SystemExit("Set PDESK_LOGIN_USER and valid PDESK_PASSWORD_HASH before startup")
# Render mounts may initially be root-owned. Drop privileges before opening data.
if os.geteuid() == 0 and os.environ.get("PDESK_CONTAINER") == "1":
    import pwd
    account = pwd.getpwnam("appuser")
    data_dir = config.db_path().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chown(data_dir, account.pw_uid, account.pw_gid)
    for directory, dirs, files in os.walk(data_dir, followlinks=False):
        for name in dirs + files:
            os.chown(Path(directory)/name, account.pw_uid, account.pw_gid, follow_symlinks=False)
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
if not config.CONFIG_PATH.exists():
    # Exclusive create prevents accidental overwriting of persistent settings.
    with config.CONFIG_PATH.open("x") as target:
        target.write((config.ROOT / "config.yaml").read_text())
config.db_path().parent.mkdir(parents=True, exist_ok=True)
db.init_db()
db.close_conn()
# A small supervisor makes backups independently of browser visits.
import signal
import subprocess
import threading
import logging
from core import backups
stop = threading.Event()
def backup_loop():
    while not stop.is_set():
        try:
            db.init_db()
            backups.create_bundle()
            # Keep the latest 168 hourly bundles; download off-server copies too.
            files = sorted((config.db_path().parent / "backups").glob("full-*.zip"))
            for old in files[:-168]:
                old.unlink()
        except Exception:
            logging.exception("Scheduled backup failed")
        finally:
            db.close_conn()
        stop.wait(3600)
child = subprocess.Popen(["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port="+os.environ.get("PORT", "8501"), "--server.headless=true"])
def shutdown(signum, frame):
    stop.set()
    child.terminate()
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
worker = threading.Thread(target=backup_loop, daemon=True)
worker.start()
try:
    code = child.wait()
finally:
    stop.set()
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=15)
    worker.join(timeout=10)
sys.exit(code)
