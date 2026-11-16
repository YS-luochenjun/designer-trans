"""Finish source lettering evidence before cleanup or target lettering starts."""
from copy import deepcopy
from pathlib import Path

from ..domain.execution import identity
from ..domain.production import SourceDesign
from ..integrations.agents.errors import CapabilityUnavailable
from ..persistence.store import now
from .frame_clock import frame_window, request_time, sample_index
from .review_checkpoint import frame_receipts


REQUIREMENTS = {
    "before_cleanup": True,
    "appearance": ["font or explicit visual approximation/artwork reference", "color", "size",
                   "outline", "shadow", "layout", "source region"],
    "temporal": ["appearance/disappearance", "static or actual keyframed motion",
                 "translation/scale/opacity", "pulsing or beat-related changes",
                 "foreground occlusion", "speech or music timing when applicable"],
    "supported_keyframes": ["x", "y", "scale", "opacity"],
    "unsupported_keyframe_effects": ["rotation", "perspective", "deformation", "per_glyph"],
    "instructions": (
        "先观察原片，逐项保存可用于重建的设计。unknown不是static。"
        "运动含跳动、缩放和淡入淡出，不只是位移；随音乐变化时记录实际关键时刻，"
        "未听音或未分析音频不可声称拍点已核实。艺术字保留样式依据。"
        "无法准确识别字体名称可明确近似或艺术字方法，不伪造精确字体。"
        "motion_evidence_times_ms引用实际观察；需要补看时填写inspection_times_ms。"
        "超出当前轨迹渲染能力的效果如实记录，由Route选原生工具或其他制作方法。"),
}


def readiness(entries, texts, receipts, clock):
    seen = {row["frame_index"] for row in receipts}
    missing = []
    for entry in entries:
        source_id = entry["source_id"]
        item = texts[source_id]
        times = entry.get("motion_evidence_times_ms", [])
        indices = {sample_index(time, clock["rate"], clock["frames"]) for time in times
                   if item["start_ms"] <= time <= item["end_ms"]}
        if entry.get("motion_status", "unknown") == "unknown":
            missing.append({"source_id": source_id, "check": "motion", "reason": "文字运动状态尚未确认"})
        first, stop = frame_window(item["start_ms"], item["end_ms"], clock["rate"])
        required = min(2, max(1, min(stop, clock["frames"]) - first))
        if len(indices) < required or not indices.issubset(seen):
            missing.append({"source_id": source_id, "check": "motion_evidence",
                            "reason": "缺少覆盖文字区间的不同实际帧运动证据"})
        if entry.get("motion_status") == "dynamic" and len(entry.get("motion", [])) < 2:
            missing.append({"source_id": source_id, "check": "motion_keyframes",
                            "reason": "动态文字缺少可复用的实际轨迹"})
        if entry.get("needs_occlusion") and not entry.get("occlusion"):
            missing.append({"source_id": source_id, "check": "occlusion",
                            "reason": "前景遮挡尚缺实际几何关键帧"})
        unsupported = set(entry.get("animation_components", [])) - {
            "translation", "scale", "opacity", "beat_sync"}
        if unsupported:
            missing.append({"source_id": source_id, "check": "render_capability",
                            "reason": "当前轨迹渲染器不支持：" + ", ".join(sorted(unsupported))})
    return {"ready": not missing, "missing": missing}


def additional_times(texts, receipts, clock, requested):
    seen = {row["frame_index"] for row in receipts}
    if requested:
        chosen = sorted({sample_index(time, clock["rate"], clock["frames"]) for time in requested
                         if type(time) is int and 0 <= time < clock["duration_ms"]} - seen)
        return [request_time(index, clock["rate"]) for index in chosen[:12]]
    windows = [frame_window(item["start_ms"], item["end_ms"], clock["rate"])
               for item in texts.values()]
    candidates = []
    for first, stop in windows:
        stop = min(stop, clock["frames"])
        markers = sorted({first - 1, stop, *[index for index in seen if first <= index < stop]})
        for left, right in zip(markers, markers[1:]):
            if right - left > 1:
                candidates.append((right - left, (left + right) // 2))
    candidates.sort(reverse=True)
    return [request_time(index, clock["rate"])
            for index in sorted({index for _, index in candidates[:12]})]


def analyze(controller):
    from media_capabilities.fonts import catalog
    c = controller
    if not c.texts:
        return {"lettering": [], "scene_boundaries_ms": [], "_skip": "没有需要替换的画面文字"}
    _, clock = c.media._info(c.source)
    source_sha = c.service.hash_file(c.source)
    result = {"lettering": [], "scene_boundaries_ms": [], "observations": []}
    items = list(c.texts.values())
    for offset in range(0, len(items), 4):
        batch = items[offset:offset + 4]
        texts = {item["id"]: item for item in batch}
        key = identity({"task_id": c.task["id"], "node_id": c.node["id"],
                        "source_sha256": source_sha, "texts": texts, "protocol": 1})
        state = c.store.get("source_analysis_session", key) or {
            "id": key, "task_id": c.task["id"], "node_id": c.node["id"],
            "source_sha256": source_sha, "created_at": now(), "turn": 0,
            "times_ms": c.times(batch), "observations": [], "missing": [], "reports": [],
        }
        for receipt in state["observations"]:
            path = Path(receipt["path"])
            if not path.is_file() or c.service.hash_file(path) != receipt["sha256"]:
                raise CapabilityUnavailable("原片设计的已观察帧缺失或变化，交Route核原证据")
        while True:
            c.active()
            if c.store.require("task", c.task["id"]).get("control_paused"):
                c.store.put("source_analysis_session", state)
                return {"_paused": True}
            if state.get("finished"):
                part = state["previous_design"]
                result["lettering"].extend(part["lettering"])
                result["scene_boundaries_ms"].extend(part["scene_boundaries_ms"])
                result["observations"].extend(part["observations"])
                break
            pending = state.get("pending_check")
            if pending is None:
                frames = c.frames(c.source, state["times_ms"], "原片",
                                  f"source-design-{offset}-{state['turn']}")
                observed = {(row["frame_index"], row["sha256"]): row for row in state["observations"]}
                for row in frame_receipts(c, frames):
                    observed[row["frame_index"], row["sha256"]] = row
                state["observations"] = list(observed.values())
                inputs = {
                "inventory": {**c.inventory, "texts": batch}, "media": c.prepared["media"],
                "available_fonts": list(c.fonts()), "observed_times_ms": state["times_ms"],
                "font_catalog": catalog(c.fonts()),
                "analysis_requirements": REQUIREMENTS,
                "analysis_checkpoint": {
                    "missing": state["missing"], "observations": state["observations"],
                    "previous_design": state.get("previous_design"),
                },
                    "instructions": REQUIREMENTS["instructions"],
                }
                pending = {"inputs": inputs, "frames": frames,
                           "key": f"source-design-{offset}-{state['turn']}"}
                state["pending_check"] = pending
                c.store.put("source_analysis_session", state)
            part = c.decision(pending["inputs"], SourceDesign, pending["frames"], key=pending["key"])
            ids = [entry["source_id"] for entry in part["lettering"]]
            if set(ids) != set(texts) or len(ids) != len(set(ids)):
                raise ValueError("源设计批次遗漏文字或混入其他批次实例")
            seen_indices = {row["frame_index"] for row in state["observations"]}
            for entry in part["lettering"]:
                for time in entry["evidence_times_ms"]:
                    if (not 0 <= time < c.duration
                            or sample_index(time, clock["rate"], clock["frames"]) not in seen_indices):
                        raise ValueError("源设计引用了并未提供的图像时间")
                speech_ids = {speech["id"] for speech in c.inventory["speech"]}
                if entry.get("speech_id") and entry["speech_id"] not in speech_ids:
                    raise ValueError("口播字幕关联了不存在的人声实例")
                c.check_motion(entry["motion"], texts[entry["source_id"]])
            audit = readiness(part["lettering"], texts, state["observations"], clock)
            state.update(previous_design=deepcopy(part), missing=audit["missing"], turn=state["turn"] + 1)
            state.pop("pending_check", None)
            state["reports"].append({"ready": audit["ready"], "missing": audit["missing"]})
            c.store.put("source_analysis_session", state)
            if audit["ready"]:
                state["finished"] = True
                c.store.put("source_analysis_session", state)
                result["lettering"].extend(part["lettering"])
                result["scene_boundaries_ms"].extend(part["scene_boundaries_ms"])
                result["observations"].extend(part["observations"])
                break
            times = additional_times(
                texts, state["observations"], clock, part.get("inspection_times_ms", []))
            if not times or any(issue["check"] == "render_capability" for issue in audit["missing"]):
                c.service.write_json(c.task["id"], c.node, "source-design-unresolved.json", state)
                raise CapabilityUnavailable(
                    "源文字设计尚未核实，不能进入清底；" +
                    "；".join(f"{issue['source_id']}：{issue['reason']}" for issue in audit["missing"]) +
                    "。原观察与设计已保留，交Route补充证据或选择其他制作方法")
            state["times_ms"] = times
            c.store.put("source_analysis_session", state)
    result.update(
        source_sha256=source_sha, scene_boundaries_ms=sorted(set(result["scene_boundaries_ms"])),
        design_readiness={"protocol_version": 1, "ready": True, "before_cleanup": True,
                          "source_ids": sorted(c.texts), "requirements": REQUIREMENTS})
    c.service.write_json(c.task["id"], c.node, "source-design.json", result)
    return result
