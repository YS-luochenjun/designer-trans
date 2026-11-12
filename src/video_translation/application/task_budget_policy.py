"""Explicit task-local testing policy; original limits remain as reference values."""
from copy import copy
from types import SimpleNamespace

from .tasks import Conflict
from ..domain.execution import identity
from ..domain.limits import budget_policy
from ..persistence.store import now
from ..persistence.transaction import TransactionStore

KIND = "task_budget_policy_change"


def context(service, task_id):
    task = service.store.require("task", task_id)
    basis = {k: task.get(k) for k in (
        "id", "status", "control_paused", "updated_at", "execution_snapshot",
        "paid_calls", "image_calls", "revisions", "max_revisions",
        "max_paid_calls", "max_kie_credits", "max_video_credits", "max_image_calls", "max_visual_steps")}
    return {"task_id": task_id, "basis_sha256": identity(basis),
            "budget_policy": budget_policy(task),
            "reference_limits": {
                **{k: task.get(k) for k in ("max_paid_calls", "max_kie_credits", "max_video_credits",
                                          "max_image_calls", "max_visual_steps", "max_revisions")},
                **(task.get("execution_snapshot", {}).get("resource_budget") or {})}}


def change(service, task_id, request):
    key = identity({"task_id": task_id, "request_id": request.request_id})
    request_sha = identity(vars(request) if isinstance(request, SimpleNamespace) else request.model_dump())
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        previous = store.get(KIND, key)
        if previous:
            if previous["request_sha256"] != request_sha:
                raise Conflict("The original testing-policy request has different content")
            return previous
        local = copy(service)
        local.store = store
        task = store.require("task", task_id)
        if (not task.get("control_paused") or task["status"] in {"cancelled", "completed"}
                or store.get("deletion_receipt", task_id)
                or context(local, task_id)["basis_sha256"] != request.expected_basis
                or not request.reason.strip()):
            raise Conflict("Testing policy requires the unchanged paused, unfinished task")
        if any(n["status"] in {"running", "waiting_external"} or n.get("external_pending")
               for n in task["nodes"]):
            raise Conflict("Wait for the original Work before changing its budget policy")
        if any(row.get("task_id") == task_id and row.get("status") in {
                "running", "pending", "queued", "claimed", "verifying", "planned", "waiting_work"}
               for kind in ("operation", "production_decision", "imagegen_request", "route_run",
                            "route_probe", "conversation", "task_route_decision", "task_route_event")
               for row in store.list(kind)):
            raise Conflict("Original requests, user messages and Route plans take priority")
        for state in store.list("production_session"):
            if state.get("task_id") != task_id:
                continue
            if any(d.get("task_id") == task_id and d.get("node_id") == state["node_id"]
                   and d.get("key") == f"clean-{state['turn']}" and d.get("status") == "succeeded"
                   for d in store.list("production_decision")):
                raise Conflict("Apply the retained current decision before changing its request context")
        mode = "testing_unlimited" if request.enabled else "bounded"
        record = {"id": key, "task_id": task_id, "created_at": now(),
                  "request_sha256": request_sha, "authority": "explicit_user",
                  "reason": request.reason, "previous_policy": budget_policy(task), "mode": mode,
                  "reference_limits": context(local, task_id)["reference_limits"],
                  "usage_at_change": {k: task.get(k) for k in ("paid_calls", "image_calls", "revisions")},
                  "usage_reset": False, "production_resumed": False,
                  "quality_acceptance": False, "media_authorization_unchanged": True}
        task["execution_snapshot"]["budget_mode"] = mode
        task["updated_at"] = now()
        store.put("task", task)
        if request.enabled:
            from .clean_execution_authorization import authorize
            clean = next((n for n in task["nodes"] if n["definition_id"] == "clean"
                          and n["status"] == "failed"), None)
            states = [s for s in store.list("production_session")
                      if clean and s.get("task_id") == task_id and s.get("node_id") == clean["id"]]
            if len(states) == 1:
                state = states[0]
                budget = task["execution_snapshot"].get("resource_budget") or {}
                if budget:
                    try:
                        receipt = authorize(local, task_id, SimpleNamespace(
                            request_id=key, node_id=clean["id"], session_id=state["id"],
                            expected_attempt=clean["attempt"],
                            expected_revision=task.get("brief_revision", 0),
                            expected_turn=state["turn"], expected_base_sha256=state["base"]["sha256"],
                            expected_clean_limit=budget["max_clean_turns"], additional_turns=0,
                            reason=request.reason))
                        record["continuation_authorization_id"] = receipt["id"]
                        record["continuation_status"] = "awaiting_route"
                    except Conflict as exc:
                        record["continuation_status"] = "original_work_has_priority"
                        record["continuation_reason"] = str(exc)
        store.put(KIND, record)
        return record
