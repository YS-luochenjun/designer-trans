"""Deterministic clean progress and unchanged-blocker checks, outside Agent prompts."""
import hashlib
import json

from .clean_policy import coverage_summary


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def progress(texts, state):
    queue = coverage_summary(texts, state)
    total = sum(max(0, item["end_ms"] - item["start_ms"]) for item in texts.values())
    remaining = sum(b - a for row in queue for a, b in row["remaining_intervals_ms"])
    unresolved = [row for row in queue if row["remaining_intervals_ms"]]
    return {
        "accepted_patches": len(state["accepted"]),
        "total_instance_ms": total, "covered_instance_ms": total - remaining,
        "coverage_percent": round(100 * (total - remaining) / total, 1) if total else 100,
        "unresolved_instances": len(unresolved),
        "all_deferred": bool(unresolved) and all(row["fully_deferred"] for row in unresolved),
        "remaining": [{"source_id": row["source_id"], "intervals_ms": row["remaining_intervals_ms"],
                       "actionable_intervals_ms": row["actionable_intervals_ms"],
                       "deferred_intervals_ms": row["deferred_intervals_ms"],
                       "obstacle": (row["deferred"] or {}).get("reason")} for row in unresolved],
        "quality_passed": False,
        "coverage_basis": ("accepted intervals; boundary-reviewed local edits use CFR display bounds; "
                           "not whole-video visual verification"),
    }


def inspection_key(base_sha, source_id, times, overrides):
    return fingerprint({"base": base_sha, "source_id": source_id,
                        "times": sorted(set(times)), "overrides": overrides})


def reuse_trial_inspection(state, trial, source_id, key):
    """A new authorized model trial may re-observe old samples once, not loop."""
    if (not trial or trial["source_id"] != source_id or trial.get("operation_key")
            or trial["base_sha256"] != state["base"]["sha256"]):
        return False
    identity = fingerprint({"inspection": key, "trial": trial["id"]})
    reused = state.setdefault("trial_inspection_reuse", {})
    if identity in reused:
        return False
    reused[identity] = {"inspection_key": key, "trial_id": trial["id"],
                        "turn": state["turn"]}
    return True


def reconsider_accepted_source(texts, state, candidate):
    """A reviewed improvement supersedes a historical obstacle, not its evidence."""
    source_id = candidate["source_id"]
    deferred = state.get("deferred", {}).get(source_id)
    if not deferred:
        return False
    if deferred.get("intervals_ms") is not None:
        # Local success does not release unrelated blurred/occluded intervals.
        return False
    state.setdefault("deferred_history", []).append({
        "source_id": source_id, "obstacle": deferred,
        "resolved_by_candidate": candidate["id"],
        "turn": state["turn"],
        "reason": "Accepted repair supplies new method evidence; reassess remaining intervals.",
    })
    del state["deferred"][source_id]
    remaining = next((row["remaining_intervals_ms"] for row in coverage_summary(texts, state)
                      if row["source_id"] == source_id), [])
    if remaining:
        # Focus the next observation on the gap, not the already accepted repair.
        start, end = remaining[0]
        state["times_ms"] = sorted({start, (start + end) // 2, max(start, end - 1)})
    return bool(remaining)
