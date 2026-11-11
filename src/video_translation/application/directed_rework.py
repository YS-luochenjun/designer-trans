"""V3 isolated, evidence-bound branch creation with version-specific approvals."""
from copy import deepcopy
import json
from pathlib import Path

from video_translation.domain.execution import identity
from video_translation.domain.models import TaskCreate
from video_translation.domain.rework import ReworkRequest
from video_translation.execution.workspace import file_sha
from video_translation.persistence.store import identifier, now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import instantiate, SUCCESS
from .collaboration import affected_nodes
from .tasks import Conflict, TaskService


class DirectedRework:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def _files(self, value, found, allowed):
        if isinstance(value, str):
            path = Path(value)
            if path.is_absolute():
                self._files({"path": value}, found, allowed)
            return
        if isinstance(value, dict):
            if isinstance(value.get("path"), str):
                path = Path(value["path"])
                if path.is_symlink() or not any(
                        path.resolve() == root or path.resolve().is_relative_to(root) for root in allowed):
                    raise Conflict("Inherited file leaves the project runtime")
                sha = file_sha(path)
                if value.get("sha256") and value["sha256"] != sha:
                    raise Conflict("Inherited evidence changed")
                resolved = str(path.resolve())
                if resolved not in found:
                    found[resolved] = sha
                    if path.name.endswith(".vpatch.json"):
                        self._files(json.loads(path.read_bytes()), found, allowed)
            for key, child in value.items():
                if key != "path":
                    self._files(child, found, allowed)
        elif isinstance(value, list):
            for child in value:
                self._files(child, found, allowed)

    def _basis(self, store, task_id, request):
        task = store.require("task", task_id)
        if task["status"] == "cancelled" or task.get("control_paused"):
            raise Conflict("Cancelled or paused tasks do not authorize new production")
        node = next((n for n in task["nodes"] if n["id"] == request.node_id), None)
        if not node or request.locale not in task["target_locales"] or node["locale"] not in (None, request.locale):
            raise Conflict("Rework must identify one existing source and language branch")
        selected = [n for n in task["nodes"] if n["asset_id"] == node["asset_id"]
                    and n["locale"] in (None, request.locale)]
        ids = {n["id"] for n in selected}
        affected = affected_nodes(task, node["id"], "downstream") & ids
        if any(n["status"] in {"running", "unknown", "waiting_external"} or n.get("external_pending")
               for n in selected):
            raise Conflict("Original branch has in-flight or unknown work; reconcile original receipts first")
        from .agent_recovery import replaced_with_authorization
        if any(r.get("node_id") in ids and r.get("status") in {"running", "unknown", "invalid_response",
                                                               "pending", "claimed"}
               and not replaced_with_authorization(store, r, kind)
               for kind in ("operation", "production_decision", "imagegen_request") for r in store.list(kind)):
            raise Conflict("Original external result is unresolved; branching cannot bypass it")
        inherited = [n for n in selected if n["id"] not in affected]
        if any(n["status"] not in SUCCESS for n in inherited):
            raise Conflict("Rework prerequisites must have verified successful outputs")
        evidence, files = [], {}
        allowed = [(self.service.settings.runtime / "tasks" / task_id).resolve(),
                   self.service.checked_path(store.require("asset", node["asset_id"])).resolve()]
        allowed.extend(Path(ref["path"]).resolve() for ref in task.get("rework_inputs", []))
        for evidence_id in request.evidence_ids:
            record = store.get("artifact", evidence_id) or store.get("agent_trace", evidence_id)
            if not record or record.get("task_id") != task_id or record.get("node_id") not in ids:
                raise Conflict("Rework evidence must belong to this source and language branch")
            self._files(record, files, allowed)
            evidence.append(record)
        for item in inherited:
            self._files(item["output"], files, allowed)
        basis = {"task_id": task_id, "brief_revision": task.get("brief_revision", 0),
                 "request": request.model_dump(), "nodes": selected, "evidence": evidence,
                 "files": files, "snapshots": {k: task.get(k) for k in (
                     "workflow", "agent_engine", "media_provider", "model_snapshot", "resource_snapshot",
                     "separation_snapshot", "image_snapshot", "execution_snapshot", "production_policy")
                     if k in task}}
        return task, node, selected, affected, basis

    def propose(self, task_id, value):
        request = ReworkRequest.model_validate(value)
        task, node, selected, affected, basis = self._basis(self.store, task_id, request)
        fingerprint = identity(basis)
        existing = self.store.get("directed_rework", fingerprint)
        if existing:
            return existing
        return self.store.put("directed_rework", {"id": fingerprint, "created_at": now(),
            "task_id": task_id, "request": request.model_dump(), "basis_sha256": fingerprint,
            "affected_node_ids": sorted(affected), "asset_id": node["asset_id"],
            "inherited_node_ids": [n["id"] for n in selected if n["id"] not in affected],
            "files": basis["files"], "status": "proposed", "quality_passed": False,
            "cost": {"new_allowance": {"calls": request.max_paid_calls, "images": request.max_image_calls,
                                      "kie_credits": request.max_kie_credits},
                     "amount": None, "original_committed_credits": task.get("video_credits_committed", 0),
                     "original_costs_preserved": True}})

    def apply(self, proposal_id, expected_basis, *, approve=False):
        if not approve:
            raise Conflict("Explicit approval of the evidence and new allowance is required")
        with self.store.connection() as db:
            store = TransactionStore(self.store, db)
            proposal = store.require("directed_rework", proposal_id)
            if expected_basis != proposal["basis_sha256"]:
                raise Conflict("Approval names a different evidence version")
            if proposal["status"] == "applied":
                return store.require("task", proposal["branch_task_id"])
            request = ReworkRequest.model_validate(proposal["request"])
            task, node, selected, affected, basis = self._basis(store, proposal["task_id"], request)
            if identity(basis) != expected_basis:
                raise Conflict("Rework proposal is stale; inspect the changed evidence")
            fields = {key: task[key] for key in TaskCreate.model_fields if key in task}
            fields.update(title=(task["title"][:160] + " / directed rework"),
                asset_ids=[node["asset_id"]], target_locales=[request.locale], allow_review_draft=False,
                max_paid_calls=request.max_paid_calls, max_image_calls=request.max_image_calls,
                max_video_credits=request.max_kie_credits, max_kie_credits=request.max_kie_credits,
                max_revisions=0)
            branch = TaskService(self.service.settings, store).create(TaskCreate(**fields), source="directed_rework")
            branch = store.require("task", branch["id"])
            branch.update(deepcopy(basis["snapshots"]))
            branch["nodes"] = instantiate(task["workflow"], [node["asset_id"]], [request.locale])
            for fresh in branch["nodes"]:
                original = next(n for n in selected if n["definition_id"] == fresh["definition_id"]
                                and n["locale"] == fresh["locale"])
                if original["id"] not in affected:
                    fresh.update(status=original["status"], output=deepcopy(original["output"]),
                        skip_reason=original.get("skip_reason"), reused_from={
                            "task_id": task["id"], "node_id": original["id"], "attempt": original["attempt"],
                            "output_sha256": identity(original["output"])})
            branch["rework_inputs"] = [{"path": path, "sha256": sha} for path, sha in proposal["files"].items()]
            branch["rework_origin"] = {"proposal_id": proposal_id, "task_id": task["id"],
                "node_id": node["id"], "reason": request.reason, "evidence_ids": request.evidence_ids}
            branch["notes"] = (task.get("notes", "")[:5500] + "\nRework: " + request.reason)
            branch["control_paused"] = False
            branch["brief_revision"] = task.get("brief_revision", 0)
            branch["confirmed_briefs"] = {
                node["asset_id"]: task["confirmed_briefs"][node["asset_id"]]
            } if node["asset_id"] in task.get("confirmed_briefs", {}) else {}
            store.put("task", branch)
            proposal.update(status="applied", branch_task_id=branch["id"], applied_at=now(),
                            authorization_id=identifier())
            store.put("directed_rework", proposal)
            return branch
