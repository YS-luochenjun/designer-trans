"""Create an explicitly unapproved full-timeline preview without any model calls."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from video_translation.application.tasks import Conflict
from video_translation.execution.adaptive import digest
from video_translation.execution.clean_progress import progress
from video_translation.execution.lettering import LetteringProduction
from video_translation.execution.media import MediaTools
from video_translation.execution.visual_media import VisualMedia


def create_preview(service, task_id, asset_id, locale):
    task = service.store.require("task", task_id)
    if task["status"] in {"running", "cancelled"} or task.get("control_paused"):
        raise Conflict("Preview requires an idle, non-cancelled task without a user pause")
    nodes = [node for node in task["nodes"] if node["asset_id"] == asset_id]
    if locale not in task["target_locales"] or not nodes:
        raise ValueError("Asset or locale does not belong to this task")

    def output(_task, _node, name):
        found = next((n for n in nodes if n["definition_id"] == name
                      and n["locale"] in {None, locale}), None)
        if not found or found["status"] not in {"succeeded", "skipped"}:
            raise Conflict(f"Preview prerequisite is not ready: {name}")
        return deepcopy(found["output"])

    clean_node = next(n for n in nodes if n["definition_id"] == "clean")
    sessions = [s for s in service.store.list("production_session")
                if s["task_id"] == task_id and s["node_id"] == clean_node["id"]]
    if len(sessions) != 1:
        raise Conflict("Preview requires one unambiguous clean session")
    state = sessions[0]
    if not state.get("finished") or state.get("progress", {}).get("remaining"):
        raise Conflict("请先完成无字底片；有原文残留的底片不再生成叠字草稿")
    if state.get("pending_turn") is not None or clean_node.get("external_pending"):
        raise Conflict("Reconcile the pending production action before preview")
    node = next(n for n in nodes if n["definition_id"] == "compose" and n["locale"] == locale)
    tools = MediaTools(service.settings.ffmpeg, service.settings.ffprobe)
    # This adapter exposes only existing data and local media, never Agent/provider methods.
    local = SimpleNamespace(service=service, store=service.store, media=tools, output=output)
    producer = LetteringProduction(local, task, clean_node)
    for source_id, override in state.get("text_overrides", {}).items():
        producer.texts[source_id].update(override)
    base = producer.verified(state["base"])
    video_end = producer.media._info(base)[1]["duration_ms"]
    plans = output(task, node, "plan")["lettering"]
    audio = output(task, node, "audio")
    inputs = {"base": state["base"]["sha256"], "plan": plans,
              "translation": output(task, node, "translate"), "design": output(task, node, "visual_analyze"),
              "audio": audio, "overrides": state.get("text_overrides", {}), "version": 3}
    fonts = producer.fonts()
    inputs["fonts"] = {key: service.hash_file(Path(path)) for key, path in fonts.items()}
    identity = digest(inputs | {"task": task_id, "asset": asset_id, "locale": locale})
    prior = service.store.get("local_preview", identity)
    if prior:
        producer.verified(prior)
        return prior
    directory = service.settings.runtime / "tasks" / task_id / "previews" / identity
    directory.mkdir(parents=True, exist_ok=True)
    layers, issues, adaptations = [], [], []
    for entry in plans:
        try:
            if entry["treatment"] != "font":
                adaptations.append({"source_id": entry["source_id"],
                                    "reason": "Draft-only font substitute for artwork; not style approval"})
            layer = producer.layers([entry])[0]
            if layer["end_ms"] > video_end:
                if layer["start_ms"] >= video_end:
                    raise ValueError("Lettering starts beyond the final decoded video frame")
                for field in ("motion", "occlusion"):
                    keys = layer[field]
                    if keys and keys[-1]["time_ms"] > video_end:
                        if field == "motion":
                            final = dict(producer.media._position(keys, video_end))
                        else:
                            validated = producer.media._occlusion(keys, layer["start_ms"], layer["end_ms"])
                            final = {"points": [{"x": x, "y": y} for x, y in
                                                producer.media._polygon_at(validated, video_end)]}
                        layer[field] = [k for k in keys if k["time_ms"] < video_end] + [
                            {**final, "time_ms": video_end}]
                adaptations.append({"source_id": entry["source_id"], "end_ms": video_end,
                                    "reason": "Clip draft lettering to decoded video, not container audio tail"})
                layer["end_ms"] = video_end
            font = fonts.get(layer["style"]["font_id"])
            if not font:
                raise ValueError("Requested font is unavailable")
            visual = VisualMedia(MediaTools(tools.ffmpeg, tools.ffprobe, timeout=tools.timeout, font=font))
            rect = visual.rectangle(producer.prepared["media"], layer["region"])
            image = directory / f"layer-{len(layers)}.png"
            style = {k: v for k, v in layer["style"].items() if k != "font_id"}
            original_size = style["font_size"]
            while True:
                try:
                    visual.letter_layer(layer["text"], image, width=rect["width"], height=rect["height"], style=style)
                    break
                except ValueError as exc:
                    if "Text does not fit" not in str(exc) or style["font_size"] <= 8:
                        raise
                    style["font_size"] -= 1
            if style["font_size"] != original_size:
                adaptations.append({"source_id": entry["source_id"], "from_font_size": original_size,
                                    "to_font_size": style["font_size"], "reason": "Fit draft text inside source region"})
            layers.append({k: layer[k] for k in ("start_ms", "end_ms", "region", "motion", "occlusion")}
                          | {"artwork_path": str(image)})
        except (ValueError, RuntimeError) as exc:
            issues.append({"source_id": entry["source_id"], "reason": str(exc)})
    if not layers:
        raise Conflict("No renderable translated layers; cannot label the source as a translated preview")
    visual_path = directory / "visual.mp4"
    producer.media.typography(base, visual_path, layers)
    audio_path = producer.verified(audio) if audio.get("sha256") else service.checked_path(audio)
    target = directory / "preview.mp4"
    tools._run([tools.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", visual_path, "-i", audio_path,
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-crf", "18",
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", target])
    result_info = tools.probe(target)
    if abs(result_info["duration_ms"] - producer.duration) > 150:
        raise ValueError("Preview timeline differs from source")
    tools._run([tools.ffmpeg, "-v", "error", "-i", target, "-f", "null", "-"])
    if service.store.require("task", task_id)["updated_at"] != task["updated_at"]:
        raise Conflict("Task changed during preview; output not published")
    artifact = service.artifact(task_id, node, target, "video", f"未验收草稿 · {locale} · 存在待解决区域")
    report = {"id": identity, "task_id": task_id, "path": str(target), "sha256": artifact["sha256"],
              "artifact_id": artifact["id"], "quality_passed": False, "delivery_allowed": False,
              "rendered_layers": len(layers), "omitted_layers": issues, "adaptations": adaptations,
              "progress": progress(producer.texts, state),
              "limitations": ["Unresolved source text may remain under translated layers.",
                              "This is a preview, not acceptance, delivery, or full visual/audio review."],
              "inputs": inputs}
    service.store.put("local_preview", report)
    service.write_json(task_id, node, f"preview-{identity[:12]}.json", report)
    return report
