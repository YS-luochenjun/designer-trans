"""Explicit, single-dispatch video-model trial without replacing the task snapshot."""
from copy import deepcopy

from video_translation.application.tasks import Conflict
from video_translation.execution.clean_progress import fingerprint, progress
from video_translation.execution.fidelity import wan_exclusions
from video_translation.integrations.providers.model_catalog import resolve_selection
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status


def trial_task(task):
    trial = task.get("clean_video_trial")
    if not trial:
        return task
    return {**task, "model_snapshot": trial["model_snapshot"]}


def reserve_trial(current, supplied, identity, key, estimate):
    """Run inside the existing remote reservation transaction, for new requests only."""
    trial = current.get("clean_video_trial")
    if not trial or identity.get("clean_video_trial") != trial["id"]:
        raise Conflict("Video trial authorization is missing or changed")
    if (identity.get("source_id") != trial["source_id"]
            or identity.get("model") != "wan/3-0-video"
            or supplied["model_snapshot"] != trial["model_snapshot"]
            or estimate is None or estimate > trial["max_credits"]):
        raise Conflict("Video trial exceeds its source, model or credit boundary")
    if trial.get("operation_key") and trial["operation_key"] != key:
        raise Conflict("This video trial already dispatched once; review its retained result")
    trial["operation_key"] = key


def enable_video_trial(service, task_id, source_id, expected_base_sha256):
    from video_translation.execution.adaptive import AdaptiveProduction
    from video_translation.execution.nodes import NodeExecutor
    from video_translation.execution.credit_budget import check_kie_budget

    task = service.store.require("task", task_id)
    sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
    if len(sessions) != 1:
        raise Conflict("Video trial requires one clean session")
    state = sessions[0]
    before_task, before_state = deepcopy(task), deepcopy(state)
    node = next(n for n in task["nodes"] if n["id"] == state["node_id"])
    if (task["status"] != "blocked" or task.get("control_paused")
            or node["definition_id"] != "clean" or node["status"] != "failed"
            or any(n["status"] in {"running", "unknown", "waiting_external"}
                   or n.get("external_pending") for n in task["nodes"])
            or state.get("candidate") or state.get("pending_turn") is not None):
        raise Conflict("Video trial requires a stopped, unpaused clean task with no pending result")
    if state["base"]["sha256"] != expected_base_sha256:
        raise Conflict("Base changed; inspect current media")
    if any(d.get("node_id") == node["id"] and d.get("key") == f"clean-{state['turn']}"
           and d["status"] in {"running", "unknown", "succeeded"}
           for d in service.store.list("production_decision")):
        raise Conflict("Reconcile the current Agent decision first")
    if source_id in wan_exclusions(state):
        raise Conflict("Structural-damage exclusions remain; select a different source")
    controller = AdaptiveProduction(NodeExecutor(service), task, node)
    controller.verified(state["base"])
    for sid, override in state.get("text_overrides", {}).items():
        controller.texts[sid].update(override)
    remaining = {r["source_id"]: r["intervals_ms"]
                 for r in progress(controller.texts, state)["remaining"]}
    existing = task.get("clean_video_trial")
    if existing:
        from video_translation.execution.clean_progress import inspection_key, reuse_trial_inspection
        last = state.get("history", [])[-1] if state.get("history") else {}
        action = last.get("decision", {})
        matches = [d for d in service.store.list("production_decision")
                   if d.get("task_id") == task_id and d.get("node_id") == node["id"]
                   and d.get("key") == f"clean-{last.get('turn')}" and d["status"] == "succeeded"
                   and d.get("result") == action]
        if (existing["source_id"] != source_id or existing.get("operation_key")
                or existing["base_sha256"] != expected_base_sha256
                or last.get("turn") != state["turn"] - 1
                or action.get("action") != "inspect" or action.get("source_id") != source_id
                or last.get("tool_error") != "相同底片和范围已检查；需要新证据或具体修复方法"
                or len(matches) != 1 or source_id not in remaining
                or source_id in state.get("deferred", {})):
            raise Conflict("This task already has a retained video trial; do not reset its allowance")
        times = action["times_ms"]
        if not times or any(t < 0 or t >= controller.duration for t in times):
            raise Conflict("Retained inspection timestamps are invalid")
        key = inspection_key(expected_base_sha256, source_id, times, state.get("text_overrides", {}))
        if key not in state.get("inspection_requests", {}) or not reuse_trial_inspection(
                state, existing, source_id, key):
            raise Conflict("This trial already reused the retained inspection")
        state["times_ms"] = times
        state.setdefault("resume_events", []).append({
            "at": now(), "trial_id": existing["id"], "retained_inspection_turn": last["turn"],
            "reason": "Reuse the saved successful inspection decision; no agent replay or allowance reset"})
        trial = existing
    else:
        trial = None
    if trial is None and (source_id not in remaining or source_id not in state.get("deferred", {})):
        raise Conflict("Select one unresolved deferred source")
    selected = resolve_selection(service.settings.root)
    if (selected["models"]["video"]["model"] != "wan/3-0-video"
            or selected["models"]["video"]["options"]["resolution"] != "720p"):
        raise Conflict("Configure the supported Wan3 720p selection first")
    snapshot = deepcopy(task["model_snapshot"])
    snapshot["models"]["video"] = selected["models"]["video"]
    snapshot["sha256"] = fingerprint({k: v for k, v in snapshot.items() if k != "sha256"})
    trial = trial or {"id": fingerprint({"task": task_id, "base": expected_base_sha256,
                                "source": source_id, "snapshot": snapshot["sha256"]}),
             "source_id": source_id, "base_sha256": expected_base_sha256,
             "node_id": node["id"], "model_snapshot": snapshot,
             "max_credits": 64, "max_dispatches": 1, "at": now(),
             "reason": "User authorized one Wan3 short-segment evaluation; work chooses geometry",
             "recipe_supplied": False}
    task["clean_video_trial"] = trial
    if not existing:
        obstacle = state["deferred"].pop(source_id)
        state.setdefault("deferred_history", []).append({
            "source_id": source_id, "obstacle": obstacle, "video_trial_id": trial["id"],
            "turn": state["turn"], "reason": trial["reason"]})
        lo, hi = remaining[source_id][0]
        state["times_ms"] = sorted({lo, (lo + hi) // 2, hi - 1})
    state["progress"] = progress(controller.texts, state)
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if (tx.require("task", task_id) != before_task
                or tx.require("production_session", state["id"]) != before_state):
            raise Conflict("Task changed; trial was not enabled")
        check_kie_budget(db, before_task, 64)
        tx.put("task", task)
        tx.put("production_session", state)
    return {"task_id": task_id, "trial": trial, "accepted_unchanged": True,
            "retained_inspection_resumed": bool(existing),
            "original_model_snapshot_unchanged": True, "recipe_supplied": False}
