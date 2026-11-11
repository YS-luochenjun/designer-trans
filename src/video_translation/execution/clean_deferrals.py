"""Interval-scoped obstacles never count as accepted coverage."""
from copy import deepcopy

from .frame_clock import frame_window, request_time


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def _display_bounds(bounds, clock):
    for start, end in bounds:
        if clock:
            first, stop = frame_window(start, end, clock["rate"])
            display_end = request_time(stop, clock["rate"])
            # Correct only integer-ms rounding, never defer another video frame.
            if 0 <= first < stop <= clock["frames"] and 0 < display_end - end <= 1:
                end = display_end
        yield start, end


def interval_partition(gaps, obstacle, clock=None):
    """Legacy unbounded obstacles conservatively block all remaining intervals."""
    if not obstacle:
        return [], deepcopy(gaps)
    bounds = obstacle.get("intervals_ms")
    if bounds is None:
        return deepcopy(gaps), []
    blocked = merge_intervals([
        [max(a, lo), min(b, hi)] for a, b in gaps for lo, hi in _display_bounds(bounds, clock)
        if max(a, lo) < min(b, hi)
    ])
    actionable = []
    for start, end in gaps:
        cursor = start
        for lo, hi in blocked:
            if hi <= cursor or lo >= end:
                continue
            if lo > cursor:
                actionable.append([cursor, lo])
            cursor = max(cursor, hi)
        if cursor < end:
            actionable.append([cursor, end])
    return blocked, actionable


def defer_source(texts, state, action):
    from .clean_policy import coverage_summary

    source_id = action["source_id"]
    item = texts[source_id]
    start, end = action.get("start_ms"), action.get("end_ms")
    if (start is None) != (end is None):
        raise ValueError("Deferred instance range requires both bounds")
    if start is not None and not item["start_ms"] <= start < end <= item["end_ms"]:
        raise ValueError("Deferred range must be inside the source instance")
    previous = state.get("deferred", {}).get(source_id)
    record = {"reason": action["reason"], "evidence": action.get("evidence", []),
              "turn": state["turn"]}
    if previous:
        record["previous_obstacles"] = deepcopy(previous.get("previous_obstacles", []))
        record["previous_obstacles"].append({k: deepcopy(v) for k, v in previous.items()
                                              if k != "previous_obstacles"})
    if start is not None and (not previous or "intervals_ms" in previous):
        record["intervals_ms"] = merge_intervals([
            *(previous or {}).get("intervals_ms", []), [start, end]])
    state.setdefault("deferred", {})[source_id] = record
    rows = coverage_summary(texts, state)
    actionable = [r for r in rows if r["actionable_intervals_ms"]]
    # Prefer the current instance's other windows before an unrelated source.
    row = next((r for r in actionable if r["source_id"] == source_id),
               actionable[0] if actionable else None)
    if row:
        lo, hi = row["actionable_intervals_ms"][0]
        state["times_ms"] = sorted({lo, (lo + hi) // 2, hi - 1})
