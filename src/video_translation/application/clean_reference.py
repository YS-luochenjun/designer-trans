"""Admit an explicitly supplied built-in imagegen reference, without a recipe."""
from copy import deepcopy
from pathlib import Path

from PIL import Image

from video_translation.application.tasks import Conflict
from video_translation.execution.clean_progress import fingerprint, progress
from video_translation.execution.frame_clock import request_time
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status


def stopped_clean(service, task_id, expected_base_sha256, tool_request_id=None):
    from video_translation.execution.adaptive import AdaptiveProduction
    from video_translation.execution.nodes import NodeExecutor
    task = service.store.require("task", task_id)
    sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
    # An unresolved downstream result cannot mutate this clean base. It stays
    # frozen, but must not prevent the original upstream tool request advancing.
    downstream = set()
    if tool_request_id and len(sessions) == 1:
        downstream.add(sessions[0]["node_id"])
        for item in task["nodes"]:
            if downstream.intersection(item["depends_on"]):
                downstream.add(item["id"])
        downstream.discard(sessions[0]["node_id"])
    if (task["status"] not in ({"running"} if tool_request_id else {"blocked"})
            or task.get("control_paused") or len(sessions) != 1
            or any((n["status"] in {"running", "unknown", "waiting_external"}
                    and not (tool_request_id and n.get("tool_request_id") == tool_request_id
                             and n["status"] == "waiting_external")
                    and not (n["id"] in downstream and n["status"] == "unknown"))
                   or n.get("external_pending")
                   for n in task["nodes"])):
        raise Conflict("Requires a stopped, unpaused task without in-flight work")
    state = sessions[0]
    node = next(n for n in task["nodes"] if n["id"] == state["node_id"])
    if (node["definition_id"] != "clean"
            or node["status"] != ("waiting_external" if tool_request_id else "failed")
            or state.get("candidate") or state.get("pending_turn") is not None
            or state["base"]["sha256"] != expected_base_sha256):
        raise Conflict("Reconcile the clean state and current base first")
    if any(d.get("node_id") == node["id"] and d.get("key") == f"clean-{state['turn']}"
           and d["status"] in {"running", "unknown", "succeeded"}
           for d in service.store.list("production_decision")):
        raise Conflict("Reconcile the current decision first")
    controller = AdaptiveProduction(NodeExecutor(service), task, node)
    controller.verified(state["base"])
    for source_id, override in state.get("text_overrides", {}).items():
        controller.texts[source_id].update(override)
    return task, state, node, controller


def register_reference(service, task_id, source_id, expected_base_sha256,
                       frame_index, original_frame_path, generated_image_path, request_record=None):
    task, state, node, controller = stopped_clean(
        service, task_id, expected_base_sha256, (request_record or {}).get("id"))
    original_task, original_state = deepcopy(task), deepcopy(state)
    remaining = {r["source_id"] for r in progress(controller.texts, state)["remaining"]}
    if source_id not in remaining or (not request_record and source_id not in state.get("deferred", {})):
        raise Conflict("Select an unresolved deferred source")
    root = (service.settings.runtime / "tasks" / task_id).resolve()
    original, generated = Path(original_frame_path), Path(generated_image_path)
    for path in (original, generated):
        if (path.is_symlink() or not path.resolve().is_relative_to(root)
                or not path.is_file() or not 0 < path.stat().st_size <= 20 * 1024 * 1024):
            raise Conflict("Reference files must be intact task-owned images")
    _, clock = controller.media._info(controller.source)
    if not 0 <= frame_index < clock["frames"]:
        raise Conflict("Reference frame outside source")
    time_ms = request_time(frame_index, clock["rate"])
    item = controller.texts[source_id]
    if not item["start_ms"] <= time_ms < item["end_ms"]:
        raise Conflict("Reference frame outside selected source interval")
    decoded = controller.frames(controller.source, [time_ms], "原片身份核验", "reference-admission")[0]
    if decoded["frame_index"] != frame_index or service.hash_file(Path(decoded["path"])) != service.hash_file(original):
        raise Conflict("Original reference does not match redecoded source frame")
    with Image.open(generated) as image:
        if image.format != "PNG" or image.width * image.height > 16_777_216:
            raise Conflict("Generated reference must be a bounded PNG")
        if abs(image.width / image.height / (clock["width"] / clock["height"]) - 1) > .01:
            raise Conflict("Generated reference must preserve full-frame aspect ratio")
        image.load()
    identity = {"task_id": task_id, "source_id": source_id, "base_sha256": expected_base_sha256,
                "frame_index": frame_index, "sha256": service.hash_file(generated)}
    key = fingerprint(identity)
    if service.store.get("clean_reference", key):
        raise Conflict("This reference has already been admitted; do not reset its attempt")
    record = {**identity, "id": key, "node_id": node["id"], "path": str(generated.resolve()),
              "original_sha256": service.hash_file(original), "time_ms": time_ms,
              "source_sha256": service.hash_file(controller.source), "at": now(),
              "provider": "codex_builtin_imagegen" if request_record else "user_supplied",
              "quality_passed": False,
              "meaning": "Generated reference still; work chooses geometry and reviews actual video"}
    obstacle = state.get("deferred", {}).pop(source_id, {"reason": "Work requested generated reference"})
    state.setdefault("deferred_history", []).append({
        "source_id": source_id, "obstacle": obstacle, "reference_id": key, "turn": state["turn"]})
    state.pop("draft_handoff", None)
    state.pop("imagegen_request_id", None)
    state["times_ms"] = [time_ms]
    state["route_work_order"] = {
        "source_id": source_id, "method": "reference_plate", "local_only": True,
        "generation_authorized": False, "reference_id": key,
        **({"route_review": state["route_work_order"]["route_review"]}
           if (state.get("route_work_order") or {}).get("route_review") else {}),
        "question": "Inspect this generated reference against source/current frames. Independently select "
                    "a suitable short local edit or record its limitations. Preserve motion and meaningful "
                    "content; minor texture need not be perfect. A clean still is not video acceptance."}
    node.update(status="pending", error=None, finished_at=None, tool_request_id=None, output={})
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if tx.require("task", task_id) != original_task or tx.require("production_session", state["id"]) != original_state:
            raise Conflict("Task changed during reference admission")
        if request_record:
            if tx.require("imagegen_request", request_record["id"]) != request_record:
                raise Conflict("Imagegen request changed during admission")
            tx.put("imagegen_request", {**request_record, "status": "succeeded",
                                       "reference_id": key, "finished_at": now()})
        tx.put("clean_reference", record)
        tx.put("production_session", state)
        tx.put("task", task)
    return {"task_id": task_id, "reference_id": key, "status": task["status"],
            "accepted_unchanged": True, "new_provider_calls": 0}
