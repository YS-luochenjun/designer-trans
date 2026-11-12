"""Explicit, audited extensions of existing task supervision limits."""
from copy import deepcopy

from .tasks import Conflict
from ..domain.execution import ResourceBudget
from ..persistence.store import identifier, now
from ..persistence.transaction import TransactionStore


def increase(service, task_id, expected_recovery_limit, new_recovery_limit,
             expected_revision_limit, new_revision_limit, reason):
    if (not 0 <= expected_recovery_limit <= new_recovery_limit <= 1000
            or not 0 <= expected_revision_limit <= new_revision_limit <= 3
            or (expected_recovery_limit, expected_revision_limit)
            == (new_recovery_limit, new_revision_limit) or not reason.strip()):
        raise Conflict("A bounded increase and explicit reason are required")
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        task = store.require("task", task_id)
        budget = task.get("execution_snapshot", {}).get("resource_budget")
        if (not task.get("control_paused") or task["status"] in {"cancelled", "completed"}
                or store.get("deletion_receipt", task_id) or not budget
                or budget["max_recovery_calls"] != expected_recovery_limit
                or task["max_revisions"] != expected_revision_limit
                or any(n["status"] in {"running", "waiting_external"} or n.get("external_pending")
                       for n in task["nodes"])):
            raise Conflict("Increase requires the current paused task and unchanged limits")
        active = {"running", "pending", "claimed", "verifying", "queued"}
        if any(row.get("task_id") == task_id and row.get("status") in active
               for kind in ("operation", "production_decision", "imagegen_request", "route_run",
                            "route_probe", "conversation")
               for row in store.list(kind)):
            raise Conflict("Original in-flight results and user messages take priority")
        updated = ResourceBudget.model_validate(
            {**budget, "max_recovery_calls": new_recovery_limit}).model_dump()
        count = sum(row.get("task_id") == task_id
                    for kind in ("route_run", "route_probe") for row in store.list(kind))
        count += sum(row.get("task_id") == task_id and bool(row.get("started_at"))
                     for row in store.list("task_route_event"))
        change = {
            "id": identifier(), "task_id": task_id, "at": now(), "reason": reason,
            "authority": "explicit_user", "previous_resource_budget": deepcopy(budget),
            "previous_recovery_limit": expected_recovery_limit, "new_recovery_limit": new_recovery_limit,
            "previous_revision_limit": expected_revision_limit, "new_revision_limit": new_revision_limit,
            "usage_at_change": {"paid_calls": task.get("paid_calls", 0),
                                "recovery_calls": count, "revisions": deepcopy(task["revisions"])},
            "usage_reset": False, "media_authorized": False, "production_resumed": False,
        }
        task["execution_snapshot"]["resource_budget"] = updated
        task["resource_budget"] = deepcopy(updated)
        task.update(max_revisions=new_revision_limit, updated_at=now())
        store.put("supervision_budget_change", change)
        store.put("task", task)
        return {"task_id": task_id, "change": change, "paused": True}
