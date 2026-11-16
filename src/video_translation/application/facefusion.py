"""A fixed external job, independent of translation nodes and Route messages."""
from __future__ import annotations

import json
import subprocess

from .tasks import Conflict, asset_kind
from ..domain.facefusion import FaceFusionProfile
from ..integrations import facefusion as adapter
from ..persistence.store import identifier, now
from ..persistence.transaction import TransactionStore
from ..workflow.graph import task_status


def checked_asset(service, asset_id, kind):
    asset = service.store.require("asset", asset_id)
    path = service.checked_path(asset)
    if asset_kind(asset) != kind or service.hash_file(path) != asset["sha256"]:
        raise Conflict("FaceFusion 素材类型或原文件身份已变化")
    return asset, path


def preview(service, asset_id, profile):
    asset, video = checked_asset(service, asset_id, "video")
    state = adapter.installation()
    if not state["ready"]:
        raise Conflict(state["reason"])
    from ..execution.media import MediaTools
    metadata = MediaTools(service.settings.ffmpeg, service.settings.ffprobe).probe(video)
    frame_count = metadata.get("frame_count") or round(
        metadata["duration_ms"] * float(metadata.get("fps", 25)) / 1000)
    if profile.reference_frame_number >= frame_count:
        raise ValueError("参考帧超出视频范围")
    key = identifier()
    directory = service.settings.runtime / "uploads" / "face-previews" / key
    directory.mkdir(parents=True)
    try:
        result = adapter.inspect_target(state, video, directory, profile.model_dump())
    except (subprocess.SubprocessError, OSError, ValueError):
        raise Conflict("FaceFusion 未完成目标脸检测；本轮日志已保留，请检查本地环境或选择另一参考帧") from None
    files = [directory / "frame.png", *[
        directory / f"face-{face['position']}.png" for face in result["faces"]]]
    if any(not path.is_file() for path in files):
        raise Conflict("FaceFusion 检测回执缺少实际图片")
    checked_asset(service, asset_id, "video")
    if adapter.installation()["source_sha256"] != state["source_sha256"]:
        raise Conflict("检测期间 FaceFusion 版本已变化，请重新检测")
    record = {"id": key, "asset_id": asset_id, "source_sha256": asset["sha256"],
              "executor_sha256": state["source_sha256"], "created_at": now(),
              "frame_number": profile.reference_frame_number, "order": profile.face_selector_order,
              "faces": result["faces"],
              "files": [{"path": str(path), "sha256": service.hash_file(path)} for path in files]}
    service.store.put("face_preview", record)
    return public_preview(record)


def public_preview(record):
    base = f"/api/facefusion/previews/{record['id']}/files/"
    return {key: record[key] for key in (
        "id", "asset_id", "source_sha256", "frame_number", "order", "created_at",
    )} | {"frame_url": base + "0", "faces": [
        {**face, "content_url": base + str(index + 1)} for index, face in enumerate(record["faces"])]}


def require_preview(service, task, profile, executor_sha256):
    record = service.store.require("face_preview", profile.target_preview_id)
    asset, _ = checked_asset(service, task["asset_ids"][0], "video")
    if (record["asset_id"] != asset["id"] or record["source_sha256"] != asset["sha256"]
            or record["executor_sha256"] != executor_sha256
            or record["frame_number"] != profile.reference_frame_number
            or record["order"] != profile.face_selector_order
            or profile.reference_face_position not in {face["position"] for face in record["faces"]}):
        raise Conflict("目标脸选择与当前视频、参考帧、排序或 FaceFusion 版本不一致，请重新检测")
    for file in record["files"]:
        if service.hash_file(service.checked_path(file)) != file["sha256"]:
            raise Conflict("目标脸参考图片已变化，请重新检测")
    return record


def initial_execution(service, task):
    profile = FaceFusionProfile.model_validate(task.get("facefusion") or {})
    state = adapter.installation()
    if profile.target_preview_id:
        require_preview(service, task, profile, state["source_sha256"])
    configured = profile.face_selector_mode != "reference" or bool(profile.target_preview_id)
    return {"id": identifier(), "executor": "facefusion",
            "state": "pending" if configured else "configuration_required",
            "source_sha256": state["source_sha256"],
            "created_at": now(), "quality_passed": False,
            "artifact_id": None, "error": None}


def configure(service, task_id, request):
    with service.store.lock:
        task = service.store.require("task", task_id)
        if task.get("task_kind") != "face_swap":
            raise Conflict("只能配置 FaceFusion 任务")
        profile = request.profile
        old = task.get("external_execution") or {}
        prior = service.store.get("face_configuration", request.request_id)
        body = request.model_dump(mode="json")
        if prior:
            if prior["task_id"] != task_id or prior["request"] != body:
                raise Conflict("配置请求身份已用于另一份参数")
            return detail(service, task)
        if task["status"] in {"cancelled", "completed"}:
            raise Conflict("只能配置未结束的 FaceFusion 任务")
        if old.get("id") != request.expected_run_id:
            raise Conflict("FaceFusion 当前运行已变化，请刷新后核对人物和参数")
        if old.get("state") in {"pending", "running", "unknown"}:
            raise Conflict("原 FaceFusion 运行尚未结清，请保留原身份核对结果")
        if service.store.get("deletion_receipt", task_id):
            raise Conflict("任务正在删除")
        candidate = {**task, "facefusion": profile.model_dump()}
        execution = initial_execution(service, candidate)
        if execution["state"] != "pending":
            raise Conflict("请先检测参考帧并选择目标脸")
        def mutate(row):
            row.setdefault("external_history", []).append(old)
            row.update(facefusion=profile.model_dump(), external_execution=execution)
            row["status"] = task_status(row)
        with service.store.connection() as db:
            store = TransactionStore(service.store, db)
            store.update("task", task_id, mutate)
            store.put("face_configuration", {
                "id": request.request_id, "task_id": task_id, "request": body,
                "run_id": execution["id"], "created_at": now(),
            })
    return detail(service, service.store.require("task", task_id))


def detail(service, task):
    assets = [service.public_asset(service.store.require("asset", key)) for key in task["asset_ids"]]
    preview_id = (task.get("facefusion") or {}).get("target_preview_id")
    target = service.store.get("face_preview", preview_id) if preview_id else None
    execution = task.get("external_execution") or {
        "state": "configuration_required", "executor": "facefusion", "quality_passed": False,
    }
    # Old graph records are preserved in storage but never projected as executable Work.
    return {**task, **service.summary(task), "nodes": [], "assets": assets,
            "route_report": None, "task_route_events": [], "conversations": [],
            "agent_traces": [], "production_sessions": [], "production_decisions": [],
            "imagegen_requests": [], "questions": [],
            "external_execution": execution,
            "design_workspace": {"task_kind": "face_swap", "execution": "external_tool",
                                 "quality_passed": execution.get("quality_passed", False),
                                 "target_preview": public_preview(target) if target else None,
                                 "display_steps": ["确认目标人物与参数", "FaceFusion 处理视频", "查看结果与下载"]},
            "artifacts": [service.public_asset(row) for row in service.store.list("artifact")
                          if row.get("task_id") == task["id"]],
            "deliveries": [row for row in service.store.list("delivery") if row.get("task_id") == task["id"]]}


def update_execution(service, task_id, run_id, **changes):
    def mutate(task):
        if task.get("external_execution", {}).get("id") != run_id:
            raise Conflict("FaceFusion 原运行身份已变化")
        task["external_execution"].update(changes)
        task["status"] = task_status(task)
    return service.store.update("task", task_id, mutate)


def execute(service, task_id, stop_event):
    run_id, process = None, None
    try:
        with service.store.lock:
            task = service.store.require("task", task_id)
            run = task.get("external_execution") or {}
            if task.get("control_paused") or task["status"] == "cancelled" or run.get("state") != "pending":
                return
            run_id = run["id"]
            from ..execution.resource_budget import guard_storage
            guard_storage(service, task)
            state = adapter.installation()
            if not state["ready"] or state["source_sha256"] != run["source_sha256"]:
                raise Conflict("FaceFusion 环境缺失或版本已变化，请核对后更新任务配置")
            video_asset, video = checked_asset(service, task["asset_ids"][0], "video")
            face_asset, face = checked_asset(service, task["asset_ids"][1], "face_source")
            profile = FaceFusionProfile.model_validate(task["facefusion"])
            if profile.face_selector_mode == "reference":
                require_preview(service, task, profile, state["source_sha256"])
            directory = service.settings.runtime / "tasks" / task_id / "facefusion" / run_id
            directory.mkdir(parents=True)
            (directory / "facefusion.ini").write_text("", encoding="utf-8")
            output = directory / "result.mp4"
            args = adapter.arguments(profile.model_dump(), video, face, output, directory)
            receipt = {"id": run_id, "task_id": task_id, "executor": "facefusion",
                       "source_sha256": video_asset["sha256"], "face_sha256": face_asset["sha256"],
                       "executor_sha256": state["source_sha256"], "profile": profile.model_dump(),
                       "arguments": args, "created_at": now(), "quality_passed": False}
            service.store.put("external_run", {**receipt, "state": "running"})
            update_execution(service, task_id, run_id, state="running", started_at=now())
        with (directory / "execution.log").open("wb") as log:
            process = adapter.start(state, args, log)
            service.store.update("external_run", run_id, lambda row: row.update(process_id=process.pid))
            while process.poll() is None:
                if stop_event.wait(1):
                    raise InterruptedError("工作台停止，原 FaceFusion 输出保留供核对")
                current = service.store.require("task", task_id)
                if current["status"] == "cancelled":
                    raise InterruptedError("任务已取消，原 FaceFusion 输出保留")
                guard_storage(service, current)
            if process.returncode != 0:
                raise Conflict(f"FaceFusion 执行失败（退出码 {process.returncode}）；原日志和输出已保留")
        from ..execution.media import MediaTools
        tools = MediaTools(service.settings.ffmpeg, service.settings.ffprobe)
        source_info, info = tools.probe(video), tools.probe(output)
        if (abs(info["duration_ms"] - source_info["duration_ms"]) > 250
                or (info["width"], info["height"]) != (source_info["width"], source_info["height"])
                or source_info["has_audio"] and not info["has_audio"]):
            raise Conflict("FaceFusion 输出时长、尺寸或必需音轨不匹配，候选保留")
        tools.validate(output)
        checked_asset(service, task["asset_ids"][0], "video")
        checked_asset(service, task["asset_ids"][1], "face_source")
        if adapter.installation()["source_sha256"] != state["source_sha256"]:
            raise Conflict("FaceFusion 执行期间版本变化，原输出保留供核对")
        artifact = service.artifact(task_id, {"id": "facefusion-" + run_id, "locale": None},
                                    output, "video", "FaceFusion 换脸候选.mp4")
        receipt.update(state="succeeded", artifact_id=artifact["id"], sha256=artifact["sha256"],
                       media=info, finished_at=now())
        (directory / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        service.store.put("external_run", receipt)
        update_execution(service, task_id, run_id, state="succeeded", artifact_id=artifact["id"],
                         sha256=artifact["sha256"], finished_at=now(), error=None)
    except Exception as exc:
        if process is not None and process.poll() is None:
            adapter.terminate(process)
        if run_id:
            status = "unknown" if isinstance(exc, InterruptedError) else "failed"
            error = str(exc)[:1500]
            if service.store.get("external_run", run_id):
                service.store.update("external_run", run_id, lambda row: row.update(
                    state=status, error=error, finished_at=now()))
            update_execution(service, task_id, run_id, state=status, error=error)


def recover(service, task):
    execution = task.get("external_execution") or {}
    if execution.get("state") == "running":
        error = "服务中断，原 FaceFusion 运行待核对，未自动重复执行"
        with service.store.connection() as db:
            store = TransactionStore(service.store, db)
            task["external_execution"].update(state="unknown", error=error)
            task.update(status=task_status(task), updated_at=now())
            store.put("task", task)
            if store.get("external_run", execution["id"]):
                store.update("external_run", execution["id"], lambda row: row.update(
                    state="unknown", error=error))
    elif task["status"] not in {"cancelled", "completed"}:
        service.store.update("task", task["id"], lambda row: row.update(status=task_status(row)))


def approve(service, task_id, sha256):
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        task = store.require("task", task_id)
        run = task.get("external_execution") or {}
        if task.get("task_kind") != "face_swap" or task["status"] == "cancelled":
            raise Conflict("不是可验收的 FaceFusion 任务")
        if run.get("state") not in {"succeeded", "approved"} or sha256 != run.get("sha256"):
            raise Conflict("请完整观看并核对当前换脸视频")
        artifact = store.require("artifact", run["artifact_id"])
        if service.hash_file(service.checked_path(artifact)) != sha256:
            raise Conflict("换脸候选内容已变化，旧验收不能放行")
        if run["state"] != "approved":
            run.update(state="approved", quality_passed=True, approved_at=now(), approved_sha256=sha256)
            task.update(status="completed")
            store.put("task", task)
            store.put("delivery", {
                "id": identifier(), "task_id": task_id, "asset_id": task["asset_ids"][0],
                "artifact_id": artifact["id"], "locale": "", "title": task["title"],
                "quality_status": "human_approved", "sha256": sha256, "status": "accepted",
                "content_url": artifact["content_url"] + "?download=true", "created_at": now(),
                "notes": "FaceFusion 外接工具结果，人工完整视听验收",
            })
    return detail(service, service.store.require("task", task_id))
