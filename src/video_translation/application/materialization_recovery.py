"""One local output recovery, retaining the failed representation and all work."""
import json
from pathlib import Path
from types import SimpleNamespace

from .agent_recovery import replaced_with_authorization
from .convergence_recovery import _media_evidence
from .tasks import Conflict
from .visual_recovery import _ancestors, _frozen
from ..domain.execution import identity
from ..execution.efficient_media import visual_for
from ..execution.media import MediaTools
from ..execution.review_draft import complete_candidate
from ..persistence.store import now
from ..workflow.graph import SUCCESS, task_status

ERROR = "Materialization did not preserve decoded pixels and audio packets"
KIND = "materialization_recovery"


def _binding(task, node):
    ancestors = _ancestors(task, node)
    return identity({
        "frozen": _frozen(task), "output_goal": task.get("output_goal"),
        "usage": {k: task.get(k) for k in (
            "paid_calls", "image_calls", "video_credits_committed", "delivery_count")},
        "instructions": task.get("collaboration_rules"),
        "prerequisites": [n for n in task["nodes"] if n["id"] in ancestors],
    })


def _checked(service, task, node, state):
    policy = service.store.require("route_policy", task["id"])
    if (not policy.get("enabled") or not policy.get("recover_production_route")
            or policy["node_id"] != node["id"] or not complete_candidate(task)
            or task.get("agent_engine") != "codex" or task.get("control_paused")
            or task["status"] in {"cancelled", "completed"}
            or service.store.get("deletion_receipt", task["id"])
            or service.collaboration.pending(task["id"])
            or state.get("candidate") or state.get("pending_turn") is not None
            or state.get("finished") or state.get("draft_handoff")
            or state.get("archive_review") or state.get("pipeline_review")
            or any(a.get("needs_scope_review") for a in state.get("accepted", []))):
        raise Conflict("Local output recovery requires unchanged delegated, settled viewing work")
    if (any(n["status"] in {"running", "waiting_external", "unknown"} or n.get("external_pending")
            for n in task["nodes"] if n["id"] != node["id"])
            or node.get("external_pending")
            or any(n["status"] not in SUCCESS for n in task["nodes"]
                   if n["id"] in _ancestors(task, node))):
        raise Conflict("Other production or prerequisites require reconciliation")
    for kind in ("operation", "production_decision", "imagegen_request", "route_run", "route_probe"):
        if any(r.get("task_id") == task["id"] and r.get("status") in {
                "running", "unknown", "invalid_response", "pending", "claimed", "verifying"}
               and not replaced_with_authorization(service.store, r, kind)
               for r in service.store.list(kind)):
            raise Conflict("Unsettled original requests take priority over local output")
    source_record = service.store.require("asset", node["asset_id"])
    source = service.checked_path(source_record)
    if node["asset_id"] not in task["asset_ids"] or service.hash_file(source) != source_record["sha256"]:
        raise Conflict("Original source changed")
    files = _media_evidence(service, task, state, source)
    files[str(source)] = source_record["sha256"]
    return policy, files


def _audio_packets(media, path):
    data = json.loads(media.media._run([
        media.media.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
        "-select_streams", "a", "-show_packets", "-show_data_hash", "sha256",
        "-show_entries", "packet=stream_index,pts,dts,duration,size,data_hash",
        "-of", "json", path,
    ], capture=True))
    packets = data.get("packets", [])
    if not packets or any(not p.get("data_hash", "").startswith("SHA256:") for p in packets):
        raise Conflict("Original audio packet evidence is missing")
    return packets


def _failure_evidence(service, task, state):
    media = visual_for(SimpleNamespace(
        service=service, store=service.store,
        media=MediaTools(service.settings.ffmpeg, service.settings.ffprobe)), task)
    root, info, _ = media._chain(state["base"]["path"])
    if not media.is_manifest(state["base"]["path"]) or not info["has_audio"]:
        raise Conflict("Not a sparse representation with original audio")
    target = media.workspace.root / "checkpoints" / (state["base"]["sha256"] + ".mp4")
    if (target.is_symlink() or not target.is_file()
            or target.with_suffix(".mp4.representation.json").exists()):
        raise Conflict("A retained representation needs its own result reconciliation")
    for p in (target, *target.parents):
        if p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 1024:
            raise Conflict("Linked output evidence is not supported")
    failed_sha = service.hash_file(target)
    _, output_info = media._info(target)
    if (any(info[k] != output_info[k] for k in ("width", "height", "rate", "frames", "has_audio"))
            or media._decoded_sha(state["base"]["path"]) != media._decoded_sha(target)):
        raise Conflict("Failed output does not preserve the source picture")
    original, copied = _audio_packets(media, root), _audio_packets(media, target)
    # Only a strict retained prefix proves this specific video-duration cutoff.
    if not (len(copied) < len(original) and copied == original[:len(copied)]):
        raise Conflict("Failed audio is not a strict original packet prefix")
    if service.hash_file(target) != failed_sha:
        raise Conflict("Failed output changed during diagnosis")
    return {"path": str(target), "sha256": failed_sha, "source_packets": len(original),
            "retained_packets": len(copied), "picture_preserved": True,
            "failure": "audio_tail_truncated", "quality_passed": False}


def schedule(service, task):
    """Manager-owned scheduling; no model, generation, media write or turn reset."""
    node = next((n for n in task["nodes"] if n["definition_id"] == "clean"
                 and n["status"] == "failed" and n.get("error") == ERROR), None)
    if not node:
        return False
    sessions = [s for s in service.store.list("production_session")
                if s.get("task_id") == task["id"] and s.get("node_id") == node["id"]]
    if (task["status"] != "blocked" or len(sessions) != 1 or node.get("materialization_recovery_id")
            or any(r.get("task_id") == task["id"] and r.get("node_id") == node["id"]
                   for r in service.store.list(KIND))):
        raise Conflict("Local output recovery was consumed or stopped work changed")
    state = sessions[0]
    policy, files = _checked(service, task, node, state)
    failure = _failure_evidence(service, task, state)
    if _checked(service, task, node, state)[1] != files:
        raise Conflict("Media changed during local diagnosis")
    from ..execution.resource_budget import guard_storage
    guard_storage(service, task)
    key = identity({"task": task["id"], "session": state["id"], "failure": failure})
    target = Path(failure["path"]).with_name(state["base"]["sha256"] + "-audio-tail-v1.mp4")
    if target.exists() or target.with_suffix(".mp4.representation.json").exists():
        raise Conflict("Replacement representation already exists; reconcile its receipt")
    service.store.put(KIND, {
        "id": key, "task_id": task["id"], "node_id": node["id"], "status": "scheduled",
        "created_at": now(), "original_attempt": node["attempt"], "original_error": node["error"],
        "session_id": state["id"], "session_sha256": identity(state), "files": files,
        "task_binding_sha256": _binding(task, node), "policy_sha256": identity(policy),
        "failure": failure, "target": str(target), "quality_passed": False,
        "scope": "one_local_viewing_output", "external_calls": 0, "allowance_reset": False,
    })
    node.update(status="pending", error=None, finished_at=None, materialization_recovery_id=key)
    task.update(status=task_status(task), updated_at=now())
    service.store.put("task", task)
    return True


def resume_handoff(controller, state):
    """The normal Worker alone renders the same accepted base to a new artifact."""
    key = controller.node.get("materialization_recovery_id")
    if not key:
        return None
    store, service = controller.store, controller.service
    task = store.require("task", controller.task["id"])
    if task.get("control_paused"):
        return {"_paused": True}
    node = next(n for n in task["nodes"] if n["id"] == controller.node["id"])
    record = store.require(KIND, key)
    if (record["status"] == "succeeded" and record["task_id"] == task["id"]
            and record["node_id"] == node["id"] and record["session_id"] == state["id"]
            and node["attempt"] > record["original_attempt"] + 1):
        # A later cleanup attempt owns new work, not the consumed diagnostic export.
        return None
    policy, files = _checked(service, task, node, state)
    failure = record["failure"]
    target = Path(record["target"])
    if (record["status"] != "scheduled" or record["task_id"] != task["id"]
            or record["node_id"] != node["id"] or node["status"] != "running"
            or node["attempt"] != record["original_attempt"] + 1
            or record["session_id"] != state["id"] or identity(state) != record["session_sha256"]
            or identity(store.require("production_session", state["id"])) != record["session_sha256"]
            or _binding(task, node) != record["task_binding_sha256"]
            or identity(policy) != record["policy_sha256"] or files != record["files"]
            or service.hash_file(Path(failure["path"])) != failure["sha256"]
            or Path(failure["path"]).with_suffix(".mp4.representation.json").exists()
            or target != Path(failure["path"]).with_name(
                state["base"]["sha256"] + "-audio-tail-v1.mp4")
            or target.exists() or target.with_suffix(".mp4.representation.json").exists()):
        raise Conflict("Local output intent was consumed or its bound evidence changed")
    store.update(KIND, key, lambda r: r.update(status="running", started_at=now()))
    try:
        from ..execution.clean_progress import progress
        from ..execution.review_draft import handoff
        result = handoff(controller, state, progress(controller.texts, state),
                         representation_target=target)
    except Exception as exc:
        error = str(exc)[:1500]
        store.update(KIND, key, lambda r: r.update(
            status="failed", error=error, finished_at=now()))
        raise
    store.update(KIND, key, lambda r: r.update(
        status="succeeded", output_sha256=result["sha256"], finished_at=now()))
    return result


def completed_representation(controller, state):
    """Reuse the registered output of this exact base; materialize verifies its receipt."""
    key = controller.node.get("materialization_recovery_id")
    record = controller.store.get(KIND, key) if key else None
    if not record or record["status"] != "succeeded":
        return None
    if (record["task_id"] != controller.task["id"]
            or record["node_id"] != controller.node["id"] or record["session_id"] != state["id"]):
        raise Conflict("Completed representation belongs to another production session")
    base = state["base"]
    if record["files"].get(str(Path(base["path"]))) != base["sha256"]:
        return None
    target = controller.media.workspace.confined(record["target"])
    expected = controller.media.workspace.root / "checkpoints" / (
        base["sha256"] + "-audio-tail-v1.mp4")
    if (target != expected or not target.is_file()
            or controller.service.hash_file(target) != record.get("output_sha256")):
        raise Conflict("Completed representation changed; reconcile its original receipt")
    return target
