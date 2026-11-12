"""A concise Route-facing status projection; original Work errors stay in evidence."""


def report(task, events, node=None):
    nodes = [node] if node else task["nodes"]
    current = node or next((n for n in nodes if n["status"] in {
        "running", "waiting_external", "waiting_user"}), None)
    current = current or next((n for n in nodes if n["status"] in {"failed", "interrupted", "unknown"}), None)
    event = next((e for e in reversed(events) if current and e["node_id"] == current["id"]
                  and e["attempt"] == current["attempt"]), None)
    result = {"owner": "route", "source": "route_state_projection", "needs_user_decision": False,
              "node_id": current["id"] if current else None}
    label = (current or {}).get("label", "制作")
    if task["status"] == "cancelled":
        phase, summary, next_step = "cancelled", "任务已停止，原结果保留。", "等待新的制作要求。"
    elif task.get("control_paused"):
        phase, summary, next_step = "paused", "任务已暂停，当前结果和要求已保留。", "恢复后由 Route 接续协调。"
    elif current and current["status"] in {"running", "waiting_external"}:
        phase, summary, next_step = "working", f"Work 正在处理{label}。", "结果先交给 Route 检阅，再安排后续制作。"
    elif current and (current["status"] == "unknown" or event and event["status"] == "unknown"):
        phase, summary, next_step = (
            "awaiting_result", "上次请求的结果尚未确认，后续制作等待核对。",
            "Route 需要先核对原请求回执，再决定后续工作。")
    elif event and event["status"] in {"queued", "planned", "running", "waiting_work"}:
        phase, summary, next_step = "coordinating", "Work 的报告已交给 Route，正在协调下一步。", "核对结果与要求后安排可执行的工作。"
    elif current and current["status"] == "waiting_user":
        phase, summary, next_step = "waiting_user", "Route 有一项需要你确认的决定。", "请查看下方选项或当前视频的视听验收。"
        result["needs_user_decision"] = True
    elif event and event["status"] == "blocked":
        phase, summary, next_step = "blocked", "Route 本轮未能安排可执行的后续工作，制作仍未完成。", "先处理已记录的执行阻塞，再由 Route 接续 Work。"
        concise = (event.get("plan") or {}).get("user_summary", "").strip()
        if concise and event.get("plan", {}).get("action") == "blocked":
            summary = concise
            result["source"] = "route_plan"
    elif current and current["status"] in {"failed", "interrupted"}:
        phase, summary, next_step = "needs_coordination", "本阶段尚未完成，已保留 Work 的结果和问题。", "由 Route 核查原因后决定后续工作。"
    elif task["status"] == "completed":
        phase, summary, next_step = "completed", "制作流程已完成。", "交付和验收记录保留在任务详情。"
    else:
        phase, summary, next_step = "queued", "任务正在等待制作安排。", "满足前置条件后由 Work 执行，Route 负责协调。"
    return {**result, "phase": phase, "summary": summary, "next_step": next_step}
