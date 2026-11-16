from pathlib import Path
from datetime import datetime, timezone

import yaml

from video_translation.persistence.store import identifier
from video_translation.domain.task_types import uses_route_work


SUCCESS = {"succeeded", "skipped"}


def load_definition(root: Path, *, adaptive: bool = False, task_kind: str = "video_translation") -> dict:
    if task_kind not in {"video_translation", "image_translation", "face_swap"}:
        raise ValueError("未知任务类型")
    adaptive = adaptive and task_kind == "video_translation"
    filename = "adaptive.yaml" if adaptive else "workflow.yaml"
    definition = yaml.safe_load((root / "workflows" / task_kind / filename).read_text("utf-8"))
    seen = set()
    for node in definition["nodes"]:
        if node["id"] in seen or not set(node["depends_on"]).issubset(seen):
            raise ValueError("工作流含重复节点、前向引用或环")
        seen.add(node["id"])
    return definition


def instantiate(definition: dict, assets: list[str], locales: list[str]) -> list[dict]:
    if definition.get("executor") == "facefusion":
        return []
    if any(spec["scope"] in {"task", "locale_bundle"} for spec in definition["nodes"]):
        return _instantiate_grouped(definition, assets, locales)
    result = []
    for asset in assets:
        ids = {}
        for spec in definition["nodes"]:
            if spec["scope"] == "source":
                branches = [None]
            elif spec["scope"] == "locale":
                branches = locales
            else:
                raise ValueError(f"不支持的工作流范围: {spec['scope']}")
            for locale in branches:
                node_id = identifier()
                ids[(spec["id"], locale)] = node_id
                dependencies = [ids.get((dep, locale)) or ids[(dep, None)] for dep in spec["depends_on"]]
                result.append(
                    {
                        "id": node_id,
                        "definition_id": spec["id"],
                        "label": spec["label"],
                        "stage": spec["stage"],
                        "locale": locale,
                        "asset_id": asset,
                        "status": "pending",
                        "depends_on": dependencies,
                        "attempt": 0,
                        "started_at": None,
                        "finished_at": None,
                        "error": None,
                        "output": {},
                        "artifact_ids": [],
                        "skip_reason": None,
                    }
                )
    return result


def _instantiate_grouped(definition, assets, locales):
    result, indexed = [], {}
    for spec in definition["nodes"]:
        scope = spec["scope"]
        if scope == "task":
            branches = [(assets[0], None)]
        elif scope == "source":
            branches = [(asset, None) for asset in assets]
        elif scope == "locale":
            branches = [(asset, locale) for asset in assets for locale in locales]
        elif scope == "locale_bundle":
            branches = [("", locale) for locale in locales]
        else:
            raise ValueError(f"不支持的工作流范围: {scope}")
        for asset, locale in branches:
            dependencies = []
            for dependency in spec["depends_on"]:
                matches = [
                    item for item in indexed[dependency]
                    if (not asset or not item["asset_id"] or item["asset_id"] == asset)
                    and (item["locale"] is None or item["locale"] == locale)
                ]
                if not matches:
                    raise ValueError(f"工作流依赖范围不匹配: {spec['id']} -> {dependency}")
                dependencies.extend(item["id"] for item in matches)
            node = {
                "id": identifier(), "definition_id": spec["id"], "label": spec["label"],
                "stage": spec["stage"], "locale": locale, "asset_id": asset,
                "scope": scope, "status": "pending", "depends_on": dependencies,
                "attempt": 0, "started_at": None, "finished_at": None,
                "error": None, "output": {}, "artifact_ids": [], "skip_reason": None,
            }
            indexed.setdefault(spec["id"], []).append(node)
            result.append(node)
    return result


def ready_nodes(task: dict) -> list[dict]:
    if (not uses_route_work(task) or task.get("control_paused")
            or task.get("workflow", {}).get("execution") == "draft_only"):
        return []
    states = {node["id"]: node["status"] for node in task["nodes"]}
    current_time = datetime.now(timezone.utc).isoformat()
    return [
        node
        for node in task["nodes"]
        if (node["status"] in {"pending", "queued"}
            or node["status"] == "waiting_external" and node.get("external_pending") is True
            and bool(node.get("next_poll_at")) and node["next_poll_at"] <= current_time)
        and all(states[dep] in SUCCESS for dep in node["depends_on"])
    ]


def task_status(task: dict) -> str:
    if task["status"] == "cancelled":
        return "cancelled"
    if not uses_route_work(task):
        state = task.get("external_execution", {}).get("state", "configuration_required")
        if task.get("control_paused") and state != "running":
            return "waiting_user"
        return {"pending": "queued", "running": "running", "succeeded": "waiting_user",
                "approved": "completed", "failed": "blocked", "unknown": "blocked",
                "configuration_required": "draft"}.get(state, "draft")
    if task.get("workflow", {}).get("execution") == "draft_only":
        return "draft"
    nodes = task["nodes"]
    deliveries = [node for node in nodes if node["definition_id"] == "deliver"]
    done = sum(node["status"] == "succeeded" for node in deliveries)
    if deliveries and done == len(deliveries):
        return "completed"
    if any(node["status"] in {"running", "waiting_external"} for node in nodes) or ready_nodes(task):
        return "running"
    if task.get("control_paused"):
        return "waiting_user"
    if done:
        return "partial_completed"
    if any(node["status"] == "waiting_user" for node in nodes):
        return "waiting_user"
    if any(node["status"] in {"failed", "unknown"} for node in nodes):
        return "blocked"
    if any(node["status"] == "interrupted" for node in nodes):
        return "interrupted"
    return "queued"
