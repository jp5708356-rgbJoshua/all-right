"""配置读取。

所有权重、阈值、提醒天数、Webhook 地址只从 config.yaml 来。
本文件不提供任何业务数值的"内置默认值"——缺失就明确报错，
避免出现代码里藏着一份权重、配置文件改了却不生效的情况。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("PDESK_CONFIG_PATH", str(ROOT / "config.yaml")))

# 仅放与业务无关的结构性默认值（路径、条数上限这类），不放权重和阈值
_STRUCTURAL_DEFAULTS: dict[str, Any] = {
    "app.name": "个人采购工作台",
    "app.db_path": "data/procurement.db",
    "app.page_size": 50,
    "push.max_chars": 1800,
    "push.max_orders_per_section": 10,
    "fx.base_currency": "CNY",
}


class ConfigError(RuntimeError):
    """配置文件缺失或关键配置项缺失。"""


@lru_cache(maxsize=1)
def _load() -> dict:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"找不到配置文件：{CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError("config.yaml 顶层必须是键值结构")
    return data


def reload() -> dict:
    """设置页保存后调用，让改动立即生效。"""
    _load.cache_clear()
    return _load()


def get(path: str, default: Any = None) -> Any:
    """按点路径取值，如 get("reminders.contract_days")。"""
    node: Any = _load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _STRUCTURAL_DEFAULTS.get(path, default)
        node = node[part]
    return node


def require(path: str) -> Any:
    """关键配置，缺了直接报错并指出该改哪一行。"""
    value = get(path)
    if value in (None, "", {}, []):
        raise ConfigError(f"config.yaml 缺少必填配置项：{path}")
    return value


def db_path() -> Path:
    # 环境变量优先，便于测试或多套数据（如 PDESK_DB_PATH=data/demo.db）切换
    raw = os.environ.get("PDESK_DB_PATH") or str(get("app.db_path", "data/procurement.db"))
    path = Path(raw)
    return path if path.is_absolute() else (ROOT / path)


# ---- 评分卡：权重与评级阈值，全部走配置文件 ----

def scorecard_weights() -> dict[str, float]:
    weights = require("scorecard.weights")
    if not isinstance(weights, dict):
        raise ConfigError("config.yaml 的 scorecard.weights 必须是四维键值对")
    cleaned = {str(k): float(v) for k, v in weights.items() if v is not None}
    total = sum(cleaned.values())
    if total <= 0:
        raise ConfigError("scorecard.weights 权重之和必须大于 0")
    # 和不为 1 时按比例归一化，不静默改语义
    return {k: v / total for k, v in cleaned.items()}


def grade_thresholds() -> dict[str, float]:
    grades = require("scorecard.grades")
    if not isinstance(grades, dict):
        raise ConfigError("config.yaml 的 scorecard.grades 必须是评级到阈值的映射")
    return {str(k): float(v) for k, v in grades.items()}


def reminder_days(key: str) -> int:
    value = get(f"reminders.{key}")
    if value is None:
        raise ConfigError(f"config.yaml 缺少 reminders.{key}")
    return int(value)


def push_config() -> dict:
    return dict(get("push", {}) or {})


def update_yaml_scalar(section: str, key: str, value) -> bool:
    """按行替换 config.yaml 里某个标量值，保留注释与缩进。

    用 yaml.safe_dump 整体重写会丢注释、打乱格式，所以这里做定点替换。
    找不到对应键时返回 False，由页面提示用户手改。
    """
    if not CONFIG_PATH.exists():
        return False

    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_section = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if line[:1] not in (" ", "\t"):
            in_section = stripped.split(":", 1)[0].strip() == section
            continue

        if not in_section or not stripped.startswith(f"{key}:"):
            continue

        indent = line[: len(line) - len(line.lstrip())]
        tail = ""
        if "#" in stripped:
            tail = "  " + stripped[stripped.index("#"):]
        lines[idx] = f"{indent}{key}: {value}{tail}"
        import tempfile
        fd, temp_path = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix=".config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, CONFIG_PATH)
        finally:
            Path(temp_path).unlink(missing_ok=True)
        reload()
        return True

    return False
