"""Project-owned AuthHub rotation; uncertain refresh requests are never replayed."""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

import httpx

from video_translation.persistence.store import WorkerLock, now

REFRESH_URL = "https://authhub.cms1.cc/api/v1/auth/refresh"
MAX_BYTES = 64 * 1024
_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


class CredentialError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _token(value):
    if not isinstance(value, str) or not value or len(value) > 16384 or any(c.isspace() or c == "\x00" for c in value):
        raise CredentialError("invalid_credentials", "Fireplay 本项目登录凭据格式无效，请重新登录")
    return value


def token_expiry(token):
    """An unsigned local expiry hint, never an authorization decision."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        value = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))).get("exp")
        if type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 253402300799:
            return float(value)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        pass
    return None


def read_credentials(path):
    path = Path(path)
    if not path.is_file():
        return {}
    if path.stat().st_size > MAX_BYTES:
        raise CredentialError("invalid_credentials", "Fireplay 本项目凭据文件超过上限")
    text = path.read_text("utf-8").strip()
    if not text.startswith("{"):
        return {"access_token": _token(text)} if text else {}
    try:
        data = json.loads(text)
        if data.get("version") != 1 or not isinstance(data.get("renewal"), dict):
            raise ValueError()
        _token(data.get("access_token"))
        _token(data.get("refresh_token"))
        if data["renewal"].get("status") not in {"ready", "pending", "unknown", "login_required"}:
            raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{32}", data.get("session_id", "")):
            raise ValueError()
        return data
    except (ValueError, TypeError, AttributeError):
        raise CredentialError("invalid_credentials", "Fireplay 本项目凭据文件无效，请重新登录") from None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CredentialSession:
    def __init__(self, path, *, client=None):
        self.path = Path(path)
        self.client = client
        with _locks_guard:
            self.lock = _locks.setdefault(os.path.normcase(str(self.path.resolve())), threading.RLock())

    def status(self):
        data = read_credentials(self.path)
        state = data.get("renewal", {}).get("status", "legacy")
        return {"automatic_renewal": bool(data.get("refresh_token")),
                "renewal_status": "unknown" if state == "pending" else state}

    def install(self, access_token, refresh_token=None):
        access_token = _token(access_token)
        if refresh_token is not None:
            refresh_token = _token(refresh_token)
        with self.lock:
            guard = WorkerLock(self.path.with_suffix(".rotation.lock"))
            try:
                guard.acquire()
            except RuntimeError:
                raise CredentialError("refresh_busy", "另一进程正在更新本项目登录，请稍后重试") from None
            try:
                data = {"version": 1, "session_id": uuid4().hex,
                        "access_token": access_token, "refresh_token": refresh_token,
                        "renewal": {"status": "ready", "updated_at": now()}} if refresh_token else access_token
                _write(self.path, data)
            finally:
                guard.release()

    def ensure_fresh(self, *, force=False, rejected_token=None):
        with self.lock:
            data = read_credentials(self.path)
            if not data.get("refresh_token"):
                return False
            if rejected_token is not None and data["access_token"] != rejected_token:
                return False
            state = data["renewal"]["status"]
            if state in {"pending", "unknown", "login_required"}:
                raise CredentialError("refresh_" + ("unknown" if state == "pending" else state),
                                      "Fireplay 自动续期未确认或已失效，请在本项目重新登录；不会重复提交原续期")
            expiry = token_expiry(data["access_token"])
            if not force and (expiry is None or expiry > time.time() + 120):
                return False
            guard = WorkerLock(self.path.with_suffix(".rotation.lock"))
            try:
                guard.acquire()
            except RuntimeError:
                raise CredentialError("refresh_busy", "另一进程正在更新本项目登录，请稍后重试") from None
            try:
                # Another process may have rotated before this OS lock was acquired.
                if read_credentials(self.path) != data:
                    return False
                data["renewal"] = {"status": "pending", "attempt_id": uuid4().hex, "started_at": now()}
                _write(self.path, data)
                try:
                    result = self._refresh(data["refresh_token"])
                    access, refresh = _token(result.get("access_token")), _token(result.get("refresh_token"))
                    if token_expiry(access) is None or token_expiry(access) <= time.time():
                        raise CredentialError("refresh_unknown", "Fireplay 续期返回无效凭据，请重新登录")
                except Exception as exc:
                    code = exc.code if isinstance(exc, CredentialError) else "refresh_unknown"
                    data["renewal"].update(status="login_required" if code == "refresh_rejected" else "unknown",
                                          finished_at=now())
                    _write(self.path, data)
                    raise CredentialError(code, "Fireplay 自动续期未成功确认，请重新登录；不会重放原续期请求") from None
                updated = {"version": 1, "session_id": data["session_id"],
                           "access_token": access, "refresh_token": refresh,
                           "renewal": {"status": "ready", "updated_at": now()}}
                # A failed final save leaves the durable pending intent, preventing replay.
                _write(self.path, updated)
                return True
            finally:
                guard.release()

    def _refresh(self, refresh_token):
        if self.client is None:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
                return self._request(client, refresh_token)
        return self._request(self.client, refresh_token)

    @staticmethod
    def _request(client, refresh_token):
        with client.stream("POST", REFRESH_URL,
                           json={"refresh_token": refresh_token, "system_code": "authhub"},
                           headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                           follow_redirects=False, timeout=15) as response:
            if response.status_code in {400, 401, 403}:
                raise CredentialError("refresh_rejected", "Fireplay 登录续期已失效")
            if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                raise CredentialError("refresh_unknown", "Fireplay 登录续期结果未确认")
            payload = bytearray()
            for part in response.iter_bytes():
                payload.extend(part)
                if len(payload) > MAX_BYTES:
                    raise CredentialError("refresh_unknown", "Fireplay 登录续期响应超过上限")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise CredentialError("refresh_unknown", "Fireplay 登录续期响应无效")
            return result
