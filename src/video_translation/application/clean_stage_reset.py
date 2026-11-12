"""Explicit, version-bound replacement of cleanup and its dependent production."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

from .agent_recovery import replaced_with_authorization
from .collaboration import affected_nodes, node_for
from .tasks import Conflict
from ..domain.execution import identity
from ..persistence.store import now
from ..persistence.transaction import TransactionStore
from ..workflow.graph import task_status

KIND = "clean_stage_reset"
# These remain available for accounting and original-request reconciliation.
LEDGER_KINDS = {"operation", "production_decision", "agent_recovery_authorization",
                "route_run", "route_probe", "task_route_event", "task_route_decision"}
UNSETTLED = {"running", "pending", "queued", "claimed", "verifying", "planned",
             "waiting_work", "unknown", "invalid_response", "reserved"}


def _paths(value):
    if isinstance(value, str):
        if Path(value).is_absolute():
            yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _paths(key)
            yield from _paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child)


def _checked(path, root):
    path = Path(path)
    if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise Conflict("Reset media path leaves its selected node")
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise Conflict("Linked reset paths are not supported")
        if part == root:
            break
    return path


def _basis(service, store, task_id, node_id):
    task = store.require("task", task_id)
    node = node_for(task, node_id)
    if (not task.get("control_paused") or task["status"] in {"completed", "cancelled"}
            or store.get("deletion_receipt", task_id)
            or not node or node["definition_id"] != "clean"):
        raise Conflict("Pause the unfinished task before explicitly resetting cleanup")
    ids = affected_nodes(task, node_id, "downstream")
    selected = [n for n in task["nodes"] if n["id"] in ids]
    if any(n["status"] in {"running", "waiting_external", "unknown"}
           or n.get("external_pending") for n in task["nodes"]):
        raise Conflict("Original Work must settle before a reset")
    if any(n["definition_id"] == "deliver" and n["status"] == "succeeded" for n in selected):
        raise Conflict("A delivered version requires a separate rework branch")
    with store.connection() as db:
        records = [(k, i, json.loads(d)) for k, i, d in db.execute(
            "SELECT kind,id,data FROM records ORDER BY kind,id")]
    for kind, _, row in records:
        if row.get("task_id") != task_id:
            continue
        if kind in LEDGER_KINDS | {"conversation", "imagegen_request", "production_image"}:
            if row.get("status") in UNSETTLED and not replaced_with_authorization(store, row, kind):
                raise Conflict("Unsettled original requests or new user messages take priority")
    source = service.checked_path(store.require("asset", node["asset_id"]))
    if service.hash_file(source) != store.require("asset", node["asset_id"])["sha256"]:
        raise Conflict("The original source changed")
    roots = [service.settings.runtime / "tasks" / task_id / "nodes" / n["id"] for n in selected]
    files = {}
    for root in roots:
        _checked(root, service.settings.runtime / "tasks" / task_id / "nodes")
        if root.exists():
            for path in sorted(root.rglob("*")):
                _checked(path, root)
                if path.is_file():
                    files[str(path)] = {"sha256": service.hash_file(path), "bytes": path.stat().st_size}
    owned = [(k, i, r) for k, i, r in records if r.get("node_id") in ids
             or k == "clean_method_experience" and r.get("task_id") == task_id]
    cache = service.settings.runtime / "tasks" / task_id / "image-cache"
    for kind, _, row in owned:
        if kind != "production_image":
            continue
        for path in cache.glob(row["id"] + "*") if cache.exists() else []:
            _checked(path, cache)
            if path.is_file() and path.suffix != ".lock":
                files[str(path)] = {"sha256": service.hash_file(path), "bytes": path.stat().st_size}
    protected = set()
    for kind, _, row in records:
        if kind == "agent_recovery_authorization" and row.get("task_id") == task_id:
            references = {Path(p).resolve() for p in _paths(row)}
            protected.update(p for p in files if Path(p).resolve() in references)
            recovery = row.get("original_recovery_path")
            if recovery:
                protected.update(p for p in files if Path(p).is_relative_to(Path(recovery)))
    basis = {"task": task, "records": owned, "files": files, "source_sha256": service.hash_file(source)}
    return task, selected, owned, files, protected, identity(basis)


def preview(service, task_id, node_id):
    _, nodes, _, files, protected, digest = _basis(service, service.store, task_id, node_id)
    return {"basis_sha256": digest, "node_id": node_id,
            "stages": [n["definition_id"] for n in nodes],
            "file_count": len(files), "bytes": sum(f["bytes"] for f in files.values()),
            "pinned_request_file_count": len(protected), "source": "original_asset",
            "accepted_after_reset": 0, "costs_reset": False, "resumes_task": False}


def apply(service, task_id, request):
    key = identity({"task_id": task_id, "request_id": request.request_id})
    previous = service.store.get(KIND, key)
    if previous:
        if previous["request"] != vars(request) and previous["request"] != (
                request.model_dump() if hasattr(request, "model_dump") else vars(request)):
            raise Conflict("Reset request identity was already used for different instructions")
        return previous if previous.get("files_retired") else _retire_files(service, previous)
    task, selected, owned, files, protected, digest = _basis(
        service, service.store, task_id, request.node_id)
    if digest != request.expected_basis or not request.reason.strip():
        raise Conflict("The selected production or its media changed; preview the reset again")
    archive = service.settings.runtime / "retired-production" / task_id / key
    _checked(archive, service.settings.runtime)
    budget = task.get("execution_snapshot", {}).get("resource_budget") or {}
    if shutil.disk_usage(service.settings.runtime).free < (
            sum(ref["bytes"] for ref in files.values()) + budget.get("min_free_bytes", 1024**3)):
        raise Conflict("Insufficient free space for a verified reset backup; no records removed")
    archive.mkdir(parents=True, exist_ok=True)
    task_root = service.settings.runtime / "tasks" / task_id
    for name, ref in files.items():
        path = Path(name)
        target = archive / "files" / path.relative_to(task_root)
        _checked(target, archive)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(path, target)
        if service.hash_file(target) != ref["sha256"] or service.hash_file(path) != ref["sha256"]:
            raise Conflict("Reset backup or original changed; no production was reset")
    snapshot = {"task": task, "records": owned, "files": files, "protected": sorted(protected),
                "basis_sha256": digest}
    backup = archive / "records.json"
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if backup.exists() and backup.read_bytes() != encoded:
        raise Conflict("An existing reset backup has a different identity")
    backup.write_bytes(encoded)
    old_ids = {n["id"] for n in selected}
    mapping = {old: identity({"reset": key, "node": old})[:32] for old in old_ids}
    at = now()
    receipt = {"id": key, "task_id": task_id, "request": (
        request.model_dump() if hasattr(request, "model_dump") else vars(request)),
        "created_at": at, "status": "applied", "replaced_node_ids": sorted(old_ids),
        "new_node_ids": mapping, "archive_path": str(archive),
        "backup_sha256": service.hash_file(backup), "file_count": len(files),
        "pinned_request_file_count": len(protected), "quality_acceptance": False,
        "cumulative_clean_turns_before_reset": sum(
            row.get("turn", 0) for kind, _, row in owned if kind == "production_session"),
        "paid_calls_preserved": task.get("paid_calls", 0), "source_reused": "original_asset",
        "counters_reset": ["current_stage_steps", "current_generation_revisions"],
        "cumulative_revisions_before_reset": deepcopy(task.get("revisions", {})),
        "production_resumed": False, "files_retired": False}
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        current, _, _, _, _, current_digest = _basis(service, store, task_id, request.node_id)
        if current_digest != digest:
            raise Conflict("Production changed during backup; no records removed")
        for kind, rid, row in owned:
            if kind in LEDGER_KINDS or kind == "route_policy":
                continue
            store.put("stage_reset." + kind, row)
            db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, rid))
        for node in current["nodes"]:
            if node["id"] in old_ids:
                fresh = {k: deepcopy(node[k]) for k in ("definition_id", "label", "stage", "locale", "asset_id")}
                fresh.update(id=mapping[node["id"]], depends_on=node["depends_on"],
                             status="pending", attempt=0, output={}, artifact_ids=[],
                             started_at=None, finished_at=None, error=None, skip_reason=None,
                             stage_reset_id=key)
                node.clear()
                node.update(fresh)
            node["depends_on"] = [mapping.get(dep, dep) for dep in node["depends_on"]]
        current["collaboration_rules"] = [
            r for r in current.get("collaboration_rules", []) if r.get("node_id") not in old_ids]
        current["revisions"] = {branch: 0 if branch.startswith(selected[0]["asset_id"] + ":") else count
                                for branch, count in current.get("revisions", {}).items()}
        current.setdefault("stage_reset_history", []).append(key)
        current.update(status=task_status(current), error=None, updated_at=at)
        policy = store.get("route_policy", task_id)
        if policy and policy.get("node_id") in old_ids:
            policy.update(node_id=mapping[policy["node_id"]])
            # The new original-source generation has its own explicit user authority.
            policy["expected_base_sha256"] = store.require("asset", selected[0]["asset_id"])["sha256"]
            policy["stage_reset_id"] = key
            store.put("route_policy", policy)
        store.put("task", current)
        route_status = store.get("route_status", task_id)
        if route_status:
            store.put("stage_reset.route_status", route_status)
            db.execute("DELETE FROM records WHERE kind='route_status' AND id=?", (task_id,))
        store.put(KIND, receipt)
    return _retire_files(service, receipt)


def _retire_files(service, receipt):
    """A failed filesystem cleanup can resume without resetting the task twice."""
    task_id = receipt["task_id"]
    if not service.store.require("task", task_id).get("control_paused"):
        raise Conflict("Keep reset cleanup paused until its backed-up files are retired")
    archive = _checked(Path(receipt["archive_path"]), service.settings.runtime / "retired-production")
    backup = _checked(archive / "records.json", archive)
    if service.hash_file(backup) != receipt["backup_sha256"]:
        raise Conflict("Reset backup changed; no further files removed")
    snapshot = json.loads(backup.read_bytes())
    files, protected = snapshot["files"], set(snapshot["protected"])
    task_root = service.settings.runtime / "tasks" / task_id
    for name, ref in files.items():
        if name not in protected:
            path = _checked(Path(name), task_root)
            target = _checked(archive / "files" / path.relative_to(task_root), archive)
            if not target.is_file() or service.hash_file(target) != ref["sha256"]:
                raise Conflict("Reset media backup changed; no further files removed")
            if path.exists() and service.hash_file(path) != ref["sha256"]:
                raise Conflict("Reset applied but an old file changed; cleanup remains paused")
    removed = 0
    for name in files:
        if name not in protected:
            path = _checked(Path(name), task_root)
            if path.exists():
                path.unlink()
                removed += 1
    receipt.update(removed_file_count=removed, files_retired=True)
    service.store.put(KIND, receipt)
    return receipt
