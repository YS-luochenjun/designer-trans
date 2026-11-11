"""Non-secret settings that can be changed for future tasks."""

from __future__ import annotations
import json
import os
from pathlib import Path
from threading import RLock
from video_translation.integrations.providers.model_catalog import resolve_selection

LOCK = RLock()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def public_selection(root: Path):
    with LOCK:
        selection = json.loads((root / "config/models/selection.json").read_text("utf-8"))
        catalog = json.loads((root / "config/models/catalog.json").read_text("utf-8"))
        return {
            "agent_engine": os.environ.get("VT_AGENT_ENGINE", "custom"),
            "selection": selection,
            "catalog": catalog,
            "resolved": resolve_selection(root, selection),
        }


def update_selection(root: Path, body):
    if not isinstance(body, dict) or set(body) - {"agent_engine", "selection"} or not body:
        raise ValueError("Only agent_engine and model selection can be edited here")
    with LOCK:
        if "agent_engine" in body and body["agent_engine"] not in {"codex", "harness", "custom"}:
            raise ValueError("Agent must be codex, harness, or custom")
        if "selection" in body:
            resolve_selection(root, body["selection"])
        if "agent_engine" in body:
            path = root / "config/application/local.json"
            current = (
                json.loads(path.read_text("utf-8"))
                if path.exists()
                else {"version": "1.0.0", "environment": {}}
            )
            current.setdefault("environment", {})["VT_AGENT_ENGINE"] = body["agent_engine"]
            atomic_json(path, current)
            os.environ["VT_AGENT_ENGINE"] = body["agent_engine"]
        if "selection" in body:
            atomic_json(root / "config/models/selection.json", body["selection"])
        return public_selection(root)
