"""Fixed, read-only lookup of existing Fireplay originals by canonical YouTube ID.

The reviewed material_youtube_original_lookup contract explicitly returns the
original R2 resource. Generic material videoUrl/videoR2Url and requirement delivery
materials are not equivalent proof. Returned URLs are internal download inputs;
callers must never serialize signed URLs into public UI, errors or logs.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

_MESSAGES = {
    "invalid_youtube_id": "需要标准的11位 YouTube 视频 ID。",
    "library_contract_unverified": "素材库原视频查询合同尚未核验；请补传原视频。",
    "library_read_failed": "素材库只读查询失败；请检查登录或补传原视频。",
    "library_response_unverified": "素材库返回的原视频身份或资源无法核验；请补传原视频。",
}


class LibraryLookupError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else "library_read_failed"
        super().__init__(_MESSAGES[self.code])


def _uuid(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _youtube_id(url):
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 80, 443):
            return None
        host = parsed.hostname
        if host == "youtu.be":
            candidate = parsed.path.lstrip("/")
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            parts = parsed.path.split("/")
            values = parse_qs(parsed.query).get("v", [])
            if parsed.path == "/watch" and len(values) == 1:
                candidate = values[0]
            elif len(parts) == 3 and parts[1] in {"embed", "shorts", "live"}:
                candidate = parts[2]
            else:
                return None
        else:
            return None
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None
    except ValueError:
        return None


def _download_url(value):
    """Syntax check only; the downloader must check DNS and every redirect too."""
    if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment:
            return None
        if parsed.port not in (None, 443):
            return None
        if host in {"localhost", "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}:
            return None
        if host.endswith((".localhost", ".local", ".internal", ".youtube.com", ".youtu.be")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host:
                return None
    except ValueError:
        return None
    return value


class LibraryResolver:
    """Use only mcp.library_original_lookup, never generic tool dispatch.

    The MCP wrapper pins the business tool and validates its catalog contract.
    The official query is scoped to the caller's visible library. It is not a
    library census and exposes no paging contract: metadata.complete stays false.
    All proven matches are preserved; the caller chooses if there is ambiguity.
    """
    def __init__(self, mcp):
        self.mcp = mcp
        self.metadata = self._metadata()

    @staticmethod
    def _metadata():
        return {"origin": "material_library", "complete": False, "pages": 0,
                "candidates_checked": 0, "code": "not_started",
                "message": "尚未查询素材库。"}

    def resolve(self, youtube_id):
        self.metadata = self._metadata()
        if not isinstance(youtube_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_id):
            raise LibraryLookupError("invalid_youtube_id")
        lookup = getattr(self.mcp, "library_original_lookup", None)
        if not callable(lookup):
            raise LibraryLookupError("library_contract_unverified")
        try:
            payload = lookup(youtube_id)
        except Exception:
            self.metadata.update(code="library_read_failed", message=_MESSAGES["library_read_failed"])
            raise LibraryLookupError("library_read_failed") from None
        try:
            result = self._parse(payload, youtube_id)
        except LibraryLookupError:
            self.metadata.update(code="library_response_unverified", message=_MESSAGES["library_response_unverified"])
            raise
        self.metadata.update(pages=1, candidates_checked=len(payload["items"]),
                             code="found" if result else "missing",
                             message=("已找到同一视频的库内原片；结果仅限当前身份可见范围，不证明全库覆盖。"
                                      if result else "当前素材库查询未找到可核验原片，请补传原视频。"))
        return result

    @staticmethod
    def _parse(payload, youtube_id):
        if not isinstance(payload, dict) or payload.get("status") == "error" or payload.get("success") is False:
            raise LibraryLookupError("library_response_unverified")
        rows = payload.get("items")
        if not isinstance(rows, list) or not rows or len(rows) > 50:
            raise LibraryLookupError("library_response_unverified")
        result, seen = [], set()
        for row in rows:
            if (not isinstance(row, dict) or row.get("youtube_video_id") != youtube_id
                    or _youtube_id(row.get("youtube_url")) != youtube_id):
                raise LibraryLookupError("library_response_unverified")
            if row.get("status") == "missing":
                if row.get("video_r2_url"):
                    raise LibraryLookupError("library_response_unverified")
                continue
            if row.get("status") != "found":
                raise LibraryLookupError("library_response_unverified")
            material_id = _uuid(row.get("material_id"))
            resource_id = _uuid(row.get("source_resource_id"))
            url = _download_url(row.get("video_r2_url"))
            if not material_id or not resource_id or not url:
                raise LibraryLookupError("library_response_unverified")
            identity = (material_id, resource_id)
            if identity in seen:
                continue
            name = row.get("material_name")
            if not isinstance(name, str) or len(name) > 500 or "://" in name:
                name = "素材库原视频"
            result.append({"id": material_id, "resource_id": resource_id, "name": name, "url": url,
                           "youtube_id": youtube_id, "origin": "material_library"})
            seen.add(identity)
        return result
