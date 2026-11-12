from __future__ import annotations

import argparse
import json

from video_translation.config import Settings
from video_translation.domain.models import CONTRACTS


def main():
    parser = argparse.ArgumentParser(description="视频翻译 v2")
    parser.add_argument("command", choices=["serve", "status", "doctor", "export-schemas", "preview", "local-repair", "track"], nargs="?", default="serve")
    parser.add_argument("--task")
    parser.add_argument("--asset")
    parser.add_argument("--locale")
    parser.add_argument("--recipe")
    parser.add_argument("--video", help="Local input for read-only motion diagnostics")
    parser.add_argument("--output", help="New JSON diagnostics path; never overwrites an existing file")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--check-identity", action="store_true", help="Doctor: run the selected CLI identity check; no model request")
    args = parser.parse_args()
    settings = Settings.load()
    import os
    import tempfile
    temporary = settings.runtime / "tmp/service"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ.update(TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary))
    tempfile.tempdir = str(temporary)
    if args.command in {"serve", "status"}:
        from video_translation.interfaces.runtime_status import inspect_runtime, startup_message
        state = inspect_runtime(settings, args.port)
        message = startup_message(state)
        if args.command == "status":
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return
        if message:
            print(message)
            return
    if args.command == "track":
        if not all((args.video, args.recipe, args.output)):
            parser.error("track requires --video, --recipe and --output")
        from pathlib import Path
        from video_translation.execution.motion_tracking import track_region
        from video_translation.execution.media import MediaTools
        from video_translation.execution.visual_media import VisualMedia
        target = Path(args.output).resolve()
        if target.exists():
            parser.error("Tracking output already exists; reuse the existing report")
        recipe = json.loads(Path(args.recipe).read_text("utf-8"))
        if set(recipe) != {"start_ms", "end_ms", "region"}:
            parser.error("Tracking recipe requires only start_ms, end_ms and region")
        visual = VisualMedia(MediaTools(settings.ffmpeg, settings.ffprobe, timeout=180))
        result = track_region(visual, args.video, **recipe)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"status": result["status"], "verified": False, "report": str(target),
                          "frames": len(result["frames"]), "failure": result["failure"]}, ensure_ascii=False))
    elif args.command in {"preview", "local-repair"}:
        if args.command == "preview" and not all((args.task, args.asset, args.locale)):
            parser.error("preview requires --task, --asset and --locale")
        from video_translation.application.local_preview import create_preview
        from video_translation.application.tasks import TaskService
        from video_translation.persistence.store import Store
        service = TaskService(settings, Store(settings.runtime / "database/project.sqlite3"))
        if args.command == "local-repair":
            if not args.task or not args.recipe:
                parser.error("local-repair requires --task and --recipe")
            from pathlib import Path
            from video_translation.application.local_candidate import create_local_candidate
            from video_translation.persistence.store import WorkerLock
            local_lock = WorkerLock(settings.runtime / "worker.lock")
            local_lock.acquire()
            try:
                report = create_local_candidate(service, args.task, json.loads(Path(args.recipe).read_text("utf-8")))
            finally:
                local_lock.release()
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            report = create_preview(service, args.task, args.asset, args.locale)
            print(json.dumps({key: report[key] for key in (
                "id", "path", "sha256", "artifact_id", "quality_passed", "rendered_layers", "omitted_layers")},
                ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        from video_translation.diagnostics import diagnose
        report = diagnose(settings, check_identity=args.check_identity)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ready_for_trial"]:
            raise SystemExit(1)
    elif args.command == "export-schemas":
        from video_translation.domain.production import PRODUCTION_CONTRACTS
        from video_translation.execution.adaptive_observe import ObservationDecision
        from video_translation.execution.task_route import ObservationRouteDecision

        contracts = {**CONTRACTS, **PRODUCTION_CONTRACTS, "agent/observation-decision": ObservationDecision,
                     "agent/observation-route": ObservationRouteDecision}
        for name, model in contracts.items():
            path = settings.root / "contracts/schemas" / f"{name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            schema = model.model_json_schema()
            schema["$id"] = f"urn:video-translation:{name}:1.0.0"
            schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
            print(name)
    else:
        import uvicorn

        uvicorn.run(
            "video_translation.app:create_app", factory=True, host="127.0.0.1", port=args.port, workers=1
        )


if __name__ == "__main__":
    main()
