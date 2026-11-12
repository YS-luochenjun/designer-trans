"""Verify a planner snapshot across one zero-action Worker checkpoint insertion."""
from copy import deepcopy
import re

from ..domain.execution import identity

FIELDS = ("base_sha256", "scope_sha256", "capability_sha256")


def matches(state, expected_sha256):
    if identity(state) == expected_sha256:
        return True
    binding = state.get("execution_binding", {})
    checkpoints = state.get("execution_checkpoints", {})
    if (not isinstance(binding, dict) or set(binding) != set(FIELDS)
            or not isinstance(checkpoints, dict)
            or len(checkpoints) > 256
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(binding[k])) for k in FIELDS)
            or binding["base_sha256"] != state["base"]["sha256"]):
        return False
    assignment = {k: (state.get("route_work_order") or {}).get(k)
                  for k in ("source_id", "method")}
    key = identity({**binding, "assignment": assignment})
    if checkpoints.get(key) != {**binding, "start_turn": state["turn"], "actions": 0}:
        return False
    # Reconstruct the exact signed snapshot; no production field or old counter is excluded.
    before = deepcopy(state)
    del before["execution_checkpoints"][key]
    candidates = [None]
    for row in before["execution_checkpoints"].values():
        if isinstance(row, dict) and all(k in row for k in FIELDS):
            previous = {k: row[k] for k in FIELDS}
            if all(previous[k] == binding[k] for k in ("base_sha256", "scope_sha256")):
                candidates.append(previous)
    for previous in candidates:
        if previous is None:
            before.pop("execution_binding", None)
        else:
            before["execution_binding"] = previous
        if identity(before) == expected_sha256:
            return True
        if not before["execution_checkpoints"]:
            without_checkpoints = {k: v for k, v in before.items()
                                   if k != "execution_checkpoints"}
            if identity(without_checkpoints) == expected_sha256:
                return True
    return False
