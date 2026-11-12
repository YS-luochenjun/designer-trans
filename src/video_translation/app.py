from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from video_translation.application.collaboration import CleanContinuation, MessageCreate

from video_translation.agent_runtime.resources import ResourceCatalog
from video_translation.application.deletion import TaskDeletionService
from video_translation.application.tasks import Conflict, TaskService
from video_translation.config import Settings
from video_translation.domain.models import Answer, ExternalLimits, ScheduleCreate, TaskCreate
from video_translation.execution.nodes import NodeExecutor
from video_translation.execution.worker import Worker
from video_translation.integrations.fireplay.bridge import FireplayBridge
from video_translation.persistence.store import Store, identifier
from video_translation.scheduler.schedules import Scheduler


class TaskControl(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paused: bool


def create_app(settings: Settings | None = None, executor_factory=None):
    settings = settings or Settings.load()
    store = Store(settings.runtime / "database/project.sqlite3")
    service = TaskService(settings, store)
    fireplay = FireplayBridge(settings, store)
    scheduler = Scheduler(service, fireplay)
    executor = executor_factory(service) if executor_factory else NodeExecutor(service)
    worker = Worker(service, executor, scheduler)
    from video_translation.application.management import ProjectManager
    manager = ProjectManager(service, fireplay, worker.dispatch_lock)
    manager.route_recovery.guard = worker.guard
    scheduler.manager = manager
    deletion = TaskDeletionService(settings, store, quiescence=worker.guard)

    @asynccontextmanager
    async def lifespan(app):
        if settings.worker_enabled:
            worker.start()
            try:
                manager.start()
            except Exception:
                worker.stop()
                raise
        try:
            fireplay.start()
            yield
        finally:
            if settings.worker_enabled:
                manager.stop()
                worker.stop()
            fireplay.close()

    app = FastAPI(title="视频翻译 v2", version="0.1.0", lifespan=lifespan)
    app.state.service, app.state.worker, app.state.scheduler = service, worker, scheduler
    app.state.manager = manager
    from video_translation.interfaces.management_api import management_routes
    app.include_router(management_routes(manager, worker))
    from video_translation.interfaces.requirements_api import requirement_routes
    app.include_router(requirement_routes(service, fireplay, manager.intake))
    from video_translation.interfaces.preview_api import preview_routes
    app.include_router(preview_routes(service, worker))
    from video_translation.interfaces.clean_resume_api import clean_resume_routes
    app.include_router(clean_resume_routes(service, worker))
    from video_translation.interfaces.route_recovery_api import route_recovery_routes
    app.include_router(route_recovery_routes(service, worker, manager))
    from video_translation.interfaces.visual_recovery_api import visual_recovery_routes
    app.include_router(visual_recovery_routes(service, worker, manager))
    from video_translation.interfaces.source_observation_api import source_observation_routes
    app.include_router(source_observation_routes(service, worker))
    from video_translation.interfaces.execution_api import execution_routes
    app.include_router(execution_routes(service))
    from video_translation.interfaces.advanced_execution_api import advanced_routes
    app.include_router(advanced_routes(service, worker, manager))
    from video_translation.interfaces.storage_api import storage_routes
    app.include_router(storage_routes(service, worker))
    from video_translation.integrations.fireplay.mcp import FireplayMCPError

    @app.exception_handler(FireplayMCPError)
    async def fireplay_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=503)

    allowed_hosts = {"127.0.0.1", "localhost", "::1", "testserver", *settings.lan_hosts}
    allowed_networks = tuple(ip_network(value) for value in settings.lan_networks)
    if settings.lan_hosts and not allowed_networks:
        raise ValueError("LAN access requires explicit allowed_networks")

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # Validate the peer independently of Host; forwarding headers cannot grant LAN access.
        if allowed_networks:
            try:
                peer = ip_address(request.client.host) if request.client else None
            except ValueError:
                peer = None
            if peer is None or not (peer.is_loopback or any(peer in network for network in allowed_networks)):
                return JSONResponse({"detail": "仅支持本机和已配置的局域网访问"}, status_code=403,
                                    media_type="application/json; charset=utf-8")
        host = request.url.hostname
        if host not in allowed_hosts:
            return JSONResponse({"detail": "访问地址不在工作台允许列表中"}, status_code=403,
                                media_type="application/json; charset=utf-8")
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "拒绝跨站写入请求"}, status_code=403,
                                    media_type="application/json; charset=utf-8")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    same_origin = (parsed.scheme == request.url.scheme
                                   and parsed.netloc.lower() == request.url.netloc.lower())
                except ValueError:
                    same_origin = False
                if not same_origin:
                    return JSONResponse({"detail": "拒绝跨站写入请求"}, status_code=403,
                                        media_type="application/json; charset=utf-8")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "记录或文件不存在"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    from video_translation.application.tasks import ArtifactRetired

    @app.exception_handler(ArtifactRetired)
    async def retired_artifact(request, exc):
        return JSONResponse({"detail": str(exc), "retirement": exc.receipt}, status_code=410)

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/health")
    def health():
        import os
        from video_translation.domain.execution import EXECUTION_VERSION
        return {
            "status": "ok" if not worker.last_error else "degraded",
            "version": "0.1.0",
            "execution_version": EXECUTION_VERSION,
            "worker_running": worker.running,
            "error": worker.last_error,
            "process_id": os.getpid(),
            "manager_running": manager.running,
            "manager_error": manager.last_error,
            "manager_protocol_version": "3",
            "maintenance_protocol_version": "1",
            "work_handoff_protocol_version": "1",
            "observation_clock_version": "2",
            "observation_continuation_protocol_version": "1",
            "task_route_protocol_version": "1",
            "complete_candidate_protocol_version": "1",
            "cleanup_stage_gate_protocol_version": "1",
            "node_continuation_protocol_version": "1",
            "clean_execution_authorization_protocol_version": "1",
            "clean_stage_reset_protocol_version": "1",
            "task_supervision_protocol_version": "1",
            "work_report_recovery_protocol_version": "1",
            "clean_stop_scope_protocol_version": "1",
            "testing_budget_policy_protocol_version": "1",
            "clean_check_recovery_protocol_version": "1",
            "parallel_work_protocol_version": "1",
            "max_concurrent_nodes": settings.max_concurrent_nodes,
            "clean_work_protocol_version": "2",
            "clean_route_recovery_protocol_version": "2",
            "materialization_audio_protocol_version": "1",
            "materialization_recovery_lifecycle_version": "3",
            "task_deletion_policy_version": "2",
            "fireplay_renewal_protocol_version": "1",
            "work_tool_profile": "native_codex_tools",
            "imagegen_executor": "project_codex_worker",
            "work_autonomy": os.environ.get("VT_CODEX_WORK_AUTONOMY", "bounded"),
            "route_work_recovery": "task_delegated",
            "features": ["node_conversations", "brief_confirmation", "fireplay_login", "task_deletion", "local_preview", "clean_reconsideration", "clean_scope_review", "stage_quality_gates", "node_output_contracts", "scoped_node_retry", "profile_bound_image_capability", "scoped_imagegen_dispatch", "external_tool_request_visibility", "retained_native_image_recovery", "tolerant_agent_output", "bounded_agent_format_repair", "scoped_agent_replacement", "reviewed_reference_frame_coverage", "scoped_clean_route_resume", "completion_font_fallback", "browser_color_preview", "local_clean_candidate", "clean_route_work", "versioned_clean_reassessment", "codex_event_result_recovery", "scoped_missing_agent_replacement"],
        }

    @app.get("/api/overview")
    def overview():
        tasks = store.list("task")
        snapshot = fireplay.projects()
        return {
            "total": len(tasks),
            "running": sum(t["status"] in {"running", "queued"} for t in tasks),
            "waiting": sum(t["status"] in {"waiting_user", "blocked", "interrupted"} for t in tasks),
            "completed": sum(t["status"] == "completed" for t in tasks),
            "partial": sum(t["status"] == "partial_completed" for t in tasks),
            "failed": sum(t["status"] == "blocked" for t in tasks),
            "deliveries": len(store.list("delivery")),
            "fireplay": {k: snapshot[k] for k in ("configured", "last_synced_at", "complete", "total")},
        }

    @app.post("/api/assets", status_code=201)
    async def upload(file: UploadFile):
        temporary = settings.runtime / "uploads" / identifier()
        size = 0
        try:
            with temporary.open("wb") as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "视频超过上传大小限制")
                    target.write(chunk)
            if not size:
                raise ValueError("上传文件为空")
            return service.register_asset(temporary, file.filename or "video.mp4")
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()

    @app.get("/api/assets/{asset_id}/content")
    def asset_content(asset_id: str):
        record = store.require("asset", asset_id)
        return FileResponse(
            service.checked_path(record), filename=record["name"], content_disposition_type="inline"
        )

    @app.get("/api/settings/selection")
    def model_selection():
        from video_translation.application.selections import public_selection
        return public_selection(settings.root)

    @app.patch("/api/settings/selection")
    def save_model_selection(body: dict):
        from video_translation.application.selections import update_selection
        return update_selection(settings.root, body)

    @app.get("/api/tasks")
    def tasks(q: str = "", status: str | None = None, limit: int = 50, offset: int = 0):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("分页参数无效")
        records = [
            t
            for t in store.list("task")
            if (not status or t["status"] == status) and q.casefold() in (t["title"] + t["id"]).casefold()
        ]
        return {
            "items": [service.summary(t) for t in records[offset : offset + limit]],
            "total": len(records),
        }

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskCreate):
        # Serialize asset validation with deletion of the last asset reference.
        with store.lock:
            return service.create(body)

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return service.detail(task_id)

    @app.post("/api/tasks/{task_id}/nodes/{node_id}/messages", status_code=202)
    def send_message(task_id: str, node_id: str, body: MessageCreate):
        with worker.dispatch_lock:
            return service.collaboration.submit(task_id, node_id, body)

    @app.post("/api/tasks/{task_id}/messages/{message_id}/apply")
    def apply_message(task_id: str, message_id: str):
        with worker.dispatch_lock:
            return service.collaboration.apply(task_id, message_id)

    @app.post("/api/tasks/{task_id}/messages/{message_id}/continue-clean")
    def continue_clean_message(task_id: str, message_id: str, body: CleanContinuation):
        with worker.guard(task_id):
            return service.collaboration.continue_clean(task_id, message_id, body)

    @app.post("/api/tasks/{task_id}/control")
    def task_control(task_id: str, body: TaskControl):
        with worker.dispatch_lock:
            return service.collaboration.pause(task_id, body.paused)

    @app.get("/api/tasks/{task_id}/deletion-preview")
    def deletion_preview(task_id: str):
        return deletion.preview(task_id)

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str, fingerprint: str):
        return deletion.execute(task_id, fingerprint)

    def ensure_not_deleting(task_id):
        if store.get("deletion_receipt", task_id):
            raise Conflict("任务删除尚未收口，请使用原删除指纹重试")

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        with worker.dispatch_lock:
            ensure_not_deleting(task_id)
            return service.cancel(task_id)

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        with worker.dispatch_lock:
            ensure_not_deleting(task_id)
            return service.retry(task_id)

    @app.put("/api/tasks/{task_id}/external-limits")
    def external_limits(task_id: str, body: ExternalLimits):
        with worker.dispatch_lock:
            ensure_not_deleting(task_id)
            return service.set_external_limits(task_id, body)

    @app.post("/api/tasks/{task_id}/questions/{question_id}/answer")
    def answer(task_id: str, question_id: str, body: Answer):
        with worker.dispatch_lock:
            ensure_not_deleting(task_id)
            return service.answer(task_id, question_id, body.answer, body.feedback)

    @app.get("/api/artifacts/{artifact_id}/content")
    def artifact_content(artifact_id: str, download: bool = False):
        record = store.require("artifact", artifact_id)
        path = service.checked_path(record)
        headers = {}
        if not download and path.suffix.lower() == ".mp4":
            from video_translation.execution.browser_media import browser_video
            from video_translation.execution.media import MediaTools
            original = path
            path = browser_video(
                MediaTools(settings.ffmpeg, settings.ffprobe),
                path, settings.runtime / "cache/browser-media",
            )
            if path != original:
                headers = {"X-Media-Representation": "browser-preview",
                           "Cache-Control": "no-cache"}
        return FileResponse(
            path,
            filename=record["name"],
            media_type="video/mp4" if path.suffix.lower() == ".mp4" else None,
            content_disposition_type="attachment" if download else "inline",
            headers=headers,
        )

    @app.get("/api/deliveries")
    def deliveries():
        records = store.list("delivery")
        return {"items": records, "total": len(records)}

    @app.get("/api/resources/{kind}")
    def resources(kind: str):
        records = ResourceCatalog(settings.root).list(kind)
        return {"items": records, "total": len(records)}

    @app.get("/api/settings")
    def configuration():
        from video_translation.integrations.agents.adapter import AgentConfig
        from video_translation.integrations.agents.status import configuration_status
        from video_translation.application.selections import public_selection
        agent = AgentConfig.from_env()
        status = configuration_status(settings.root)
        models = public_selection(settings.root)["resolved"]["models"]
        from video_translation.integrations.providers.separation import SeparationConfig
        separation = SeparationConfig.from_env(settings.root).status()
        kie = executor.kie_config
        kie_ready = bool(kie and kie.api_key and kie.enabled_model_calls)
        asr, tts = executor.transcriber.config, executor.speech.config
        agent_ready = (
            agent.mode == "http"
            and bool(agent.base_url and agent.model)
            or agent.mode == "command"
            and bool(agent.command)
        )
        agent_ready = agent_ready and status["configured"]
        capabilities = [
            {
                "id": "media",
                "label": "本地媒体处理",
                "configured": settings.media_available(),
                "detail": "FFmpeg / FFprobe；实色字层、音轨对齐与编码",
            },
            {
                "id": "agent",
                "label": "后台 Agent",
                "configured": bool(agent_ready),
                "detail": f"{status['engine']}；项目独立CLI/SDK。凭据配置不代表真实推理已验收；临时会话不自动同步桌面",
            },
            {
                "id": "asr",
                "label": "人声识别",
                "configured": kie_ready if kie else bool(asr.base_url and asr.model),
                "detail": f"KIE · {models['transcription']['model']}；音频理解转写，时间戳为估计，需完整核对" if kie else "VT_ASR_BASE_URL / MODEL / API_KEY",
            },
            {
                "id": "tts",
                "label": "目标语配音",
                "configured": kie_ready if kie else bool(tts.base_url and tts.model),
                "detail": f"KIE · {models['tts']['model']}；音色由模型配置选择，模型结果尚未实片验收" if kie else "VT_TTS_BASE_URL / MODEL / API_KEY / VOICE",
            },
            {
                "id": "video-edit",
                "label": "KIE 视频编辑",
                "configured": kie_ready,
                "detail": f"{models['video']['model']}；2–10秒整片、额度显式设置，长片自动分镜尚未实现",
            },
            {
                "id": "separation",
                "label": "背景音乐与人声分离",
                "configured": separation["ready"],
                "detail": f"本地 CPU · UVR Inst HQ3；{separation['reason']}。对白窗口使用分离伴奏，其他时段保留原声；需完整听验",
            },
            {
                "id": "fireplay",
                "label": "Fireplay 需求 MCP",
                "configured": fireplay.configured,
                "detail": "项目直连 AI Designer MCP；需独立业务身份。需求读取与审核后回传分开记录，不操作广告",
            },
        ]
        return {
            "capabilities": capabilities,
            "runtime": {
                "max_concurrent_tasks": settings.max_concurrent_tasks,
                "ffmpeg": bool(shutil.which(settings.ffmpeg)),
                "ffprobe": bool(shutil.which(settings.ffprobe)),
                "agent_mode": agent.mode,
            },
            "resources": {
                k: len(ResourceCatalog(settings.root).list(k)) for k in ("prompts", "knowledge", "skills")
            },
        }

    @app.get("/api/schedules")
    def schedules(retired: bool = False):
        records = scheduler.list(retired=retired)
        return {"items": records, "total": len(records)}

    @app.post("/api/schedules", status_code=201)
    def create_schedule(body: ScheduleCreate):
        with worker.dispatch_lock:
            return scheduler.create(body)

    @app.patch("/api/schedules/{schedule_id}")
    def patch_schedule(schedule_id: str, body: dict):
        with worker.dispatch_lock:
            return scheduler.patch(schedule_id, body)

    @app.delete("/api/schedules/{schedule_id}", status_code=204)
    def delete_schedule(schedule_id: str):
        scheduler.delete(schedule_id)
        return Response(status_code=204)

    @app.post("/api/schedules/{schedule_id}/run")
    def run_schedule(schedule_id: str):
        with worker.dispatch_lock:
            return scheduler.trigger(schedule_id, manual=True)

    @app.get("/api/schedules/{schedule_id}/runs")
    def schedule_runs(schedule_id: str):
        store.require("schedule", schedule_id)
        records = [scheduler.result(r) for r in store.list("schedule_run") if r["schedule_id"] == schedule_id]
        return {"items": records, "total": len(records)}

    @app.get("/api/fireplay/projects")
    def projects():
        return fireplay.projects()

    distribution = settings.root / "ui/dist"
    if distribution.exists():
        app.mount("/", StaticFiles(directory=distribution, html=True), name="workbench")
    else:

        @app.get("/")
        def missing_ui():
            return JSONResponse(
                {"detail": "前端尚未构建，请执行README中的构建命令", "docs": "/docs"}, status_code=503
            )

    return app
