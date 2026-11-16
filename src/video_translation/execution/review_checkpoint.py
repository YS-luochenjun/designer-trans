"""Candidate-bound review facts, distinct from model reasoning and approval."""
from collections import Counter
from copy import deepcopy
from pathlib import Path

from ..domain.execution import identity
from ..integrations.agents.errors import CapabilityUnavailable
from ..persistence.store import now
from .frame_clock import sample_index


def frame_receipts(controller, frames):
    receipts = []
    for frame in frames:
        receipts.append({
            "role": frame.get("label", "").split(" · ", 1)[0],
            "requested_time_ms": frame["time_ms"],
            "sampled_time_ms": frame.get("sampled_time_ms", frame["time_ms"]),
            "frame_index": frame["frame_index"],
            "path": frame["path"],
            "sha256": controller.service.hash_file(Path(frame["path"])),
        })
    return receipts


def historical_reports(controller, candidate_sha):
    rows = [row for row in controller.store.list("production_decision")
            if row.get("node_id") == controller.node["id"]
            and row.get("status") == "succeeded"
            and (row.get("result") or {}).get("candidate_sha256") == candidate_sha]
    rows.sort(key=lambda row: row.get("created_at") or "")
    return {
        "count": len(rows),
        "actions": dict(Counter(row["result"].get("action") for row in rows)),
        "reported_times_ms": sorted({
            time for row in rows for time in row["result"].get("times_ms", [])}),
        "recent": [
            {"decision_id": row["id"], "action": row["result"]["action"],
             "reason": row["result"]["reason"], "evidence": row["result"].get("evidence", []),
             "requested_times_ms": row["result"].get("times_ms", [])}
            for row in rows[-3:]],
        "meaning": "Retained reports, not new observations or an acceptance of this batch.",
    }


class ReviewCheckpoint:
    def __init__(self, controller, result, batches):
        self.c = controller
        clean = controller.e.output(controller.task, controller.node, "clean")
        basis = {
            "task_id": controller.task["id"], "node_id": controller.node["id"],
            "source_sha256": controller.service.hash_file(controller.source),
            "clean_sha256": clean["sha256"], "initial_candidate_sha256": result["sha256"],
            "source_design": controller.e.output(controller.task, controller.node, "visual_analyze"),
            "translation": controller.e.output(controller.task, controller.node, "translate"),
            "plan": result["plan"],
            "batches": [[item["id"] for item in batch] for batch in batches],
        }
        key = identity({"protocol": 1, "basis": basis})
        self.state = controller.store.get("visual_review_session", key) or {
            "id": key, "task_id": controller.task["id"], "node_id": controller.node["id"],
            "basis": basis, "created_at": now(), "turn": 0, "batch_index": 0,
            "result": deepcopy(result), "times_ms": controller.times(batches[0]),
            "observations": [], "reports": [], "batch_evidence": [],
            "requests": [], "focus": "general", "revisions": 0,
            "historical_reports": historical_reports(controller, result["sha256"]),
        }
        controller.verified(self.state["result"])
        for receipt in self.state["observations"]:
            path = Path(receipt["path"])
            if not path.is_file() or controller.service.hash_file(path) != receipt["sha256"]:
                raise CapabilityUnavailable("已保存审阅帧缺失或变化；保留检查记录并交Route核对")

    def save(self):
        self.state["updated_at"] = now()
        self.c.store.put("visual_review_session", self.state)
        self.c.service.write_json(
            self.c.task["id"], self.c.node, "visual-review-checkpoint.json", self.state)

    def observe(self, frames):
        existing = {(row["role"], row["frame_index"]): row
                    for row in self.state["observations"]}
        for receipt in frame_receipts(self.c, frames):
            previous = existing.get((receipt["role"], receipt["frame_index"]))
            if previous and previous["sha256"] != receipt["sha256"]:
                raise CapabilityUnavailable("同一候选的已观察帧发生变化；保留原审阅并核对媒体身份")
            existing[receipt["role"], receipt["frame_index"]] = receipt
        self.state["observations"] = list(existing.values())
        self.save()

    def context(self):
        return {
            "id": self.state["id"], "candidate_sha256": self.state["result"]["sha256"],
            "batch_index": self.state["batch_index"],
            "focus": self.state["focus"], "observations": self.state["observations"],
            "reports": self.state["reports"], "accepted_batches": self.state["batch_evidence"],
            "historical_reports": self.state["historical_reports"],
            "unresolved": self.state.get("unresolved"),
            "instructions": (
                "接续同一候选的事实记录。observations含真实帧号、图片路径和SHA，"
                "reports含前轮已完成检查，historical_reports只是旧报告。"
                "需要对照旧图时可读取这些原文件；不能因本轮未再次附图就称从未观察。"
                "补查须写inspection_focus，说明新增要核实的实际问题。"
                "同一检查重复索要同一实际帧不会形成新证据。"
                "已见证据足够则完成本批accept；有具体字层缺陷则revise；"
                "需要连续片段或缺工具时明确blocked交Route换法，不无限往返补图。"),
        }

    def report(self, action):
        self.state["reports"].append({
            "turn": self.state["turn"], "batch_index": self.state["batch_index"],
            "candidate_sha256": self.state["result"]["sha256"],
            "action": action["action"], "reason": action["reason"],
            "evidence": action["evidence"], "times_ms": list(self.state["times_ms"]),
            "requested_times_ms": action.get("times_ms", []),
            "inspection_focus": action.get("inspection_focus", "general"),
        })
        self.state["turn"] += 1
        self.state["pending_action"] = deepcopy(action)
        self.state.pop("pending_check", None)
        self.save()

    def applied(self):
        self.state.pop("pending_action", None)
        self.save()

    def inspect(self, action, source_ids):
        times = action["times_ms"]
        if (not times or len(set(times)) > 8
                or any(type(time) is not int or not 0 <= time < self.c.duration for time in times)):
            raise ValueError("三路补充观察需要原片内1–8个时间点，保证单次图像输入可用")
        _, clock = self.c.media._info(self.c.source)
        indices = sorted({sample_index(time, clock["rate"], clock["frames"]) for time in times})
        focus = action.get("inspection_focus", "general")
        request = {
            "candidate_sha256": self.state["result"]["sha256"],
            "source_ids": sorted(source_ids), "frame_indices": indices, "focus": focus,
        }
        checked = {index for previous in self.state["requests"]
                   if previous["candidate_sha256"] == request["candidate_sha256"]
                   and previous["source_ids"] == request["source_ids"]
                   and previous["focus"] == focus for index in previous["frame_indices"]}
        if set(indices).issubset(checked):
            self.state["unresolved"] = {
                "reason": "同一候选、检查项和实际帧的重复补查，没有新增证据",
                "request": request, "last_report": deepcopy(self.state["reports"][-1]),
            }
            self.state.pop("pending_action", None)
            self.save()
            raise CapabilityUnavailable(
                "画面对照重复请求同一检查项的实际帧，没有新增证据；"
                "原译片和审阅记录已保留，交Route改用连续片段或其他核验方法")
        self.state["requests"].append(request)
        self.state.update(times_ms=list(times), focus=focus)
        self.applied()

    def new_candidate(self, result, plan, batches):
        self.state.update(result=deepcopy(result), batch_index=0,
                          times_ms=self.c.times(batches[0]), observations=[],
                          batch_evidence=[], requests=[], focus="general")
        self.state.pop("unresolved", None)
        self.applied()
