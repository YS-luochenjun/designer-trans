"""Executable assignment scopes, separate from tool availability and media approval."""

LOCAL_REPAIRS = ("flat", "donor", "local_surface", "tracked_donor", "reference_plate")
PAID_REPAIRS = ("image_edit", "wan")


def assignment_permissions(controller, method):
    from .work_autonomy import enabled

    tools = controller.capabilities()
    if method in PAID_REPAIRS and enabled(controller.task) and method in tools:
        return {"execution_scope": "execute_and_review", "allowed_repair_methods": [
                    *[name for name in LOCAL_REPAIRS if name in tools], method],
                "generation_authorized": True, "local_only": False}
    return {
        "execution_scope": "execute_and_review",
        "allowed_repair_methods": [name for name in LOCAL_REPAIRS if name in tools],
        "generation_authorized": False, "local_only": True,
    }


def repair_obstacle(order, action):
    if not order or action["action"] != "repair":
        return None
    method = action.get("method")
    allowed = order.get("allowed_repair_methods")
    if (order.get("local_only") or order.get("generation_authorized") is False) and method in PAID_REPAIRS:
        return "当前Route工单包含未授权的收费生成；保留证据，由Route选择现有任务额度内的方法"
    if allowed is not None and method not in allowed:
        return "Route工单未分派此修复方法；返回方法需求，由Route选择现有能力，不提交外部请求"
    return None
