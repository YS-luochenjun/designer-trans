"""Local maintenance facts and review candidates, independent of production workers."""
from __future__ import annotations

import re
import shutil

from video_translation.domain.execution import identity
from video_translation.execution.workspace import storage_inventory
from video_translation.persistence.store import now
from .tasks import Conflict

INTENTS = {"storage", "cleanup_plan", "knowledge_summary"}


def maintenance_intent(text):
    if any(token in text for token in ("总结经验", "知识总结", "总结任务")):
        intent = "knowledge_summary"
    elif any(token in text for token in ("清理计划", "回收计划", "清理项目", "清理任务")):
        intent = "cleanup_plan"
    elif any(token in text for token in ("检查空间", "磁盘占用", "为什么占空间", "查看空间")):
        intent = "storage"
    else:
        return None
    ids = set(re.findall(r"(?<![a-fA-F0-9])[a-fA-F0-9]{32}(?![a-fA-F0-9])", text))
    return {"intent": intent, "task_id": next(iter(ids)) if len(ids) == 1 else None,
            "ambiguous": len(ids) > 1}


class ProjectMaintenance:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def target(self, task_id=None):
        if task_id:
            return self.store.require("task", task_id.lower())
        tasks = [row for row in self.store.list("task") if row["status"] != "cancelled"]
        if len(tasks) != 1:
            raise Conflict("请指定一个任务 ID；维护查询不会自行选择或新建任务")
        return tasks[0]

    def storage(self, task_id):
        task = self.target(task_id)
        root = self.service.settings.runtime.resolve() / "tasks" / task["id"]
        inventory = storage_inventory(root)
        route = storage_inventory(self.service.settings.runtime.resolve() / "route" / task["id"])
        logical_bytes = inventory["logical_bytes"] + route["logical_bytes"]
        files = inventory["files"] + route["files"]
        skipped = inventory["skipped"] + route["skipped"]
        return {
            "task_id": task["id"], "status": task["status"], "paused": task.get("control_paused", False),
            "logical_bytes": logical_bytes, "files": files, "groups": inventory["groups"],
            "route_groups": route["groups"], "task_logical_bytes": inventory["logical_bytes"],
            "route_logical_bytes": route["logical_bytes"],
            "scan_complete": not (skipped or inventory["truncated"] or route["truncated"]),
            "scan_errors": skipped, "scope": "task and task-local Route files; logical file lengths",
            "disk_free_bytes": shutil.disk_usage(self.service.settings.runtime).free,
            "resource_budget": task.get("execution_snapshot", {}).get("resource_budget"),
            "call_count": task.get("paid_calls", 0), "call_count_is_money": False,
            "deleted_files": 0, "scheduled_new_calls": False,
        }

    def summarize(self, task_id):
        task = self.target(task_id)
        kinds = ("production_decision", "operation", "agent_trace", "production_session",
                 "route_run", "route_probe", "storage_retirement")
        rows = [(kind, row) for kind in kinds for row in self.store.list(kind)
                if row.get("task_id") == task["id"]]
        counts, refs = {}, []
        for kind, row in rows:
            state = row.get("status", "recorded")
            key = f"{kind}:{state}"
            counts[key] = counts.get(key, 0) + 1
            refs.append({"kind": kind, "id": row["id"], "sha256": identity(row)})
        basis = identity({"task": task, "evidence": refs})
        candidate_id = identity(["maintenance-summary-v1", task["id"], basis])
        previous = self.store.get("knowledge_candidate", candidate_id)
        if previous:
            return previous
        candidate = {
            "id": candidate_id, "task_id": task["id"], "created_at": now(),
            "basis_sha256": basis, "review_status": "candidate", "scope": "single_task_case",
            "observations": {"status": task["status"], "paused": task.get("control_paused", False),
                             "call_count": task.get("paid_calls", 0), "evidence_counts": counts},
            "source_refs": refs, "hypotheses": [], "counterexamples": [],
            "limitations": ["State counts do not establish a root cause, visual quality or actual cost.",
                            "Single-task facts are not general production knowledge."],
            "active_production_knowledge": False, "scheduled_new_calls": False,
        }
        return self.store.put("knowledge_candidate", candidate)

    def query(self, intent, task_id=None):
        task = self.target(task_id)
        if intent == "storage":
            result = self.storage(task["id"])
            reply = (f"任务现场逻辑大小 {result['logical_bytes'] / 1024**3:.2f} GiB，"
                     f"{result['files']} 个文件；所在卷余量 {result['disk_free_bytes'] / 1024**3:.2f} GiB。"
                     "这是占用查询，未清理、未恢复制作。")
        elif intent == "cleanup_plan":
            from .task_storage import history_plan
            plan = history_plan(self.service, task["id"], hash_candidates=False)
            result = {key: plan[key] for key in (
                "task_id", "estimated_reclaim_bytes", "task_logical_bytes", "impact", "hashes_complete")}
            result.update(candidate_video_count=len(plan["files"]),
                          protected_video_count=len(plan["protected_videos"]), executed=False)
            reply = (f"历史视频初筛约可回收 {plan['estimated_reclaim_bytes'] / 1024**3:.2f} GiB。"
                     "尚未执行；执行前须生成完整 SHA 清单。退役后旧视频无法再回看，"
                     "当前依赖、未决原操作与费用记录保留。")
        elif intent == "knowledge_summary":
            candidate = self.summarize(task["id"])
            result = {key: candidate[key] for key in (
                "id", "task_id", "basis_sha256", "review_status", "scope", "observations",
                "limitations", "active_production_knowledge")}
            result["source_count"] = len(candidate["source_refs"])
            reply = "已按原记录生成本任务事实总结候选；未推断根因，未启用为生产知识，未重新制作。"
        else:
            raise ValueError("Unknown maintenance intent")
        return {"intent": intent, "task_id": task["id"], "reply": reply, "result": result}
