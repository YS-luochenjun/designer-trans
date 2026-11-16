"""Task-local, evidence-bound conclusions reused without another model call."""
from copy import deepcopy

from .clean_progress import fingerprint


def retain(state, action, *, turn, base_sha256, frames, role="work", tool_error=None):
    value = {
        "role": role, "turn": turn, "source_id": action.get("source_id"),
        "input_base_sha256": base_sha256, "action": action["action"],
        "candidate_id": action.get("candidate_id"),
        "review_sha256": action.get("review_sha256"),
        "action_sha256": fingerprint(action),
        "reason": action.get("reason", "")[:600],
        "findings": [text[:300] for text in action.get("process_findings", [])[:2]],
        "evidence": [text[:240] for text in action.get("evidence", [])[:2]],
        "full_details": "Original decision and retained history; this is a bounded summary",
        "observed_frames": sorted({f["frame_index"] for f in frames if "frame_index" in f}),
        "tool_error": tool_error,
        "scope": "Task-local process evidence; no automatic quality verdict or knowledge publication",
    }
    stills = [f["path"] for f in frames if f.get("kind") in {"still_pipeline", "generated_reference"}]
    if stills:
        value["observed_still_images"] = list(dict.fromkeys(stills))
    key = fingerprint(value)
    ledger = state.setdefault("work_conclusions", {})
    ledger.setdefault(key, {"id": key, **deepcopy(value)})
    while len(ledger) > 24:
        del ledger[next(iter(ledger))]
    return key


def capacity(task):
    from video_translation.domain.limits import effective_limit
    limit = effective_limit(task, "max_paid_calls", None)
    remaining = max(0, limit - task.get("paid_calls", 0)) if limit is not None else None
    model_stages = {"observe", "visual_analyze", "translate", "plan", "visual", "review"}
    reserve = sum(n.get("definition_id") in model_stages
                  and n.get("status") not in {"succeeded", "skipped", "cancelled"}
                  for n in task.get("nodes", []))
    return {"remaining_external_calls": remaining, "downstream_model_call_reserve": reserve,
            "meaning": "Planning estimate, not extra allowance; media/image/audio requests also use the ledger"}


def continuity(state, queue):
    """Factual working memory, rebuilt from applied outcomes rather than private reasoning."""
    recent = list(state.get("work_conclusions", {}).values())[-6:]
    order = state.get("route_work_order") or {}
    return {
        "base_sha256": state["base"]["sha256"], "turn": state["turn"],
        "current_assignment": {k: order.get(k) for k in (
            "source_id", "method", "question", "execution_scope", "allowed_repair_methods")},
        "remaining_scopes": [{k: row.get(k) for k in (
            "source_id", "remaining_intervals_ms", "actionable_intervals_ms", "obstacles")}
            for row in queue.get("remaining", [])[:24]],
        "last_applied_outcomes": recent,
        "next_step": "Review the pending candidate first" if state.get("candidate") else
                     "Use observed outcomes to choose useful work in the current assignment",
        "keep": ["accepted pixels", "original requests and costs", "scope and quality evidence"],
        "do_not_repeat": "An identical bound failed recipe; reconcile unknown requests before new dispatch.",
        "quality_acceptance": False,
    }


def preserve_output_reserve(controller, state):
    from .review_draft import complete_candidate
    from video_translation.application.agent_recovery import replaced_with_authorization
    from video_translation.integrations.agents.errors import UnknownExternalResult

    report = capacity(controller.task)
    remaining, reserve = report["remaining_external_calls"], report["downstream_model_call_reserve"]
    if (not complete_candidate(controller.task) or remaining is None or not reserve
            or remaining > reserve or state.get("pending_turn") is not None
            or controller.node.get("external_pending")):
        return False
    for kind in ("operation", "production_decision"):
        if any(row.get("task_id") == controller.task["id"]
               and row.get("status") in {"running", "unknown", "invalid_response"}
               and not replaced_with_authorization(controller.store, row, kind)
               for row in controller.store.list(kind)):
            raise UnknownExternalResult("原请求仍待核对，不能借下游资源预留绕过原调用")
    if any(row.get("task_id") == controller.task["id"] and row["status"] in {"pending", "claimed", "unknown"}
           for row in controller.store.list("imagegen_request")):
        raise UnknownExternalResult("原图片请求仍待核对，不能借下游资源预留绕过")
    state["routing_stop"] = {
        "reason": "Preserve the actual remaining call allowance for downstream whole-output work; "
                  "unresolved and unreviewed pixels remain unapplied",
        "capacity": report, "base_sha256": state["base"]["sha256"],
    }
    return True
