"""Reconnect reviewed cleanup after Route refines source lettering evidence."""
from copy import deepcopy

from ..domain.execution import identity
from ..integrations.agents.errors import CapabilityUnavailable
from ..persistence.store import now
from .clean_policy import coverage_summary
from .stage_quality import require_clean_plate, require_motion_evidence

ERROR = (
    "\u6e90\u8bbe\u8ba1\u5df2\u53d8\u5316\u4f46\u5df2\u6709\u6e05\u5e95\u4f1a\u8bdd\uff1b"
    "\u5148\u6838\u5bf9\u539f\u63a5\u53d7\u8bb0\u5f55\u4e0e\u65b0\u8fd0\u52a8\u8303\u56f4\uff0c"
    "\u4e0d\u80fd\u53e6\u5efa\u4f1a\u8bdd\u4e22\u5931\u5df2\u4fee\u590d\u5185\u5bb9"
)
SETTLED_ERROR = "Source design continuation requires settled reviewed cleanup"


def effective_texts(task, node, state):
    observe = next(n for n in task["nodes"] if n["definition_id"] == "observe"
                   and n["asset_id"] == node["asset_id"])
    texts = {t["id"]: deepcopy(t) for t in observe["output"]["texts"]
             if t["treatment"] == "translate"}
    for source_id, override in state.get("text_overrides", {}).items():
        texts[source_id].update(override)
    return texts


def checked_rebinding(service, task, node, state, design):
    """Read-only proof: same source/scope, retained review, static motion refinement."""
    from .adaptive import digest

    asset = service.store.require("asset", node["asset_id"])
    source_sha = service.hash_file(service.checked_path(asset))
    if (asset["id"] not in task["asset_ids"] or source_sha != asset["sha256"]
            or design.get("source_sha256", source_sha) != source_sha):
        raise CapabilityUnavailable("Original source changed during source design continuation")
    visual = next(n for n in task["nodes"] if n["definition_id"] == "visual_analyze"
                  and n["asset_id"] == node["asset_id"])
    if visual["status"] != "succeeded":
        raise CapabilityUnavailable("Updated source design is not complete")
    originals = [r["output"] for r in visual.get("history", [])
                 if r["status"] == "succeeded" and digest({
                     "task": task["id"], "node": node["id"], "source": source_sha,
                     "design": r["output"]}) == state["id"]]
    if not originals:
        raise CapabilityUnavailable("Original source design identity is not retained")
    old = originals[0]
    texts = effective_texts(task, node, state)
    if identity(texts) != state.get("execution_binding", {}).get("scope_sha256"):
        raise CapabilityUnavailable("Cleanup scope changed; original session needs scoped review")
    # An already verified design association survives ordinary Route rework.
    # This reuses identity only: the current plate still needs a fresh review.
    if any(all(binding.get(key) == value for key, value in {
            "session_id": state["id"], "source_sha256": source_sha,
            "original_design_sha256": identity(old), "design_sha256": identity(design),
            "scope_sha256": identity(texts)}.items())
           for binding in state.get("source_design_rebindings", [])):
        return None
    if (not state.get("finished") or state.get("candidate")
            or state.get("pending_turn") is not None or node.get("external_pending")):
        raise CapabilityUnavailable(SETTLED_ERROR)
    require_clean_plate({"sha256": state["base"]["sha256"], "quality_passed": True,
                         "clean_review": state.get("clean_review")})
    if (set(state["clean_review"].get("source_ids", [])) != set(texts)
            or any(r["remaining_intervals_ms"] for r in coverage_summary(texts, state))):
        raise CapabilityUnavailable("Reviewed cleanup no longer covers the current text scope")
    previous = {r["source_id"]: r for r in old["lettering"]}
    updated = {r["source_id"]: r for r in design["lettering"]}
    if (set(previous) != set(texts) or set(updated) != set(texts)
            or len(updated) != len(design["lettering"])):
        raise CapabilityUnavailable("Source design text identities changed")
    for source_id, entry in updated.items():
        require_motion_evidence(entry)
        # Descriptive refinements and verified static identity transforms add no
        # newly moving/occluded cleanup area. Other changes need a fresh review.
        if (previous[source_id].get("motion_status") not in {"unknown", "static"}
                or entry["motion_status"] != "static" or entry.get("needs_occlusion")
                or entry.get("occlusion") or any(
                    point.get("x", 0) != 0 or point.get("y", 0) != 0
                    or point.get("scale", 1) != 1 or point.get("opacity", 1) != 1
                    for point in entry.get("motion", []))):
            raise CapabilityUnavailable("Updated motion or occlusion requires scoped cleanup review")
    return {
        "original_design_sha256": identity(old), "design_sha256": identity(design),
        "source_sha256": source_sha, "scope_sha256": identity(texts),
        "base_sha256": state["base"]["sha256"],
        "clean_review_sha256": identity(state["clean_review"]),
        "session_id": state["id"], "external_calls": 0,
    }


def retain_rebinding(store, state, proof):
    bindings = state.setdefault("source_design_rebindings", [])
    if not any(r["design_sha256"] == proof["design_sha256"]
               and r["base_sha256"] == proof["base_sha256"] for r in bindings):
        bindings.append({**proof, "created_at": now()})
        store.put("production_session", state)
