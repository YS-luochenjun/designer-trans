"""Compact cross-stage context, not a claim of persistent model memory."""


def production_context(task):
    stages = []
    missing_motion = []
    for node in task.get("nodes", []):
        output = node.get("output", {})
        stages.append({
            "stage": node["definition_id"], "locale": node.get("locale"),
            "status": node["status"], "error": (node.get("error") or "")[:500],
            "sha256": output.get("sha256"), "quality_passed": output.get("quality_passed"),
            "completion_state": output.get("completion_state"),
        })
        if node["definition_id"] == "visual_analyze":
            missing_motion.extend(d["source_id"] for d in output.get("lettering", [])
                                  if d.get("motion_status", "unknown") == "unknown")
    return {
        "memory_model": "Independent invocation; only supplied packet, history summaries and images are visible.",
        "objective": ("Produce a fully translated video through the project workflow: independently verified "
                      "clean plate, complete translated speech/text, source style/motion, preserved music/effects, "
                      "and audiovisual review. Partial coverage or a successful tool call is not completion."),
        "roles": {
            "route": "Diagnose workflow/context/capability gaps and assign a bounded evidence check.",
            "work": "Observe actual media, choose supported methods, propose processing and review candidates.",
            "executor": "Validate proposals, run tools, preserve receipts, enforce budgets and quality gates.",
            "authority": "No agent can waive user pauses, unknown-call reconciliation or final acceptance.",
        },
        "stages": stages, "policy": task.get("production_policy", {}),
        "user_feedback": task.get("production_feedback", [])[-5:],
        "missing_verified_motion": missing_motion,
        "media_boundary": "Sampled images do not constitute continuous playback or listening.",
        "process_feedback": "Use process_findings when schema permits: identify the failing stage, evidence, missing capability and a concrete next check. Suggestions do not modify code or bypass quality gates.",
    }
