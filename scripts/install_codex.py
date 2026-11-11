"""Optional project-local installation of the reviewed public Codex CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.166.0"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=ROOT / "runtime/tools/codex")
    args = parser.parse_args(argv)
    prefix = args.prefix.resolve()
    if not prefix.is_relative_to(ROOT / "runtime"):
        parser.error("installation must remain inside project runtime")
    node = shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not node or not npm:
        parser.error("Node.js and npm required; no global tools will be installed")
    npm_path = Path(npm).resolve()
    npm_js = npm_path if npm_path.suffix == ".js" else Path(npm).parent / "node_modules/npm/bin/npm-cli.js"
    if not npm_js.is_file():
        parser.error("npm-cli.js not found next to npm; use a portable Node distribution")
    prefix.mkdir(parents=True, exist_ok=True)
    temporary_root = ROOT / "runtime/tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    # Preserve the actual installation on all outcomes; npm can repair it on rerun.
    try:
        with TemporaryDirectory(prefix="codex-install-", dir=temporary_root) as directory:
            temporary = Path(directory)
            env = dict(os.environ)
            env.update(TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary),
                       npm_config_cache=str(temporary / "npm-cache"))
            result = subprocess.run(
                [
                    node,
                    str(npm_js),
                    "install",
                    "--prefix",
                    str(prefix),
                    "--cache",
                    str(temporary / "npm-cache"),
                    "--no-audit",
                    "--no-fund",
                    "--ignore-scripts",
                    f"@openai/codex@{VERSION}",
                ],
                env=env,
                timeout=300,
                check=False,
            )
            code = result.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Install did not complete: {type(exc).__name__}", file=sys.stderr)
        code = 1
    if code:
        print("Installation is not atomic; rerun to repair a partial installation. Existing installation was retained.",
              file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
