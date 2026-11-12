from __future__ import annotations

import logging
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from video_translation.application.tasks import Conflict

from video_translation.integrations.agents import UnknownExternalResult
from video_translation.integrations.providers.kie import PendingExternalResult
from video_translation.persistence.store import WorkerLock, now
from video_translation.workflow.graph import ready_nodes, task_status

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, service, executor, scheduler):
        self.service, self.store, self.executor, self.scheduler = service, service.store, executor, scheduler
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(
            max_workers=service.settings.max_concurrent_tasks, thread_name_prefix="translation"
        )
        self.node_pool = ThreadPoolExecutor(
            max_workers=service.settings.max_concurrent_tasks * service.settings.max_concurrent_nodes,
            thread_name_prefix="work")
        self.route_pool = ThreadPoolExecutor(max_workers=service.settings.max_concurrent_tasks,
                                             thread_name_prefix="task-route")
        self.route_futures = {}
        self.worker_lock = WorkerLock(service.settings.runtime / "worker.lock")
        self.dispatch_lock = threading.RLock()
        self.futures = {}
        self.thread = None
        self.timer_thread = None
        self.running = False
        self.last_error = None

    def start(self):
        with self.dispatch_lock:
            self.worker_lock.acquire()
            self.service.recover()
            self.scheduler.recover()
            self.running = True
            self.thread = threading.Thread(target=self.loop, daemon=True, name="task-queue")
            self.timer_thread = threading.Thread(target=self.timer_loop, daemon=True, name="schedule-timer")
            self.thread.start()
            self.timer_thread.start()

    @contextmanager
    def guard(self, task_id):
        """Fence dispatch and reject unfinished writers, including cancelled tasks."""
        with self.dispatch_lock:
            future = self.futures.get(task_id)
            route_future = self.route_futures.get(task_id)
            if (future is not None and not future.done()
                    or route_future is not None and not route_future.done()):
                raise Conflict("任务执行尚未停止，请先取消并等待当前节点退出后再删除")
            external_guard = None
            if not self.running:
                external_guard = WorkerLock(self.service.settings.runtime / "worker.lock")
                try:
                    external_guard.acquire()
                except RuntimeError as exc:
                    raise Conflict("另一个后台执行器仍运行，无法确认任务已停稳") from exc
            try:
                yield
            finally:
                if external_guard is not None:
                    external_guard.release()

    def stop(self):
        self.stop_event.set()
        for thread in (self.thread, self.timer_thread):
            if thread:
                thread.join(timeout=3)
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.node_pool.shutdown(wait=True, cancel_futures=True)
        self.route_pool.shutdown(wait=True, cancel_futures=True)
        with self.dispatch_lock:
            self.worker_lock.release()
            self.running = False

    def loop(self):
        while not self.stop_event.is_set():
            try:
                with self.dispatch_lock:
                    self._dispatch()
            except Exception:
                self.last_error = "后台队列检查失败"
                logger.exception("Queue iteration failed")
            self.stop_event.wait(0.5)

    def _dispatch(self):
        # Caller holds dispatch_lock through snapshot, submit, and registration.
        self._dispatch_route()
        for task_id, future in list(self.futures.items()):
            if future.done():
                del self.futures[task_id]
                if not future.cancelled() and future.exception():
                    self.last_error = "任务执行器异常，已保留节点现场"
                    logger.error("Task runner failed for %s: %s", task_id, type(future.exception()).__name__)
        capacity = self.service.settings.max_concurrent_tasks - len(self.futures)
        if capacity <= 0 or self.stop_event.is_set():
            return
        deleting = {r["id"] for r in self.store.list("deletion_receipt")}
        from .native_imagegen import pending_request
        for task in reversed(self.store.list("task")):
            if task["id"] not in self.futures and task["status"] == "waiting_user":
                from video_translation.application.observation_route_handoff import queue_legacy_observation
                try:
                    if queue_legacy_observation(self.service, task["id"]):
                        task = self.store.require("task", task["id"])
                except (Conflict, OSError, KeyError):
                    pass  # Ordinary user questions and unsettled evidence retain their current owner.
            if (task["id"] not in self.futures and task["id"] not in deleting
                    and (self.service.collaboration.pending(task["id"], interaction="proposal")
                         or task["status"] != "cancelled" and ready_nodes(task)
                         or pending_request(self.service, task))):
                self.futures[task["id"]] = self.pool.submit(self.run_task, task["id"])
                capacity -= 1
                if capacity == 0:
                    break

    def timer_loop(self):
        while not self.stop_event.is_set():
            try:
                # Serialize scheduled dispatch with task controls and queue dispatch.
                with self.dispatch_lock:
                    self.scheduler.tick()
            except Exception:
                self.last_error = "定时触发检查失败"
                logger.exception("Schedule iteration failed")
            self.stop_event.wait(2)

    def _dispatch_route(self):
        for task_id, future in list(self.route_futures.items()):
            if future.done():
                del self.route_futures[task_id]
                if not future.cancelled() and future.exception():
                    logger.error("Task Route failed for %s: %s", task_id, type(future.exception()).__name__)
        from video_translation.application.task_supervision import pending_event, reconcile_work_reports
        for task in reversed(self.store.list("task")):
            if len(self.route_futures) >= self.service.settings.max_concurrent_tasks:
                break
            if task["id"] in self.route_futures or self.store.get("deletion_receipt", task["id"]):
                continue
            future = self.futures.get(task["id"])
            if future is None or future.done():
                from video_translation.application.retained_clean_checks import reconcile
                if reconcile(self.service, task["id"]):
                    continue
            reconcile_work_reports(self.service, task["id"])
            message = self.service.collaboration.pending(task["id"], interaction="route")
            event = (not task.get("control_paused") and task["status"] not in {"cancelled", "completed"}
                     and pending_event(self.service, task["id"]))
            if message or event:
                self.route_futures[task["id"]] = self.route_pool.submit(self.run_route, task["id"])

    def run_route(self, task_id):
        message = self.service.collaboration.pending(task_id, interaction="route")
        if message:
            from .coordination import discuss
            discuss(self.executor, self.store.require("task", task_id), message)
        else:
            from .task_supervisor import process_event
            process_event(self.executor, task_id)

    def run_task(self, task_id):
        active = set()
        while not self.stop_event.is_set() or active:
            if active:
                done, active = wait(active, timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
            if self.stop_event.is_set():
                continue
            # Message acceptance, pause and next-node claim share one local ordering point.
            with self.store.lock:
                task = self.store.require("task", task_id)
                from .native_imagegen import pending_request, execute_request
                tool_request = pending_request(self.service, task)
                message = (self.service.collaboration.pending(task_id, interaction="proposal")
                           if not active else None)
                if not message and not tool_request:
                    if task["status"] in {"cancelled", "completed"} and not active:
                        return
                    ready = ready_nodes(task)
                    if not ready:
                        if active:
                            continue
                        self.store.update("task", task_id, lambda t: t.update(status=task_status(t)))
                        return
                    slots = self.service.settings.max_concurrent_nodes - len(active)
                    for item in ready[:max(0, slots)]:
                        node = dict(item, attempt=item["attempt"] + 1, status="running",
                                    started_at=now(), finished_at=None)
                        self.service.update_node(
                            task_id, node["id"],
                            **{k: node[k] for k in ("attempt", "status", "started_at", "finished_at")},
                            error=None)
                        active.add(self.node_pool.submit(self.run_node, task_id, task, node))
            if message:
                from .coordination import discuss
                discuss(self.executor, task, message)
                continue
            if tool_request:
                try:
                    execute_request(self.executor, task, tool_request)
                except Conflict as exc:
                    # A concurrent pause/cancel or claim leaves the durable request untouched.
                    reason = str(exc)[:1500]
                    if tool_request.get("dispatch_blocked_reason") != reason:
                        self.store.update("imagegen_request", tool_request["id"], lambda r: r.update(
                            dispatch_blocked_reason=reason) if r["status"] == "pending" else None)
                    return
                continue

    def run_node(self, task_id, task, node):
        try:
            from video_translation.application.task_supervision import live_directions
            from video_translation.domain.execution import identity
            original_directions = identity(live_directions(self.service, task_id, node["id"]))
            if node.get("external_pending"):
                from .remote import reconcile_kie_operations
                reconcile_kie_operations(self.service, task_id, node_id=node["id"])
            output = self.executor.execute(task, node)
            with self.store.lock:
                self.finish_node(task_id, task, node, output, original_directions)
        except Exception as exc:
            pending = isinstance(exc, PendingExternalResult) and bool(getattr(exc, "task_id", None))
            status = "waiting_external" if pending else "unknown" if isinstance(exc, UnknownExternalResult) else "failed"
            message = str(exc)[:1500]
            self.service.update_node(
                task_id, node["id"], status=status,
                error=None if pending else message, finished_at=None if pending else now(),
                external_pending=pending,
                next_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat() if pending else None,
            )
            from video_translation.application.task_supervision import report_work
            if not pending and status != "unknown":
                report_work(self.service, task_id, node["id"])
            # Continue independent locale/source branches instead of failing the entire task.

    def finish_node(self, task_id, task, node, output, original_directions):
        from video_translation.application.task_supervision import live_directions, report_work, unread_directions
        from video_translation.domain.execution import identity
        if output.get("_paused"):
            self.service.update_node(task_id, node["id"], status="pending",
                                     error=None, finished_at=None)
            return
        if original_directions != identity(live_directions(self.service, task_id, node["id"])):
            pending = unread_directions(self.service, task_id, node["id"])
            if pending:
                self.service.update_node(
                    task_id, node["id"], status="failed", output=output,
                    stage_readiness="needs_repair", finished_at=now(),
                    error="制作期间收到新方向；保留原输出，Route 核对后继续")
                report_work(self.service, task_id, node["id"], trigger="direction_after_work")
                return
        if output.get("_revision"):
            self.revise(task_id, node, output)
            return
        if output.get("_tool_wait"):
            self.service.update_node(task_id, node["id"], status="waiting_external",
                                     tool_request_id=output["_tool_wait"], output=output,
                                     external_pending=False, next_poll_at=None, error=None)
            return
        from .stage_quality import require_node_output
        try:
            require_node_output(task, node, output)
        except Exception:
            # Retain the actual preview/error without approving its use
            # by downstream production nodes.
            self.service.update_node(
                task_id, node["id"], output=output, stage_readiness="needs_repair")
            raise
        artifacts = [
            item["id"] for item in self.store.list("artifact") if item["node_id"] == node["id"]
        ]
        status = (
            "waiting_user"
            if output.get("_wait")
            else "skipped"
            if output.get("_skip")
            else "succeeded"
        )
        self.service.update_node(
            task_id,
            node["id"],
            output=output,
            status=status,
            skip_reason=output.get("_skip"),
            artifact_ids=artifacts,
            finished_at=now(),
            external_pending=False,
            next_poll_at=None,
            stage_readiness="ready",
            stale_upstream=None,
        )
        if not output.get("_wait"):
            report_work(self.service, task_id, node["id"])

    def revise(self, task_id, node, output):
        if self.store.require("task", task_id).get("visual_route") == "agent_adaptive":
            self.service.update_node(task_id, node["id"], output=output, status="failed",
                                     error="审核发现需修正项，已交任务 Route 安排返工", finished_at=now())
            from video_translation.application.task_supervision import report_work
            report_work(self.service, task_id, node["id"], trigger="review_needs_revision")
            return
        def mutate(task):
            branch = node["asset_id"] + ":" + node["locale"]
            count = task["revisions"].get(branch, 0)
            review = next(n for n in task["nodes"] if n["id"] == node["id"])
            review.update(output=output, finished_at=now())
            if task["status"] == "cancelled":
                return
            from video_translation.domain.limits import allows_revision
            if not allows_revision(task, count):
                review.update(status="failed", error="审核未通过且已达到局部修正次数上限；候选已保留")
            else:
                task["revisions"][branch] = count + 1
                task.setdefault("revision_feedback", {})[branch] = output.get("defects", [])
                for item in task["nodes"]:
                    if item["asset_id"] == node["asset_id"] and item["locale"] == node["locale"]:
                        item.setdefault("history", []).append(
                            {
                                "attempt": item["attempt"],
                                "status": item["status"],
                                "output": item["output"],
                                "artifact_ids": item["artifact_ids"],
                            }
                        )
                        item.update(
                            status="pending", output={}, error=None, finished_at=None, artifact_ids=[]
                        )
            task["status"] = task_status(task)

        self.store.update("task", task_id, mutate)
