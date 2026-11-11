"""Bounded CPU template tracking. Results are proposals, never verified motion.

Uses the initial appearance rather than updating the template (avoids cumulative
appearance drift). Translation and modest scale only; occlusion/cuts must fail.
"""
import hashlib
from pathlib import Path

from PIL import Image

from .frame_clock import frame_window


def available():
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _match(frame, template, box, radius):
    import numpy as np
    template_array = np.asarray(template, dtype=np.float32)
    template_array -= template_array.mean()
    norm = float(np.linalg.norm(template_array))
    if norm < 20:
        raise ValueError("Tracking seed lacks distinctive texture")
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    cx, cy = (left + right) / 2, (top + bottom) / 2
    best = None
    for scale in (0.97, 1.0, 1.03):
        w, h = max(5, round(width * scale)), max(5, round(height * scale))
        x0, y0 = max(0, round(cx - w / 2) - radius), max(0, round(cy - h / 2) - radius)
        x1, y1 = min(frame.width, round(cx + w / 2) + radius), min(frame.height, round(cy + h / 2) + radius)
        # Search on a small normalized grid, independent of video resolution.
        sx, sy = template.width / w, template.height / h
        search = frame.crop((x0, y0, x1, y1)).resize(
            (max(template.width, round((x1 - x0) * sx)),
             max(template.height, round((y1 - y0) * sy))), Image.Resampling.BILINEAR)
        values = np.asarray(search, dtype=np.float32)
        windows = np.lib.stride_tricks.sliding_window_view(values, template_array.shape)
        # Bound temporary allocations even for a small seed and wide search.
        score, iy, ix = -1.0, 0, 0
        for offset in range(0, windows.shape[0], 4):
            chunk = windows[offset:offset + 4]
            centered = chunk - chunk.mean(axis=(-1, -2), keepdims=True)
            denom = np.sqrt((centered * centered).sum(axis=(-1, -2))) * norm
            scores = (centered * template_array).sum(axis=(-1, -2)) / np.maximum(denom, 1e-6)
            row, col = np.unravel_index(np.argmax(scores), scores.shape)
            if float(scores[row, col]) > score:
                score, iy, ix = float(scores[row, col]), row + offset, col
        if best is None or score > best[0]:
            x, y = round(x0 + ix / sx), round(y0 + iy / sy)
            best = (score, (x, y, x + w, y + h))
    return best


def track_region(visual, source, *, start_ms, end_ms, region, max_frames=300):
    source, info = visual._info(source)
    visual._window(info, start_ms, end_ms)
    first, exclusive = frame_window(start_ms, end_ms, info["rate"])
    if end_ms - start_ms > 10000 or exclusive - first > max_frames:
        raise ValueError("Track one observed shot, at most 10 seconds / 300 frames")
    box = visual._rect(info, region)
    if min(box[2] - box[0], box[3] - box[1]) < 8:
        raise ValueError("Tracking seed is too small")
    rows, template, failed = [], None, None
    with visual._decoded(source, info) as frames:
        for index, image in enumerate(frames):
            if index < first:
                continue
            if index >= exclusive:
                break
            gray = image.convert("L")
            if template is None:
                template = gray.crop(box)
                template.thumbnail((32, 32), Image.Resampling.BILINEAR)
                # Validate seed texture even if the range has only one frame.
                confidence, _ = _match(gray, template, box, 0)
            else:
                confidence, proposed = _match(gray, template, box, 64)
                if confidence < 0.85 or not (
                    0 <= proposed[0] < proposed[2] <= info["width"]
                    and 0 <= proposed[1] < proposed[3] <= info["height"]
                ):
                    failed = {"frame_index": index, "reason": "appearance_lost_or_occluded",
                              "confidence": round(confidence, 4)}
                    break
                box = proposed
            rows.append({
                "frame_index": index, "sampled_time_ms": index * 1000 / info["fps"],
                "confidence": round(confidence, 4),
                "region": {"x": box[0] / info["width"], "y": box[1] / info["height"],
                           "width": (box[2] - box[0]) / info["width"],
                           "height": (box[3] - box[1]) / info["height"]},
            })
    with Path(source).open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"source_sha256": sha, "rate": info["rate"],
            "first_frame": first, "exclusive_end_frame": exclusive,
            "status": "needs_review" if failed is None else "lost",
            "verified": False, "failure": failed, "frames": rows,
            "limitations": ["Template translation/scale only; no rotation, perspective or occlusion recovery",
                            "High correlation is not proof of tracking the intended object"]}
