"""Explicit task call-limit increases with an append-only change record."""
from copy import deepcopy

from .tasks import Conflict
from ..domain.execution import ResourceBudget
from ..persistence.store import identifier, now
from ..persistence.transaction import TransactionStore


def increase(service, task_id, expected_limit, new_limit, reason):
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        task = store.require("task", task_id)
        budget = task.get("execution_snapshot", {}).get("resource_budget")
        if (not task.get("control_paused") or task["status"] in {"cancelled", "completed"}
                or store.get("deletion_receipt", task_id)
                or any(n["status"] in {"running", "waiting_external"} or n.get("external_pending")
                       for n in task["nodes"]) or not budget
                or budget["max_external_calls"] != expected_limit or new_limit <= expected_limit):
            raise Conflict("Increase requires the current paused task and unchanged existing limit")
        if any(r.get("task_id") == task_id and r.get("status") in {"running", "claimed"}
               for kind in ("operation", "route_run", "route_probe", "imagegen_request")
               for r in store.list(kind)):
            raise Conflict("Wait for the original in-flight operation before changing the limit")
        updated = ResourceBudget.model_validate({**budget, "max_external_calls": new_limit}).model_dump()
        change = {
            "id": identifier(), "task_id": task_id, "at": now(), "reason": reason,
            "authority": "explicit_user", "previous_limit": expected_limit, "new_limit": new_limit,
            "usage_at_change": task.get("paid_calls", 0), "previous_resource_budget": deepcopy(budget),
            "previous_max_paid_calls": task.get("max_paid_calls"),
            "usage_reset": False, "media_authorized": False,
        }
        task["execution_snapshot"]["resource_budget"] = updated
        task["resource_budget"] = deepcopy(updated)
        if task.get("max_paid_calls") is not None and task["max_paid_calls"] < new_limit:
            task["max_paid_calls"] = new_limit
        task["updated_at"] = now()
        store.put("resource_budget_change", change)
        store.put("task", task)
        return {"task_id": task_id, "change": change, "paused": True}
