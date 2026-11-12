"""Inspect a stopped production Route without adopting its reply or dispatching."""
import hashlib
import json
import re

from .agent_recovery import replaced_with_authorization
from ..integrations.agents.failure_evidence import safe_failure_evidence


def _read(path, root, limit):
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink() or (
                part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Linked incident evidence")
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Missing or oversized incident evidence")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("Incident evidence grew")
    return value


def diagnostic(service, task):
    node = next((n for n in task["nodes"] if n["definition_id"] == "clean"
                 and n["status"] == "unknown"), None)
    if node is None or task.get("control_paused"):
        return None
    decisions = [
        d for d in service.store.list("production_decision")
        if d.get("task_id") == task["id"] and d.get("node_id") == node["id"]
        and d.get("status") == "unknown"
        and not replaced_with_authorization(service.store, d, "production_decision")
    ]
    if len(decisions) != 1:
        return None
    decision = decisions[0]
    if (not re.fullmatch(r"clean-route-[0-9a-f]{64}(?:-recovery-[0-9a-f]{20})?",
                        decision.get("key", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", decision["id"])):
        return None
    root = service.settings.runtime / "tasks" / task["id"]
    recovery = (root / "nodes" / node["id"] / str(node["attempt"])
                / (decision["id"][:20] + "-codex-recovery"))
    value = {
        "original_kind": "clean_route", "original_decision_id": decision["id"],
        "reason": "Production Route result needs verified original evidence; no Work replay is allowed.",
    }
    try:
        request = _read(recovery / "request.json", root, 256 * 1024)
        receipt_bytes = _read(recovery / "receipt.json", root, 16 * 1024)
        receipt = json.loads(receipt_bytes)
        if not isinstance(receipt, dict):
            raise ValueError("Invalid receipt")
        value.update(
            original_request_sha256=hashlib.sha256(request).hexdigest(),
            original_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            retained_answer=False, **safe_failure_evidence(receipt))
        answer_path = recovery / "answer.json"
        if answer_path.exists() or answer_path.is_symlink():
            answer = _read(answer_path, root, 2 * 1024 * 1024)
            value.update(retained_answer=True,
                         retained_answer_sha256=hashlib.sha256(answer).hexdigest())
        if receipt.get("event_stream_invalid") is True:
            value.setdefault("event_invalid_reason", "unrecorded")
            value["reason"] = (
                "Production Route event stream is invalid. Retained output is not a verified "
                "decision; preserve the original call and inspect its bridge evidence.")
    except (OSError, ValueError):
        value["reason"] = "Production Route original evidence is missing, unsafe or unreadable."
    return value
