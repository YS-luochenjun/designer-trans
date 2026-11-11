"""Isolated CPU source separation with verified weights and immutable receipts."""
from __future__ import annotations
import hashlib
import json
import math
import os
import signal
import subprocess
import time
import uuid
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

MODEL_ID = "uvr-inst-hq3"
MODEL_NAME = "UVR-MDX-NET-Inst_HQ_3.onnx"
MODEL_SHA256 = "c207b845d51dc1303c543503f6f5a8b596da5d7de97bead77b3e16ec38e8d5b3"
ENGINE_VERSION = "1.13.8"
NUMPY_VERSION = "2.2.6"
PROVIDER = "sherpa-onnx-cpu"


class SeparationError(RuntimeError):
    """A bounded local operation failed; never substitute the original mix."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_process(args, *, timeout, cwd, env=None):
    """Bound runtime and diagnostics; terminate the entire process tree on timeout."""
    temporary = Path(cwd) / "runtime/tmp/separation-process"
    temporary.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="process-", dir=temporary) as scratch, \
            TemporaryFile(dir=scratch) as stdout, TemporaryFile(dir=scratch) as stderr:
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {
            "start_new_session": True
        }
        try:
            process = subprocess.Popen(
                [str(arg) for arg in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, shell=False, **options,
            )
        except OSError:
            raise SeparationError("Separation executable unavailable") from None
        try:
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)
            raise SeparationError("Separation process exceeded timeout or was interrupted") from None
        if process.returncode:
            raise SeparationError(f"Separation process failed (exit {process.returncode}); no fallback produced")
        stdout.seek(0)
        data = stdout.read(65537)
        if len(data) > 65536:
            raise SeparationError("Separation process response exceeds limit")
        return data


def wav_info(path: Path, *, expected_duration=None) -> dict:
    """Validate complete PCM bytes; signed PCM cannot represent NaN or infinity."""
    try:
        with wave.open(str(path), "rb") as stream:
            rate, channels, width = stream.getframerate(), stream.getnchannels(), stream.getsampwidth()
            frames = stream.getnframes()
            if (rate, channels, width, stream.getcomptype()) != (44100, 2, 2, "NONE") or frames <= 0:
                raise SeparationError("Separation requires nonempty 44.1 kHz stereo signed 16-bit PCM")
            total = 0
            while block := stream.readframes(65536):
                total += len(block)
            if total != frames * channels * width:
                raise SeparationError("Separation WAV is truncated")
    except (OSError, wave.Error, EOFError):
        raise SeparationError("Separation WAV cannot be decoded") from None
    duration = frames / rate
    if not math.isfinite(duration) or (expected_duration is not None and abs(duration - expected_duration) > .25):
        raise SeparationError("Separation stem duration differs by more than 250 ms")
    return {"duration_seconds": duration, "sample_rate": rate, "channels": channels, "samples": frames}


@dataclass(frozen=True)
class SeparationConfig:
    root: Path
    threads: int = 4
    timeout: float = 600
    lock_timeout: float = 30
    max_source_seconds: float = 600

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).resolve())
        if not 1 <= self.threads <= 8:
            raise ValueError("Separation threads must be between 1 and 8")
        for value in (self.timeout, self.lock_timeout, self.max_source_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Separation limits must be finite and positive")

    @classmethod
    def from_env(cls, root):
        return cls(Path(root), threads=int(os.environ.get("VT_SEPARATION_THREADS", "4")),
                   timeout=float(os.environ.get("VT_SEPARATION_TIMEOUT_SECONDS", "600")),
                   lock_timeout=float(os.environ.get("VT_SEPARATION_LOCK_TIMEOUT_SECONDS", "30")),
                   max_source_seconds=float(os.environ.get("VT_SEPARATION_MAX_SOURCE_SECONDS", "600")))

    @property
    def python(self):
        return self.root / ".venv-audio" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @property
    def model(self):
        return self.root / "runtime/models/separation" / MODEL_NAME

    @property
    def worker(self):
        return self.root / "scripts/separate_audio.py"

    def snapshot(self):
        return {"provider": PROVIDER, "model_id": MODEL_ID, "model_sha256": MODEL_SHA256,
                "engine_version": ENGINE_VERSION, "threads": self.threads}

    def status(self):
        result = {**self.snapshot(), "ready": False, "reason": "not_installed",
                  "python_present": self.python.is_file(), "model_present": self.model.is_file(),
                  "installed_engine_version": None}
        if not result["python_present"] or not result["model_present"] or not self.worker.is_file():
            return result
        if sha256(self.model) != MODEL_SHA256:
            return {**result, "reason": "model_checksum_mismatch"}
        try:
            installed = json.loads(run_process([self.python, "-B", self.worker, "--worker-status"],
                                              timeout=30, cwd=self.root))
        except (SeparationError, ValueError):
            return {**result, "reason": "dependency_unavailable"}
        if not isinstance(installed, dict):
            return {**result, "reason": "dependency_unavailable"}
        result["installed_engine_version"] = installed.get("engine_version")
        if installed.get("engine_version") != ENGINE_VERSION or installed.get("numpy_version") != NUMPY_VERSION:
            return {**result, "reason": "dependency_version_mismatch"}
        return {**result, "ready": True, "reason": "ready"}


@contextmanager
def cache_lock(path: Path, timeout: float):
    token = uuid.uuid4().hex
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(token)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SeparationError("Separation cache busy; retry after the active worker completes") from None
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        try:
            if path.read_text(encoding="utf-8") == token:
                path.unlink()
        except FileNotFoundError:
            pass


class Separator:
    def __init__(self, config: SeparationConfig):
        self.config = config

    def _cached(self, directory, identity, duration):
        receipt = directory / "provenance.json"
        if not receipt.exists():
            return None
        try:
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if record.get("identity") != identity:
                raise SeparationError("Separation cache identity changed")
            for name in ("background", "vocals"):
                path = directory / f"{name}.wav"
                if record[name]["sha256"] != sha256(path):
                    raise SeparationError("Separation cached stem hash changed")
                wav_info(path, expected_duration=duration)
                record[name]["path"] = str(path)
        except (OSError, ValueError, KeyError, TypeError):
            raise SeparationError("Separation cache is invalid; preserve it for inspection") from None
        return {**record, "reused": True}

    def separate(self, source_audio, output_dir, media=None) -> dict:
        config = self.config
        status = config.status()
        if not status["ready"]:
            raise SeparationError("Local separation unavailable: " + status["reason"])
        source = Path(source_audio).resolve()
        info = wav_info(source)
        duration = info["duration_seconds"]
        if duration > config.max_source_seconds:
            raise SeparationError("Source exceeds the configured separation duration limit")
        identity = {**config.snapshot(), "numpy_version": NUMPY_VERSION,
                    "source_sha256": sha256(source), "worker_sha256": sha256(config.worker),
                    "implementation_sha256": sha256(Path(__file__))}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        directory = Path(output_dir).resolve() / fingerprint
        directory.mkdir(parents=True, exist_ok=True)
        with cache_lock(directory / ".lock", config.lock_timeout):
            saved = self._cached(directory, identity, duration)
            if saved is not None:
                return saved
            temporary = config.root / "runtime/tmp/separation"
            temporary.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="inference-", dir=temporary) as scratch:
                scratch = Path(scratch)
                environment = {**os.environ, "TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch),
                               "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": str(config.threads),
                               "OPENBLAS_NUM_THREADS": str(config.threads)}
                started = time.monotonic()
                raw = run_process([config.python, "-B", config.worker, "--worker", "--source", source,
                                   "--model", config.model, "--output", scratch, "--threads", config.threads],
                                  timeout=config.timeout, cwd=config.root, env=environment)
                try:
                    result = json.loads(raw)
                except ValueError:
                    raise SeparationError("Separation worker returned invalid metadata") from None
                if not isinstance(result, dict) or result.get("finite") is not True or result.get("engine_version") != ENGINE_VERSION:
                    raise SeparationError("Separation worker failed finite/version validation")
                if sha256(source) != identity["source_sha256"] or sha256(config.model) != MODEL_SHA256:
                    raise SeparationError("Separation source or model changed during inference")
                record = {"identity": identity, **info, "reused": False,
                          "elapsed_seconds": round(time.monotonic() - started, 3),
                          "quality_state": "needs_review", "finite": True,
                          "proof_boundary": "Local model inference; retained music and voice residue need review"}
                for name in ("background", "vocals"):
                    stage = scratch / f"{name}.wav"
                    wav_info(stage, expected_duration=duration)
                    destination = directory / stage.name
                    stage.replace(destination)
                    record[name] = {"path": str(destination), "sha256": sha256(destination)}
                staged_receipt = directory / f".receipt-{uuid.uuid4().hex}.tmp"
                try:
                    staged_receipt.write_text(json.dumps(record, indent=2), encoding="utf-8")
                    staged_receipt.replace(directory / "provenance.json")
                finally:
                    staged_receipt.unlink(missing_ok=True)
                return record
