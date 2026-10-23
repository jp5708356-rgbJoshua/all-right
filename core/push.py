"""消息推送（单向，只发不收）。

约束（需求 §4.2）：
- 只推待办摘要 + 单号，不推完整详情，不带联系人 / 电话 / 银行账号
- Webhook 地址只从 config.yaml 读，代码里没有任何 URL 字面量
- 默认关闭；关闭时 push_once() 直接返回 skipped，不发任何请求
- 不实现任何回调接收端点，不处理审批写回
- 定时触发交给系统 cron / launchd 调 push_daily.py，本模块不自带调度
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import config, todos

try:  # 延迟到真正发送时才需要，没装不影响其它功能
    import requests
except ImportError:  # pragma: no cover
    requests = None

PRIORITY_MARKS = {1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥"}


def build_summary(conn: sqlite3.Connection | None = None) -> dict:
    """按优先级汇总待办数量与单号，不携带任何明细字段。"""
    per_section = int(config.get("push.max_orders_per_section", 10) or 10)
    items = todos.build_todos(conn)

    groups: dict[int, dict] = {}
    for item in items:
        group = groups.setdefault(
            item["priority"], {"kind": item["kind"], "count": 0, "refs": []}
        )
        group["count"] += 1
        if len(group["refs"]) < per_section:
            group["refs"].append(item["ref"])

    return {
        "date": date.today().isoformat(),
        "total": len(items),
        "groups": groups,
        "truncated": {
            priority: group["count"] - len(group["refs"])
            for priority, group in groups.items()
            if group["count"] > len(group["refs"])
        },
    }


def render_text(summary: dict, max_chars: int = 1800) -> str:
    lines = [
        f"【采购工作台】{summary['date']} 待办摘要",
        f"共 {summary['total']} 条",
        "",
    ]

    for priority in sorted(summary["groups"]):
        group = summary["groups"][priority]
        lines.append(f"{PRIORITY_MARKS.get(priority, priority)} {group['kind']}　{group['count']} 条")
        if group["refs"]:
            extra = summary["truncated"].get(priority)
            suffix = f" 等（另有 {extra} 条）" if extra else ""
            lines.append("　" + "、".join(group["refs"]) + suffix)
        lines.append("")

    lines.append("详情请在本地工作台查看。")
    text = "\n".join(lines)

    if len(text) > max_chars:
        text = text[: max(0, max_chars - 24)] + "\n…（内容过长已截断，请打开工作台）"
    return text


def send(
    text: str,
    webhook_url: str,
    channel: str = "dingtalk",
    timeout: int = 8,
    session=None,
) -> dict:
    """发一条 text 消息。钉钉与企微的 text 消息体结构一致。"""
    if requests is None and session is None:
        return {"ok": False, "error": "未安装 requests，请先 pip install requests"}

    if not webhook_url or not webhook_url.strip():
        return {"ok": False, "error": "未配置 webhook_url"}

    payload = {"msgtype": "text", "text": {"content": text}}
    client = session or requests

    try:
        response = client.post(webhook_url.strip(), json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 网络异常一律转成可读结果
        return {"ok": False, "error": f"请求失败：{type(exc).__name__}: {exc}"}

    try:
        body = response.json() if getattr(response, "content", b"") else {}
    except ValueError:
        body = {}

    errcode = body.get("errcode") if isinstance(body, dict) else None
    ok = bool(getattr(response, "ok", False)) and errcode in (0, None)

    return {
        "ok": ok,
        "status": getattr(response, "status_code", None),
        "errcode": errcode,
        "channel": channel,
        "chars": len(text),
        "error": None if ok else (body.get("errmsg") if isinstance(body, dict) else None) or "推送被拒绝",
    }


def push_once(conn: sqlite3.Connection | None = None, session=None) -> dict:
    """读配置推一次。关闭时静默返回，不组装内容也不发请求。"""
    cfg = config.push_config()

    if not cfg.get("enabled"):
        return {"skipped": True, "reason": "推送未开启（config.yaml 里 push.enabled 为 false）"}

    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        return {"skipped": False, "ok": False, "error": "推送已开启但没填 webhook_url"}

    summary = build_summary(conn)
    if summary["total"] == 0:
        return {"skipped": True, "reason": "今天没有待办，不发空消息"}

    text = render_text(summary, int(cfg.get("max_chars", 1800) or 1800))
    result = send(text, url, cfg.get("channel", "dingtalk"), session=session)
    result["total"] = summary["total"]
    result["preview"] = text
    return result
