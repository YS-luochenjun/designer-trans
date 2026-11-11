"""V2 bounded admission, persistent reservations, and original-Intake dispatch."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from video_translation.domain.execution import identity
from video_translation.domain.intake_policy import IntakePolicy
from video_translation.persistence.store import now, identifier
from video_translation.persistence.transaction import TransactionStore
from .intake import IntakeCreate, digest
from .tasks import Conflict


TERMINAL = {"completed", "cancelled"}


class AdmissionCapacity(Conflict):
    pass


class PolicyIntake:
    def __init__(self, intake):
        self.intake, self.store = intake, intake.store

    def save(self, policy_id, value):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", policy_id):
            raise ValueError("Invalid policy identifier")
        policy = IntakePolicy.model_validate(value)
        if policy.identity != self.intake._identity() or policy.scope != self.intake.fireplay.settings.fireplay_scope:
            raise Conflict("Policy must bind the current account and scope")
        with self.store.connection() as db:
            store = TransactionStore(self.store, db)
            previous = store.get("intake_policy", policy_id)
            return store.put("intake_policy", {"id": policy_id, "created_at": now(),
                "version": (previous or {}).get("version", 0) + 1, "policy": policy.model_dump(),
                "policy_sha256": identity(policy.model_dump()), "enabled": False,
                "authorization_id": None})

    def _current(self, row):
        policy = IntakePolicy.model_validate(row["policy"])
        if (policy.identity != self.intake._identity()
                or policy.scope != self.intake.fireplay.settings.fireplay_scope):
            raise Conflict("Account or business scope changed; authorization does not carry over")
        return policy

    def _snapshot(self, policy):
        snapshot = self.intake.fireplay.projects()
        if (snapshot.get("complete") is not True or snapshot.get("error")
                or snapshot.get("scope") != policy.scope
                or set(snapshot.get("selected_delivery_types", [])) != set(policy.delivery_types)
                or len(snapshot["items"]) > policy.max_scan):
            raise Conflict("Complete, bounded pagination under the exact delivery scope is required")
        synced = snapshot.get("last_synced_at")
        if not synced or datetime.fromisoformat(synced) < datetime.now(timezone.utc) - timedelta(minutes=30):
            raise Conflict("Requirement snapshot is stale")
        return snapshot

    def _eligible(self, policy, preview):
        brief = preview["brief"]
        if (brief["deptId"] not in policy.department_ids or brief.get("aiMode") not in policy.delivery_types
                or not brief["locales"] or set(brief["locales"]) != set(policy.locales)):
            raise Conflict("Department, delivery type or language is outside the reviewed policy")
        selected = []
        for source in preview["sources"]:
            parsed = urlsplit(source.get("url", ""))
            if (source.get("downloadable") and source.get("origin_field") in policy.trusted_origin_fields
                    and parsed.scheme == "https" and parsed.hostname in policy.trusted_hosts
                    and not parsed.username and not parsed.password and parsed.port in (None, 443)):
                selected.append(source["id"])
        if not selected or len(selected) != len(preview["sources"]):
            raise Conflict("Every source must have an explicitly trusted origin and exact HTTPS host")
        return selected

    def dry_run(self, policy_id):
        row = self.store.require("intake_policy", policy_id)
        policy = self._current(row)
        snapshot = self._snapshot(policy)
        result, seen = [], set()
        for item in sorted(snapshot["items"], key=lambda r: str(r["id"])):
            if item["id"] in seen:
                raise Conflict("Requirement snapshot contains duplicate identities")
            seen.add(item["id"])
            try:
                preview = self.intake.preview(item["id"])
                original = self.store.require("intake_preview", preview["preview_id"])
                source_ids = self._eligible(policy, original)
                key = identity([policy.identity, original["brief"]["id"]])
                reason = "already_reserved" if self.store.get("intake_dispatch", key) else "eligible"
                result.append({"requirement_id": item["id"], "preview_id": preview["preview_id"],
                    "brief_sha256": original["brief_hash"], "source_ids": source_ids,
                    "dispatch_key": key, "decision": reason})
            except (ValueError, KeyError):
                result.append({"requirement_id": item["id"], "decision": "outside_policy_or_unavailable"})
        report = {"id": identifier(), "policy_id": policy_id, "policy_sha256": row["policy_sha256"],
                  "version": row["version"], "identity": policy.identity, "created_at": now(),
                  "snapshot_sha256": identity(snapshot["items"]), "snapshot_at": snapshot["last_synced_at"],
                  "items": result, "production_mutations": False}
        self.store.put("intake_dry_run", report)
        return report

    def authorize(self, policy_id, report_id, enabled):
        reviewed = self.store.require("intake_policy", policy_id)
        snapshot = self._snapshot(self._current(reviewed)) if enabled else None
        with self.store.connection() as db:
            store = TransactionStore(self.store, db)
            row = store.require("intake_policy", policy_id)
            if not enabled:
                row.update(enabled=False, revoked_at=now())
                for dispatch in store.list("intake_dispatch"):
                    if dispatch["policy_id"] == policy_id and dispatch["status"] in {"queued", "deferred"}:
                        dispatch.update(status="cancelled_before_dispatch", cancelled_at=now())
                        store.put("intake_dispatch", dispatch)
                return store.put("intake_policy", row)
            report = store.require("intake_dry_run", report_id)
            policy = self._current(row)
            if (row["version"] != reviewed["version"] or row["policy_sha256"] != reviewed["policy_sha256"]
                    or report["policy_id"] != policy_id or report["version"] != row["version"]
                    or report["policy_sha256"] != row["policy_sha256"]
                    or report["identity"] != policy.identity
                    or report["snapshot_sha256"] != identity(snapshot["items"])
                    or datetime.fromisoformat(report["created_at"]) <
                    datetime.now(timezone.utc) - timedelta(minutes=15)):
                raise Conflict("A fresh dry run of this exact policy must be reviewed")
            row.update(enabled=True, authorization_id=identifier(), dry_run_id=report_id, enabled_at=now())
            return store.put("intake_policy", row)

    def _capacity(self, store, policy, *, exclude=None):
        rows = [r for r in store.list("intake_dispatch")
                if r["identity"] == policy.identity and r["id"] != exclude
                and r["status"] not in {"cancelled_before_dispatch"}]
        today = datetime.now(timezone.utc).date().isoformat()
        reserved = {"calls": 0, "images": 0, "kie": 0}
        queued, active = 0, 0
        unresolved_tasks = {r.get("task_id") for kind in ("operation", "production_decision", "imagegen_request")
                            for r in store.list(kind) if r.get("status") in
                            {"running", "unknown", "invalid_response", "pending", "claimed"}}
        for row in rows:
            task = store.get("task", row.get("task_id", ""))
            settled = task and task["status"] in TERMINAL and task["id"] not in unresolved_tasks
            if row["created_at"][:10] == today or not settled:
                amount = dict(row["reserve"])
                if task:
                    for key, limit, used in (("calls", "max_paid_calls", "paid_calls"),
                                             ("images", "max_image_calls", "image_calls"),
                                             ("kie", "max_kie_credits", "video_credits_committed")):
                        if task.get(limit) is None and not settled:
                            raise AdmissionCapacity("An existing admitted task has an unbounded allowance")
                        amount[key] = max(amount[key], task.get(used, 0), task.get(limit) or 0)
                for key in reserved:
                    reserved[key] += amount[key]
            queued += row["status"] in {"queued", "deferred", "dispatching", "unknown"}
            active += bool(row["status"] in {"dispatching", "unknown"} or task and task["status"] not in TERMINAL)
        limits = {"calls": policy.daily_calls, "images": policy.daily_images, "kie": policy.daily_kie_credits}
        amount = {"calls": policy.task_calls, "images": policy.task_images, "kie": policy.task_kie_credits}
        if queued >= policy.max_queue or active >= policy.max_active:
            raise AdmissionCapacity("Admission concurrency or queue limit reached")
        if any(reserved[key] + amount[key] > limits[key] for key in reserved):
            raise AdmissionCapacity("Daily admission allowance is reserved, including unresolved prior-day work")
        return amount

    def scan(self, policy_id):
        row = self.store.require("intake_policy", policy_id)
        policy = self._current(row)
        if not row["enabled"]:
            return {"queued": 0, "reason": "disabled"}
        report = self.dry_run(policy_id)
        count = 0
        with self.store.connection() as db:
            store = TransactionStore(self.store, db)
            current = store.require("intake_policy", policy_id)
            if not current["enabled"] or current["authorization_id"] != row["authorization_id"]:
                raise Conflict("Policy was revoked or replaced during scan")
            for item in report["items"]:
                if item["decision"] != "eligible" or store.get("intake_dispatch", item["dispatch_key"]):
                    continue
                try:
                    reserve = self._capacity(store, policy)
                except Conflict:
                    break
                store.put("intake_dispatch", {"id": item["dispatch_key"], **item,
                    "policy_id": policy_id, "version": row["version"], "identity": policy.identity,
                    "authorization_id": row["authorization_id"], "status": "queued", "created_at": now(),
                    "priority": 0, "defer_until": None, "reserve": reserve, "task_id": None})
                count += 1
            store.put("intake_checkpoint", {"id": policy_id, "created_at": now(), "report_id": report["id"],
                "snapshot_sha256": report["snapshot_sha256"], "complete": True, "queued": count})
        return {"queued": count, "report_id": report["id"]}

    def defer(self, dispatch_id, *, until=None, priority=0):
        if not -100 <= priority <= 100:
            raise ValueError("Priority is outside the supported range")
        if until is not None and datetime.fromisoformat(until).tzinfo is None:
            raise ValueError("Deferral requires an explicit timezone")
        def mutate(row):
            if row["status"] not in {"queued", "deferred"}:
                raise Conflict("Only undispatched work can be deferred")
            row.update(status="deferred" if until else "queued", defer_until=until, priority=priority)
        return self.store.update("intake_dispatch", dispatch_id, mutate)

    def process_one(self):
        candidates = sorted((r for r in self.store.list("intake_dispatch")
            if r["status"] in {"queued", "deferred"}
            and (not r.get("defer_until") or datetime.fromisoformat(r["defer_until"]) <=
                 datetime.now(timezone.utc))), key=lambda r: (-r["priority"], r["created_at"], r["id"]))
        for original in candidates:
            row = self.store.require("intake_policy", original["policy_id"])
            if not row["enabled"]:
                continue
            try:
                policy = self._current(row)
                self._snapshot(policy)
                if (row["authorization_id"] != original["authorization_id"]
                        or row["version"] != original["version"]):
                    raise Conflict("Queued authorization is stale")
                preview = self.store.require("intake_preview", original["preview_id"])
                if datetime.fromisoformat(preview["expires_at"]) < datetime.now(timezone.utc):
                    refreshed = self.intake.preview(preview["brief"]["id"])
                    replacement = self.store.require("intake_preview", refreshed["preview_id"])
                    if (replacement["brief_hash"] != original["brief_sha256"]
                            or self._eligible(policy, replacement) != original["source_ids"]):
                        raise Conflict("Deferred brief or source changed")
                    preview = replacement
                if self._eligible(policy, preview) != original["source_ids"]:
                    raise Conflict("Source selection changed")
                request = IntakeCreate(preview_id=preview["id"], title=preview["brief"]["title"] or preview["brief"]["code"],
                    source_ids=original["source_ids"], confirm_sources=True, target_locales=policy.locales,
                    idempotency_key=original["id"], max_paid_calls=policy.task_calls,
                    max_image_calls=policy.task_images, max_video_credits=policy.task_kie_credits,
                    max_kie_credits=policy.task_kie_credits, max_visual_steps=policy.max_visual_steps,
                    execution_profile=policy.execution_profile, notes=policy.notes)
                with self.store.connection() as db:
                    store = TransactionStore(self.store, db)
                    latest = store.require("intake_dispatch", original["id"])
                    if latest["status"] not in {"queued", "deferred"}:
                        continue
                    self._capacity(store, policy, exclude=original["id"])
                    latest.update(status="dispatching", started_at=now(), payload_sha256=digest(request.model_dump()))
                    store.put("intake_dispatch", latest)

                def guard(store, current_preview, body):
                    current = store.require("intake_policy", row["id"])
                    if (not current["enabled"] or current["authorization_id"] != original["authorization_id"]
                            or current["version"] != original["version"]):
                        raise Conflict("Policy revoked before task creation")
                    self._current(current)
                    self._capacity(store, policy, exclude=original["id"])
                    if self._eligible(policy, current_preview) != original["source_ids"]:
                        raise Conflict("Actual download selection changed")

                task = self.intake.create(request, before_fetch=guard, before_commit=guard,
                                          download_hosts=policy.trusted_hosts)
                self.store.update("intake_dispatch", original["id"],
                    lambda r: r.update(status="succeeded", task_id=task["id"], finished_at=now()))
            except AdmissionCapacity:
                self.store.update("intake_dispatch", original["id"], lambda r: r.update(
                    status="deferred", defer_until=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()))
                continue
            except Exception:
                receipt = self.store.get("intake_receipt", original["id"])
                self.store.update("intake_dispatch", original["id"],
                    lambda r: r.update(status="succeeded" if receipt else "blocked",
                        task_id=receipt["task_id"] if receipt else None,
                        error=None if receipt else "Admission blocked; inspect original preview, policy and receipt"))
            return True
        return False

    def recover(self):
        for row in self.store.list("intake_dispatch"):
            if row["status"] != "dispatching":
                continue
            receipt = self.store.get("intake_receipt", row["id"])
            self.store.update("intake_dispatch", row["id"],
                lambda r: r.update(status="succeeded" if receipt else "unknown",
                    task_id=receipt["task_id"] if receipt else None))

    def poll(self):
        """Called by the project's manager; absence of enabled policy is a no-op."""
        for row in self.store.list("intake_policy"):
            if not row["enabled"]:
                continue
            checkpoint = self.store.get("intake_checkpoint", row["id"])
            if checkpoint and datetime.fromisoformat(checkpoint["created_at"]) > (
                    datetime.now(timezone.utc) - timedelta(minutes=1)):
                continue
            try:
                self.scan(row["id"])
            except (ValueError, KeyError):
                self.store.put("intake_checkpoint", {"id": row["id"], "created_at": now(),
                    "complete": False, "reason": "Policy, identity or complete snapshot needs review"})
