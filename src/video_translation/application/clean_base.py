"""Verify both initial registered sources and task-local accepted clean bases."""
from pathlib import Path

from .tasks import Conflict


def verified_clean_base(service, task, state, expected_sha256, *, store=None):
    path = Path(state["base"]["path"])
    runtime = service.settings.runtime.resolve()
    root = runtime / "tasks" / task["id"]
    resolved = path.resolve()
    if (not path.is_file() or path.is_symlink() or not resolved.is_relative_to(runtime)
            or state["base"].get("sha256") != expected_sha256):
        raise Conflict("Retained base is missing, changed or outside the task")
    for parent in path.parents:
        if parent == runtime:
            break
        if parent.is_symlink():
            raise Conflict("Retained base contains a linked parent")
    if not resolved.is_relative_to(root):
        node = next((n for n in task["nodes"] if n["id"] == state["node_id"]
                     and n["definition_id"] == "clean"), None)
        asset_id = node.get("asset_id") if node else None
        asset = (store or service.store).get("asset", asset_id) if asset_id in task.get("asset_ids", []) else None
        if (not asset or Path(asset["path"]).resolve() != resolved
                or asset.get("sha256") != expected_sha256
                or not resolved.is_relative_to(runtime / "assets")
                or state.get("accepted")):
            raise Conflict("Retained base is not this clean node's registered original source")
    if service.hash_file(resolved) != expected_sha256:
        raise Conflict("Retained base SHA changed; reconcile original evidence")
    return resolved
