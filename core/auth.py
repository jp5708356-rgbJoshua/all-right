"""Single-owner login. No default credentials or authentication bypass."""
import contextlib
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path
from . import config

ITERATIONS = 600_000
TTL = 8 * 3600

def password_hash(password):
    if len(password) < 12:
        raise ValueError("密码至少12个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"

def verify(password, encoded):
    try:
        algorithm, iterations, salt, digest = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not 600_000 <= int(iterations) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False

def configured():
    encoded = os.environ.get("PDESK_PASSWORD_HASH", "")
    try:
        alg, count, salt, digest = encoded.split("$")
        return bool(os.environ.get("PDESK_LOGIN_USER")) and alg == "pbkdf2_sha256" and 600_000 <= int(count) <= 2_000_000 and len(bytes.fromhex(salt)) >= 16 and len(bytes.fromhex(digest)) == 32
    except (ValueError, TypeError):
        return False

def authenticate(username, password, now=None):
    if not configured():
        return False
    now = time.time() if now is None else now
    path = config.db_path().parent / "auth.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Single-owner global limiter persists across sessions and process restarts.
    with contextlib.closing(sqlite3.connect(path, timeout=10)) as c, c:
        c.execute("CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, failures INTEGER, blocked REAL)")
        c.execute("INSERT OR IGNORE INTO attempts VALUES (1,0,0)")
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        failures, blocked = c.execute("SELECT failures,blocked FROM attempts WHERE id=1").fetchone()
        if blocked > now:
            return False
        if blocked:
            failures = 0
        valid_password = verify(password, os.environ["PDESK_PASSWORD_HASH"])
        valid = hmac.compare_digest(username.encode(), os.environ["PDESK_LOGIN_USER"].encode()) and valid_password
        failures = 0 if valid else failures + 1
        c.execute("UPDATE attempts SET failures=?,blocked=? WHERE id=1", (failures, now+900 if failures >= 10 else 0))
        return valid

def require_login(st):
    if not configured():
        st.error("管理员尚未配置登录账号和密码哈希，工作区已锁定。")
        st.stop()
    fingerprint = hashlib.sha256((os.environ["PDESK_LOGIN_USER"] + os.environ["PDESK_PASSWORD_HASH"]).encode()).hexdigest()
    session = st.session_state.get("_pdesk_auth")
    if session and session[0] == fingerprint and 0 <= time.time()-session[1] < TTL:
        if st.sidebar.button("退出登录", key="_pdesk_logout"):
            st.session_state.clear()
            st.rerun()
        return
    st.session_state.pop("_pdesk_auth", None)
    st.title("采购工作台 · 登录")
    with st.form("_pdesk_login", clear_on_submit=True):
        username = st.text_input("账号")
        password = st.text_input("密码", type="password")
        submitted = st.form_submit_button("登录")
    if submitted:
        if authenticate(username, password):
            st.session_state["_pdesk_auth"] = (fingerprint, time.time())
            st.rerun()
        else:
            st.error("账号或密码错误，或尝试次数过多。连续失败10次后需等待15分钟。")
    st.stop()
