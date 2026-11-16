"""Import native Work's sparse pixels into the ordinary unreviewed candidate chain."""
from pathlib import Path
import shutil
import stat
from uuid import uuid4

from PIL import Image

from video_translation.domain.execution import FileRef, PatchManifest
from .frame_clock import frame_window
from .workspace import file_sha, publish_json


def _checked(root, ref, *, maximum):
    path = Path(ref["path"])
    if not path.is_absolute() or not path.resolve().is_relative_to(root):
        raise ValueError("Native candidate dependency leaves the task")
    for cursor in (path, *path.parents):
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked native candidate dependency")
    info = path.stat()
    if (not path.is_file() or info.st_nlink != 1 or info.st_size > maximum
            or file_sha(path) != ref["sha256"]):
        raise ValueError("Native candidate dependency changed or exceeds capacity")
    return path


def register(controller, action, base, target, region, start, end):
    with controller.media.workspace.local_write():
        return _register(controller, action, base, target, region, start, end)


def _register(controller, action, base, target, region, start, end):
    root = (controller.service.settings.runtime / "tasks" / controller.task["id"]).resolve()
    original = _checked(root, action["candidate_manifest"], maximum=2 * 1024**2)
    manifest = PatchManifest.model_validate_json(original.read_bytes())
    before = file_sha(base)
    if Path(manifest.base.path).resolve() != Path(base).resolve() or manifest.base.sha256 != before:
        raise ValueError("Native candidate does not bind the current base")
    _, clock = controller.media._info(base)
    if (manifest.width, manifest.height, manifest.frames, manifest.rate) != (
            clock["width"], clock["height"], clock["frames"], clock["rate"]):
        raise ValueError("Native candidate clock or geometry changed")
    if not manifest.patches:
        raise ValueError("Native candidate contains no changed frames")
    first, stop = frame_window(start, end, clock["rate"])
    x, y, width, height = controller.media.pixel_region(region, manifest.width, manifest.height)
    total, dependencies = 0, []
    for patch in manifest.patches:
        left, top, right, bottom = patch.rectangle
        if not (first <= patch.frame_index < stop and x <= left < right <= x + width
                and y <= top < bottom <= y + height):
            raise ValueError("Native candidate changes pixels outside its assigned scope")
        image = _checked(root, patch.image.model_dump(), maximum=32 * 1024**2)
        with Image.open(image) as pixels:
            if (pixels.format != "PNG" or pixels.mode != "RGB"
                    or pixels.size != (right - left, bottom - top)):
                raise ValueError("Native candidate pixels do not match their rectangle")
            pixels.load()
        total += image.stat().st_size
        dependencies.append((patch, image))
    controller.media.workspace.preflight(
        target, total + original.stat().st_size, estimated_files=len(dependencies) + 1)
    # Snapshot scratch pixels. Later native edits cannot mutate a registered candidate.
    assets = target.with_name(target.stem + "-" + uuid4().hex[:12] + ".frames")
    assets.mkdir()
    published = []
    for patch, image in dependencies:
        destination = assets / f"{patch.frame_index}.png"
        with image.open("rb") as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output)
        if file_sha(destination) != patch.image.sha256:
            raise ValueError("Native pixels changed during registration")
        published.append(patch.model_copy(update={
            "image": FileRef(path=str(destination.resolve()), sha256=patch.image.sha256)}))
    if file_sha(base) != before or file_sha(original) != action["candidate_manifest"]["sha256"]:
        raise ValueError("Native input changed during registration")
    admitted = manifest.model_copy(update={"patches": published})
    publish_json(target, admitted.model_dump())
    controller.media._info(target)
    return {
        "manifest_sha256": action["candidate_manifest"]["sha256"],
        "tool_receipt": action["tool_receipt"], "evidence": action["evidence"],
        "changed_frames": len(published), "first_changed_frame": min(p.frame_index for p in published),
        "last_changed_frame": max(p.frame_index for p in published),
        "audio_source_preserved": True, "quality_passed": False,
        "meaning": "Sparse changes only; unchanged frames and original audio are inherited from the base.",
    }
