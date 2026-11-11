"""AI delivery selection over the verified Fireplay requirement contract.

Options come from requirement_create_options.fixed.aiModes; requirement_list
accepts ai_mode and returns scalar aiMode. No defaults or labels are invented.
This module neither reads credentials nor sends network/platform mutations.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re

import jsonschema

_MODE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


def delivery_options(payload: Mapping) -> list[dict]:
    """Project only nonempty enum values/labels; never expose owners or members.

    Disabled modes remain valid read filters for historical requirements.
    """
    fixed = payload.get("fixed")
    raw = fixed.get("aiModes") if isinstance(fixed, Mapping) else None
    if not isinstance(raw, list):
        raise ValueError("Fireplay未返回AI承接交付选项合同")
    result, seen = [], set()
    for option in raw:
        if not isinstance(option, Mapping):
            raise ValueError("Fireplay交付选项结构无效")
        value = option.get("value")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        label = option.get("label")
        if not isinstance(value, str) or not _MODE.fullmatch(value):
            raise ValueError("Fireplay交付选项值无效")
        if not isinstance(label, str) or not label.strip() or value in seen:
            raise ValueError("Fireplay交付选项标签缺失或值重复")
        seen.add(value)
        result.append({"value": value, "label": label.strip()})
    return result


def validate_delivery_selection(selected: Sequence[str], options: Sequence[Mapping]) -> tuple[str, ...]:
    """Require an explicit nonempty selection; sort/deduplicate for stable cache keys."""
    if not isinstance(selected, (list, tuple)) or not selected or len(selected) > 200:
        raise ValueError("请选择至少一项AI承接交付类型（最多200项）")
    allowed = {option["value"] for option in options}
    if any(not isinstance(value, str) or value not in allowed for value in selected):
        raise ValueError("所选AI承接交付类型已失效，请刷新选项后重新选择")
    return tuple(sorted(set(selected)))


def supports_ai_mode_filter(schema: Mapping) -> bool:
    """Only explicit schema properties establish support, not additionalProperties."""
    properties = schema.get("properties")
    return isinstance(properties, Mapping) and isinstance(properties.get("ai_mode"), Mapping)


def build_delivery_query(selected: Sequence[str], options: Sequence[Mapping], schema: Mapping,
                         *, scope: str = "all", limit: int = 100) -> dict:
    """Build summary query; fail on an incompatible advertised filter schema.

    Absence of ai_mode permits bounded local filtering by the caller. Other schema
    changes do not silently broaden a query. The MCP facade currently caps limit at 100.
    """
    values = validate_delivery_selection(selected, options)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("需求读取每页数量必须为1至100")
    arguments = {"scope": scope, "view": "summary", "limit": limit, "pagination": "cursor"}
    if supports_ai_mode_filter(schema):
        arguments["ai_mode"] = list(values)
    try:
        jsonschema.Draft202012Validator(schema).validate(arguments)
    except (jsonschema.ValidationError, jsonschema.SchemaError):
        raise ValueError("Fireplay当前需求查询合同不兼容") from None
    return arguments


def filter_delivery_rows(rows: Sequence[Mapping], selected: Sequence[str]) -> list[dict]:
    """OR on scalar aiMode; null/blank values never match. Return shallow copies."""
    values = set(selected)
    if not values:
        raise ValueError("请选择至少一项AI承接交付类型")
    result = []
    for row in rows:
        if not isinstance(row, Mapping) or "aiMode" not in row:
            raise ValueError("需求摘要缺少AI承接交付字段，无法证明筛选结果")
        value = row["aiMode"]
        if value is not None and not isinstance(value, str):
            raise ValueError("需求AI承接交付字段结构变化")
        if value and value.strip() and value in values:
            result.append(dict(row))
    return result


def collect_delivery_requirements(read_page: Callable[[dict], Mapping], selected: Sequence[str],
                                  options: Sequence[Mapping], schema: Mapping, *, scope: str = "all",
                                  max_pages: int = 10, limit: int = 100) -> dict:
    """Bounded cursor reads; read_page supplies an unwrapped business payload.

    `total` is null when the selected population cannot be counted. `source_total`
    keeps the initial source count (later cursor totals can be remainder counts).
    Even a complete traversal is live data, never a frozen snapshot.
    """
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 10:
        raise ValueError("需求读取页数必须为1至10")
    arguments = build_delivery_query(selected, options, schema, scope=scope, limit=limit)
    values = validate_delivery_selection(selected, options)
    server_filter = "ai_mode" in arguments
    matched, ids, cursors = {}, set(), set()
    source_total, scanned, pages = None, 0, 0
    complete, duplicate, total_exact = False, False, False
    source_limited = False
    count_usable_for_coverage = True
    for _ in range(max_pages):
        payload = read_page(dict(arguments))
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            raise ValueError("Fireplay需求分页结构无效")
        rows = payload["items"]
        if len(rows) > limit:
            raise ValueError("Fireplay需求分页超出约定上限")
        meta = payload.get("query_meta", {})
        if not isinstance(meta, Mapping):
            raise ValueError("Fireplay需求覆盖元数据无效")
        source_limited = source_limited or bool(meta.get("source_limit_reached"))
        count_usable_for_coverage = count_usable_for_coverage and meta.get("total_exact") is not False
        if pages == 0:
            count = payload.get("total")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                source_total = count
            total_exact = source_total is not None and meta.get("total_exact") is True
        pages += 1
        filtered = filter_delivery_rows(rows, values)
        if server_filter and len(filtered) != len(rows):
            raise ValueError("Fireplay返回行与服务端交付筛选不一致")
        for row in rows:
            identity = row.get("id")
            if not isinstance(identity, str) or not identity:
                raise ValueError("Fireplay需求摘要缺少有效ID")
            duplicate = duplicate or identity in ids
            ids.add(identity)
        matched.update((row["id"], row) for row in filtered)
        scanned += len(rows)
        cursor = payload.get("next_cursor")
        if cursor is None or cursor == "":
            terminal = payload.get("has_more") is False
            counted = source_total == scanned and count_usable_for_coverage
            complete = (terminal or counted) and payload.get("has_more") is not True
            complete = complete and not duplicate and not source_limited
            break
        if not isinstance(cursor, str) or cursor in cursors or len(cursor) > 2048:
            raise ValueError("Fireplay需求分页游标无效或循环")
        cursors.add(cursor)
        arguments["cursor"] = cursor
    if complete:
        total = len(matched)
        total_exact = True
    elif server_filter and total_exact:
        total = source_total
    else:
        total, total_exact = None, False
    return {"items": list(matched.values()), "total": total, "total_exact": total_exact,
            "source_total": source_total, "returned": len(matched), "scanned": scanned,
            "pages_read": pages, "complete": complete, "snapshot": False,
            "filter_mode": "server" if server_filter else "bounded_local",
            "selected_delivery_types": list(values)}
