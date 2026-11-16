"""Home-page observations; never reconcile requests or change production state."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from math import isfinite
from pathlib import Path

CALL_KINDS = ("operation", "manager_operation", "route_run", "route_probe")


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _amount(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        return None
    return Decimal(str(value))


def _duration(task, end: datetime | None = None) -> float | None:
    starts, finishes = [], []
    for node in task.get("nodes", []):
        start, finish = _timestamp(node.get("started_at")), _timestamp(node.get("finished_at"))
        if start:
            starts.append(start)
        if finish:
            finishes.append(finish)
    if not starts or (end is None and not finishes):
        return None
    seconds = ((end or max(finishes)) - min(starts)).total_seconds()
    return seconds if seconds >= 0 else None


def _within(value, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    stamp = _timestamp(value)
    return stamp is not None and (start is None or stamp >= start) and (end is None or stamp < end)


def _finished_at(task) -> str | None:
    latest = max(
        (stamp for node in task.get("nodes", [])
         if (stamp := _timestamp(node.get("finished_at"))) is not None),
        default=None,
    )
    return latest.isoformat() if latest else None


def _provider(call: dict) -> str:
    provider = call.get("provider") or "unspecified"
    agent_call = provider in {"agent", "manager_operation", "route_run", "route_probe", "codex"}
    if agent_call and (provider == "codex" or call.get("engine") == "codex"
                       or call.get("task_agent_engine") == "codex"
                       or str(call.get("model") or "").startswith("codex/")):
        return "codex"
    return provider


def _costs(calls: list[dict]) -> dict:
    groups = defaultdict(lambda: {
        "confirmed": Decimal(0), "reserved": Decimal(0),
        "confirmed_operations": 0, "unsettled_operations": 0, "unknown_operations": 0,
    })
    unsettled, unpriced = 0, 0
    for call in calls:
        provider = _provider(call)
        actual, estimate = _amount(call.get("actual_cost")), _amount(call.get("estimated_cost"))
        # An Agent request with no monetary unit is unpriced, even when it completed.
        unit = call.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            unpriced += 1
            if provider == "codex":
                groups[(provider, None)]["unknown_operations"] += 1
            continue
        group = groups[(provider, unit.strip())]
        # Route's workflow status can change after the model has returned a decision.
        settled = (call.get("status") == "succeeded" or call.get("journal_state") == "fail"
                   or (call.get("kind") == "route_run"
                       and _timestamp(call.get("decided_at")) is not None))
        if settled and actual is not None:
            group["confirmed"] += actual
            group["confirmed_operations"] += 1
        else:
            if estimate is not None or actual is not None:
                unsettled += 1
                group["unsettled_operations"] += 1
                group["reserved"] += max(estimate or Decimal(0), actual or Decimal(0))
            else:
                unpriced += 1
                group["unknown_operations"] += 1
    return {
        "items": [
            {
                "provider": provider, "unit": unit, **values,
                "confirmed": float(values["confirmed"]) if values["confirmed_operations"] else None,
                "reserved": float(values["reserved"]) if values["unsettled_operations"] else None,
            }
            for (provider, unit), values in sorted(
                groups.items(), key=lambda item: (item[0][0], item[0][1] or ""))
        ],
        "unsettled_calls": unsettled,
        "unpriced_calls": unpriced,
        "recorded_calls": len(calls),
    }


def build_project_metrics(
    tasks: list[dict], deliveries: list[dict], calls: list[dict], *,
    as_of: datetime | None = None,
    start: datetime | None = None, end: datetime | None = None,
) -> dict:
    current = as_of or datetime.now(timezone.utc)
    selected_tasks = [task for task in tasks if _within(task.get("created_at"), start, end)]
    completed_spans = [
        seconds for task in tasks if task.get("status") == "completed"
        and (seconds := _duration(task)) is not None
        and _within(_finished_at(task), start, end)
    ]
    open_spans = [
        seconds for task in selected_tasks if task.get("status") not in {"completed", "cancelled"}
        and (seconds := _duration(task, current)) is not None
    ]
    linked = {task["requirement_id"] for task in selected_tasks if task.get("requirement_id")}
    selected_deliveries = [item for item in deliveries if _within(item.get("created_at"), start, end)]
    return {
        "as_of": current.isoformat(),
        "window": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        "tasks": {"total": len(selected_tasks), "linked_requirements": len(linked)},
        "deliveries": {
            "total": len(selected_deliveries),
            "with_issues": sum(item.get("status") == "delivered_with_issues" for item in selected_deliveries),
        },
        "duration": {
            "average_seconds": sum(completed_spans) / len(completed_spans) if completed_spans else None,
            "measured_tasks": len(completed_spans),
            "longest_open_seconds": max(open_spans) if open_spans else None,
            "scope": "completed_task_wall_clock",
        },
        "costs": _costs([call for call in calls if _within(call.get("created_at"), start, end)]),
    }


def read_project_metrics(
    database: Path, *, start: datetime | None = None, end: datetime | None = None,
) -> dict:
    # Use a deferred read snapshot, not Store.connection's BEGIN IMMEDIATE write lock.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tasks = [
            json.loads(row[0]) for row in db.execute("""
                SELECT json_object(
                    'created_at', COALESCE(json_extract(data, '$.created_at'), created_at),
                    'status', json_extract(data, '$.status'),
                    'requirement_id', json_extract(data, '$.requirement_id'),
                    'nodes', json((
                        SELECT json_group_array(json_object(
                            'started_at', json_extract(value, '$.started_at'),
                            'finished_at', json_extract(value, '$.finished_at')
                        )) FROM json_each(data, '$.nodes')
                    ))
                ) FROM records WHERE kind='task'
            """)
        ]
        deliveries = [
            {"status": row[0], "created_at": row[1]} for row in db.execute("""
                SELECT json_extract(data, '$.status'),
                    COALESCE(json_extract(data, '$.created_at'), created_at)
                FROM records WHERE kind='delivery'
            """)
        ]
        placeholders = ",".join("?" for _ in CALL_KINDS)
        calls = [
            json.loads(row[0]) for row in db.execute(f"""
                SELECT json_object(
                    'kind', c.kind,
                    'provider', COALESCE(json_extract(c.data, '$.provider'), c.kind),
                    'engine', json_extract(c.data, '$.engine'),
                    'model', json_extract(c.data, '$.model'),
                    'task_agent_engine', json_extract(t.data, '$.agent_engine'),
                    'created_at', COALESCE(json_extract(c.data, '$.created_at'), c.created_at),
                    'status', json_extract(c.data, '$.status'),
                    'decided_at', json_extract(c.data, '$.decided_at'),
                    'journal_state', json_extract(c.data, '$.journal_state'),
                    'actual_cost', CASE WHEN json_type(c.data, '$.actual_cost') IN ('integer', 'real')
                        THEN json_extract(c.data, '$.actual_cost') END,
                    'estimated_cost', CASE WHEN json_type(c.data, '$.estimated_cost') IN ('integer', 'real')
                        THEN json_extract(c.data, '$.estimated_cost') END,
                    'unit', json_extract(c.data, '$.unit')
                ) FROM records c LEFT JOIN records t ON t.kind='task'
                    AND t.id=json_extract(c.data, '$.task_id')
                WHERE c.kind IN ({placeholders})
            """, CALL_KINDS)
        ]
        return build_project_metrics(tasks, deliveries, calls, start=start, end=end)
