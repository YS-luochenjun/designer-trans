"""Return a lost Task Route quality direction to the original normal Work queue."""
from copy import deepcopy

from .convergence_recovery import _media_evidence
from .task_supervision import unsettled
from .tasks import Conflict
from .visual_recovery import _ancestors
from ..domain.execution import identity
from ..execution.clean_progress import progress
from ..execution.clean_rework import unreceived
from ..execution.clean_supervision import ready
from ..persistence.store import now
from ..workflow.graph import SUCCESS, task_status

KIND = "clean_rework_continuation"
ERROR = "无字底片尚未清理完成，禁止叠加译文；先处理残字并复核底片"


def schedule(service, task):
    store = service.store
    policy = store.require("route_policy", task["id"])
    node = next((n for n in task["nodes"] if n["id"] == policy["node_id"]
                 and n["definition_id"] == "clean" and n["status"] == "failed"
                 and n.get("error") == ERROR), None)
    if not node:
        return False
    if (task.get("control_paused") or task["status"] != "blocked"
            or not policy.get("enabled") or not policy.get("recover_production_route")
            or store.get("deletion_receipt", task["id"])
            or service.collaboration.pending(task["id"])):
        raise Conflict("Quality rework waits for task control and messages")
    if (unsettled(store, task, {n["id"] for n in task["nodes"]})
            or any(n["status"] not in SUCCESS for n in task["nodes"]
                   if n["id"] in _ancestors(task, node))):
        raise Conflict("Quality rework waits for original requests and prerequisites")
    sessions = [s for s in store.list("production_session")
                if s.get("task_id") == task["id"] and s.get("node_id") == node["id"]]
    if len(sessions) != 1:
        raise Conflict("Quality rework requires the unique original session")
    state = sessions[0]
    if state.get("finished") or state.get("clean_review") or state.get("candidate") or not ready(state, node):
        return False
    reports = unreceived(state)
    if not reports:
        return False
    for report in reports:
        event = store.get("task_route_event", report["id"]) or {}
        if (event.get("status") != "applied" or event.get("task_id") != task["id"]
                or event.get("scheduled_node_id") != node["id"]
                or event.get("plan", {}).get("action") != "rework"
                or event["plan"].get("instruction") != report["reason"]):
            raise Conflict("Original applied Route rework direction changed")
    if any(d.get("task_id") == task["id"] and d.get("node_id") == node["id"]
           and d.get("key") == f"clean-{state['turn']}" and d.get("status") == "succeeded"
           for d in store.list("production_decision")):
        raise Conflict("Original successful Work decision must be applied first")
    observe = next(n for n in task["nodes"] if n["definition_id"] == "observe"
                   and n["asset_id"] == node["asset_id"])
    texts = {t["id"]: deepcopy(t) for t in observe["output"]["texts"] if t["treatment"] == "translate"}
    for source_id, override in state.get("text_overrides", {}).items():
        texts[source_id].update(override)
    if progress(texts, state)["unresolved_instances"]:
        return False
    draft = state.get("draft_handoff") or {}
    if (draft.get("quality_passed") is not False or draft.get("completion_state") != "review_draft"
            or draft.get("unresolved")
            or draft.get("source_representation_sha256", draft.get("sha256")) != state["base"]["sha256"]):
        return False
    source_record = store.require("asset", node["asset_id"])
    source = service.checked_path(source_record)
    if service.hash_file(source) != source_record["sha256"]:
        raise Conflict("Original quality rework source changed")
    files = _media_evidence(service, task, state, source)
    key = identity({"task": task["id"], "session": state["id"], "turn": state["turn"],
                    "base": state["base"]["sha256"], "reports": sorted(r["id"] for r in reports)})
    if store.get(KIND, key):
        raise Conflict("This exact lost quality direction was already returned to Work")
    store.put(KIND, {
        "id": key, "task_id": task["id"], "node_id": node["id"], "status": "scheduled",
        "created_at": now(), "original_attempt": node["attempt"], "original_error": node["error"],
        "session_id": state["id"], "session_sha256": identity(state), "files": files,
        "report_ids": sorted(r["id"] for r in reports), "external_calls": 0,
        "quality_acceptance": False, "allowance_reset": False,
    })
    node.update(status="pending", error=None, finished_at=None, clean_rework_continuation_id=key)
    task.update(status=task_status(task), updated_at=now())
    store.put("task", task)
    return True
