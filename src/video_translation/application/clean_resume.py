"""Resume a stopped clean node only when accepted work supersedes its obstacle."""
import json

from video_translation.execution.clean_progress import reconsider_accepted_source
from video_translation.persistence.store import now
from video_translation.workflow.graph import task_status
from .tasks import Conflict


def resume_clean(service, task_id):
    # Import locally: constructing the controller reads prerequisites, not models.
    from video_translation.execution.adaptive import AdaptiveProduction
    from video_translation.execution.nodes import NodeExecutor

    task = service.store.require("task", task_id)
    original_task = json.dumps(task, sort_keys=True)
    if task["status"] != "blocked" or task.get("control_paused"):
        raise Conflict("仅恢复阻塞且未被用户暂停的任务")
    nodes = [n for n in task["nodes"] if n["definition_id"] == "clean" and n["status"] == "failed"]
    if len(nodes) != 1:
        raise Conflict("需要一个明确失败的清底节点")
    node = nodes[0]
    sessions = [s for s in service.store.list("production_session") if s["node_id"] == node["id"]]
    if len(sessions) != 1:
        raise Conflict("需要一个明确的制作会话")
    state = sessions[0]
    original_state = json.dumps(state, sort_keys=True)
    if state.get("candidate") or state.get("pending_turn") is not None or node.get("external_pending"):
        raise Conflict("先处理原候选或在途操作，不启动新的制作")
    decisions = service.store.list("production_decision")
    if any(d.get("task_id") == task_id and d.get("node_id") == node["id"]
           and d.get("key") == f"clean-{state['turn']}"
           and d.get("status") in {"running", "unknown", "succeeded"} for d in decisions):
        raise Conflict("本轮已有Agent操作或尚未应用的结果，先恢复原操作")
    controller = AdaptiveProduction(NodeExecutor(service), task, node)
    for source_id, override in state.get("text_overrides", {}).items():
        controller.texts[source_id].update(override)
    reopened = []
    for accepted in reversed(state["accepted"]):
        source_id = accepted["source_id"]
        obstacle = state.get("deferred", {}).get(source_id)
        if not obstacle or accepted.get("needs_scope_review"):
            continue
        acceptance_turn = next((h["turn"] for h in reversed(state["history"])
                                if h["decision"].get("action") == "accept"
                                and h["decision"].get("candidate_id") == accepted["id"]
                                and not h.get("tool_error")), -1)
        if acceptance_turn <= obstacle.get("turn", acceptance_turn):
            continue
        if reconsider_accepted_source(controller.texts, state, accepted):
            reopened.append(source_id)
    scope_review = None
    if any(a.get("needs_scope_review") for a in state["accepted"]):
        from video_translation.execution.scope_review import pending_scope_review
        _, clock = controller.media._info(controller.source)
        scope_review = pending_scope_review(controller.texts, state, clock)
    if not reopened and not scope_review:
        raise Conflict("没有新通过的修补能解除旧障碍；需补充方法或证据，不重复空转")
    node.update(status="pending", error=None, finished_at=None)
    task["status"] = task_status(task)
    task["updated_at"] = now()
    state.setdefault("resume_events", []).append({
        "at": now(), "sources": reopened, "scope_review": scope_review,
        "reason": "Accepted repairs supersede older deferrals or need bounded scope review",
    })
    # Commit session and scheduling together; do not overwrite a concurrent pause.
    with service.store.connection() as db:
        for kind, record_id, original in (
            ("task", task_id, original_task), ("production_session", state["id"], original_state),
        ):
            current = db.execute("SELECT data FROM records WHERE kind=? AND id=?",
                                 (kind, record_id)).fetchone()
            if not current or json.dumps(json.loads(current[0]), sort_keys=True) != original:
                raise Conflict("任务状态已变化，未恢复制作")
        for kind, row in (("task", task), ("production_session", state)):
            db.execute("UPDATE records SET data=? WHERE kind=? AND id=?",
                       (json.dumps(row, ensure_ascii=False), kind, row["id"]))
    return {"task_id": task_id, "status": task["status"], "reconsidered_sources": reopened,
            "scope_review": scope_review}
