"""Durable handoff to a Codex host with the built-in imagegen tool.

Claimed requests are not automatically reissued after an uncertain host failure.
"""
from pathlib import Path

from video_translation.execution.clean_progress import fingerprint
from video_translation.persistence.store import now
from video_translation.integrations.agents.errors import CapabilityUnavailable
from video_translation.workflow.graph import task_status

# Identifies the actual corrected tool wiring, not a task retry counter.
NATIVE_TOOL_PROFILE = "codex-native-actor-catalog-v1"


def requests(controller):
    return [r for r in controller.store.list("imagegen_request")
            if r["task_id"] == controller.task["id"] and r["node_id"] == controller.node["id"]]


def can_request(controller, source_id=None):
    policy = controller.task.get("review_draft_authorization") or {}
    if not policy.get("imagegen_bridge") or policy.get("image_provider") != "codex_builtin_imagegen":
        return False
    records = requests(controller)
    task_records = [r for r in controller.store.list("imagegen_request")
                    if r["task_id"] == controller.task["id"]]
    if any(r.get("failure_kind") == "native_tool_unavailable"
           and r.get("native_tool_profile") == NATIVE_TOOL_PROFILE for r in task_records):
        return False
    from .work_autonomy import enabled
    if enabled(controller.task):
        return not any(r["status"] in {"pending", "claimed", "unknown"} for r in records)
    return (len(task_records) < policy.get("max_imagegen_requests", 0)
            and not any(r["status"] in {"pending", "claimed", "unknown"} for r in records)
            and not any(r["source_id"] == source_id for r in records))


def capability_context(controller):
    """Bind capability claims to the actual wiring and original execution receipts."""
    records = requests(controller) if controller.task.get("review_draft_authorization") else []
    return {
        "native_tool_profile": NATIVE_TOOL_PROFILE,
        "request_allowed": can_request(controller),
        "execution_owner": "project_codex",
        "meaning": "Configured request path; not proof of image generation or video quality",
        "failure_receipts": [
            {"request_id": r["id"], "native_tool_profile": r.get("native_tool_profile"),
             "failure_kind": r.get("failure_kind"),
             "applies_to_current_profile": r.get("native_tool_profile") == NATIVE_TOOL_PROFILE}
            for r in records if r["status"] == "failed"],
        "outstanding_request_ids": sorted(r["id"] for r in records
                                          if r["status"] in {"pending", "claimed", "unknown"}),
    }


def enqueue(controller, state, action, frames):
    source_id = action["source_id"]
    if not can_request(controller, source_id) or state.get("candidate"):
        raise CapabilityUnavailable("No authorized unused built-in imagegen request for this source")
    item = controller.texts.get(source_id)
    time_ms = action["times_ms"][0]
    observed = next((f for f in frames if f["time_ms"] == time_ms and "原片" in f["label"]), None)
    if not item or not item["start_ms"] <= time_ms < item["end_ms"] or not observed:
        raise ValueError("Imagegen must select an actually supplied source frame inside the text interval")
    image = controller.frames(controller.source, [time_ms], "原片", "imagegen-input")[0]
    identity = {"task_id": controller.task["id"], "node_id": controller.node["id"],
                "source_id": source_id, "base_sha256": state["base"]["sha256"],
                "frame_index": image["frame_index"]}
    from .work_autonomy import enabled
    if enabled(controller.task):
        identity["request_sequence"] = len(requests(controller))
    record = {**identity, "id": fingerprint(identity), "status": "pending", "created_at": now(),
              "time_ms": image["time_ms"], "original_frame_path": image["path"],
              "original_sha256": controller.service.hash_file(Path(image["path"])),
              "source_sha256": controller.service.hash_file(controller.source),
              "prompt": action["prompt"], "evidence": action["evidence"],
              "native_tool_profile": NATIVE_TOOL_PROFILE,
              "provider": "codex_builtin_imagegen", "quality_passed": False}
    state["turn"] += 1
    state["imagegen_request_id"] = record["id"]
    with controller.store.connection() as db:
        from video_translation.persistence.transaction import TransactionStore
        tx = TransactionStore(controller.store, db)
        if tx.get("imagegen_request", record["id"]):
            raise CapabilityUnavailable("This request already exists; reconcile it without replay")
        current = tx.require("task", controller.task["id"])
        if current.get("control_paused") or current["status"] == "cancelled":
            raise CapabilityUnavailable("Task paused or cancelled before imagegen handoff")
        node = next(n for n in current["nodes"] if n["id"] == controller.node["id"])
        node.update(status="waiting_external", tool_request_id=record["id"],
                    external_pending=False, next_poll_at=None, error=None)
        current.update(status=task_status(current), updated_at=now())
        tx.put("imagegen_request", record)
        tx.put("production_session", state)
        tx.put("task", current)
    return {"_tool_wait": record["id"], "provider": record["provider"]}
