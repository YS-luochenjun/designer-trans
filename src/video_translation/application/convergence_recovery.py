"""Guarded handoff of already-stopped work to the finite production Route."""
from copy import deepcopy
from pathlib import Path

from ..domain.execution import ExecutionProfile, PatchManifest, identity
from ..execution.clean_progress import progress
from ..execution.convergence import ERROR, assignment_handoff
from ..persistence.store import now
from .agent_recovery import replaced_with_authorization
from .tasks import Conflict


def _media_evidence(service, task, state, source):
    root = (service.settings.runtime / "tasks" / task["id"]).resolve()
    files, manifests = {}, set()

    def checked(record):
        path = Path(record["path"])
        for cursor in (path, *path.parents):
            if cursor.is_symlink() or getattr(cursor.lstat(), "st_file_attributes", 0) & 1024:
                raise Conflict("Linked convergence evidence is not supported")
        path = path.resolve()
        if path != source and not path.is_relative_to(root):
            raise Conflict("Convergence media escaped the task")
        if not path.is_file() or service.hash_file(path) != record["sha256"]:
            raise Conflict("Convergence media changed")
        files[str(path)] = record["sha256"]
        if not str(path).endswith(".vpatch.json"):
            return
        if path in manifests or len(manifests) >= 256 or path.stat().st_size > 2 * 1024**2:
            raise Conflict("Invalid convergence patch dependency chain")
        manifests.add(path)
        manifest = PatchManifest.model_validate_json(path.read_bytes())
        checked(manifest.base.model_dump())
        for patch in manifest.patches:
            checked(patch.image.model_dump())

    checked(state["base"])
    return files


def context(service, task):
    node = next((n for n in task["nodes"] if n["definition_id"] == "clean"
                 and n["status"] == "failed" and n.get("error") == ERROR), None)
    if (not node or task.get("control_paused") or task["status"] in {"cancelled", "completed"}
            or service.store.get("deletion_receipt", task["id"])):
        raise Conflict("Not an active stopped convergence incident")
    sessions = [s for s in service.store.list("production_session")
                if s.get("task_id") == task["id"] and s["node_id"] == node["id"]]
    if len(sessions) != 1 or node.get("external_pending"):
        raise Conflict("A unique stopped clean session with no pending external work is required")
    state = sessions[0]
    incident_id = (state.get("routing_stop") or {}).get("incident_id")
    incident = state.get("execution_incidents", {}).get(incident_id)
    profile = ExecutionProfile.model_validate(task.get("execution_snapshot", {}))
    binding = state.get("execution_binding", {})
    order = state.get("route_work_order") or {}
    expected_incident = identity({**binding, "assignment": {
        k: order.get(k) for k in ("source_id", "method")}})
    checkpoint = state.get("execution_checkpoints", {}).get(incident_id, {})
    if (not incident or incident.get("kind") != "no_base_progress"
            or incident_id != expected_incident or state.get("finished")
            or incident.get("node_id") != node["id"] or incident.get("last_turn") != state["turn"]
            or incident.get("actions", 0) != state["turn"] - incident["first_turn"]
            or incident["actions"] < profile.max_unchanged_actions
            or checkpoint.get("start_turn") != incident["first_turn"]
            or checkpoint.get("actions") != incident["actions"]
            or incident.get("base_sha256") != state["base"]["sha256"]
            or incident_id in state.get("convergence_recoveries", {})):
        raise Conflict("Convergence incident changed or its handoff was already consumed")
    for kind in ("operation", "production_decision", "imagegen_request"):
        for row in service.store.list(kind):
            if (row.get("task_id") == task["id"] and row.get("status") in {
                    "running", "unknown", "invalid_response", "pending", "claimed"}
                    and not replaced_with_authorization(service.store, row, kind)):
                raise Conflict("Unsettled external results take priority over convergence handoff")
    observe = next((n for n in task["nodes"] if n["definition_id"] == "observe"
                    and n["asset_id"] == node["asset_id"]), None)
    if observe is None:
        raise Conflict("Original observation inventory is missing")
    texts = {r["id"]: deepcopy(r) for r in observe.get("output", {}).get("texts", [])
             if r.get("treatment") == "translate"}
    for source_id, override in state.get("text_overrides", {}).items():
        texts[source_id].update(override)
    if (not texts or identity(texts) != incident["scope_sha256"]
            or any(state.get("execution_binding", {}).get(k) != incident.get(k)
                   for k in ("base_sha256", "scope_sha256", "capability_sha256"))):
        raise Conflict("Convergence scope or capability binding changed")
    summary = progress(texts, state)
    handoff = assignment_handoff(state, summary, incident)
    if not handoff:
        raise Conflict("No safe exhausted assignment can be returned to the finite clean Route")
    decisions = [r for r in service.store.list("production_decision")
                 if r.get("task_id") == task["id"] and r.get("node_id") == node["id"]]
    if any(d.get("key") == f"clean-{state['turn']}" for d in decisions):
        raise Conflict("The stopped turn already has a result; reconcile it before handoff")
    originals = []
    for history in state["history"]:
        if not incident["first_turn"] <= history["turn"] < state["turn"]:
            continue
        matches = [d for d in decisions if d.get("key") == f"clean-{history['turn']}"
                   and d.get("status") == "succeeded" and d.get("result") == history["decision"]]
        if len(matches) != 1:
            raise Conflict("Applied Work result is missing or ambiguous")
        originals.append({"id": matches[0]["id"], "result_sha256": identity(matches[0])})
    if not originals:
        raise Conflict("No original applied Work evidence exists")
    asset = service.store.require("asset", node["asset_id"])
    source = service.checked_path(asset)
    source_sha = service.hash_file(source)
    if source_sha != asset["sha256"]:
        raise Conflict("Original source changed")
    files = _media_evidence(service, task, state, source)
    reference_binding = None
    if handoff.get("reference_id"):
        reference = service.store.get("clean_reference", handoff["reference_id"])
        if (not reference or reference.get("task_id") != task["id"]
                or reference.get("node_id") != node["id"]
                or reference.get("source_id") != handoff["source_id"]):
            raise Conflict("Original returned reference binding is missing")
        files.update(_media_evidence(service, task, {"base": reference}, source))
        reference_binding = identity(reference)
    evidence = {
        "session_id": state["id"], "session_sha256": identity(state),
        "incident": incident, "handoff": handoff, "original_results": originals,
        "files": files, "source_sha256": source_sha, "reference_binding_sha256": reference_binding,
        "task_binding_sha256": identity({k: task.get(k) for k in (
            "model_snapshot", "resource_snapshot", "execution_snapshot", "production_policy",
            "max_kie_credits", "video_credits_committed", "max_paid_calls", "max_image_calls")}),
    }
    return {
        "kind": "convergence", "node_id": node["id"], "turn": state["turn"],
        "expected": {"context_sha256": identity(evidence),
                     "expected_base_sha256": state["base"]["sha256"]},
        "evidence": evidence, "receipt": {"failure_kind": "bounded_assignment_exhausted"},
        "diagnostics": {"receipt_integrity": {
            "passed": True, "original_results_succeeded": True, "media_dependencies_verified": True,
            "meaning": "Unresolved handoff only; no replay, quality approval or new media authority"}},
    }


def apply(service, task, fresh, run):
    state = service.store.require("production_session", fresh["evidence"]["session_id"])
    incident_id = fresh["evidence"]["incident"]["id"]
    state.setdefault("convergence_recoveries", {})[incident_id] = {
        "route_run_id": run["id"], "delegation_id": run["authorization_id"], "at": now(),
        "context_sha256": fresh["expected"]["context_sha256"],
        "meaning": "Return the unchanged stopped assignment to the finite clean Route",
    }
    node = next(n for n in task["nodes"] if n["id"] == fresh["node_id"])
    node.update(status="pending", error=None, finished_at=None)
    task.update(status="queued", error=None)
    service.store.put("production_session", state)
    service.store.put("task", task)
    return {"incident_id": incident_id, "scheduled_worker": True, "original_results_preserved": True,
            "allowance_reset": False, "quality_passed": False}
