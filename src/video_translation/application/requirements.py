"""Production requirements and reviewed return intents, independent of advertising.

No network calls or credentials live here. The injected adapter validates discovered
remote schemas locally; a separate transport owns execution and authenticated I/O.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Protocol
from uuid import UUID

from .tasks import Conflict, TaskService
from ..persistence.store import now


class RequirementAdapter(Protocol):
    def validate_operation(self, submission: dict, operation: dict) -> str:
        """Pure validation against discovered schemas; return the schema SHA-256.

        Raise if authentication/capability/schema is unavailable. Never execute a
        business write, return credentials, or invent an unknown status payload.
        """
        ...


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Requirement/material identifier must be a UUID") from None


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("A remote updated_at timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid remote updated_at timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("Remote updated_at must include a timezone")
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("Expected a SHA-256 identity")
    return value


def _read(db, kind, key):
    row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
    if row is None:
        raise KeyError(f"{kind}: {key}")
    return json.loads(row[0])


def _save(db, kind, record):
    db.execute("INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
               (kind, record["id"], json.dumps(record, ensure_ascii=False), record.get("created_at", now())))


class RequirementService:
    def __init__(self, tasks: TaskService, adapter: RequirementAdapter | None = None):
        self.tasks, self.store, self.adapter = tasks, tasks.store, adapter

    def list(self):
        return self.store.list("requirement")

    def get(self, requirement_id):
        return self.store.require("requirement", _uuid(requirement_id))

    def submissions(self, requirement_id=None):
        rows = self.store.list("requirement_submission")
        return rows if requirement_id is None else [r for r in rows if r["requirement_id"] == _uuid(requirement_id)]

    def register(self, brief: dict, sources: list, locales: list[str]) -> dict:
        """Register an explicitly selected brief and local assets; never claim it remotely."""
        requirement_id = _uuid(brief.get("id"))
        code = brief.get("code", "")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", code):
            raise ValueError("A stable requirement code is required")
        if not locales or any(not isinstance(v, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", v) for v in locales):
            raise ValueError("Explicit canonical target locales are required")
        locales = sorted(set(locales))
        if brief.get("locales") and set(brief["locales"]) != set(locales):
            raise Conflict("Brief locales differ from the explicitly selected locales")
        if brief.get("assetType") != "video":
            raise ValueError("Only production video requirements are supported")
        normalized_sources = []
        for item in sources:
            item = {"asset_id": item} if isinstance(item, str) else item
            if not isinstance(item, dict) or not isinstance(item.get("asset_id"), str):
                raise ValueError("Each source needs a registered asset_id")
            asset = self.store.require("asset", item["asset_id"])
            digest = self.tasks.hash_file(self.tasks.checked_path(asset))
            if digest != asset["sha256"] or item.get("sha256", digest) != digest:
                raise Conflict("Registered source bytes changed")
            source_id = item.get("source_id", asset["id"])
            if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
                raise ValueError("Source identity must be a bounded nonempty string")
            normalized_sources.append({"asset_id": asset["id"], "source_id": source_id, "sha256": digest})
        if not normalized_sources or len({s["asset_id"] for s in normalized_sources}) != len(normalized_sources):
            raise ValueError("Sources must be nonempty and unique")
        if len({s["source_id"] for s in normalized_sources}) != len(normalized_sources):
            raise ValueError("Business source identities must be unique")
        normalized_sources.sort(key=lambda s: s["asset_id"])
        # A whitelist prevents auth/header/raw connector objects from reaching SQLite.
        business = {k: brief.get(k) for k in ("title", "description", "refText", "deptId", "quantity", "channels")}
        if "aiMode" in brief:
            mode = brief["aiMode"]
            if mode is not None and (not isinstance(mode, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", mode)):
                raise ValueError("Brief aiMode must be a valid delivery type or null")
            business["aiMode"] = mode
        for key in ("title", "description", "refText", "deptId"):
            if business[key] is not None and not isinstance(business[key], str):
                raise ValueError("Brief text fields must be strings or null")
        if business["quantity"] is not None and (type(business["quantity"]) is not int or business["quantity"] < 1):
            raise ValueError("Brief quantity must be a positive integer")
        if business["channels"] is not None and (not isinstance(business["channels"], list) or not all(isinstance(v, str) for v in business["channels"])):
            raise ValueError("Brief channels must be strings")
        content = {"id": requirement_id, "code": code, "assetType": "video", **business,
                   "sources": normalized_sources, "locales": locales}
        version = _digest(content)
        record = {**content, "content_version": version, "remote_updated_at": _timestamp(brief.get("updatedAt")),
                  "remote_status": brief.get("status") if isinstance(brief.get("status"), str) else None,
                  "permissions": {"canBindMaterials": brief.get("canBindMaterials") is True,
                                  "canUpdateStatus": brief.get("canUpdateStatus") is True},
                  "expected": [{"asset_id": s["asset_id"], "locale": loc} for s in normalized_sources for loc in locales],
                  "local_status": "registered", "remote_claimed": False, "task_ids": [],
                  "created_at": now(), "updated_at": now(), "history": []}
        with self.store.connection() as db:
            duplicate = db.execute("SELECT data FROM records WHERE kind='requirement'").fetchall()
            if any(json.loads(row[0])["code"] == code and json.loads(row[0])["id"] != requirement_id for row in duplicate):
                raise Conflict("Requirement code already identifies a different UUID")
            old = next((json.loads(row[0]) for row in duplicate if json.loads(row[0])["id"] == requirement_id), None)
            if old:
                record.update(created_at=old["created_at"], task_ids=old["task_ids"], history=old.get("history", []))
                if old["content_version"] == version:
                    record["local_status"] = old["local_status"]
                else:
                    record["history"] = [*record["history"], {k: old[k] for k in
                        ("content_version", "sources", "locales", "expected", "remote_updated_at")}]
            _save(db, "requirement", record)
        return record

    def accept(self, requirement_id):
        def mutate(record):
            if record["local_status"] != "returned":
                record.update(local_status="accepted", accepted_at=now(), remote_claimed=False)
        return self.store.update("requirement", _uuid(requirement_id), mutate)

    def link_task(self, requirement_id, task_id):
        with self.store.connection() as db:
            record, task = _read(db, "requirement", _uuid(requirement_id)), _read(db, "task", task_id)
            if record["local_status"] != "accepted":
                raise Conflict("Explicit local acceptance is required before task association")
            if task.get("requirement_id") not in (None, record["id"]):
                raise Conflict("Task is already associated with another requirement")
            expected = {(p["asset_id"], p["locale"]) for p in record["expected"]}
            if expected != {(a, loc) for a in task["asset_ids"] for loc in task["target_locales"]}:
                raise Conflict("Task assets/locales must cover the exact requirement matrix")
            if task.get("requirement_content_version") not in (None, record["content_version"]):
                raise Conflict("Existing task belongs to a different requirement version")
            # One active production task per version prevents duplicate external delivery.
            for other_id in record["task_ids"]:
                other = _read(db, "task", other_id)
                if other_id != task_id and other.get("requirement_content_version") == record["content_version"] and other["status"] != "cancelled":
                    raise Conflict("This requirement version already has an active production task")
            task.update(requirement_id=record["id"], requirement_content_version=record["content_version"], updated_at=now())
            record["task_ids"] = list(dict.fromkeys([*record["task_ids"], task_id]))
            record["updated_at"] = now()
            _save(db, "task", task)
            _save(db, "requirement", record)
        return record

    def _deliverables(self, db, task, requirement):
        if task["status"] != "completed" or task.get("requirement_content_version") != requirement["content_version"]:
            raise Conflict("Task is incomplete or the requirement content version changed")
        expected = {(p["asset_id"], p["locale"]) for p in requirement["expected"]}
        if expected != {(a, loc) for a in task["asset_ids"] for loc in task["target_locales"]}:
            raise Conflict("Task coverage changed")
        for source in requirement["sources"]:
            asset = _read(db, "asset", source["asset_id"])
            if self.tasks.hash_file(self.tasks.checked_path(asset)) != source["sha256"]:
                raise Conflict("Requirement source bytes changed")
        deliveries = [json.loads(r[0]) for r in db.execute("SELECT data FROM records WHERE kind='delivery'").fetchall()]
        deliveries = [d for d in deliveries if d["task_id"] == task["id"]]
        if len(deliveries) != len(expected) or {(d["asset_id"], d["locale"]) for d in deliveries} != expected:
            raise Conflict("Every source/locale needs exactly one reviewed delivery")
        result = []
        for delivery in deliveries:
            artifact = _read(db, "artifact", delivery["artifact_id"])
            if artifact["task_id"] != task["id"] or artifact["locale"] != delivery["locale"]:
                raise Conflict("Delivery artifact belongs to another task or locale")
            digest = self.tasks.hash_file(self.tasks.checked_path(artifact))
            reviews = [n for n in task["nodes"] if n["definition_id"] == "review" and
                       n["asset_id"] == delivery["asset_id"] and n["locale"] == delivery["locale"]]
            if (len(reviews) != 1 or reviews[0]["status"] != "succeeded" or
                    reviews[0]["output"].get("quality_status") != "human_approved" or
                    reviews[0]["output"].get("approved_sha256") != digest or
                    delivery.get("quality_status") != "human_approved" or artifact["sha256"] != digest):
                raise Conflict("Delivery is not bound to its current complete human approval")
            result.append({"delivery_id": delivery["id"], "asset_id": delivery["asset_id"], "locale": delivery["locale"],
                           "artifact_id": artifact["id"], "sha256": digest, "review_node_id": reviews[0]["id"]})
        return sorted(result, key=lambda item: (item["asset_id"], item["locale"]))

    def prepare_submission(self, task_id):
        with self.store.connection() as db:
            task = _read(db, "task", task_id)
            if not task.get("requirement_id"):
                raise Conflict("Task is not linked to a production requirement")
            requirement = _read(db, "requirement", task["requirement_id"])
            if requirement["local_status"] not in {"accepted", "returned"}:
                raise Conflict("The current requirement version has not been accepted locally")
            deliveries = self._deliverables(db, task, requirement)
            identity = {"requirement_id": requirement["id"], "task_id": task_id,
                        "content_version": requirement["content_version"], "deliverables": deliveries}
            submission_id = _digest(identity)
            existing = db.execute("SELECT data FROM records WHERE kind='requirement_submission'").fetchall()
            for row in existing:
                old = json.loads(row[0])
                if old["id"] == submission_id:
                    return old
                if old["task_id"] == task_id:
                    raise Conflict("Preserve the earlier submission; explicit revision reconciliation is required")
            operations = []
            for item in deliveries:
                for phase in ("upload", "import", "verify"):
                    operations.append({"id": f"{phase}:{item['delivery_id']}", "phase": phase, "state": "draft",
                                       "delivery_id": item["delivery_id"], "sha256": item["sha256"]})
            operations.extend({"id": phase, "phase": phase, "state": "draft"}
                              for phase in ("bind", "verify_delivery", "complete", "verify_status"))
            blockers = [] if self.adapter is not None else ["requirement_adapter_unavailable", "remote_schema_not_verified"]
            if not requirement["permissions"]["canBindMaterials"]:
                blockers.append("material_binding_permission_missing")
            if not requirement["permissions"]["canUpdateStatus"]:
                blockers.append("status_update_permission_missing")
            result = {"id": submission_id, **identity, "code": requirement["code"], "expected": requirement["expected"],
                      "base_updated_at": requirement["remote_updated_at"], "state": "prepared", "mode": "local_preview",
                      "operations": operations, "blockers": blockers, "external_call_executed": False,
                      "executable": False, "live_schema_verified": False,
                      "remote_completion_verified": False, "created_at": now(), "updated_at": now()}
            _save(db, "requirement_submission", result)
        return result

    def _current(self, db, submission):
        task = _read(db, "task", submission["task_id"])
        requirement = _read(db, "requirement", submission["requirement_id"])
        if submission["content_version"] != requirement["content_version"]:
            raise Conflict("Requirement business content changed after submission preparation")
        if self._deliverables(db, task, requirement) != submission["deliverables"]:
            raise Conflict("Reviewed delivery identity changed after preview")
        return requirement

    def mark_unknown(self, submission_id, operation_id):
        """Persist BEFORE the caller sends a write; unknown operations cannot be replayed."""
        if self.adapter is None:
            raise Conflict("Requirement adapter/schema unavailable; preview is not executable")
        with self.store.connection() as db:
            submission = _read(db, "requirement_submission", submission_id)
            requirement = self._current(db, submission)
            operations = submission["operations"]
            matches = [i for i, op in enumerate(operations) if op["id"] == operation_id]
            if len(matches) != 1:
                raise KeyError(operation_id)
            index = matches[0]
            operation = operations[index]
            if operation["state"] not in {"draft", "prepared"}:
                raise Conflict("Operation has already been dispatched; reconcile instead of replaying")
            if any(op["state"] != "verified" for op in operations[:index]):
                raise Conflict("Previous submission stages are not verified")
            if not requirement["permissions"]["canBindMaterials"]:
                raise Conflict("Current requirement does not allow material binding")
            if operation["phase"] == "complete":
                if not requirement["permissions"]["canUpdateStatus"]:
                    raise Conflict("Current requirement does not allow completion status updates")
                observed = next(op for op in operations if op["id"] == "verify_delivery")["receipt"]
                if observed["waiting_selection"] is not False:
                    raise Conflict("Platform material selection is still pending")
                if requirement["remote_updated_at"] != observed["remote_updated_at"]:
                    raise Conflict("Requirement updated_at changed since the verified delivery read")
                # The preceding actual delivery read owns the optimistic concurrency version.
                submission["base_updated_at"] = observed["remote_updated_at"]
            schema_sha256 = _sha(self.adapter.validate_operation(copy.deepcopy(submission), copy.deepcopy(operation)))
            operation.update(state="unknown", schema_sha256=schema_sha256, dispatched_at=now())
            submission.update(state="unknown", mode="pending_external_evidence", external_call_executed=None, updated_at=now())
            _save(db, "requirement_submission", submission)
        return copy.deepcopy(operation)

    def needs_reconciliation(self, submission_id, operation_id):
        def mutate(submission):
            operation = next((op for op in submission["operations"] if op["id"] == operation_id), None)
            if operation is None:
                raise KeyError(operation_id)
            if operation["state"] not in {"unknown", "needs_reconciliation"}:
                raise Conflict("Only an uncertain dispatched operation can require reconciliation")
            operation["state"] = "needs_reconciliation"
            submission["state"] = "needs_reconciliation"
        return self.store.update("requirement_submission", submission_id, mutate)

    def verify_operation(self, submission_id, operation_id, receipt: dict):
        """Consume normalized authenticated evidence, never raw headers or credentials.

        This is an internal transport boundary, not a user-facing 'mark success' API.
        Receipt fields are domain facts, not invented remote tool parameters.
        """
        with self.store.connection() as db:
            submission = _read(db, "requirement_submission", submission_id)
            requirement = self._current(db, submission)
            operation = next((op for op in submission["operations"] if op["id"] == operation_id), None)
            if operation is None:
                raise KeyError(operation_id)
            if operation["state"] not in {"unknown", "needs_reconciliation"}:
                raise Conflict("Operation is not awaiting external verification")
            if (receipt.get("verified") is not True or receipt.get("preview") is True or
                    receipt.get("operation_id") != operation_id or
                    receipt.get("requirement_id") != submission["requirement_id"] or
                    receipt.get("content_version") != submission["content_version"]):
                raise Conflict("Receipt does not verify this requirement/version/operation")
            observed_at = _timestamp(receipt.get("observed_at"))
            if datetime.fromisoformat(observed_at.replace("Z", "+00:00")) < datetime.fromisoformat(operation["dispatched_at"]):
                raise Conflict("Read-back predates the dispatched operation")
            normalized = {"observed_at": observed_at, "evidence_sha256": _sha(receipt.get("evidence_sha256"))}
            phase = operation["phase"]
            material_ids = [op["receipt"]["material_id"] for op in submission["operations"]
                            if op["phase"] == "import" and op["state"] == "verified"]
            if phase in {"upload", "import", "verify"}:
                if receipt.get("sha256") != operation["sha256"]:
                    raise Conflict("External receipt belongs to different reviewed bytes")
                normalized["sha256"] = operation["sha256"]
                if phase == "upload":
                    normalized["resource_id"] = _uuid(receipt.get("resource_id"))
                else:
                    material_id = _uuid(receipt.get("material_id"))
                    if phase == "import" and material_id in material_ids:
                        raise Conflict("One material cannot satisfy multiple requested deliveries")
                    if phase == "verify":
                        imported = next(op for op in submission["operations"] if op["id"] == f"import:{operation['delivery_id']}")
                        if material_id != imported["receipt"]["material_id"]:
                            raise Conflict("Material read-back differs from the imported identity")
                    normalized["material_id"] = material_id
            elif phase in {"bind", "verify_delivery"}:
                actual = receipt.get("material_ids")
                if not isinstance(actual, list) or not set(material_ids).issubset({_uuid(v) for v in actual}):
                    raise Conflict("Requirement binding does not cover all reviewed materials")
                normalized["material_ids"] = material_ids
                if phase == "verify_delivery":
                    if receipt.get("waiting_selection") is not False:
                        raise Conflict("Platform selection is pending or unverified")
                    normalized.update(waiting_selection=False, remote_updated_at=_timestamp(receipt.get("remote_updated_at")))
                    if datetime.fromisoformat(normalized["remote_updated_at"].replace("Z", "+00:00")) < datetime.fromisoformat(requirement["remote_updated_at"].replace("Z", "+00:00")):
                        raise Conflict("Delivery read-back has an obsolete requirement updated_at")
                    requirement.update(remote_updated_at=normalized["remote_updated_at"], updated_at=now())
                    _save(db, "requirement", requirement)
            else:
                if receipt.get("completed") is not True:
                    raise Conflict("Remote completion was not verified")
                if phase == "complete" and receipt.get("base_updated_at") != submission["base_updated_at"]:
                    raise Conflict("Completion update used a different optimistic concurrency version")
                if phase == "verify_status" and receipt.get("waiting_selection") is not False:
                    raise Conflict("Cannot finish while platform selection is pending")
                normalized.update(completed=True, remote_updated_at=_timestamp(receipt.get("remote_updated_at")))
            operation.update(state="verified", receipt=normalized, verified_at=now())
            complete = all(op["state"] == "verified" for op in submission["operations"])
            submission.update(state="verified" if complete else "prepared", external_call_executed=True,
                              remote_completion_verified=complete, updated_at=now())
            if complete:
                requirement.update(local_status="returned", remote_completion_verified=True,
                                   remote_updated_at=normalized["remote_updated_at"], updated_at=now())
                _save(db, "requirement", requirement)
            _save(db, "requirement_submission", submission)
        return submission
