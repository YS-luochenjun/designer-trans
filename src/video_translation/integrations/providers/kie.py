"""KIE contracts, durable paid-request checkpoints and bounded public media downloads.

Chat transcription is synchronous, prompt-directed and uncertain. TTS/video use
jobs. A persisted submit intent without a task ID is never replayed automatically.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import math
import mimetypes
import os
import re
import socket
import time
import wave
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

import httpx

from ..agents.errors import CapabilityUnavailable, ConfirmedJobFailure, ProviderError, UnknownExternalResult
from ..agents.transport import request_bytes

API = "https://api.kie.ai"
UPLOAD = "https://kieai.redpandaai.co/api/file-stream-upload"
CHAT_ROUTE = "/gemini-3.1-pro/v1/chat/completions"
TTS_MODEL = "google/gemini-2-5-pro-tts"
VIDEO_MODEL = "wan/2-7-videoedit"
WAN3_MODEL = "wan/3-0-video"
VOICES = frozenset(
    (
        "Zephyr",
        "Puck",
        "Charon",
        "Kore",
        "Fenrir",
        "Leda",
        "Orus",
        "Aoede",
        "Callirrhoe",
        "Autonoe",
        "Enceladus",
        "Iapetus",
        "Umbriel",
        "Algieba",
        "Despina",
        "Erinome",
        "Algenib",
        "Rasalgethi",
        "Laomedeia",
        "Achernar",
        "Alnilam",
        "Schedar",
        "Gacrux",
        "Pulcherrima",
        "Achird",
        "Zubenelgenubi",
        "Vindemiatrix",
        "Sadachbia",
        "Sadaltager",
        "Sulafat",
    )
)
TRANSCRIPT_PROMPT = (
    "Transcribe the source audio in its original language. Return only a JSON object "
    "with keys language (language code or null) and segments (array). Each segment must "
    "have start_ms and end_ms as integer milliseconds from the start of this audio and "
    "text as nonempty spoken text. Estimate nonoverlapping chronological speech times; "
    "do not invent words, translations, speakers or word-level alignment. Use an empty "
    "segments array only if no speech is audible. These timestamps will be treated as "
    "uncertain model estimates requiring alignment review."
)


class PendingExternalResult(UnknownExternalResult):
    """A known task remains resumable; retry its GET, never its create POST."""

    def __init__(self, message: str, task_id: str):
        super().__init__(message)
        self.task_id = task_id


@dataclass
class KieConfig:
    api_key: str = field(default="", repr=False)
    enabled_model_calls: bool = False
    timeout: float = 120
    poll_seconds: float = 2
    max_wait: float = 60
    voice: str = "Kore"
    chat_model: str = "gemini-3.1-pro"
    chat_path: str = CHAT_ROUTE
    chat_protocol: str = "gemini_audio_chat"
    tts_model: str = TTS_MODEL
    tts_protocol: str = "gemini_speakers"
    tts_options: dict = field(default_factory=dict)
    image_model: str = "gpt-image-2-image-to-image"
    video_model: str = VIDEO_MODEL
    video_protocol: str = "wan_video_edit"
    video_options: dict = field(default_factory=dict)
    max_upload_bytes: int = 100 * 1024 * 1024
    max_download_bytes: int = 500 * 1024 * 1024
    max_response_bytes: int = 4 * 1024 * 1024

    def __post_init__(self):
        for name in ("timeout", "poll_seconds", "max_wait"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid KIE {name}")
            if value < 0 or (name != "max_wait" and value == 0):
                raise ValueError(f"Invalid KIE {name}")
        for name in ("max_upload_bytes", "max_download_bytes", "max_response_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"Invalid KIE {name}")
        if self.voice not in VOICES:
            raise ValueError("Unsupported Gemini TTS voice_name")
        if type(self.enabled_model_calls) is not bool:
            raise ValueError("KIE enabled_model_calls must be a boolean")
        if not isinstance(self.tts_options, dict) or not isinstance(self.video_options, dict):
            raise ValueError("KIE model options must be objects")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.chat_model, self.tts_model, self.video_model)
        ):
            raise ValueError("KIE model identifiers must be nonempty")
        import re

        if not re.fullmatch(r"/[A-Za-z0-9._-]+/v1/chat/completions", self.chat_path):
            raise ValueError("KIE chat_path must be a documented relative chat completions path")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(
            api_key=env.get("VT_KIE_API_KEY", env.get("KIE_API_KEY", "")),
            enabled_model_calls=str(env.get("VT_KIE_ENABLED", "")).lower() in {"1", "true", "yes"},
            timeout=float(env.get("VT_KIE_TIMEOUT", 120)),
            poll_seconds=float(env.get("VT_KIE_POLL_SECONDS", 2)),
            max_wait=float(env.get("VT_KIE_MAX_WAIT", 60)),
            voice=env.get("VT_KIE_VOICE", "Kore"),
            chat_model=env.get("VT_KIE_CHAT_MODEL", "gemini-3.1-pro"),
            chat_path=env.get("VT_KIE_CHAT_PATH", CHAT_ROUTE),
            chat_protocol=env.get("VT_KIE_CHAT_PROTOCOL", "gemini_audio_chat"),
            tts_model=env.get("VT_KIE_TTS_MODEL", TTS_MODEL),
            tts_protocol=env.get("VT_KIE_TTS_PROTOCOL", "gemini_speakers"),
            tts_options=json.loads(env.get("VT_KIE_TTS_OPTIONS", "{}")),
            video_model=env.get("VT_KIE_VIDEO_MODEL", VIDEO_MODEL),
            video_protocol=env.get("VT_KIE_VIDEO_PROTOCOL", "wan_video_edit"),
            video_options=json.loads(env.get("VT_KIE_VIDEO_OPTIONS", "{}")),
        )


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _url_shape(url: str):
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
            and not parts.fragment
            and not any(c.isspace() for c in url)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderError("Media URL must be public HTTPS without credentials or redirects")
    return parts


def validate_public_url(url: str) -> str:
    """Resolve before each download; reject every private, reserved and loopback address."""
    parts = _url_shape(url)
    try:
        addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ProviderError("Media URL resolves to a non-public address")
    except (OSError, ValueError):
        raise ProviderError("Media URL could not be validated") from None
    return url


def build_wan_payload(
    video_url: str,
    prompt: str,
    *,
    duration_seconds: float,
    width: int,
    height: int,
    size_bytes: int,
    resolution: str = "720p",
    aspect_ratio: str | None = None,
    negative_prompt: str = "",
    model: str = VIDEO_MODEL,
) -> dict:
    """Build only the verified single-clip, original-audio contract; no implicit retiming."""
    _url_shape(video_url)
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(duration_seconds)
        or not 2 <= duration_seconds <= 10
    ):
        raise ValueError("Wan source duration must be 2-10 seconds")
    if any(type(value) is not int or not 240 <= value <= 4096 for value in (width, height)):
        raise ValueError("Wan source dimensions must each be 240-4096 pixels")
    if not 1 / 8 <= width / height <= 8:
        raise ValueError("Wan source aspect ratio is outside 1:8-8:1")
    if type(size_bytes) is not int or not 0 < size_bytes <= 100_000_000:
        raise ValueError("Wan source must be nonempty and at most 100MB")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 5000:
        raise ValueError("Wan editing prompt must contain 1-5000 characters")
    if not isinstance(negative_prompt, str) or len(negative_prompt) > 500:
        raise ValueError("Wan negative prompt exceeds 500 characters")
    if resolution not in ("720p", "1080p"):
        raise ValueError("Wan resolution must be 720p or 1080p")
    if aspect_ratio is not None and aspect_ratio not in ("16:9", "9:16", "1:1", "4:3", "3:4"):
        raise ValueError("Unsupported Wan output aspect ratio")
    inputs = {
        "video_url": video_url,
        "prompt": prompt,
        "duration": 0,
        "resolution": resolution,
        "audio_setting": "origin",
        "prompt_extend": False,
        "watermark": False,
    }
    if aspect_ratio is not None:
        inputs["aspect_ratio"] = aspect_ratio
    if negative_prompt:
        inputs["negative_prompt"] = negative_prompt
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Wan model identifier is required")
    if model == WAN3_MODEL:
        raise ValueError("Wan 3.0 requires its reference-video protocol")
    return {"model": model, "input": inputs}


def build_wan3_payload(video_url, prompt, *, duration_seconds, width, height,
                       size_bytes, resolution="720p", aspect_ratio=None, seed=None):
    """One source reference and explicit matching output seconds; original audio stays local."""
    # Share bounded source validation, never reuse the legacy request fields.
    build_wan_payload(video_url, prompt[:5000] if isinstance(prompt, str) else prompt,
                      duration_seconds=duration_seconds, width=width, height=height,
                      size_bytes=size_bytes, resolution=resolution)
    if len(prompt) > 20000:
        raise ValueError("Wan 3.0 prompt exceeds 20000 characters")
    duration = round(duration_seconds)
    if abs(duration_seconds - duration) > .05:
        raise ValueError("Wan 3.0 reference must be padded locally to integer seconds before upload")
    if aspect_ratio not in {None, "adaptive", "16:9", "9:16", "1:1", "4:3", "3:4"}:
        raise ValueError("Unsupported Wan 3.0 aspect ratio")
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 2147483647):
        raise ValueError("Wan 3.0 seed must be in 0-2147483647")
    inputs = {"reference_video_urls": [video_url],
              "prompt": "Use Video1 as the source timeline. Preserve its framing and motion. " + prompt,
              "duration": duration, "resolution": resolution.upper(),
              "aspect_ratio": aspect_ratio or "adaptive", "audio": False}
    if len(inputs["prompt"]) > 20000:
        raise ValueError("Wan 3.0 prompt exceeds 20000 characters including source reference")
    if seed is not None:
        inputs["seed"] = seed
    return {"model": WAN3_MODEL, "input": inputs}


class KieClient:
    def __init__(self, config: KieConfig, client=None, directory: Path | str = "runtime/kie"):
        self.config = config
        self.client = client
        self.directory = Path(directory)

    def _enabled(self):
        if not self.config.enabled_model_calls:
            raise CapabilityUnavailable("KIE model calls are disabled (VT_KIE_ENABLED)")
        self._key()

    def _key(self):
        if not self.config.api_key:
            raise CapabilityUnavailable("KIE API key is not configured")

    def _safe(self, value):
        if isinstance(value, str):
            return value.replace(self.config.api_key, "[REDACTED]") if self.config.api_key else value
        if isinstance(value, dict):
            return {
                key: self._safe(item)
                for key, item in value.items()
                if key.lower() not in {"authorization", "api_key", "apikey", "token", "cookie"}
            }
        if isinstance(value, list):
            return [self._safe(item) for item in value]
        return value

    def _path(self, request_key: str) -> Path:
        if not isinstance(request_key, str) or not request_key or len(request_key) > 2048:
            raise ValueError("KIE request_key must be a nonempty stable identifier")
        return self.directory / (hashlib.sha256(request_key.encode()).hexdigest() + ".json")

    @contextmanager
    def _locked(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise UnknownExternalResult(
                "KIE request is locked; inspect existing process/checkpoint"
            ) from None
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(str(os.getpid()))
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            # Process crashes leave the lock in place. Never steal a possibly live request.
            lock.unlink(missing_ok=True)

    def _save(self, path: Path, record: dict):
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix="kie-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self._safe(record), stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _load(self, path: Path, fingerprint: str):
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ValueError
        except (OSError, ValueError):
            raise UnknownExternalResult(
                "KIE checkpoint is unreadable; submission must not be replayed"
            ) from None
        if record.get("fingerprint") != fingerprint:
            raise ProviderError("KIE request_key conflicts with an existing different payload")
        return record

    def _event(self, callback, phase: str, record: dict):
        if callback is not None:
            event = self._safe(
                {
                    "phase": phase,
                    "task_id": record.get("task_id"),
                    "state": record.get("state"),
                    "model": record.get("model"),
                    "credits_consumed": record.get("credits_consumed"),
                }
            )
            # The durable checkpoint is authoritative even if the caller's event sink fails.
            try:
                callback(event)
            except Exception:
                pass

    def _json(self, method: str, url: str, **kwargs):
        self._key()
        try:
            raw = request_bytes(
                self.client,
                method,
                url,
                timeout=self.config.timeout,
                max_bytes=self.config.max_response_bytes,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                follow_redirects=False,
                **kwargs,
            )
        except ProviderError as exc:
            if method == "POST" and not str(exc).startswith("Provider HTTP 4"):
                raise UnknownExternalResult(
                    "KIE POST response could not be fully read; outcome unknown"
                ) from None
            raise
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise UnknownExternalResult("KIE returned an unreadable response") from None
        code = result.get("code", 200)
        if code != 200 or result.get("success") is False:
            if str(code) in {"408", "500", "501", "502", "503", "504"}:
                raise UnknownExternalResult("KIE response did not establish a known submission outcome")
            raise ProviderError("KIE rejected the request; inspect safe status metadata")
        return result

    def upload(self, path: Path | str) -> str:
        self._enabled()
        path = Path(path)
        if not path.is_file() or not 0 < path.stat().st_size <= self.config.max_upload_bytes:
            raise ValueError("KIE input file missing, empty or larger than the configured upload limit")
        with path.open("rb") as stream:
            name = hashlib.sha256(os.urandom(32)).hexdigest() + path.suffix.lower()
            result = self._json(
                "POST",
                UPLOAD,
                data={"uploadPath": "video-translation", "fileName": name},
                files={"file": (name, stream, mimetypes.guess_type(name)[0] or "application/octet-stream")},
            )
        data = result.get("data")
        url = data.get("downloadUrl") if isinstance(data, dict) else None
        if not isinstance(url, str):
            raise ProviderError("KIE upload returned no media URL")
        _url_shape(url)
        return url

    def _asset_url(self, source: Path, request_key: str) -> str:
        fingerprint = _digest({"source_sha256": _file_digest(source), "suffix": source.suffix.lower()})
        path = self._path("asset:" + request_key)
        with self._locked(path):
            record = self._load(path, fingerprint)
            if record is not None:
                age = time.time() - record.get("uploaded_at", 0)
                if 0 <= age < 24 * 60 * 60 or self._path(request_key).exists():
                    # An accepted/unknown task freezes the original payload even after source URL expiry.
                    return record["url"]
            url = self.upload(source)
            self._save(
                path, {"version": 1, "fingerprint": fingerprint, "url": url, "uploaded_at": time.time()}
            )
            return url

    @staticmethod
    def _credits(value):
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ProviderError("KIE returned invalid credit consumption")
        return value

    @staticmethod
    def _result(record):
        return {
            "taskId": record["task_id"],
            "state": record["state"],
            "resultUrls": record.get("result_urls", []),
            "resultObject": record.get("result_object", {}),
            "creditsConsumed": record.get("credits_consumed"),
        }

    def job(self, model: str, inputs: dict, request_key: str, on_event=None) -> dict:
        if model not in {self.config.tts_model, self.config.video_model, self.config.image_model} or not isinstance(inputs, dict):
            raise ValueError("KIE job model must match a configured task capability")
        body = {"model": model, "input": inputs}
        fingerprint = _digest(body)
        path = self._path(request_key)
        with self._locked(path):
            record = self._load(path, fingerprint)
            if record is None:
                self._enabled()
                record = {
                    "version": 1,
                    "fingerprint": fingerprint,
                    "model": model,
                    "state": "submit-intent",
                    "task_id": None,
                    "credits_consumed": None,
                }
                # This write/fsync/replace MUST precede the billable POST.
                self._save(path, record)
                self._event(on_event, "submit-intent", record)
                try:
                    result = self._json("POST", API + "/api/v1/jobs/createTask", json=body)
                except ProviderError:
                    record["state"] = "rejected"
                    self._save(path, record)
                    self._event(on_event, "rejected", record)
                    raise
                except Exception:
                    record["state"] = "unknown"
                    self._save(path, record)
                    self._event(on_event, "unknown", record)
                    raise UnknownExternalResult("KIE submission outcome unknown; do not resubmit") from None
                task_id = (
                    result.get("data", {}).get("taskId") if isinstance(result.get("data"), dict) else None
                )
                if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 512:
                    record["state"] = "unknown"
                    self._save(path, record)
                    raise UnknownExternalResult("KIE submission returned no usable taskId; do not resubmit")
                record.update(state="submitted", task_id=task_id)
                self._save(path, record)
                self._event(on_event, "submitted", record)
            elif record.get("state") == "rejected":
                raise ProviderError("KIE previously rejected this request; use explicit revised input")
            elif not record.get("task_id"):
                raise UnknownExternalResult("KIE submit intent has no taskId; do not resubmit")
            if record["state"] == "success":
                self._event(on_event, "success", record)
                return self._result(record)
            if record["state"] == "fail":
                self._event(on_event, "fail", record)
                raise self._confirmed_failure(record)
            return self._poll(path, record, on_event)

    def _poll(self, path: Path, record: dict, on_event):
        deadline = time.monotonic() + self.config.max_wait
        task_id = record["task_id"]
        while True:
            try:
                result = self._json("GET", API + "/api/v1/jobs/recordInfo", params={"taskId": task_id})
                data = result.get("data")
                if not isinstance(data, dict) or data.get("state") not in {
                    "waiting",
                    "queuing",
                    "generating",
                    "success",
                    "fail",
                }:
                    raise ProviderError("KIE task status is not understood")
                if (
                    data.get("taskId", task_id) != task_id
                    or data.get("model", record["model"]) != record["model"]
                ):
                    raise ProviderError("KIE task identity mismatch")
                credits = self._credits(data.get("creditsConsumed"))
                state = data["state"]
                # Persist the receipt first. Invalid success output remains queryable by the original ID.
                record["provider_receipt"] = {
                    key: self._safe(data[key])
                    for key in (
                        "state",
                        "resultJson",
                        "response",
                        "creditsConsumed",
                        "failCode",
                        "failMsg",
                        "costTime",
                        "createTime",
                        "completeTime",
                    )
                    if key in data
                }
                if credits is not None:
                    record["credits_consumed"] = credits
                self._save(path, record)
                updates = {"state": state}
                if state == "success":
                    output = data.get("response")
                    if not isinstance(output, dict):
                        raw = data.get("resultJson")
                        output = json.loads(raw) if isinstance(raw, str) else raw
                    if not isinstance(output, dict):
                        raise ProviderError("KIE successful task returned invalid result JSON")
                    urls, obj = output.get("resultUrls", []), output.get("resultObject", {})
                    if (
                        not isinstance(urls, list)
                        or len(urls) > 100
                        or not all(isinstance(url, str) for url in urls)
                        or not isinstance(obj, dict)
                    ):
                        raise ProviderError("KIE result media structure is invalid")
                    updates.update(result_urls=urls, result_object=obj)
                record.update(updates)
                self._save(path, record)
            except Exception:
                # GET failures cannot establish task failure and never erase its ID.
                self._event(on_event, "poll", record)
                raise PendingExternalResult(
                    "KIE status unavailable; resume polling the existing task", task_id
                ) from None
            self._event(on_event, "poll", record)
            if record["state"] == "success":
                self._event(on_event, "success", record)
                return self._result(record)
            if record["state"] == "fail":
                self._event(on_event, "fail", record)
                raise self._confirmed_failure(record)
            if time.monotonic() >= deadline:
                raise PendingExternalResult("KIE task is still pending; resume the existing task", task_id)
            time.sleep(min(self.config.poll_seconds, max(0, deadline - time.monotonic())))

    @staticmethod
    def _confirmed_failure(record):
        receipt = record.get("provider_receipt", {})
        reason = str(receipt.get("failMsg", "")).lower()
        if "throttl" in reason or "rate limit" in reason:
            return ConfirmedJobFailure("KIE服务商限流，原请求已确认失败；不要重复提交相同参数，优先复用候选或改用本地方案")
        return ConfirmedJobFailure("KIE原请求已确认失败；保留回执，不重复提交相同参数，需重新评估方案")

    def download(self, url: str, output: Path | str) -> Path:
        validate_public_url(url)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        owned = self.client is None
        client = self.client or httpx.Client(follow_redirects=False)
        temporary = None
        try:
            request = client.build_request("GET", url, timeout=self.config.timeout)
            for header in ("authorization", "proxy-authorization", "cookie"):
                request.headers.pop(header, None)
            with closing(client.send(request, stream=True, follow_redirects=False, auth=None)) as response:
                if response.status_code != 200:
                    raise ProviderError("KIE media download failed or attempted a redirect")
                size = 0
                with NamedTemporaryFile(
                    dir=output.parent, prefix="kie-download-", suffix=".part", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.config.max_download_bytes:
                            raise ProviderError("KIE media exceeds download limit")
                        stream.write(chunk)
                    if size == 0:
                        raise ProviderError("KIE media download is empty")
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(temporary, output)
        except (httpx.HTTPError, OSError):
            raise ProviderError(
                "KIE media download interrupted; retry download using the completed task"
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if owned:
                client.close()
        return output

    @staticmethod
    def _transcript(content):
        try:
            if isinstance(content, str):
                content = content.strip()
                fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", content, re.IGNORECASE)
                if fenced:
                    content = fenced.group(1)
            result = json.loads(content)
            rows = result["segments"]
            language = result.get("language")
            if (
                not isinstance(rows, list)
                or len(rows) > 10000
                or not (language is None or isinstance(language, str))
            ):
                raise ValueError
            segments = []
            previous_end = 0
            for item in rows:
                start, end, text = item["start_ms"], item["end_ms"], item["text"]
                if (
                    type(start) is not int
                    or type(end) is not int
                    or start < previous_end
                    or end <= start
                    or not isinstance(text, str)
                    or not text.strip()
                    or len(text) > 10000
                ):
                    raise ValueError
                segments.append({"start_ms": start, "end_ms": end, "text": text, "uncertain": True})
                previous_end = end
            return {
                "text": " ".join(row["text"] for row in segments),
                "language": language,
                "segments": segments,
                "uncertain": True,
                "timing_source": "model_estimate",
            }
        except (ValueError, TypeError, KeyError, IndexError):
            raise ProviderError(
                "Gemini transcription must contain strict chronological millisecond segments"
            ) from None

    def chat_transcribe(self, audio: Path | str, request_key: str | None = None, on_event=None) -> dict:
        if self.config.chat_protocol != "gemini_audio_chat":
            raise CapabilityUnavailable("Configured transcription protocol is not implemented by KIE client")
        audio = Path(audio)
        if not audio.is_file() or not 0 < audio.stat().st_size <= self.config.max_upload_bytes:
            raise ValueError("Transcription audio missing, empty or too large")
        fingerprint = _digest(
            {
                "source_sha256": _file_digest(audio),
                "model": self.config.chat_model,
                "route": self.config.chat_path,
                "prompt": TRANSCRIPT_PROMPT,
            }
        )
        request_key = request_key or "transcribe:" + fingerprint
        path = self._path(request_key)
        with self._locked(path):
            record = self._load(path, fingerprint)
            if record is not None:
                if record.get("state") == "success":
                    self._event(on_event, "success", record)
                    return record["transcript"]
                raise UnknownExternalResult(
                    "Synchronous Gemini request already attempted; no task query or automatic replay"
                )
            self._enabled()
            url = self._asset_url(audio, request_key)
            body = {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": TRANSCRIPT_PROMPT},
                            {"type": "image_url", "image_url": {"url": url}},
                        ],
                    }
                ],
                "stream": False,
                "reasoning_effort": "low",
            }
            record = {
                "version": 1,
                "fingerprint": fingerprint,
                "model": self.config.chat_model,
                "state": "submit-intent",
                "task_id": None,
                "credits_consumed": None,
            }
            self._save(path, record)
            self._event(on_event, "submit-intent", record)
            try:
                result = self._json("POST", API + self.config.chat_path, json=body)
            except ProviderError:
                record["state"] = "rejected"
                self._save(path, record)
                raise
            except Exception:
                record["state"] = "unknown"
                self._save(path, record)
                self._event(on_event, "unknown", record)
                raise UnknownExternalResult(
                    "Gemini synchronous request outcome unknown; do not resubmit"
                ) from None
            # Preserve receipt and billing BEFORE parsing content; bad JSON cannot erase cost evidence.
            record["state"] = "response-invalid"
            record["response"] = self._safe(result)
            usage = result.get("usage", {})
            credit = result.get(
                "credits_consumed", usage.get("credits_consumed") if isinstance(usage, dict) else None
            )
            self._save(path, record)
            record["credits_consumed"] = self._credits(credit)
            self._save(path, record)
            self._event(on_event, "response", record)
            try:
                record["transcript"] = self._transcript(result["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError):
                raise ProviderError("Gemini chat response has no usable transcription") from None
            record["state"] = "success"
            self._save(path, record)
            self._event(on_event, "success", record)
            return record["transcript"]

    def transcribe(self, audio: Path | str, **kwargs) -> dict:
        return self.chat_transcribe(audio, **kwargs)

    def synthesize(
        self, text: str, locale: str, output: Path | str, request_key: str | None = None, on_event=None
    ) -> Path:
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError("Gemini TTS text must contain 1-10000 characters")
        if not isinstance(locale, str) or not locale.strip() or len(locale) > 100:
            raise ValueError("TTS target locale is required")
        if self.config.tts_protocol not in {"gemini_speakers", "gemini38_speakers"}:
            raise CapabilityUnavailable("Configured TTS protocol is not implemented by KIE client")
        options = dict(self.config.tts_options)
        allowed = {
            "voice_name",
            "accent",
            "audio_profile",
            "style",
            "pace",
            "temperature",
            "scene",
            "sample_context",
        }
        if set(options) - allowed:
            raise ValueError("Unsupported Gemini speaker TTS option")
        speaker = {
            "speaker_id": "Speaker 1",
            "voice_name": options.pop("voice_name", self.config.voice),
            "accent": options.pop("accent", "Neutral"),
        }
        if speaker["voice_name"] not in VOICES or speaker["accent"] not in {
            "Neutral",
            "American (Gen)",
            "American (Valley)",
            "American (South)",
            "British (RP)",
            "British (Brixton)",
            "Transatlantic",
            "Australian",
        }:
            raise ValueError("Unsupported Gemini voice or accent")
        for key, choices in {
            "style": {"Vocal Smile", "Newscaster", "Whisper", "Empathetic", "Promo/Hype", "Deadpan"},
            "pace": {"Natural", "Rapid Fire", "The Drift", "Staccato"},
        }.items():
            if key in options:
                if options[key] not in choices:
                    raise ValueError(f"Unsupported Gemini {key}")
                speaker[key] = options.pop(key)
        if "audio_profile" in options:
            speaker["audio_profile"] = options.pop("audio_profile")
            if not isinstance(speaker["audio_profile"], str):
                raise ValueError("Gemini audio_profile must be a string")
        if "temperature" in options:
            value = options["temperature"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 2:
                raise ValueError("Gemini temperature must be 0-2")
        if any(not isinstance(options[key], str) for key in ("scene", "sample_context") if key in options):
            raise ValueError("Gemini scene and sample_context must be strings")
        inputs = {
            "speakers": [speaker],
            "dialogue_turns": [{"speaker_id": "Speaker 1", "text": text}],
            "scene": f"Read only the exact supplied text in {locale}. Voice only, no music or additional speech.",
            **options,
        }
        if self.config.tts_protocol == "gemini38_speakers":
            if set(self.config.tts_options) - {"voice_name", "temperature"}:
                raise ValueError("Gemini 3.8 supports preset voice and temperature in this adapter")
            speaker.pop("accent")
            inputs.pop("scene")
        result = self.job(self.config.tts_model, inputs, request_key or "tts:" + _digest(inputs), on_event)
        if len(result["resultUrls"]) != 1:
            raise ProviderError("Gemini TTS must return exactly one audio file")
        output = self.download(result["resultUrls"][0], output)
        try:
            with wave.open(io.BytesIO(output.read_bytes()), "rb") as audio:
                expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or audio.getframerate() != 24000
                    or expected <= 0
                    or len(audio.readframes(audio.getnframes())) != expected
                ):
                    raise ValueError
        except (wave.Error, EOFError, ValueError):
            raise ProviderError("Gemini TTS output is not a complete 24kHz mono PCM16 WAV") from None
        return output

    def video_edit(
        self,
        source: Path | str,
        output: Path | str,
        prompt: str,
        media: dict,
        request_key: str,
        on_event=None,
    ) -> Path:
        if self.config.video_protocol not in {"wan_video_edit", "wan_reference_video"}:
            raise CapabilityUnavailable("Configured video editing protocol is not implemented by KIE client")
        source = Path(source)
        if not source.is_file() or source.suffix.lower() not in {".mp4", ".mov"}:
            raise ValueError("Wan editing requires an existing MP4 or MOV clip")
        options = dict(self.config.video_options)
        for key in ("price", "price_credits_per_second", "credits_per_second"):
            options.pop(key, None)  # Local estimates are never sent as model input.
        if self.config.video_protocol == "wan_reference_video":
            return self._wan3_edit(source, output, prompt, media, options, request_key, on_event)
        if set(options) - {
            "resolution",
            "aspect_ratio",
            "negative_prompt",
            "audio_setting",
            "prompt_extend",
            "watermark",
            "seed",
        }:
            raise ValueError("Unsupported Wan video option")
        extra = {
            key: options.pop(key)
            for key in ("audio_setting", "prompt_extend", "watermark", "seed")
            if key in options
        }
        if extra.get("audio_setting", "origin") not in {"auto", "origin"}:
            raise ValueError("Wan audio_setting must be auto or origin")
        if any(type(extra[key]) is not bool for key in ("prompt_extend", "watermark") if key in extra):
            raise ValueError("Wan prompt_extend and watermark must be booleans")
        if "seed" in extra and (type(extra["seed"]) is not int or not 0 <= extra["seed"] <= 2147483647):
            raise ValueError("Wan seed must be in 0-2147483647")
        duration = media.get("duration_seconds", media.get("duration"))
        if duration is None and type(media.get("duration_ms")) in {int, float}:
            duration = media["duration_ms"] / 1000
        arguments = dict(
            duration_seconds=duration,
            width=media.get("width"),
            height=media.get("height"),
            size_bytes=source.stat().st_size,
            resolution=media.get("resolution", options.get("resolution", "720p")),
            aspect_ratio=media.get("aspect_ratio", options.get("aspect_ratio")),
            negative_prompt=media.get("negative_prompt", options.get("negative_prompt", "")),
            model=self.config.video_model,
        )
        # Validate all bounded clip requirements before any upload.
        build_wan_payload("https://example.com/preflight.mp4", prompt, **arguments)
        url = self._asset_url(source, request_key)
        payload = build_wan_payload(url, prompt, **arguments)
        payload["input"].update(extra)
        result = self.job(payload["model"], payload["input"], request_key, on_event)
        if len(result["resultUrls"]) != 1:
            raise ProviderError("Wan editing must return exactly one video file")
        return self.download(result["resultUrls"][0], output)

    def _wan3_edit(self, source, output, prompt, media, options, request_key, on_event):
        if self.config.video_model != WAN3_MODEL:
            raise ValueError("Wan 3.0 protocol requires the verified wan/3-0-video model")
        if set(options) - {"resolution", "aspect_ratio", "seed"}:
            raise ValueError("Unsupported Wan 3.0 option; legacy video-edit fields cannot be reused")
        duration = media.get("duration_seconds", media.get("duration"))
        if duration is None and type(media.get("duration_ms")) in {int, float}:
            duration = media["duration_ms"] / 1000
        arguments = dict(duration_seconds=duration, width=media.get("width"), height=media.get("height"),
                         size_bytes=source.stat().st_size,
                         resolution=media.get("resolution", options.get("resolution", "720p")),
                         aspect_ratio=options.get("aspect_ratio"), seed=options.get("seed"))
        build_wan3_payload("https://example.com/preflight.mp4", prompt, **arguments)
        url = self._asset_url(source, request_key)
        payload = build_wan3_payload(url, prompt, **arguments)
        result = self.job(payload["model"], payload["input"], request_key, on_event)
        if len(result["resultUrls"]) != 1:
            raise ProviderError("Wan 3.0 must return exactly one video file")
        return self.download(result["resultUrls"][0], output)
