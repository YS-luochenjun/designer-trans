"""Explicitly replace one unrecoverable clean proposal, preserving its unknown receipt."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, ValidationError

from video_translation.application.agent_recovery import replaced_with_authorization
from video_translation.application.tasks import Conflict
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status


def _recoverable_answer(text, schema):
    # Presentation-only differences require recovery, not another model request.
    text = text.removeprefix("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*)\r?\n```", text, re.DOTALL)
    if fenced:
        text = fenced[1]
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        return False
    try:
        value = json.loads(text, strict=False)
        json.dumps(value, allow_nan=False)
        if not isinstance(value, dict):
            return False
        Draft202012Validator(schema).validate(value)
    except (ValueError, ValidationError):
        return False
    return True


def authorize_clean_agent_replacement(service, task_id, expected_decision_id,
                                      expected_base_sha256, expected_answer_sha256):
    return _authorize_replacement(service, task_id, expected_decision_id, expected_base_sha256,
                                  expected_answer_sha256=expected_answer_sha256)


def authorize_missing_clean_agent_replacement(service, task_id, expected_decision_id,
                                              expected_base_sha256, expected_request_sha256,
                                              expected_receipt_sha256):
    return _authorize_replacement(service, task_id, expected_decision_id, expected_base_sha256,
                                  expected_request_sha256=expected_request_sha256,
                                  expected_receipt_sha256=expected_receipt_sha256)


def _authorize_replacement(service, task_id, expected_decision_id, expected_base_sha256, *,
                           expected_answer_sha256=None, expected_request_sha256=None,
                           expected_receipt_sha256=None, route_run_id=None, apply=True,
                           expected_candidate_fingerprint=None, route_preview=False):
    task = service.store.require("task", task_id)
    before_task = deepcopy(task)
    decision = service.store.require("production_decision", expected_decision_id)
    node = next((n for n in task["nodes"] if n["id"] == decision.get("node_id")), None)
    sessions = [s for s in service.store.list("production_session")
                if s.get("task_id") == task_id and node and s.get("node_id") == node["id"]]
    if (task.get("agent_engine") != "codex" or task["status"] != "blocked"
            or task.get("control_paused") or not node or node["definition_id"] != "clean"
            or node["status"] != "unknown" or decision.get("task_id") != task_id
            or decision["status"] != "unknown" or len(sessions) != 1):
        raise Conflict("Replacement requires the stopped current unknown clean proposal")
    state = sessions[0]
    before_state = deepcopy(state)
    candidate = state.get("candidate")
    candidate_bound = False
    if candidate and (route_run_id or route_preview and not apply) and expected_candidate_fingerprint:
        candidate_bound = hashlib.sha256(json.dumps(
            candidate, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == expected_candidate_fingerprint
        candidate_path = Path(candidate.get("path", ""))
        root = (service.settings.runtime / "tasks" / task_id).resolve()
        candidate_bound = bool(candidate_bound and candidate.get("input_sha256") == expected_base_sha256
                               and not candidate_path.is_symlink() and candidate_path.is_file()
                               and candidate_path.resolve().is_relative_to(root)
                               and service.hash_file(candidate_path) == candidate.get("sha256"))
    if (candidate and not candidate_bound or not candidate and expected_candidate_fingerprint
            or state.get("pending_turn") is not None
            or state.get("finished") or decision.get("key") != f"clean-{state['turn']}"
            or state["base"]["sha256"] != expected_base_sha256
            or any(n["status"] in {"running", "waiting_external"} or n.get("external_pending")
                   for n in task["nodes"])):
        raise Conflict("Task/base/current proposal changed; inspect before authorizing")
    # Downstream unknown calls retain their own identity and remain blocked.
    ancestors, pending = set(node["depends_on"]), list(node["depends_on"])
    while pending:
        parent_id = pending.pop()
        parent = next(n for n in task["nodes"] if n["id"] == parent_id)
        for identifier in parent["depends_on"]:
            if identifier not in ancestors:
                ancestors.add(identifier)
                pending.append(identifier)
    if any(n["id"] in ancestors and n["status"] not in {"succeeded", "skipped"} for n in task["nodes"]):
        raise Conflict("Upstream prerequisites are not ready")
    outstanding = [d for d in service.store.list("production_decision")
                   if d.get("task_id") == task_id and d.get("node_id") == node["id"]
                   and d["status"] in {"running", "unknown", "invalid_response"}
                   and not replaced_with_authorization(service.store, d, "production_decision")]
    operations = [op for op in service.store.list("operation")
                  if op.get("task_id") == task_id and op.get("node_id") == node["id"]
                  and op["status"] in {"running", "unknown"}
                  and not replaced_with_authorization(service.store, op, "operation")]
    traces = [tr for tr in service.store.list("agent_trace")
              if tr.get("task_id") == task_id and tr.get("node_id") == node["id"]
              and tr.get("decision_id") == decision["id"][:20] and tr["status"] == "unknown"]
    if (len(outstanding) != 1 or outstanding[0]["id"] != decision["id"]
            or len(operations) != 1 or operations[0].get("provider") != "agent"
            or operations[0].get("kind") != "clean" or len(traces) != 1
            or operations[0]["created_at"] < decision["created_at"]
            or traces[0].get("attempt") != node["attempt"]):
        raise Conflict("Cannot bind replacement to one original Agent operation and trace")
    if any(r.get("task_id") == task_id and r["status"] in {"pending", "claimed"}
           for r in service.store.list("imagegen_request")):
        raise Conflict("An image tool request still needs reconciliation")
    missing = expected_answer_sha256 is None
    if missing:
        descendants = {node["id"]}
        # Workflow nodes are in topological order.
        for child in task["nodes"]:
            if descendants.intersection(child["depends_on"]):
                descendants.add(child["id"])
        if (any(op.get("task_id") == task_id and op.get("status") == "running"
                for op in service.store.list("operation"))
                or any(r.get("task_id") == task_id and r.get("status") == "unknown"
                       for r in service.store.list("imagegen_request"))
                or any(n["status"] == "unknown" and n["id"] not in descendants for n in task["nodes"])
                or any(op.get("task_id") == task_id and op.get("status") == "unknown"
                       and op.get("node_id") not in descendants
                       for op in service.store.list("operation"))):
            raise Conflict("An external operation or unknown image still needs reconciliation")
    root = (service.settings.runtime / "tasks" / task_id).resolve()
    from .clean_base import verified_clean_base
    verified_clean_base(service, task, state, expected_base_sha256)
    recovery = root / "nodes" / node["id"] / str(node["attempt"]) / (decision["id"][:20] + "-codex-recovery")
    answer = recovery / "answer.json"
    if (recovery.is_symlink()
            or not recovery.resolve().is_relative_to(root)):
        raise Conflict("Retained base or original recovery directory is missing/changed")
    for path in (recovery / "request.json", recovery / "receipt.json"):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise Conflict("Original request/receipt evidence is missing")
    try:
        request = json.loads((recovery / "request.json").read_text("utf-8"))
        schema = request["schema"]
        receipt = json.loads((recovery / "receipt.json").read_text("utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise Conflict("Original request/receipt cannot be validated") from None
    if receipt.get("dispatch_possible") is not True:
        raise Conflict("Original dispatch identity cannot be confirmed")
    if missing:
        if (service.hash_file(recovery / "request.json") != expected_request_sha256
                or service.hash_file(recovery / "receipt.json") != expected_receipt_sha256
                or not re.fullmatch(r"[0-9a-f]{64}", str(request.get("sha256", "")))
                or receipt.get("status") != "unknown"
                or receipt.get("reason") not in {
                    "cli_nonzero_after_dispatch", "deadline_exceeded", "worker_interrupted",
                    "result_unknown", "missing_final_answer"}):
            raise Conflict("Original stopped request/receipt evidence is missing/changed")
        if any((recovery / name).exists() or (recovery / name).is_symlink()
               for name in ("answer.json", "result.json", "event-result.json")):
            raise Conflict("Retained result evidence requires reconciliation before replacement")
        evidence = {
            "evidence_kind": "missing_final_answer",
            "original_request_sha256": expected_request_sha256,
            "original_receipt_sha256": expected_receipt_sha256,
            "request_fingerprint": request["sha256"],
        }
        evidence_id = expected_receipt_sha256
    else:
        if (answer.is_symlink() or not answer.is_file() or answer.stat().st_size > 2 * 1024 * 1024
                or service.hash_file(answer) != expected_answer_sha256):
            raise Conflict("Retained original answer is missing/changed")
        if _recoverable_answer(answer.read_text("utf-8"), schema):
            raise Conflict("A valid retained answer must be reconciled, not replaced")
        evidence = {"evidence_kind": "invalid_final_answer", "original_answer_sha256": expected_answer_sha256}
        evidence_id = expected_answer_sha256
    replacement_key = f"clean-{state['turn'] + 1}"
    if any(d.get("task_id") == task_id and d.get("node_id") == node["id"]
           and d.get("key") == replacement_key for d in service.store.list("production_decision")):
        raise Conflict("Replacement turn is already assigned")
    delegated = None
    if route_run_id:
        policy = service.store.require("route_policy", task_id)
        run = service.store.require("route_run", route_run_id)
        expected = {
            "expected_decision_id": expected_decision_id, "expected_base_sha256": expected_base_sha256,
            "expected_request_sha256": expected_request_sha256, "expected_receipt_sha256": expected_receipt_sha256}
        if expected_candidate_fingerprint:
            expected["expected_candidate_fingerprint"] = expected_candidate_fingerprint
        contract_probe_id = None
        if receipt.get("failure_kind") == "request_rejected":
            from .route_contract_probe import successful_rejection_probe
            contract_probe_id = successful_rejection_probe(service, task, expected, policy)
        if (not missing or not policy["enabled"] or policy["node_id"] != node["id"]
                or run["status"] != "decided" or run["task_id"] != task_id
                or run["authorization_id"] != policy["authorization_id"]
                or run["decision"]["action"] != "replace_work"
                or run["expected"] != expected
                or (receipt.get("failure_kind") not in {"transport", "rate_limit", "provider_unavailable"}
                    and not contract_probe_id)):
            raise Conflict("Route decision is outside the delegated recovery authority")
        delegated = {"authority": "route_agent", "route_run_id": route_run_id,
                     "delegation_id": policy["authorization_id"], "explicit_user_authorization": False}
        if candidate_bound:
            delegated["retained_candidate_fingerprint"] = expected_candidate_fingerprint
        if contract_probe_id:
            delegated["contract_probe_id"] = contract_probe_id
            delegated["recovery_basis"] = "one_new_work_after_successful_structured_contract_probe"
    authorization = {
        "id": hashlib.sha256(f"{task_id}:{decision['id']}:{evidence_id}".encode()).hexdigest(),
        "task_id": task_id, "node_id": node["id"], "status": "applied", "at": now(),
        "scope": "one_new_clean_proposal", "replacement_key": replacement_key,
        "base_sha256": expected_base_sha256, **evidence,
        "original_recovery_path": str(recovery), "explicit_user_authorization": True,
        "superseded": {"operation": operations[0]["id"], "production_decision": decision["id"]},
        "original_status_unchanged": True, "cost_status": "unknown",
        "media_generation_authorization_unchanged": True,
        **(delegated or {}),
    }
    if not apply:
        return {"authorization": authorization, "applied": False}
    state.setdefault("agent_replacement_authorizations", []).append(authorization["id"])
    state["turn"] += 1
    node.setdefault("history", []).append({
        "attempt": node["attempt"], "status": node["status"], "error": node.get("error"),
        "reason": "Explicit replacement of one stopped Agent proposal",
        "authorization_id": authorization["id"],
    })
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if (tx.require("task", task_id) != before_task
                or tx.require("production_session", state["id"]) != before_state
                or tx.require("production_decision", decision["id"]) != decision
                or tx.require("operation", operations[0]["id"]) != operations[0]
                or tx.require("agent_trace", traces[0]["id"]) != traces[0]):
            raise Conflict("Task changed; no authorization was applied")
        tx.put("agent_recovery_authorization", authorization)
        for kind, record in (("operation", operations[0]), ("production_decision", decision)):
            tx.update(kind, record["id"], lambda row: row.update(
                replacement_authorization_id=authorization["id"]))
        tx.put("production_session", state)
        tx.put("task", task)
    return {"task_id": task_id, "status": task["status"], "authorization": authorization,
            "accepted_unchanged": True, "budget_unchanged": True}
