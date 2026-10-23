"""Portable backup bundles. Restore only into an empty, offline directory."""
import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
import yaml
from . import config, db

def create_bundle(target=None):
    data = config.db_path().parent
    target = Path(target) if target else data / "backups" / ("full-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        stage = Path(tmp)
        snapshot = db.snapshot(stage / "procurement.db")
        contents = {"procurement.db": snapshot.read_bytes(), "config.yaml": config.CONFIG_PATH.read_bytes()}
        for p in (data / "daily_reports").glob("*.txt"):
            contents["daily_reports/"+p.name] = p.read_bytes()
        manifest = {k: hashlib.sha256(v).hexdigest() for k,v in contents.items()}
        with zipfile.ZipFile(stage / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for name, payload in contents.items():
                z.writestr(name, payload)
            z.writestr("manifest.json", json.dumps(manifest))
        os.replace(stage / "bundle.zip", target)
    return target

def restore_bundle(source, destination):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("恢复目标必须为空目录；禁止覆盖正在运行的数据库")
    with zipfile.ZipFile(source) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or sum(i.file_size for i in z.infolist()) > 512 * 1024**2:
            raise ValueError("备份重复文件或超过512MiB恢复限制")
        allowed = lambda n: n in {"procurement.db", "config.yaml", "manifest.json"} or (n.startswith("daily_reports/") and Path(n).suffix == ".txt" and len(Path(n).parts) == 2)
        if any(not allowed(n) or ".." in Path(n).parts or Path(n).is_absolute() for n in names):
            raise ValueError("备份包含非法路径")
        contents = {n:z.read(n) for n in names if n != "manifest.json"}
        manifest = json.loads(z.read("manifest.json"))
        if set(manifest) != set(contents) or not {"procurement.db","config.yaml"} <= set(contents):
            raise ValueError("备份清单不完整")
        if any(hashlib.sha256(v).hexdigest() != manifest[n] for n,v in contents.items()):
            raise ValueError("备份校验失败")
        if not isinstance(yaml.safe_load(contents["config.yaml"]), dict):
            raise ValueError("配置无效")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            stage = Path(tmp)
            for n,v in contents.items():
                p=stage/n
                p.parent.mkdir(parents=True,exist_ok=True)
                p.write_bytes(v)
            with contextlib.closing(sqlite3.connect(stage/"procurement.db")) as c:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or c.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("数据库完整性检查失败")
                expected = {"orders","suppliers","items","contracts","quotes","reconciliation","followups","fx_rates"}
                actual = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not expected <= actual:
                    raise ValueError("数据库缺少业务表")
            # All validation completes before any restored file is published.
            if destination.exists():
                destination.rmdir()
            os.replace(stage, destination)
    return destination
