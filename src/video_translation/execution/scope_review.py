"""Recheck existing coverage after envelope edits without generating another patch."""
from .clean_policy import validate_quality
from .clean_progress import fingerprint
from .frame_clock import boundary_indices, request_time


def pending_scope_review(texts, state, clock):
    for accepted in state["accepted"]:
        if not accepted.get("needs_scope_review"):
            continue
        region = texts[accepted["source_id"]]["region"]
        identity = {"accepted": accepted["id"], "base": state["base"]["sha256"], "region": region}
        disputes = [d for d in state.get("acceptance_disputes", [])
                    if d["accepted_id"] == accepted["id"]]
        if disputes:
            identity["dispute_decisions"] = [d["decision_key"] for d in disputes]
        key = fingerprint(identity)
        if key in state.get("scope_review_attempts", {}):
            continue
        indices = boundary_indices(accepted["start_ms"], accepted["end_ms"], clock["rate"], clock["frames"])
        times = [request_time(i, clock["rate"]) for i in indices]
        times += [accepted["start_ms"] + (accepted["end_ms"] - accepted["start_ms"]) * n // 4
                  for n in (1, 2, 3)]
        return {"key": key, "accepted_id": accepted["id"], "source_id": accepted["source_id"],
                "region": region, "start_ms": accepted["start_ms"], "end_ms": accepted["end_ms"],
                "times_ms": sorted(set(times)), "acceptance_conflicts": disputes}
    return None


def confirm_scope_review(state, review, action, observed_frames, clock, policy):
    if not review or action["candidate_id"] != review["accepted_id"]:
        raise ValueError("Scope confirmation must reference the scheduled accepted result")
    if state.get("candidate"):
        raise ValueError("Review the current repair candidate first")
    required = set(boundary_indices(review["start_ms"], review["end_ms"], clock["rate"], clock["frames"]))
    if not required.issubset(observed_frames):
        raise ValueError("Scope confirmation requires actual inner and outer boundary frames")
    score = validate_quality(action.get("quality"), policy)
    accepted = next(a for a in state["accepted"] if a["id"] == review["accepted_id"])
    accepted.setdefault("scope_reviews", []).append({
        "baseline_sha256": state["base"]["sha256"], "region": review["region"],
        "evidence": action["evidence"], "quality": action["quality"], "score": score,
        "required_frames": sorted(required), "observed_frames": sorted(observed_frames),
    })
    accepted["needs_scope_review"] = False
    for dispute in state.get("acceptance_disputes", []):
        if dispute["accepted_id"] == accepted["id"]:
            dispute["status"] = "resolved_by_current_plate_review"
            dispute["resolution_review_index"] = len(accepted["scope_reviews"]) - 1
