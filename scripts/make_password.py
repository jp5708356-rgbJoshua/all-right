import getpass
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.auth import password_hash
password = getpass.getpass("新密码（至少12字符）: ")
if password != getpass.getpass("再次输入: "):
    raise SystemExit("两次输入不一致")
print(password_hash(password))
