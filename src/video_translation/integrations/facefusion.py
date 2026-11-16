"""Run the installed FaceFusion CLI without importing it into the workbench."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess

from video_translation.domain.facefusion import FaceFusionProfile


def installation():
    root = Path(os.environ.get(
        "VT_FACEFUSION_ROOT", str(Path.home() / "Desktop" / "AI-faces" / "facefusion"))).resolve()
    python = Path(os.environ.get("VT_FACEFUSION_PYTHON", str(root / ".venv/Scripts/python.exe")))
    required = [root / "facefusion.py", python, root / "facefusion/program.py",
                root / "facefusion/face_selector.py"]
    ready = all(path.is_file() for path in required)
    digest = hashlib.sha256()
    if ready:
        for path in [root / "facefusion.py", *sorted((root / "facefusion").rglob("*.py"))]:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return {"ready": ready, "root": root, "python": python,
            "source_sha256": digest.hexdigest() if ready else None,
            "reason": "" if ready else "FaceFusion 项目或其 Python 环境不可用"}


def public_status():
    state = installation()
    return {key: state[key] for key in ("ready", "source_sha256", "reason")} | {
        "executor": "facefusion", "modes": ["reference", "many", "one"],
        "route_work": False,
    }


def arguments(profile, video, face, output, directory):
    profile = FaceFusionProfile.model_validate(profile)
    return [
        "headless-run", "--config-path", str(directory / "facefusion.ini"),
        "--jobs-path", str(directory / "jobs"), "--temp-path", str(directory / "temp"),
        "--source-paths", str(face), "--target-path", str(video), "--output-path", str(output),
        "--processors", "face_swapper", "--face-selector-mode", profile.face_selector_mode,
        "--face-selector-order", profile.face_selector_order,
        "--reference-frame-number", str(profile.reference_frame_number),
        "--reference-face-position", str(profile.reference_face_position),
        "--reference-face-distance", f"{profile.reference_face_distance:.2f}",
        "--face-mask-types", *profile.face_mask_types, "--face-mask-blur", f"{profile.face_mask_blur:.2f}",
        "--output-video-quality", str(profile.output_video_quality),
        "--output-video-encoder", "libx264", "--output-audio-encoder", "aac",
        "--output-audio-volume", "100", "--output-video-scale", "1.0",
        "--workflow-strategy", "memory", "--execution-thread-count", "4",
    ]


def start(state, args, log):
    temporary = Path(args[args.index("--temp-path") + 1])
    temporary.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [str(state["python"]), str(state["root"] / "facefusion.py"), *args],
        cwd=state["root"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
        # FaceFusion writes FFmpeg concat lists using Python's default encoding.
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
             "PYTHONDONTWRITEBYTECODE": "1",
             "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary)},
    )


def terminate(process):
    """Stop this owned run and its encoders before releasing the task writer."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def inspect_target(state, video, directory, profile):
    recipe = directory / "inspect.json"
    recipe.write_text(json.dumps({"video": str(video), "output": str(directory),
                                 "profile": profile}, ensure_ascii=False), encoding="utf-8")
    script = Path(__file__).with_name("facefusion_inspect.py")
    with (directory / "inspect.log").open("wb") as log:
        subprocess.run(
            [str(state["python"]), str(script), str(state["root"]), str(recipe)],
            cwd=directory, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                 "PYTHONDONTWRITEBYTECODE": "1"},
        )
    return json.loads((directory / "faces.json").read_text("utf-8"))
