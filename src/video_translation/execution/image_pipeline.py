"""Read retained image pipelines without restoring or accepting their pixels."""
from pathlib import Path

from PIL import Image

from video_translation.application.tasks import Conflict


def still_frames(controller, pipeline):
    result = []
    for role, label in (("input", "模型输入"), ("generated", "模型原始返回"),
                        ("conformed", "本地缩放结果")):
        if not pipeline.get(role):
            raise ValueError(f"链路未登记{role}角色；可放弃此候选并选择新方法")
        path = controller.verified(pipeline[role])
        with Image.open(path) as image:
            image.verify()
        result.append({"path": str(path), "time_ms": None, "kind": "still_pipeline",
                       "label": f"{label} · 静态图（无视频时间或帧号）"})
    return result


def request(controller, state, action):
    candidates = [state["candidate"]] if state.get("candidate") else []
    candidates += state.get("candidate_archive", [])
    matches = {c["id"]: c for c in candidates if (
        c["id"] == action["candidate_id"] if action.get("candidate_id")
        else c["source_id"] == action.get("source_id"))}
    if len(matches) != 1:
        raise ValueError("请用candidate_id指定唯一当前或历史候选；不会恢复拒绝像素")
    candidate = next(iter(matches.values()))
    pipeline = candidate.get("pipeline")
    if not pipeline:
        raise ValueError(f"候选{candidate['id']}未登记链路角色，不能推断文件丢失；"
                         "可放弃旧候选，依据原片选择新方法")
    times = sorted(set(action.get("times_ms") or []))
    if len(times) > 8 or any(t < 0 for t in times):
        raise ValueError("链路观察单次最多8个非负时间点")
    inspection = {"candidate_id": candidate["id"], "media_type": pipeline.get("media_type", "video")}
    if pipeline.get("media_type") == "still_image":
        still_frames(controller, pipeline)
        inspection["requested_times_not_applicable_to_still"] = times
    else:
        times = controller.pipeline_sample_times(candidate, times)
    for role in ("input", "generated", "conformed"):
        if role not in pipeline or not Path(pipeline[role].get("path", "")).is_file():
            raise Conflict(f"链路{role}角色文件缺失")
        controller.verified(pipeline[role])
    if (state.get("candidate") or {}).get("id") == candidate["id"]:
        state["pipeline_review"] = candidate["id"]
    else:
        state["archive_review"] = candidate["id"]
        state.pop("archive_requested_times", None)
    state["pipeline_times_ms"] = times
    state["pipeline_inspection"] = inspection
    action["candidate_id"] = candidate["id"]
