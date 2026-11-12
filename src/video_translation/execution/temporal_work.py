"""Continuous problem scopes for existing Route/Work calls; no new paid operation."""
from __future__ import annotations

import math
from fractions import Fraction

from video_translation.domain.execution import identity
from video_translation.domain.limits import budget_policy, testing_unlimited
from .frame_clock import frame_window, request_time


def temporal_work_orders(controller, state):
    from .clean_policy import coverage_summary
    info = state.get("coverage_clock") or getattr(controller, "prepared", {}).get("media", {})
    rate = info.get("rate") or info.get("frame_rate") or info.get("fps")
    if not rate:
        return {"items": [], "reason": "Native frame rate unavailable; no invented frame mapping"}
    try:
        rate = Fraction(str(rate))
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return {"items": [], "reason": "Native frame rate invalid; no invented frame mapping"}
    if not 0 < rate <= 120:
        return {"items": [], "reason": "Native frame rate invalid"}
    count = info.get("frames") or info.get("frame_count")
    if not count:
        return {"items": [], "reason": "Actual frame count unavailable; media must verify the clock first"}
    if isinstance(count, bool) or not str(count).isdigit():
        return {"items": [], "reason": "Actual frame count invalid; no invented frame mapping"}
    count = int(count)
    if not 0 < count <= 108_000:
        return {"items": [], "reason": "Actual frame count exceeds supported media limits"}
    budget = controller.task.get("execution_snapshot", {}).get("resource_budget")
    maximum = max(1, math.floor(float(rate) * 2))
    route = state.get("route_work_order") or {}
    rows = coverage_summary(controller.texts, state)
    if route.get("source_id"):
        rows.sort(key=lambda row: row["source_id"] != route["source_id"])
    result = []
    for row in rows:
        intervals = row.get("actionable_intervals_ms") or []
        for start, end in intervals:
            first, stop = frame_window(start, end, rate)
            stop = min(stop, count)
            if stop <= first:
                continue
            windows = []
            window_count = math.ceil((stop - first) / maximum)
            for a in list(range(first, stop, maximum))[:64]:
                b = min(a + maximum, stop)
                windows.append({
                    "first_frame": a, "stop_frame": b,
                    "context_first_frame": max(0, a - 2), "context_stop_frame": min(count, b + 2),
                    "write_only_first_frame": a, "write_only_stop_frame": b,
                })
            value = {
                "revision": 1, "kind": "continuous_problem_work_order",
                "task_id": controller.task["id"], "source_id": row["source_id"],
                "base_sha256": state["base"]["sha256"], "first_frame": first, "stop_frame": stop,
                "rate": str(rate), "actual_source_frames": count,
                "requested_interval_ms": [start, end], "display_interval_ms": [
                    request_time(first, rate), request_time(stop, rate)],
                "windows": windows, "windows_complete": window_count <= 64,
                "total_compute_windows": window_count,
                "resource_limits": ({**budget, **dict.fromkeys((
                    "max_external_calls", "max_recovery_calls", "max_clean_turns"))}
                    if budget and testing_unlimited(controller.task) else budget),
                "budget_policy": budget_policy(controller.task),
                "quality_policy": "Existing clean quality and current-base review remain mandatory.",
                "grouping": "One unresolved continuous source interval; never merge gaps or different sources.",
                "execution": "Use existing interval tools; windows are compute bounds, not separate Agent calls.",
                "stops": ["scene cut, changed occlusion or mask requires verified subdivision",
                          "unknown result, user pause/cancel, storage protection or changed base stops new work",
                          "numeric allowances apply only when budget_policy.usage_quotas_enforced is true",
                          "window success cannot approve unprocessed frames or the whole plate"],
                "authorization_source": "Current Route work order and task executor ledger",
                "automatic_acceptance": False,
            }
            result.append({**value, "work_order_id": identity(value)})
    return {"items": result[:24], "complete": len(result) <= 24,
            "default_compute_window_ms": 2000, "scene_boundaries_verified": False,
            "review_unit": "Full affected problem interval; investigate only actual anomalies by frame.",
            "new_model_calls_scheduled": 0}
