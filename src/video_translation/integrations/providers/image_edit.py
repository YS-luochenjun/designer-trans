"""Project-owned optional image editing; never retries a paid POST.

Caller owns cost authorization, operation journaling and local path confinement.
No session tools, credential discovery or image URL downloads are used here.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

from ..agents.errors import CapabilityUnavailable, ProviderError, UnknownExternalResult

DEFAULT_ENDPOINT = "https://api.openai.com/v1/images/edits"
DEFAULT_MODEL = "gpt-image-2"
DOCUMENTED_MODELS = frozenset({"gpt-image-2", "gpt-image-2-2026-04-21"})
KIE_IMAGE_MODEL = "gpt-image-2-image-to-image"


def _boolean(env, name, default):
    raw = env.get(name)
    if raw is None:
        return default
    if raw.strip().lower() not in {"1", "0", "true", "false", "yes", "no"}:
        raise ValueError("Invalid image edit boolean")
    return raw.strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class ImageEditConfig:
    api_key: str = field(default="", repr=False)
    enabled: bool = False
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    supports_reference: bool = True
    supports_mask: bool = True
    supports_alpha: bool = False
    timeout: float = 120
    max_input_bytes: int = 20 * 1024 * 1024
    max_output_bytes: int = 20 * 1024 * 1024
    max_pixels: int = 16_777_216
    max_prompt_chars: int = 32000

    def __post_init__(self):
        for name in ("enabled", "supports_reference", "supports_mask", "supports_alpha"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"Invalid image edit {name}")
        if not isinstance(self.api_key, str) or any(ord(c) < 33 or ord(c) > 126 for c in self.api_key):
            raise ValueError("Invalid image edit API key")
        if not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", self.model):
            raise ValueError("Invalid image edit model identifier")
        try:
            parts = urlsplit(self.endpoint)
            valid = (parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                     and not parts.query and not parts.fragment and parts.port in (None, 443)
                     and parts.path.endswith("/images/edits") and not any(c.isspace() for c in self.endpoint))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Image edit endpoint must be an HTTPS images/edits URL without credentials or query")
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 600):
            raise ValueError("Image edit timeout must be finite and between 0 and 600 seconds")
        for name, ceiling in (("max_input_bytes", 50 * 1024 * 1024),
                              ("max_output_bytes", 50 * 1024 * 1024),
                              ("max_pixels", 16_777_216), ("max_prompt_chars", 32000)):
            if type(getattr(self, name)) is not int or not 0 < getattr(self, name) <= ceiling:
                raise ValueError(f"Invalid image edit {name}")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        try:
            return cls(
                api_key=env.get("VT_IMAGE_EDIT_API_KEY", ""),
                enabled=_boolean(env, "VT_IMAGE_EDIT_ENABLED", False),
                endpoint=env.get("VT_IMAGE_EDIT_ENDPOINT", DEFAULT_ENDPOINT),
                model=env.get("VT_IMAGE_EDIT_MODEL", DEFAULT_MODEL),
                supports_reference=_boolean(env, "VT_IMAGE_EDIT_SUPPORTS_REFERENCE", True),
                supports_mask=_boolean(env, "VT_IMAGE_EDIT_SUPPORTS_MASK", True),
                supports_alpha=_boolean(env, "VT_IMAGE_EDIT_SUPPORTS_ALPHA", False),
                timeout=float(env.get("VT_IMAGE_EDIT_TIMEOUT", "120")),
                max_input_bytes=int(env.get("VT_IMAGE_EDIT_MAX_INPUT_BYTES", str(20 * 1024 * 1024))),
                max_output_bytes=int(env.get("VT_IMAGE_EDIT_MAX_OUTPUT_BYTES", str(20 * 1024 * 1024))),
                max_pixels=int(env.get("VT_IMAGE_EDIT_MAX_PIXELS", "16777216")),
                max_prompt_chars=int(env.get("VT_IMAGE_EDIT_MAX_PROMPT_CHARS", "32000")),
            )
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid image edit configuration; check named VT_IMAGE_EDIT_* settings") from None

    def snapshot(self):
        documented = self.endpoint == DEFAULT_ENDPOINT and self.model in DOCUMENTED_MODELS
        return {
            "provider": "openai-compatible-image-edit", "protocol": "images_edits_multipart",
            "model": self.model, "endpoint": self.endpoint,
            "capabilities": {"reference": self.supports_reference, "mask": self.supports_mask,
                             "alpha": self.supports_alpha, "text_only_generation": False,
                             "input_formats": ["PNG", "JPEG"], "output_format": "PNG",
                             "max_references": 1, "exact_mask_preservation": False},
            "contract_status": "official_documented" if documented else "operator_configured_unverified",
            "alpha_status": "preview" if documented else "operator_configured_unverified",
            "provider_verified": False, "timeout_seconds": self.timeout,
            "max_input_bytes": self.max_input_bytes, "max_output_bytes": self.max_output_bytes,
            "max_pixels": self.max_pixels, "max_prompt_chars": self.max_prompt_chars,
        }

    def status(self):
        reason = ("disabled" if not self.enabled else "missing_api_key" if not self.api_key.strip()
                  else "reference_not_supported" if not self.supports_reference else "configured_not_live_verified")
        return {**self.snapshot(), "enabled": self.enabled, "api_key_present": bool(self.api_key.strip()),
                "ready": reason == "configured_not_live_verified", "reason": reason}


def _image_info(data, max_pixels):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt, size = image.format, image.size
                if fmt not in {"PNG", "JPEG"} or size[0] * size[1] > max_pixels or getattr(image, "n_frames", 1) != 1:
                    raise ValueError()
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                alpha = "A" in image.getbands() or "transparency" in image.info
                transparent = alpha and image.convert("RGBA").getchannel("A").getextrema()[0] < 255
                return fmt, size, alpha, transparent
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Image must be a complete, single-frame PNG/JPEG within pixel limits") from None


def _read_image(path, config):
    if not isinstance(path, Path):
        raise ValueError("Image inputs must be caller-validated local Path objects")
    try:
        if not path.is_file() or path.stat().st_size > config.max_input_bytes:
            raise ValueError("Image input is missing or exceeds byte limit")
        with path.open("rb") as stream:
            data = stream.read(config.max_input_bytes + 1)
    except OSError:
        raise ValueError("Image input cannot be read") from None
    if len(data) > config.max_input_bytes:
        raise ValueError("Image input exceeds byte limit")
    return data, _image_info(data, config.max_pixels)


class ImageEditor:
    def __init__(self, config: ImageEditConfig, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._transport = transport

    def snapshot(self):
        return self.config.snapshot()

    def status(self):
        return self.config.status()

    def edit(self, source: Path, target: Path, prompt: str, *, mask: Path | None = None,
             transparent: bool = False) -> Path:
        cfg = self.config
        if not cfg.status()["ready"]:
            raise CapabilityUnavailable(f"Image editing unavailable: {cfg.status()['reason']}")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > cfg.max_prompt_chars:
            raise ValueError("Image edit prompt is empty or exceeds character limit")
        if type(transparent) is not bool:
            raise ValueError("Image edit transparent must be a boolean")
        if transparent and not cfg.supports_alpha:
            raise CapabilityUnavailable("Image edit alpha capability is not enabled")
        if mask is not None and not cfg.supports_mask:
            raise CapabilityUnavailable("Image edit mask capability is not enabled")
        if not isinstance(target, Path) or target.suffix.lower() != ".png":
            raise ValueError("Image edit target must be a caller-validated local PNG Path")
        if target.exists() or not target.parent.is_dir():
            raise ValueError("Image edit target must be new and its parent directory must exist")
        source_data, (source_format, source_size, _, _) = _read_image(source, cfg)
        files = {"image": ("reference.png" if source_format == "PNG" else "reference.jpg", source_data,
                           "image/png" if source_format == "PNG" else "image/jpeg")}
        if mask is not None:
            mask_data, (mask_format, mask_size, has_alpha, _) = _read_image(mask, cfg)
            if mask_format != "PNG" or source_format != "PNG" or not has_alpha or mask_size != source_size:
                raise ValueError("Mask and reference must be PNG; mask needs alpha and matching dimensions")
            files["mask"] = ("mask.png", mask_data, "image/png")
        form = {"model": cfg.model, "prompt": prompt, "n": "1", "output_format": "png"}
        if transparent:
            form["background"] = "transparent"
        output = self._decode(self._post(form, files), transparent)
        created = False
        try:
            with target.open("xb") as stream:
                created = True
                stream.write(output)
        except OSError:
            if created:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            raise UnknownExternalResult("Image edit returned output but local save failed; do not resubmit") from None
        return target

    def _post(self, form, files):
        cfg = self.config
        limit = 4 * ((cfg.max_output_bytes + 2) // 3) + 65536
        deadline = time.monotonic() + cfg.timeout
        try:
            with httpx.Client(transport=self._transport, follow_redirects=False, trust_env=False,
                              timeout=cfg.timeout) as client:
                with client.stream("POST", cfg.endpoint,
                                   headers={"Authorization": f"Bearer {cfg.api_key}", "Accept-Encoding": "identity"},
                                   data=form, files=files) as response:
                    status = response.status_code
                    if status == 408 or status >= 500:
                        raise UnknownExternalResult(f"Image edit HTTP {status}; outcome unknown; do not resubmit")
                    if status >= 300:
                        raise ProviderError(f"Image edit HTTP {status}; response body withheld")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise UnknownExternalResult("Compressed image edit response refused; do not resubmit")
                    result = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise UnknownExternalResult("Image edit response exceeded time limit; do not resubmit")
                        if len(result) + len(chunk) > limit:
                            raise UnknownExternalResult("Image edit response exceeds byte limit; do not resubmit")
                        result.extend(chunk)
                    return bytes(result)
        except (httpx.HTTPError, OSError):
            raise UnknownExternalResult("Image edit connection interrupted; outcome unknown; do not resubmit") from None

    def _decode(self, raw, transparent):
        try:
            body = json.loads(raw)
            items = body["data"]
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise ValueError()
            encoded = items[0]["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > 4 * ((self.config.max_output_bytes + 2) // 3):
                raise ValueError()
            output = base64.b64decode(encoded, validate=True)
            if len(output) > self.config.max_output_bytes:
                raise ValueError()
            fmt, _, alpha, has_transparency = _image_info(output, self.config.max_pixels)
            if fmt != "PNG" or (transparent and not (alpha and has_transparency)):
                raise ValueError()
            return output
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
            raise UnknownExternalResult(
                "Image edit returned invalid output (expected one base64 PNG with requested alpha); do not resubmit"
            ) from None


def build_kie_image_input(source_url: str, prompt: str, *, aspect_ratio: str = "auto",
                          resolution: str = "1K", transparent: bool = False) -> dict:
    """Pure KIE input contract; caller must supply its validated upload URL.

    Does not upload, fetch, submit, poll, discover credentials or claim availability.
    Use KIE_IMAGE_MODEL for the outer createTask model. KIE documents no mask field.
    """
    try:
        parts = urlsplit(source_url)
        valid = (parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                 and not parts.fragment and parts.port in (None, 443)
                 and not any(c.isspace() for c in source_url))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("KIE image reference must be a caller-validated HTTPS upload URL")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise ValueError("KIE image prompt must contain 1 to 20000 characters")
    if type(transparent) is not bool:
        raise ValueError("KIE image transparent must be a boolean")
    ratios = {"auto", "1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5", "16:9", "9:16",
              "2:1", "1:2", "3:1", "1:3", "21:9", "9:21"}
    if aspect_ratio not in ratios or resolution not in {"1K", "2K", "4K"}:
        raise ValueError("Unsupported KIE image aspect ratio or resolution")
    unsupported = {"1K": set(), "2K": {"5:4", "4:5", "3:1", "1:3", "9:21"},
                   "4K": {"3:1", "1:3", "9:21"}}
    if aspect_ratio in unsupported[resolution] or (transparent and resolution != "1K"):
        raise ValueError("KIE image ratio/background is unsupported at this resolution")
    result = {"prompt": prompt, "input_urls": [source_url], "aspect_ratio": aspect_ratio,
              "resolution": resolution}
    if transparent:
        result["background"] = "transparent"
    return result
