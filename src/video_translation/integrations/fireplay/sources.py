"""Traceable, unconfirmed brief references and bounded direct-video downloads.

A candidate is never proof of source role. The application must obtain explicit
user confirmation before fetch(). Delivery materials and landing pages are not
inputs to this module. YouTube extraction remains deliberately unavailable until
its network boundary can meet the same guarantees as direct downloads.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import http.client
import importlib.util
import ipaddress
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from uuid import uuid4

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"})
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
MAX_URL_BYTES = 8192


class SourceFetchError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped or ip.sixtofour or ip.teredo:
            return False
        if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return False
    return True


def _validate_url(url: str):
    if not isinstance(url, str) or not url or len(url.encode("utf-8")) > MAX_URL_BYTES:
        raise SourceFetchError("invalid_url", "引用地址为空或过长")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url) or "\\" in url:
        raise SourceFetchError("invalid_url", "引用地址格式无效")
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError()
        if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise ValueError()
        if parsed.netloc.endswith(":"):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if "%" in host or host.endswith("."):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
                raise ValueError()
            if "." not in host or ".." in host or host.endswith(".localhost"):
                raise ValueError()
        else:
            if not _public_ip(host):
                raise ValueError()
    except (ValueError, UnicodeError):
        raise SourceFetchError("unsafe_url", "仅允许无登录信息的公开 HTTPS 地址及标准443端口") from None
    return parsed, host


def _youtube_id(parsed, host: str) -> str | None:
    if host not in YOUTUBE_HOSTS:
        return None
    values = parse_qs(parsed.query, keep_blank_values=True)
    if "list" in values:
        return None
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.removeprefix("/")
    elif parsed.path == "/watch":
        candidates = values.get("v", [])
        candidate = candidates[0] if len(candidates) == 1 else ""
    elif parsed.path.startswith("/shorts/"):
        candidate = parsed.path.removeprefix("/shorts/")
    else:
        return None
    return candidate if VIDEO_ID.fullmatch(candidate) else None


def _youtube_installed() -> bool:
    try:
        return importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError, AttributeError):
        return False


class _References(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        if not self.ignored:
            self.parts.extend(value for key, value in attrs if key in {"href", "src"} and value)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def sources_from_brief(brief: dict) -> list[dict]:
    """Return at most 32 reference candidates; every one needs source confirmation."""
    results, seen = [], set()
    for field in ("refText", "description"):
        text = brief.get(field)
        if not isinstance(text, str) or len(text) > 65536:
            continue
        parser = _References()
        parser.feed(text)
        parser.close()
        for part in parser.parts:
            for match in URL_PATTERN.finditer(part):
                url = match.group().rstrip(".,;!?。，；！？、)]}〉》")
                if url in seen:
                    continue
                seen.add(url)
                candidate = {
                    "id": hashlib.sha256(url.encode()).hexdigest()[:24],
                    "name": "外部视频引用",
                    "kind": "unsupported",
                    "downloadable": False,
                    "origin_field": field,
                    "url": url,
                    "requires_confirmation": True,
                }
                try:
                    parsed, host = _validate_url(url)
                    name = Path(unquote(parsed.path)).name
                    candidate["name"] = re.sub(r"[\x00-\x1f\x7f]", "", name)[:120] or host
                    if host in YOUTUBE_HOSTS:
                        video_id = _youtube_id(parsed, host)
                        if not video_id:
                            candidate["reason"] = "仅支持单个YouTube视频，不支持播放列表或其他页面"
                        else:
                            candidate.update(
                                kind="youtube",
                                name="YouTube 视频引用",
                                youtube_id=video_id,
                                url=f"https://www.youtube.com/watch?v={video_id}",
                            )
                            candidate["id"] = hashlib.sha256(candidate["url"].encode()).hexdigest()[:24]
                            candidate["reason"] = (
                                "已检测到yt-dlp，但受控YouTube下载尚未启用；请手动上传原片"
                                if _youtube_installed()
                                else "未安装可选yt-dlp；当前请手动上传原片，安装后仍需启用受控下载适配"
                            )
                    elif Path(unquote(parsed.path)).suffix.lower() in VIDEO_SUFFIXES:
                        candidate.update(kind="reference_video", downloadable=True)
                    else:
                        candidate["reason"] = "链接不是支持的视频文件直链，请补传原片"
                except SourceFetchError as exc:
                    candidate["reason"] = str(exc)
                if not any(item["id"] == candidate["id"] for item in results):
                    results.append(candidate)
                if len(results) == 32:
                    return results
    return results


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceFetchError("download_timeout", "原片下载超时，请重试或手动上传")
    return remaining


def _resolve_public(host: str, deadline: float):
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError:
            result.put(None)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise SourceFetchError("download_timeout", "原片地址解析超时") from None
    if not addresses or any(
        entry[0] not in {socket.AF_INET, socket.AF_INET6} or not _public_ip(entry[4][0])
        for entry in addresses
    ):
        raise SourceFetchError("unsafe_address", "原片地址无法解析为公开网络地址")
    return addresses[0][0], addresses[0][4]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Resolve once outside this class; connect to that exact IP, verify original host."""

    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._address = address
        self._tls_socket = None
        self._raw_socket = None

    def connect(self):
        family, sockaddr = self._address
        raw = socket.socket(family, socket.SOCK_STREAM)
        self._raw_socket = raw
        try:
            raw.settimeout(self.timeout)
            raw.connect(sockaddr)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self._tls_socket = self.sock
            self.sock.do_handshake()
        except BaseException:
            raw.close()
            raise

    def abort(self):
        sock = self._tls_socket or self._raw_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()


def _directory(path: Path) -> Path:
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise SourceFetchError("unsafe_directory", "下载目录不能经过链接或重解析目录")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _video_header(data: bytes) -> bool:
    return len(data) >= 12 and (
        data[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"}
        or data[:4] == b"\x1a\x45\xdf\xa3"
        or (data[:4] == b"RIFF" and data[8:12] == b"AVI ")
    )


class SourceFetcher:
    """Fetch a caller-confirmed direct reference. No shared cookies, auth or proxies."""

    def __init__(self, settings):
        self.max_bytes = settings.max_upload_bytes
        self.timeout = float(getattr(settings, "source_download_timeout_seconds", 120))
        if not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool) or self.max_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if not 0 < self.timeout <= 600:
            raise ValueError("source_download_timeout_seconds must be in (0, 600]")

    def fetch(self, source: dict, directory: Path, *, trusted_hosts=None) -> Path:
        if source.get("kind") == "youtube":
            raise SourceFetchError("youtube_unavailable", "受控YouTube下载尚未启用，请手动上传原片")
        library = source.get("kind") == "library_video"
        if (
            source.get("kind") not in {"reference_video", "library_video"}
            or source.get("downloadable") is not True
        ):
            raise SourceFetchError("unsupported_source", "此引用不能自动下载，请补传原片")
        url = source.get("url")
        parsed, _ = _validate_url(url)
        extension = Path(unquote(parsed.path)).suffix.lower()
        if extension not in VIDEO_SUFFIXES and not library:
            raise SourceFetchError("unsupported_source", "原片引用不是支持的视频直链")
        directory = _directory(directory)
        deadline, temporary = time.monotonic() + self.timeout, None
        try:
            for hop in range(6):
                parsed, host = _validate_url(url)
                if trusted_hosts is not None and host not in trusted_hosts:
                    raise SourceFetchError("untrusted_host", "原片或重定向地址超出已授权的精确来源主机")
                address = _resolve_public(host, deadline)
                connection = _PinnedHTTPSConnection(host, address, _remaining(deadline))
                timer = threading.Timer(_remaining(deadline), connection.abort)
                timer.daemon = True
                timer.start()
                response = None
                try:
                    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
                    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~[]")
                    target = urlunsplit(("", "", path, query, ""))
                    connection.request(
                        "GET",
                        target,
                        headers={
                            "Accept": "video/*, application/octet-stream",
                            "Accept-Encoding": "identity",
                            "Connection": "close",
                            "User-Agent": "VideoTranslationSource/1.0",
                        },
                    )
                    response = connection.getresponse()
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        if not location or hop == 5:
                            raise SourceFetchError("redirect_limit", "原片地址重定向无效或次数过多")
                        url = urljoin(url, location)
                        _validate_url(url)
                        continue
                    if response.status != 200:
                        raise SourceFetchError(
                            "source_http_error", f"原片服务返回HTTP {response.status}，请检查引用或补传"
                        )
                    encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
                    media_type = (response.getheader("Content-Type") or "").split(";", 1)[0].lower().strip()
                    if encoding != "identity" or (
                        media_type
                        and not (
                            media_type.startswith("video/")
                            or media_type in {"application/octet-stream", "application/mp4"}
                        )
                    ):
                        raise SourceFetchError("invalid_media", "引用返回的不是受支持的视频文件")
                    if extension not in VIDEO_SUFFIXES:
                        extension = {
                            "video/mp4": ".mp4",
                            "video/webm": ".webm",
                            "video/quicktime": ".mov",
                            "video/x-matroska": ".mkv",
                            "video/x-msvideo": ".avi",
                        }.get(media_type)
                        if extension is None:
                            raise SourceFetchError(
                                "invalid_media", "无后缀素材必须返回明确的视频Content-Type"
                            )
                    length = response.getheader("Content-Length")
                    if length is not None and (not length.isdecimal() or int(length) > self.max_bytes):
                        raise SourceFetchError("source_too_large", "原片超过大小限制或响应长度无效")
                    with tempfile.NamedTemporaryFile(
                        dir=directory, prefix=".source-", suffix=".part", delete=False
                    ) as handle:
                        temporary = Path(handle.name)
                        size, prefix = 0, b""
                        while True:
                            _remaining(deadline)
                            chunk = response.read(min(65536, self.max_bytes - size + 1))
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise SourceFetchError("source_too_large", "原片超过配置的大小限制")
                            prefix = (prefix + chunk)[:32]
                            if len(prefix) >= 12 and not _video_header(prefix):
                                raise SourceFetchError("invalid_media", "引用内容不是受支持的视频容器")
                            handle.write(chunk)
                        if not _video_header(prefix) or (length is not None and int(length) != size):
                            raise SourceFetchError("invalid_media", "视频文件不完整或容器格式无效")
                        _remaining(deadline)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if _directory(directory) != directory:
                        raise SourceFetchError("unsafe_directory", "下载目录发生变化")
                    output = directory / ("source-" + uuid4().hex + extension)
                    os.replace(temporary, output)
                    temporary = None
                    return output
                finally:
                    timer.cancel()
                    if response is not None:
                        response.close()
                    connection.abort()
        except SourceFetchError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise SourceFetchError("download_failed", "原片下载失败，请检查引用或手动上传") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        raise SourceFetchError("download_failed", "无法获取原片")
