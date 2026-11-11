"""Clone-time installer; no global changes, login, credentials, or paid model calls."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", action="append", choices=["codex", "harness"], default=[])
    parser.add_argument(
        "--check", action="store_true", help="Inspect prerequisites without installing anything"
    )
    args = parser.parse_args()
    checks = {
        "python_3_12": sys.version_info >= (3, 12),
        "node": bool(shutil.which("node")),
        "pnpm": bool(shutil.which("pnpm")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
    }
    print(json.dumps({"prerequisites": checks, "selected_agents": args.agent, "scope": "project-local"}))
    if args.check:
        return 0 if all(checks.values()) else 1
    if not all(checks.values()):
        parser.error(
            "Install Python 3.12+, Node 22+, pnpm 11 and FFmpeg/ffprobe first; see docs/operations/clone-install.md"
        )
    subprocess.run([sys.executable, str(ROOT / "scripts/setup.py")], cwd=ROOT, check=True)
    # Official pnpm launchers may be batch files on Windows; execute their JS via Node.
    pnpm = Path(shutil.which("pnpm"))
    node = shutil.which("node")
    candidates = [
        pnpm.parent / "node_modules/pnpm/bin/pnpm.cjs",
        pnpm.parent / "node_modules/pnpm/bin/pnpm.mjs",
        pnpm.parent / "pnpm.cjs",
        pnpm.parent / "pnpm.mjs",
    ]
    if os.name == "nt":
        js = next((path for path in candidates if path.is_file()), None)
        if not js:
            parser.error(
                "Unable to resolve pnpm JS entry; run pnpm install --frozen-lockfile and pnpm build in ui manually"
            )
        command = [node, str(js)]
    else:
        command = [str(pnpm)]
    for arguments in (["install", "--frozen-lockfile"], ["run", "build"]):
        subprocess.run([*command, *arguments], cwd=ROOT / "ui", check=True)
    for agent in dict.fromkeys(args.agent):
        subprocess.run([sys.executable, str(ROOT / f"scripts/install_{agent}.py")], cwd=ROOT, check=True)
    print(
        "Installed. Copy local.example.json to local.json, set private credentials, then start the workbench. No provider request was sent."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
