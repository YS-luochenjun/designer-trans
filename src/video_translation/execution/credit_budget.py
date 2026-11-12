"""Check cumulative KIE spending inside the operation reservation transaction."""
import json
import math
from decimal import Decimal

from video_translation.application.tasks import Conflict
from video_translation.domain.limits import effective_limit


def _credits(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return Decimal(str(value))


def kie_budget_snapshot(db, task):
    settled_total = Decimal(0)
    pending_total = Decimal(0)
    unknown = []
    for row in db.execute("SELECT data FROM records WHERE kind='operation'"):
        operation = json.loads(row[0])
        if operation.get("task_id") != task["id"] or operation.get("provider") != "kie":
            continue
        actual = _credits(operation.get("actual_cost"))
        estimated = _credits(operation.get("estimated_cost"))
        settled = operation.get("status") == "succeeded" or operation.get("journal_state") == "fail"
        if settled and actual is not None:
            settled_total += actual
        elif estimated is not None:
            pending_total += max(estimated, actual or Decimal(0))
        else:
            unknown.append(operation["id"])
    total = settled_total + pending_total
    limit = effective_limit(task, "max_kie_credits", None)
    return {
        "total_credit_limit": limit,
        "settled_credits": float(settled_total),
        "reserved_credits": float(pending_total),
        "committed_credits": float(total),
        "remaining_credits": None if unknown or limit is None else float(max(Decimal(0), Decimal(str(limit)) - total)),
        "unknown_cost_operation_ids": unknown,
        "accounting_complete": not unknown,
    }


def check_kie_budget(db, task, estimate):
    limit = effective_limit(task, "max_kie_credits", None)
    if limit is None:
        return
    reserved = _credits(estimate)
    if reserved is None:
        raise Conflict("KIE累计预算已启用；新请求费用无法可靠预估，停止提交")
    snapshot = kie_budget_snapshot(db, task)
    if not snapshot["accounting_complete"]:
        raise Conflict("KIE历史请求费用尚未结清且缺少预留金额；仅可查询原请求")
    total = Decimal(str(snapshot["committed_credits"]))
    if total + reserved > Decimal(str(limit)):
        raise Conflict(f"KIE累计预算不足：已计入{total}，本次需预留{reserved}，上限{limit} credits；未提交新请求")
