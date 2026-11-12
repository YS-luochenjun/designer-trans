"""Evidence-bound handoffs over the existing program workflow, not extra model calls."""
from __future__ import annotations

from pathlib import Path
import stat

from video_translation.agent_runtime.packet_context import node_work_order
from video_translation.domain.execution import identity
from video_translation.domain.work_handoffs import HandoffRequest  # noqa: F401
from video_translation.persistence.store import now
from .tasks import Conflict


def _nodes(task, source_id, target_id):
    nodes = {node["id"]: node for node in task["nodes"]}
    if source_id not in nodes or target_id not in nodes or source_id == target_id:
        raise Conflict("Handoff requires two real nodes in this task")
    source, target = nodes[source_id], nodes[target_id]
    seen, visiting = set(), set()

    def visit(key):
        if key in visiting:
            raise Conflict("Workflow contains a dependency cycle")
        if key not in nodes:
            raise Conflict("Workflow contains a missing dependency")
        if key in seen:
            return
        visiting.add(key)
        for dependency in nodes[key]["depends_on"]:
            visit(dependency)
        visiting.remove(key)
        seen.add(key)
    visit(target_id)
    seen.remove(target_id)
    if source_id not in seen or source["status"] not in {"succeeded", "skipped"}:
        raise Conflict("Handoff source must be a completed upstream dependency")
    if source["asset_id"] != target["asset_id"] or source.get("locale") not in {None, target.get("locale")}:
        raise Conflict("Handoff cannot cross unrelated assets or locales")
    return source, target


def handoff_basis(task, source_id, target_id):
    source, target = _nodes(task, source_id, target_id)
    return identity({
        "task_id": task["id"], "source": node_work_order(task, source),
        "source_output": source.get("output", {}), "target": node_work_order(task, target),
        "source_attempt": source.get("attempt", 0), "target_attempt": target.get("attempt", 0),
        "brief_revision": task.get("brief_revision", 0),
        "execution_snapshot": task.get("execution_snapshot"),
        "resource_snapshot": task.get("resource_snapshot"),
    })


def _evidence_valid(service, reference):
    path, root = Path(reference["path"]), service.settings.runtime.resolve()
    if not path.is_absolute() or not path.is_relative_to(root) or not path.is_file():
        return False
    try:
        for item in (path, *path.parents):
            if item == root.parent:
                break
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        return path.resolve().is_relative_to(root) and service.hash_file(path) == reference["sha256"]
    except OSError:
        return False


def publish_handoff(service, task_id, request):
    with service.store.lock:
        task = service.store.require("task", task_id)
        basis = handoff_basis(task, request.source_node_id, request.target_node_id)
        if request.expected_basis_sha256 != basis:
            raise Conflict("Handoff inputs changed; refresh the upstream and target basis")
        for reference in request.evidence:
            path = Path(reference.path)
            if not _evidence_valid(service, reference.model_dump()):
                raise Conflict("Handoff evidence changed or leaves the runtime")
            # Evidence must already be registered for the same task, never an arbitrary private file.
            if not any(row.get("task_id") == task_id and row.get("sha256") == reference.sha256
                       and Path(row["path"]).resolve() == path.resolve()
                       for row in service.store.list("artifact")):
                raise Conflict("Handoff evidence must be registered to this task")
        value = {"task_id": task_id, **request.model_dump(), "basis_sha256": basis,
                 "authority": "upstream_evidence_only", "status": "published"}
        key = identity(value)
        previous = service.store.get("work_handoff", key)
        if previous:
            return previous
        return service.store.put("work_handoff", {**value, "id": key, "created_at": now()})


def handoffs_for(service, task_id, target_node_id):
    task = service.store.require("task", task_id)
    if not any(node["id"] == target_node_id for node in task["nodes"]):
        raise Conflict("Handoff target must be a real node in this task")
    rows = []
    for row in service.store.list("work_handoff"):
        if row["task_id"] != task_id or row["target_node_id"] != target_node_id:
            continue
        try:
            valid = row["basis_sha256"] == handoff_basis(task, row["source_node_id"], target_node_id)
            if valid:
                for reference in row["evidence"]:
                    if not _evidence_valid(service, reference):
                        valid = False
                        break
        except (Conflict, OSError, KeyError):
            valid = False
        rows.append({**row, "current_status": "available" if valid else "stale",
                     "usable_as_instruction": False, "grants_new_permissions": False})
    return {
        "items": [{key: value for key, value in row.items()
                   if row["current_status"] == "available" or key not in {"note", "evidence"}}
                  for row in rows[:20]],
        "complete": len(rows) <= 20,
        "boundary": "Notes are scoped business data; verified node outputs and task controls remain authoritative.",
    }
