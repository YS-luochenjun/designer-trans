"""Install isolated CPU separation dependencies and copy explicitly supplied weights.

Model downloads require explicit --download-model; no previous-project discovery.
Public downloads are bounded and checksum verified; model_source remains read-only.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from video_translation.integrations.providers.separation import (  # noqa: E402
    ENGINE_VERSION, MODEL_SHA256, NUMPY_VERSION, SeparationConfig, SeparationError,
    cache_lock, run_process, sha256,
)


def install(model_source):
    config = SeparationConfig.from_env(ROOT)
    source = Path(model_source).resolve()
    if not source.is_file() or sha256(source) != MODEL_SHA256:
        raise SeparationError("Explicit model source is missing or has the wrong checksum")
    config.model.parent.mkdir(parents=True, exist_ok=True)
    temporary = ROOT / "runtime/tmp/separation-install"
    temporary.mkdir(parents=True, exist_ok=True)
    with cache_lock(temporary / ".lock", 2), TemporaryDirectory(prefix="install-", dir=temporary) as scratch:
        environment = {**os.environ, "TMP": scratch, "TEMP": scratch, "TMPDIR": scratch,
                       "PYTHONDONTWRITEBYTECODE": "1"}
        if not config.python.is_file():
            run_process([sys.executable, "-m", "venv", ROOT / ".venv-audio"],
                        timeout=120, cwd=ROOT, env=environment)
        run_process([config.python, "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
                     "--no-cache-dir", "--only-binary=:all:", "--index-url", "https://pypi.org/simple",
                     "--timeout", "30", "--retries", "1", f"numpy=={NUMPY_VERSION}",
                     f"sherpa-onnx=={ENGINE_VERSION}", f"sherpa-onnx-core=={ENGINE_VERSION}"],
                    timeout=300, cwd=ROOT, env=environment)
        if config.model.exists():
            if sha256(config.model) != MODEL_SHA256:
                raise SeparationError("Installed model checksum mismatch; preserve it for inspection")
        else:
            stage = Path(scratch) / config.model.name
            shutil.copyfile(source, stage)
            if sha256(stage) != MODEL_SHA256:
                raise SeparationError("Copied model checksum mismatch")
            stage.replace(config.model)
    status = config.status()
    if not status["ready"]:
        raise SeparationError("Installed separation runtime failed readiness: " + status["reason"])
    return {**status, "model_bytes": config.model.stat().st_size,
            "dependency_environment": str(config.python.parent.parent), "model_path": str(config.model)}


MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/source-separation-models/UVR-MDX-NET-Inst_HQ_3.onnx"


def install_download():
    """Download only the reviewed model, check its identity, then install locally."""
    import httpx
    import time
    config = SeparationConfig.from_env(ROOT)
    if config.model.is_file() and sha256(config.model) == MODEL_SHA256:
        return install(config.model)
    temporary = ROOT / "runtime/tmp/separation-download"
    temporary.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="model-", dir=temporary) as directory:
        target = Path(directory) / "model.onnx"
        deadline, size = time.monotonic() + 300, 0
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=30) as response:
            response.raise_for_status()
            if response.url.scheme != "https":
                raise SeparationError("Model download requires HTTPS")
            with target.open("wb") as output:
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > 80 * 1024**2 or time.monotonic() > deadline:
                        raise SeparationError("Model download exceeded size/time limit")
                    output.write(chunk)
        if sha256(target) != MODEL_SHA256:
            raise SeparationError("Downloaded model checksum mismatch")
        return install(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--model-source", type=Path)
    group.add_argument("--download-model", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install_download() if args.download_model else install(args.model_source), ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Separation installation failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
