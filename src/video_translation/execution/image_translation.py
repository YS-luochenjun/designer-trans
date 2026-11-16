"""Source/locale image production using the shared Work, ledger and Route queues."""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Literal
import zipfile

from PIL import Image, ImageChops, ImageDraw, ImageOps
from pydantic import Field

from video_translation.application.tasks import Conflict
from video_translation.domain.execution import FileRef, identity
from video_translation.domain.models import (
    Contract, ImageInventory, ImageReviewReport, TranslationPackage,
)
from video_translation.domain.production import LetterStyle
from video_translation.integrations.agents import CapabilityUnavailable, UnknownExternalResult
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from .workspace import workspace_for


class ImageRepair(Contract):
    source_id: str
    method: Literal["solid", "image_edit", "native_file"]
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    prompt: str = Field(default="", max_length=10000)
    candidate: FileRef | None = None
    reason: str = Field(min_length=1, max_length=1600)


class ImageCleanPlan(Contract):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repairs: list[ImageRepair] = Field(default_factory=list, max_length=200)
    evidence: list[str] = Field(min_length=1, max_length=30)


class ImageLettering(Contract):
    source_id: str
    style: LetterStyle


class ImageLetteringPlan(Contract):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clean_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locale: str
    items: list[ImageLettering] = Field(default_factory=list, max_length=200)


def rectangle(region):
    x, y, width, height = region["bbox"]
    return x, y, x + width, y + height


def intersects(left, right):
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


class ImageTranslation:
    def __init__(self, executor):
        self.e, self.service, self.store = executor, executor.service, executor.store

    def active(self, task):
        current = self.store.require("task", task["id"])
        if current.get("control_paused") or current["status"] == "cancelled":
            raise Conflict("图片任务已暂停或取消，保留原输出")

    def checked(self, reference):
        path = self.service.checked_path(reference)
        if self.service.hash_file(path) != reference["sha256"]:
            raise Conflict("图片或依赖文件身份已变化，旧审核不能放行")
        return path

    def output(self, task, node, definition):
        current = self.store.require("task", task["id"])
        return self.e.output(current, node, definition)

    def decision(self, task, node, inputs, model, images=()):
        """Reuse a completed decision; changing inputs never bypasses an unsettled call."""
        self.active(task)
        from video_translation.application.task_supervision import live_directions
        from video_translation.application.agent_recovery import replaced_with_authorization
        inputs = {**inputs, "task_kind": "image_translation",
                  "route_directions": live_directions(self.service, task["id"], node["id"])}
        frames = [{**item, "time_ms": None, "kind": "still_image"} for item in images]
        dependencies = [self.service.hash_file(Path(frame["path"])) for frame in frames]
        key = identity({"task": task["id"], "node": node["id"], "inputs": inputs,
                        "notes": task.get("notes"), "brief_revision": task.get("brief_revision", 0),
                        "schema": model.model_json_schema(), "images": dependencies,
                        "resources": task.get("resource_snapshot"), "engine": task.get("agent_engine")})
        with self.store.connection() as db:
            tx = TransactionStore(self.store, db)
            previous = tx.get("production_decision", key)
            if previous and previous["status"] == "succeeded":
                return model.model_validate(previous["result"]).model_dump()
            outstanding = [row for row in tx.list("production_decision")
                           if row.get("task_id") == task["id"] and row.get("node_id") == node["id"]
                           and row["status"] in {"running", "unknown", "invalid_response"}
                           and not replaced_with_authorization(tx, row, "production_decision")]
            if outstanding:
                raise UnknownExternalResult("图片 Work 原请求尚未核清，不能重复提交")
            if previous:
                raise CapabilityUnavailable("相同图片方案已失败；Route 需根据证据调整方法或工单")
            before = {row["id"] for row in tx.list("operation") if row.get("node_id") == node["id"]}
            tx.put("production_decision", {
                "id": key, "task_id": task["id"], "node_id": node["id"], "stage": node["definition_id"],
                "key": key, "status": "running", "created_at": now(),
            })
        try:
            result, media = self.e.call_agent(task, node, inputs, model, frames, decision_id=key[:20])
            if dependencies != [self.service.hash_file(Path(frame["path"])) for frame in frames]:
                raise Conflict("图片观察期间输入变化，保留原决定供核对")
        except Exception as exc:
            operations = [row for row in self.store.list("operation")
                          if row.get("node_id") == node["id"] and row["id"] not in before]
            status = ("unknown" if isinstance(exc, UnknownExternalResult)
                      or any(row["status"] in {"unknown", "running"} for row in operations)
                      else "invalid_response" if any(row["status"] == "succeeded" for row in operations)
                      else "failed")
            error = str(exc)[:1500]
            self.store.update("production_decision", key, lambda row: row.update(
                status=status, error=error, finished_at=now()))
            raise
        self.store.update("production_decision", key, lambda row: row.update(
            status="succeeded", result=result, media=media, finished_at=now()))
        return result

    def prepare_images(self, task, node):
        self.active(task)
        asset = self.store.require("asset", node["asset_id"])
        source = self.checked(asset)
        from video_translation.application.image_assets import inspect_image
        info = inspect_image(source)["image"]
        directory = self.service.node_directory(task["id"], node)
        target = directory / "source.png"
        workspace = workspace_for(self.service, task)
        with workspace.local_write() if workspace else nullcontext():
            if workspace:
                workspace.preflight(target, info["display_size"][0] * info["display_size"][1] * 4)
            with Image.open(source) as image:
                normalized = ImageOps.exif_transpose(image)
                normalized = normalized.convert("RGBA" if (
                    "A" in normalized.getbands() or "transparency" in normalized.info) else "RGB")
                normalized.info.clear()
                normalized.save(target)
        if self.service.hash_file(source) != asset["sha256"]:
            raise Conflict("规范化期间原图变化")
        artifact = self.service.artifact(task["id"], node, target, "image", "规范化原图.png")
        return {"path": str(target), "sha256": artifact["sha256"], "artifact_id": artifact["id"],
                "original_sha256": asset["sha256"], "width": normalized.width, "height": normalized.height}

    def analyze_images(self, task, node):
        prepared = self.output(task, node, "prepare_images")
        source = self.checked(prepared)
        result = self.decision(task, node, {
            "source": prepared, "coordinate_system": "EXIF normalized PNG pixels: bbox=[x,y,width,height]",
            "instructions": (
                "实际观察整图，完整登记文字、原文和样式备注。标明translate/preserve；人物、标志、"
                "保留文字等登记protected_regions并设action=preserve。不要伪造OCR或视频帧号。"
                "待译矩形不得重叠或覆盖保护区；无法明确观察应交Route具体缺口。"),
        }, ImageInventory, [{"path": str(source), "label": "原图"}])
        if (result["source_sha256"], result["width"], result["height"]) != (
                prepared["sha256"], prepared["width"], prepared["height"]):
            raise Conflict("图片分析未绑定当前规范化原图")
        translated = [region for region in result["regions"] if region["action"] == "translate"]
        protected = [region for region in result["regions"] if region["action"] == "preserve"]
        protected += result["protected_regions"]
        if not result["observations"] or any(region["action"] != "preserve" for region in protected):
            raise ValueError("图片观察与保护区说明不完整")
        for index, region in enumerate(translated):
            if any(intersects(rectangle(region), rectangle(other))
                   for other in translated[index + 1:] + protected):
                raise ValueError("图片待译区域互相重叠或覆盖保护区，请细化区域")
        if task.get("confirm_brief") and not node["output"].get("brief_confirmed"):
            question = self.service.question(task["id"], node,
                "原图已理解，请核对文字范围与保留要求，确认后开始制作。",
                "brief", [], {"inventory": result, "notes": task["notes"]})
            return {**result, "_wait": question["id"]}
        return result

    def review(self, task, node, inventory, candidate, scope, translation=None):
        prepared = self.output(task, node, "prepare_images")
        self.checked(candidate)
        result = self.decision(task, node, {
            "inventory": inventory, "source_sha256": prepared["sha256"],
            "candidate_sha256": candidate["sha256"], "scope": scope, "translation": translation,
            "instructions": (
                "对照实际原图和当前候选，逐一覆盖regions和protected_regions的ID，检查残字、"
                "保护内容、尺寸、边缘、译文语义、完整性和字形。清底复核时不要求出现译文。"
                "少量纹理差异可记录minor；可读原文、叠字、主体损伤或漏译是major。"
                "只有当前图实际达标才pass，技术文件或以前审核不能代替本轮观察。"),
        }, ImageReviewReport, [
            {"path": str(self.checked(prepared)), "label": "原图"},
            {"path": str(self.checked(candidate)), "label": scope},
        ])
        ids = [region["id"] for region in inventory["regions"] + inventory["protected_regions"]]
        if (set(result["covered_ids"]) != set(ids) or len(result["covered_ids"]) != len(ids)
                or not result["evidence"]):
            raise ValueError("图片复核必须逐项覆盖当前全部区域并提供实际观察")
        return {**result, "sha256": candidate["sha256"], "scope": scope}

    def background_review(self, task, node):
        prepared = self.output(task, node, "prepare_images")
        inventory = self.output(task, node, "analyze_images")
        source = self.checked(prepared)
        regions = [region for region in inventory["regions"] if region["action"] == "translate"]
        directory = self.service.node_directory(task["id"], node)
        plan = self.decision(task, node, {
            "source": prepared, "inventory": inventory,
            "image_edit_available": self.e.image_ready(task),
            "native_output_directory": str(directory),
            "instructions": (
                "为每个待译ID选择清底方法。solid只适用已观察的真实纯色背景；"
                "image_edit用当前已配置图片工具；native_file允许Work原生工具制作PNG，"
                "必须在native_output_directory内保存、返回candidate的path/sha256并保留工具回执。"
                "程序仅粘贴登记的待译矩形，保护区和其他像素不改；不要把没有工具当作已制作。"),
        }, ImageCleanPlan, [{"path": str(source), "label": "待清理原图"}])
        repairs = {repair["source_id"]: repair for repair in plan["repairs"]}
        if (plan["source_sha256"] != prepared["sha256"] or len(repairs) != len(plan["repairs"])
                or set(repairs) != {region["id"] for region in regions}):
            raise ValueError("图片清底计划必须完整绑定当前原图和待译范围")
        key = identity({"source": prepared["sha256"], "plan": plan})
        cached = self.store.get("image_clean_candidate", task["id"] + "-" + key)
        if cached:
            candidate = cached
            self.checked(candidate)
        else:
            with Image.open(source) as image:
                clean = image.convert("RGBA")
            for region in regions:
                self.active(task)
                repair, bounds = repairs[region["id"]], rectangle(region)
                if repair["method"] == "solid":
                    ImageDraw.Draw(clean).rectangle(
                        (bounds[0], bounds[1], bounds[2] - 1, bounds[3] - 1), fill=repair["color"])
                    continue
                if repair["method"] == "image_edit":
                    generated = directory / f"{key[:12]}-{identity(region)[:12]}-generated.png"
                    self.e.edit_image(task, node, source, generated, repair["prompt"] or (
                        "Remove only this text and reconstruct its real background: " + region["source_text"]))
                else:
                    if not repair["candidate"]:
                        raise ValueError("原生图片候选必须带真实文件身份")
                    generated = self.checked(repair["candidate"])
                    if not generated.resolve().is_relative_to(directory.resolve()):
                        raise Conflict("原生候选必须位于当前Work输出目录")
                from video_translation.application.image_assets import inspect_image
                inspect_image(generated)
                with Image.open(generated) as edited:
                    if edited.size != clean.size:
                        raise ValueError("清底候选必须保留原图尺寸，不能自动拉伸")
                    clean.paste(edited.convert("RGBA").crop(bounds), bounds[:2])
            target = directory / (key[:16] + "-clean.png")
            workspace = workspace_for(self.service, task)
            with workspace.local_write():
                workspace.preflight(target, clean.width * clean.height * 4)
                clean.save(target)
            artifact = self.service.artifact(task["id"], node, target, "image", "图片无字候选.png")
            candidate = {"id": task["id"] + "-" + key, "task_id": task["id"], "node_id": node["id"],
                         "created_at": now(), "path": str(target), "sha256": artifact["sha256"],
                         "artifact_id": artifact["id"], "source_sha256": prepared["sha256"],
                         "plan": plan, "quality_passed": False}
            self.store.put("image_clean_candidate", candidate)
        report = self.review(task, node, inventory, candidate, "clean_plate")
        passed = report["verdict"] == "pass" and not any(
            issue["severity"] in {"critical", "major"} for issue in report["defects"])
        return {**candidate, "clean_review": report, "quality_passed": passed,
                **({} if passed else {"_revision": True, "defects": report["defects"]})}

    def translate_images(self, task, node):
        inventory = self.output(task, node, "analyze_images")
        required = {region["id"] for region in inventory["regions"] if region["action"] == "translate"}
        if not required:
            return {"locale": node["locale"], "items": [], "_skip": "没有待翻译图片文字"}
        result = self.decision(task, node, {
            "inventory": inventory, "locale": node["locale"],
            "instructions": "逐项翻译待译文字，保留语义、数字、品牌和语气；不得漏项或改动preserve内容。",
        }, TranslationPackage, [{"path": str(self.checked(self.output(task, node, "prepare_images"))),
                                "label": "翻译语境"}])
        ids = [item["source_id"] for item in result["items"]]
        if result["locale"] != node["locale"] or set(ids) != required or len(ids) != len(required):
            raise ValueError("图片译文未准确覆盖当前语言全部待译区域")
        return result

    def fonts(self):
        from media_capabilities.fonts import discover
        return discover(self.service.settings.root / "config" / "fonts", self.e.media.font)

    def compose_images(self, task, node):
        from media_capabilities.fonts import catalog
        prepared = self.output(task, node, "prepare_images")
        inventory = self.output(task, node, "analyze_images")
        clean = self.output(task, node, "background_review")
        from .stage_quality import require_clean_plate
        require_clean_plate(clean, task)
        if clean.get("source_sha256") != prepared["sha256"]:
            raise Conflict("清底不属于当前原图")
        translation = self.output(task, node, "translate_images")
        source, background = self.checked(prepared), self.checked(clean)
        fonts = self.fonts()
        plan = self.decision(task, node, {
            "source": prepared, "clean_sha256": clean["sha256"], "inventory": inventory,
            "translation": translation, "fonts": list(fonts), "locale": node["locale"],
            "font_catalog": catalog(fonts, ''.join(i["text"] for i in translation["items"])),
            "instructions": "为每个待译区域选择真实字体与样式，沿用原图构图、字号和颜色，文字须完整放进原矩形。",
        }, ImageLetteringPlan, [{"path": str(source), "label": "原图字样"}])
        regions = {region["id"]: region for region in inventory["regions"] if region["action"] == "translate"}
        items = {item["source_id"]: item for item in plan["items"]}
        if (plan["source_sha256"], plan["clean_sha256"], plan["locale"]) != (
                prepared["sha256"], clean["sha256"], node["locale"]) or (
                set(items) != set(regions) or len(items) != len(plan["items"])):
            raise ValueError("图片字层计划未绑定当前原图、清底、语言或完整范围")
        translated = {item["source_id"]: item["text"] for item in translation["items"]}
        if set(translated) != set(regions):
            raise ValueError("图片译文范围与字层不一致")
        from .visual_media import VisualMedia
        from .media import MediaTools
        directory = self.service.node_directory(task["id"], node)
        with Image.open(background) as image:
            composite = image.convert("RGBA")
        layer = Image.new("RGBA", composite.size)
        font_hashes, layout = {}, []
        for source_id, item in items.items():
            style = dict(item["style"])
            font_id = style.pop("font_id")
            font = fonts.get(font_id)
            if not font:
                raise CapabilityUnavailable("计划字体不存在，Route需调整字层计划")
            font_hashes[font] = self.service.hash_file(Path(font))
            bounds = rectangle(regions[source_id])
            tools = VisualMedia(MediaTools(font=font))
            text = translated[source_id]
            # Reuse the video renderer's real glyph/overflow checks for still-image layers.
            patch = tools._text_image({"text": text, "style": style},
                                     (bounds[2] - bounds[0], bounds[3] - bounds[1]))
            layer.alpha_composite(patch, bounds[:2])
            layout.append({"source_id": source_id, "bbox": regions[source_id]["bbox"],
                           "text": text, "style": item["style"]})
        composite = Image.alpha_composite(composite, layer)
        target, text_layer = directory / "translated.png", directory / "text-layer.png"
        workspace = workspace_for(self.service, task)
        with workspace.local_write():
            workspace.preflight(target, composite.width * composite.height * 8)
            composite.save(target)
            layer.save(text_layer)
        if any(self.service.hash_file(Path(font)) != digest for font, digest in font_hashes.items()):
            raise Conflict("渲染期间字体变化，候选未放行")
        artifact = self.service.artifact(task["id"], node, target, "image", f"图片译文 · {node['locale']}.png")
        self.service.artifact(task["id"], node, text_layer, "image", "独立图片字层.png")
        receipt = {"path": str(target), "sha256": artifact["sha256"], "artifact_id": artifact["id"],
                   "source_sha256": prepared["sha256"], "clean_sha256": clean["sha256"],
                   "translation_sha256": identity(translation), "layout": layout,
                   "font_sha256": font_hashes, "quality_passed": False}
        self.service.write_json(task["id"], node, "image-render-receipt.json", receipt)
        return receipt

    def image_review(self, task, node):
        inventory = self.output(task, node, "analyze_images")
        candidate = self.output(task, node, "compose_images")
        prepared = self.output(task, node, "prepare_images")
        source, target = self.checked(prepared), self.checked(candidate)
        with Image.open(source) as original, Image.open(target) as translated:
            if original.size != translated.size:
                raise Conflict("图片成品尺寸变化")
            for region in (inventory["protected_regions"] + [
                    r for r in inventory["regions"] if r["action"] == "preserve"]):
                bounds = rectangle(region)
                if ImageChops.difference(original.convert("RGBA").crop(bounds),
                                         translated.convert("RGBA").crop(bounds)).getbbox(alpha_only=False):
                    raise Conflict("图片成品改变保护区像素")
        translation = self.output(task, node, "translate_images")
        report = self.review(task, node, inventory, candidate, "translated_image", translation)
        output = {**report, "artifact_id": candidate["artifact_id"],
                  "source_sha256": prepared["sha256"], "quality_passed": False}
        if report["verdict"] == "needs_revision" or any(
                issue["severity"] in {"critical", "major"} for issue in report["defects"]):
            return {**output, "_revision": True}
        question = self.service.question(task["id"], node,
            "图片候选已生成。请对照原图完整检查译文、残字、主体、字形和布局；批准仅绑定当前图片。",
            "review", ["approve", "reject"],
            {"artifact_id": candidate["artifact_id"], "sha256": candidate["sha256"], "media_kind": "image"})
        return {**output, "quality_status": "awaiting_human", "_wait": question["id"]}

    def deliver(self, task, node):
        current = self.store.require("task", task["id"])
        reviews = [item for item in current["nodes"] if item["id"] in node["depends_on"]]
        if (len(reviews) != len(task["asset_ids"])
                or {item["asset_id"] for item in reviews} != set(task["asset_ids"])):
            raise Conflict("图片语言包缺少来源成员")
        members = []
        for review in sorted(reviews, key=lambda item: item["asset_id"]):
            output = review["output"]
            if (review["definition_id"] != "image_review" or review["locale"] != node["locale"]
                    or review["status"] != "succeeded" or output.get("quality_status") != "human_approved"):
                raise Conflict("图片语言包须全部成员人工批准")
            artifact = self.store.require("artifact", output["artifact_id"])
            path = self.checked(artifact)
            if output.get("approved_sha256") != artifact["sha256"]:
                raise Conflict("图片语言包成员的批准版本不一致")
            members.append({"asset_id": review["asset_id"], "artifact_id": artifact["id"],
                            "sha256": artifact["sha256"], "path": str(path)})
        destination = self.service.node_directory(task["id"], node)
        target = destination / (node["locale"] + "-images.zip")
        self.active(task)
        workspace = workspace_for(self.service, task)
        with workspace.local_write():
            workspace.preflight(target, sum(Path(item["path"]).stat().st_size for item in members))
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
                for index, member in enumerate(members):
                    member["archive_name"] = f"{index + 1:02d}-{member['asset_id'][:12]}.png"
                    archive.write(member["path"], member["archive_name"])
        for member in members:
            self.checked(member)
            member.pop("path")
        artifact = self.service.artifact(task["id"], node, target, "archive",
                                         f"{task['title']} · {node['locale']}.zip")
        delivery = {"id": node["id"], "task_id": task["id"], "asset_id": "", "locale": node["locale"],
                    "title": task["title"], "artifact_id": artifact["id"], "sha256": artifact["sha256"],
                    "members": members, "quality_status": "human_approved", "status": "accepted",
                    "created_at": now(), "content_url": artifact["content_url"], "notes": "逐成员人工批准的图片语言包"}
        self.service.commit_delivery(task["id"], node["id"], delivery, artifact["sha256"])
        return delivery
