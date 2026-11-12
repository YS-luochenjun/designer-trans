"""Explicit playable-output boundary for a reviewed sparse representation."""
from pathlib import Path

from video_translation.integrations.agents.errors import CapabilityUnavailable


def playable_base(controller, state, *, target=None):
    base = state["base"]
    if not getattr(getattr(controller, "media", None), "is_manifest", lambda p: False)(base.get("path", "")):
        return dict(base)
    controller.verified(base)
    if target is None:
        from ..application.materialization_recovery import completed_representation
        target = completed_representation(controller, state)
    target = target or controller.media.workspace.root / "checkpoints" / (base["sha256"] + ".mp4")
    path, receipt = controller.media.materialize(base["path"], target)
    return {"path": str(path), "sha256": receipt["output_sha256"],
            "source_representation_sha256": base["sha256"],
            "representation_verification": receipt}


def finished_output(controller, state):
    review = state.get("clean_review") or {}
    if review and review.get("sha256") != state["base"]["sha256"]:
        raise CapabilityUnavailable("Clean review does not bind the current base")
    base = playable_base(controller, state)
    if review and base["sha256"] != review["sha256"]:
        review = {**review, "sha256": base["sha256"],
                  "reviewed_representation_sha256": review["sha256"],
                  "representation_verification": base["representation_verification"]}
    result = {**base, "accepted": state["accepted"], "decision_session_id": state["id"],
              "evidence": state.get("evidence", []), "text_overrides": state.get("text_overrides", {}),
              "quality_passed": bool(review), "clean_review": review}
    from .stage_quality import require_clean_plate
    require_clean_plate(result)
    existing = [a for a in controller.store.list("artifact") if a["task_id"] == controller.task["id"]
                and a["node_id"] == controller.node["id"] and a.get("sha256") == base["sha256"]
                and a.get("name") == "Shared clean plate"]
    if not existing:
        controller.service.artifact(controller.task["id"], controller.node, Path(base["path"]),
                                    "video", "Shared clean plate")
    return result
