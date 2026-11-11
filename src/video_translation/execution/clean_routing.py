"""Finite route/work feedback before a genuine clean capability stop."""
from video_translation.domain.production import CleanRouteDecision
from video_translation.persistence.store import now
from .clean_contract import execution_contract
from .clean_progress import fingerprint
from .clean_policy import coverage_summary
from .clean_experience import method_advisories
from .clean_route_feedback import capture_completed_work, frontier


def route_deferred(controller, state, queue):
    """The route model assigns an evidence check, never a recipe or acceptance."""
    resources = controller.task.get("resource_snapshot", {}).get("knowledge", [])
    methods = next((r for r in resources if r["id"] == "video-translation.methods"
                    and r.get("status") == "active"), None)
    if not methods:
        return False
    contract = execution_contract()
    capture_completed_work(state, queue)
    available = frontier(controller, state, queue, contract["revision"], methods["sha256"])
    if not available:
        state["routing_stop"] = {"reason": "All available local source/method checks have been assigned",
                                 "base_sha256": state["base"]["sha256"]}
        controller.store.put("production_session", state)
        return False
    method_history = coverage_summary(controller.texts, state)
    from .imagegen_bridge import capability_context
    image_capability = capability_context(controller)
    identity_inputs = {
        "base": state["base"]["sha256"], "texts": controller.texts,
        "remaining": [(r["source_id"], r["intervals_ms"]) for r in queue["remaining"]],
        "contract": contract["revision"], "methods": methods["sha256"],
        "available_tools": sorted(controller.capabilities()),
        "imagegen_capability": image_capability,
    }
    from .work_autonomy import enabled
    if enabled(controller.task):
        identity_inputs["work_autonomy"] = "open"
    reviewed_ids = {a["id"] for a in state["accepted"] if not a.get("needs_scope_review")}
    resolutions = [
        {"accepted_id": r["accepted_id"], "decision_key": r["dispute_decision_key"],
         "review_sha256": r["review_sha256"], "interval_ms": r["interval_ms"]}
        for row in method_history for r in row["acceptance_conflict_resolutions"]
        if r["applies_to_current_base"] and r["accepted_id"] in reviewed_ids
    ]
    # Reworded obstacles and turn increments do not create another diagnosis allowance.
    if resolutions:
        identity_inputs["verified_acceptance_resolutions"] = resolutions
    if state.get("route_work_outcomes"):
        # Only completed, issued work advances this cursor; rephrased reasons do not.
        identity_inputs["completed_work_outcomes"] = sorted(state["route_work_outcomes"])
    if state.get("work_budget_outcomes"):
        identity_inputs["work_budget_outcomes"] = sorted(state["work_budget_outcomes"])
    if controller.task.get("review_draft_authorization"):
        identity_inputs["host_tools"] = controller.task["review_draft_authorization"]["id"]
        from .imagegen_bridge import requests
        identity_inputs["host_tool_receipts"] = sorted(
            (r["id"], r["status"]) for r in requests(controller) if r["status"] in {"succeeded", "failed"})
    identity = fingerprint(identity_inputs)
    if identity in state.get("route_reviews", {}):
        return False
    decision = controller.decision({
        "agent_role": "route",
        "goal": "Enable the work agent to make verified clean-plate progress using implemented tools.",
        "instructions": (
            "Diagnose whether a deferred obstacle contradicts execution_contract or loaded knowledge. "
            "You manage process and context, not pixels. You have no new video observations. "
            "Assign exactly one unresolved source a concrete question and available local method only "
            "when there is a new actionable capability/evidence check. Distinguish explicit polygon "
            "protection from automatic segmentation. Do not repeat a rejected recipe, invent donor "
            "geometry, waive quality gates, adjust budgets, replay unknown calls or claim success. "
            "If only genuinely missing capabilities remain, return blocked with the missing evidence."
            " A local blurred/occluded interval does not prove the entire remaining source is infeasible. "
            "Check whether other stable windows remain unexamined; assign evidence-led segmentation "
            "when justified. Do not invent scene boundaries or call a deferred range accepted."
            " Obstacle reasons are historical claims. Consult acceptance_conflict_resolutions for "
            "later scoped verdicts and their media SHA before diagnosing the same acceptance again. "
            "A resolved conflict does not release other deferred frames or prove another text instance "
            "is intentionally retained."
            " Completed work outcomes are new process evidence even when pixels did not change. "
            "Use them to choose a different source/method from unassigned_local_checks when a "
            "supported, untested hypothesis exists. Previously assigned checks cannot be reissued "
            "on the same base/capability merely by rewording the question. These are local-only "
            "checks, not permission for another paid request. If no listed check is actually "
            "applicable, report the specific missing capability instead of inspecting everything."
            " image_reference is an authorized Codex host tool handoff: work selects an observed frame "
            "and a removal-only prompt through request_reference. reference_plate uses returned "
            "synthetic stills only if their short-window motion/lighting applicability is observed. "
            "A still does not prove video continuity. Exhausted supported methods may produce an "
            "explicit review draft when user authorized; that does not accept unresolved text."
            " imagegen_capability binds historical failures to their native_tool_profile. "
            "A failure with applies_to_current_profile=false does not establish that the corrected "
            "current tool wiring is unavailable. request_allowed permits a new work-selected request "
            "using current wiring, never a replay of an unknown or claimed request. Treat generation "
            "success and subsequent video quality as unproven until actual results are reviewed."
            + (" Open-work mode permits revisiting a source/method with a different evidence-based "
               "hypothesis, segmentation or parameters. Prior failed geometry is diagnostic history, "
               "not a permanent prohibition of a local method. Do not repeat identical failed work."
               if enabled(controller.task) else "")
        ),
        "execution_contract": contract, "work_queue": queue,
        "instance_method_history": method_history,
        "method_experience_advisories": method_advisories(controller.store),
        "available_tools": controller.capabilities(),
        "imagegen_capability": image_capability,
        "history": [{"turn": h.get("turn"), "decision": h["decision"],
                     "tool_error": h.get("tool_error")} for h in state["history"][-6:]],
        "budget": controller.budget_snapshot(),
        "wan_exclusions": controller.wan_exclusions(state),
        "completed_work_outcomes": list(state.get("route_work_outcomes", {}).values()),
        "bounded_work_outcomes": list(state.get("work_budget_outcomes", {}).values()),
        "unassigned_local_checks": available,
        "resource_mode": "on_demand", "resource_ids": [],
        "required_resource_ids": ["video-translation.methods", "video-translation.clean-quality"],
    }, CleanRouteDecision, [], key=f"clean-route-{identity}")
    # Keep a durable terminal reason rather than making the caller infer it from
    # the number of deferred sources.
    record = {"at": now(), "decision": decision, "base_sha256": state["base"]["sha256"],
              "contract_revision": contract["revision"], "methods_sha256": methods["sha256"]}
    state.setdefault("route_reviews", {})[identity] = record
    source_id = decision["source_id"]
    remaining = {r["source_id"]: r["intervals_ms"] for r in queue["remaining"]}
    if (decision["action"] != "assign_work" or not decision["question"]
            or source_id not in remaining or source_id not in state.get("deferred", {})
            or decision["method"] not in {"inspect", "local_surface", "tracked_donor",
                                          "image_reference", "reference_plate"}
            or {"source_id": source_id, "method": decision["method"]} not in available):
        state["routing_stop"] = {"reason": decision["reason"], "route_review": identity,
                                 "base_sha256": state["base"]["sha256"]}
        controller.store.put("production_session", state)
        return False
    obstacle = state["deferred"].pop(source_id)
    state.setdefault("deferred_history", []).append({
        "source_id": source_id, "obstacle": obstacle, "route_review": identity,
        "turn": state["turn"], "reason": "Route agent assigned a bounded capability/evidence check",
    })
    state.pop("routing_stop", None)
    state["route_work_order"] = {**decision, "route_review": identity,
                               "local_only": not enabled(controller.task),
                               "generation_authorized": False}
    start, end = remaining[source_id][0]
    state["times_ms"] = sorted({start, (start + end) // 2, end - 1})
    controller.store.put("production_session", state)
    return True
