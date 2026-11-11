"""Install the public Harness SDK into a project-owned virtual environment.

No desktop discovery, credential reads, model calls, or global package changes.
Run with any Python >=3.12: python scripts/install_harness.py
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SDK_VERSION = "0.2.1rc2"
# Versions exercised by the installed-runtime loopback probe; no desktop dependency.
REQUIREMENTS = (
    "deepseek-harness-sdk==0.2.1rc2",
    "deepseek-harness-runtime-bin==0.2.1rc2",
    "jsonschema==4.26.0",
    "pydantic==2.14.2",
    "pydantic-core==2.50.2",
    "attrs==26.1.0",
    "annotated-types==0.8.0",
    "jsonschema-specifications==2025.9.1",
    "referencing==0.37.0",
    "rpds-py==2026.9.1",
    "typing-extensions==4.16.0",
    "typing-inspection==0.4.4",
)


def install(root: Path = ROOT, *, timeout: float = 600) -> Path:
    environment_path = root / ".venv-dsh"
    temporary_root = root / "runtime/tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    python = environment_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # The context cleans this invocation's downloads/cache even when pip raises or times out.
    # Installation itself is not atomic: retain the venv so a later run can repair it.
    with TemporaryDirectory(prefix="dsh-install-", dir=temporary_root) as directory:
        return _install(environment_path, python, root, Path(directory), timeout)


def _install(environment_path: Path, python: Path, root: Path, temporary: Path, timeout: float) -> Path:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}
    env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary), PYTHONUTF8="1")
    if not python.exists():
        subprocess.run(
            [sys.executable, "-m", "venv", str(environment_path)], env=env, cwd=root, timeout=120, check=True
        )
    command = [
        str(python),
        "-m",
        "pip",
        "--isolated",
        "install",
        "--index-url",
        "https://pypi.org/simple",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--only-binary=:all:",
        "--progress-bar",
        "off",
        "--timeout",
        "20",
        "--retries",
        "1",
        *REQUIREMENTS,
    ]
    result = subprocess.run(command, env=env, cwd=root, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"Harness SDK installation failed with exit {result.returncode}")
    subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m; "
            "print('deepseek-harness-sdk=' + m.version('deepseek-harness-sdk')); "
            "print('deepseek-harness-runtime-bin=' + m.version('deepseek-harness-runtime-bin'))",
        ],
        env=env,
        cwd=root,
        timeout=20,
        check=True,
    )
    return python


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    try:
        executable = install(timeout=args.timeout)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # This script sends no private credential material to pip.
        print(f"Install did not complete: {type(exc).__name__}", file=sys.stderr)
        print("Installation is not atomic; rerun to repair a partial environment. Existing installation was retained.",
              file=sys.stderr)
        return 1
    print(f"Harness Python: {executable}")
    print("Installed a separate SDK runtime. No model request or desktop update was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
