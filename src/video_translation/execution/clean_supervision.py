"""Send material Work outcomes to Route without resetting work or accepting pixels."""
from video_translation.domain.execution import identity
from .review_draft import complete_candidate


def ready(state, node):
    return not (state.get("pending_turn") is not None
                or node.get("external_pending") or state.get("archive_review")
                or state.get("pipeline_review")
                or any(a.get("needs_scope_review") for a in state.get("accepted", [])))


def request(controller, state, trigger, reason, *, source_id=None, basis=None):
    if not complete_candidate(controller.task):
        return
    from .convergence import binding
    key = identity({**binding(controller, state), "trigger": trigger, "basis": basis})
    reports = state.setdefault("work_feedback", {})
    return reports.setdefault(key, {
        "id": key, "trigger": trigger, "reason": reason, "source_id": source_id,
        "turn": state["turn"], "base_sha256": state["base"]["sha256"],
        "status": "pending", "quality_passed": False,
    })


def pending(controller, state):
    if not complete_candidate(controller.task) or not ready(state, controller.node):
        return []
    history = state.get("history", [])
    if (history and not state.get("candidate")
            and history[-1]["turn"] != state.get("work_feedback_cursor")):
        last = history[-1]
        state["work_feedback_cursor"] = last["turn"]
        action = last["decision"]
        trigger = ("tool_error" if last.get("tool_error") else action["action"]
                   if action["action"] in {"reject_candidate", "defer", "blocked"} else None)
        if trigger:
            # Explanatory wording does not give a repeated recipe another routing turn.
            from .convergence import operative_action
            report = request(controller, state, trigger, last.get("tool_error") or action["reason"],
                             source_id=action.get("source_id"), basis=operative_action(action))
            if report["status"] != "pending":
                repeated = request(controller, state, "repeated_work",
                                   "Work repeated an already routed outcome; choose a different next step",
                                   source_id=action.get("source_id"), basis=report["id"])
                if repeated["status"] != "pending":
                    state["completion_feedback_stop"] = {
                        "reason": "Work repeated the same resolved routing feedback; retain a complete viewing candidate",
                        "feedback_id": repeated["id"], "quality_passed": False,
                        "execution_binding": dict(state.get("execution_binding", {}))}
    from .clean_progress import progress
    if state.get("completion_review_due") and progress(controller.texts, state)["unresolved_instances"]:
        request(controller, state, "whole_output_review",
                "Review the whole output and remaining methods; do not fail a node by action count",
                basis="whole_output_review")
    reports = [r for r in state.get("work_feedback", {}).values()
               if r["status"] == "pending" and r["base_sha256"] == state["base"]["sha256"]]
    for report in reports:
        source_id = report.get("source_id")
        if source_id in controller.texts:
            state.setdefault("deferred", {}).setdefault(source_id, {
                "turn": state["turn"], "reason": report["reason"],
                "work_feedback_id": report["id"],
            })
    if reports:
        controller.store.put("production_session", state)
    return reports


def acknowledge(controller, state, reports, assigned):
    for report in reports:
        report["status"] = "assigned" if assigned else "concluded"
        report["route_review"] = (state.get("route_work_order") or {}).get("route_review") if assigned else None
    controller.store.put("production_session", state)
