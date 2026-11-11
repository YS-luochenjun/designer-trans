from pathlib import Path
from datetime import datetime, timezone

import yaml

from video_translation.persistence.store import identifier


SUCCESS = {"succeeded", "skipped"}


def load_definition(root: Path, *, adaptive: bool = False) -> dict:
    filename = "adaptive.yaml" if adaptive else "workflow.yaml"
    definition = yaml.safe_load((root / "workflows/video_translation" / filename).read_text("utf-8"))
    seen = set()
    for node in definition["nodes"]:
        if node["id"] in seen or not set(node["depends_on"]).issubset(seen):
            raise ValueError("工作流含重复节点、前向引用或环")
        seen.add(node["id"])
    return definition


def instantiate(definition: dict, assets: list[str], locales: list[str]) -> list[dict]:
    result = []
    for asset in assets:
        ids = {}
        for spec in definition["nodes"]:
            branches = [None] if spec["scope"] == "source" else locales
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


def ready_nodes(task: dict) -> list[dict]:
    if task.get("control_paused"):
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
