"""Project-owned Fireplay requirement reads; never inherits a desktop MCP session."""
from __future__ import annotations

import hashlib
import json
import threading
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from video_translation.persistence.store import now
from .delivery_filter import collect_delivery_requirements, delivery_options, validate_delivery_selection


def requirement_payload(payload):
    """Accept a business data envelope, not arbitrary success-shaped content."""
    for _ in range(3):
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            break
        payload = payload["data"]
    raise ValueError("需求返回结构无法识别；请核对当前MCP合同")


class FireplayBridge:
    def __init__(self, settings, store, client=None):
        self.settings, self.store, self.client = settings, store, client
        self.mcp = None
        self.lock = threading.RLock()
        if settings.fireplay_mode == "mcp":
            from .mcp import FireplayMCP, FireplayMCPConfig
            self.mcp = FireplayMCP(FireplayMCPConfig.from_env(settings.root), client=client)
        elif settings.fireplay_mode != "bridge":
            raise ValueError("Fireplay模式必须为mcp或bridge")
        self._refresh_identity()

    def _identity_cache_id(self):
        identity = self.mcp.config.fingerprint() if self.mcp else self.settings.fireplay_url + "|" + self.settings.fireplay_token
        return "fireplay:" + hashlib.sha256((identity + "|" + self.settings.fireplay_scope).encode()).hexdigest()

    def _refresh_identity(self):
        self.cache_id = self._identity_cache_id()

    @property
    def configured(self):
        if self.mcp:
            return self.mcp.config.service == "ai_designer" and self.mcp.status()["configured"]
        return bool(self.settings.fireplay_url and self.settings.fireplay_scope)

    def connection(self, probe=False):
        if self.mcp:
            return self.mcp.status(probe=probe)
        return {"mode": "bridge", "configured": self.configured, "business_authorized": None}

    def _selected_delivery_types(self, cache_id=None):
        record = self.store.get("fireplay_filter", self.cache_id if cache_id is None else cache_id) if self.store else None
        return (record or {}).get("selected_delivery_types", ["video_translate"])

    def _snapshot_id(self, selected, cache_id=None):
        cache_id = self.cache_id if cache_id is None else cache_id
        if not self.mcp:
            return cache_id
        selection = json.dumps(sorted(set(selected)), separators=(",", ":"))
        return cache_id + ":delivery:" + hashlib.sha256(selection.encode()).hexdigest()

    def delivery_options(self):
        with self.lock:
            self._refresh_identity()
            if not self.mcp or not self.configured:
                raise ValueError("AI Designer MCP业务身份尚未配置")
            payload = self.mcp.requirement_delivery_options()
            options = delivery_options(payload)
            selected = self._selected_delivery_types()
            valid = bool(selected) and all(value in {o["value"] for o in options} for value in selected)
            return {"items": options, "selected_delivery_types": selected, "selection_valid": valid,
                    "source": "requirement_create_options", "revision": payload.get("optionsRevision")}

    def projects(self):
        # Local snapshots must not wait behind remote option reads or pagination.
        # Keep this identity local: a GET must not change an in-flight sync's key.
        cache_id = self._identity_cache_id()
        with self.store.lock:
            selected = self._selected_delivery_types(cache_id) if self.mcp else []
            snapshot = self.store.get("sync", self._snapshot_id(selected, cache_id)) or {}
        return {"items": snapshot.get("items", []), "total": snapshot.get("total"),
                "configured": self.configured, "complete": snapshot.get("complete", False),
                "last_synced_at": snapshot.get("last_synced_at"), "scope": self.settings.fireplay_scope,
                "error": snapshot.get("error"), "connection": self.connection(),
                "selected_delivery_types": selected, "snapshot": False,
                "total_exact": snapshot.get("total_exact", False),
                "source_total": snapshot.get("source_total"),
                "returned": len(snapshot.get("items", [])), "scanned": snapshot.get("scanned", 0),
                "pages_read": snapshot.get("pages_read", 0), "filter_mode": snapshot.get("filter_mode"),
                "source_kind": "requirements" if self.mcp else "legacy_bridge"}

    def brief(self, requirement_id):
        if not self.mcp or not self.configured:
            raise ValueError("AI Designer MCP业务身份尚未配置")
        requirement_id = str(UUID(requirement_id))
        payload = requirement_payload(self.mcp.requirement_list({"scope": self.settings.fireplay_scope,
                    "view": "blind_brief", "requirement_ids": [requirement_id]}))
        matches = [row for row in payload["items"] if row.get("id") == requirement_id]
        if len(matches) != 1:
            raise ValueError("需求详情未返回唯一匹配，请核对权限与ID")
        return matches[0]

    def _mcp_rows(self, selected=None, options=None):
        options = self.delivery_options()["items"] if options is None else options
        selected = self._selected_delivery_types() if selected is None else selected
        tools = {tool["name"]: tool for tool in self.mcp.list_tools()}
        if "requirement_list" not in tools:
            raise ValueError("Fireplay未提供需求读取接口")
        result = collect_delivery_requirements(
            lambda arguments: requirement_payload(self.mcp.requirement_list(arguments)),
            selected, options, tools["requirement_list"]["inputSchema"], scope=self.settings.fireplay_scope)
        rows = []
        labels = {option["value"]: option["label"] for option in options}
        for row in result["items"]:
            UUID(row["id"])
            rows.append({"id": row["id"], "code": str(row.get("code", "")), "title": str(row["title"]),
                         "status": str(row.get("status", "unknown")), "updated_at": row.get("updatedAt"),
                         "asset_type": row.get("assetType"), "ai_mode": row["aiMode"],
                         "delivery_label": labels.get(row["aiMode"], row["aiMode"]),
                         "source": "fireplay", "external_only": True})
        return {**result, "items": rows}

    def _legacy_rows(self):
        url = urlsplit(self.settings.fireplay_url)
        if url.scheme not in {"http", "https"} or url.username or url.password:
            raise ValueError("桥接地址必须是无内嵌凭据的HTTP(S)地址")
        if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("远端桥接必须使用HTTPS")
        headers = {"Authorization": f"Bearer {self.settings.fireplay_token}"} if self.settings.fireplay_token else {}
        items, seen, cursors = [], set(), set()
        cursor, complete, total = None, False, 0
        with (httpx.Client(timeout=30, follow_redirects=False) if self.client is None else _Borrow(self.client)) as client:
            for _ in range(100):
                params = {"scope": self.settings.fireplay_scope, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                response = client.get(self.settings.fireplay_url.rstrip("/") + "/projects", params=params, headers=headers)
                response.raise_for_status()
                if len(response.content) > 5 * 1024 * 1024:
                    raise ValueError("单页同步结果过大")
                payload = response.json()
                if payload.get("scope") != self.settings.fireplay_scope:
                    raise ValueError("桥接返回范围与请求不一致")
                total = payload["total"]
                if not isinstance(total, int) or total < 0 or not isinstance(payload["items"], list):
                    raise ValueError("桥接返回结构不符合合同")
                for item in payload["items"]:
                    key = str(item["id"])
                    if key not in seen:
                        seen.add(key)
                        items.append({"id": key, "title": str(item["title"]), "status": str(item["status"]),
                                      "updated_at": item.get("updated_at"), "source": "fireplay", "external_only": True})
                cursor = payload.get("next_cursor")
                if not cursor:
                    complete = len(items) == total
                    break
                if not isinstance(cursor, str) or cursor in cursors:
                    raise ValueError("桥接分页游标循环或无效")
                cursors.add(cursor)
        return items, total, complete

    def sync(self, delivery_types=None):
        from .mcp import FireplayMCPError
        with self.lock:
            self._refresh_identity()
            if not self.configured:
                raise ValueError("Fireplay地址、业务身份或范围尚未配置；桌面MCP登录不会自动继承")
            selected, options = [], None
            if self.mcp:
                options = self.delivery_options()["items"]
                selected = list(validate_delivery_selection(
                    self._selected_delivery_types() if delivery_types is None else delivery_types, options))
                self.store.put("fireplay_filter", {"id": self.cache_id, "selected_delivery_types": selected})
            elif delivery_types is not None:
                raise ValueError("旧版桥接未提供AI承接交付筛选合同")
            snapshot_id = self._snapshot_id(selected)
            previous = self.store.get("sync", snapshot_id) or {}
            try:
                if self.mcp:
                    result = self._mcp_rows(selected, options)
                else:
                    items, total, complete = self._legacy_rows()
                    result = {"items": items, "total": total, "complete": complete,
                              "source_total": total, "total_exact": complete, "scanned": len(items)}
                self.store.put("sync", {**result, "id": snapshot_id,
                               "created_at": previous.get("created_at", now()), "last_synced_at": now(),
                               "selected_delivery_types": selected, "scope": self.settings.fireplay_scope,
                               "error": None if result["complete"] else "当前为有界读取，尚未证实完整覆盖"})
            except (httpx.HTTPError, FireplayMCPError, ValueError, KeyError, TypeError) as exc:
                message = str(exc) if isinstance(exc, FireplayMCPError) else "同步失败；检查认证、范围及需求合同"
                self.store.put("sync", {**previous, "id": snapshot_id, "complete": False,
                               "error": message, "created_at": previous.get("created_at", now())})
                raise ValueError("Fireplay同步失败，旧数据不代表最新全量；" + message) from None
            return self.projects()

    def close(self):
        if self.mcp:
            self.mcp.close()


class _Borrow:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *args):
        pass
