"""Translation task kinds and source-by-locale workspace projections."""
from __future__ import annotations

KINDS = (
    {"id": "video_translation", "label": "视频翻译", "execution": "ready",
     "description": "沿用当前 Route / Work 视频制作流程。"},
    {"id": "image_translation", "label": "图片翻译", "execution": "ready",
     "description": "Route / Work 管理来源图组、逐语言制作及图片审核。"},
    {"id": "face_swap", "label": "视频换脸", "execution": "external_tool",
     "description": "来源脸、目标人物和参数直接交给 FaceFusion 执行。"},
)


def workspace(task: dict, assets: list[dict]) -> dict:
    kind = task.get("task_kind", "video_translation")
    result = {
        "task_kind": kind,
        "execution": task.get("workflow", {}).get("execution", "ready"),
        "quality_passed": False,
    }
    if kind == "image_translation":
        nodes = task["nodes"]
        members = []
        for source_id in task["asset_ids"]:
            for locale in task["target_locales"]:
                review = next(n for n in nodes if n["definition_id"] == "image_review"
                              and n["asset_id"] == source_id and n["locale"] == locale)
                members.append({
                    "id": review["id"], "source_id": source_id, "locale": locale,
                    "status": review["status"], "artifact_id": review["output"].get("artifact_id"),
                })
        result.update(source_count=len(task["asset_ids"]), locale_count=len(task["target_locales"]),
                      member_count=len(members), members=members,
                      bundles=[{"locale": locale, "expected_source_ids": task["asset_ids"],
                                "node_id": next(n["id"] for n in nodes
                                                if n["definition_id"] == "deliver" and n["locale"] == locale)}
                               for locale in task["target_locales"]])
    return result
