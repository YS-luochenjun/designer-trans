"""Bound work by semantic state; diagnostics never manufacture accepted coverage."""
from video_translation.domain.execution import ExecutionProfile, StepObservation, identity
from video_translation.integrations.agents.errors import CapabilityUnavailable

ERROR = "Unchanged base/scope/capability exhausted its action allowance; retain unresolved coverage"


def assignment_handoff(state, summary, incident):
    """An applied, exhausted assignment may yield unresolved work, never a verdict."""
    order = state.get("route_work_order") or {}
    review_id = order.get("route_review")
    review = state.get("route_reviews", {}).get(review_id, {})
    decision = review.get("decision", {})
    source_id = order.get("source_id")
    returned_reference = (decision.get("method") == "image_reference"
                          and order.get("method") == "reference_plate" and order.get("reference_id")
                          and any(h.get("reference_id") == order["reference_id"]
                                  and h.get("source_id") == source_id
                                  for h in state.get("deferred_history", [])))
    if (not review_id or decision.get("action") != "assign_work"
            or decision.get("source_id") != source_id
            or decision.get("method") != order.get("method") and not returned_reference
            or not any(h.get("route_review") == review_id for h in state.get("deferred_history", []))
            or state.get("candidate") or state.get("pending_turn") is not None
            or state.get("archive_review") or state.get("pipeline_review")
            or any(a.get("needs_scope_review") for a in state.get("accepted", []))):
        return None
    row = next((r for r in summary["remaining"] if r["source_id"] == source_id), None)
    if not row or any(r.get("actionable_intervals_ms") for r in summary["remaining"]
                      if r["source_id"] != source_id):
        return None
    applied = [h for h in state.get("history", [])
               if incident["first_turn"] <= h["turn"] < state["turn"]
               and h["decision"].get("source_id") == source_id]
    if not applied or not any(h["decision"]["action"] in {"inspect", "repair", "defer", "blocked"}
                              for h in applied):
        return None
    return {"source_id": source_id, "method": order["method"], "route_review": review_id,
            "original_method": decision["method"], "reference_id": order.get("reference_id"),
            "intervals_ms": row["intervals_ms"], "applied_turns": [h["turn"] for h in applied],
            "contract_revision": review["contract_revision"],
            "methods_sha256": review["methods_sha256"]}


def yield_assignment(controller, state, summary, incident):
    if controller.node.get("external_pending"):
        return False
    outcome = assignment_handoff(state, summary, incident)
    if not outcome:
        return False
    from copy import deepcopy
    from .clean_progress import progress
    source_id = outcome["source_id"]
    previous = deepcopy(state.get("deferred", {}).get(source_id))
    reason = "Assignment allowance exhausted; retain unresolved intervals for a different finite Route check"
    state.setdefault("deferred", {})[source_id] = {
        "turn": state["turn"], "reason": reason, "intervals_ms": outcome["intervals_ms"],
        "previous_obstacles": [previous] if previous else [],
        "execution_incident_id": incident["id"],
    }
    state.setdefault("work_budget_outcomes", {}).setdefault(incident["id"], {
        **outcome, **{k: incident[k] for k in (
            "base_sha256", "scope_sha256", "capability_sha256")},
        "reason": reason, "quality_passed": False, "incident_id": incident["id"],
        "meaning": "Bounded unresolved work; no acceptance, replay or new generation authority",
    })
    incident["status"] = "yielded_to_clean_route"
    state["progress"] = progress(controller.texts, state)
    controller.store.put("production_session", state)
    return True


def binding(controller, state):
    from .clean_contract import execution_contract
    profile = ExecutionProfile.model_validate(controller.task.get("execution_snapshot", {}))
    execution = profile.model_dump()
    if profile.resource_budget is None:
        execution.pop("resource_budget")
    return {"base_sha256": state["base"]["sha256"],
            "scope_sha256": identity(controller.texts),
            "capability_sha256": identity({
                "tools": sorted(controller.capabilities()),
                "contract": execution_contract(),
                "model": controller.task.get("model_snapshot"),
                "resources": controller.task.get("resource_snapshot"),
                "execution": execution,
            })}


def record_observation(controller, state, action, frames):
    from .workspace import file_sha
    evidence = [{"base": f.get("base_sha256"), "frame": f.get("frame_index"),
                 "sha256": f.get("sha256") or file_sha(f["path"])} for f in frames]
    observation = StepObservation(**binding(controller, state),
        evidence_sha256=identity(evidence), action=action["action"],
        method=action.get("method"), candidate_id=action.get("candidate_id"))
    state["history"][-1]["execution_observation"] = {
        **observation.model_dump(), "evidence_binding_id": observation.incident_id}


def guard(controller, state, summary):
    inputs = binding(controller, state)
    state["execution_binding"] = inputs
    order = state.get("route_work_order") or {}
    assignment = {k: order.get(k) for k in ("source_id", "method")}
    key = identity({**inputs, "assignment": assignment})
    checkpoints = state.setdefault("execution_checkpoints", {})
    checkpoint = checkpoints.setdefault(key, {**inputs, "start_turn": state["turn"], "actions": 0})
    checkpoint["actions"] = state["turn"] - checkpoint["start_turn"]
    profile = ExecutionProfile.model_validate(controller.task.get("execution_snapshot", {}))
    # Let the existing finite Route frontier select another untried assignment.
    deferred_ready = (summary.get("all_deferred") and not state.get("candidate")
                      and state.get("pending_turn") is None
                      and not controller.node.get("external_pending"))
    if checkpoint["actions"] < profile.max_unchanged_actions:
        return
    from .review_draft import complete_candidate
    from video_translation.domain.limits import testing_unlimited
    if testing_unlimited(controller.task) or complete_candidate(controller.task):
        from .clean_supervision import request
        request(controller, state, "progress_review",
                "The base is unchanged; Route must assess useful next work or a complete viewing candidate",
                basis={"checkpoint": key})
        return
    incident = state.setdefault("execution_incidents", {}).setdefault(key, {
        "id": key, "kind": "no_base_progress", **inputs, "node_id": controller.node["id"],
        "first_turn": checkpoint["start_turn"], "last_turn": state["turn"],
        "actions": checkpoint["actions"], "status": "needs_new_evidence_or_capability",
        "unresolved_source_ids": [r["source_id"] for r in summary["remaining"]],
        "candidate_id": (state.get("candidate") or {}).get("id"),
        "quality_passed": False,
        "rework": {"stage": "clean", "mode": "proposal", "expected_base_sha256": inputs["base_sha256"],
                   "preserve_accepted": True, "preserve_unknown": True,
                   "invalidates_downstream": ["typography", "visual", "compose", "review"],
                   "requires": "New bound evidence/capability and explicit application decision"},
    })
    if yield_assignment(controller, state, summary, incident):
        return
    if deferred_ready:
        return
    state["routing_stop"] = {"incident_id": incident["id"], "reason": ERROR}
    controller.store.put("production_session", state)
    raise CapabilityUnavailable(state["routing_stop"]["reason"])


def operative_action(value):
    if isinstance(value, dict):
        return {k: operative_action(v) for k, v in value.items()
                if k not in {"reason", "evidence", "process_findings", "quality"}}
    if isinstance(value, list):
        return [operative_action(v) for v in value]
    return value


def reserve_action(controller, state, action, *, resuming=False):
    """A rephrased failed repair cannot create another local or paid dispatch."""
    if action["action"] != "repair":
        return
    semantic = operative_action(action)
    item = controller.texts.get(action.get("source_id"), {})
    for field in ("start_ms", "end_ms", "region"):
        if semantic.get(field) is None and field in item:
            semantic[field] = item[field]
    if state.get("new_execution"):
        for history in state.get("history", []):
            if (history["turn"] >= state["new_execution"]["start_turn"]
                    or history.get("input_base_sha256") != state["base"]["sha256"]
                    or history["decision"].get("action") != "repair"):
                continue
            previous_action = operative_action(history["decision"])
            for field in ("start_ms", "end_ms", "region"):
                if previous_action.get(field) is None and field in item:
                    previous_action[field] = item[field]
            if previous_action == semantic:
                raise CapabilityUnavailable("Explicit new execution does not replay an identical original repair")
    key = identity({**binding(controller, state), "action": semantic})
    attempts = state.setdefault("execution_action_intents", {})
    previous = attempts.get(key)
    if previous and not (resuming and previous["turn"] == state["turn"]):
        controller.store.put("production_session", state)
        raise CapabilityUnavailable("Identical bound repair already dispatched; inspect original result")
    if not previous:
        attempts[key] = {"turn": state["turn"], "status": "reserved",
                         "base_sha256": state["base"]["sha256"], "method": action.get("method")}
        controller.store.put("production_session", state)
