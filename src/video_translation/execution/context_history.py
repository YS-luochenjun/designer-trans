"""One replaceable read-only history index, not one full snapshot per turn."""
import json
import os

from .workspace import file_sha


def retain_context_history(controller, state):
    path = controller.directory / "clean-context-history.json"
    if path.is_symlink():
        raise ValueError("History index cannot replace a linked file")
    payload = {
        "revision": 1, "task_id": controller.task["id"], "session_id": state["id"],
        "base_sha256": state["base"]["sha256"],
        "accepted": state.get("accepted", []),
        "candidate_archive": state.get("candidate_archive", []),
        "repair_history": [row for row in state.get("history", [])
                           if row["decision"]["action"] == "repair"],
        "limitation": "Retained records only; historical acceptance does not approve the current full video.",
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not path.exists() or path.read_bytes() != data:
        temporary = path.with_suffix(".pending.json")
        with temporary.open("xb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    return {"path": str(path), "sha256": file_sha(path), "bytes": len(data),
            "scope": "Read-only task history index; select IDs instead of loading all records."}
