"""Exact CFR frame indices for half-open edit windows and observations."""
import math
from fractions import Fraction


def frame_window(start_ms, end_ms, rate):
    rate = Fraction(str(rate))
    return math.ceil(start_ms * rate / 1000), math.ceil(end_ms * rate / 1000)


def sample_index(time_ms, rate, count):
    return min(math.floor(time_ms * Fraction(str(rate)) / 1000), count - 1)


def request_time(index, rate):
    """Integer timestamp guaranteed to select this frame, not its predecessor."""
    return math.ceil(index * 1000 / Fraction(str(rate)))


def observation_clock(info, container_duration_ms):
    """Expose a finite video clock independently of trailing audio padding."""
    rate = Fraction(str(info["rate"]))
    count = info["frames"]
    if (type(count) is not int or count <= 0 or rate <= 0
            or type(container_duration_ms) is not int or container_duration_ms <= 0):
        raise ValueError("Observation requires a positive native video clock")
    video_end = count * 1000 / rate
    return {
        "rate": str(rate), "frame_count": count, "container_duration_ms": container_duration_ms,
        "video_duration_ms": float(video_end),
        "last_frame_time_ms": float((count - 1) * 1000 / rate),
        "last_request_time_ms": min(container_duration_ms - 1, math.ceil(video_end) - 1),
        "endpoint_rule": "Container endpoint and audio padding select the last video frame; later times are invalid.",
    }


def inspection_requests(times_ms, clock):
    """Keep original requested times; map valid audio-tail requests into video."""
    if (not isinstance(times_ms, list) or not times_ms or
            any(type(t) is not int or not 0 <= t <= clock["container_duration_ms"] for t in times_ms)):
        raise ValueError("Inspection time is outside original source")
    return {t: min(t, clock["last_request_time_ms"]) for t in times_ms}


def boundary_indices(start_ms, end_ms, rate, count):
    first, stop = frame_window(start_ms, end_ms, rate)
    stop = min(stop, count)
    return sorted({i for i in (first - 1, first, stop - 1, stop) if 0 <= i < count})
