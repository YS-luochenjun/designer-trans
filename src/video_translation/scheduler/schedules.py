from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from video_translation.application.tasks import Conflict
from video_translation.domain.models import ScheduleCreate
from video_translation.persistence.store import identifier, now


class Scheduler:
    kinds = {"project_patrol", "requirements_check", "agent_message"}

    def __init__(self, service, fireplay):
        self.service, self.store, self.fireplay = service, service.store, fireplay
        self.lock = threading.Lock()
        self.manager = None

    def create(self, request: ScheduleCreate):
        if request.trigger == "once" and request.run_at <= datetime.now(timezone.utc):
            raise ValueError("一次性计划必须晚于当前时间")
        value = request.model_dump(mode="json")
        value.update(
            id=identifier(),
            created_at=now(),
            last_run_at=None,
            last_result=None,
            next_run_at=self.next_time(value),
            running=False,
        )
        return self.store.put("schedule", value)

    @staticmethod
    def next_time(schedule):
        if not schedule["enabled"]:
            return None
        if schedule["trigger"] == "once":
            return schedule["run_at"]
        return (datetime.now(timezone.utc) + timedelta(seconds=schedule["interval_seconds"])).isoformat()

    def patch(self, schedule_id, changes):
        with self.lock:
            return self._patch_locked(schedule_id, changes)

    def _patch_locked(self, schedule_id, changes):
        current = self.store.require("schedule", schedule_id)
        fields = set(ScheduleCreate.model_fields)
        if not changes or set(changes) - fields:
            raise ValueError("包含无效的计划字段")
        request = ScheduleCreate.model_validate({**{k: current[k] for k in fields if k in current}, **changes})
        if request.trigger == "once" and request.enabled and request.run_at <= datetime.now(timezone.utc):
            raise ValueError("启用的一次性计划必须晚于当前时间")

        def mutate(schedule):
            if schedule.get("running"):
                raise Conflict("计划正在触发，请稍后编辑")
            if any(schedule.get(k, "" if k == "prompt" else False) != getattr(request, k)
                   for k in ("kind", "prompt", "refresh_requirements")):
                schedule.pop("discussion_root_event_id", None)
            schedule.update(request.model_dump(mode="json"))
            schedule["next_run_at"] = self.next_time(schedule)

        return self.store.update("schedule", schedule_id, mutate)

    def list(self, retired=False):
        return [self.result(r) for r in self.store.list("schedule")
                if (r["kind"] not in self.kinds) == retired]

    def delete(self, schedule_id):
        with self.lock:
            current = self.store.require("schedule", schedule_id)
            if current.get("running"):
                raise Conflict("计划正在触发，请稍后删除")
            self.store.delete("schedule", schedule_id)

    def trigger(self, schedule_id, manual=False):
        with self.lock:
            schedule = self.store.require("schedule", schedule_id)
            if schedule["kind"] not in self.kinds:
                raise Conflict("旧版制作或同步计划已停用，请新建项目管理计划")
            if schedule.get("running"):
                raise Conflict("同一计划上一轮正在触发")
            previous_event = self.store.get("manager_event", schedule.get("last_event_id", ""))
            if previous_event and previous_event["status"] in {"queued", "running"}:
                raise Conflict("上一轮管理事件尚未处理，已阻止重复排队")
            if not manual and (not schedule["enabled"] or not schedule.get("next_run_at")
                    or datetime.fromisoformat(schedule["next_run_at"]) > datetime.now(timezone.utc)):
                return None
            self.store.update("schedule", schedule_id, lambda s: s.update(running=True))
        run = {
            "id": identifier(),
            "schedule_id": schedule_id,
            "created_at": now(),
            "triggered_at": now(),
            "status": "running",
            "source": "manual" if manual else "timer",
            "task_id": None,
            "error": None,
        }
        self.store.put("schedule_run", run)
        try:
            if self.manager is None:
                raise Conflict("项目管理器尚未接入")
            payload = {"schedule_id": schedule_id, "schedule_name": schedule["name"]}
            if schedule["kind"] == "agent_message":
                if not schedule.get("prompt", "").strip():
                    raise ValueError("自定义轮询消息不能为空")
                event_kind = "scheduled_message"
                payload.update(text=schedule["prompt"],
                               refresh_requirements=schedule.get("refresh_requirements", False),
                               root_event_id=schedule.get("discussion_root_event_id") or run["id"])
            else:
                event_kind = "patrol" if schedule["kind"] == "project_patrol" else "requirements_check"
            event = self.manager.enqueue(event_kind, payload, run["id"])
            run["event_id"] = event["id"]
            run["management_status"] = "queued"
            run["status"] = "queued"
        except Exception as exc:
            run.update(status="failed", error=str(exc)[:1000])
        finally:
            self.store.put("schedule_run", run)

            def finish(current):
                current.update(running=False, last_run_at=run["triggered_at"], last_result=run["status"])
                if run.get("event_id"):
                    current["last_event_id"] = run["event_id"]
                    if schedule["kind"] == "agent_message":
                        current["discussion_root_event_id"] = payload["root_event_id"]
                if current["trigger"] == "once":
                    current["enabled"] = False
                current["next_run_at"] = self.next_time(current)

            self.store.update("schedule", schedule_id, finish)
        return run

    def result(self, record):
        """Queue admission is distinct from the durable management event's outcome."""
        key = record.get("event_id") or record.get("last_event_id")
        event = self.store.get("manager_event", key) if key else None
        if not event:
            return record
        if "schedule_id" in record:
            return {**record, "status": event["status"], "management_status": event["status"],
                    "error": event.get("error"), "reply": event.get("reply"),
                    "message": event.get("payload", {}).get("text")}
        return {**record, "last_result": event["status"], "last_reply": event.get("reply"),
                "last_error": event.get("error")}

    def tick(self):
        clock = datetime.now(timezone.utc)
        for schedule in self.store.list("schedule"):
            if (
                schedule["kind"] in self.kinds
                and schedule["enabled"]
                and schedule.get("next_run_at")
                and datetime.fromisoformat(schedule["next_run_at"]) <= clock
            ):
                try:
                    self.trigger(schedule["id"])
                except Conflict as exc:
                    self.store.put(
                        "schedule_run",
                        {
                            "id": identifier(),
                            "schedule_id": schedule["id"],
                            "created_at": now(),
                            "triggered_at": now(),
                            "status": "skipped",
                            "task_id": None,
                            "error": str(exc),
                            "source": "timer",
                        },
                    )

                    def advance(current):
                        if current["trigger"] == "once":
                            current["enabled"] = False
                        current["next_run_at"] = self.next_time(current)

                    self.store.update("schedule", schedule["id"], advance)

    def recover(self):
        for schedule in self.store.list("schedule"):
            if schedule["kind"] not in self.kinds:
                if not schedule.get("retired_at"):
                    self.store.update("schedule", schedule["id"], lambda s: s.update(
                        enabled=False, running=False, next_run_at=None, last_result="retired",
                        retired_at=now(), retired_reason="旧版制作或同步计划已移除；历史与已创建任务保留",
                    ))
                continue
            if schedule.get("running"):
                # Queue admission may have succeeded before a crash. Never replay it blindly.
                self.store.update(
                    "schedule",
                    schedule["id"],
                    lambda s: s.update(
                        running=False, enabled=False, next_run_at=None, last_result="interrupted"
                    ),
                )
        for run in self.store.list("schedule_run"):
            if run["status"] == "running":
                self.store.update(
                    "schedule_run",
                    run["id"],
                    lambda r: r.update(status="interrupted", error="服务中断，核对管理事件记录后再启用计划"),
                )
