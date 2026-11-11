"""Install backend dependencies with a self-cleaning project-local temporary directory."""
from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory

root = Path(__file__).resolve().parents[1]
temporary = root / "runtime/tmp/pip"
temporary.mkdir(parents=True, exist_ok=True)
venv = root / ".venv"
python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
with TemporaryDirectory(prefix="install-", dir=temporary) as session:
    install_env = {**os.environ, "TMP": session, "TEMP": session, "TMPDIR": session}
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], env=install_env, check=True)
    subprocess.run([str(python), "-m", "pip", "--isolated", "--disable-pip-version-check",
                    "install", "--no-cache-dir", "--timeout", "30", "--retries", "1", "-e", ".[dev]"],
                   cwd=root, env=install_env, check=True)
print("Backend ready. Build ui and run the project CLI. Installer temporary files were cleaned.")
