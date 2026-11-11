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


def boundary_indices(start_ms, end_ms, rate, count):
    first, stop = frame_window(start_ms, end_ms, rate)
    return sorted({i for i in (first - 1, first, stop - 1, stop) if 0 <= i < count})
