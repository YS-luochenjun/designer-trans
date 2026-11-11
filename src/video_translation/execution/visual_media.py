"""Bounded, local visual edits. Rectangles are normalized; time windows are half-open.

No text, color, path or motion expression is interpolated into an FFmpeg filter.
Intermediate outputs use lossless RGB H.264: exact decoded pixels outside an edit
are preferred over small files. The watchdog rejects outputs above 512 MiB.
Only ordinary constant-frame-rate, square-pixel, unrotated video is supported.
"""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
import threading
import time

from PIL import Image, ImageColor, ImageDraw

from .media import MediaError, MediaTools
from .frame_clock import frame_window, sample_index


class MediaDecodeError(MediaError):
    """A failed decode, with bounded local-process evidence for recovery."""

    def __init__(self, returncode, decoded, expected, diagnostic):
        self.details = {"returncode": returncode, "decoded_frames": decoded,
                        "expected_frames": expected, "stderr": diagnostic[-1200:]}
        super().__init__(
            f"Source decoding failed: exit={returncode}, decoded={decoded}/{expected}; "
            f"{self.details['stderr'] or 'no FFmpeg diagnostic'}")

    @property
    def retryable_resource(self):
        diagnostic = self.details["stderr"].lower()
        return self.details["returncode"] != 0 and any(marker in diagnostic for marker in (
            "cannot allocate memory", "resource temporarily unavailable",
            "paging file is too small", "not enough memory",
        )) and not any(marker in diagnostic for marker in (
            "invalid data", "corrupt", "error while decoding",
        ))


class VisualMedia:
    MAX_OUTPUT_BYTES = 512 * 1024**2
    MAX_PIXELS = 8_294_400
    MAX_VIDEO_FRAMES = 108_000

    def __init__(self, media: MediaTools):
        self.media = media
        if not math.isfinite(float(media.timeout)) or media.timeout <= 0 or media.max_source_bytes <= 0:
            raise ValueError("Media timeout and byte limit must be positive and finite")

    @staticmethod
    def _number(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        return value

    @classmethod
    def _ms(cls, value, name):
        cls._number(value, name)
        if not isinstance(value, int):
            raise ValueError(f"{name} must be integer milliseconds")
        return value

    def _info(self, source):
        source = self.media._source(source)
        raw = self.media._run([self.media.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
                               "-show_streams", "-show_format", "-of", "json", source], capture=True)
        try:
            data = json.loads(raw)
            stream = next(s for s in data["streams"] if s["codec_type"] == "video")
            fps = Fraction(stream["avg_frame_rate"])
            nominal = Fraction(stream["r_frame_rate"])
            width, height = int(stream["width"]), int(stream["height"])
            duration = float(stream.get("duration", data["format"]["duration"]))
            if not (math.isfinite(duration) and 0 < duration <= 3600 and 0 < fps <= 120):
                raise ValueError()
            if fps != nominal or stream.get("sample_aspect_ratio", "1:1") not in {"1:1", "N/A"}:
                raise ValueError()
            if any(s.get("rotation", 0) for s in stream.get("side_data_list", [])):
                raise ValueError()
            if float(stream.get("tags", {}).get("rotate", 0)) != 0:
                raise ValueError()
            if not (width > 0 and height > 0 and width * height <= self.MAX_PIXELS):
                raise ValueError()
            frames = round(duration * fps)
            if not 0 < frames <= self.MAX_VIDEO_FRAMES:
                raise ValueError()
            return source, {"duration_ms": round(duration * 1000), "width": width, "height": height,
                            "fps": float(fps), "rate": str(fps), "frames": frames,
                            "has_audio": any(s["codec_type"] == "audio" for s in data["streams"])}
        except (ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError):
            raise ValueError("Unsupported video: finite CFR, square pixels, no rotation, and bounded size required") from None

    def _window(self, info, start_ms, end_ms):
        self._ms(start_ms, "start_ms")
        self._ms(end_ms, "end_ms")
        if not 0 <= start_ms < end_ms <= info["duration_ms"]:
            raise ValueError("Time window is outside source duration")

    def _rect(self, info, region):
        if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"}:
            raise ValueError("region requires normalized x, y, width, height")
        x, y, w, h = [self._number(region[k], f"region.{k}") for k in ("x", "y", "width", "height")]
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
            raise ValueError("region is outside normalized frame")
        left, top = round(x * info["width"]), round(y * info["height"])
        right, bottom = round((x + w) * info["width"]), round((y + h) * info["height"])
        if right <= left or bottom <= top:
            raise ValueError("region is smaller than one pixel")
        return left, top, right, bottom

    def rectangle(self, info: dict, region: dict) -> dict:
        """Return shared rounded pixel bounds for controller-side asset sizing.

        Rounding applies to both normalized edges, not independently to width.
        info is the width/height dictionary returned by MediaTools.probe.
        """
        x, y, right, bottom = self._rect(info, region)
        return {"x": x, "y": y, "width": right - x, "height": bottom - y}

    def pixel_region(self, region: dict, width: int, height: int) -> tuple[int, int, int, int]:
        """Controller-friendly tuple form of rectangle, using identical edge rounding."""
        result = self.rectangle({"width": width, "height": height}, region)
        return result["x"], result["y"], result["width"], result["height"]

    def letter_layer(self, text, output_png, *, width, height, style) -> Path:
        """Persist an independently reviewable transparent text layer without a video."""
        self._ms(width, "width")
        self._ms(height, "height")
        if width <= 0 or height <= 0 or width * height > self.MAX_PIXELS:
            raise ValueError("Letter layer dimensions exceed pixel limit")
        output = Path(output_png).resolve()
        if output.suffix.lower() != ".png":
            raise ValueError("Letter layer output must be PNG")
        if self.media.font:
            font = Path(self.media.font).resolve()
            if output == font or (output.exists() and font.exists() and os.path.samefile(output, font)):
                raise ValueError("Output must differ from trusted font")
        image = self._text_image({"text": text, "style": style}, (width, height))
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="visual-letter-", dir=output.parent) as scratch:
            temporary = Path(scratch) / "layer.png"
            image.save(temporary, format="PNG")
            if temporary.stat().st_size >= min(self.MAX_OUTPUT_BYTES, self.media.max_source_bytes):
                raise MediaError("Letter layer exceeded output byte limit")
            os.replace(temporary, output)
        return output

    def _target(self, source, target, *other_inputs):
        target = Path(target).resolve()
        for original in (source, *other_inputs):
            original = Path(original).resolve()
            if target == original or (target.exists() and os.path.samefile(target, original)):
                raise ValueError("Output must differ from every input")
        if target.suffix.lower() not in {".mp4", ".mkv", ".mov"}:
            raise ValueError("Output must be mp4, mkv or mov")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    @contextmanager
    def _guard(self, processes, outputs, logs=()):
        """Kill even a blocked pipe read/write when time or artifact limits are exceeded."""
        stopped = threading.Event()
        failure = []
        deadline = time.monotonic() + float(self.media.timeout)
        limit = min(self.MAX_OUTPUT_BYTES, self.media.max_source_bytes)

        def watch():
            while not stopped.wait(.05):
                if time.monotonic() > deadline:
                    failure.append("Visual media exceeded timeout")
                elif any(p.exists() and p.stat().st_size >= limit for p in outputs):
                    failure.append("Visual media exceeded output byte limit")
                elif any(os.fstat(log.fileno()).st_size >= 1024**2 for log in logs):
                    failure.append("Visual media exceeded diagnostics byte limit")
                if failure:
                    for process in list(processes):
                        if process.poll() is None:
                            process.kill()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            yield
            if failure:
                raise MediaError(failure[0])
            if any(p.exists() and p.stat().st_size >= limit for p in outputs):
                raise MediaError("Visual media exceeded output byte limit")
        except Exception as exc:
            if failure:
                raise MediaError(failure[0]) from exc
            if isinstance(exc, OSError):
                raise MediaError("Visual media process or pipe failed") from exc
            raise
        finally:
            stopped.set()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            watcher.join(timeout=1)

    def _base(self):
        return [self.media.ffmpeg, "-v", "error", "-nostdin", "-y", "-threads", "1",
                "-filter_threads", "1", "-protocol_whitelist", "file,pipe"]

    def _limits(self, seconds):
        return ["-t", str(seconds), "-fs", str(min(self.MAX_OUTPUT_BYTES, self.media.max_source_bytes))]

    def _run(self, args, output):
        with TemporaryFile() as errors:
            process = subprocess.Popen([str(a) for a in args], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=errors)
            with self._guard([process], [output], [errors]):
                if process.wait() != 0:
                    raise MediaError("Visual media FFmpeg operation failed")
            if not output.is_file() or not output.stat().st_size:
                raise MediaError("Visual media produced no output")

    def _still(self, source, info, time_ms, target):
        self._ms(time_ms, "time_ms")
        if not 0 <= time_ms < info["duration_ms"]:
            raise ValueError("Capture time is outside source duration")
        self._still_frame(source, info, sample_index(time_ms, info["rate"], info["frames"]), target)

    def _still_frame(self, source, info, index, target):
        self._run(self._base() + ["-i", source, "-map", "0:v:0", "-an",
            "-vf", f"select=eq(n\\,{index}),setpts=PTS-STARTPTS", "-frames:v", "1"]
            + self._limits(1 / info["fps"]) + [target], target)

    def capture(self, source, directory, times_ms: list[int], *, timeline_duration_ms=None) -> list[dict]:
        source, info = self._info(source)
        # Audio packet padding can extend the container slightly past the final video frame.
        container_duration = self.media.probe(source)["duration_ms"]
        # Comparison videos share the original timeline, but remuxed audio may
        # have shorter packet padding. Keep the bounded tail allowance identical.
        if timeline_duration_ms is not None:
            self._ms(timeline_duration_ms, "timeline_duration_ms")
            container_duration = timeline_duration_ms
        capture_end = min(container_duration, info["duration_ms"] + math.ceil(1000 / info["fps"]) + 50)
        if not isinstance(times_ms, list) or not 1 <= len(times_ms) <= self.media.max_frames:
            raise ValueError("Capture count exceeds configured max_frames")
        for value in times_ms:
            self._ms(value, "time_ms")
            if not 0 <= value < capture_end:
                raise ValueError("Capture time is outside source duration")
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        result = []
        with TemporaryDirectory(prefix="visual-capture-", dir=directory) as scratch:
            for index, value in enumerate(times_ms):
                path = directory / f"capture-{index:03d}.png"
                if path == source or (path.exists() and os.path.samefile(path, source)):
                    raise ValueError("Output must differ from source")
                temporary = Path(scratch) / path.name
                frame_index = sample_index(value, info["rate"], info["frames"])
                self._still_frame(source, info, frame_index, temporary)
                os.replace(temporary, path)
                result.append({"path": str(path), "time_ms": value,
                               "frame_index": frame_index,
                               "sampled_time_ms": frame_index * 1000 / info["fps"],
                               "next_frame_time_ms": (frame_index + 1) * 1000 / info["fps"],
                               "exclusive_end_ms": math.floor((frame_index + 1) * 1000 / info["fps"])})
        return result

    def clip(self, source, target, start_ms, end_ms, region=None) -> dict:
        source, info = self._info(source)
        self._window(info, start_ms, end_ms)
        target = self._target(source, target)
        filters = []
        if region is not None:
            x, y, right, bottom = self._rect(info, region)
            filters = ["-vf", f"format=rgb24,crop={right-x}:{bottom-y}:{x}:{y}:exact=1"]
        with TemporaryDirectory(prefix="visual-clip-", dir=target.parent) as scratch:
            output = Path(scratch) / target.name
            self._run(self._base() + ["-ss", str(start_ms / 1000), "-i", source, "-map", "0:v:0", "-an"]
                      + filters + ["-c:v", "libx264rgb", "-crf", "0", "-preset", "ultrafast",
                                   "-pix_fmt", "rgb24", "-fps_mode", "passthrough"]
                      + self._limits((end_ms-start_ms) / 1000) + [output], output)
            _, result = self._info(output)
            os.replace(output, target)
        return {k: v for k, v in result.items() if k not in {"rate", "frames"}} | {
            "path": str(target), "start_ms": start_ms, "end_ms": end_ms}

    def clip_frames(self, source, target, first, count):
        """Trim model padding on the decoded frame clock, not rounded milliseconds."""
        source, info = self._info(source)
        if (type(first) is not int or type(count) is not int
                or first < 0 or count <= 0 or first + count > info["frames"]):
            raise ValueError("Frame window is outside source")
        target = self._target(source, target)
        with TemporaryDirectory(prefix="visual-frame-clip-", dir=target.parent) as scratch:
            output = Path(scratch) / target.name
            self._run(self._base() + ["-i", source, "-map", "0:v:0", "-an", "-vf",
                f"trim=start_frame={first}:end_frame={first+count},setpts=PTS-STARTPTS",
                "-c:v", "libx264rgb", "-crf", "0", "-preset", "ultrafast",
                "-pix_fmt", "rgb24", "-fps_mode", "passthrough"]
                + self._limits(count / info["fps"]) + [output], output)
            _, result = self._info(output)
            if result["frames"] != count or result["rate"] != info["rate"]:
                raise MediaError("Frame trim changed frame count or rate")
            os.replace(output, target)
        return target

    @contextmanager
    def _decoded(self, source, info):
        """Keep one decoded frame in memory; never materialize a video on disk."""
        with TemporaryFile() as errors:
            process = subprocess.Popen([str(a) for a in self._base() + ["-i", source, "-map", "0:v:0",
                                        "-an", "-sn", "-dn", "-pix_fmt", "rgb24", "-fps_mode", "passthrough",
                                        "-t", str(info["duration_ms"] / 1000), "-threads", "1",
                                        "-f", "rawvideo", "pipe:1"]],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
            size = info["width"] * info["height"] * 3

            def frames():
                count = 0
                while True:
                    raw = process.stdout.read(size)
                    if not raw:
                        break
                    count += 1
                    if len(raw) != size or count > info["frames"]:
                        raise MediaError("Invalid decoded frame count or incomplete frame")
                    yield Image.frombytes("RGB", (info["width"], info["height"]), raw)
                returncode = process.wait()
                if returncode != 0 or count != info["frames"]:
                    errors.seek(0, os.SEEK_END)
                    errors.seek(max(0, errors.tell() - 4096))
                    diagnostic = errors.read().decode("utf-8", errors="replace")
                    diagnostic = diagnostic.replace(str(source), "<source>")
                    raise MediaDecodeError(returncode, count, info["frames"], diagnostic)

            try:
                with self._guard([process], [], [errors]):
                    yield frames()
            finally:
                process.stdout.close()

    def _render(self, source, target, info, transform):
        source, target = Path(source), Path(target)

        def fingerprint():
            with source.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

        source_sha = fingerprint()
        receipt_path = target.with_suffix(target.suffix + ".decode-recovery.json")
        try:
            return self._render_once(source, target, info, transform)
        except MediaDecodeError as exc:
            if not exc.retryable_resource or fingerprint() != source_sha:
                raise
            # A restart cannot reset the one-retry allowance for this artifact.
            if receipt_path.exists():
                previous = json.loads(receipt_path.read_text(encoding="utf-8"))
                if previous.get("source_sha256") == source_sha:
                    raise
            receipt = {"source_sha256": source_sha, "failure": exc.details,
                       "attempts": 2, "status": "retrying_local_decode"}
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            try:
                result = self._render_once(source, target, info, transform)
            except Exception as retry_error:
                receipt.update(status="failed", retry_error=str(retry_error)[:1500])
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                raise
            receipt["status"] = "succeeded"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            return result

    def _audio_source(self, source):
        return source

    def _render_once(self, source, target, info, transform):
        with TemporaryDirectory(prefix="visual-render-", dir=target.parent) as scratch, TemporaryFile() as errors:
            output = Path(scratch) / target.name
            args = self._base() + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                                   f"{info['width']}x{info['height']}", "-r", info["rate"], "-i", "pipe:0",
                                   "-protocol_whitelist", "file,pipe", "-i", self._audio_source(source),
                                   "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "-1",
                                   "-c:v", "libx264rgb", "-crf", "0", "-preset", "ultrafast", "-threads", "1",
                                   "-pix_fmt", "rgb24", "-c:a", "copy", "-fps_mode", "passthrough"]
            args += self._limits(info["duration_ms"] / 1000) + [output]
            process = subprocess.Popen([str(a) for a in args], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=errors)
            try:
                with self._guard([process], [output], [errors]), self._decoded(source, info) as frames:
                    for index, image in enumerate(frames):
                        transform(image, index * 1000 / info["fps"])
                        process.stdin.write(image.tobytes())
                    process.stdin.close()
                    if process.wait() != 0:
                        raise MediaError("Visual media encoding failed")
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass  # Preserve the original validation/watchdog error after process termination.
            _, result = self._info(output)
            if any(result[key] != info[key] for key in ("width", "height", "rate", "frames", "has_audio")):
                raise MediaError("Rendered media failed geometry, duration or audio validation")
            os.replace(output, target)
        return target

    @staticmethod
    def _color(value):
        if not isinstance(value, str) or len(value) not in {7, 9} or not value.startswith("#"):
            raise ValueError("Colors must be #RRGGBB or #RRGGBBAA")
        try:
            int(value[1:], 16)
            return ImageColor.getcolor(value, "RGBA")
        except ValueError:
            raise ValueError("Invalid color") from None

    def _image(self, path, size, *, opaque=False):
        path = self.media._source(path)
        try:
            with Image.open(path) as image:
                if image.size != size or image.width * image.height > self.MAX_PIXELS:
                    raise ValueError("Image dimensions must match region dimensions")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Only single-frame images are supported")
                image.load()
                result = image.convert("RGBA")
                if opaque and result.getextrema()[3] != (255, 255):
                    raise ValueError("Restoration image must be opaque")
                return result
        except (OSError, Image.DecompressionBombError):
            raise ValueError("Image is invalid or exceeds pixel limit") from None

    @staticmethod
    def _simple_background(image, color):
        # A caller's flat-color assertion must also fit the actual source. Check
        # every affected frame, allowing sparse text but refusing textured areas.
        # This pixel gate is not semantic occlusion detection: callers must also
        # establish that the area is a simple background, not a moving subject.
        sample = image.copy()
        sample.thumbnail((64, 64))
        pixels = [sample.getpixel((x, y)) for y in range(sample.height) for x in range(sample.width)]
        matches = sum(max(abs(pixel[i] - color[i]) for i in range(3)) <= 12 for pixel in pixels)
        if matches < len(pixels) * .8:
            raise ValueError("flat requires a verified simple source background; use donor/image instead")

    def restore(self, source, target, *, start_ms, end_ms, region, method,
                background="#000000", donor_time_ms=None, donor_region=None, image_path=None) -> Path:
        source, info = self._info(source)
        self._window(info, start_ms, end_ms)
        rect = self._rect(info, region)
        size = (rect[2] - rect[0], rect[3] - rect[1])
        target = self._target(source, target, *([image_path] if image_path is not None else []))
        if method not in {"flat", "donor", "image"}:
            raise ValueError("Unsupported restoration method; complex occlusion requires another capability")
        if method == "flat":
            if image_path is not None or donor_time_ms is not None or donor_region is not None:
                raise ValueError("flat cannot include donor/image inputs")
            color = self._color(background)
            if color[3] != 255:
                raise ValueError("Flat background must be opaque")
            replacement = Image.new("RGB", size, color[:3])
        elif method == "image":
            if image_path is None or donor_time_ms is not None or donor_region is not None:
                raise ValueError("image requires only image_path")
            replacement = self._image(image_path, size, opaque=True).convert("RGB")
        else:
            if donor_time_ms is None or image_path is not None:
                raise ValueError("donor requires donor_time_ms and no image_path")
            donor_rect = self._rect(info, donor_region if donor_region is not None else region)
            if (donor_rect[2] - donor_rect[0], donor_rect[3] - donor_rect[1]) != size:
                raise ValueError("Donor region dimensions must match target region")
            with TemporaryDirectory(prefix="visual-donor-", dir=target.parent) as scratch:
                still = Path(scratch) / "donor.png"
                self._still(source, info, donor_time_ms, still)
                with Image.open(still) as image:
                    replacement = image.convert("RGB").crop(donor_rect)

        def transform(image, timestamp):
            if start_ms <= timestamp < end_ms:
                if method == "flat":
                    self._simple_background(image.crop(rect), color)
                image.paste(replacement, rect[:2])

        return self._render(source, target, info, transform)

    def patch_video(self, source, patch, target, *, start_ms, end_ms, region, blend_core=None) -> Path:
        source, info = self._info(source)
        patch, patch_info = self._info(patch)
        self._window(info, start_ms, end_ms)
        rect = self._rect(info, region)
        mask = None
        if blend_core is not None:
            core = self._rect(info, blend_core)
            if not (rect[0] <= core[0] < core[2] <= rect[2]
                    and rect[1] <= core[1] < core[3] <= rect[3]):
                raise ValueError("Blend core must stay inside patch region")
            width, height = rect[2] - rect[0], rect[3] - rect[1]
            left, top, right, bottom = (core[0] - rect[0], core[1] - rect[1],
                                       core[2] - rect[0], core[3] - rect[1])
            # Fade only the extra context margin. The lettering envelope remains
            # fully replaced so blending cannot restore the removed source text.
            def ramp(position, low, high, size):
                if position < low:
                    return position / low
                if position >= high:
                    return (size - 1 - position) / (size - high)
                return 1.0
            horizontal = [ramp(x, left, right, width) for x in range(width)]
            vertical = [ramp(y, top, bottom, height) for y in range(height)]
            mask = Image.frombytes("L", (width, height), bytes(
                round(255 * min(x, y)) for y in vertical for x in horizontal))
        if (patch_info["width"], patch_info["height"]) != (rect[2] - rect[0], rect[3] - rect[1]):
            raise ValueError("Patch dimensions must match region dimensions")
        first, stop = frame_window(start_ms, end_ms, info["rate"])
        required = stop - first
        if (required <= 0 or patch_info["rate"] != info["rate"] or patch_info["frames"] != required
                or abs(patch_info["duration_ms"] - (end_ms - start_ms)) > 1000 / info["fps"]):
            raise ValueError("Patch duration and fps must match the source time window")
        target = self._target(source, target, patch)
        with self._decoded(patch, patch_info) as frames:
            consumed = 0

            def transform(image, timestamp):
                nonlocal consumed
                if first <= round(timestamp * info["fps"] / 1000) < stop:
                    try:
                        replacement = next(frames)
                    except StopIteration:
                        raise MediaError("Patch ended before its required time window") from None
                    image.paste(replacement, rect[:2], mask)
                    consumed += 1
                    if consumed == required and next(frames, None) is not None:
                        raise MediaError("Patch has unexpected extra frames")
            result = self._render(source, target, info, transform)
        return result

    def _text_image(self, layer, size):
        text = layer.get("text")
        style = layer.get("style", {})
        allowed = {"font_path", "font_size", "color", "outline_color", "outline_width", "shadow_color",
                   "shadow_x", "shadow_y", "align", "bold"}
        if not isinstance(style, dict) or set(style) - allowed:
            raise ValueError("Unsupported text style")
        if not isinstance(text, str) or not 0 < len(text) <= 10000 or not text.strip():
            raise ValueError("Text must be a nonempty string within 10000 characters")
        if style.get("bold", False) is not False:
            raise ValueError("Bind a trusted bold font; synthetic bold is unsupported")
        font_path = style.get("font_path")
        if font_path is not None and (not self.media.font or Path(font_path).resolve() != Path(self.media.font).resolve()):
            raise ValueError("font_path must equal the trusted parent-bound MediaTools.font")
        font_size = self._ms(style.get("font_size", 24), "font_size")
        outline = self._ms(style.get("outline_width", 0), "outline_width")
        shadow_x = self._ms(style.get("shadow_x", 0), "shadow_x")
        shadow_y = self._ms(style.get("shadow_y", 0), "shadow_y")
        align = style.get("align", "center")
        if not (6 <= font_size <= 512 and 0 <= outline <= 32 and abs(shadow_x) <= 64 and abs(shadow_y) <= 64):
            raise ValueError("Text dimensions exceed limits")
        if align not in {"left", "center", "right"}:
            raise ValueError("align must be left, center or right")
        if self.media.font:
            from PIL import ImageFont
            trusted = Path(self.media.font)
            if not trusted.is_file() or trusted.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
                raise ValueError("Trusted font is missing or unsupported")
            try:
                font = ImageFont.truetype(str(trusted), font_size)
            except OSError:
                raise ValueError("Trusted font could not be decoded") from None
        else:
            font = self.media._font(font_size)
        missing = font.getmask(chr(0x10FFFF))
        signature = (missing.size, bytes(missing))
        for character in set(text):
            if not character.isspace():
                glyph = font.getmask(character)
                if (glyph.size, bytes(glyph)) == signature:
                    raise ValueError("Configured font lacks a required glyph")
        color = self._color(style.get("color", "#ffffff"))
        outline_color = self._color(style.get("outline_color", "#000000"))
        shadow_color = self._color(style.get("shadow_color", "#00000000"))
        image = Image.new("RGBA", size)
        draw = ImageDraw.Draw(image)
        bounds = draw.multiline_textbbox((0, 0), text, font=font, align=align, stroke_width=outline)
        left = bounds[0] + min(0, shadow_x)
        top = bounds[1] + min(0, shadow_y)
        width = bounds[2] + max(0, shadow_x) - left
        height = bounds[3] + max(0, shadow_y) - top
        if width > size[0] or height > size[1]:
            raise ValueError("Text does not fit its region; choose a smaller font or another layout")
        offset = {"left": 0, "center": (size[0] - width) // 2, "right": size[0] - width}[align]
        origin = (offset - left, -top)
        if shadow_color[3]:
            draw.multiline_text((origin[0] + shadow_x, origin[1] + shadow_y), text, font=font,
                                fill=shadow_color, align=align, stroke_width=outline, stroke_fill=shadow_color)
        draw.multiline_text(origin, text, font=font, fill=color, align=align,
                            stroke_width=outline, stroke_fill=outline_color)
        return image

    def _motion(self, motion, start, end, size):
        if not isinstance(motion, list) or len(motion) > 64:
            raise ValueError("motion must contain at most 64 keyframes")
        result = []
        previous = -1
        for key in motion:
            if not isinstance(key, dict) or set(key) - {"time_ms", "x", "y", "scale", "opacity"}:
                raise ValueError("Unsupported motion; only translation, scale and opacity are supported")
            timestamp = self._ms(key.get("time_ms"), "motion.time_ms")
            x = self._number(key.get("x"), "motion.x")
            y = self._number(key.get("y"), "motion.y")
            scale = self._number(key.get("scale", 1), "motion.scale")
            opacity = self._number(key.get("opacity", 1), "motion.opacity")
            if not start <= timestamp <= end or timestamp <= previous:
                raise ValueError("Motion keyframe times must increase within layer time window")
            if not (abs(x) <= size[0] * 4 and abs(y) <= size[1] * 4 and .1 <= scale <= 4 and 0 <= opacity <= 1):
                raise ValueError("Motion values exceed limits")
            if round(size[0] * scale) * round(size[1] * scale) > self.MAX_PIXELS:
                raise ValueError("Scaled artwork exceeds pixel limit")
            result.append({"time_ms": timestamp, "x": x, "y": y, "scale": scale, "opacity": opacity})
            previous = timestamp
        return result or [{"time_ms": start, "x": 0, "y": 0, "scale": 1, "opacity": 1}]

    @staticmethod
    def _position(keys, timestamp):
        if timestamp <= keys[0]["time_ms"]:
            return keys[0]
        for left, right in zip(keys, keys[1:]):
            if timestamp <= right["time_ms"]:
                part = (timestamp - left["time_ms"]) / (right["time_ms"] - left["time_ms"])
                return {k: left[k] + (right[k] - left[k]) * part for k in ("x", "y", "scale", "opacity")}
        return keys[-1]

    def _occlusion(self, keys, start, end):
        if not isinstance(keys, list) or len(keys) > 32:
            raise ValueError("Unsupported occlusion: supply at most 32 explicit polygon keyframes")
        result = []
        previous, count = -1, None
        for key in keys:
            if not isinstance(key, dict) or set(key) != {"time_ms", "points"}:
                raise ValueError("Occlusion keyframes require time_ms and points")
            timestamp = self._ms(key["time_ms"], "occlusion.time_ms")
            if not start <= timestamp <= end or timestamp <= previous:
                raise ValueError("Occlusion keyframe times must increase within the layer window")
            points = key["points"]
            if not isinstance(points, list) or not 3 <= len(points) <= 32:
                raise ValueError("Occlusion polygons require 3..32 explicit points")
            if count is not None and len(points) != count:
                raise ValueError("Occlusion keyframes must use the same point count and correspondence")
            polygon = []
            for point in points:
                if not isinstance(point, dict) or set(point) != {"x", "y"}:
                    raise ValueError("Occlusion points require normalized x and y")
                x = self._number(point["x"], "occlusion.x")
                y = self._number(point["y"], "occlusion.y")
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    raise ValueError("Occlusion point is outside the source frame")
                polygon.append((x, y))
            if len(set(polygon)) != len(polygon) or abs(sum(
                a[0] * b[1] - b[0] * a[1] for a, b in zip(polygon, polygon[1:] + polygon[:1])
            )) <= 1e-12:
                raise ValueError("Occlusion polygon is degenerate")
            result.append({"time_ms": timestamp, "points": polygon})
            count, previous = len(points), timestamp
        return result

    @staticmethod
    def _polygon_at(keys, timestamp):
        if timestamp <= keys[0]["time_ms"]:
            return keys[0]["points"]
        for left, right in zip(keys, keys[1:]):
            if timestamp <= right["time_ms"]:
                part = (timestamp - left["time_ms"]) / (right["time_ms"] - left["time_ms"])
                return [(a[0] + (b[0] - a[0]) * part, a[1] + (b[1] - a[1]) * part)
                        for a, b in zip(left["points"], right["points"])]
        return keys[-1]["points"]

    def typography(self, source, target, layers: list[dict]) -> Path:
        """Composite ordered transparent layers, clipped to each declared region.

        Motion time_ms is source-absolute. x/y are pixel offsets from the region
        origin; scale is uniform; opacity is 0..1. Numeric keyframes interpolate
        linearly and hold outside the first/last keyframe inside the layer window.
        A supplied font_path must be exactly the parent-bound media.font path.
        Optional occlusion=[{time_ms, points:[{x,y}, ...]}] supplies source-frame
        normalized foreground polygons. Corresponding vertices interpolate; the
        polygon clears lettering alpha to expose the clean base. This is an
        explicit keyframed mask, not inferred segmentation or automatic tracking.
        """
        source, info = self._info(source)
        if not isinstance(layers, list) or not 1 <= len(layers) <= 32:
            raise ValueError("typography requires 1..32 layers")
        prepared, inputs = [], []
        total_pixels = 0
        for layer in layers:
            if not isinstance(layer, dict) or set(layer) - {"text", "artwork_path", "start_ms", "end_ms",
                                                           "region", "style", "motion", "occlusion"}:
                raise ValueError("Unsupported typography fields or complex occlusion")
            start, end = layer.get("start_ms"), layer.get("end_ms")
            self._window(info, start, end)
            rect = self._rect(info, layer.get("region"))
            size = (rect[2] - rect[0], rect[3] - rect[1])
            total_pixels += size[0] * size[1]
            if total_pixels > self.MAX_PIXELS * 4:
                raise ValueError("Combined typography layers exceed memory limit")
            artwork = layer.get("artwork_path")
            if artwork is not None:
                if layer.get("text") or layer.get("style"):
                    raise ValueError("Choose artwork or styled text for each layer")
                image = self._image(artwork, size)
                inputs.append(artwork)
            else:
                image = self._text_image(layer, size)
            keys = self._motion(layer.get("motion", []), start, end, size)
            occlusion = self._occlusion(layer.get("occlusion", []), start, end)
            prepared.append((start, end, rect, image, keys, occlusion))
        target = self._target(source, target, *inputs)

        def transform(frame, timestamp):
            for start, end, rect, artwork, keys, occlusion in prepared:
                if not start <= timestamp < end:
                    continue
                position = self._position(keys, timestamp)
                width, height = artwork.size
                size = (max(1, round(width * position["scale"])), max(1, round(height * position["scale"])))
                image = artwork.resize(size, Image.Resampling.LANCZOS) if size != artwork.size else artwork.copy()
                if position["opacity"] != 1:
                    image.putalpha(image.getchannel("A").point(lambda alpha: round(alpha * position["opacity"])))
                canvas = Image.new("RGBA", artwork.size)
                canvas.alpha_composite(image, (round(position["x"]), round(position["y"])))
                if occlusion:
                    polygon = [(round(x * info["width"]) - rect[0], round(y * info["height"]) - rect[1])
                               for x, y in self._polygon_at(occlusion, timestamp)]
                    ImageDraw.Draw(canvas).polygon(polygon, fill=(0, 0, 0, 0))
                frame.paste(canvas, rect[:2], canvas)

        return self._render(source, target, info, transform)
