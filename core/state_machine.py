"""订单状态机 —— 全项目唯一的状态真源。

规则（需求 §2）：
1. 只能按顺序单步向前，不允许跳级；
2. 任意非终态可标记「异常」，异常独立记录；
3. 异常可恢复到标记前状态，或取消；
4. 已完成、已取消为终态。

页面只调 next_states() 拿按钮列表，不允许自己写 if status == ... 判断。
"""

from __future__ import annotations

STATES = ["已下单", "供应商已确认", "生产中", "已发货", "已到货", "已对账", "已完成"]
TERMINAL = {"已完成", "已取消"}
EXCEPTION = "异常"
CANCELLED = "已取消"
ALL_STATUSES = [*STATES, EXCEPTION, CANCELLED]

# 显式允许流转字典：key = 当前状态，value = 允许流转到的下一状态。
# 正常流每个状态只放一个后继，结构上就堵死"跳级"。
ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    "已下单": ["供应商已确认"],
    "供应商已确认": ["生产中"],
    "生产中": ["已发货"],
    "已发货": ["已到货"],
    "已到货": ["已对账"],
    "已对账": ["已完成"],
    "已完成": [],
    "已取消": [],
}

# 导入即自检：后人想给某个状态加第二个后继，会在这里当场炸掉
for _status, _targets in ALLOWED_TRANSITIONS.items():
    if len(_targets) > 1:
        raise RuntimeError(
            f"状态机配置错误：{_status} 允许了多个后继 {_targets}，违反「不允许跳级」约定"
        )
    for _target in _targets:
        if _target not in ALL_STATUSES:
            raise RuntimeError(f"状态机配置错误：{_status} 指向了未定义状态 {_target}")

# 异常不进正常流转链；已取消在字典里显式给空列表，走独立函数置入
assert EXCEPTION not in ALLOWED_TRANSITIONS
assert ALLOWED_TRANSITIONS[CANCELLED] == []


class StateError(RuntimeError):
    """非法状态流转。"""


def next_states(status: str) -> list[str]:
    """页面按钮组直接渲染这个列表。"""
    return list(ALLOWED_TRANSITIONS.get(status, []))


def can_transition(src: str, dst: str) -> bool:
    return dst in next_states(src)


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def is_active(status: str) -> bool:
    """进行中 = 非终态（含异常）。"""
    return not is_terminal(status)


def can_mark_exception(status: str) -> bool:
    return status not in TERMINAL and status != EXCEPTION


def can_cancel(status: str) -> bool:
    return status not in TERMINAL and status != EXCEPTION


def can_restore(status: str) -> bool:
    return status == EXCEPTION


def restore_target(order_row: dict) -> str:
    """异常恢复目标 = 标记前的状态。数据缺失时退回「已下单」并让调用方提示。"""
    target = (order_row or {}).get("status_before_exception")
    return target if target in STATES else "已下单"
