"""Local readiness checks without creating a database, starting workers or calling models."""
from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from video_translation.agent_runtime.resources import ResourceCatalog
from video_translation.integrations.agents.status import configuration_status, ensure_engine_ready
from video_translation.integrations.providers.model_catalog import resolve_selection


def diagnose(settings, *, check_identity=False):
    checks = []

    def record(name, passed, detail, *, required=True, action=""):
        checks.append({"id": name, "status": "ready" if passed else "missing", "required": required,
                       "detail": detail, "action": "" if passed else action})

    record("python", sys.version_info >= (3, 12), ".".join(map(str, sys.version_info[:3])),
           action="Install Python 3.12 or later and recreate the project virtual environment.")
    for name, executable in (("ffmpeg", settings.ffmpeg), ("ffprobe", settings.ffprobe)):
        ready = bool(shutil.which(executable))
        if ready:
            try:
                result = subprocess.run([executable, "-version"], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=10, check=False)
                ready = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ready = False
        record(name, ready, "executable smoke check", action=f"Install {name} and add it to PATH, or set VT_{name.upper()}.")
    record("ui", (settings.root / "ui/dist/index.html").is_file(), "built workbench",
           action="In ui, run pnpm install --frozen-lockfile then pnpm run build.")
    try:
        settings.runtime.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="doctor-", dir=settings.runtime) as directory:
            test = Path(directory) / "write-check"
            test.write_bytes(b"ok")
            writable = test.read_bytes() == b"ok"
        free = shutil.disk_usage(settings.runtime).free
    except OSError:
        writable, free = False, 0
    record("runtime", writable, "runtime write/read/cleanup", action="Choose a writable VT_RUNTIME directory.")
    record("disk", free >= 1024**3, f"{free // (1024**2)} MiB free; allow additional space for videos/models",
           required=False, action="Free disk space before installing models or processing videos.")
    try:
        resolve_selection(settings.root)
        for kind in ("prompts", "knowledge", "skills"):
            if not ResourceCatalog(settings.root).list(kind):
                raise ValueError()
        resources_valid = True
    except (OSError, ValueError, KeyError, TypeError):
        resources_valid = False
    record("resources", resources_valid, "model catalog and instruction resources",
           action="Restore config/models, prompts, skills, knowledge from the source package.")
    try:
        state = configuration_status(settings.root)
        ready = state["configured"]
        if ready and check_identity:
            ensure_engine_ready(settings.root, state["engine"])
        engine = state["engine"]
    except Exception:
        ready, engine = False, "invalid"
    record("agent", ready, f"{engine}; configuration/CLI identity only, no model inference",
           action="Install the selected Agent runtime and configure its identity; see device-setup.md.")
    media_provider = os.environ.get("VT_MEDIA_PROVIDER", "compatible")
    if media_provider == "kie":
        from video_translation.integrations.providers.kie import KieConfig
        try:
            kie = KieConfig.from_env()
            ready = bool(kie.api_key and kie.enabled_model_calls)
        except (ValueError, TypeError):
            ready = False
        record("media_provider", ready, "KIE key present and calls enabled; network authentication not checked",
               action="Set VT_KIE_API_KEY using a private file and VT_KIE_ENABLED=true, then run scripts/check_kie.py.")
    else:
        from video_translation.integrations.providers import ProviderConfig
        ready = all(c.base_url and c.model for c in (ProviderConfig.from_env("asr"), ProviderConfig.from_env("tts")))
        record("media_provider", bool(ready), "compatible ASR/TTS configuration",
               action="Configure VT_ASR_* and VT_TTS_* or choose KIE.")
    from video_translation.integrations.providers.separation import SeparationConfig
    try:
        separation = SeparationConfig.from_env(settings.root).status()
    except Exception:
        separation = {"ready": False, "reason": "invalid_configuration"}
    record("separation", separation["ready"], separation["reason"], required=False,
           action="Run scripts/install_separation.py --download-model; required for preserve-background tasks.")
    from video_translation.integrations.fireplay.mcp import FireplayMCPConfig, FireplayMCP
    try:
        connection = FireplayMCP(FireplayMCPConfig.from_env(settings.root)).status()
    except Exception:
        connection = {"configured": False, "code": "invalid_configuration"}
    record("fireplay", connection["configured"], connection.get("code", "unconfigured"), required=False,
           action="Configure the AI Designer MCP endpoint and private identity; see device-setup.md.")
    versions = {}
    for package in ("video-translation-v2", "fastapi", "pydantic", "httpx"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {"ready_for_trial": all(c["status"] == "ready" for c in checks if c["required"]),
            "checks": checks, "versions": versions, "model_called": False,
            "quality_verified": False, "scope": "local-readiness"}
