"""Compact persistent evidence for practical, coverage-led clean decisions."""

import json
from pathlib import Path

from .frame_clock import boundary_indices, frame_window, request_time
from .clean_deferrals import interval_partition

QUALITY_RESOURCE_ID = "video-translation.clean-quality"


def default_policy():
    return json.loads((Path(__file__).resolve().parents[3] / "knowledge/clean-quality/policy.json").read_text("utf-8"))


def recent_history(history):
    return [{"turn": h["turn"],
             "decision": {k: (v[:350] if k == "reason" else v) for k, v in h["decision"].items()
                          if k in {"action", "source_id", "method", "candidate_id", "reason", "quality", "process_findings"}},
             **({"tool_error": h["tool_error"][:350]} if h.get("tool_error") else {})}
            for h in history[-10:]]


def accepted_interval(record, clock=None):
    """Use actual CFR display bounds only for boundary-reviewed local edits."""
    interval = record["start_ms"], record["end_ms"]
    review = record.get("boundary_review")
    if (not clock or not review
            or record.get("method") not in {
                "local_surface", "tracked_donor", "donor", "flat", "reference_plate", "image_edit"}):
        return interval
    first, stop = frame_window(*interval, clock["rate"])
    required = set(boundary_indices(*interval, clock["rate"], clock["frames"]))
    if (first >= stop or first < 0 or stop > clock["frames"]
            or set(review.get("required_frames", [])) != required
            or not required.issubset(review.get("observed_frames", []))):
        return interval
    return request_time(first, clock["rate"]), request_time(stop, clock["rate"])


def acceptance_conflict_resolutions(source_id, state):
    """Keep later, scoped visual verdicts alongside historical obstacle claims."""
    if not state.get("acceptance_disputes"):
        return []
    accepted = {a["id"]: a for a in state["accepted"] if a["source_id"] == source_id}
    result = []
    for dispute in state.get("acceptance_disputes", []):
        record = accepted.get(dispute["accepted_id"])
        if not record or dispute.get("status") != "resolved_by_current_plate_review":
            continue
        index = dispute.get("resolution_review_index")
        reviews = record.get("scope_reviews", [])
        if type(index) is not int or not 0 <= index < len(reviews):
            continue
        review = reviews[index]
        result.append({
            "accepted_id": record["id"], "dispute_decision_key": dispute["decision_key"],
            "interval_ms": [record["start_ms"], record["end_ms"]],
            "review_sha256": review["baseline_sha256"],
            "applies_to_current_base": review["baseline_sha256"] == state["base"]["sha256"],
            "evidence": review["evidence"], "quality": review["quality"],
            "boundary": "Scoped confirmation only; remaining deferrals and historical evidence retained",
        })
    return result


def coverage_summary(texts, state):
    result = []
    for source_id, item in texts.items():
        start, end = item["start_ms"], item["end_ms"]
        cursor, gaps = start, []
        for a, b in sorted((max(start, a), min(end, b))
                           for r in state["accepted"] if r["source_id"] == source_id
                           and not r.get("needs_scope_review")
                           for a, b in [accepted_interval(r, state.get("coverage_clock"))]):
            if b <= a:
                continue
            if b <= cursor:
                continue
            if a > cursor:
                gaps.append([cursor, a])
            cursor = max(cursor, b)
        if cursor < end:
            gaps.append([cursor, end])
        history = [h for h in state["history"] if h["decision"].get("source_id") == source_id]
        repairs = [h for h in history if h["decision"]["action"] == "repair"]
        obstacle = state.get("deferred", {}).get(source_id)
        deferred_intervals, actionable = interval_partition(gaps, obstacle, state.get("coverage_clock"))
        result.append({
            "source_id": source_id, "text": item.get("text", ""), "remaining_intervals_ms": gaps,
            "acceptance_conflict_resolutions": acceptance_conflict_resolutions(source_id, state),
            "deferred": obstacle, "deferred_intervals_ms": deferred_intervals,
            "actionable_intervals_ms": actionable,
            "fully_deferred": bool(gaps) and not actionable,
            "repair_attempts": len(repairs), "decision_count": len(history),
            "methods_tried": sorted({h["decision"]["method"] for h in repairs}),
            "repair_outcomes": [
                {"turn": h.get("turn"), "method": h["decision"]["method"],
                 "start_ms": h["decision"].get("start_ms"), "end_ms": h["decision"].get("end_ms"),
                 "tool_error": h.get("tool_error"),
                 "candidate_ids": [c["id"] for c in state.get("candidate_archive", [])
                                   if c["source_id"] == source_id
                                   and c.get("start_ms") == h["decision"].get("start_ms")
                                   and c.get("end_ms") == h["decision"].get("end_ms")]}
                for h in repairs],
            "last_obstacle": next(((h.get("tool_error") or h["decision"]["reason"])[:350]
                                   for h in reversed(history)
                                   if h.get("tool_error") or h["decision"]["action"] in {"inspect", "blocked", "reject_candidate"}), None),
        })
    return result


def validate_quality(quality, policy=None):
    policy = policy or default_policy()
    if quality is None:
        raise ValueError("接受候选必须提供质量评分")
    total = sum(quality[name] for name in policy["weights"])
    if total < policy["pass_score"] or quality["hard_failures"]:
        raise ValueError("候选未达到80分或存在硬伤，不能接受")
    return total
