"""Verify every image approval and the exact bytes inside its language package."""
from __future__ import annotations

import hashlib
import zipfile

from .tasks import Conflict


def reviewed_members(service, task, delivery, require):
    locale = delivery["locale"]
    members = delivery.get("members", [])
    expected = set(task["asset_ids"])
    if (locale not in task["target_locales"] or len(members) != len(expected)
            or {member["asset_id"] for member in members} != expected
            or delivery.get("quality_status") != "human_approved"):
        raise Conflict("图片语言包必须逐一覆盖当前来源和语言")
    bundle = require("artifact", delivery["artifact_id"])
    if (bundle["task_id"] != task["id"] or bundle["locale"] != locale
            or bundle["sha256"] != delivery.get("sha256")
            or service.hash_file(service.checked_path(bundle)) != bundle["sha256"]):
        raise Conflict("图片语言包文件身份已变化")
    names = [member.get("archive_name") for member in members]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise Conflict("图片语言包成员名称缺失或重复")
    try:
        with zipfile.ZipFile(service.checked_path(bundle)) as archive:
            if sorted(archive.namelist()) != sorted(names):
                raise Conflict("图片语言包内的实际成员与登记不一致")
            for member in members:
                artifact = require("artifact", member["artifact_id"])
                reviews = [node for node in task["nodes"] if node["definition_id"] == "image_review"
                           and node["asset_id"] == member["asset_id"] and node["locale"] == locale]
                if (len(reviews) != 1 or reviews[0]["status"] != "succeeded"
                        or reviews[0]["output"].get("quality_status") != "human_approved"
                        or reviews[0]["output"].get("approved_sha256") != member["sha256"]
                        or reviews[0]["output"].get("artifact_id") != member["artifact_id"]
                        or artifact["task_id"] != task["id"] or artifact["locale"] != locale
                        or artifact["sha256"] != member["sha256"]
                        or service.hash_file(service.checked_path(artifact)) != member["sha256"]):
                    raise Conflict("图片成员与当前人工批准版本不一致")
                info = archive.getinfo(member["archive_name"])
                if info.file_size != artifact["size_bytes"]:
                    raise Conflict("语言包成员大小与已批准图片不一致")
                with archive.open(info) as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != member["sha256"]:
                        raise Conflict("语言包中的图片字节与人工批准版本不一致")
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise Conflict("无法核实图片语言包实际内容") from exc
    return members
