"""Fixed business nodes. Models propose data; this module controls execution."""

from __future__ import annotations
from contextlib import nullcontext

from video_translation.domain.limits import allows_new_call, allows_usage

import shutil
import json
import hashlib
import os
from pathlib import Path

from video_translation.agent_runtime.resources import ResourceCatalog
from video_translation.application.tasks import Conflict, TaskService
from video_translation.domain.models import Inventory, ProductionPlan, ReviewReport, TranslationPackage
from video_translation.execution.media import MediaTools
from video_translation.integrations.agents import (
    AgentAdapter,
    AgentConfig,
    CapabilityUnavailable,
    UnknownExternalResult,
)
from video_translation.integrations.providers import ProviderConfig, SpeechSynthesizer, Transcriber
from video_translation.persistence.store import identifier, now


class NodeExecutor:
    def __init__(self, service: TaskService, agent=None, transcriber=None, speech=None, media=None, separator=None):
        self.service, self.store = service, service.store
        settings = service.settings
        self.media = media or MediaTools(settings.ffmpeg, settings.ffprobe)
        self.injected_agent = agent is not None
        self.agent = agent or AgentAdapter(AgentConfig.from_env())
        self.transcriber = transcriber or Transcriber(ProviderConfig.from_env("asr"))
        self.speech = speech or SpeechSynthesizer(ProviderConfig.from_env("tts"))
        self.kie_config = None
        if os.environ.get("VT_MEDIA_PROVIDER") == "kie" and transcriber is None and speech is None:
            from video_translation.integrations.providers.kie import KieConfig
            self.kie_config = KieConfig.from_env()
        from video_translation.integrations.providers.separation import SeparationConfig, Separator
        self.separation_config = SeparationConfig.from_env(settings.root)
        self.separator = separator or Separator(self.separation_config)
        self.injected_separator = separator is not None
        self.catalog = ResourceCatalog(settings.root)

    def agent_for(self, task):
        if self.injected_agent or not isinstance(self.agent, AgentAdapter):
            return self.agent
        return AgentAdapter(AgentConfig.from_env({**os.environ, "VT_AGENT_ENGINE": task.get("agent_engine", "custom")}))

    def kie_for(self, task):
        from video_translation.integrations.providers.model_catalog import apply_selection
        return apply_selection(self.kie_config, task["model_snapshot"]) if self.kie_config and task.get("model_snapshot") else self.kie_config

    def kie(self, task, node, kind, identity, action):
        from video_translation.execution.remote import run_kie
        return run_kie(self.service, self.kie_for(task), task, node, kind, identity, action)

    @staticmethod
    def preserve_background(task, inventory=None, plan=None):
        if task.get("audio_strategy"):
            return task["audio_strategy"] == "preserve_background"
        # Historical tasks retain their original explicit music requirement.
        return bool(inventory and inventory["has_music"] and (inventory["preserve_music"] or (plan or {}).get("preserve_music")))

    def check_separation(self, task):
        if self.injected_separator:
            return
        if task.get("separation_snapshot") and task["separation_snapshot"] != self.separation_config.snapshot():
            raise CapabilityUnavailable("分离配置与任务快照不一致，请恢复原配置或创建新任务")
        if not self.separation_config.status()["ready"]:
            raise CapabilityUnavailable("保留背景音需要本地分离组件；请运行 scripts/install_separation.py 并通过 doctor，未发送付费请求")

    def preflight(self, task, has_audio):
        if has_audio and self.preserve_background(task):
            self.check_separation(task)
        if not self.injected_agent and task.get("agent_engine") in {"codex", "harness"}:
            from video_translation.integrations.agents.status import ensure_engine_ready
            ensure_engine_ready(self.service.settings.root, task["agent_engine"])
        cfg = getattr(self.agent_for(task), "config", None)
        if cfg and (cfg.mode == "unconfigured" or cfg.mode == "command" and not cfg.command or cfg.mode == "http" and (not cfg.base_url or not cfg.model)):
            raise CapabilityUnavailable("制作前检查：后台Agent尚未配置，未发送付费请求")
        if self.kie_config:
            if not self.kie_config.api_key or not self.kie_config.enabled_model_calls:
                raise CapabilityUnavailable("制作前检查：KIE密钥或本项目模型调用开关尚未配置，未发送付费请求")
        elif has_audio:
            for provider, label in ((self.transcriber, "ASR"), (self.speech, "TTS")):
                config = getattr(provider, "config", None)
                if config and (not config.base_url or not config.model):
                    raise CapabilityUnavailable(f"制作前检查：{label}尚未配置，未发送付费请求")

    def sibling(self, task, node, definition):
        candidates = [
            n for n in task["nodes"] if n["asset_id"] == node["asset_id"] and n["definition_id"] == definition
        ]
        return next(n for n in candidates if n["locale"] in {None, node["locale"]})

    def output(self, task, node, definition):
        return self.sibling(task, node, definition)["output"]

    def paid(self, task, node, provider, kind, action, *, allow_inactive=False):
        from .resource_budget import guard_external_call, guard_storage
        current = self.store.require("task", task["id"])
        guard_external_call(current)
        guard_storage(self.service, current)

        def reserve(current):
            if current["status"] == "cancelled" and not (allow_inactive and kind == "discussion"):
                raise Conflict("任务已取消")
            if current.get("control_paused") and not (allow_inactive and kind == "discussion"):
                from .task_route import TaskRoutePaused
                raise TaskRoutePaused("Task paused before external call reservation")
            count = current.get("paid_calls", 0)
            if not allows_new_call(current, 'max_paid_calls', count):
                raise Conflict("已达到本任务外部调用次数上限；重试不会重置额度")
            guard_external_call(current)
            current["paid_calls"] = count + 1

        self.store.update("task", task["id"], reserve)
        op = {
            "id": identifier(),
            "task_id": task["id"],
            "node_id": node["id"],
            "provider": provider,
            "kind": kind,
            "status": "running",
            "external_id": None,
            "estimated_cost": None,
            "actual_cost": None,
            "unit": None,
            "created_at": now(),
            "error": None,
        }
        self.store.put("operation", op)
        try:
            result = action()
        except UnknownExternalResult as exc:
            message = str(exc)
            self.store.update(
                "operation", op["id"], lambda record: record.update(status="unknown", error=message)
            )
            raise
        except Exception as exc:
            message = str(exc)[:1000]
            self.store.update(
                "operation", op["id"], lambda record: record.update(status="failed", error=message)
            )
            raise
        self.store.update(
            "operation", op["id"], lambda record: record.update(status="succeeded", finished_at=now())
        )
        return result

    def call_agent(self, task, node, inputs, model, frames=None, *, decision_id=None):
        inputs = {"agent_role": "work", **inputs}
        from .resource_budget import guard_external_call, guard_storage
        current = self.store.require("task", task["id"])
        guard_external_call(current)
        guard_storage(self.service, current)
        agent = self.agent_for(task)
        cfg = getattr(agent, "config", None)
        if cfg and (cfg.mode == "unconfigured" or cfg.mode == "http" and (not cfg.base_url or not cfg.model)):
            raise CapabilityUnavailable("尚未配置后台Agent，请在设置页查看接入步骤")
        schema = model.model_json_schema()
        packet = self.catalog.packet(task, node, inputs, schema)
        from video_translation.domain.limits import budget_policy, testing_unlimited
        if testing_unlimited(task):
            packet["budget_policy"] = budget_policy(task)
        packet["agent_role"] = inputs["agent_role"]
        packet["work_domain"] = ("audio" if node["definition_id"] in {
            "transcribe", "audio_prepare", "audio_voice", "audio"} else "visual")
        if node["definition_id"] == "observe" and inputs.get("agent_role") in {
                "task_route", "observation_work"}:
            from .task_route import ROUTE_INSTRUCTIONS, WORK_INSTRUCTIONS
            guidance = ROUTE_INSTRUCTIONS if inputs["agent_role"] == "task_route" else WORK_INSTRUCTIONS
            packet["instruction"] = packet.get("instruction", "") + (
                "\n\n本轮按以下程序指定角色和输出合同执行，上游观察规则仅作为业务约束：\n" + guidance)
            packet["agent_role"] = inputs["agent_role"]
        from video_translation.application.work_handoffs import handoffs_for
        packet["shared_handoffs"] = handoffs_for(self.service, task["id"], node["id"])
        from video_translation.application.task_supervision import live_directions
        packet["route_directions"] = live_directions(self.service, task["id"], node["id"])
        packet["images"] = frames or []
        if decision_id:
            packet["decision_id"] = decision_id
        prefix = (decision_id + "-") if decision_id else ""
        self.service.write_json(task["id"], node, prefix + "work-packet.json", packet)
        supplied = {item["id"] for item in packet["resource_versions"]}
        trace = {
            "id": identifier(), "task_id": task["id"], "node_id": node["id"],
            "agent_role": inputs.get("agent_role"),
            "attempt": node["attempt"], "decision_id": decision_id,
            "stage": node["definition_id"], "status": "running", "created_at": now(),
            "resources": [item for item in packet["resource_index"] if item["id"] in supplied],
            "requested_resources": inputs.get("resource_ids", []),
            "frame_times_ms": [frame.get("time_ms") for frame in (frames or [])],
            "result": None, "error": None,
        }
        self.store.put("agent_trace", trace)
        try:
            from .agent_format import run_agent
            from .workspace import workspace_for
            workspace = workspace_for(self.service, task)
            with workspace.measure("model_operation", node_id=node["id"], decision_id=decision_id,
                                   packet_bytes=len(json.dumps(packet, ensure_ascii=False).encode("utf-8")),
                                   supplied_images=len(frames or [])) if workspace else nullcontext():
                result = run_agent(self, agent, task, node, packet, schema,
                                   self.service.node_directory(task["id"], node))
            media = result.pop("_media", {})
            validated = model.model_validate(result).model_dump()
            self.service.write_json(task["id"], node, prefix + "agent-result.json", {**validated, "_media": media})
        except Exception as exc:
            failure_status = "unknown" if isinstance(exc, UnknownExternalResult) else "failed"
            failure_message = str(exc)[:1500]
            self.store.update("agent_trace", trace["id"], lambda row: row.update(
                status=failure_status, error=failure_message, finished_at=now()))
            raise
        self.store.update("agent_trace", trace["id"], lambda row: row.update(
            status="succeeded", result=validated, media=media, finished_at=now()))
        from video_translation.application.task_supervision import acknowledge_directions
        acknowledge_directions(self.service, task["id"], node["id"],
                               max((r["revision"] for r in packet["route_directions"]), default=-1))
        return validated, media

    def execute(self, task, node):
        for reference in task.get("rework_inputs", []):
            path = Path(reference["path"])
            if (path.is_symlink() or not path.resolve().is_relative_to(self.service.settings.runtime.resolve())
                    or self.service.hash_file(path) != reference["sha256"]):
                raise Conflict("Inherited rework evidence changed; do not execute against another version")
        from video_translation.application.collaboration import instructions_for
        instructions = instructions_for(task, node)
        brief = task.get("confirmed_briefs", {}).get(node["asset_id"])
        if instructions or brief:
            # Existing bounded sessions hash notes: a new confirmed brief cannot reuse old decisions.
            task = {**task, "notes": task["notes"] + "\n用户确认的制作补充（业务数据）：\n" +
                    json.dumps({"brief": brief, "node_instructions": instructions}, ensure_ascii=False)}
        method = getattr(self, "run_" + node["definition_id"])
        from .workspace import workspace_for
        workspace = workspace_for(self.service, task)
        if workspace:
            workspace.preflight(workspace.root / "nodes" / node["id"], 0)
        with workspace.measure("node", node_id=node["id"], stage=node["definition_id"]) if workspace else nullcontext():
            return method(task, node)

    def image_ready(self, task, *, transparent=False):
        if task.get("review_draft_authorization", {}).get("image_provider") == "codex_builtin_imagegen":
            return False
        from .adaptive_image import ImageProduction
        images = ImageProduction(self)
        return images.ready(task) and (not transparent or images.snapshot()["capabilities"].get("alpha", False))

    def edit_image(self, task, node, source, target, prompt, *, transparent=False):
        from .adaptive_image import ImageProduction
        return ImageProduction(self).edit(task, node, source, target, prompt, transparent=transparent)

    def run_visual_analyze(self, task, node):
        from .adaptive import AdaptiveProduction
        return AdaptiveProduction(self, task, node).analyze()

    def run_clean(self, task, node):
        from .adaptive import AdaptiveProduction
        return AdaptiveProduction(self, task, node).clean()

    def run_typography(self, task, node):
        from .lettering import LetteringProduction
        return LetteringProduction(self, task, node).typography()

    def run_audio_prepare(self, task, node):
        from .adaptive_audio import AdaptiveAudio
        return AdaptiveAudio(self, task, node).prepare()

    def run_audio_voice(self, task, node):
        from .adaptive_audio import AdaptiveAudio
        return AdaptiveAudio(self, task, node).voice()

    def run_prepare(self, task, node):
        source = self.service.checked_path(self.store.require("asset", node["asset_id"]))
        info = self.media.probe(source)
        if info["duration_ms"] > self.service.settings.max_duration_ms:
            raise ValueError("首版支持不超过180秒的视频，请先裁剪素材")
        if task.get("visual_route") == "wan_video_edit":
            if not self.kie_config or not 2000 <= info["duration_ms"] <= 10000 or source.suffix.lower() not in {".mp4", ".mov"}:
                raise CapabilityUnavailable("Wan当前仅支持2–10秒MP4/MOV且需要KIE；未发送模型请求")
            video=task["model_snapshot"]["models"]["video"]
            from video_translation.integrations.providers.video_pricing import video_estimate
            estimate = video_estimate(video, video["options"]["resolution"], info["duration_ms"])
            if not allows_usage(task, 'max_video_credits', estimate):
                raise CapabilityUnavailable("Wan视频额度不足；未发送模型请求，创建任务时请明确视频额度")
        self.preflight(task, info["has_audio"])
        from .workspace import workspace_for
        workspace = workspace_for(self.service, task)
        directory = self.service.node_directory(task["id"], node)
        with workspace.local_write() if workspace else nullcontext():
            if workspace:
                estimate = (info["width"] * info["height"] * 3 * self.media.max_frames
                            + info["duration_ms"] * 44100 * 4 // 1000)
                workspace.preflight(directory / "prepared.json", estimate)
            with workspace.measure("prepare") if workspace else nullcontext():
                prepared = self.media.prepare(source, directory)
        self.store.update("asset", node["asset_id"], lambda asset: asset.update(media=prepared["media"]))
        for frame in prepared["frames"]:
            self.service.artifact(task["id"], node, Path(frame["path"]), "image")
        if prepared.get("audio_path"):
            self.service.artifact(task["id"], node, Path(prepared["audio_path"]), "audio")
        return prepared

    def run_transcribe(self, task, node):
        prepared = self.output(task, node, "prepare")
        if not prepared["media"]["has_audio"]:
            return {"_skip": "原片没有音轨", "segments": [], "text": ""}
        audio = Path(prepared["audio_path"])
        if self.kie_config:
            result = self.kie(task, node, "audio_understanding", {"sha256": self.service.hash_file(audio), "model": self.kie_for(task).chat_model, "selection":task["model_snapshot"]["sha256"]},
                lambda client, key, event: client.chat_transcribe(audio, request_key=key, on_event=event))
        else:
            cfg = getattr(self.transcriber, "config", None)
            if cfg and (not cfg.base_url or not cfg.model):
                raise CapabilityUnavailable("原片含音轨，需配置ASR才能识别并核对人声")
            result = self.paid(task, node, "asr", "transcribe", lambda: self.transcriber.transcribe(audio))
        for segment in result.get("segments", []):
            if segment["end_ms"] > prepared["media"]["duration_ms"] + 200:
                raise ValueError("转写时间轴超出原片")
        return result

    def run_observe(self, task, node):
        prepared = self.output(task, node, "prepare")
        route_question = None
        inputs = {
            "media": prepared["media"],
            "transcript": self.output(task, node, "transcribe"),
            "frames": [{"time_ms": f["time_ms"]} for f in prepared["frames"]],
            "user_answer": node["output"].get("user_answer"),
        }
        if task.get("visual_route") == "agent_adaptive":
            from .adaptive_observe import observe
            inventory, consumed = observe(self, task, node, inputs, prepared["frames"])
            if inventory["unknowns"] or consumed.get("clarification_answer_changed"):
                from .task_route import TaskRoutePaused, supervise_observation
                try:
                    inventory, route_evidence, route_question = supervise_observation(
                        self, task, node, inputs, inventory, consumed)
                except TaskRoutePaused:
                    return {"_paused": True}
                consumed = {**consumed, "task_route": route_evidence}
        else:
            inventory, consumed = self.call_agent(task, node, inputs, Inventory, prepared["frames"])
        if any(
            item["end_ms"] > prepared["media"]["duration_ms"]
            for item in inventory["texts"] + inventory["speech"]
        ):
            raise ValueError("内容清单时间超出原片")
        # Text-only output cannot manufacture speech on a silent input.
        if inventory["speech"] and not prepared["media"]["has_audio"]:
            raise ValueError("无音轨原片的清单不能包含人声")
        transcript = inputs["transcript"]
        if transcript.get("segments") and not inventory["speech"]:
            raise ValueError("ASR识别到人声，但清单遗漏全部人声")
        if inventory["unknowns"]:
            if task.get("visual_route") == "agent_adaptive" and route_question is None:
                raise CapabilityUnavailable("Task Route must resolve or classify remaining observation gaps")
            if route_question:
                existing = [q for q in self.store.list("question")
                            if q.get("task_id") == task["id"] and q.get("node_id") == node["id"]
                            and q.get("status") == "open"
                            and q.get("context", {}).get("task_route_session_id") ==
                            consumed["task_route"]["session_id"]]
                if existing:
                    if len(existing) != 1 or existing[0]["prompt"] != route_question:
                        raise Conflict("Task Route question evidence changed")
                    return {**inventory, "_wait": existing[0]["id"], "_media": consumed}
            question = self.service.question(
                task["id"],
                node,
                route_question or ("请补充原片中无法确认的内容：" + "；".join(inventory["unknowns"])),
                "clarification",
                [],
                context=({"task_route_session_id": consumed["task_route"]["session_id"],
                          "category": "user_intent"} if route_question else None),
            )
            return {**inventory, "_wait": question["id"], "_media": consumed}
        if task.get("confirm_brief") and not node["output"].get("brief_confirmed"):
            question = self.service.question(
                task["id"], node,
                "原片已理解。请核对内容清单，并确认或补充制作要求；确认后开始后续制作。",
                "brief", [], context={"inventory": inventory, "notes": task["notes"]},
            )
            return {**inventory, "_wait": question["id"], "_media": consumed}
        return {**inventory, "_media": consumed}

    def run_translate(self, task, node):
        inventory = self.output(task, node, "observe")
        required = {i["id"] for i in inventory["texts"] if i["treatment"] == "translate"} | {
            i["id"] for i in inventory["speech"]
        }
        if not required:
            return {"locale": node["locale"], "items": [], "_skip": "没有待译文字或人声"}
        result, consumed = self.call_agent(
            task,
            node,
            {
                "inventory": inventory,
                "locale": node["locale"],
                "revision_feedback": task.get("revision_feedback", {}).get(node["asset_id"] + ":" + node["locale"], []),
            },
            TranslationPackage,
        )
        actual = [i["source_id"] for i in result["items"]]
        if set(actual) != required or len(actual) != len(set(actual)) or result["locale"] != node["locale"]:
            raise ValueError("译文必须逐项覆盖所有待译文字及人声，并匹配目标语言")
        return {**result, "_media": consumed}

    def run_plan(self, task, node):
        if task.get("visual_route") == "agent_adaptive":
            from .lettering import LetteringProduction
            return LetteringProduction(self, task, node).plan()
        route = task.get("visual_route", "solid_overlay")
        inventory = self.output(task, node, "observe")
        if route == "wan_video_edit" and any(item["treatment"] == "translate" for item in inventory["texts"]):
            info = self.output(task, node, "prepare")["media"]
            if not self.kie_config:
                raise CapabilityUnavailable("Wan画面路线需要KIE接入，不自动回退实色色块")
            if not 2000 <= info["duration_ms"] <= 10000:
                raise CapabilityUnavailable("当前Wan短片路线仅支持2–10秒；长片分镜尚未实现，不自动裁掉其余内容")
            video = task["model_snapshot"]["models"]["video"]
            price = video["estimated_credits_per_second"][video["options"]["resolution"]]
            if not allows_usage(task, 'max_video_credits', __import__('math').ceil(info['duration_ms'] / 1000) * price):
                raise CapabilityUnavailable("视频模型额度不足：按本任务模型配置预留，0额度禁用；请新建明确额度的任务")
        result, consumed = self.call_agent(
            task,
            node,
            {
                "inventory": self.output(task, node, "observe"),
                "translation": self.output(task, node, "translate"),
                "available_routes": [route],
                "audio_strategy": task.get("audio_strategy", "legacy"),
                "preserve_background": self.preserve_background(task, inventory),
                "unavailable": ["generative_inpainting", "external_clean_plate"],
            },
            ProductionPlan,
        )
        inventory = self.output(task, node, "observe")
        required = {i["id"] for i in inventory["texts"] if i["treatment"] == "translate"}
        actual = [step["source_id"] for step in result["visual_steps"]]
        if set(actual) != required or len(actual) != len(set(actual)):
            raise ValueError("制作计划必须覆盖全部待译画面文字")
        if inventory["speech"] and self.preserve_background(task, inventory) and not result["preserve_music"]:
            raise ValueError("制作方案不能自行放弃保留背景音乐")
        if any(step["route"] != route for step in result["visual_steps"]):
            raise CapabilityUnavailable("模型提出了任务未授权的画面路线；不会自行切换模型或降级覆盖")
        return {**result, "_media": consumed}

    def run_wan_visual(self, task, node, source, inventory, translation):
        info = self.output(task, node, "prepare")["media"]
        changes = [{"source": item["text"], "target": next(t["text"] for t in translation["items"] if t["source_id"] == item["id"]),
                    "start_ms":item["start_ms"],"end_ms":item["end_ms"],"region":item["region"]}
                   for item in inventory["texts"] if item["treatment"] == "translate"]
        prompt = ("Localize only the listed on-screen text in this source video. Replace each source with its exact target text at the same time and location. Preserve original typography, motion and occlusion as closely as possible. Naturally reconstruct the background behind removed source text; do not add solid rectangles, captions, patches, new subjects or new scenes. Preserve all unlisted text, characters, camera movement, composition and entire duration. Target language: " + node["locale"] + ". Changes: " + json.dumps(changes,ensure_ascii=False))
        directory = self.service.node_directory(task["id"], node)
        generated, target = directory / "wan-original.mp4", directory / "visual.mp4"
        identity = {"source_sha256":self.service.hash_file(source),"prompt":prompt,"model":self.kie_for(task).video_model,"selection":task["model_snapshot"]["sha256"],"resolution":self.kie_for(task).video_options["resolution"],"duration_ms":info["duration_ms"]}
        self.kie(task,node,"video_edit",identity,lambda client,key,event:client.video_edit(source,generated,prompt,media=info,request_key=key,on_event=event))
        self.service.artifact(task["id"],node,generated,"video")
        self.media.conform_video_edit(source,generated,target)
        artifact=self.service.artifact(task["id"],node,target,"video")
        return {"path":str(target),"artifact_id":artifact["id"],"route":"wan_video_edit","limitations":["生成式编辑可能改变主体、动作与译文字形；需对照原片完整审核", "720p模型结果按原比例恢复源尺寸，不代表恢复源细节"]}

    def run_visual(self, task, node):
        if task.get("visual_route") == "agent_adaptive":
            from .lettering import LetteringProduction
            return LetteringProduction(self, task, node).visual()
        source = self.service.checked_path(self.store.require("asset", node["asset_id"]))
        inventory, translation, plan = (
            self.output(task, node, definition) for definition in ("observe", "translate", "plan")
        )
        if not plan["visual_steps"]:
            return {"path": str(source), "_skip": "没有需要替换的画面文字"}
        if task.get("visual_route") == "wan_video_edit":
            return self.run_wan_visual(task,node,source,inventory,translation)
        info = self.output(task, node, "prepare")["media"]
        texts = {i["id"]: i for i in inventory["texts"]}
        translated = {i["source_id"]: i["text"] for i in translation["items"]}
        overlays = []
        for step in plan["visual_steps"]:
            original = texts[step["source_id"]]
            region = original["region"]
            x, y = int(region["x"] * info["width"]), int(region["y"] * info["height"])
            overlays.append(
                {
                    "text": translated[original["id"]],
                    "start_ms": original["start_ms"],
                    "end_ms": original["end_ms"],
                    "x": x,
                    "y": y,
                    "width": min(info["width"] - x, max(1, int(region["width"] * info["width"]))),
                    "height": min(info["height"] - y, max(1, int(region["height"] * info["height"]))),
                    "font_size": step["font_size"],
                    "color": step["color"],
                    "background": step["background"],
                }
            )
        target = self.service.node_directory(task["id"], node) / "visual.mp4"
        self.media.render_visual(source, target, overlays)
        artifact = self.service.artifact(task["id"], node, target, "video")
        return {
            "path": str(target),
            "artifact_id": artifact["id"],
            "route": "solid_overlay",
            "overlays": overlays,
            "limitations": ["实色覆盖并非背景修复，需逐帧核对遮挡与运动"],
        }

    def run_audio(self, task, node):
        if task.get("visual_route") == "agent_adaptive":
            from .adaptive_audio import AdaptiveAudio
            return AdaptiveAudio(self, task, node).mix()
        inventory, translation = (
            self.output(task, node, definition) for definition in ("observe", "translate")
        )
        if not inventory["speech"]:
            return {"path": None, "preserve_original_audio": True, "_skip": "没有需要替换的人声，保留原音轨"}
        separation = None
        if self.preserve_background(task, inventory, self.output(task, node, "plan")):
            self.check_separation(task)
            source = self.service.checked_path(self.store.require("asset", node["asset_id"]))
            source_audio = self.service.node_directory(task["id"], node) / "source-mix.wav"
            self.media.extract_original_audio(source, source_audio)
            cache = self.service.settings.runtime / "tasks" / task["id"] / "separation" / node["asset_id"]
            separation = self.separator.separate(source_audio, cache, media=self.media)
            for kind in ("background", "vocals"):
                path = Path(separation[kind]["path"])
                if self.service.hash_file(path) != separation[kind]["sha256"]:
                    raise Conflict("分离音轨与回执哈希不一致")
                self.service.artifact(task["id"], node, path, "audio", "伴奏/背景音" if kind == "background" else "原人声（审核参考）")
            background = self.service.node_directory(task["id"], node) / "dialogue-background.wav"
            self.media.background_for_dialogue(source_audio, Path(separation["background"]["path"]), background, inventory["speech"])
            separation["mixed_background"] = {"path": str(background), "sha256": self.service.hash_file(background),
                                               "policy": "original_outside_dialogue_crossfade_40ms"}
            self.service.artifact(task["id"], node, background, "audio", "用于混音的背景轨")
            self.service.write_json(task["id"], node, "separation.json", separation)
        cfg = self.kie_for(task) or getattr(self.speech, "config", None)
        if not self.kie_config and cfg and (not cfg.base_url or not cfg.model):
            raise CapabilityUnavailable("尚未配置目标语配音供应商")
        translated = {i["source_id"]: i["text"] for i in translation["items"]}
        directory = self.service.node_directory(task["id"], node)
        segments = []
        for index, speech in enumerate(inventory["speech"]):
            current = self.store.require("task", task["id"])
            if current["status"] == "cancelled":
                raise Conflict("任务已取消")
            target = directory / f"speech-{index:03d}.wav"
            identity = {"task": task["id"], "asset": node["asset_id"], "locale": node["locale"],
                        "source_id": speech["id"], "text": translated[speech["id"]],
                        "base_url": getattr(cfg, "base_url", "fixture"), "model": getattr(cfg, "model", "fixture"),
                        "voice": getattr(cfg, "voice", "fixture"), "model_snapshot":task.get("model_snapshot",{}).get("sha256")}
            key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cached = self.store.get("audio_segment", key)
            if cached:
                cached_path = Path(cached["path"])
                if not cached_path.is_file() or self.service.hash_file(cached_path) != cached["sha256"]:
                    raise Conflict("已收费配音缓存缺失或变化，需核对产物后再继续，不能自动重新收费")
                shutil.copyfile(cached_path, target)
            else:
                def synthesize_and_cache():
                    if self.kie_config:
                        self.kie(task,node,"synthesize",identity,lambda client,key,event:client.synthesize(translated[speech["id"]],node["locale"],target,request_key=key,on_event=event))
                    else:
                        self.speech.synthesize(translated[speech["id"]], node["locale"], target)
                    self.store.put("audio_segment", {"id": key, "task_id": task["id"], "path": str(target),
                        "sha256": self.service.hash_file(target), "created_at": now()})
                    return target
                if self.kie_config:
                    synthesize_and_cache()
                else:
                    self.paid(task, node, "tts", "synthesize", synthesize_and_cache)
            self.service.artifact(task["id"], node, target, "audio")
            segments.append(
                {
                    "path": str(target),
                    "start_ms": speech["start_ms"],
                    "end_ms": speech["end_ms"],
                    "text": translated[speech["id"]],
                }
            )
        target = directory / "voice.wav"
        self.media.assemble_audio(
            segments, target, self.output(task, node, "prepare")["media"]["duration_ms"]
        )
        artifact = self.service.artifact(task["id"], node, target, "audio")
        return {
            "path": str(target),
            "artifact_id": artifact["id"],
            "segments": segments,
            "preserve_original_audio": False,
            "background_path": separation["mixed_background"]["path"] if separation else None,
            "separation": separation,
            "limitations": ["分离伴奏可能残留原人声或影响音效；须完整听验"] if separation else ["按任务选择替换原音轨，未保留背景音"],
        }

    def run_compose(self, task, node):
        from .review_draft import metadata
        visual, audio = (self.output(task, node, definition) for definition in ("visual", "audio"))
        if task.get("visual_route") == "agent_adaptive":
            from .stage_quality import require_production_base, require_visual_review
            require_production_base(self.output(task, node, "clean"), task)
            require_visual_review(visual, task)
        target = self.service.node_directory(task["id"], node) / "candidate.mp4"
        self.media.compose(
            Path(visual["path"]),
            target,
            audio=Path(audio["path"]) if audio.get("path") else None,
            preserve_original_audio=audio.get("preserve_original_audio", True),
            background=Path(audio["background_path"]) if audio.get("background_path") else None,
        )
        technical = self.media.validate(target)
        source_info = self.output(task, node, "prepare")["media"]
        actual = self.media.probe(target)
        if abs(actual["duration_ms"] - source_info["duration_ms"]) > 250:
            raise ValueError("候选视频时长与原片偏差超过250ms")
        if (actual["width"], actual["height"]) != (source_info["width"], source_info["height"]):
            raise ValueError("候选尺寸与原片不一致")
        if source_info["has_audio"] and not actual["has_audio"]:
            raise ValueError("候选视频丢失音轨")
        artifact = self.service.artifact(task["id"], node, target, "video")
        return {
            "path": str(target),
            "artifact_id": artifact["id"],
            "sha256": artifact["sha256"],
            "technical": technical,
            **metadata(visual, task),
            **({"completion_state": "best_available", "quality_passed": False,
                "unresolved": visual.get("unresolved", []),
                "lettering_limitations": [
                    {"source_id": layer["source_id"], "fidelity": layer.get("fidelity", {})}
                    for layer in visual.get("layers", [])
                ]} if task.get("production_policy", {}).get("mode") == "completion_first" else {}),
        }

    def run_review(self, task, node):
        if task.get("visual_route") == "agent_adaptive":
            from .stage_quality import require_production_base
            require_production_base(self.output(task, node, "clean"), task)
        candidate = self.output(task, node, "compose")
        inventory = self.output(task, node, "observe")
        directory = self.service.node_directory(task["id"], node)
        prepared = self.media.prepare(Path(candidate["path"]), directory / "review-evidence")
        for frame in prepared["frames"]:
            self.service.artifact(task["id"], node, Path(frame["path"]), "image")
        result, consumed = self.call_agent(
            task,
            node,
            {
                "inventory": inventory,
                "translation": self.output(task, node, "translate"),
                "final_spoken_texts": self.output(task, node, "audio_voice").get("spoken_texts", {}) if task.get("visual_route") == "agent_adaptive" else {},
                "speech_revisions": self.output(task, node, "audio_voice").get("revisions", []) if task.get("visual_route") == "agent_adaptive" else [],
                "visual_evidence": self.output(task, node, "visual").get("visual_evidence", []) if task.get("visual_route") == "agent_adaptive" else [],
                "lettering_fidelity": [
                    {"source_id": layer["source_id"], "fidelity": layer.get("fidelity", {})}
                    for layer in self.output(task, node, "visual").get("layers", [])
                ] if task.get("visual_route") == "agent_adaptive" else [],
                "candidate_sha256": candidate["sha256"],
                "technical": candidate["technical"],
                "completion_state": candidate.get("completion_state"),
                "unresolved_cleanup": candidate.get("unresolved", []),
                "review_priority": (
                    "完成优先：核对全部译文、原字残留、主体和连续性；少量发丝/纹理差异属minor。"
                    "已知缺少遮挡的数据不是轻微瑕疵，逐项指出可见影响。未播放不能声称流畅。"
                    "如有问题照实needs_revision，系统将保留候选及报告，不自动重复整条制作。"
                ) if task.get("production_policy", {}).get("mode") == "completion_first" else None,
                "source_frames": self.output(task, node, "prepare")["frames"],
                "candidate_frames": prepared["frames"],
                "limitations": ["只提供抽帧及文本，未完整播放或收听",
                                *candidate.get("limitations", []),
                                *self.output(task, node, "audio").get("limitations", [])],
            },
            ReviewReport,
            self.output(task, node, "prepare")["frames"] + prepared["frames"],
        )
        required = {i["id"] for i in inventory["texts"] + inventory["speech"]}
        if set(result["covered_ids"]) != required:
            raise ValueError("审核未逐项核对内容清单")
        output = {
            **result,
            "_media": consumed,
            "artifact_id": candidate["artifact_id"],
            "sha256": candidate["sha256"],
        }
        from .review_draft import is_review_draft, metadata
        if is_review_draft(candidate, task):
            question = self.service.question(
                task["id"], node,
                "尽力审看草稿已生成，尚有已记录的清底或画面问题，未通过质量验收。"
                "请完整播放并核对译文、声音、残字、人物和流畅度；approve表示明确接受此有缺陷版本，"
                "reject表示保留草稿并提出修改意见。",
                "review", ["approve", "reject"],
                {"artifact_id": candidate["artifact_id"], "sha256": candidate["sha256"],
                 "completion_state": "review_draft", "known_issues": candidate.get("unresolved", []),
                 "limitations": candidate.get("limitations", [])})
            return {**output, **metadata(candidate, task), "quality_status": "awaiting_human",
                    "_wait": question["id"]}
        if (
            any(d["severity"] in {"critical", "major"} for d in result["defects"])
            or result["verdict"] == "needs_revision"
        ):
            return {**output, "_revision": True}
        question = self.service.question(
            task["id"],
            node,
            "候选已完成技术检查和抽帧审核。请完整播放原片与候选，核对译文、声音、音乐及画面遮挡；确认后交付。",
            "review",
            ["approve", "reject"],
            {"artifact_id": candidate["artifact_id"], "sha256": candidate["sha256"]},
        )
        return {**output, "quality_status": "awaiting_human", "_wait": question["id"]}

    def run_deliver(self, task, node):
        review = self.output(task, node, "review")
        candidate = self.output(task, node, "compose")
        path = Path(candidate["path"])
        digest = self.service.hash_file(path)
        if review.get("quality_status") != "human_approved" or review.get("approved_sha256") != digest:
            raise Conflict("交付缺少当前成片的完整审核批准")
        destination = (
            self.service.settings.runtime / "deliveries" / task["id"] / node["asset_id"] / node["locale"]
        )
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{digest[:12]}.mp4"
        shutil.copyfile(path, target)
        artifact = self.service.artifact(
            task["id"], node, target, "video", f"{task['title']} · {node['locale']}.mp4"
        )
        if artifact["sha256"] != digest:
            raise Conflict("交付副本与批准成片不一致")
        delivery = {
            "id": identifier(),
            "task_id": task["id"],
            "title": task["title"],
            "locale": node["locale"],
            "asset_id": node["asset_id"],
            "artifact_id": artifact["id"],
            "quality_status": review["quality_status"],
            "status": "accepted",
            "created_at": now(),
            "content_url": artifact["content_url"],
            "notes": "本地交付；人工确认完整视听。",
        }
        from .review_draft import is_review_draft
        if is_review_draft(candidate, task):
            delivery.update(status="delivered_with_issues", quality_passed=False,
                            unresolved=candidate.get("unresolved", []),
                            limitations=candidate.get("limitations", []),
                            notes="用户明确接受的尽力版本；已知问题保留，不代表质量全部通过")
        # Replayed local delivery replaces the same logical record, not a duplicate output.
        delivery["id"] = node["id"]
        self.service.commit_delivery(task["id"], node["id"], delivery, digest)
        return delivery
