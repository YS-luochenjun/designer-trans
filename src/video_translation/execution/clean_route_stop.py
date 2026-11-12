"""A local method failure is not a whole-output capability verdict."""
from copy import deepcopy

from video_translation.domain.production import CleanRouteDecision
from video_translation.persistence.store import now
from .clean_progress import fingerprint
from .review_draft import complete_candidate


def stop_gaps(decision, queue, tools):
    remaining = {row["source_id"] for row in queue["remaining"]}
    rows = decision.get("remaining_assessments", [])
    assessed = {row["source_id"] for row in rows}
    generation = sorted(set(tools) & {"image_edit", "wan", "image_reference", "reference_plate"})
    missing_methods = {
        row["source_id"]: sorted(set(generation) - set(row["considered_methods"]))
        for row in rows if row["source_id"] in remaining
        and set(generation) - set(row["considered_methods"])}
    return {
        "missing_sources": sorted(remaining - assessed),
        "unexpected_sources": sorted(assessed - remaining),
        "duplicate_sources": len(assessed) != len(rows),
        "actionable_sources": sorted(row["source_id"] for row in rows
                                     if row["disposition"] == "actionable"),
        "unassessed_generation_methods": missing_methods,
        "available_generation_methods": generation,
    }


def incomplete(gaps):
    return any(gaps[key] for key in (
        "missing_sources", "unexpected_sources", "duplicate_sources",
        "actionable_sources", "unassessed_generation_methods"))


def review_stop(controller, state, queue, packet, decision, route_identity):
    """One durable correction at most, using the same ledger and original guards."""
    if (decision["action"] != "blocked" or not complete_candidate(controller.task)
            or state.get("candidate")):
        return decision, None
    gaps = stop_gaps(decision, queue, packet["available_tools"])
    if not incomplete(gaps):
        return decision, None
    key = fingerprint({"route": route_identity, "initial_decision": decision,
                       "contract": "whole-output-stop-v1"})
    audits = state.setdefault("route_stop_audits", {})
    audit = audits.setdefault(key, {
        "at": now(), "initial_decision": deepcopy(decision), "scope_validation": gaps,
        "base_sha256": state["base"]["sha256"], "status": "pending",
    })
    controller.store.put("production_session", state)
    if audit.get("decision"):
        corrected = audit["decision"]
    else:
        from .work_conclusions import capacity
        report = capacity(controller.task)
        remaining = report["remaining_external_calls"]
        if (not audit.get("correction_started") and remaining is not None
                and remaining <= report["downstream_model_call_reserve"]):
            gaps["correction"] = "insufficient_call_reserve"
            audit.update(status="resource_blocked", scope_validation=gaps)
            controller.store.put("production_session", state)
            return decision, gaps
        audit["correction_started"] = True
        controller.store.put("production_session", state)
        corrected = controller.decision({
            **packet, "stop_review": gaps, "previous_stop": decision,
            "stop_review_instructions": (
                "The previous stop does not establish whole-output infeasibility. "
                "A narrow Work assignment limits that assignment, not Route's authority to "
                "choose another source or an available method within the original task. "
                "Review every remaining source and the listed available generation methods. "
                "Native imagegen request_allowed=false does not disable KIE image_edit or wan. "
                "Assign one supported different method/source when useful; do not repeat a failed "
                "recipe, force paid experiments or waive clean quality. A whole-output blocked "
                "decision must provide remaining_assessments for exactly every remaining source, "
                "with disposition=blocked, considered_methods and source-bound evidence explaining "
                "why the available alternatives lack supported benefit. Do not invent observations. "
                "An actionable source requires assign_work. There is one correction, not a loop."
            ),
        }, CleanRouteDecision, [], key=f"clean-route-stop-{key}")
        audit.update(status="reviewed", decision=deepcopy(corrected))
        controller.store.put("production_session", state)
    if corrected["action"] != "blocked":
        return corrected, None
    checked = stop_gaps(corrected, queue, packet["available_tools"])
    if incomplete(checked):
        return corrected, checked
    return corrected, None
