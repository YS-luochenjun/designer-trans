"""User-clicked cleanup continuation; never a side effect of ordinary discussion."""
from copy import copy, deepcopy

from .agent_recovery import replaced_with_authorization
from .collaboration import Collaboration, affected_nodes, node_for
from .convergence_recovery import _media_evidence
from .tasks import Conflict
from ..domain.execution import identity
from ..domain.limits import allows_new_call
from ..execution.stage_recovery import invalidate_descendants
from ..execution.review_draft import complete_candidate
from ..execution.work_conclusions import capacity
from ..persistence.store import now
from ..persistence.transaction import TransactionStore
from ..workflow.graph import SUCCESS, task_status


def continue_clean(service, task_id, message_id, request):
    """Caller holds Worker.guard; applying requirements and queuing commit together."""
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        task = tx.require("task", task_id)
        message = tx.require("conversation", message_id)
        if message["task_id"] != task_id:
            raise KeyError(message_id)
        if message.get("continuation"):
            return message["continuation"]
        if not complete_candidate(task):
            raise Conflict("此继续入口仅用于完整候选目标的清底，其他目标保留原受控返工流程")
        node = node_for(task, message["node_id"])
        if (not node or node["definition_id"] != "clean"
                or node["status"] not in {"failed", "interrupted", "succeeded"}
                or node["status"] == "succeeded"
                and node.get("output", {}).get("quality_passed") is not False):
            raise Conflict("这里只能继续停稳且尚未合格的清底节点")
        if (task["status"] in {"completed", "cancelled"} or not task.get("control_paused")
                or tx.get("deletion_receipt", task_id)):
            raise Conflict("先暂停并等待当前制作停稳；已结束或删除中的任务不能继续")
        if (task.get("brief_revision", 0) != request.expected_revision
                or node["attempt"] != request.expected_attempt
                or node["status"] != request.expected_status
                or node.get("output", {}).get("sha256") != request.expected_output_sha256):
            raise Conflict("节点或要求已变化，请刷新后核对当前建议")
        if message["status"] != "succeeded" or not (message.get("reply") or {}).get("proposed_instruction"):
            raise Conflict("需要已回复的具体制作建议")
        for kind in ("operation", "production_decision", "imagegen_request", "route_run", "route_probe"):
            if any(r.get("task_id") == task_id and r.get("status") in {
                    "running", "unknown", "invalid_response", "pending", "claimed", "verifying"}
                   and not replaced_with_authorization(tx, r, kind) for r in tx.list(kind)):
                raise Conflict("先核对原外部请求；继续制作不会重发未知调用")
        if (any(n["status"] in {"running", "waiting_external", "unknown"} or n.get("external_pending")
                for n in task["nodes"])
                or any(m["task_id"] == task_id and m["status"] in {"queued", "running"}
                       for m in tx.list("conversation"))):
            raise Conflict("先等待原制作或其他消息处理完毕")
        if any(n["status"] not in SUCCESS for n in task["nodes"] if n["id"] in node["depends_on"]):
            raise Conflict("清底上游尚未完成，不能跳过前置节点")
        descendants = affected_nodes(task, node["id"], "downstream")
        if any(n["id"] in descendants and n["definition_id"] == "deliver"
               and n["status"] == "succeeded" for n in task["nodes"]):
            raise Conflict("该底片已有交付版本，需使用定向返工")
        report = capacity(task)
        remaining = report["remaining_external_calls"]
        if (not allows_new_call(task, "max_paid_calls", task.get("paid_calls", 0))
                or remaining is not None and remaining <= report["downstream_model_call_reserve"]):
            raise Conflict("原调用额度不足以继续清底并保留后续制作，未增加预算或清零用量")
        sessions = [s for s in tx.list("production_session")
                    if s["task_id"] == task_id and s["node_id"] == node["id"]]
        if len(sessions) != 1:
            raise Conflict("需要唯一的原清底会话，不能另建会话丢失已接受内容")
        state = sessions[0]
        if (state.get("finished") or state.get("candidate") or state.get("pending_turn") is not None
                or state.get("archive_review") or state.get("pipeline_review")
                or any(a.get("needs_scope_review") for a in state.get("accepted", []))):
            raise Conflict("先处理原候选、范围争议或当前轮结果")
        if any(d.get("task_id") == task_id and d.get("node_id") == node["id"]
               and d.get("key") == f"clean-{state['turn']}"
               and d.get("status") in {"running", "unknown", "succeeded", "invalid_response"}
               for d in tx.list("production_decision")):
            raise Conflict("当前轮存在尚未应用的原决策，不能另排一次调用")
        base = state.get("base") or {}
        try:
            asset = tx.require("asset", node["asset_id"])
            source = service.checked_path(asset)
            if node["asset_id"] not in task["asset_ids"] or service.hash_file(source) != asset["sha256"]:
                raise Conflict("原片身份已变化，不能沿用旧清底会话")
            _media_evidence(service, task, state, source)
        except (OSError, KeyError, ValueError) as exc:
            raise Conflict("原底片或依赖缺失或已变化，先核对媒体证据") from exc
        local = copy(service)
        local.store = tx
        local.collaboration = Collaboration(local)
        if message.get("applied_revision") is None:
            message = local.collaboration.apply(task_id, message_id)
            task = tx.require("task", task_id)
            node = node_for(task, message["node_id"])
        elif (message["applied_revision"] != task.get("brief_revision")
              or not any(r.get("message_id") == message_id
                         and r.get("revision") == message["applied_revision"]
                         for r in task.get("collaboration_rules", []))):
            raise Conflict("该建议后已有其他要求，请依据最新要求继续")
        before = {k: deepcopy(node.get(k)) for k in (
            "attempt", "status", "output", "artifact_ids", "error")}
        node.setdefault("history", []).append({
            **before, "reason": "User explicitly continued cleanup from a discussion proposal",
            "message_id": message_id})
        invalidate_descendants(local, task, node)
        draft = state.pop("draft_handoff", None)
        if draft:
            state.setdefault("draft_preview_archive", []).append(draft)
        state.pop("routing_stop", None)
        state.pop("completion_feedback_stop", None)
        key = identity({"message_id": message_id, "revision": message["applied_revision"],
                        "base_sha256": base["sha256"], "node_id": node["id"]})
        state.setdefault("work_feedback", {})[key] = {
            "id": key, "trigger": "user_continuation", "message_id": message_id,
            "reason": message["reply"]["proposed_instruction"], "source_id": None,
            "turn": state["turn"], "base_sha256": base["sha256"], "status": "pending",
            "quality_passed": False,
        }
        node.update(status="pending", stage_readiness="needs_repair", error=None, finished_at=None)
        task["control_paused"] = False
        task.update(status=task_status(task), updated_at=now())
        result = {"task_id": task_id, "node_id": node["id"], "message_id": message_id,
                  "status": "queued", "at": now(), "revision": message["applied_revision"],
                  "base_sha256": base["sha256"], "feedback_id": key,
                  "scheduled_node_ids": [node["id"]], "invalidated_node_ids": sorted(descendants - {node["id"]}),
                  "usage_unchanged": True, "quality_acceptance": False}
        message["continuation"] = result
        tx.put("production_session", state)
        tx.put("task", task)
        tx.put("conversation", message)
        return result
