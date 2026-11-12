"""Durable optional image production over explicit OpenAI or existing KIE config."""
from __future__ import annotations

from video_translation.domain.limits import allows_new_call, effective_limit
from video_translation.execution.credit_budget import check_kie_budget
from video_translation.integrations.providers.image_pricing import image_quote

import hashlib
import io
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image

from video_translation.application.tasks import Conflict
from video_translation.integrations.agents.errors import CapabilityUnavailable, UnknownExternalResult
from video_translation.integrations.providers.image_edit import (
    ImageEditConfig, ImageEditor, KIE_IMAGE_MODEL, _image_info, _read_image, build_kie_image_input,
)
from video_translation.persistence.store import WorkerLock, now


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def image_snapshot(kie_config=None, image_config=None):
    """Credential-free task snapshot. An explicit OpenAI enable never falls back."""
    config = image_config if image_config is not None else ImageEditConfig.from_env()
    if config.enabled or kie_config is None or not kie_config.enabled_model_calls:
        return config.snapshot()
    return {
        "provider": "kie", "protocol": "kie_image_to_image", "model": KIE_IMAGE_MODEL,
        "endpoint": "https://api.kie.ai/api/v1/jobs/createTask",
        "resolution": "1K", "aspect_ratio": "auto",
        "capabilities": {"reference": True, "mask": False, "alpha": True,
                         "text_only_generation": False, "input_formats": ["PNG", "JPEG"],
                         "output_format": "PNG", "max_references": 1, "exact_mask_preservation": False},
        "contract_status": "official_documented", "alpha_status": "documented_1k_only",
        "provider_verified": False, "timeout_seconds": kie_config.timeout,
        "max_input_bytes": min(20 * 1024 * 1024, kie_config.max_upload_bytes),
        "max_output_bytes": min(20 * 1024 * 1024, kie_config.max_download_bytes),
        "max_pixels": 16_777_216, "max_prompt_chars": 20000,
    }


class ImageProduction:
    def __init__(self, executor, *, image_config=None, image_editor=None):
        self.e, self.service, self.store = executor, executor.service, executor.store
        self.config = image_config if image_config is not None else ImageEditConfig.from_env()
        self.editor = image_editor if image_editor is not None else ImageEditor(self.config)

    def snapshot(self):
        return image_snapshot(getattr(self.e, "kie_config", None), self.config)

    def status(self, task=None):
        snapshot = self.snapshot()
        if snapshot["provider"] == "kie":
            config = self.e.kie_config
            ready = bool(config.enabled_model_calls and config.api_key.strip())
            reason = "configured_not_live_verified" if ready else "missing_api_key"
        else:
            state = self.config.status()
            ready, reason = state["ready"], state["reason"]
        if task is not None:
            if task.get("image_snapshot") != snapshot:
                ready, reason = False, "task_snapshot_mismatch"
            elif not allows_new_call(task, 'max_image_calls'):
                ready, reason = False, "image_budget_disabled"
            elif (snapshot["provider"] == "kie"
                  and effective_limit(task, "max_kie_credits", None) is not None
                  and image_quote(snapshot) is None):
                ready, reason = False, "image_quote_unavailable"
        return {**snapshot, "ready": ready, "reason": reason}

    def ready(self, task):
        return self.status(task)["ready"]

    def _directory(self, task):
        if not isinstance(task.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task["id"]):
            raise Conflict("图片任务身份无效")
        runtime = Path(self.service.settings.runtime).resolve()
        directory = runtime / "tasks" / task["id"]
        if not directory.resolve().is_relative_to(runtime):
            raise Conflict("图片缓存目录越出任务运行目录")
        directory = directory / "image-cache"
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(runtime / "tasks" / task["id"]):
            raise Conflict("图片缓存目录无法安全核验")
        return directory

    def _limits(self, snapshot):
        return ImageEditConfig(max_input_bytes=snapshot["max_input_bytes"],
                               max_output_bytes=snapshot["max_output_bytes"],
                               max_pixels=snapshot["max_pixels"], max_prompt_chars=snapshot["max_prompt_chars"])

    def _reserve(self, task, node, identity, snapshot, identity_fields):
        """One transaction reserves one image slot per immutable request identity."""
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM records WHERE kind='task' AND id=?", (task["id"],)).fetchone()
            if row is None:
                raise Conflict("图片任务不存在")
            current = json.loads(row[0])
            if current.get("status") == "cancelled":
                raise Conflict("任务已取消")
            if current.get("image_snapshot") != snapshot:
                raise Conflict("图片模型配置与任务快照不一致")
            old = db.execute("SELECT data FROM records WHERE kind='production_image' AND id=?",
                             (identity,)).fetchone()
            if old:
                return json.loads(old[0]), False
            if snapshot["provider"] == "kie":
                quote = image_quote(snapshot)
                check_kie_budget(db, current, quote["estimated_credits"] if quote else None)
            if not allows_new_call(current, 'max_image_calls', current.get('image_calls', 0)):
                raise Conflict("已达到任务图片调用上限；复用已有结果不扣新次数")
            if not allows_new_call(current, 'max_paid_calls', current.get('paid_calls', 0)):
                raise Conflict("已达到任务外部调用总上限；未预留图片调用")
            current["image_calls"] = current.get("image_calls", 0) + 1
            current["updated_at"] = now()
            record = {"id": identity, "task_id": task["id"], "node_id": node["id"],
                      "provider": snapshot["provider"], "model": snapshot["model"],
                      "status": "reserved", "job_started": False, "external_id": None,
                      "created_at": now(), "input": identity_fields, "output": None}
            db.execute("UPDATE records SET data=? WHERE kind='task' AND id=?",
                       (json.dumps(current, ensure_ascii=False), task["id"]))
            db.execute("INSERT INTO records VALUES(?,?,?,?)",
                       ("production_image", identity, json.dumps(record, ensure_ascii=False), record["created_at"]))
        return record, True

    def _finish(self, identity, output, snapshot, transparent):
        if not output.is_file() or output.stat().st_size > snapshot["max_output_bytes"]:
            raise UnknownExternalResult("图片结果缺失或超出大小限制；保留原请求，禁止重投")
        data = output.read_bytes()
        try:
            fmt, size, alpha, pixels_transparent = _image_info(data, snapshot["max_pixels"])
            if fmt != "PNG" or transparent and not (alpha and pixels_transparent):
                raise ValueError()
        except ValueError:
            raise UnknownExternalResult("图片结果未通过PNG/透明通道校验；禁止重新付费提交") from None
        result = {"path": str(output), "sha256": hashlib.sha256(data).hexdigest(),
                  "width": size[0], "height": size[1], "format": fmt, "alpha": alpha,
                  "transparent": bool(pixels_transparent)}
        self.store.update("production_image", identity,
                          lambda record: record.update(status="succeeded", output=result, finished_at=now()))
        return result

    def _materialize(self, record, target, snapshot, transparent):
        output = record.get("output")
        if not isinstance(output, dict):
            raise Conflict("图片成功回执缺少输出信息；不能重做收费操作")
        cached = Path(output["path"])
        directory = self._directory({"id": record["task_id"]})
        if (cached.is_symlink() or not cached.resolve().is_relative_to(directory.resolve())
                or not cached.is_file() or cached.stat().st_size > snapshot["max_output_bytes"]):
            raise Conflict("图片缓存缺失或不属于任务；不能重做收费操作")
        data = cached.read_bytes()
        if hashlib.sha256(data).hexdigest() != output["sha256"]:
            raise Conflict("图片缓存校验和变化；不能重做收费操作")
        try:
            fmt, _, alpha, pixels_transparent = _image_info(data, snapshot["max_pixels"])
            if fmt != "PNG" or transparent and not (alpha and pixels_transparent):
                raise ValueError()
        except ValueError:
            raise Conflict("图片缓存格式或透明通道变化") from None
        if target.exists():
            if (target.is_file() and target.stat().st_size == len(data)
                    and hashlib.sha256(target.read_bytes()).hexdigest() == output["sha256"]):
                return target
            raise Conflict("图片目标已存在且内容不同，拒绝覆盖")
        with target.open("xb") as stream:
            stream.write(data)
        return target

    def edit(self, task, node, source: Path, target: Path, prompt: str, *, transparent=False) -> Path:
        state = self.status(task)
        if not state["ready"]:
            raise CapabilityUnavailable(f"图片编辑不可用：{state['reason']}")
        snapshot = self.snapshot()
        if type(transparent) is not bool or transparent and not snapshot["capabilities"]["alpha"]:
            raise CapabilityUnavailable("当前图片模型未启用透明输出")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > snapshot["max_prompt_chars"]:
            raise ValueError("图片编辑提示词为空或超出长度限制")
        directory = self._directory(task)
        task_directory = directory.parent.resolve()
        if (not isinstance(target, Path) or target.suffix.lower() != ".png" or target.is_symlink()
                or not target.resolve().is_relative_to(task_directory) or not target.parent.is_dir()):
            raise Conflict("图片输出必须是当前任务目录下已校验的PNG路径")
        data, (source_format, _, _, _) = _read_image(source, self._limits(snapshot))
        identity_fields = {"task": task["id"], "source_sha256": hashlib.sha256(data).hexdigest(),
                           "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                           "snapshot": snapshot, "transparent": transparent}
        if node.get("stage_reset_id"):
            identity_fields["stage_reset_id"] = node["stage_reset_id"]
        identity = "image-" + _digest(identity_fields)
        lock_path = directory / f"{identity}.lock"
        if lock_path.is_symlink():
            raise Conflict("图片请求锁路径无法安全核验")
        lock = WorkerLock(lock_path)
        try:
            lock.acquire()
        except RuntimeError:
            raise Conflict("相同图片请求正在处理，不能并发重复提交") from None
        try:
            existing = self.store.get("production_image", identity)
            if target.exists() and (not existing or existing["status"] != "succeeded"):
                raise Conflict("图片目标已存在且没有匹配成功回执，拒绝收费重做")
            record, fresh = self._reserve(task, node, identity, snapshot, identity_fields)
            if record["status"] == "succeeded":
                return self._materialize(record, target, snapshot, transparent)
            if not fresh and snapshot["provider"] != "kie":
                raise UnknownExternalResult("已有图片编辑操作没有成功回执；结果不明时禁止重投")
            source_copy = directory / (identity + (".source.png" if source_format == "PNG" else ".source.jpg"))
            if source_copy.exists():
                if (source_copy.stat().st_size != len(data)
                        or hashlib.sha256(source_copy.read_bytes()).hexdigest() != identity_fields["source_sha256"]):
                    raise Conflict("图片参考缓存变化，禁止使用不同输入继续请求")
            else:
                with source_copy.open("xb") as stream:
                    stream.write(data)
            output = directory / (identity + ".png")
            self.store.update("production_image", identity, lambda current: current.update(status="running"))
            try:
                if snapshot["provider"] == "kie":
                    origin = next((n for n in task.get("nodes", []) if n["id"] == record["node_id"]), None)
                    if origin is None:
                        if record["node_id"] != node["id"]:
                            raise Conflict("图片原始节点不可核验，不能以新节点重投")
                        origin = node
                    self.e.kie(task, origin, "image_edit", identity_fields,
                               lambda client, key, event: self._kie(
                                   client, key, event, identity, source_copy, output, prompt, snapshot, transparent))
                else:
                    def call():
                        self.editor.edit(source_copy, output, prompt, transparent=transparent)
                        return self._finish(identity, output, snapshot, transparent)
                    self.e.paid(task, node, "openai-compatible-image-edit", "image_edit", call)
            except BaseException:
                previous = self.store.require("production_image", identity)
                if previous["status"] != "succeeded":
                    self.store.update("production_image", identity,
                                      lambda current: current.update(status="unknown", error="图片结果待核验；禁止重投"))
                raise
            return self._materialize(self.store.require("production_image", identity), target, snapshot, transparent)
        finally:
            lock.release()

    def _kie(self, client, key, event, identity, source, output, prompt, snapshot, transparent):
        current = self.store.require("production_image", identity)
        if current.get("job_started") and not client._path(key).is_file():
            raise UnknownExternalResult("KIE图片请求回执缺失，不能重新提交")
        # Preserve the original upload URL and frozen payload when resuming a known job.
        url = client._asset_url(source, key)
        inputs = build_kie_image_input(url, prompt, resolution="1K", transparent=transparent)
        self.store.update("production_image", identity, lambda row: row.update(job_started=True))

        def observe(update):
            event(update)
            if update.get("task_id"):
                self.store.update("production_image", identity,
                                  lambda row: row.update(external_id=update["task_id"]))

        result = client.job(KIE_IMAGE_MODEL, inputs, key, observe)
        urls = result.get("resultUrls")
        if not isinstance(urls, list) or len(urls) != 1 or not isinstance(urls[0], str):
            raise UnknownExternalResult("KIE图片返回数量不符合合同；保留原任务，不能重投")
        client.config = replace(client.config, max_download_bytes=min(client.config.max_download_bytes,
                                                                     snapshot["max_output_bytes"]))
        raw = output.with_suffix(".download")
        try:
            client.download(urls[0], raw)
            if not raw.is_file() or raw.stat().st_size > snapshot["max_output_bytes"]:
                raise ValueError()
            data = raw.read_bytes()
            fmt, _, alpha, pixels_transparent = _image_info(data, snapshot["max_pixels"])
            if transparent and not (alpha and pixels_transparent):
                raise ValueError()
            if fmt == "PNG":
                converted = data
            else:
                buffer = io.BytesIO()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    image.save(buffer, format="PNG")
                converted = buffer.getvalue()
            if len(converted) > snapshot["max_output_bytes"]:
                raise ValueError()
            with NamedTemporaryFile(dir=output.parent, suffix=".png", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(converted)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
            return self._finish(identity, output, snapshot, transparent)
        except (ValueError, OSError):
            raise UnknownExternalResult("KIE图片输出未通过格式/大小/透明通道校验；保留原任务，不能重投") from None
        finally:
            raw.unlink(missing_ok=True)
