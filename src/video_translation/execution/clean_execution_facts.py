"""Current file identities and applied actions for Route; no inferred tool execution."""
from pathlib import Path
import json

from video_translation.application.convergence_recovery import _media_evidence
from video_translation.application.tasks import Conflict


def facts(service, task, state):
    from video_translation.domain.limits import budget_policy, testing_unlimited
    node = next(n for n in task["nodes"] if n["id"] == state["node_id"])
    budget = task.get("execution_snapshot", {}).get("resource_budget") or {}
    limit = None if testing_unlimited(task) else budget.get("max_clean_turns", state["turn"])
    result = {
        "node_id": node["id"], "session_id": state["id"], "turn": state["turn"],
        "base": dict(state["base"]), "accepted_count": len(state.get("accepted", [])),
        "clean_turn_limit": limit,
        "remaining_actions": None if limit is None else max(0, limit - state["turn"]),
        "budget_policy": budget_policy(task),
        "quality_passed": False, "media_dependencies_verified": False,
        "applied_work": [{"turn": h["turn"], "action": h["decision"]["action"],
                          "source_id": h["decision"].get("source_id"),
                          "candidate_id": h["decision"].get("candidate_id"),
                          "tool_error": h.get("tool_error")}
                         for h in state.get("history", [])[-3:]],
        "meaning": "Only applied_work proves executed actions; a Route question is an assignment, not a receipt.",
    }
    records = [r for r in service.store.list("clean_execution_authorization")
               if r["task_id"] == task["id"] and r["node_id"] == node["id"]]
    if records:
        record = max(records, key=lambda r: (r.get("created_at", ""), r["id"]))
        result["execution_authorization"] = {k: record.get(k) for k in (
            "id", "status", "start_turn", "new_clean_limit", "reason")}
    try:
        source = service.checked_path(service.store.require("asset", node["asset_id"]))
        dependencies = _media_evidence(service, task, state, source)
        result.update(media_dependencies_verified=True, dependency_count=len(dependencies))
        output = state.get("draft_handoff") or node.get("output", {})
        if not output.get("path"):
            return result
        path = Path(output["path"])
        root = service.settings.runtime / "tasks" / task["id"]
        if (path.is_symlink() or not (path.resolve().is_relative_to(root.resolve())
                                    or path.resolve() == source.resolve())):
            raise Conflict("Playable output leaves the task")
        if service.hash_file(path) != output.get("sha256"):
            raise Conflict("Playable output changed")
        playable = {"path": str(path), "sha256": output["sha256"],
                    "current_file_matches_receipt": False}
        if output.get("source_representation_sha256"):
            receipt_path = path.with_suffix(".mp4.representation.json")
            if receipt_path.is_symlink() or receipt_path.stat().st_size > 65536:
                raise Conflict("Invalid representation receipt")
            receipt = json.loads(receipt_path.read_bytes())
            if (receipt != output.get("representation_verification")
                    or receipt.get("input_sha256") != state["base"]["sha256"]
                    or receipt.get("output_sha256") != output["sha256"]
                    or receipt.get("audio_packets_preserved") is not True):
                raise Conflict("Representation receipt does not bind this base and output")
            playable.update(current_file_matches_receipt=True,
                            source_representation_sha256=receipt["input_sha256"],
                            receipt_sha256=service.hash_file(receipt_path),
                            recorded_rgb_sha256=receipt.get("decoded_rgb_sha256"),
                            recorded_audio_packets_preserved=True)
        elif output["sha256"] == state["base"]["sha256"] and path.resolve() == Path(
                state["base"]["path"]).resolve():
            playable.update(current_file_matches_receipt=True, direct_base=True)
        else:
            raise Conflict("Playable output is not bound to the current base")
        result["playable_output"] = playable
    except (Conflict, OSError, ValueError, KeyError) as exc:
        result["evidence_error"] = str(exc)[:400]
    return result
