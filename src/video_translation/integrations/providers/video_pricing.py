"""Frozen video quotes shared by model context, preflight and reservation."""
import math


WAN3_PROTOCOL = "kie_wan_reference_v1"


def reference_duration_ms(duration_ms, protocol):
    if (isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float))
            or not math.isfinite(duration_ms) or not 0 < duration_ms <= 10000):
        raise ValueError("Wan local reference must be within 0-10 seconds")
    if protocol == WAN3_PROTOCOL:
        return max(2000, math.ceil(duration_ms / 1000) * 1000)
    return max(2000, duration_ms)


def video_estimate(spec, resolution, duration_ms):
    if (isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float))
            or not math.isfinite(duration_ms) or duration_ms <= 0):
        raise ValueError("Video quote requires a finite positive duration")
    price = spec["estimated_credits_per_second"][resolution]
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise ValueError("Video quote requires a verified positive unit price")
    seconds = math.ceil(duration_ms / 1000)
    multiplier = 2 if spec.get("protocol") == WAN3_PROTOCOL else 1
    return seconds * multiplier * price
