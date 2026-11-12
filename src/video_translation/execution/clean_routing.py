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
    candidate = state.get("candidate")
    if candidate:
        available = ([{"source_id": candidate["source_id"], "method": "inspect"}]
                     if candidate["id"] not in state.get("candidate_route_reviews", {}) else [])
    if not available:
        state["routing_stop"] = {"reason": "All available local source/method checks have been assigned",
                                 "base_sha256": state["base"]["sha256"]}
        controller.store.put("production_session", state)
        return False
    method_history = coverage_summary(controller.texts, state)
    from .imagegen_bridge import capability_context
    from .clean_execution_facts import facts
    image_capability = capability_context(controller)
    current_facts = facts(controller.service, controller.task, state)
    identity_inputs = {
        "base": state["base"]["sha256"], "texts": controller.texts,
        "remaining": [(r["source_id"], r["intervals_ms"]) for r in queue["remaining"]],
        "contract": contract["revision"], "methods": methods["sha256"],
        "available_tools": sorted(controller.capabilities()),
        "imagegen_capability": image_capability,
        "clean_execution_facts": current_facts,
        "stop_scope_protocol": 1,
    }
    if candidate:
        identity_inputs["candidate_id"] = candidate["id"]
    feedback = queue.get("work_feedback", [])
    if feedback:
        identity_inputs["work_feedback"] = sorted(r["id"] for r in feedback)
    from .work_autonomy import enabled
    from .work_conclusions import capacity, retain
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
    packet = {
        "agent_role": "route",
        "goal": "Enable the work agent to make verified clean-plate progress using implemented tools.",
        "instructions": (
            "Diagnose whether a deferred obstacle contradicts execution_contract or loaded knowledge. "
            "You manage process and context, not pixels. You have no new video observations. "
            "Assign one continuous unresolved problem a concrete outcome and an implemented method "
            "when there is a new actionable capability/evidence check. Distinguish explicit polygon "
            "protection from automatic segmentation. Do not repeat a rejected recipe, invent donor "
            "geometry, waive quality gates, adjust budgets, replay unknown calls or claim success. "
            "If only genuinely missing capabilities remain, return blocked with the missing evidence."
            " For a whole-output stop, remaining_assessments must cover exactly every remaining "
            "source with disposition=blocked, source-specific evidence and considered_methods. "
            "Assess available image_edit/wan/reference methods without requiring paid trials; "
            "a local-only Work order is not a task-wide ban on Route selecting them next. "
            "If any other source remains actionable, assign it rather than concluding all work."
            " The output goal is a complete, useful viewing candidate, not perfect local pixels. "
            "work_feedback contains actual Work errors, rejected candidates or progress reviews. "
            "Manage these before asking a person a technical question. Prefer an applicable "
            "different method or still-actionable source; do not require every local method to be tried. "
            "If further work has no supported benefit, explain the remaining whole-output gaps "
            "and return blocked so the executor can retain a disclosed, unapproved viewing candidate. "
            "Action counts are review signals, never evidence that media passed or that all methods failed. "
            "Reuse work_conclusions and original receipts; summarize the method outcome in this same "
            "decision instead of scheduling a separate summary call. Review full_output_capacity before "
            "spending more calls on inspections; reserve the remaining downstream work. Prefer useful "
            "whole-output progress to exhaustive per-frame perfection, while retaining actual defects. "
            "If candidate_review is present, only assign its existing candidate a concrete review "
            "question using inspect; do not switch source, start a repair or accept unseen pixels. "
            "If its review cannot be resolved, blocked holds that candidate unapplied and preserves "
            "its evidence while the viewing branch uses only the prior base. "
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
            "on the same base/capability merely by rewording the question. A local method permits "
            "Work to observe, execute a supported local repair and review it in this assignment; "
            "do not create an extra authorization handoff after a successful diagnosis. "
            "An inspect assignment may proceed to a supported local repair once its evidence is "
            "established, except candidate_review which remains review-only. "
            "image_edit/wan assignments use the existing task executor "
            "and ledger; availability is not a new budget or permission to replay a request. "
            "If no listed check is actually "
            "applicable, report the specific missing capability instead of inspecting everything."
            " image_reference is an authorized Codex host tool handoff: work selects an observed frame "
            "and a removal-only prompt through request_reference. reference_plate uses returned "
            "synthetic stills only if their short-window motion/lighting applicability is observed. "
            "A still does not prove video continuity. Exhausted supported methods may produce an "
            "diagnostic preview when user authorized; it cannot unlock lettering or final review. "
            "Practical clean quality permits minor texture or edge differences at 80 points, "
            "but readable original text, bilingual overlap and major subject damage are not minor. "
            "Inspect the clean base independently before lettering. A successful export, nonzero "
            "patch count or a complete-candidate objective cannot establish cleanup readiness. "
            "user_continuation feedback is a new explicit requirement, not a reset of old failures: "
            "choose a changed, evidence-based hypothesis, never an identical failed recipe."
            " clean_execution_facts reports current file/receipt identities and the last applied "
            "Work actions. Source representation SHA and exported video SHA are distinct identities. "
            "An assigned question without an applied action is pending work, not a missing tool result. "
            "Use the supplied verified identities rather than repeatedly asking for the same receipt. "
            "An explicit new execution permits fresh Work assessment within its remaining actions; "
            "historical failure remains evidence, not a permanent block on all different hypotheses."
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
        "work_feedback": feedback,
        "candidate_review": {k: candidate.get(k) for k in (
            "id", "source_id", "sha256", "input_sha256", "start_ms", "end_ms")} if candidate else None,
        "instance_method_history": method_history,
        "method_experience_advisories": method_advisories(controller.store),
        "available_tools": controller.capabilities(),
        "imagegen_capability": image_capability,
        "history": [{"turn": h.get("turn"), "decision": h["decision"],
                     "tool_error": h.get("tool_error")} for h in state["history"][-6:]],
        "budget": controller.budget_snapshot(),
        "full_output_capacity": capacity(controller.task),
        "clean_execution_facts": current_facts,
        "work_conclusions": list(state.get("work_conclusions", {}).values()),
        "wan_exclusions": controller.wan_exclusions(state),
        "completed_work_outcomes": list(state.get("route_work_outcomes", {}).values()),
        "bounded_work_outcomes": list(state.get("work_budget_outcomes", {}).values()),
        "unassigned_local_checks": available,
        "resource_mode": "on_demand", "resource_ids": [],
        "required_resource_ids": ["video-translation.methods", "video-translation.clean-quality"],
    }
    decision = controller.decision(packet, CleanRouteDecision, [], key=f"clean-route-{identity}")
    from .clean_route_stop import review_stop
    decision, stop_validation = review_stop(controller, state, queue, packet, decision, identity)
    # Keep a durable terminal reason rather than making the caller infer it from
    # the number of deferred sources.
    record = {"at": now(), "decision": decision, "base_sha256": state["base"]["sha256"],
              "contract_revision": contract["revision"], "methods_sha256": methods["sha256"]}
    state.setdefault("route_reviews", {})[identity] = record
    retain(state, decision, turn=state["turn"], base_sha256=state["base"]["sha256"],
           frames=[], role="route")
    if stop_validation:
        state["routing_stop"] = {
            "reason": "Route has not established a supported whole-output stop after one "
                      "bounded review; unresolved scopes and available alternatives remain unverified.",
            "scope_validation": stop_validation, "route_review": identity,
            "base_sha256": state["base"]["sha256"],
        }
        controller.store.put("production_session", state)
        return False
    source_id = decision["source_id"]
    remaining = {r["source_id"]: r["intervals_ms"] for r in queue["remaining"]}
    from .review_draft import complete_candidate
    if (decision["action"] != "assign_work" or not decision["question"]
            or source_id not in remaining
            or not complete_candidate(controller.task) and source_id not in state.get("deferred", {})
            or decision["method"] not in controller.capabilities()
            or {"source_id": source_id, "method": decision["method"]} not in available):
        state["routing_stop"] = {"reason": decision["reason"], "route_review": identity,
                                 "base_sha256": state["base"]["sha256"]}
        controller.store.put("production_session", state)
        return False
    obstacle = state.setdefault("deferred", {}).pop(source_id, {
        "reason": "Route selected an independently actionable source after Work feedback"})
    state.setdefault("deferred_history", []).append({
        "source_id": source_id, "obstacle": obstacle, "route_review": identity,
        "turn": state["turn"], "reason": "Route agent assigned a bounded capability/evidence check",
    })
    state.pop("routing_stop", None)
    from .clean_authority import assignment_permissions
    state["route_work_order"] = {**decision, "route_review": identity,
                               **assignment_permissions(controller, decision["method"])}
    if candidate:
        state["route_work_order"].update(execution_scope="review_only",
                                         allowed_repair_methods=[],
                                         generation_authorized=False, local_only=True)
        state.setdefault("candidate_route_reviews", {})[candidate["id"]] = identity
    start, end = remaining[source_id][0]
    from .temporal_work import temporal_work_orders
    orders = temporal_work_orders(controller, state)
    state["route_work_order"]["continuous_scope"] = next(
        (order for order in orders["items"] if order["source_id"] == source_id), None)
    state["times_ms"] = sorted({start, (start + end) // 2, end - 1})
    controller.store.put("production_session", state)
    return True
