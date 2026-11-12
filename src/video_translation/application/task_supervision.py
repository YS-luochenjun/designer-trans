"""Task Route's durable reports and directions; program guards own state changes."""
from copy import copy, deepcopy

from .agent_recovery import replaced_with_authorization
from .collaboration import affected_nodes, node_for
from .tasks import Conflict
from ..domain.execution import identity
from ..domain.limits import allows_new_call
from ..execution.stage_recovery import invalidate_descendants
from ..execution.work_conclusions import capacity
from ..persistence.store import now
from ..persistence.transaction import TransactionStore
from ..workflow.graph import SUCCESS, task_status


def report_work(service, task_id, node_id, *, trigger=None, message_id=None):
    with service.store.lock:
        task = service.store.require("task", task_id)
        node = node_for(task, node_id)
        kind = trigger or ("work_completed" if node["status"] in SUCCESS else "work_blocked")
        if kind == "startup_quality_gate":
            for event in reversed(service.store.list("task_route_event")):
                if (event["task_id"] == task_id and event["node_id"] == node_id
                        and event["attempt"] == node["attempt"]
                        and event["trigger"] in {"work_blocked", "startup_quality_gate"}
                        and event["output_sha256"] == identity(node["output"])
                        and event["handoff"]["status"] == node["status"]):
                    return event
        key = identity({"task": task_id, "node": node_id, "attempt": node["attempt"],
                        "status": node["status"], "output": node["output"],
                        "trigger": kind, "message": message_id})
        previous = service.store.get("task_route_event", key)
        if previous:
            return previous
        return service.store.put("task_route_event", {
            "id": key, "task_id": task_id, "node_id": node_id, "trigger": kind,
            "attempt": node["attempt"], "output_sha256": identity(node["output"]),
            "message_id": message_id, "created_at": now(),
            "status": "acknowledged" if kind == "work_completed" else "queued",
            "handoff": {"stage": node["definition_id"], "status": node["status"],
                        "media_sha256": node["output"].get("sha256"),
                        "quality_passed": node["output"].get("quality_passed"),
                        "error": node.get("error")},
            "authority": "existing_task_limits", "quality_acceptance": False,
            "basis": plan_basis(service, task, node),
        })


def plan_basis(service, task, origin):
    return {
        "brief_revision": task.get("brief_revision", 0),
        "directions_sha256": identity(task.get("collaboration_rules", [])),
        "messages": sorted(m["id"] for m in service.store.list("conversation")
                           if m["task_id"] == task["id"]),
        "nodes": {n["id"]: identity({k: n.get(k) for k in ("attempt", "status", "output")})
                  for n in task["nodes"] if n["asset_id"] == origin["asset_id"]},
    }


def require_plan_basis(service, task, event, ids):
    basis = event.get("planning_basis") or event.get("basis")
    if not basis:
        raise Conflict("Route 计划缺少原版本依据")
    current = plan_basis(service, task, node_for(task, event["node_id"]))
    if (any(current[k] != basis[k] for k in ("brief_revision", "directions_sha256", "messages"))
            or any(current["nodes"].get(key) != basis["nodes"].get(key) for key in ids)):
        raise Conflict("计划期间要求、消息或相关 Work 已变化；保留原计划，等待最新交接")


def live_directions(service, task_id, node_id):
    task = service.store.require("task", task_id)
    return [r for r in task.get("collaboration_rules", []) if r.get("origin") == "task_route"
            and node_id in affected_nodes(task, r["node_id"], r["scope"])]


def record_direction(service, task_id, message_id):
    """Append user-authorized requirements without disturbing an in-flight Work."""
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        task = store.require("task", task_id)
        message = store.require("conversation", message_id)
        if message.get("direction"):
            return message["direction"]
        if task["status"] in {"cancelled", "completed"} or store.get("deletion_receipt", task_id):
            raise Conflict("已结束或删除中的任务仅保留沟通，制作方向未更改")
        if (message["task_id"] != task_id or message.get("interaction") != "route"
                or message["status"] != "succeeded" or message["reply"]["action"] != "direct"):
            raise Conflict("需要 Route 对用户制作指令的明确转达")
        selected = node_for(task, message["node_id"])
        target = node_for(task, message["reply"].get("target_node_id") or message["node_id"])
        if (not target or target["asset_id"] != selected["asset_id"]
                or target["locale"] not in {None, selected["locale"]}):
            raise Conflict("方向须交给当前素材和语言分支中的实际 Work")
        task["brief_revision"] = task.get("brief_revision", 0) + 1
        rule = {"message_id": message_id, "node_id": target["id"],
                "scope": message["scope"], "text": message["reply"]["proposed_instruction"],
                "revision": task["brief_revision"], "created_at": now(), "origin": "task_route"}
        task.setdefault("collaboration_rules", []).append(rule)
        receipt = {"id": message_id, "node_id": target["id"], "revision": rule["revision"],
                   "status": "pending_boundary", "at": now(), "quality_acceptance": False}
        message.update(applied_revision=rule["revision"], direction=receipt)
        store.put("task", task)
        store.put("conversation", message)
        local = copy(service)
        local.store = store
        report_work(local, task_id, target["id"], trigger="user_direction", message_id=message_id)
        return receipt


def acknowledge_directions(service, task_id, node_id, revision):
    with service.store.lock:
        for message in service.store.list("conversation"):
            receipt = message.get("direction")
            if (message["task_id"] == task_id and receipt
                    and receipt["revision"] <= revision
                    and node_id in affected_nodes(service.store.require("task", task_id),
                                                   receipt.get("node_id", message["node_id"]), message["scope"])):
                def received(m):
                    direction = m["direction"]
                    nodes = direction.setdefault("received_node_ids", [])
                    if node_id not in nodes:
                        nodes.append(node_id)
                    direction.update(status="received_by_work", received_node_id=node_id, received_at=now())
                service.store.update("conversation", message["id"], received)


def unread_directions(service, task_id, node_id):
    messages = {m["id"]: m for m in service.store.list("conversation") if m["task_id"] == task_id}
    return [r for r in live_directions(service, task_id, node_id)
            if r.get("message_id") in messages
            and node_id not in messages[r["message_id"]].get("direction", {}).get("received_node_ids", [])]


def pending_event(service, task_id):
    return next((e for e in reversed(service.store.list("task_route_event"))
                 if e["task_id"] == task_id and e["status"] in {"queued", "planned", "waiting_work"}), None)


def reconcile_work_reports(service, task_id):
    """Repair a missing handoff, without retrying Work or reopening a Route conclusion."""
    with service.store.lock:
        task = service.store.require("task", task_id)
        if (task.get("visual_route") != "agent_adaptive" or task.get("control_paused")
                or task["status"] in {"cancelled", "completed"}
                or service.store.get("deletion_receipt", task_id)):
            return []
        stopped = [n for n in task["nodes"] if n["status"] in {"failed", "interrupted"}]
        if not stopped:
            return []
        if any(m.get("task_id") == task_id and m.get("status") in {"queued", "running"}
               for m in service.store.list("conversation")):
            return []
        events = [e for e in service.store.list("task_route_event") if e["task_id"] == task_id]
        sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
        created = []
        for node in stopped:
            # An existing outcome, including blocked/unknown/superseded, is not a lost report.
            if any(e["node_id"] == node["id"] and e["attempt"] == node["attempt"] for e in events):
                continue
            if unsettled(service.store, task, {node["id"]}):
                continue
            if any(s["node_id"] == node["id"] and (
                s.get("candidate") or s.get("pending_turn") is not None
                or s.get("archive_review") or s.get("pipeline_review")
                or any(a.get("needs_scope_review") for a in s.get("accepted", []))
            ) for s in sessions):
                continue
            event = report_work(service, task_id, node["id"])
            events.append(event)
            created.append(event)
        return created


def unsettled(store, task, ids):
    if any(n["id"] in ids and (n["status"] in {"running", "unknown", "waiting_external"}
                              or n.get("external_pending")) for n in task["nodes"]):
        return True
    return any(r.get("task_id") == task["id"] and r.get("node_id") in ids
               and r.get("status") in {"running", "unknown", "invalid_response", "pending", "claimed", "verifying"}
               and not replaced_with_authorization(store, r, kind)
               for kind in ("operation", "production_decision", "imagegen_request", "task_route_decision")
               for r in store.list(kind))


def request_facts(store, task, ids):
    """Bounded request identities; retained unknown costs stay distinct from blockers."""
    active, retained = [], []
    for kind in ("operation", "production_decision", "imagegen_request", "route_run", "route_probe"):
        for row in store.list(kind):
            if (row.get("task_id") != task["id"] or row.get("node_id") not in ids
                    or row.get("status") not in {
                        "running", "unknown", "invalid_response", "pending", "claimed", "verifying"}):
                continue
            replaced = replaced_with_authorization(store, row, kind)
            item = {k: row.get(k) for k in (
                "id", "node_id", "status", "key", "stage", "created_at", "cost_status")}
            item["kind"] = kind
            item["replacement_authorization_id"] = row.get("replacement_authorization_id")
            (retained if replaced else active).append(item)
    local = [{k: row.get(k) for k in (
        "id", "node_id", "status", "original_attempt", "output_sha256", "finished_at")}
             for row in store.list("materialization_recovery")
             if row.get("task_id") == task["id"] and row.get("node_id") in ids]
    return {
        "unsettled_count": len(active), "unsettled_requests": active[-12:],
        "authorized_historical_unknown_count": len(retained),
        "authorized_historical_unknown_requests": retained[-12:],
        "local_output_recoveries": local[-6:],
        "meaning": "Applied replacements retain unknown costs. Completed local exports do not own later cleanup.",
    }


def apply_plan(service, event_id, plan):
    """Apply a once-only rework plan inside the original budget and branch."""
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        local = copy(service)
        local.store = store
        event = store.require("task_route_event", event_id)
        if event["status"] == "applied":
            return event
        task = store.require("task", event["task_id"])
        origin = node_for(task, event["node_id"])
        node = node_for(task, plan.get("node_id"))
        if (not node or node["asset_id"] != origin["asset_id"]
                or node["locale"] not in {None, origin["locale"]}
                or node["definition_id"] in {"prepare", "deliver", "review"}):
            raise Conflict("Route 返工须选择同素材和语言的实际制作节点")
        if task.get("control_paused") or task["status"] in {"cancelled", "completed"}:
            raise Conflict("任务已暂停或结束，Route 保留计划等待用户恢复")
        if store.get("deletion_receipt", task["id"]):
            raise Conflict("任务正在删除")
        if origin["attempt"] != event["attempt"] or identity(origin["output"]) != event["output_sha256"]:
            raise Conflict("原 Work 报告已过期，不能重排新结果")
        descendants = affected_nodes(task, node["id"], "downstream")
        require_plan_basis(local, task, event, descendants | {origin["id"]})
        if any(m["task_id"] == task["id"] and m["status"] in {"queued", "running"}
               for m in store.list("conversation")):
            raise Conflict("先处理用户已发送的消息，再决定返工")
        if unsettled(store, task, descendants):
            raise Conflict("原分支仍有在途或未知结果，先核原请求")
        if any(n["definition_id"] == "deliver" and n["id"] in descendants and n["status"] == "succeeded"
               for n in task["nodes"]):
            raise Conflict("已交付版本需独立定向返工")
        report = capacity(task)
        remaining = report["remaining_external_calls"]
        branches = [node["asset_id"] + ":" + locale for locale in (
            [origin["locale"]] if origin["locale"] else task["target_locales"])]
        if (not allows_new_call(task, "max_paid_calls", task.get("paid_calls", 0))
                or remaining is not None and remaining <= report["downstream_model_call_reserve"]):
            raise Conflict("原调用额度不足，Route 不能扩额或清零用量")
        if any(task["revisions"].get(branch, 0) >= task["max_revisions"] for branch in branches):
            raise Conflict("已达到原任务返工上限，候选和未完成项保留")
        if node["definition_id"] == "clean":
            _reopen_clean(local, task, node, event, plan)
        node.setdefault("history", []).append({
            **{k: deepcopy(node.get(k)) for k in ("attempt", "status", "output", "artifact_ids", "error")},
            "reason": "Task Route applied bounded rework", "route_event_id": event_id})
        review_defects = (deepcopy(origin["output"].get("defects", []))
                          if origin["definition_id"] == "review" else [])
        invalidate_descendants(local, task, node)
        node.update(status="pending", error=None, finished_at=None, stage_readiness="needs_repair")
        for branch in branches:
            task["revisions"][branch] = task["revisions"].get(branch, 0) + 1
            if review_defects:
                task.setdefault("revision_feedback", {})[branch] = deepcopy(review_defects)
        task.setdefault("collaboration_rules", []).append({
            "node_id": node["id"], "scope": "downstream", "text": plan["instruction"],
            "revision": task.get("brief_revision", 0), "origin": "task_route",
            "route_event_id": event_id, "created_at": now()})
        task.update(status=task_status(task), updated_at=now())
        event.update(status="applied", plan=plan, applied_at=now(), scheduled_node_id=node["id"])
        store.put("task", task)
        store.put("task_route_event", event)
        return event


def _reopen_clean(service, task, node, event, plan):
    from .convergence_recovery import _media_evidence
    sessions = [s for s in service.store.list("production_session")
                if s["task_id"] == task["id"] and s["node_id"] == node["id"]]
    if len(sessions) != 1:
        raise Conflict("返工需唯一原清底会话")
    state = sessions[0]
    if (state.get("candidate") or state.get("pending_turn") is not None
            or state.get("archive_review") or state.get("pipeline_review")
            or any(a.get("needs_scope_review") for a in state.get("accepted", []))):
        raise Conflict("先处理原候选或范围争议")
    if any(d.get("task_id") == task["id"] and d.get("node_id") == node["id"]
           and d.get("key") == f"clean-{state['turn']}" and d["status"] in {
               "running", "unknown", "succeeded", "invalid_response"}
           for d in service.store.list("production_decision")):
        raise Conflict("原当前轮决策尚未结清")
    asset = service.store.require("asset", node["asset_id"])
    source = service.checked_path(asset)
    if service.hash_file(source) != asset["sha256"]:
        raise Conflict("原片身份变化")
    try:
        _media_evidence(service, task, state, source)
    except (OSError, KeyError, ValueError) as exc:
        raise Conflict("原清底媒体或依赖已变化") from exc
    if event.get("execution_authorization_id"):
        from .clean_execution_authorization import begin
        begin(service, state, event)
        state["finished"] = False
        service.store.put("production_session", state)
        return
    if state.get("draft_handoff"):
        state.setdefault("draft_preview_archive", []).append(state.pop("draft_handoff"))
    if state.get("clean_review"):
        state.setdefault("clean_review_archive", []).append(state.pop("clean_review"))
    state["finished"] = False
    state.pop("routing_stop", None)
    state.pop("completion_feedback_stop", None)
    state.setdefault("work_feedback", {})[event["id"]] = {
        "id": event["id"], "trigger": "user_continuation" if event["trigger"] in {
            "user_direction", "direction_after_work", "human_rejected", "user_answer"} else "route_rework",
        "reason": plan["instruction"], "source_id": None, "turn": state["turn"],
        "base_sha256": state["base"]["sha256"], "status": "pending", "quality_passed": False}
    service.store.put("production_session", state)
