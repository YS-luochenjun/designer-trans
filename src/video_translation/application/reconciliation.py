"""Read-only ownership of current requests, separate from replacement admission."""
from .task_supervision import request_facts
from ..domain.execution import identity


def snapshot(service, task, node_ids=None):
    ids = node_ids if node_ids is not None else {n["id"] for n in task["nodes"]}
    # Retired nodes no longer appear in the graph, but their request identities remain.
    if node_ids is None:
        ids |= {r.get("node_id") for r in service.store.list("operation")
                if r.get("task_id") == task["id"]}
    facts = request_facts(service.store, task, ids)
    current = {n["id"] for n in task["nodes"]}
    active = [{**r, "classification": "active_current" if r["node_id"] in current
               else "historical_unresolved"} for r in facts["unsettled_requests"]]
    retained = [{**r, "classification": "historical_superseded"}
                for r in facts["authorized_historical_unknown_requests"]]
    result = {
        "owner": "technical_route",
        "active": active,
        "active_count": facts["unsettled_count"],
        "retained_unknown": retained,
        "retained_unknown_count": facts["authorized_historical_unknown_count"],
        "next_step": "Inspect original receipts; recheck current evidence before any new dispatch.",
        "read_only": True, "quality_acceptance": False,
    }
    result["basis_sha256"] = identity({
        "task": task["id"], "brief_revision": task.get("brief_revision"),
        "nodes": [{k: n.get(k) for k in ("id", "attempt", "status")} for n in task["nodes"]],
        "active": active, "retained": retained})
    return result
