"""Same-frame background interpolation with observed motion and foreground masks.

Only narrow, low-texture surfaces with observed opposing clean margins qualify.
This does not infer masks, generate imagery, or establish visual acceptance.
"""
from PIL import Image, ImageDraw

from video_translation.domain.local_repair import SurfaceRepair


def planar_patch(frame, rect):
    """Fit only a surrounding three-pixel ring; never sample the erased area."""
    try:
        import numpy as np
    except ImportError as exc:
        raise ValueError("Planar surface requires the optional NumPy vision dependency") from exc

    x, y, right, bottom = rect
    band = 3
    if x < band or y < band or right + band > frame.width or bottom + band > frame.height:
        raise ValueError("Planar surface requires three clean pixels on all four margins")
    data = np.asarray(frame.convert("RGB"))
    yy, xx = np.mgrid[y - band:bottom + band, x - band:right + band]
    ring = (xx < x) | (xx >= right) | (yy < y) | (yy >= bottom)
    nx = (xx - x) / (right - x)
    ny = (yy - y) / (bottom - y)
    design = np.stack([np.ones_like(nx), nx, ny], axis=-1)
    samples = data[y - band:bottom + band, x - band:right + band][ring].astype(float)
    coefficients, _, rank, _ = np.linalg.lstsq(design[ring], samples, rcond=None)
    errors = np.abs(design[ring] @ coefficients - samples)
    # A gradient may span many levels, but texture, steps and contaminated
    # sampling bands must still fail rather than be averaged away.
    if rank != 3 or errors.max() > 12 or np.sqrt(np.mean(errors ** 2)) > 4:
        raise ValueError("Planar surface has textured, discontinuous or contaminated margins")
    # Validate the actual output levels before uint8 conversion can wrap them.
    interior = np.rint(design[band:-band, band:-band] @ coefficients)
    if not np.isfinite(interior).all() or interior.min() < 0 or interior.max() > 255:
        raise ValueError("Planar surface fit exceeds the observed color range")
    return Image.fromarray(interior.astype("uint8"))


def region_at(keys, timestamp):
    if timestamp <= keys[0]["time_ms"]:
        return keys[0]["region"]
    for left, right in zip(keys, keys[1:]):
        if timestamp <= right["time_ms"]:
            weight = (timestamp - left["time_ms"]) / (right["time_ms"] - left["time_ms"])
            return {name: left["region"][name] * (1 - weight) + right["region"][name] * weight
                    for name in left["region"]}
    return keys[-1]["region"]


def restore_surface(visual, source, target, *, start_ms, end_ms, envelope, recipe, occlusion=()):
    recipe = SurfaceRepair.model_validate(recipe).model_dump()
    source, info = visual._info(source)
    visual._window(info, start_ms, end_ms)
    target = visual._target(source, target)
    bounds = visual._rect(info, envelope)
    keys = recipe["keyframes"]
    if keys[0]["time_ms"] != start_ms or keys[-1]["time_ms"] > end_ms:
        raise ValueError("Repair geometry must begin at the edit start and stay within the edit window")
    for key in keys:
        rect = visual._rect(info, key["region"])
        if rect[0] < bounds[0] or rect[1] < bounds[1] or rect[2] > bounds[2] or rect[3] > bounds[3]:
            raise ValueError("Repair geometry exceeds the observed text envelope")
        if recipe["model"] == "plane" and (
                rect[0] < 3 or rect[1] < 3
                or rect[2] + 3 > info["width"] or rect[3] + 3 > info["height"]):
            raise ValueError("Planar surface requires clean margins on all four sides")
        if recipe["axis"] == "horizontal" and (rect[0] < 1 or rect[2] >= info["width"]):
            raise ValueError("Surface repair requires visible clean margins on both sides")
        if recipe["axis"] == "vertical" and (rect[1] < 1 or rect[3] >= info["height"]):
            raise ValueError("Vertical surface repair requires clean top and bottom margins")
    protected = visual._occlusion(list(occlusion), start_ms, end_ms)

    def transform(frame, timestamp):
        if not start_ms <= timestamp < end_ms:
            return
        x, y, right, bottom = visual._rect(info, region_at(keys, timestamp))
        mask = Image.new("L", frame.size, 0)
        ImageDraw.Draw(mask).rectangle((x, y, right - 1, bottom - 1), fill=255)
        if protected:
            polygon = [(round(px * info["width"]), round(py * info["height"]))
                       for px, py in visual._polygon_at(protected, timestamp)]
            ImageDraw.Draw(mask).polygon(polygon, fill=0)
        if recipe["model"] == "plane":
            try:
                patch = planar_patch(frame, (x, y, right, bottom))
            except ValueError as exc:
                raise ValueError(f"{exc} at frame time {timestamp:.3f}ms") from exc
            frame.paste(patch, (x, y), mask.crop((x, y, right, bottom)))
            return
        patch = Image.new("RGB", (right - x, bottom - y))
        pixels = patch.load()
        vertical = recipe["axis"] == "vertical"
        for line in range(x, right) if vertical else range(y, bottom):
            left_pixel, right_pixel = (
                (frame.getpixel((line, y - 1)), frame.getpixel((line, bottom))) if vertical
                else (frame.getpixel((x - 1, line)), frame.getpixel((right, line))))
            # Refuse clearly incompatible margins instead of smearing across a subject.
            if max(abs(a - b) for a, b in zip(left_pixel, right_pixel)) > recipe["max_edge_difference"]:
                raise ValueError(f"Surface margins differ at frame time {timestamp:.3f}ms; use another method")
            length = bottom - y if vertical else right - x
            for offset in range(length):
                weight = (offset + 1) / (length + 1)
                position = (line - x, offset) if vertical else (offset, line - y)
                pixels[position] = tuple(round(a * (1 - weight) + b * weight)
                                         for a, b in zip(left_pixel, right_pixel))
        frame.paste(patch, (x, y), mask.crop((x, y, right, bottom)))

    return visual._render_window(source, target, info, transform, start_ms, end_ms)
