"""A bounded, media-free contract diagnostic never supplies a production answer."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from jsonschema import Draft202012Validator

from .tasks import Conflict
from ..domain.production import CleanAction
from ..integrations.agents.schema import provider_schema


def contract_details(service, task, expected):
    node = next(n for n in task["nodes"] if n["definition_id"] == "clean")
    original = (service.settings.runtime / "tasks" / task["id"] / "nodes" / node["id"]
                / str(node["attempt"]) / (expected["expected_decision_id"][:20] + "-codex-recovery")
                / "request.json")
    root = (service.settings.runtime / "tasks" / task["id"]).resolve()
    if (original.is_symlink() or not original.is_file() or original.stat().st_size > 256 * 1024
            or not original.resolve().is_relative_to(root)
            or service.hash_file(original) != expected["expected_request_sha256"]):
        raise Conflict("Original contract file is missing or changed")
    schema = json.loads(original.read_text("utf-8"))["schema"]
    schema_sha256 = hashlib.sha256(json.dumps(provider_schema(schema), sort_keys=True).encode()).hexdigest()
    sample = CleanAction(action="blocked", reason="Technical contract probe only; no production action.").model_dump()
    Draft202012Validator(schema).validate(sample)
    signature = hashlib.sha256(json.dumps({
        "task_id": task["id"], "base_sha256": expected["expected_base_sha256"],
        "provider_schema_sha256": schema_sha256, "model_snapshot": task.get("model_snapshot"),
    }, sort_keys=True).encode()).hexdigest()
    return schema, sample, schema_sha256, signature


def successful_rejection_probe(service, task, expected, policy):
    if not policy.get("diagnose_request_rejections"):
        return None
    _, sample, schema_sha256, signature = contract_details(service, task, expected)
    candidates = [p for p in service.store.list("route_probe")
                  if p["task_id"] == task["id"] and p.get("contract_signature") == signature]
    if len(candidates) != 1:
        return None
    probe = candidates[0]
    if (probe["status"] != "succeeded" or probe["expected"] != expected
            or probe["authorization_id"] != policy["authorization_id"]
            or probe.get("provider_schema_sha256") != schema_sha256
            or datetime.fromisoformat(probe["created_at"]) <
            datetime.now(timezone.utc) - timedelta(minutes=15)
            or any(a.get("contract_probe_id") == probe["id"]
                   for a in service.store.list("agent_recovery_authorization"))):
        return None
    path = service.settings.runtime / "route" / task["id"] / probe["id"] / "result.json"
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024
            or not path.resolve().is_relative_to((service.settings.runtime / "route" / task["id"]).resolve())
            or service.hash_file(path) != probe.get("result_sha256")):
        raise Conflict("Contract diagnostic result is missing or changed")
    if json.loads(path.read_text("utf-8")) != sample:
        raise Conflict("Contract diagnostic is not the non-production sample answer")
    return probe["id"]
