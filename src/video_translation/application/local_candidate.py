"""Register an observed local-only repair as a candidate, never an acceptance."""
from types import SimpleNamespace
import json

from video_translation.domain.production import CleanAction
from video_translation.execution.adaptive import AdaptiveProduction, digest
from video_translation.execution.media import MediaTools
from .tasks import Conflict


def create_local_candidate(service, task_id, action, *, expected_base_sha256=None):
    """Caller must hold the worker guard (HTTP) or exclusive worker lock (CLI)."""
    action = CleanAction.model_validate(action).model_dump()
    if action["action"] != "repair" or action["method"] not in {"local_surface", "tracked_donor"}:
        raise ValueError("This entry accepts only local_surface/tracked_donor; no provider dispatch")
    task = service.store.require("task", task_id)
    if task["status"] not in {"blocked", "waiting_user"}:
        raise Conflict("Local repair requires a stopped task")
    if task.get("control_paused") and task.get("production_policy", {}).get("mode") != "stage_verified":
        raise Conflict("Restore stage quality gates before staging a paused local candidate")
    if any(n["status"] in {"running", "unknown", "waiting_external"} or n.get("external_pending")
           for n in task["nodes"]):
        raise Conflict("Reconcile active or unknown node results first")
    sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
    if len(sessions) != 1:
        raise Conflict("Select a task with one unambiguous clean session")
    state = sessions[0]
    node = next(n for n in task["nodes"] if n["id"] == state["node_id"])
    if node["definition_id"] != "clean" or node["status"] != "failed":
        raise Conflict("Local candidate requires the stopped clean stage")
    if state.get("candidate") or state.get("pending_turn") is not None or node.get("external_pending"):
        raise Conflict("Reconcile or review the existing candidate/action first")
    if expected_base_sha256 is not None and state["base"]["sha256"] != expected_base_sha256:
        raise Conflict("Base changed; inspect the current lossless plate before rendering")
    if any(d.get("task_id") == task_id and d.get("node_id") == node["id"]
           and d.get("key") == f"clean-{state['turn']}"
           and d.get("status") in {"running", "unknown", "succeeded"}
           for d in service.store.list("production_decision")):
        raise Conflict("Reconcile the current Agent decision before a local repair")

    def output(_task, _node, name):
        prior = next(n for n in task["nodes"] if n["asset_id"] == node["asset_id"]
                     and n["definition_id"] == name)
        if prior["status"] not in {"succeeded", "skipped"}:
            raise Conflict(f"Local prerequisite is not ready: {name}")
        return prior["output"]

    media = MediaTools(service.settings.ffmpeg, service.settings.ffprobe)
    local = SimpleNamespace(service=service, store=service.store, media=media, output=output,
                            kie_for=lambda task: None, image_ready=lambda task: False)
    controller = AdaptiveProduction(local, task, node)
    for source_id, override in state.get("text_overrides", {}).items():
        controller.texts[source_id].update(override)
    identity = digest({"task": task_id, "base": state["base"], "action": action, "version": 1})
    previous = service.store.get("local_candidate", identity)
    if previous:
        controller.verified(previous["candidate"])
        raise Conflict("This local recipe already ran on this base; inspect its retained candidate")
    controller.directory = service.settings.runtime / "tasks" / task_id / "local-candidates" / identity
    controller.directory.mkdir(parents=True, exist_ok=True)
    before = digest(state)
    task_before = digest(task)
    candidate = controller.repair(action, controller.verified(state["base"]), state["turn"])
    receipt = {"id": identity, "task_id": task_id, "candidate": candidate, "action": action,
               "quality_passed": False, "paid_requests": 0}
    service.store.put("local_candidate", receipt)
    state["candidate"] = candidate
    state.setdefault("candidate_archive", []).append(dict(candidate))
    state["times_ms"] = controller.candidate_times(candidate)
    state.setdefault("local_repairs", []).append({"receipt_id": identity, "action": action})
    # Compare and attach in one transaction; retain the unbound receipt on conflict.
    with service.store.connection() as db:
        for kind, record_id, expected in (
            ("task", task_id, task_before), ("production_session", state["id"], before),
        ):
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
            if not row or digest(json.loads(row[0])) != expected:
                raise Conflict("Production changed; retained local candidate was not attached")
        db.execute("UPDATE records SET data=? WHERE kind='production_session' AND id=?",
                   (json.dumps(state, ensure_ascii=False), state["id"]))
    return receipt
