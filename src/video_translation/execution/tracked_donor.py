"""CPU same-shot donor compositing. Geometry is explicit, not inferred here."""
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops, ImageDraw, ImageStat

from video_translation.domain.local_repair import TrackedDonorRepair
from .surface_repair import region_at


def apply_tracked_donor(visual, source, target, *, start_ms, end_ms, envelope,
                        donor_time_ms, donor_region, recipe, occlusion=()):
    recipe = TrackedDonorRepair.model_validate(recipe).model_dump()
    source, info = visual._info(source)
    visual._window(info, start_ms, end_ms)
    visual._ms(donor_time_ms, "donor_time_ms")
    target = visual._target(source, target)
    if not (0 <= recipe["scene_start_ms"] <= start_ms < end_ms
            <= recipe["scene_end_ms"] <= info["duration_ms"]
            and recipe["scene_start_ms"] <= donor_time_ms < recipe["scene_end_ms"]):
        raise ValueError("Donor and edit must belong to the same observed shot")
    keys = recipe["keyframes"]
    if keys[0]["time_ms"] != start_ms or keys[-1]["time_ms"] != end_ms:
        raise ValueError("Observed donor trajectory must cover both edit endpoints")
    bounds = visual._rect(info, envelope)
    for key in keys:
        left, top, right, bottom = visual._rect(info, key["region"])
        if not (bounds[0] <= left < right <= bounds[2] and bounds[1] <= top < bottom <= bounds[3]):
            raise ValueError("Tracked patch escapes the declared edit envelope")
        if min(right - left, bottom - top) < 5:
            raise ValueError("Tracked patch needs a clean border at least five pixels wide/high")
    protected = visual._occlusion(list(occlusion), start_ms, end_ms)
    donor_box = visual._rect(info, donor_region)
    with TemporaryDirectory(prefix="tracked-donor-", dir=target.parent) as scratch:
        still = Path(scratch) / "donor.png"
        visual._still(source, info, donor_time_ms, still)
        with Image.open(still) as image:
            donor = image.convert("RGB").crop(donor_box)

    def transform(frame, timestamp):
        if not start_ms <= timestamp < end_ms:
            return
        left, top, right, bottom = visual._rect(info, region_at(keys, timestamp))
        size = (right - left, bottom - top)
        patch = donor.resize(size, Image.Resampling.BICUBIC)
        mask = Image.new("L", frame.size, 0)
        ImageDraw.Draw(mask).rectangle((left, top, right - 1, bottom - 1), fill=255)
        if protected:
            polygon = [(round(x * info["width"]), round(y * info["height"]))
                       for x, y in visual._polygon_at(protected, timestamp)]
            ImageDraw.Draw(mask).polygon(polygon, fill=0)
        local_mask = mask.crop((left, top, right, bottom))
        border = Image.new("L", size, 255)
        ImageDraw.Draw(border).rectangle((2, 2, size[0] - 3, size[1] - 3), fill=0)
        border = ImageChops.multiply(border, local_mask)
        if border.getbbox():
            difference = ImageChops.difference(frame.crop((left, top, right, bottom)), patch)
            if max(ImageStat.Stat(difference, border).mean) > recipe["max_border_error"]:
                raise ValueError(f"Donor border does not match at {timestamp:.3f}ms; inspect motion or lighting")
        frame.paste(patch, (left, top), local_mask)

    return visual._render(source, target, info, transform)
