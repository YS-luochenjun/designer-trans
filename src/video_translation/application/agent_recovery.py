"""Explicit, single-incident replacement authorization; original receipts stay unknown."""


def replaced_with_authorization(store, record, kind):
    authorization_id = record.get("replacement_authorization_id")
    if not authorization_id:
        return False
    authorization = store.get("agent_recovery_authorization", authorization_id)
    bound = bool(
        authorization
        and authorization.get("status") == "applied"
        and authorization.get("task_id") == record.get("task_id")
        and authorization.get("node_id") == record.get("node_id")
        and authorization.get("superseded", {}).get(kind) == record.get("id")
        and authorization.get("replacement_key")
    )
    if bound and authorization.get("scope") == "one_new_visual_review":
        from .visual_recovery import valid_supersession
        return valid_supersession(store, authorization)
    return bound
