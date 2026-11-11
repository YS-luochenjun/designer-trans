"""Local FFmpeg media operations with explicit timeouts and bounded artifacts."""

from __future__ import annotations
import json
import math
import os
import subprocess
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from ..integrations.agents.errors import CapabilityUnavailable


class MediaError(RuntimeError):
    pass


class MediaTools:
    def __init__(
        self,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        *,
        timeout=180,
        font=None,
        max_source_bytes=2 * 1024**3,
        max_frames=12,
    ):
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.timeout, self.font = timeout, font
        self.max_source_bytes, self.max_frames = max_source_bytes, max_frames

    def _source(self, path):
        path = Path(path).resolve()
        if not path.is_file() or path.stat().st_size > self.max_source_bytes:
            raise ValueError("Media source missing or exceeds size limit")
        return path

    def _output(self, source, output):
        output = Path(output).resolve()
        if output == Path(source).resolve():
            raise ValueError("Output must differ from source")
        output.parent.mkdir(parents=True, exist_ok=True)
        return output

    def _run(self, args, *, capture=False, cwd=None):
        with TemporaryFile() as stdout, TemporaryFile() as stderr:
            try:
                result = subprocess.run(
                    [str(x) for x in args],
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=self.timeout,
                    cwd=cwd,
                    check=False,
                )
            except FileNotFoundError:
                raise CapabilityUnavailable("Configured media executable was not found") from None
            except subprocess.TimeoutExpired:
                raise MediaError("Media command exceeded timeout") from None
            if result.returncode:
                # Only static classifications escape; raw diagnostics may contain paths or metadata.
                stderr.seek(0, os.SEEK_END)
                stderr.seek(max(0, stderr.tell() - 4096))
                diagnostic = stderr.read(4096).decode("utf-8", errors="replace").lower()
                reason = "unclassified media error"
                categories = {
                    "no space left on device": "storage full",
                    "disk full": "storage full",
                    "permission denied": "file access denied",
                    "invalid data found": "invalid media data",
                    "cannot allocate memory": "insufficient memory",
                    "resource temporarily unavailable": "temporary resource exhaustion",
                    "invalid argument": "invalid media arguments",
                }
                for marker, category in categories.items():
                    if marker in diagnostic:
                        reason = category
                        break
                code = result.returncode
                if 2**31 <= code < 2**32:
                    code -= 2**32
                raise MediaError(f"Media command failed with exit code {code}: {reason}")
            if capture:
                stdout.seek(0)
                data = stdout.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise MediaError("Media metadata exceeds size limit")
                return data

    def probe(self, source: Path) -> dict:
        source = self._source(source)
        raw = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height,avg_frame_rate,duration",
                "-of",
                "json",
                source,
            ],
            capture=True,
        )
        try:
            data = json.loads(raw)
            streams = data.get("streams", [])
            video = next((s for s in streams if s.get("codec_type") == "video"), {})
            duration = float(data.get("format", {}).get("duration", video.get("duration", 0)))
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError()
            rate = video.get("avg_frame_rate", "0/1")
            fps = float(Fraction(rate)) if rate not in {"0/0", "N/A"} else 0.0
            return {
                "duration_ms": round(duration * 1000),
                "width": int(video.get("width", 0)),
                "height": int(video.get("height", 0)),
                "has_audio": any(s.get("codec_type") == "audio" for s in streams),
                "fps": fps,
            }
        except (ValueError, TypeError, KeyError, ZeroDivisionError):
            raise MediaError("Media metadata is invalid or has no finite duration") from None

    def prepare(self, source: Path, directory: Path) -> dict:
        source = self._source(source)
        info = self.probe(source)
        if not info["width"] or info["width"] * info["height"] > 33_177_600:
            raise ValueError("Video missing or exceeds pixel limit")
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        frames = []
        count = min(self.max_frames, max(1, math.ceil(info["duration_ms"] / 3000)))
        if count < 1:
            raise ValueError("max_frames must be positive")
        with TemporaryDirectory(prefix="prepare-", dir=directory) as scratch:
            scratch = Path(scratch)
            for index in range(count):
                time_ms = round(index * info["duration_ms"] / count)
                name = f"frame-{index:03d}.png"
                self._run(
                    [
                        self.ffmpeg,
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-ss",
                        str(time_ms / 1000),
                        "-i",
                        source,
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale='min(1280,iw)':-2",
                        scratch / name,
                    ]
                )
                frames.append({"path": str(directory / name), "time_ms": time_ms})
            if info["has_audio"]:
                self._run(
                    [
                        self.ffmpeg,
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-i",
                        source,
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "pcm_s16le",
                        scratch / "audio.wav",
                    ]
                )
            for item in scratch.iterdir():
                os.replace(item, directory / item.name)
        result = {"media": info, "frames": frames}
        if info["has_audio"]:
            result["audio_path"] = str(directory / "audio.wav")
        return result

    def extract_original_audio(self, source: Path, output: Path):
        source = self._source(source)
        output = self._output(source, output)
        if not self.probe(source)["has_audio"]:
            raise ValueError("Source has no audio track to separate")
        with TemporaryDirectory(prefix="source-audio-", dir=output.parent) as scratch:
            target = Path(scratch) / "source.wav"
            self._run([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", source,
                       "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", target])
            os.replace(target, output)
        return output

    def _font(self, size):
        try:
            from PIL import ImageFont
        except ImportError:
            raise CapabilityUnavailable("Pillow is required for text overlays") from None
        candidates = [self.font] if self.font else []
        candidates += [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/meiryo.ttc",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return ImageFont.truetype(str(candidate), size)
        raise CapabilityUnavailable("No usable overlay font; configure MediaTools(font=...)")

    def _overlay_image(self, overlay, info, path):
        from PIL import Image, ImageDraw, ImageColor

        x, y = int(overlay.get("x", 0)), int(overlay.get("y", 0))
        w, h = int(overlay.get("width", info["width"])), int(overlay.get("height", info["height"] // 4))
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > info["width"] or y + h > info["height"]:
            raise ValueError("Overlay rectangle is outside the video")
        text = str(overlay.get("text", ""))
        size = int(overlay.get("font_size", max(12, h // 3)))
        if len(text) > 10000 or not 6 <= size <= 512:
            raise ValueError("Overlay text or font exceeds limit")
        font = self._font(size)
        missing = font.getmask(chr(0x10FFFF))
        missing_signature = (missing.size, bytes(missing))
        for character in set(text):
            if character.isspace():
                continue
            glyph = font.getmask(character)
            if (glyph.size, bytes(glyph)) == missing_signature:
                raise ValueError("Configured font lacks a required glyph; select a suitable font")
        image = Image.new(
            "RGBA", (w, h), ImageColor.getcolor(str(overlay.get("background", "black")), "RGBA")
        )
        draw = ImageDraw.Draw(image)
        lines = []
        for paragraph in text.split("\n"):
            line = ""
            for character in paragraph:
                candidate = line + character
                if draw.textlength(candidate, font=font) > max(1, w - 12) and line:
                    lines.append(line)
                    line = character
                else:
                    line = candidate
            lines.append(line)
        rendered = "\n".join(lines)
        bbox = draw.multiline_textbbox((0, 0), rendered, font=font, spacing=4)
        if bbox[2] - bbox[0] > w - 8 or bbox[3] - bbox[1] > h - 8:
            raise ValueError("Translated overlay text does not fit its rectangle")
        draw.multiline_text(
            (4 - bbox[0], 4 - bbox[1]),
            rendered,
            font=font,
            fill=ImageColor.getcolor(str(overlay.get("color", "white")), "RGBA"),
            spacing=4,
        )
        image.save(path)
        image.close()
        return x, y

    def conform_video_edit(self, source: Path, generated: Path, output: Path):
        """Preserve source duration/aspect and original audio after remote video editing."""
        source, generated = self._source(source), self._source(generated)
        output = self._output(source, output)
        if output == generated:
            raise ValueError("Keep the original provider result as evidence")
        original, edited = self.probe(source), self.probe(generated)
        if abs(original["duration_ms"] - edited["duration_ms"]) > 250:
            raise ValueError("视频模型改变了原片时长，保留候选并阻塞，不裁掉源内容")
        if not edited["width"] or not edited["height"] or abs((original["width"] / original["height"]) / (edited["width"] / edited["height"]) - 1) > .02:
            raise ValueError("视频模型改变了画面比例，不能拉伸冒充原构图")
        from .visual_media import VisualMedia
        visual = VisualMedia(self)
        _, clock = visual._info(source)
        _, edited_clock = visual._info(generated)
        source_seconds = clock["frames"] / clock["fps"]
        edited_seconds = edited_clock["frames"] / edited_clock["fps"]
        if source_seconds - edited_seconds > 1 / clock["fps"] + 1e-6:
            raise ValueError("模型缺失超过一个末帧，不能自动复制补足")
        filters = (f"scale={original['width']}:{original['height']},setsar=1,"
                   f"fps={clock['rate']},tpad=stop_mode=clone:stop_duration={1/clock['fps']},"
                   f"trim=end_frame={clock['frames']},setpts=PTS-STARTPTS")
        self._run([self.ffmpeg,"-v","error","-nostdin","-y","-i",generated,"-i",source,
            "-map","0:v:0","-map","1:a?","-vf",filters,
            "-c:v","libx264rgb","-crf","0","-pix_fmt","rgb24","-c:a","aac","-movflags","+faststart",output])
        self.validate(output)
        _, aligned = visual._info(output)
        if aligned["rate"] != clock["rate"] or aligned["frames"] != clock["frames"]:
            raise ValueError("本地帧对齐未满足源帧时钟，保留结果待诊断")
        return output

    def render_visual(self, source: Path, output: Path, overlays: list[dict]):
        source = self._source(source)
        output = self._output(source, output)
        info = self.probe(source)
        if not info["width"] or info["width"] * info["height"] > 33_177_600 or len(overlays) > 32:
            raise ValueError("Invalid video or too many overlays")
        with TemporaryDirectory(prefix="visual-", dir=output.parent) as scratch:
            scratch = Path(scratch)
            args = [self.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", source]
            filters = []
            previous = "0:v"
            for index, overlay in enumerate(overlays):
                start, end = (
                    float(overlay.get("start_ms", 0)),
                    float(overlay.get("end_ms", info["duration_ms"])),
                )
                if (
                    not math.isfinite(start + end)
                    or start < 0
                    or end <= start
                    or end > info["duration_ms"] + 1
                ):
                    raise ValueError("Overlay time range is invalid")
                image = scratch / f"overlay-{index}.png"
                x, y = self._overlay_image(overlay, info, image)
                args += ["-loop", "1", "-i", image]
                label = f"v{index}"
                filters.append(
                    f"[{previous}][{index + 1}:v]overlay={x}:{y}:enable='gte(t,{start / 1000})*lt(t,{end / 1000})'[{label}]"
                )
                previous = label
            if filters:
                args += ["-filter_complex", ";".join(filters), "-map", f"[{previous}]"]
            else:
                args += ["-map", "0:v:0"]
            target = scratch / "visual.mp4"
            args += [
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-t",
                str(info["duration_ms"] / 1000),
                "-movflags",
                "+faststart",
                target,
            ]
            self._run(args)
            os.replace(target, output)
        return output

    def compose(
        self,
        visual: Path,
        output: Path,
        audio: Path | None = None,
        background: Path | None = None,
        subtitles: list[dict] | None = None,
        preserve_original_audio: bool = True,
    ):
        visual = self._source(visual)
        output = self._output(visual, output)
        info = self.probe(visual)
        with TemporaryDirectory(prefix="compose-", dir=output.parent) as scratch:
            scratch = Path(scratch)
            if subtitles:
                overlays = [
                    dict(
                        item,
                        x=0,
                        y=int(info["height"] * 0.72),
                        width=info["width"],
                        height=info["height"] - int(info["height"] * 0.72),
                        font_size=item.get("font_size", max(12, info["height"] // 24)),
                    )
                    for item in subtitles
                ]
                visual = self.render_visual(visual, scratch / "subtitled.mp4", overlays)
            args = [self.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", visual]
            inputs = []
            # An explicit speech track replaces original speech. Background must be supplied.
            if audio is None and preserve_original_audio and info["has_audio"]:
                inputs.append("0:a:0")
            for index, track in enumerate([p for p in (audio, background) if p is not None], start=1):
                track = self._source(track)
                if not self.probe(track)["has_audio"]:
                    raise ValueError("Supplied audio track has no audio stream")
                args += ["-i", track]
                inputs.append(f"{index}:a:0")
            args += ["-map", "0:v:0", "-c:v", "copy"]
            if inputs:
                if len(inputs) > 1:
                    filt = (
                        "".join(f"[{x}]" for x in inputs)
                        + f"amix=inputs={len(inputs)}:duration=longest:normalize=0,alimiter=limit=0.95,apad[a]"
                    )
                else:
                    filt = f"[{inputs[0]}]apad[a]"
                args += ["-filter_complex", filt, "-map", "[a]", "-c:a", "aac"]
            else:
                args += ["-an"]
            target = scratch / "composed.mp4"
            args += ["-t", str(info["duration_ms"] / 1000), "-movflags", "+faststart", target]
            self._run(args)
            os.replace(target, output)
        return output

    def background_for_dialogue(self, original: Path, separated: Path, output: Path, speech: list[dict]):
        original, separated = self._source(original), self._source(separated)
        if Path(output).resolve() == separated:
            raise ValueError("Background output must differ from separated source")
        output = self._output(original, output)
        info = self.probe(original)
        if abs(info["duration_ms"] - self.probe(separated)["duration_ms"]) > 250:
            raise ValueError("Separated background duration differs from original")
        if not speech or len(speech) > 128:
            raise ValueError("Dialogue windows must contain 1..128 segments")
        windows = []
        for segment in sorted(speech, key=lambda value: value["start_ms"]):
            start, end = segment["start_ms"] / 1000, segment["end_ms"] / 1000
            if not math.isfinite(start + end) or start < 0 or end <= start or end * 1000 > info["duration_ms"] + 1:
                raise ValueError("Invalid dialogue window")
            if windows and start <= windows[-1][1] + .08:
                windows[-1][1] = max(end, windows[-1][1])
            else:
                windows.append([start, end])
        # Coalesce gaps shorter than both fades; avoid rapid original-voice leakage.
        gates = [f"min(1,max(0,(t-{start - .04:.6f})/.04))*min(1,max(0,({end + .04:.6f}-t)/.04))"
                 for start, end in windows]
        def balanced_sum(items):
            if len(items) == 1:
                return items[0]
            middle = len(items) // 2
            return f"({balanced_sum(items[:middle])}+{balanced_sum(items[middle:])})"
        gate = "min(1," + balanced_sum(gates) + ")"
        filters = (f"[0:a]aformat=channel_layouts=stereo,aeval='val(0)*(1-({gate}))|val(1)*(1-({gate}))':c=stereo[o];"
                   f"[1:a]aformat=channel_layouts=stereo,aeval='val(0)*({gate})|val(1)*({gate})':c=stereo[b];"
                   "[o][b]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95:level=false:latency=true[a]")
        with TemporaryDirectory(prefix="background-", dir=output.parent) as scratch:
            target = Path(scratch) / "background.wav"
            graph = Path(scratch) / "mix.filter"
            graph.write_text(filters, encoding="utf-8")
            self._run([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", original, "-i", separated,
                       "-filter_complex_script", graph, "-map", "[a]", "-ac", "2", "-ar", "44100",
                       "-c:a", "pcm_s16le", "-t", str(info["duration_ms"] / 1000), target])
            os.replace(target, output)
        return output

    def fit_audio(self, audio: Path, output: Path, duration_ms):
        audio = self._source(audio)
        output = self._output(audio, output)
        info = self.probe(audio)
        if not info["has_audio"] or not math.isfinite(duration_ms) or duration_ms <= 0:
            raise ValueError("Audio and positive target duration are required")
        ratio = info["duration_ms"] / duration_ms
        if ratio > 1.25 + 0.001:
            raise ValueError("Speech would require speedup above 25%; revise the text")
        speed = max(0.75, min(1.25, ratio))
        # Size follows the selected PCM format; a muxer cap is independent of filter EOF.
        raw = self._run([self.ffprobe, "-v", "error", "-select_streams", "a:0",
                         "-show_entries", "stream=sample_rate,channels", "-of", "json", audio], capture=True)
        try:
            stream = json.loads(raw)["streams"][0]
            sample_rate, channels = int(stream["sample_rate"]), int(stream["channels"])
            if not 1 <= sample_rate <= 768000 or not 1 <= channels <= 64:
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError):
            raise MediaError("Audio format cannot establish a finite output size") from None
        max_bytes = math.ceil(duration_ms / 1000 * sample_rate * channels * 2) + 65536
        with TemporaryDirectory(prefix="fit-", dir=output.parent) as scratch:
            target = Path(scratch) / "fitted.wav"
            self._run(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    audio,
                    "-vn",
                    "-af",
                    f"atempo={speed:.8f},apad,atrim=duration={duration_ms / 1000:.6f}",
                    "-map", "0:a:0",
                    "-t", f"{duration_ms / 1000:.6f}",
                    "-fs", str(max_bytes),
                    "-c:a",
                    "pcm_s16le",
                    target,
                ]
            )
            if target.stat().st_size > max_bytes or abs(self.probe(target)["duration_ms"] - duration_ms) > 2:
                raise MediaError("Fitted audio exceeded its size or duration boundary")
            os.replace(target, output)
        return output

    def assemble_audio(self, segments: list[dict], output: Path, duration_ms: int) -> Path:
        if not math.isfinite(duration_ms) or duration_ms <= 0 or len(segments) > 64:
            raise ValueError("Audio timeline duration or segment count is invalid")
        output = Path(output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(segments, key=lambda item: float(item["start_ms"]))
        previous_end = 0
        for segment in ordered:
            start, end = float(segment["start_ms"]), float(segment["end_ms"])
            if not math.isfinite(start + end) or start < previous_end or end <= start or end > duration_ms:
                raise ValueError("Speech segments overlap or fall outside the timeline")
            if self._source(segment["path"]) == output:
                raise ValueError("Audio output must differ from every source")
            previous_end = end
        with TemporaryDirectory(prefix="timeline-", dir=output.parent) as scratch:
            scratch = Path(scratch)
            args = [self.ffmpeg, "-v", "error", "-nostdin", "-y"]
            filters = []
            for index, segment in enumerate(ordered):
                start, end = float(segment["start_ms"]), float(segment["end_ms"])
                fitted = self.fit_audio(Path(segment["path"]), scratch / f"segment-{index}.wav", end - start)
                args += ["-i", fitted]
                filters.append(f"[{index}:a]adelay={round(start)}:all=1[s{index}]")
            if ordered:
                filters.append(
                    "".join(f"[s{i}]" for i in range(len(ordered)))
                    + f"amix=inputs={len(ordered)}:duration=longest:normalize=0,apad,atrim=duration={duration_ms / 1000:.6f}[timeline]"
                )
                args += ["-filter_complex", ";".join(filters), "-map", "[timeline]"]
            else:
                args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            target = scratch / "timeline.wav"
            max_bytes = math.ceil(duration_ms / 1000 * 48000 * 2 * 2) + 65536
            args += ["-t", f"{duration_ms / 1000:.6f}", "-fs", str(max_bytes),
                     "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", target]
            self._run(args)
            if target.stat().st_size > max_bytes or abs(self.probe(target)["duration_ms"] - duration_ms) > 2:
                raise MediaError("Audio timeline exceeded its size or duration boundary")
            os.replace(target, output)
        return output

    def validate(self, path: Path) -> dict:
        path = self._source(path)
        info = self.probe(path)
        self._run(
            [
                self.ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                path,
                "-map",
                "0:v?",
                "-map",
                "0:a?",
                "-f",
                "null",
                "-",
            ]
        )
        return {**info, "valid": True, "decode_complete": True, "quality_checked": False}
