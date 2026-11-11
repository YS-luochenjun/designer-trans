"""Apply a retained work-agent acceptance conflict without replaying that agent."""
import json
from copy import deepcopy
from pathlib import Path

from PIL import Image, ImageChops

from video_translation.execution.acceptance_disputes import dispute_acceptance
from video_translation.execution.clean_progress import progress
from video_translation.execution.frame_clock import sample_index
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status
from .tasks import Conflict


def audit_clean_acceptance(service, task_id, decision_id, expected_base_sha256):
    """Caller holds worker guard. No supplied verdict, coordinates or candidate."""
    from video_translation.execution.adaptive import AdaptiveProduction
    from video_translation.execution.nodes import NodeExecutor

    task = service.store.require("task", task_id)
    sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
    if len(sessions) != 1:
        raise Conflict("Acceptance audit requires one retained clean session")
    state = sessions[0]
    original_task, original_state = deepcopy(task), deepcopy(state)
    if task["status"] != "blocked" or task.get("control_paused"):
        raise Conflict("Acceptance audit requires a blocked, unpaused task")
    node = next(n for n in task["nodes"] if n["id"] == state["node_id"])
    if node["definition_id"] != "clean" or node["status"] != "failed":
        raise Conflict("Acceptance audit requires a stopped clean node")
    if any(n["status"] in {"running", "unknown", "waiting_external"} or n.get("external_pending")
           for n in task["nodes"]):
        raise Conflict("Reconcile active or unknown node results first")
    if state.get("candidate") or state.get("pending_turn") is not None:
        raise Conflict("Review the existing candidate/action first")
    if state["base"]["sha256"] != expected_base_sha256:
        raise Conflict("Base changed; inspect current evidence")
    decisions = service.store.list("production_decision")
    if any(d.get("node_id") == node["id"] and d.get("key") == f"clean-{state['turn']}"
           and d["status"] in {"running", "unknown", "succeeded"} for d in decisions):
        raise Conflict("Reconcile the current Agent decision first")
    decision = next((d for d in decisions if d["id"] == decision_id), None)
    turn = state["turn"] - 1
    if (not decision or decision.get("task_id") != task_id or decision.get("node_id") != node["id"]
            or decision["status"] != "succeeded" or decision.get("key") != f"clean-{turn}"):
        raise Conflict("Audit requires the last applied successful clean decision")
    action = decision["result"]
    if not any(h["turn"] == turn and h["decision"] == action for h in state["history"]):
        raise Conflict("Saved decision does not match applied session history")
    traces = [t for t in service.store.list("agent_trace")
              if t.get("task_id") == task_id and t.get("node_id") == node["id"]
              and t.get("decision_id") == decision_id[:20] and t["status"] == "succeeded"
              and t.get("result") == action]
    if len(traces) != 1:
        raise Conflict("Audit requires one matching successful Agent trace")
    packet_path = (service.settings.runtime / "tasks" / task_id / "nodes" / node["id"]
                   / str(traces[0]["attempt"]) / f"{decision_id[:20]}-work-packet.json").resolve()
    artifacts = [a for a in service.store.list("artifact")
                 if a.get("task_id") == task_id and a.get("node_id") == node["id"]
                 and Path(a["path"]).resolve() == packet_path]
    if len(artifacts) != 1:
        raise Conflict("Audit requires the registered original work packet")
    controller = AdaptiveProduction(NodeExecutor(service), task, node)
    controller.verified(state["base"])
    controller.verified(artifacts[0])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    inputs = packet["inputs"]
    archive = next((a for a in state.get("candidate_archive", [])
                    if a["id"] == action.get("candidate_id")), None)
    if (not archive or packet.get("task_id") != task_id
            or packet.get("node_run_id") != f"{node['id']}:{traces[0]['attempt']}"
            or packet.get("decision_id") != decision_id[:20]
            or inputs.get("archive_under_review") != archive["id"]
            or inputs.get("baseline_sha256") != expected_base_sha256
            or (inputs.get("archive_diagnostics") or {}).get("sha256") != archive["sha256"]):
        raise Conflict("Original packet archive or current plate identity mismatch")
    video = controller.verified(archive)
    _, clock = controller.media._info(video)
    frames = [f for f in packet["images"] if "历史候选" in f.get("label", "")]
    if not frames:
        raise Conflict("Original work packet lacks historical-candidate images")
    # Re-decode to prove the supplied images belong to the disputed archive.
    decoded = controller.media.capture(video, controller.directory / f"audit-{decision_id[:20]}",
                                      [f["time_ms"] for f in frames])
    for supplied, fresh in zip(frames, decoded, strict=True):
        path = Path(supplied["path"]).resolve()
        if (not path.is_relative_to(packet_path.parent) or not path.is_file()
                or supplied["frame_index"] != fresh["frame_index"]
                or supplied["frame_index"] != sample_index(supplied["time_ms"], clock["rate"], clock["frames"])):
            raise Conflict("Retained archive image path or frame identity mismatch")
        with Image.open(path) as old, Image.open(fresh["path"]) as new:
            if old.size != new.size or ImageChops.difference(old.convert("RGB"), new.convert("RGB")).getbbox():
                raise Conflict("Retained review image differs from the verified archive")
    try:
        changed = dispute_acceptance(state, archive, action, {f["frame_index"] for f in frames},
                                     clock, turn, decision["key"])
    except ValueError as exc:
        raise Conflict(str(exc)) from exc
    if not changed:
        raise Conflict("This acceptance conflict is already recorded")
    for source_id, override in state.get("text_overrides", {}).items():
        controller.texts[source_id].update(override)
    state["progress"] = progress(controller.texts, state)
    receipt = {"decision_id": decision_id, "trace_id": traces[0]["id"],
               "packet_sha256": artifacts[0]["sha256"], "base_sha256": expected_base_sha256,
               "at": now(), "original_verdict_retained": True, "recipe_supplied": False}
    state["acceptance_disputes"][-1]["audit_receipt"] = receipt
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as connection:
        transaction = TransactionStore(service.store, connection)
        if (transaction.require("task", task_id) != original_task
                or transaction.require("production_session", state["id"]) != original_state
                or transaction.require("production_decision", decision_id) != decision
                or transaction.require("agent_trace", traces[0]["id"]) != traces[0]
                or transaction.require("artifact", artifacts[0]["id"]) != artifacts[0]):
            raise Conflict("Audit inputs changed; nothing was applied")
        transaction.put("production_session", state)
        transaction.put("task", task)
    return {"task_id": task_id, "status": task["status"], "audit": receipt,
            "scope_review_scheduled": True}
