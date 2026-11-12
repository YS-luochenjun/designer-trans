"""Bounded views over retained records, never a mutation of production facts."""
from collections import Counter
from copy import deepcopy
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _select(rows, limit, pinned):
    indices = set(range(max(0, len(rows) - limit), len(rows)))
    indices.update(i for i, row in enumerate(rows) if row.get("id") in pinned)
    return [row for i, row in enumerate(rows) if i in indices]


def _summary(rows, selected):
    return {"total": len(rows), "supplied": len(selected), "omitted": len(rows) - len(selected),
            "records_sha256": fingerprint(rows)}


def _record(row):
    fields = ("id", "source_id", "method", "start_ms", "end_ms", "sha256", "input_sha256",
              "review_status", "score", "needs_scope_review", "has_pipeline", "rebase_obstacle")
    result = {key: deepcopy(row[key]) for key in fields if key in row}
    if row.get("quality"):
        quality = row["quality"]
        result["quality_summary"] = {
            key: deepcopy(quality[key]) for key in (
                "text_removal", "subject_preservation", "temporal_consistency", "hard_failures")
            if key in quality}
        result["quality_summary"]["limitations_count"] = len(quality.get("limitations", []))
    if row.get("rejection"):
        result["rejection_retained"] = True
    return result


def compact_clean_inputs(inputs, *, can_read_history=False):
    history = inputs.get("retained_history")
    if not (can_read_history and isinstance(history, dict)
            and history.get("path") and history.get("sha256")
            and inputs.get("agent_role") in {"work", "route"}
            and ("work_queue" in inputs or "accepted" in inputs)
            and any(key in inputs for key in ("baseline_sha256", "execution_contract"))):
        return inputs
    result = dict(inputs)
    review = inputs.get("scope_review") or {}
    pinned = {inputs.get("archive_under_review"), review.get("accepted_id"), review.get("candidate_id"),
              (inputs.get("candidate") or {}).get("id")}
    pinned.update(row.get("accepted_id") for row in inputs.get("acceptance_disputes", [])
                  if row.get("status") != "resolved_by_current_plate_review")
    projection = {
        "revision": 1, "facts_unchanged": True,
        "retrieval": ("Older records and complete details remain in retained_history. Read that "
                      "task-local file by ID/source with native read-only tools; verify its SHA first. "
                      "Use inspect_archive for actual historical media. Omission never means absent, "
                      "successful, affordable or approved. No evidence file means details are unknown."),
    }
    for key, limit in (("accepted", 12), ("historical_candidates", 16)):
        rows = inputs.get(key)
        if not isinstance(rows, list):
            continue
        chosen = _select(rows, limit, pinned)
        projection[key] = _summary(rows, chosen)
        if len(rows) > limit:
            result[key] = [deepcopy(row) if row.get("id") in pinned else _record(row) for row in chosen]
            projection[key]["details"] = "Recent metadata and pinned review details; full records retained."
    queue = inputs.get("work_queue")
    if isinstance(queue, list):
        result["work_queue"] = []
        for row in queue:
            compact = dict(row)
            outcomes = row.get("repair_outcomes", [])
            if len(outcomes) > 6:
                # Every method retains its latest result; per-method aggregate counts
                # keep old failures visible without repeating every failed recipe.
                last_by_method = {}
                for index, outcome in enumerate(outcomes):
                    last_by_method[outcome.get("method")] = index
                selected = set(range(max(0, len(outcomes) - 6), len(outcomes)))
                selected.update(last_by_method.values())
                compact["repair_outcomes"] = [deepcopy(item) for i, item in enumerate(outcomes) if i in selected]
                compact["repair_history"] = {
                    **_summary(outcomes, compact["repair_outcomes"]),
                    "method_counts": dict(Counter(item.get("method") for item in outcomes)),
                    "tool_error_counts": dict(Counter(item.get("method") for item in outcomes if item.get("tool_error"))),
                }
            result["work_queue"].append(compact)
    result["context_projection"] = projection
    return result


def node_work_order(task, node):
    dependencies = []
    for dependency_id in node.get("depends_on", []):
        dependency = next((item for item in task.get("nodes", []) if item["id"] == dependency_id), None)
        if dependency is None:
            dependencies.append({"node_id": dependency_id, "status": "missing", "output_sha256": None})
            continue
        output = dependency.get("output") or {}
        dependencies.append({
            "node_id": dependency_id, "stage": dependency["definition_id"],
            "asset_id": dependency.get("asset_id"), "locale": dependency.get("locale"),
            "status": dependency["status"], "output_sha256": fingerprint(output),
            "media_sha256": output.get("sha256"), "quality_passed": output.get("quality_passed"),
        })
    return {
        "revision": 1, "authority": "program_workflow", "task_id": task["id"], "node_id": node["id"],
        "stage": node["definition_id"], "asset_id": node.get("asset_id"), "locale": node.get("locale"),
        "dependencies": dependencies,
        "local_methods": "Work chooses supported tools and evidence; supplied skills remain stage scoped.",
        "shared_state": "Versioned node outputs and explicit handoffs, not another agent's private reasoning.",
        "delegation": "Only actually available subagent tools; children share task budgets and cannot self-approve.",
    }
