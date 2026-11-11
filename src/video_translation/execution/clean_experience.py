"""Evidence-linked method advisories; never executable recipes or acceptance rules."""
from video_translation.persistence.store import now

LOCAL_METHODS = {"local_surface", "tracked_donor", "flat", "donor"}


def record_local_experience(store, state, texts):
    """Record only an acceptance already durably present in its production session."""
    persisted = store.require("production_session", state["id"])
    for accepted in persisted.get("accepted", []):
        if accepted.get("method") not in LOCAL_METHODS or not accepted.get("boundary_review"):
            continue
        identity = f"{state['id']}:{accepted['id']}"
        if store.get("clean_method_experience", identity):
            continue
        if accepted.get("needs_scope_review") or (accepted.get("quality") or {}).get("hard_failures"):
            continue
        source_id = accepted["source_id"]
        repair = next((h["decision"] for h in reversed(persisted.get("history", []))
                       if h["decision"].get("action") == "repair"
                       and h["decision"].get("source_id") == source_id
                       and h["decision"].get("method") == accepted["method"]
                       and h["decision"].get("start_ms") == accepted["start_ms"]
                       and h["decision"].get("end_ms") == accepted["end_ms"]), {})
        store.put("clean_method_experience", {
            "id": identity, "at": now(), "session_id": state["id"], "task_id": state["task_id"],
            "candidate_id": accepted["id"], "candidate_sha256": accepted["sha256"],
            "method": accepted["method"],
            "axis": (accepted.get("surface_repair") or {}).get("axis", "horizontal")
                    if accepted["method"] == "local_surface" else None,
            "used_foreground_protection": bool(accepted.get("protected_foreground")),
            "source_text": texts.get(source_id, {}).get("text", "")[:200],
            "rationale": repair.get("reason", "")[:1600],
            "score": accepted.get("score"),
            "limitations": (accepted.get("quality") or {}).get("limitations", [])[:5],
            "status": "local_advisory",
            "authority": "Candidate for method selection only; observe new media, derive new geometry and review independently.",
        })


def method_advisories(store):
    """Reject orphaned/superseded evidence; do not expose paths or old geometry."""
    result = []
    sessions = {}
    for record in reversed(store.list("clean_method_experience")):
        sid = record["session_id"]
        if sid not in sessions:
            sessions[sid] = store.get("production_session", sid)
        session = sessions[sid] or {}
        accepted = next((a for a in session.get("accepted", [])
                         if a["id"] == record["candidate_id"]
                         and a["sha256"] == record["candidate_sha256"]), None)
        if not accepted or accepted.get("needs_scope_review"):
            continue
        if (accepted.get("quality") or {}).get("hard_failures"):
            continue
        result.append(record)
        if len(result) == 6:
            break
    return result
