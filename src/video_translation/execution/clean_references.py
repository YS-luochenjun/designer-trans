"""Source-bound generated stills are repair inputs, never accepted video."""
from pathlib import Path

from PIL import Image


def available_references(controller, base_sha=None):
    if base_sha is None:
        sessions = [s for s in controller.store.list("production_session")
                    if s["node_id"] == controller.node["id"]]
        base_sha = sessions[0]["base"]["sha256"] if len(sessions) == 1 else None
    return [r for r in controller.store.list("clean_reference")
            if r["task_id"] == controller.task["id"] and r["node_id"] == controller.node["id"]
            and r["base_sha256"] == base_sha]


def reference_frames(controller, state):
    if state.get("candidate"):
        return []
    source = (state.get("route_work_order") or {}).get("source_id")
    refs = [r for r in available_references(controller, state["base"]["sha256"])
            if source is None or r["source_id"] == source]
    return [{"path": str(verified_reference(controller, r)), "time_ms": r["time_ms"],
             "frame_index": r["frame_index"],
             "kind": "generated_reference",
             "label": "生成参考底图（非原片或候选视频证据） " + r["id"]} for r in refs[:1]]


def verified_reference(controller, record):
    path = Path(record["path"])
    root = (controller.service.settings.runtime / "tasks" / controller.task["id"]).resolve()
    if (path.is_symlink() or not path.resolve().is_relative_to(root)
            or not path.is_file() or controller.service.hash_file(path) != record["sha256"]):
        raise ValueError("Generated reference file changed or left the task directory")
    return path


def repair_reference(controller, action, base, target, turn, region, start, end):
    refs = available_references(controller, controller.service.hash_file(base))
    record = next((r for r in refs if r["id"] == action.get("reference_id")
                   and r["source_id"] == action["source_id"]), None)
    if not record:
        raise ValueError("Select a generated reference bound to this source and current base")
    if end - start > 2000 or not start <= record["time_ms"] < end:
        raise ValueError("A reference still requires a window of at most two seconds containing its frame")
    boundaries = controller.e.output(controller.task, controller.node, "visual_analyze").get(
        "scene_boundaries_ms", [])
    if any(start < boundary < end for boundary in boundaries):
        raise ValueError("A generated reference cannot span a known shot boundary")
    if action.get("protected_foreground"):
        raise ValueError("Reference still compositing does not implement foreground polygon protection")
    if region["width"] * region["height"] >= .95:
        raise ValueError("Select a local reference region; full-frame still replacement freezes the video")
    reference = verified_reference(controller, record)
    _, info = controller.media._info(base)
    with Image.open(reference) as image:
        full = image.convert("RGB").resize((info["width"], info["height"]), Image.Resampling.LANCZOS)
        x, y, w, h = controller.media.pixel_region(region, info["width"], info["height"])
        plate = controller.directory / f"reference-plate-{turn}.png"
        full.crop((x, y, x + w, y + h)).save(plate)
    controller.media.restore(base, target, start_ms=start, end_ms=end, region=region,
                             method="image", image_path=plate)
