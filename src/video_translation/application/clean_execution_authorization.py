"""One explicit execution window, preserving cumulative work and original media."""
from copy import copy, deepcopy

from .agent_recovery import replaced_with_authorization
from .collaboration import node_for
from .convergence_recovery import _media_evidence
from .task_supervision import report_work
from .tasks import Conflict
from ..domain.execution import ResourceBudget, identity
from ..execution.review_draft import complete_candidate
from ..execution.work_conclusions import capacity
from ..persistence.store import now
from ..persistence.transaction import TransactionStore

KIND = "clean_execution_authorization"


def authorize(service, task_id, request):
    key = identity({"task_id": task_id, "request_id": str(request.request_id)})
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        previous = store.get(KIND, key)
        if previous:
            return previous
        task = store.require("task", task_id)
        node = node_for(task, request.node_id)
        state = store.get("production_session", request.session_id)
        budget = task.get("execution_snapshot", {}).get("resource_budget")
        if (not complete_candidate(task) or not task.get("control_paused")
                or task["status"] in {"completed", "cancelled"} or store.get("deletion_receipt", task_id)
                or not node or node["definition_id"] != "clean" or node["status"] != "failed"
                or node["attempt"] != request.expected_attempt
                or task.get("brief_revision", 0) != request.expected_revision
                or not budget or budget["max_clean_turns"] != request.expected_clean_limit):
            raise Conflict("A new execution requires the unchanged paused, unfinished cleanup")
        if (not state or state.get("task_id") != task_id or state.get("node_id") != node["id"]
                or state["turn"] != request.expected_turn
                or state["base"]["sha256"] != request.expected_base_sha256
                or state.get("finished") or state.get("candidate") or state.get("pending_turn") is not None
                or state.get("archive_review") or state.get("pipeline_review")
                or any(a.get("needs_scope_review") for a in state.get("accepted", []))
                or not 1 <= request.additional_turns <= 48 or not request.reason.strip()):
            raise Conflict("Original cleanup changed or still has a pending candidate or verdict")
        if any(n["status"] in {"running", "waiting_external", "unknown"} or n.get("external_pending")
               for n in task["nodes"]):
            raise Conflict("Wait for original Work before authorizing another execution")
        for kind in ("operation", "production_decision", "imagegen_request", "route_run", "route_probe",
                     "conversation", "task_route_decision", "task_route_event"):
            for row in store.list(kind):
                if row.get("task_id") != task_id:
                    continue
                if (row.get("status") in {"running", "pending", "queued", "claimed", "verifying",
                                         "planned", "waiting_work", "unknown", "invalid_response"}
                        and not replaced_with_authorization(store, row, kind)):
                    raise Conflict("Original requests, Route plans and user messages take priority")
                if (kind == "production_decision" and row.get("node_id") == node["id"]
                        and row.get("key") == f"clean-{state['turn']}" and row["status"] == "succeeded"):
                    raise Conflict("Apply the current original decision before another execution")
        if any(r.get("task_id") == task_id and r.get("node_id") == node["id"]
               and r["status"] == "authorized" for r in store.list(KIND)):
            raise Conflict("An explicit execution is already awaiting Route")
        report = capacity(task)
        if (report["remaining_external_calls"] is not None
                and report["remaining_external_calls"] <= report["downstream_model_call_reserve"] + 1):
            raise Conflict("Insufficient original call allowance for new Work and downstream output")
        if any(task["revisions"].get(node["asset_id"] + ":" + locale, 0) >= task["max_revisions"]
               for locale in task["target_locales"]):
            raise Conflict("The existing rework allowance is exhausted")
        asset = store.require("asset", node["asset_id"])
        try:
            source = service.checked_path(asset)
            if service.hash_file(source) != asset["sha256"]:
                raise Conflict("Original source changed")
            local = copy(service)
            local.store = store
            files = _media_evidence(local, task, state, source)
        except (OSError, ValueError, KeyError) as exc:
            raise Conflict("Original media evidence is missing or changed") from exc
        limit = max(state["turn"], request.expected_clean_limit) + request.additional_turns
        updated = ResourceBudget.model_validate({**budget, "max_clean_turns": limit}).model_dump()
        record = {"id": key, "task_id": task_id, "node_id": node["id"], "session_id": state["id"],
                  "status": "authorized", "created_at": now(), "authority": "explicit_user",
                  "reason": request.reason, "expected_attempt": node["attempt"],
                  "session_sha256": identity(state), "base_sha256": state["base"]["sha256"],
                  "previous_clean_limit": request.expected_clean_limit, "new_clean_limit": limit,
                  "additional_turns": request.additional_turns, "start_turn": state["turn"],
                  "paid_calls_at_authorization": task.get("paid_calls", 0), "media_files": files,
                  "usage_reset": False, "production_resumed": False, "quality_acceptance": False}
        task["execution_snapshot"]["resource_budget"] = updated
        task.update(resource_budget=deepcopy(updated), updated_at=now())
        store.put("task", task)
        store.put(KIND, record)
        event = report_work(local, task_id, node["id"], trigger="user_execution_authorized")
        event["execution_authorization_id"] = key
        store.put("task_route_event", event)
        return record


def begin(service, state, event):
    """Route selected new Work; retire only transient controls with an audit trail."""
    key = event.get("execution_authorization_id")
    if not key:
        return
    record = service.store.require(KIND, key)
    if (record["status"] != "authorized" or record["session_sha256"] != identity(state)
            or record["session_id"] != state["id"] or record["base_sha256"] != state["base"]["sha256"]):
        raise Conflict("The authorized execution or its original session changed")
    from ..execution.clean_execution_facts import facts
    initial_evidence = facts(service, service.store.require("task", event["task_id"]), state)
    fields = ("draft_handoff", "routing_stop", "completion_feedback_stop", "completion_review_due",
              "route_work_order", "deferred", "work_feedback", "work_feedback_cursor")
    original_sha256 = identity(state)
    state.setdefault("execution_restart_archive", []).append({
        "authorization_id": key, "state_sha256": original_sha256,
        "controls": {k: deepcopy(state[k]) for k in fields if k in state}, "at": now()})
    for name in fields:
        state.pop(name, None)
    if state.get("history"):
        state["work_feedback_cursor"] = state["history"][-1]["turn"]
    state["new_execution"] = {k: record[k] for k in (
        "id", "start_turn", "new_clean_limit", "reason")}
    state["new_execution"]["initial_evidence"] = initial_evidence
    service.store.update(KIND, key, lambda r: r.update(
        status="applied", route_event_id=event["id"], applied_at=now()))
