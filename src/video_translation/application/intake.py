"""Preview and atomically start production from a permission-scoped Fireplay brief."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from uuid import UUID

from pydantic import Field, model_validator

from .requirements import RequirementService
from .tasks import Conflict, TaskService
from ..domain.models import Contract, TaskCreate
from ..execution.media import MediaError, MediaTools
from ..integrations.fireplay.bridge import requirement_payload
from ..integrations.fireplay.sources import SourceFetcher, sources_from_brief
from ..persistence.store import identifier, now
from ..persistence.transaction import TransactionStore


class IntakePreview(Contract):
    reference: str = Field(min_length=1, max_length=128)


class IntakeCreate(TaskCreate):
    asset_ids: list[str] = Field(default_factory=list, max_length=20)
    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    confirm_sources: bool = False
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def selected_sources(self):
        if not self.asset_ids and not self.source_ids:
            raise ValueError("请选择需求引用的原视频，或补传本地原片")
        if len(self.asset_ids) + len(self.source_ids) > 20 or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("原片最多20条，且不得重复选择")
        if self.source_ids and not self.confirm_sources:
            raise ValueError("请确认选中的需求引用就是待翻译原片")
        return self


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_brief(brief):
    """Freeze only the production brief; exclude delivery and arbitrary connector fields."""
    keys = ("id", "code", "title", "description", "refText", "deptId", "quantity", "channels", "locales",
            "assetType", "updatedAt", "canBindMaterials", "canUpdateStatus")
    value = {key: brief.get(key) for key in keys}
    if "aiMode" in brief:
        mode = brief["aiMode"]
        if mode is not None and (not isinstance(mode, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", mode)):
            raise ValueError("需求AI承接交付字段格式无效")
        value["aiMode"] = mode
    if value["assetType"] != "video":
        raise ValueError("该需求不是视频需求")
    value["id"] = str(UUID(str(value["id"])))
    if not isinstance(value["code"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value["code"]):
        raise ValueError("需求编号无效")
    for key in ("title", "description", "refText", "deptId", "updatedAt"):
        if value[key] is not None and (not isinstance(value[key], str) or len(value[key]) > 32000):
            raise ValueError("需求文本字段格式无效或过长")
    locales = value["locales"] or []
    if not isinstance(locales, list) or len(locales) > 12 or any(
        not isinstance(v, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", v) for v in locales
    ):
        raise ValueError("需求语言无法映射为规范语言标签，请先在平台核对")
    value["locales"] = list(dict.fromkeys(locales))
    return value


class IntakeService:
    def __init__(self, tasks, fireplay, fetcher=None, probe=None):
        self.tasks, self.store, self.fireplay = tasks, tasks.store, fireplay
        self.fetcher = fetcher or SourceFetcher(tasks.settings)
        self.probe = probe or self._probe
        self.lock = threading.Lock()

    def _identity(self):
        if not self.fireplay.mcp or not self.fireplay.configured:
            raise ValueError("请先连接 Fireplay 并登录后再读取需求")
        return digest({"credential": self.fireplay.mcp.config.scope_fingerprint(),
                       "scope": self.fireplay.settings.fireplay_scope})

    def _resolve(self, reference):
        reference = reference.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", reference):
            raise ValueError("请输入完整需求号或需求 UUID")
        try:
            requirement_id = str(UUID(reference))
        except ValueError:
            matches, cursors = {}, set()
            arguments = {"scope": self.fireplay.settings.fireplay_scope, "view": "summary",
                         "asset_type": "video", "keyword": reference, "limit": 100, "pagination": "cursor"}
            for _ in range(5):
                result = requirement_payload(self.fireplay.mcp.requirement_list(arguments))
                for row in result["items"]:
                    if str(row.get("code", "")).casefold() == reference.casefold():
                        matches[str(UUID(row["id"]))] = row
                cursor = result.get("next_cursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in cursors:
                    raise ValueError("需求查询分页异常，请改用完整 UUID")
                cursors.add(cursor)
                arguments["cursor"] = cursor
            else:
                raise ValueError("需求号匹配超出查询上限，请使用完整 UUID")
            if len(matches) != 1:
                raise ValueError("未找到唯一的可访问视频需求，请核对完整需求号和权限")
            requirement_id = next(iter(matches))
        brief = safe_brief(self.fireplay.brief(requirement_id))
        if brief["id"] != requirement_id:
            raise Conflict("需求身份已变化，请重新查询")
        try:
            UUID(reference)
        except ValueError:
            if brief["code"].casefold() != reference.casefold():
                raise Conflict("需求号已变化，请重新查询")
        return brief

    def preview(self, reference):
        identity = self._identity()
        brief = self._resolve(reference)
        sources = sources_from_brief(brief)
        warnings = ["需求文本中的视频引用需要确认是原片；不会读取已交付成片作为输入。"]
        sources = self._library_sources(sources, warnings)
        if not brief["locales"]:
            warnings.append("该需求未填写目标语言，请补充后创建。")
        if not any(s["downloadable"] for s in sources):
            warnings.append("没有可直接导入的原片，请补传本地视频。")
        record = {"id": identifier(), "created_at": now(),
                  "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                  "identity": identity, "brief": brief, "brief_hash": digest(brief), "sources": sources}
        if identity != self._identity():
            raise Conflict("登录身份发生变化，请重新读取需求")
        self.store.put("intake_preview", record)
        return {"preview_id": record["id"], "expires_at": record["expires_at"],
                "requirement": {"id": brief["id"], "code": brief["code"], "title": brief["title"] or brief["code"],
                                "description": brief["description"] or "", "ref_text": brief["refText"] or "",
                                "locales": brief["locales"]},
                "sources": [{k: s[k] for k in ("id", "name", "kind", "downloadable", "reason", "origin_field") if k in s}
                            for s in sources],
                "suggested_target_locales": brief["locales"], "notes": "",
                "warnings": warnings, "needs_source_upload": not any(s["downloadable"] for s in sources)}

    def _library_sources(self, sources, warnings):
        from ..integrations.fireplay.library import LibraryLookupError, LibraryResolver
        result, seen = [], set()
        lookups = {}
        resolver = LibraryResolver(self.fireplay.mcp)
        for source in sources:
            video_id = source.get("youtube_id")
            if not video_id:
                result.append(source)
                continue
            if video_id not in lookups:
                if len(lookups) >= 10:
                    result.append({**source, "reason": "视频引用超过本次查找上限，请补传其余原片"})
                    continue
                try:
                    lookups[video_id] = resolver.resolve(video_id)
                except LibraryLookupError as exc:
                    warnings.append(str(exc))
                    lookups[video_id] = []
            matches = lookups[video_id]
            if not matches:
                result.append({**source, "reason": "未查到可用的素材库原视频，请补传本地原片"})
            for match in matches:
                identity = digest([video_id, match["id"], match["resource_id"]])[:24]
                if identity in seen:
                    continue
                seen.add(identity)
                result.append({"id": identity, "name": match["name"] or "素材库原视频", "kind": "library_video",
                               "downloadable": True, "url": match["url"], "youtube_id": video_id,
                               "material_id": match["id"], "resource_id": match["resource_id"],
                               "origin_field": source["origin_field"], "requires_confirmation": True})
        return result

    def _probe(self, path):
        tools = MediaTools(self.tasks.settings.ffmpeg, self.tasks.settings.ffprobe,
                           max_source_bytes=self.tasks.settings.max_upload_bytes)
        try:
            metadata = tools.probe(path)
        except MediaError:
            raise ValueError("无法解析原片，请检查视频格式或重新上传") from None
        if metadata["width"] <= 0 or metadata["height"] <= 0 or metadata["width"] * metadata["height"] > 33_177_600:
            raise ValueError("原片缺少有效视频画面或分辨率超出上限")
        if metadata["duration_ms"] > self.tasks.settings.max_duration_ms:
            raise ValueError("原片时长超过当前项目上限")
        return metadata

    def _receipt(self, store, body, payload_hash):
        receipt = store.get("intake_receipt", body.idempotency_key)
        if receipt:
            if receipt["payload_hash"] != payload_hash:
                raise Conflict("该创建标识已用于其他内容；请重新读取需求后创建")
            return self.tasks.detail(receipt["task_id"])
        return None

    def create(self, body: IntakeCreate, *, before_fetch=None, before_commit=None, download_hosts=None):
        payload_hash = digest(body.model_dump())
        previous = self._receipt(self.store, body, payload_hash)
        if previous:
            return previous
        if not self.lock.acquire(blocking=False):
            raise Conflict("需求任务正在准备，请稍后使用同一提交重试")
        try:
            previous = self._receipt(self.store, body, payload_hash)
            if previous:
                return previous
            return self._create(body, payload_hash, before_fetch=before_fetch,
                                before_commit=before_commit, download_hosts=download_hosts)
        finally:
            self.lock.release()

    def _create(self, body, payload_hash, *, before_fetch=None, before_commit=None, download_hosts=None):
        preview = self.store.require("intake_preview", body.preview_id)
        if preview["identity"] != self._identity():
            raise Conflict("登录身份已变化，请重新读取需求")
        if datetime.fromisoformat(preview["expires_at"]) < datetime.now(timezone.utc):
            raise Conflict("需求预览已过期，请重新读取")
        brief = safe_brief(self.fireplay.brief(preview["brief"]["id"]))
        if digest(brief) != preview["brief_hash"]:
            raise Conflict("平台需求已更新，请重新读取并检查原片和语言")
        if brief["locales"] and set(brief["locales"]) != set(body.target_locales):
            raise Conflict("目标语言必须与需求语言一致")
        by_id = {s["id"]: s for s in preview["sources"]}
        selected = []
        fresh_library = {}
        from ..integrations.fireplay.library import LibraryResolver
        resolver = LibraryResolver(self.fireplay.mcp)
        for source_id in body.source_ids:
            source = by_id.get(source_id)
            if not source or not source["downloadable"]:
                raise ValueError("所选原片不在当前需求的可用引用中")
            if source["kind"] == "library_video":
                video_id = source["youtube_id"]
                if video_id not in fresh_library:
                    fresh_library[video_id] = resolver.resolve(video_id)
                matches = [item for item in fresh_library[video_id] if item["id"] == source["material_id"]
                           and item["resource_id"] == source["resource_id"]]
                if len(matches) != 1:
                    raise Conflict("素材库原片已变化或不再可访问，请重新读取需求")
                source = {**source, "url": matches[0]["url"]}
            selected.append(source)
        actual_preview = {**preview, "sources": selected}
        for asset_id in body.asset_ids:
            asset = self.store.require("asset", asset_id)
            path = self.tasks.checked_path(asset)
            if self.tasks.hash_file(path) != asset["sha256"]:
                raise Conflict("本地原片内容已变化，请重新上传")
            self.probe(path)
        notes = "\n\n".join(v for v in [f"Fireplay需求 {brief['code']}", brief["description"],
                                          brief["refText"], body.notes.strip()] if v)
        if len(notes) > 8000:
            raise ValueError("需求说明与补充要求合计超过8000字，请精简后再创建")
        folder = self.tasks.settings.runtime / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        created_paths = []
        with TemporaryDirectory(prefix="intake-", dir=folder) as directory:
            from pathlib import Path
            downloads = []
            for index, source in enumerate(selected):
                if before_fetch is not None:
                    with self.store.connection() as db:
                        before_fetch(TransactionStore(self.store, db), actual_preview, body)
                destination = Path(directory) / str(index)
                destination.mkdir()
                download_policy = {"trusted_hosts": download_hosts} if download_hosts is not None else {}
                path = self.fetcher.fetch(source, destination, **download_policy)
                resolved = path.resolve()
                if not resolved.is_relative_to(destination.resolve()) or not resolved.is_file():
                    raise ValueError("原片下载位置无效")
                self.probe(resolved)
                downloads.append((source, resolved))
            # Revalidate the brief and identity after downloads, before work can be queued.
            if preview["identity"] != self._identity() or digest(safe_brief(self.fireplay.brief(brief["id"]))) != preview["brief_hash"]:
                raise Conflict("准备期间需求或登录身份已变化，请重新读取")
            try:
                with self.store.connection() as db:
                    store = TransactionStore(self.store, db)
                    existing = store.get("intake_receipt", body.idempotency_key)
                    if existing:
                        if existing["payload_hash"] != payload_hash:
                            raise Conflict("创建标识与已提交内容不同")
                        task_id = existing["task_id"]
                    else:
                        if before_commit is not None:
                            before_commit(store, actual_preview, body)
                        domain_tasks = TaskService(self.tasks.settings, store)
                        requirements = RequirementService(domain_tasks)
                        old = store.get("requirement", brief["id"])
                        if old and any(store.require("task", t)["status"] != "cancelled" for t in old["task_ids"]):
                            raise Conflict("该需求已有制作任务，请打开已有任务；如需重建，请先取消原任务")
                        sources = [{"asset_id": a} for a in body.asset_ids]
                        for source, path in downloads:
                            asset = domain_tasks.register_asset(path, source["name"] if source["name"].lower().endswith(path.suffix.lower()) else path.name)
                            stored = store.require("asset", asset["id"])
                            created_paths.append(domain_tasks.checked_path(stored))
                            sources.append({"asset_id": asset["id"], "source_id": source["id"]})
                        request_fields = {k: v for k, v in body.model_dump().items() if k in TaskCreate.model_fields}
                        request_fields.update(asset_ids=[s["asset_id"] for s in sources], notes=notes)
                        request = TaskCreate(**request_fields)
                        record = requirements.register(brief, sources, request.target_locales)
                        requirements.accept(record["id"])
                        task = domain_tasks.create(request, source="fireplay")
                        requirements.link_task(record["id"], task["id"])
                        store.put("intake_receipt", {"id": body.idempotency_key, "created_at": now(),
                                  "payload_hash": payload_hash, "task_id": task["id"], "requirement_id": record["id"]})
                        task_id = task["id"]
            except BaseException:
                for path in created_paths:
                    path.unlink(missing_ok=True)
                raise
        return self.tasks.detail(task_id)
