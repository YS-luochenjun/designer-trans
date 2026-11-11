"""Browser derivatives never replace lossless production or review artifacts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from .media import MediaError

_ENCODE_LOCK = Lock()
_VERSION = "browser-bt709-v1"


def video_stream(media, source):
    data = json.loads(media._run([
        media.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,color_space,color_transfer,width,height,avg_frame_rate,nb_frames",
        "-of", "json", source,
    ], capture=True))
    return next(iter(data.get("streams", [])), {})


def browser_video(media, source, cache):
    """Return an SDR playback copy for RGB/4:4:4 H.264, or the original."""
    source = media._source(source)
    stream = video_stream(media, source)
    if stream.get("codec_name") != "h264" or stream.get("pix_fmt") == "yuv420p":
        return source
    if stream.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
        raise MediaError("HDR browser conversion requires an explicit tone-mapping policy")
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    cache = Path(cache).resolve()
    target = cache / f"{digest}-{_VERSION}.mp4"
    with _ENCODE_LOCK:
        if target.is_file():
            return target
        cache.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="browser-", dir=cache) as temporary:
            output = Path(temporary) / "preview.mp4"
            media._run([
                media.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", source,
                "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                "-c:v", "libx264", "-crf", "16", "-preset", "fast", "-threads", "2",
                "-color_range", "tv", "-colorspace", "bt709",
                "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", "copy", "-fps_mode", "passthrough", "-movflags", "+faststart",
                output,
            ])
            result = video_stream(media, output)
            if result.get("pix_fmt") != "yuv420p":
                raise MediaError("Browser derivative has incompatible pixel format")
            for field in ("avg_frame_rate", "nb_frames"):
                if stream.get(field) not in (None, "N/A") and result.get(field) != stream[field]:
                    raise MediaError("Browser derivative changed video timing")
            before, after = media.probe(source), media.probe(output)
            if before["has_audio"] != after["has_audio"]:
                raise MediaError("Browser derivative lost audio")
            if abs(before["duration_ms"] - after["duration_ms"]) > 100:
                raise MediaError("Browser derivative changed duration")
            os.replace(output, target)
    return target
