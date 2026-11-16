"""Keep Route quality rework separate from historical text interval coverage."""
from copy import deepcopy

from video_translation.domain.execution import identity
from video_translation.persistence.store import now
from .clean_supervision import ready


def unreceived(state):
    return [r for r in state.get("work_feedback", {}).values()
            if r.get("trigger") in {"route_rework", "user_continuation"}
            and r.get("status") in {"pending", "concluded"}
            and r.get("base_sha256") == state["base"]["sha256"]
            and r.get("turn") == state["turn"]]


def prepare(controller, state, summary):
    if (state.get("finished") or summary["unresolved_instances"] or state.get("candidate")
            or not ready(state, controller.node)):
        return state.get("clean_rework")
    reports = unreceived(state)
    previous = state.get("clean_rework")
    if not reports:
        return previous
    key = identity(sorted(r["id"] for r in reports))
    if previous and previous["id"] == key:
        return previous
    if previous:
        state.setdefault("clean_rework_archive", []).append(previous)
    rework = {
        "id": key, "start_turn": state["turn"], "base_sha256": state["base"]["sha256"],
        "reports": [deepcopy(r) for r in reports], "quality_passed": False,
        "instruction": "Route reopened quality work on already covered text intervals. "
                       "Inspect the reported defect and repair within the task's actual authority. "
                       "Historical interval coverage is not visual approval. Preserve accepted "
                       "repairs and review the current plate again before finish.",
    }
    # The new Task Route direction supersedes an old, completed local assignment.
    for field, archive in (("route_work_order", "route_work_order_archive"),
                           ("draft_handoff", "draft_preview_archive")):
        if state.get(field):
            state.setdefault(archive, []).append(state.pop(field))
    state.pop("routing_stop", None)
    state.pop("completion_feedback_stop", None)
    state["clean_rework"] = rework
    controller.store.put("production_session", state)
    return rework


def acknowledge(controller, state, rework, turn):
    if not rework:
        return
    changed = False
    for report in rework["reports"]:
        original = state.get("work_feedback", {}).get(report["id"])
        if original and original.get("status") in {"pending", "concluded"}:
            original.update(status="received_by_work", received_at=now(),
                            received_turn=turn, work_decision_key=f"clean-{turn}")
            changed = True
    if changed:
        controller.store.put("production_session", state)


def requires_work(rework, summary):
    return bool(rework and not summary["unresolved_instances"])


def new_obstacle(rework, reports):
    """A failed rework returns its actual outcome to Task Route, not an empty gap queue."""
    if not rework:
        return None
    original_ids = {r["id"] for r in rework["reports"]}
    return next((r for r in reports if r["id"] not in original_ids
                 and r.get("turn", -1) > rework["start_turn"]
                 and r.get("trigger") in {"tool_error", "reject_candidate", "defer",
                                          "blocked", "repeated_work"}), None)
