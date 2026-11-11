"""Explicit best-effort viewing output, independent of quality acceptance."""


def authorized(task):
    return task.get("review_draft_authorization", {}).get("id")


def is_review_draft(output, task):
    return bool(task and authorized(task)
                and output.get("completion_state") == "review_draft"
                and output.get("draft_authorization_id") == authorized(task)
                and output.get("quality_passed") is False and output.get("sha256"))


def metadata(output, task):
    if not is_review_draft(output, task):
        return {}
    return {k: output[k] for k in ("completion_state", "quality_passed", "draft_authorization_id",
                                   "unresolved", "limitations") if k in output}


def corrected_native_tool_available(controller):
    """A confirmed pre-fix missing-tool receipt is superseded by real tool wiring."""
    from .imagegen_bridge import can_request, requests, NATIVE_TOOL_PROFILE
    return can_request(controller) and any(
        r["status"] == "failed" and r.get("failure_kind") == "native_tool_unavailable"
        and r.get("native_tool_profile") != NATIVE_TOOL_PROFILE for r in requests(controller))


def reconsider_handoff(controller, state):
    draft = state.get("draft_handoff")
    if not handoff_needs_capability_review(controller, state):
        return False
    state.setdefault("draft_preview_archive", []).append(draft)
    state.pop("draft_handoff")
    return True


def handoff_needs_capability_review(controller, state):
    from .imagegen_bridge import capability_context
    draft = state.get("draft_handoff")
    return bool(draft and corrected_native_tool_available(controller)
                and draft.get("imagegen_capability") != capability_context(controller))


def handoff(controller, state, summary):
    from .imagegen_bridge import can_request, capability_context
    from .clean_output import playable_base
    result = {**playable_base(controller, state), "accepted": state["accepted"],
              "decision_session_id": state["id"], "text_overrides": state.get("text_overrides", {}),
              "completion_state": "review_draft", "quality_passed": False,
              "draft_authorization_id": authorized(controller.task),
              "unresolved": summary["remaining"], "coverage_percent": summary["coverage_percent"],
              "imagegen_capability_available": can_request(controller),
              "imagegen_capability": capability_context(controller),
              "limitations": ["尽力审看草稿：清底未全部通过，残余缺口保留；不能视为合格成片",
                              (state.get("routing_stop") or {}).get("reason", "现有方法尝试后仍有缺口")]}
    state["draft_handoff"] = result
    controller.store.put("production_session", state)
    return result


def bound_work(controller, state, summary):
    """An unchanged assignment has a finite work allowance, independent of phrasing."""
    from .work_autonomy import enabled
    if enabled(controller.task):
        return
    if not authorized(controller.task) or state.get("pending_turn") is not None:
        return
    candidate = state.get("candidate")
    if candidate:
        checkpoint = state.get("candidate_review_checkpoint") or {}
        if checkpoint.get("candidate_id") != candidate["id"]:
            state["candidate_review_checkpoint"] = {"candidate_id": candidate["id"], "turn": state["turn"]}
            return
        if state["turn"] - checkpoint["turn"] < 8:
            return
        # Retain unjudged media without inventing a rejection or applying its pixels.
        held = {**candidate, "review_status": "needs_human_review",
                "review_issue": "Candidate review allowance exhausted without a verdict"}
        archive = state.setdefault("candidate_archive", [])
        archive[:] = [c for c in archive if c["id"] != candidate["id"]]
        archive.append(held)
        state["candidate"] = None
        state.setdefault("deferred", {})[candidate["source_id"]] = {
            "turn": state["turn"], "reason": held["review_issue"]}
        state.pop("pipeline_review", None)
        state.pop("archive_review", None)
        state.setdefault("work_budget_outcomes", {})["candidate-" + candidate["id"]] = {
            "source_id": candidate["source_id"], "base_sha256": state["base"]["sha256"],
            "candidate_id": candidate["id"], "reason": held["review_issue"]}
    order = state.get("route_work_order") or {}
    key = (state["base"]["sha256"], order.get("route_review"), order.get("reference_id"))
    checkpoint = state.get("autonomy_checkpoint")
    if not checkpoint or checkpoint["key"] != list(key):
        state["autonomy_checkpoint"] = {"key": list(key), "turn": state["turn"]}
        return
    limit = controller.task["review_draft_authorization"].get("max_work_actions_per_assignment", 8)
    if state["turn"] - checkpoint["turn"] < limit:
        return
    for row in summary["remaining"]:
        if order.get("source_id") and row["source_id"] != order["source_id"]:
            continue
        if not row.get("actionable_intervals_ms"):
            continue
        state.setdefault("deferred", {})[row["source_id"]] = {
            "turn": state["turn"],
            "reason": "Unchanged work assignment exhausted its bounded attempt allowance; "
                      "retain unresolved coverage and consider a different method or review draft"}
    state["autonomy_checkpoint"]["exhausted"] = True
    from .clean_progress import fingerprint
    state.setdefault("work_budget_outcomes", {}).setdefault(fingerprint(key), {
        "base_sha256": state["base"]["sha256"], "route_review": order.get("route_review"),
        "source_id": order.get("source_id"), "method": order.get("method"),
        "reason": "Assignment action allowance exhausted without an accepted base change",
        "applied_turns": [h["turn"] for h in state["history"]
                          if checkpoint["turn"] <= h["turn"] < state["turn"]]})
