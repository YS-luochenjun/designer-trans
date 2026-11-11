"""OpenAI-compatible speech APIs. No implicit paid request retries."""

from __future__ import annotations
import io
import json
import math
import os
import wave
from tempfile import NamedTemporaryFile
from dataclasses import dataclass, field
from pathlib import Path
from ..agents.errors import CapabilityUnavailable, ProviderError
from ..agents.transport import request_bytes


@dataclass
class ProviderConfig:
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    model: str = ""
    voice: str = "alloy"
    timeout: float = 120
    max_audio_bytes: int = 25 * 1024 * 1024
    max_response_bytes: int = 50 * 1024 * 1024

    @classmethod
    def from_env(cls, kind: str, env=None):
        env = os.environ if env is None else env
        prefix = "VT_" + kind.upper() + "_"
        return cls(
            base_url=env.get(prefix + "BASE_URL", ""),
            api_key=env.get(prefix + "API_KEY", ""),
            model=env.get(prefix + "MODEL", ""),
            voice=env.get(prefix + "VOICE", "alloy"),
        )


class _Provider:
    def __init__(self, config: ProviderConfig | None = None, client=None):
        self.config = config or ProviderConfig()
        self.client = client

    def _post(self, route, **kwargs):
        cfg = self.config
        if not cfg.base_url or not cfg.model:
            raise CapabilityUnavailable("Speech endpoint and model are not configured")
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        return request_bytes(
            self.client,
            "POST",
            cfg.base_url.rstrip("/") + route,
            timeout=cfg.timeout,
            max_bytes=cfg.max_response_bytes,
            headers=headers,
            **kwargs,
        )


class Transcriber(_Provider):
    def transcribe(self, audio: Path) -> dict:
        cfg = self.config
        if not cfg.base_url or not cfg.model:
            raise CapabilityUnavailable("ASR is not configured")
        audio = Path(audio)
        if not audio.is_file() or audio.stat().st_size > cfg.max_audio_bytes:
            raise ValueError("ASR audio missing or exceeds size limit")
        with audio.open("rb") as stream:
            raw = self._post(
                "/audio/transcriptions",
                data={
                    "model": cfg.model,
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": "segment",
                },
                files={"file": (audio.name, stream, "application/octet-stream")},
            )
        try:
            result = json.loads(raw)
            segments = []
            for item in result.get("segments", []):
                start, end = float(item["start"]), float(item["end"])
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                    raise ValueError("invalid segment times")
                segments.append(
                    {"start_ms": round(start * 1000), "end_ms": round(end * 1000), "text": str(item["text"])}
                )
            if not segments and str(result.get("text", "")).strip():
                raise ProviderError("ASR endpoint returned text without required segment timestamps")
            return {
                "text": str(result.get("text", "")),
                "language": result.get("language"),
                "segments": segments,
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError("ASR response has invalid timestamps or JSON") from None


class SpeechSynthesizer(_Provider):
    def synthesize(self, text: str, locale: str, output: Path) -> Path:
        if not text.strip() or len(text) > 20000:
            raise ValueError("TTS text must contain 1-20000 characters")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Generic compatible APIs infer language from input. No unsupported locale field.
        raw = self._post(
            "/audio/speech",
            json={
                "model": self.config.model,
                "voice": self.config.voice,
                "input": text,
                "response_format": "wav",
            },
        )
        if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            raise ProviderError("TTS endpoint did not return requested WAV audio")
        try:
            with wave.open(io.BytesIO(raw), "rb") as audio:
                expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                if (
                    expected <= 0
                    or expected > self.config.max_response_bytes
                    or len(audio.readframes(audio.getnframes())) != expected
                ):
                    raise ValueError("Truncated WAV")
        except (wave.Error, EOFError, ValueError):
            raise ProviderError("TTS endpoint returned invalid or truncated WAV audio") from None
        temporary = None
        try:
            with NamedTemporaryFile(
                prefix="speech-", suffix=".wav", dir=output.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            os.replace(temporary, output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return output
