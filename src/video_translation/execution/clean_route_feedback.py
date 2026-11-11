"""Finite feedback from completed local work assignments to the next clean route."""
from .clean_progress import fingerprint

LOCAL_METHODS = ("inspect", "local_surface", "tracked_donor")


def assignment_key(base, source_id, method, contract, methods_sha):
    return fingerprint({"base": base, "source_id": source_id, "method": method,
                        "contract": contract, "methods": methods_sha})


def assigned_checks(state):
    result = set()
    issued = {h["route_review"] for h in state.get("deferred_history", [])
              if h.get("route_review")}
    for key, review in state.get("route_reviews", {}).items():
        decision = review["decision"]
        if key not in issued or decision["action"] != "assign_work":
            continue
        result.add(assignment_key(review["base_sha256"], decision["source_id"], decision["method"],
                                  review["contract_revision"], review["methods_sha256"]))
    return result


def capture_completed_work(state, queue):
    """Only applied work after an issued assignment can advance the route cursor."""
    order = state.get("route_work_order") or {}
    route_key = order.get("route_review")
    reviews = state.get("route_reviews", {})
    if not route_key or route_key not in reviews:
        return False
    if route_key in state.get("route_work_outcomes", {}):
        return False
    issue = next((h for h in reversed(state.get("deferred_history", []))
                  if h.get("route_review") == route_key), None)
    if not issue:
        return False
    source_id = order["source_id"]
    if state.get("candidate") or state.get("pending_turn") is not None:
        return False
    candidates = {c["id"]: c["source_id"] for c in
                  [*state.get("accepted", []), *state.get("candidate_archive", [])]}
    actions = [h for h in state.get("history", [])
               if h["turn"] >= issue["turn"] and h["turn"] < state["turn"]
               and (h["decision"].get("source_id")
                    or candidates.get(h["decision"].get("candidate_id"))) == source_id]
    if not actions:
        return False
    final = actions[-1]["decision"]
    if (final["action"] not in {"defer", "blocked", "accept"}
            or not final.get("evidence")
            or not any(h["decision"]["action"] in {"inspect", "repair"} and not (
                h["decision"]["action"] == "inspect" and h.get("tool_error")) for h in actions)):
        return False
    remaining = next((r for r in queue["remaining"] if r["source_id"] == source_id), None)
    if remaining and remaining.get("actionable_intervals_ms"):
        return False
    state.setdefault("route_work_outcomes", {})[route_key] = {
        "route_review": route_key, "source_id": source_id, "method": order["method"],
        "base_sha256": reviews[route_key]["base_sha256"], "terminal_action": final["action"],
        "conclusion": final["reason"], "evidence": final["evidence"],
        "inspected_times_ms": sorted({t for h in actions if h["decision"]["action"] == "inspect"
                                      and not h.get("tool_error") for t in h["decision"]["times_ms"]}),
        "tool_errors": [h["tool_error"] for h in actions if h.get("tool_error")],
        "meaning": "Completed evidence or failed local attempt; not accepted coverage or permission to generate",
    }
    return True


def frontier(controller, state, queue, contract, methods_sha):
    attempted = assigned_checks(state)
    from .convergence import binding
    capability_sha = binding(controller, state)["capability_sha256"]
    for outcome in state.get("work_budget_outcomes", {}).values():
        if (outcome.get("capability_sha256") == capability_sha
                and outcome.get("contract_revision") == contract
                and outcome.get("methods_sha256") == methods_sha):
            attempted.add(assignment_key(outcome["base_sha256"], outcome["source_id"],
                                        outcome["method"], contract, methods_sha))
            if outcome.get("original_method"):
                attempted.add(assignment_key(outcome["base_sha256"], outcome["source_id"],
                                            outcome["original_method"], contract, methods_sha))
    capabilities = controller.capabilities()
    result = [{"source_id": row["source_id"], "method": method}
            for row in queue["remaining"] for method in LOCAL_METHODS
            if method in capabilities and assignment_key(
                state["base"]["sha256"], row["source_id"], method, contract, methods_sha) not in attempted]
    from .imagegen_bridge import can_request
    from .clean_references import available_references
    refs = available_references(controller, state["base"]["sha256"])
    for row in queue["remaining"]:
        source_id = row["source_id"]
        method = ("reference_plate" if any(r["source_id"] == source_id for r in refs)
                  else "image_reference" if can_request(controller, source_id) else None)
        if method and assignment_key(state["base"]["sha256"], source_id, method,
                                     contract, methods_sha) not in attempted:
            result.append({"source_id": source_id, "method": method})
    return result
