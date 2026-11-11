"""Bounded delivery of historical frames; observation reuse is not a new verdict."""
from copy import deepcopy
from pathlib import Path

from video_translation.application.tasks import Conflict
from video_translation.domain.execution import identity
from .frame_clock import sample_index
from .workspace import file_sha


def window_key(controller, state, times):
    _, clock = controller.media._info(controller.source)
    return identity({"revision": 1, "source": file_sha(controller.source),
                     "base": state["base"]["sha256"],
                     "scope": state.get("text_overrides", {}),
                     "frames": sorted({sample_index(t, clock["rate"], clock["frames"])
                                       for t in times})})


def normal_frames(controller, state, base, comparison, turn):
    times = state["times_ms"]
    cacheable = not any(state.get(k) for k in ("candidate", "archive_review", "pipeline_review"))
    key = window_key(controller, state, times)
    saved = state.get("observation_windows", {}).get(key) if cacheable else None
    if saved:
        _, clock = controller.media._info(controller.source)
        indexed = {}
        for frame in saved["frames"]:
            path = Path(frame["path"])
            root = controller.service.settings.runtime / "tasks" / controller.task["id"]
            if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
                    or file_sha(path) != frame["sha256"]):
                raise Conflict("Historical observation changed; reconcile the original evidence")
            indexed[(frame["label"].split(" ·", 1)[0], frame["frame_index"])] = frame
        result = []
        for role in dict.fromkeys(role for role, _ in indexed):
            for stamp in times:
                frame = deepcopy(indexed[(role, sample_index(stamp, clock["rate"], clock["frames"]))])
                frame["time_ms"] = stamp
                result.append(frame)
    else:
        result = controller.frames(controller.source, times, "原片", f"original-{turn}")
        if state.get("candidate"):
            result += controller.frames(base, times, "修复前底片（用于归因）", f"baseline-{turn}")
        result += controller.frames(comparison, times,
            "当前局部修复候选" if state.get("candidate") else "已接受底片", f"candidate-{turn}")
        if cacheable:
            for frame in result:
                frame["sha256"] = file_sha(frame["path"])
            windows = state.setdefault("observation_windows", {})
            windows[key] = {"frames": deepcopy(result), "turn": turn}
            # Trace packets retain older receipts; this is the bounded active index.
            while len(windows) > 64:
                del windows[next(iter(windows))]
            controller.store.put("production_session", state)
    state["visible_observation_key"] = key if cacheable else None
    return result


def allow_reattachment(controller, state, times):
    """Allow one return to a different retained window, never a same-visible loop."""
    key = window_key(controller, state, times)
    if key == state.get("visible_observation_key"):
        return False
    if key not in state.get("observation_windows", {}):
        return False
    used = state.setdefault("observation_reattachments", {})
    if key in used:
        return False
    used[key] = {"turn": state["turn"], "previous_window": state.get("visible_observation_key")}
    return True
