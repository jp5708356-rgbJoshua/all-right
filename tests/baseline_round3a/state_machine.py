"""订单状态规则的唯一来源；异常是独立标记，不是正常状态。"""
from dataclasses import dataclass
from datetime import datetime, timezone

ORDERED = "已下单"
CONFIRMED = "供应商已确认"
PRODUCING = "生产中"
SHIPPED = "已发货"
ARRIVED = "已到货"
RECONCILED = "已对账"
COMPLETED = "已完成"

allowed_transitions = {
    ORDERED: (CONFIRMED,),
    CONFIRMED: (PRODUCING,),
    PRODUCING: (SHIPPED,),
    SHIPPED: (ARRIVED,),
    ARRIVED: (RECONCILED,),
    RECONCILED: (COMPLETED,),
    COMPLETED: (),
}
NORMAL_STATUSES = tuple(allowed_transitions)
ACTIVE_STATUSES = NORMAL_STATUSES[:-1]
PRE_ARRIVAL_STATUSES = (ORDERED, CONFIRMED, PRODUCING, SHIPPED)


@dataclass(frozen=True)
class OrderAction:
    action: str
    label: str
    target_status: str = None
    needs_reason: bool = False


def validate_order_action(current_status, action, target_status=None,
                          is_exception=False, status_before_exception=None,
                          exception_reason=None) -> tuple[bool, str]:
    """唯一合法性入口。所有拒绝均返回 (False, 可读原因)。"""
    if action == "initialize":
        if current_status is not None or target_status != ORDERED or is_exception:
            return False, "新订单只能初始化为已下单，且不能处于异常"
        return True, ""
    if not isinstance(current_status, str) or current_status not in allowed_transitions:
        return False, "当前状态非法，不能执行操作"
    if action not in ("advance", "mark_exception", "restore"):
        return False, "未知订单动作"
    if current_status == COMPLETED:
        return False, "已完成为终态，不可流转或标记异常"
    if is_exception not in (0, 1, False, True):
        return False, "异常标记非法"
    if action == "restore":
        if not is_exception:
            return False, "当前订单未处于异常，异常来源不合法"
        if not status_before_exception:
            return False, "异常来源为空，不能恢复"
        if not isinstance(status_before_exception, str) or status_before_exception not in allowed_transitions:
            return False, "异常来源状态非法，不能恢复"
        if status_before_exception == COMPLETED:
            return False, "异常来源为已完成，不能恢复终态"
        if status_before_exception != current_status:
            return False, "异常来源与当前状态不一致，不能恢复"
        return True, ""
    if is_exception:
        return False, "订单已标记异常，请先恢复；不能推进或重复标记异常"
    if action == "mark_exception":
        if not isinstance(exception_reason, str) or not exception_reason.strip():
            return False, "标记异常必须填写原因"
        return True, ""
    if not isinstance(target_status, str) or target_status not in allowed_transitions:
        return False, "目标状态非法"
    if target_status not in allowed_transitions[current_status]:
        return False, "只允许顺序前进一步，禁止跳级、倒退或原地流转"
    return True, ""


def get_available_actions(current_status, is_exception, status_before_exception=None) -> list[OrderAction]:
    if not isinstance(current_status, str):
        return []
    candidates = [OrderAction("advance", f"推进至：{target}", target)
                  for target in allowed_transitions.get(current_status, ())]
    candidates += [OrderAction("mark_exception", "标记异常", needs_reason=True),
                   OrderAction("restore", "恢复原状态")]
    return [candidate for candidate in candidates if validate_order_action(
        current_status, candidate.action, candidate.target_status, is_exception,
        status_before_exception, "待用户填写，提交时重新校验")[0]]


def initial_fields():
    ok, reason = validate_order_action(None, "initialize", ORDERED)
    if not ok:
        raise ValueError(reason)
    return {"status": ORDERED, "is_exception": 0, "exception_reason": None,
            "status_before_exception": None, "exception_marked_at": None}


def action_fields(order, action, target_status=None, exception_reason=None):
    ok, reason = validate_order_action(order["status"], action, target_status,
                                       order["is_exception"], order["status_before_exception"],
                                       exception_reason)
    if not ok:
        raise ValueError(reason)
    if action == "advance":
        return {"status": target_status}
    if action == "mark_exception":
        return {"is_exception": 1, "exception_reason": exception_reason.strip(),
                "status_before_exception": order["status"],
                "exception_marked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return {"status": order["status_before_exception"], "is_exception": 0,
            "exception_reason": None, "status_before_exception": None,
            "exception_marked_at": None}
