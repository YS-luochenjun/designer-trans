"""Bound an acceptance conflict to inspected media; never erase its original verdict."""
from copy import deepcopy

from .frame_clock import frame_window


def dispute_acceptance(state, archive, action, observed_frames, clock, turn, decision_key):
    """Caller verifies archive bytes and supplies actual historical-candidate frame IDs."""
    accepted = next((a for a in state.get("accepted", [])
                     if a["id"] == action.get("candidate_id")), None)
    if state.get("candidate") or not archive or not accepted:
        raise ValueError("Acceptance dispute requires an inspected accepted archive and no current candidate")
    if (archive["id"] != accepted["id"] or archive["sha256"] != accepted["sha256"]
            or action.get("review_sha256") != archive["sha256"]
            or action.get("source_id") != accepted["source_id"]
            or archive["source_id"] != accepted["source_id"]):
        raise ValueError("Acceptance dispute media identity or source mismatch")
    if state.get("archive_inspections", {}).get(archive["id"]) != turn:
        raise ValueError("Acceptance dispute requires this turn's archive observation")
    if (action.get("action") not in {"blocked", "defer"} or not action.get("evidence")
            or not (action.get("quality") or {}).get("hard_failures")):
        raise ValueError("Acceptance dispute requires explicit hard-failure review evidence")
    first, stop = frame_window(archive["start_ms"], archive["end_ms"], clock["rate"])
    if not any(first <= frame < stop for frame in observed_frames):
        raise ValueError("Acceptance dispute requires an actual edited archive frame")
    if any(r["accepted_id"] == accepted["id"] and r["decision_key"] == decision_key
           for r in state.get("acceptance_disputes", [])):
        return False
    state.setdefault("acceptance_disputes", []).append({
        "accepted_id": accepted["id"], "source_id": accepted["source_id"],
        "candidate_sha256": archive["sha256"], "base_sha256": state["base"]["sha256"],
        "decision_key": decision_key, "turn": turn,
        "observed_archive_frames": sorted(observed_frames),
        "evidence": deepcopy(action["evidence"]), "quality": deepcopy(action["quality"]),
        "status": "pending_current_plate_review",
        "effect": "Original acceptance retained; coverage excluded until ordinary scope review passes",
    })
    accepted["needs_scope_review"] = True
    return True
