"""A successful inspect can recover its context without replacing its model result."""
import json
from pathlib import Path

from video_translation.domain.execution import identity
from video_translation.persistence.store import now
from .tasks import Conflict


ERROR = "已停止重复观察：底片、范围和时间点均未变化；保留原观察记录"


def context(service, task):
    node = next((n for n in task["nodes"] if n["definition_id"] == "clean"
                 and n["status"] == "failed" and n.get("error") == ERROR), None)
    if not node or task.get("control_paused") or task["status"] in {"cancelled", "completed"}:
        raise Conflict("Not a stopped observation-context incident")
    state = next((s for s in service.store.list("production_session")
                  if s.get("task_id") == task["id"] and s["node_id"] == node["id"]), None)
    if (not state or state.get("candidate") or state.get("pending_turn") is not None
            or node.get("external_pending")):
        raise Conflict("Candidate or pending work needs original-result reconciliation")
    history = state["history"][-1]
    action = history["decision"]
    if (action["action"] != "inspect" or history["turn"] + 1 != state["turn"]
            or action.get("source_id") is None):
        raise Conflict("Latest successful action is not a bound source inspection")
    from .agent_recovery import replaced_with_authorization
    for kind in ("operation", "production_decision", "imagegen_request"):
        for row in service.store.list(kind):
            if (row.get("task_id") == task["id"] and row.get("status") in {
                    "running", "unknown", "invalid_response", "pending", "claimed"}
                    and not replaced_with_authorization(service.store, row, kind)):
                raise Conflict("Unsettled external result takes priority over context recovery")
    decisions = [d for d in service.store.list("production_decision")
                 if d.get("task_id") == task["id"] and d.get("node_id") == node["id"]
                 and d.get("key") == f"clean-{history['turn']}" and d["status"] == "succeeded"]
    if len(decisions) != 1 or decisions[0]["result"] != action:
        raise Conflict("Original inspect result is missing or ambiguous")
    base = Path(state["base"]["path"])
    source = service.checked_path(service.store.require("asset", node["asset_id"]))
    source_sha = service.hash_file(source)
    if source_sha != service.store.require("asset", node["asset_id"])["sha256"]:
        raise Conflict("Original source changed")
    root = (service.settings.runtime / "tasks" / task["id"]).resolve()
    if base.is_symlink() or not base.resolve().is_relative_to(root) or service.hash_file(base) != state["base"]["sha256"]:
        raise Conflict("Observation base changed")
    files = {}
    matching = None
    for artifact in service.store.list("artifact"):
        if (artifact.get("task_id") != task["id"] or artifact.get("node_id") != node["id"]
                or not artifact.get("name", "").endswith("work-packet.json")):
            continue
        path = Path(artifact["path"])
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > 2 * 1024**2:
            continue
        if service.hash_file(path) != artifact["sha256"]:
            raise Conflict("Original work packet changed")
        packet = json.loads(path.read_bytes())
        inputs = packet.get("inputs", {})
        if (inputs.get("baseline_sha256") != state["base"]["sha256"]
                or set(inputs.get("observed_times_ms", [])) != set(action["times_ms"])
                or inputs.get("candidate") or inputs.get("time_basis") != "source_absolute"):
            continue
        images = packet.get("images", [])
        if not images:
            continue
        files = {str(path): artifact["sha256"]}
        for image in images:
            image_path = Path(image["path"])
            if image_path.is_symlink() or not image_path.resolve().is_relative_to(root):
                raise Conflict("Historical frame escaped task scope")
            files[str(image_path)] = service.hash_file(image_path)
        matching = artifact["id"]
        break
    if not matching:
        raise Conflict("Requested historical window is not retained; do not blindly retry")
    window_id = identity({"base": state["base"]["sha256"], "source": source_sha,
        "times_ms": sorted(set(action["times_ms"])), "scope": state.get("text_overrides", {})})
    if set(state["times_ms"]) == set(action["times_ms"]):
        raise Conflict("Requested window is already visible; this is not context loss")
    evidence = {"base_sha256": state["base"]["sha256"], "session_id": state["id"], "window_id": window_id,
        "turn": state["turn"], "decision_id": decisions[0]["id"], "action": action,
        "files": files, "packet_id": matching, "source_sha256": source_sha,
        "scope": state.get("text_overrides", {})}
    signature = identity(evidence)
    if window_id in state.get("observation_context_recoveries", {}):
        raise Conflict("This context recovery was already consumed")
    return {"kind": "observation_context", "node_id": node["id"], "turn": state["turn"],
        "expected": {"context_sha256": signature, "expected_base_sha256": state["base"]["sha256"]},
        "evidence": evidence, "receipt": {"failure_kind": "retained_observation_context"},
        "diagnostics": {"receipt_integrity": {"passed": True, "original_result_succeeded": True}}}


def apply(service, task, fresh, run):
    state = service.store.require("production_session", fresh["evidence"]["session_id"])
    node = next(n for n in task["nodes"] if n["id"] == fresh["node_id"])
    # Restore the requested observation window. The Worker performs extraction and
    # submits the next normal decision; no historical decision is replaced.
    state["times_ms"] = list(fresh["evidence"]["action"]["times_ms"])
    state.setdefault("observation_context_recoveries", {})[fresh["evidence"]["window_id"]] = {
        "route_run_id": run["id"], "delegation_id": run["authorization_id"],
        "original_decision_id": fresh["evidence"]["decision_id"], "at": now()}
    node.update(status="pending", error=None, finished_at=None)
    task.update(status="queued", error=None)
    service.store.put("production_session", state)
    service.store.put("task", task)
    return {"restored_times_ms": state["times_ms"], "original_result_preserved": True,
            "scheduled_worker": True, "quality_passed": False}
