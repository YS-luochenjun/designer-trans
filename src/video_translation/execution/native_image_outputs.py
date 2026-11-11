"""Collect a retained Codex tool result without invoking tools or changing pixels."""
import hashlib
import io
import json
import os
from pathlib import Path

from PIL import Image


MAX_IMAGE_BYTES = 20 * 1024 * 1024


class UnusableImage(ValueError):
    """A returned image exists, but cannot serve as a full-frame reference."""


def business_result(raw, schema):
    import jsonschema
    if not isinstance(raw, dict):
        raise ValueError("Native result must be an object")
    # AgentAdapter adds transport evidence after validating the business schema.
    # Retain it on disk; do not feed it back into additionalProperties=false.
    result = {k: v for k, v in raw.items() if k != "_media"}
    if "_media" in raw and not isinstance(raw["_media"], dict):
        raise ValueError("Invalid native result transport evidence")
    try:
        jsonschema.validate(result, schema)
    except jsonschema.ValidationError as exc:
        raise ValueError("Invalid native business receipt: " + exc.message[:240]) from exc
    return result


def load_result(path, schema):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Retained native receipt is missing or invalid")
    return business_result(json.loads(path.read_text(encoding="utf-8")), schema)


def recovery_key(path, result):
    # Recheck a genuinely changed local artifact, never poll the same invalid
    # receipt forever or start another generation after an uncertain request.
    artifact = Path(result.get("generated_image_path") or "__missing_native_image__")
    stamp = None
    if artifact.is_absolute() and artifact.is_file():
        info = artifact.stat()
        stamp = (info.st_size, info.st_mtime_ns)
    return hashlib.sha256(path.read_bytes() + repr(stamp).encode()).hexdigest()


def collect_image(executor, task, result, directory):
    service = executor.service
    source = Path(result["generated_image_path"])
    config = getattr(executor.agent_for(task), "config", None)
    env = getattr(config, "env", None) or {}
    home = Path(env.get("CODEX_HOME", service.settings.runtime / "codex-home")).resolve()
    roots = (directory.resolve(), (home / "generated_images").resolve())
    if (not source.is_absolute() or source.is_symlink()
            or not any(source.resolve().is_relative_to(root) for root in roots)):
        raise ValueError("Native image is outside this request or the configured Codex generated_images")
    if not source.is_file():
        raise FileNotFoundError("Retained native image has not been found; do not regenerate")
    with source.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise UnusableImage("Returned native image exceeds the reference size bound")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.width * image.height > 16_777_216:
                raise UnusableImage("Returned native reference must be a bounded PNG")
            dimensions = image.size
            image.load()
        record = service.store.require("imagegen_request", directory.name)
        with Image.open(record["original_frame_path"]) as original:
            original_dimensions = original.size
        if abs((dimensions[0] / dimensions[1]) /
               (original_dimensions[0] / original_dimensions[1]) - 1) > .01:
            raise UnusableImage("Returned native image changes the original full-frame aspect ratio")
    except UnusableImage:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise UnusableImage("Returned native image cannot be decoded as an intact reference") from exc
    digest = hashlib.sha256(data).hexdigest()
    target = directory / "retained-reference.png"
    if target.is_symlink() or (target.exists() and service.hash_file(target) != digest):
        raise ValueError("Existing retained reference differs from the original native output")
    if not target.exists():
        from uuid import uuid4
        temporary = directory / (uuid4().hex + ".png.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target, {
        "original_path": str(source.resolve()), "task_path": str(target.resolve()),
        "sha256": digest, "dimensions": list(dimensions),
        "original_frame_dimensions": list(original_dimensions),
        "pixels_changed": False,
    }
