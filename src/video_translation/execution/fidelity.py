"""Source-preserving production rules, independent of a provider's success receipt."""

RESOURCE_ID = "video-translation.visual-fidelity"
PRESERVED_STYLE = ("color", "outline_color", "outline_width", "shadow_color",
                   "shadow_x", "shadow_y", "align")
STRUCTURAL_FAILURES = {"subject_damage", "new_object", "severe_flicker"}


def enabled(task):
    return any(r["id"] == RESOURCE_ID and r.get("status") == "active"
               for r in task.get("resource_snapshot", {}).get("knowledge", []))


def wan_exclusions(state):
    """Rejected output is evidence; unknown or merely pending output is not."""
    excluded = {}
    for candidate in state.get("candidate_archive", []):
        quality = candidate.get("rejection", {}).get("quality") or {}
        if (candidate.get("method") == "wan"
                and candidate.get("review_status") == "rejected"
                and STRUCTURAL_FAILURES.intersection(quality.get("hard_failures", []))):
            excluded[candidate["source_id"]] = candidate["id"]
    return excluded


def preserve_lettering(entry, source):
    """Inherit recorded design, retaining explicit language/font-size adaptation."""
    style = dict(entry["style"])
    inherited = {}
    for key in PRESERVED_STYLE:
        if key in source["style"]:
            style[key] = source["style"][key]
            inherited[key] = source["style"][key]
    # Source motion uses original absolute time and is independent of target text.
    motion = source.get("motion") or entry.get("motion", [])
    adaptations = {key: {"source": source["style"].get(key), "target": style.get(key),
                         "reason": entry["rationale"]}
                   for key in ("font_id", "font_size")
                   if source["style"].get(key) != style.get(key)}
    return style, motion, {
        "inherited_style": inherited, "adaptations": adaptations,
        "motion_basis": "source_keyframes" if source.get("motion") else "plan_unverified",
        "source_uncertainty": source.get("uncertainty", ""),
    }
