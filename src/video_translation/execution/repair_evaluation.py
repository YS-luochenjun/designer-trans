"""Isolated adapter boundary with immutable intents and no implicit unknown retry."""
import json
from pathlib import Path
import time
from typing import Protocol

from PIL import Image, ImageChops

from video_translation.domain.execution import identity
from video_translation.domain.repair_evaluation import EvaluationRequest
from video_translation.integrations.agents.errors import UnknownExternalResult
from .media import MediaError
from .workspace import file_sha, publish_json


class RepairEngine(Protocol):
    name: str
    version: str
    weights_sha256: str | None

    def run(self, frames, removal_masks, *, deadline, cancelled):
        """Return RGB images in context-frame order; obey deadline and cancellation.

        This interface does not sandbox arbitrary code. Installable/remote engines
        require a separately reviewed process adapter before registration.
        """
        ...


def _mask(visual, reference, geometry):
    path = visual.workspace.confined(reference.path)
    if file_sha(path) != reference.sha256:
        raise MediaError("Evaluation mask changed")
    with Image.open(path) as opened:
        if opened.size != geometry or opened.mode != "L" or getattr(opened, "n_frames", 1) != 1:
            raise ValueError("Mask requires exact source geometry and one grayscale frame")
        if any(count and value not in {0, 255} for value, count in enumerate(opened.histogram())):
            raise ValueError("Mask must be binary")
        return opened.copy()


def evaluate(request, engine: RepairEngine, visual, *, cancelled=lambda: False):
    request = EvaluationRequest.model_validate(request)
    if (engine.name, engine.version, engine.weights_sha256) != (
            request.engine, request.engine_version, request.weights_sha256):
        raise ValueError("Requested engine version or weights are unavailable")
    workspace = visual.workspace
    source = visual._allowed(request.source.path)
    base = visual._allowed(request.base.path)
    if file_sha(source) != request.source.sha256 or file_sha(base) != request.base.sha256:
        raise MediaError("Evaluation source or base changed")
    _, clock = visual._info(base)
    if (request.width, request.height, request.rate) != (clock["width"], clock["height"], clock["rate"]):
        raise ValueError("Evaluation clock or geometry differs from the bound base")
    if request.context_first + request.context_count > clock["frames"]:
        raise ValueError("Evaluation context leaves the source frame range")
    masks = {}
    for row in request.masks:
        removal = _mask(visual, row.removal, (request.width, request.height))
        if row.protection:
            protection = _mask(visual, row.protection, removal.size)
            if ImageChops.multiply(removal, protection).getbbox():
                raise ValueError("Removal mask overlaps protected content; no silent subtraction")
        masks[row.frame_index] = removal
    if cancelled():
        raise MediaError("Evaluation cancelled before dispatch")
    key = identity(request.model_dump())
    directory = workspace.root / "evaluations" / key
    result_path, intent_path = directory / "result.json", directory / "intent.json"
    if result_path.is_file():
        result = json.loads(result_path.read_bytes())
        if result.get("id") != key:
            raise MediaError("Evaluation receipt identity changed")
        if result.get("status") == "succeeded":
            output = Path(result["path"])
            workspace.confined(output)
            if file_sha(output) != result["sha256"]:
                raise MediaError("Retained evaluation result changed")
            visual._info(output)
            return result
        if result.get("status") == "unknown":
            raise UnknownExternalResult("Original evaluation remains unknown; reconcile original operation")
        raise MediaError("Original evaluation failed; retain its receipt and choose another input")
    if intent_path.exists():
        raise UnknownExternalResult("Original evaluation is unsettled; query it, never redispatch")
    workspace.preflight(intent_path, request.max_output_bytes)
    publish_json(intent_path, {"revision": 1, "id": key, "request": request.model_dump()})
    started = time.monotonic()
    deadline = started + request.timeout_seconds
    try:
        frames = []
        with visual._decoded(base, clock) as decoded:
            for index, frame in enumerate(decoded):
                if cancelled() or time.monotonic() > deadline:
                    raise MediaError("Evaluation cancelled or exceeded its deadline")
                if request.context_first <= index < request.context_first + request.context_count:
                    frames.append(frame.copy())
                if index >= request.context_first + request.context_count - 1:
                    break
        if len(frames) != request.context_count:
            raise MediaError("Evaluation decoder returned the wrong frame count")
        context_masks = [masks[index].copy() if index in masks else Image.new("L", frames[0].size) for index in
                         range(request.context_first, request.context_first + request.context_count)]
        outputs = engine.run(frames, context_masks, deadline=deadline, cancelled=cancelled)
        if cancelled() or time.monotonic() > deadline:
            raise MediaError("Evaluation cancelled or exceeded its deadline")
        if len(outputs) != len(frames) or any(
                not isinstance(p, Image.Image) or p.size != frames[0].size or p.mode != "RGB" for p in outputs):
            raise MediaError("Engine result violates frame geometry, count or RGB contract")
        changed = {}
        for row in request.masks:
            candidate = outputs[row.frame_index - request.context_first]
            changed[row.frame_index] = candidate
        index = -1

        def transform(frame, timestamp):
            nonlocal index
            index += 1
            if cancelled() or time.monotonic() > deadline:
                raise MediaError("Evaluation cancelled or exceeded its deadline")
            if index in changed:
                frame.paste(changed[index], mask=masks[index])

        if file_sha(base) != request.base.sha256:
            raise MediaError("Evaluation base changed before candidate publication")
        output = directory / "candidate.vpatch.json"
        with workspace.measure("evaluation", engine=request.engine) as metric:
            visual._render(base, output, clock, transform, output_limit=request.max_output_bytes)
            assets = output.with_suffix(".frames")
            size = output.stat().st_size + sum(p.stat().st_size for p in assets.glob("*.png"))
            if size > request.max_output_bytes:
                raise MediaError("Evaluation candidate exceeds its output allowance")
            metric["bytes_written"] = size
        result = {"id": key, "revision": 1, "status": "succeeded", "path": str(output),
                  "sha256": file_sha(output), "base_sha256": request.base.sha256,
                  "elapsed_seconds": time.monotonic() - started, "output_bytes": size,
                  "engine": request.engine, "engine_version": request.engine_version,
                  "frame_indices": [r.frame_index for r in request.masks], "quality_passed": False,
                  "production_enabled": False, "timing_scope": "isolated local evaluation interval"}
        publish_json(result_path, result)
        return result
    except Exception as exc:
        try:
            publish_json(result_path, {"id": key, "revision": 1,
                                      "status": "unknown" if isinstance(exc, UnknownExternalResult) else "failed",
                                      "error_type": type(exc).__name__, "quality_passed": False})
        except OSError:
            pass  # The insert-only intent still prevents a redispatch after disk failure.
        raise
