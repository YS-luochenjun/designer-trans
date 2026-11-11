"""Retire unsafe completion handoff; preserve all production evidence."""
import json
from copy import deepcopy
from video_translation.agent_runtime.resources import ResourceCatalog
from video_translation.persistence.store import now
from video_translation.workflow.graph import task_status
from .tasks import Conflict


def enable_completion(service, task_id):
    raise Conflict("有残字底片直接合成的路线已停用；请使用逐阶段质量检查")


def restore_stage_gates(service, task_id):
    task = service.store.require("task", task_id)
    if task["status"] in {"running", "cancelled", "completed"}:
        raise Conflict("任务必须停稳后再恢复质量检查")
    if task.get("production_policy", {}).get("mode") == "stage_verified":
        return {"task_id": task_id, "status": task["status"], "already_restored": True}
    if any(n["status"] in {"running", "unknown", "waiting_external"} or n.get("external_pending") for n in task["nodes"]):
        raise Conflict("先核对在途结果")
    sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
    if any(s.get("candidate") or s.get("pending_turn") is not None for s in sessions):
        raise Conflict("先处理原候选或当前轮")
    originals = {s["id"]: json.dumps(s, sort_keys=True) for s in sessions}
    for state in sessions:
        if any(d.get("task_id") == task_id and d.get("node_id") == state["node_id"]
               and d.get("key") == f"clean-{state['turn']}"
               and d.get("status") in {"running", "unknown", "succeeded"}
               for d in service.store.list("production_decision")):
            raise Conflict("当前轮存在未核对的Agent决策，先恢复原结果")
    original = json.dumps(task, sort_keys=True)
    previous_resources = deepcopy(task["resource_snapshot"])
    current = ResourceCatalog(service.settings.root).snapshot()
    ids = {"video-translation.methods", "video-translation.media-review", "video-translation.clean-quality",
           "video-translation.temporal-cleanup"}
    for kind, resources in current.items():
        for resource in resources:
            if resource["id"] in ids:
                task["resource_snapshot"][kind] = [
                    r for r in task["resource_snapshot"][kind] if r["id"] != resource["id"]
                ] + [resource]
    task.setdefault("resource_upgrades", []).append({
        "at": now(), "previous_snapshot": previous_resources,
        "reason": "User correction: clean plate before typography, restore independent stage checks",
    })
    task.setdefault("production_policy_history", []).append(deepcopy(task.get("production_policy", {})))
    task["production_policy"] = {"mode": "stage_verified", "updated_at": now(), "reason": "User rejected residual source lettering and inconsistent typography"}
    task.setdefault("production_feedback", []).append({
        "at": now(), "source": "user_quality_correction",
        "text": "原英文未清理、中文重叠，大小颜色和进场不匹配；动态文字不能按单张截图静止摆放。先完成并独立展示无字底片，再按运动证据制作译字。轻微发丝差异可接受，残字和重叠不接受。",
    })
    for node in task["nodes"]:
        if node["definition_id"] not in {"clean", "typography", "visual", "compose", "review", "deliver"}:
            continue
        node.setdefault("history", []).append({k: deepcopy(node.get(k)) for k in ("attempt", "status", "output", "artifact_ids", "error")})
        node.update(output={}, artifact_ids=[], finished_at=None, skip_reason=None)
        node.update(status="failed" if node["definition_id"] == "clean" else "pending", error="无字底片尚有原文残留，复核后才能译字" if node["definition_id"] == "clean" else None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as db:
        for session_id, snapshot in originals.items():
            row = db.execute("SELECT data FROM records WHERE kind='production_session' AND id=?",
                             (session_id,)).fetchone()
            if not row or json.dumps(json.loads(row[0]), sort_keys=True) != snapshot:
                raise Conflict("制作会话已变化，未修改")
        row = db.execute("SELECT data FROM records WHERE kind='task' AND id=?", (task_id,)).fetchone()
        if not row or json.dumps(json.loads(row[0]), sort_keys=True) != original:
            raise Conflict("任务已变化，未修改")
        db.execute("UPDATE records SET data=? WHERE kind='task' AND id=?", (json.dumps(task, ensure_ascii=False), task_id))
    return {"task_id": task_id, "status": task["status"], "mode": "stage_verified", "scheduled_new_calls": False}
