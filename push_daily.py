#!/usr/bin/env python3
"""每日推送入口，交给系统定时任务调用。

    python push_daily.py

退出码：
    0  推送成功，或按配置跳过（未开启 / 没有待办）
    1  推送失败（缺 webhook、网络错误、机器人拒绝）

定时配置见 README「每日推送怎么定时」。本脚本不自带调度，也不监听任何端口。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db, push  # noqa: E402


def main() -> int:
    db.init_db()
    result = push.push_once()

    if result.get("skipped"):
        print(f"[跳过] {result.get('reason')}")
        return 0

    if result.get("ok"):
        print(f"[成功] 已推送 {result.get('total')} 条待办摘要，{result.get('chars')} 字")
        return 0

    print(f"[失败] {result.get('error')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
