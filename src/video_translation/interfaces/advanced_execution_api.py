"""Explicit admission, branch rework and local artifact lifecycle controls."""
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from video_translation.application.directed_rework import DirectedRework
from video_translation.domain.rework import ReworkRequest
from video_translation.application.execution_lifecycle import (
    cache_retention_plan, preview_representation, prune_cache,
)
from video_translation.application.tasks import Conflict
from video_translation.domain.execution import identity
from video_translation.domain.intake_policy import IntakePolicy
from video_translation.persistence.store import now


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyAuthorization(Body):
    enabled: bool
    report_id: str | None = None


class DeferRequest(Body):
    until: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)


class ReworkApproval(Body):
    expected_basis: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve: bool = False


class RetentionApproval(Body):
    expected_plan: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve: bool = False


class RepresentationPreview(Body):
    session_id: str
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FollowUp(Body):
    expected_basis: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner: str = Field(min_length=1, max_length=100)
    due_at: datetime | None = None
    defer_until: datetime | None = None
    notify: bool = True


def advanced_routes(service, worker, manager):
    router = APIRouter(prefix="/api")
    policies = manager.policy_intake
    rework = DirectedRework(service)

    @router.get("/intake-policies")
    def list_policies():
        return {"policies": service.store.list("intake_policy"),
                "dispatches": service.store.list("intake_dispatch")[:100],
                "default_enabled": False}

    @router.get("/intake-policies/context")
    def intake_context():
        return {"identity": policies.intake._identity(),
                "scope": policies.intake.fireplay.settings.fireplay_scope,
                "snapshot": policies.intake.fireplay.projects(),
                "source_trust_requires_explicit_review": True}

    @router.put("/intake-policies/{policy_id}")
    def save_policy(policy_id: str, body: IntakePolicy):
        return policies.save(policy_id, body)

    @router.post("/intake-policies/{policy_id}/dry-run")
    def dry_run(policy_id: str):
        return policies.dry_run(policy_id)

    @router.post("/intake-policies/{policy_id}/authorization")
    def authorize(policy_id: str, body: PolicyAuthorization):
        return policies.authorize(policy_id, body.report_id, body.enabled)

    @router.post("/intake-policies/{policy_id}/scan")
    def scan(policy_id: str):
        return policies.scan(policy_id)

    @router.post("/intake-dispatches/{dispatch_id}/defer")
    def defer(dispatch_id: str, body: DeferRequest):
        return policies.defer(dispatch_id, until=body.until, priority=body.priority)

    @router.post("/tasks/{task_id}/directed-rework")
    def propose(task_id: str, body: ReworkRequest):
        return rework.propose(task_id, body)

    @router.get("/directed-rework/{proposal_id}")
    def get_proposal(proposal_id: str):
        return service.store.require("directed_rework", proposal_id)

    @router.post("/directed-rework/{proposal_id}/apply")
    def apply(proposal_id: str, body: ReworkApproval):
        proposal = service.store.require("directed_rework", proposal_id)
        with worker.guard(proposal["task_id"]):
            return rework.apply(proposal_id, body.expected_basis, approve=body.approve)

    @router.post("/tasks/{task_id}/representation-preview")
    def preview(task_id: str, body: RepresentationPreview):
        with worker.guard(task_id):
            return preview_representation(service, task_id, body.session_id, body.expected_base_sha256)

    @router.get("/tasks/{task_id}/cache-retention-plan")
    def retention_plan(task_id: str):
        return cache_retention_plan(service, task_id)

    @router.post("/tasks/{task_id}/cache-retention")
    def retire(task_id: str, body: RetentionApproval):
        with worker.guard(task_id):
            return prune_cache(service, task_id, body.expected_plan, approve=body.approve)

    @router.patch("/manager/incidents/{incident_id}/follow-up")
    def follow_up(incident_id: str, body: FollowUp):
        for stamp in (body.due_at, body.defer_until):
            if stamp is not None and stamp.tzinfo is None:
                raise ValueError("Issue deadlines require an explicit timezone")
        def mutate(row):
            basis = {k: v for k, v in row.items() if k not in {"follow_up", "follow_up_history", "updated_at"}}
            if identity(basis) != body.expected_basis or row["status"] != "open":
                raise Conflict("Issue version changed; refresh before assigning responsibility")
            if row.get("follow_up"):
                row.setdefault("follow_up_history", []).append(row["follow_up"])
            row["follow_up"] = {**body.model_dump(mode="json", exclude={"expected_basis"}), "assigned_at": now()}
        return service.store.update("manager_incident", incident_id, mutate)

    @router.get("/manager/incidents/{incident_id}/follow-up")
    def get_follow_up(incident_id: str):
        row = service.store.require("manager_incident", incident_id)
        basis = {k: v for k, v in row.items() if k not in {"follow_up", "follow_up_history", "updated_at"}}
        return {"incident": row, "basis_sha256": identity(basis)}

    @router.get("/manager/follow-ups")
    def follow_ups():
        # One stable incident identity merges reminders; this endpoint sends nothing externally.
        current = datetime.now().astimezone()
        rows = []
        for row in service.store.list("manager_incident"):
            follow = row.get("follow_up")
            if row["status"] != "open" or not follow or not follow["notify"]:
                continue
            if follow.get("defer_until") and datetime.fromisoformat(follow["defer_until"]) > current:
                continue
            if follow.get("due_at") and datetime.fromisoformat(follow["due_at"]) > current:
                continue
            rows.append({"incident_id": row["id"], "target_id": row["target_id"],
                         "title": row["title"], **follow})
        return {"items": rows[:100], "external_messages_sent": False}

    return router
