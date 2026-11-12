"""Lossless sparse local edits with immutable, hash-verified dependencies.

Manifests are candidates, never video files or quality approvals. Full videos
are materialized only at an explicit boundary and checked against decoded RGB.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from PIL import Image, ImageChops

from video_translation.domain.execution import ExecutionProfile, FileRef, FramePatch, PatchManifest, identity
from .frame_clock import frame_window, sample_index
from .media import MediaError
from .visual_media import VisualMedia
from .workspace import ExecutionWorkspace, file_sha, publish_json


class EfficientVisualMedia(VisualMedia):
    MAX_DEPTH = 256
    CHECKPOINT_DEPTH = 32

    def __init__(self, media, workspace, allowed_sources=(), inherited_refs=()):
        super().__init__(media)
        self.workspace = workspace
        self.allowed_sources = {Path(p).resolve() for p in allowed_sources}
        self.inherited_refs = {Path(r["path"]).resolve(): r["sha256"] for r in inherited_refs}
        self._decoder_identity = None

    @staticmethod
    def is_manifest(path):
        return str(path).endswith(".vpatch.json")

    def _allowed(self, path):
        path = Path(path)
        if path.is_symlink():
            raise ValueError("Symlink execution artifacts are not supported")
        path = path.resolve()
        if path in self.inherited_refs:
            if file_sha(path) != self.inherited_refs[path]:
                raise MediaError("Inherited rework input changed")
        elif path not in self.allowed_sources:
            self.workspace.confined(path)
        return path

    def _chain(self, source):
        source = self._allowed(source)
        current, seen, layers = source, set(), []
        while self.is_manifest(current):
            if current in seen or len(layers) >= self.MAX_DEPTH:
                raise MediaError("Cyclic or excessive patch dependencies")
            seen.add(current)
            if current.stat().st_size > 2 * 1024**2:
                raise MediaError("Patch manifest exceeds metadata limit")
            manifest = PatchManifest.model_validate_json(current.read_bytes())
            parent = self._allowed(manifest.base.path)
            if file_sha(parent) != manifest.base.sha256:
                raise MediaError("Patch base changed; original operation must be reconciled")
            for patch in manifest.patches:
                image = self._allowed(patch.image.path)
                if file_sha(image) != patch.image.sha256:
                    raise MediaError("Patch pixels changed")
            layers.append(manifest)
            current = parent
        root, info = super()._info(current)
        for layer in layers:
            if (layer.width, layer.height, layer.frames, layer.rate) != (
                    info["width"], info["height"], info["frames"], info["rate"]):
                raise MediaError("Patch clock or geometry differs from the source")
        return root, info, list(reversed(layers))

    def _info(self, source):
        if not self.is_manifest(source):
            return super()._info(self._allowed(source))
        _, info, _ = self._chain(source)
        return Path(source).resolve(), info

    def _audio_source(self, source):
        return self._chain(source)[0] if self.is_manifest(source) else source

    @staticmethod
    def _apply(image, patches):
        for patch in patches:
            x, y, right, bottom = patch.rectangle
            with Image.open(patch.image.path) as pixels:
                if pixels.size != (right - x, bottom - y) or pixels.mode != "RGB":
                    raise MediaError("Patch pixels do not match their rectangle")
                image.paste(pixels, (x, y))
        return image

    @contextmanager
    def _decoded(self, source, info):
        if not self.is_manifest(source):
            with super()._decoded(source, info) as frames:
                yield frames
            return
        root, checked, layers = self._chain(source)
        by_index = {}
        for layer in layers:
            for patch in layer.patches:
                by_index.setdefault(patch.frame_index, []).append(patch)
        with super()._decoded(root, checked) as frames:
            yield (self._apply(image, by_index.get(index, ())) for index, image in enumerate(frames))

    @contextmanager
    def _decoded_window(self, source, info, first, stop):
        if not self.is_manifest(source):
            with super()._decoded_window(source, info, first, stop) as frames:
                yield frames
            return
        root, checked, layers = self._chain(source)
        by_index = {}
        for layer in layers:
            for patch in layer.patches:
                if first <= patch.frame_index < stop:
                    by_index.setdefault(patch.frame_index, []).append(patch)
        with super()._decoded_window(root, checked, first, stop) as frames:
            yield (self._apply(image, by_index.get(index, ()))
                   for index, image in enumerate(frames, start=first))

    def _still_frame(self, source, info, index, target):
        if not self.is_manifest(source):
            return super()._still_frame(source, info, index, target)
        root, checked, layers = self._chain(source)
        super()._still_frame(root, checked, index, target)
        with Image.open(target) as original:
            image = original.convert("RGB")
        patches = [patch for layer in layers for patch in layer.patches if patch.frame_index == index]
        self._apply(image, patches).save(target, format="PNG")

    def capture(self, source, directory, times_ms, *, timeline_duration_ms=None):
        source, info = self._info(source)
        root = self._audio_source(source)
        container_end = self.media.probe(root)["duration_ms"]
        if timeline_duration_ms is not None:
            self._ms(timeline_duration_ms, "timeline_duration_ms")
            container_end = timeline_duration_ms
        end = min(container_end, info["duration_ms"] + math.ceil(1000 / info["fps"]) + 50)
        if not isinstance(times_ms, list) or not 1 <= len(times_ms) <= self.media.max_frames:
            raise ValueError("Capture count exceeds configured max_frames")
        for value in times_ms:
            self._ms(value, "time_ms")
            if not 0 <= value < end:
                raise ValueError("Capture time is outside source duration")
        digest = file_sha(source)
        layers = []
        root_sha = digest
        if self.is_manifest(source):
            root_path, _, layers = self._chain(source)
            root_sha = file_sha(root_path)
        if self._decoder_identity is None:
            version = self.media._run([self.media.ffmpeg, "-version"], capture=True)
            self._decoder_identity = hashlib.sha256(version).hexdigest()
        decoder = {"revision": 1, "format": "rgb24", "ffmpeg_build_sha256": self._decoder_identity,
                   "clock": info["rate"], "geometry": [info["width"], info["height"]]}
        frames = []
        with self.workspace.measure("capture", cache_hits=0, bytes_written=0) as metric:
            for value in times_ms:
                index = sample_index(value, info["rate"], info["frames"])
                effects = [{"rectangle": p.rectangle, "sha256": p.image.sha256}
                           for layer in layers for p in layer.patches if p.frame_index == index]
                frame_source = identity({"root": root_sha, "effects": effects}) if effects else root_sha
                path, sha, hit = self.workspace.frame(
                    frame_source, index, decoder, lambda target: self._still_frame(source, info, index, target))
                metric["cache_hits"] += hit
                metric["bytes_written"] += 0 if hit else path.stat().st_size
                frames.append({"path": str(path), "sha256": sha, "time_ms": value,
                               "base_sha256": digest,
                               "frame_index": index, "sampled_time_ms": index * 1000 / info["fps"],
                               "next_frame_time_ms": (index + 1) * 1000 / info["fps"],
                               "exclusive_end_ms": int((index + 1) * 1000 / info["fps"])})
            if file_sha(source) != digest:
                raise MediaError("Source changed while capturing evidence")
        return frames

    def _target(self, source, target, *other_inputs):
        target = self.workspace.confined(target)
        if not self.is_manifest(target):
            return super()._target(source, target, *other_inputs)
        if target in {Path(p).resolve() for p in (source, *other_inputs)} or target.exists():
            raise ValueError("Patch target must be a new immutable artifact")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _render_window(self, source, target, info, transform, start_ms, end_ms):
        first, stop = frame_window(start_ms, end_ms, info["rate"])
        # Rounded container milliseconds can extend beyond the last native frame.
        return self._render(source, target, info, transform,
                            frame_range=(first, min(stop, info["frames"])))

    def _render(self, source, target, info, transform, *, output_limit=None, frame_range=None):
        with self.workspace.local_write():
            return self._render_serial(source, target, info, transform,
                                       output_limit=output_limit, frame_range=frame_range)

    def _render_serial(self, source, target, info, transform, *, output_limit=None, frame_range=None):
        if not self.is_manifest(target):
            self.workspace.preflight(target, min(self.MAX_OUTPUT_BYTES,
                                                info["width"] * info["height"] * 3 * info["frames"]))
            with self.workspace.measure("render") as metric:
                result = super()._render(source, target, info, transform)
                metric["bytes_written"] = Path(result).stat().st_size
                return result
        if self.is_manifest(source) and len(self._chain(source)[2]) >= self.CHECKPOINT_DEPTH:
            source, _ = self.checkpoint(source)
        first, stop = frame_range if frame_range is not None else (0, info["frames"])
        if (type(first) is not int or type(stop) is not int
                or not 0 <= first < stop <= info["frames"]):
            raise ValueError("Sparse edit has no native frames or exceeds the source")
        target = self._target(source, target)
        before = file_sha(source)
        limit = min(self.MAX_OUTPUT_BYTES, self.media.max_source_bytes,
                    output_limit if output_limit is not None else self.MAX_OUTPUT_BYTES)
        self.workspace.preflight(target, min(limit, 2 * 1024**2 + (stop - first) * (
            info["width"] * info["height"] * 3 + 64 * 1024)))
        assets = target.with_suffix(".frames")
        patches, total = [], 0
        with self.workspace.measure("patch", source_frames=info["frames"],
                                    eligible_frames=stop - first, transferred_frames=0) as metric, TemporaryDirectory(
                prefix="patch-", dir=target.parent) as scratch:
            started = time.monotonic()
            reader = (self._decoded(source, info) if frame_range is None
                      else self._decoded_window(source, info, first, stop))
            with reader as frames:
                for index, image in enumerate(frames, start=first):
                    metric["transferred_frames"] += 1
                    original = image.copy()
                    transform(image, index * 1000 / info["fps"])
                    rectangle = ImageChops.difference(original, image).getbbox()
                    if rectangle:
                        path = Path(scratch) / f"{index}.png"
                        image.crop(rectangle).save(path, format="PNG")
                        total += path.stat().st_size
                        if total >= limit:
                            raise MediaError("Sparse patches exceeded output byte limit")
                        patches.append(FramePatch(frame_index=index, rectangle=rectangle,
                            image=FileRef(path=str(assets / path.name), sha256=file_sha(path))))
                    if time.monotonic() - started > self.media.timeout:
                        raise MediaError("Sparse edit exceeded timeout")
            if file_sha(source) != before:
                raise MediaError("Base changed during sparse editing")
            if metric["transferred_frames"] != stop - first:
                raise MediaError("Sparse decoder returned an incomplete native frame window")
            if self.is_manifest(source):
                self._chain(source)
            manifest = PatchManifest(base=FileRef(path=str(Path(source).resolve()), sha256=before),
                width=info["width"], height=info["height"], frames=info["frames"],
                rate=info["rate"], patches=patches)
            if len(manifest.model_dump_json().encode()) > min(2 * 1024**2, limit - total):
                raise MediaError("Sparse manifest exceeded metadata or output byte limit")
            if assets.exists():
                raise MediaError("Unsettled patch assets require reconciliation")
            os.replace(scratch, assets)
            publish_json(target, manifest.model_dump())
            metric.update(bytes_written=total + target.stat().st_size, changed_frames=len(patches))
        return target

    def _decoded_sha(self, source):
        _, info = self._info(source)
        digest = hashlib.sha256()
        with self._decoded(source, info) as frames:
            for image in frames:
                digest.update(image.tobytes())
        return digest.hexdigest()

    def _audio_sha(self, source):
        root = self._audio_source(source)
        return self.media._run(self._base() + ["-i", root, "-map", "0:a?", "-c:a", "copy",
                                             "-vn", "-f", "hash", "-hash", "sha256", "-"], capture=True)

    def materialize(self, source, target):
        source, info = self._info(source)
        target = self._target(source, target)
        receipt_path = target.with_suffix(target.suffix + ".representation.json")
        if target.exists():
            if not receipt_path.is_file():
                raise MediaError("Unsettled materialization requires original-result reconciliation")
            receipt = json.loads(receipt_path.read_bytes())
            if (receipt.get("revision") != 1 or receipt.get("input_sha256") != file_sha(source)
                    or receipt.get("output_sha256") != file_sha(target)
                    or receipt.get("decoded_rgb_sha256") != self._decoded_sha(source)
                    or self._decoded_sha(target) != receipt["decoded_rgb_sha256"]
                    or receipt.get("audio_packets_preserved") is not True
                    or (info["has_audio"] and self._audio_sha(source) != self._audio_sha(target))):
                raise MediaError("Materialized representation failed identity verification")
            return target, receipt

        with self.workspace.measure("materialize") as metric:
            before = file_sha(source)
            expected = self._decoded_sha(source)
            super_result = self._render(source, target, info, lambda image, timestamp: None)
            if (file_sha(source) != before or self._decoded_sha(super_result) != expected
                    or (info["has_audio"] and self._audio_sha(source) != self._audio_sha(super_result))):
                raise MediaError("Materialization did not preserve decoded pixels and audio packets")
            receipt = {"revision": 1, "input_sha256": before, "decoded_rgb_sha256": expected,
                       "output_sha256": file_sha(super_result), "audio_packets_preserved": True,
                       "quality_passed": False}
            publish_json(receipt_path, receipt)
            metric["bytes_written"] = target.stat().st_size
            return target, receipt

    def checkpoint(self, source):
        """Flatten decoding dependencies, preserving all historical artifacts."""
        before = file_sha(source)
        target = self.workspace.root / "checkpoints" / f"{before}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        lock = target.with_suffix(".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise MediaError("Checkpoint publication is unsettled; reconcile original operation") from None
        try:
            os.close(descriptor)
            result, receipt = self.materialize(source, target)
            if file_sha(source) != before:
                raise MediaError("Checkpoint source changed")
            return result, receipt
        finally:
            lock.unlink()

    def clip(self, source, target, start_ms, end_ms, region=None):
        if not self.is_manifest(source):
            return super().clip(source, target, start_ms, end_ms, region)
        target = self._target(source, target)
        with TemporaryDirectory(prefix="clip-input-", dir=target.parent) as scratch:
            materialized, _ = self.materialize(source, Path(scratch) / "source.mp4")
            return super().clip(materialized, target, start_ms, end_ms, region)

    def clip_frames(self, source, target, first, count):
        if not self.is_manifest(source):
            return super().clip_frames(source, target, first, count)
        target = self._target(source, target)
        with TemporaryDirectory(prefix="clip-input-", dir=target.parent) as scratch:
            materialized, _ = self.materialize(source, Path(scratch) / "source.mp4")
            return super().clip_frames(materialized, target, first, count)


def visual_for(executor, task):
    profile = ExecutionProfile.model_validate(task.get("execution_snapshot", {}))
    root = executor.service.settings.runtime / "tasks" / task["id"]
    sources = [executor.service.checked_path(executor.store.require("asset", asset_id))
               for asset_id in task["asset_ids"]]
    return EfficientVisualMedia(executor.media, ExecutionWorkspace(root, profile), sources,
                                task.get("rework_inputs", []))
