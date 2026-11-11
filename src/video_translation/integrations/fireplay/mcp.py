"""Independent read-only Fireplay MCP client. Campaign tasks are diagnostics, not requirements."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Mapping

import httpx
import jsonschema
from referencing.exceptions import Unresolvable

SERVICES = {
    "campaigns": ("https://campaigns.fireplay.ai/api/mcp", "FIREPLAY_CAMPAIGNS_TOKEN"),
    "ai_designer": ("https://ai-designer-api.fireplay.ai/api/mcp/catalog/v1", "FIREPLAY_AI_DESIGNER_TOKEN"),
}
DEFAULT_URL, DEFAULT_TOKEN_ENV = SERVICES["ai_designer"]
PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
TASK_TOOL = "fireplay_task_query"
REFERENCE_TOOL = "fireplay_reference_query"
ALLOWED_ACTIONS = {TASK_TOOL: frozenset({"list", "get", "details"}),
                   REFERENCE_TOOL: frozenset({"user_info"})}
CONFIG_KEYS = {"enabled", "service", "url", "token_env", "timeout_seconds", "max_response_bytes"}


class FireplayMCPError(RuntimeError):
    """Safe diagnostics: no remote bodies, credentials, or session IDs."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


def _error(code: str, message: str, *, retryable: bool = False):
    return FireplayMCPError(code, message, retryable=retryable)


def _bounded_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise _error("invalid_request", f"{name} 超出允许范围")
    try:
        result = int(value)
        if result != float(value) or not 1 <= result <= 100:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise _error("invalid_request", f"{name} 超出允许范围") from None
    return result


@dataclass(frozen=True)
class FireplayMCPConfig:
    enabled: bool = False
    service: str = "ai_designer"
    url: str = ""
    token_env: str = ""
    timeout_seconds: float = 15.0
    max_response_bytes: int = 2 * 1024 * 1024
    _environment: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    _credential_file: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        # New services require an explicitly reviewed facade; never send tokens to arbitrary origins.
        if self.service not in SERVICES:
            raise _error("invalid_config", "不支持该 Fireplay MCP 服务")
        expected_url, expected_token_env = SERVICES[self.service]
        object.__setattr__(self, "url", self.url or expected_url)
        object.__setattr__(self, "token_env", self.token_env or expected_token_env)
        if self.url != expected_url:
            raise _error("invalid_config", "仅支持已审核的 Fireplay 服务 HTTPS MCP 地址")
        if not isinstance(self.enabled, bool):
            raise _error("invalid_config", "enabled 必须为布尔值")
        if not isinstance(self.token_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", self.token_env):
            raise _error("invalid_config", "凭据必须使用合法环境变量名称引用")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise _error("invalid_config", "MCP 超时配置无效")
        if not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 60:
            raise _error("invalid_config", "MCP 超时必须为 1 至 60 秒")
        if isinstance(self.max_response_bytes, bool) or not isinstance(self.max_response_bytes, int):
            raise _error("invalid_config", "MCP 响应上限配置无效")
        if not 1024 <= self.max_response_bytes <= 8 * 1024 * 1024:
            raise _error("invalid_config", "MCP 响应上限必须为 1 KiB 至 8 MiB")

    @classmethod
    def from_env(cls, root: Path, env: Mapping[str, str] | None = None) -> FireplayMCPConfig:
        environment = os.environ if env is None else env
        data = {}
        path = Path(root) / "runtime/config/fireplay.json"
        try:
            if path.is_file():
                if path.stat().st_size > 16384:
                    raise ValueError()
                data = json.loads(path.read_text("utf-8-sig"))
                if not isinstance(data, dict) or set(data) - CONFIG_KEYS:
                    raise ValueError()
            if "VT_FIREPLAY_SERVICE" in environment:
                data["service"] = environment["VT_FIREPLAY_SERVICE"]
            for variable, name in {"VT_FIREPLAY_URL": "url", "VT_FIREPLAY_TOKEN_ENV": "token_env",
                                   "VT_FIREPLAY_CAMPAIGNS_URL": "url",
                                   "VT_FIREPLAY_CAMPAIGNS_TOKEN_ENV": "token_env"}.items():
                if variable in environment:
                    data[name] = environment[variable]
            if "VT_FIREPLAY_ENABLED" in environment:
                value = environment["VT_FIREPLAY_ENABLED"].lower()
                if value not in {"0", "1", "true", "false"}:
                    raise ValueError()
                data["enabled"] = value in {"1", "true"}
            if "VT_FIREPLAY_TIMEOUT_SECONDS" in environment:
                data["timeout_seconds"] = float(environment["VT_FIREPLAY_TIMEOUT_SECONDS"])
            if "VT_FIREPLAY_MAX_RESPONSE_BYTES" in environment:
                data["max_response_bytes"] = int(environment["VT_FIREPLAY_MAX_RESPONSE_BYTES"])
            return cls(**data, _environment=environment,
                       _credential_file=Path(root) / "runtime/config/fireplay-login-token.txt")
        except (OSError, ValueError, TypeError):
            raise _error("invalid_config", "Fireplay 本机配置无效；仅接受地址、环境变量引用和传输限制") from None

    def _token(self) -> str:
        environment = os.environ if self._environment is None else self._environment
        token = environment.get(self.token_env, "")
        if self._credential_file is not None and self._credential_file.is_file():
            if self._credential_file.stat().st_size > 16384:
                raise _error("invalid_credentials", "Fireplay 登录凭据文件无效")
            token = self._credential_file.read_text("utf-8").strip()
        if not isinstance(token, str) or len(token) > 16384 or any(c in token for c in "\r\n\x00"):
            raise _error("invalid_credentials", "Fireplay 凭据格式无效")
        return token.strip()

    def fingerprint(self) -> str:
        return hashlib.sha256((self.url + "\x00" + self._token()).encode()).hexdigest()

    def credential_expiry(self) -> float | None:
        """Expiry is a local UX hint, never proof of identity or authorization."""
        try:
            parts = self._token().split(".")
            if len(parts) != 3:
                return None
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            expiry = payload.get("exp")
            if type(expiry) not in (int, float) or not math.isfinite(expiry) or not 0 <= expiry <= 253402300799:
                return None
            return float(expiry)
        except (ValueError, TypeError, AttributeError, UnicodeError):
            return None

    def public_dict(self) -> dict:
        return {"enabled": self.enabled, "service": self.service, "url": self.url, "token_env": self.token_env,
                "timeout_seconds": self.timeout_seconds, "max_response_bytes": self.max_response_bytes}


class FireplayMCP:
    """Serialized protocol session; no model execution or arbitrary public tool forwarding."""

    def __init__(self, config: FireplayMCPConfig, client: httpx.Client | None = None):
        self.config = config
        self._client = client
        self._owned_client = client is None
        self._counter = 0
        self._lock = threading.RLock()
        self._credential_identity = config.fingerprint()
        self._reset()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        with self._lock:
            if self._owned_client and self._client is not None:
                self._client.close()
                self._client = None
            self._reset()

    def _reset(self):
        self._session_id = None
        self._initialized = False
        self._tools = None
        self._protocol = PROTOCOL_VERSION

    def status(self, probe: bool = False) -> dict:
        credential = bool(self.config._token())
        result = {"mode": "mcp", "enabled": self.config.enabled, "server": self.config.service,
                  "transport": "streamable-http", "url": self.config.url, "token_env": self.config.token_env,
                  "credential_configured": credential, "credentials_available": credential,
                  "configured": self.config.enabled and credential, "probe": probe,
                  "connected": False, "business_authorized": None, "ready": False}
        if not self.config.enabled:
            result["code"] = "disabled"
            return result
        expiry = self.config.credential_expiry()
        if expiry is not None:
            result["credential_expires_at"] = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
            if expiry <= time.time():
                result.update(code="credentials_expired", message="Fireplay 登录已过期，请更新本项目的访问凭据后重新检查连接。", retryable=False)
                return result
        result["code"] = "configured" if credential else "credentials_missing"
        if probe:
            try:
                tools = self.list_tools()
                result.update(connected=True, available_tools=[t["name"] for t in tools])
                required = TASK_TOOL if self.config.service == "campaigns" else "requirement_list"
                result["ready"] = credential and required in result["available_tools"]
                if required not in result["available_tools"]:
                    result["code"] = "required_tool_missing"
                elif credential:
                    result["code"] = "protocol_ready"
            except FireplayMCPError as exc:
                result.update(exc.as_dict())
        return result

    def _require_enabled(self):
        if not self.config.enabled:
            raise _error("disabled", "Fireplay MCP 尚未启用")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "Accept-Encoding": "identity"}
        token = self.config._token()
        expiry = self.config.credential_expiry()
        if expiry is not None and expiry <= time.time():
            raise _error("credentials_expired", "Fireplay 登录已过期，请更新本项目的访问凭据后重新检查连接。")
        if token:
            headers["Authorization"] = "Bearer " + token
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._protocol
        return headers

    @staticmethod
    def _json(raw: bytes) -> dict:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise _error("invalid_response", "MCP 返回无法解析的数据") from None
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise _error("invalid_response", "MCP 返回无效协议对象")
        return value

    def _read_response(self, response: httpx.Response, request_id: int, deadline: float) -> dict:
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise _error("unsupported_encoding", "MCP 响应不支持压缩编码")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.config.max_response_bytes:
            raise _error("response_too_large", "MCP 响应超过大小上限")
        kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if kind not in {"application/json", "text/event-stream"}:
            raise _error("invalid_content_type", "MCP 返回不支持的内容类型")
        size = 0
        buffer = b""
        data_lines: list[bytes] = []
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline:
                raise _error("timeout", "MCP 查询超过时限", retryable=True)
            size += len(chunk)
            if size > self.config.max_response_bytes:
                raise _error("response_too_large", "MCP 响应超过大小上限")
            buffer += chunk
            if kind == "application/json":
                continue
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line.startswith(b"data:"):
                    data_lines.append(line[5:].lstrip(b" "))
                elif not line and data_lines:
                    message = self._json(b"\n".join(data_lines))
                    data_lines.clear()
                    if message.get("id") == request_id:
                        return message  # Stop before a persistent SSE connection closes.
        if kind == "text/event-stream":
            raise _error("incomplete_response", "MCP 事件流未返回对应请求结果")
        message = self._json(buffer)
        if message.get("id") != request_id:
            raise _error("invalid_response", "MCP 返回的请求标识不匹配")
        return message

    def _post(self, method: str, params: dict | None = None, *, notification: bool = False) -> dict:
        self._require_enabled()
        self._counter += 1
        request_id = self._counter
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = request_id
        if params is not None:
            body["params"] = params
        if self._client is None:
            self._client = httpx.Client(trust_env=False, follow_redirects=False)
        deadline = time.monotonic() + self.config.timeout_seconds
        try:
            with self._client.stream("POST", self.config.url, json=body, headers=self._headers(),
                                     timeout=self.config.timeout_seconds, follow_redirects=False) as response:
                if response.status_code in {401, 403}:
                    raise _error("unauthorized", "Fireplay 身份无效或无查询权限")
                if response.status_code == 404 and self._session_id:
                    raise _error("invalid_session", "MCP 会话已失效", retryable=True)
                if response.status_code >= 300:
                    raise _error("http_error", "MCP 请求失败", retryable=response.status_code >= 500)
                if method == "initialize":
                    session = response.headers.get("Mcp-Session-Id")
                    if session and (len(session) > 512 or not re.fullmatch(r"[\x21-\x7e]+", session)):
                        raise _error("invalid_response", "MCP 会话标识格式无效")
                    self._session_id = session
                if notification:
                    if response.status_code not in {200, 202, 204}:
                        raise _error("invalid_response", "MCP 初始化通知未被接受")
                    return {}
                message = self._read_response(response, request_id, deadline)
        except httpx.TimeoutException:
            raise _error("timeout", "MCP 查询超过时限", retryable=True) from None
        except httpx.HTTPError:
            raise _error("transport_error", "无法连接 Fireplay MCP", retryable=True) from None
        if "error" in message:
            error = message.get("error")
            text = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
            if self._session_id and any(x in text for x in ("session not found", "invalid session", "session expired")):
                raise _error("invalid_session", "MCP 会话已失效", retryable=True)
            raise _error("protocol_error", "MCP 服务返回协议错误")
        result = message.get("result")
        if not isinstance(result, dict):
            raise _error("invalid_response", "MCP 结果结构无效")
        return result

    def _initialize(self):
        identity = self.config.fingerprint()
        if identity != self._credential_identity:
            self._reset()
            self._credential_identity = identity
        if self._initialized:
            return
        self._reset()
        result = self._post("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "video-translation-v2", "version": "0.1.0"}})
        version = result.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS or not isinstance(result.get("capabilities"), dict):
            raise _error("unsupported_protocol", "MCP 协议版本或能力声明不兼容")
        if "tools" not in result["capabilities"]:
            raise _error("required_tool_missing", "MCP 服务未提供工具能力")
        self._protocol = version
        self._initialized = True
        try:
            self._post("notifications/initialized", notification=True)
        except FireplayMCPError:
            self._reset()
            raise

    def _discover(self) -> dict[str, dict]:
        self._initialize()
        if self._tools is not None:
            return self._tools
        tools = {}
        cursor = None
        seen = set()
        for _ in range(8):
            result = self._post("tools/list", {"cursor": cursor} if cursor else {})
            entries = result.get("tools")
            if not isinstance(entries, list) or len(entries) > 1000:
                raise _error("invalid_response", "MCP 工具清单无效")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    raise _error("invalid_response", "MCP 工具清单无效")
                name = entry["name"]
                if name:
                    schema = entry.get("inputSchema")
                    if not isinstance(schema, dict):
                        raise _error("invalid_schema", "MCP 工具缺少参数 schema")
                    pending = [schema]
                    while pending:
                        node = pending.pop()
                        if isinstance(node, dict):
                            if any(k in node and (not isinstance(node[k], str) or not node[k].startswith("#"))
                                   for k in ("$ref", "$dynamicRef")):
                                raise _error("invalid_schema", "MCP schema 不允许外部引用")
                            pending.extend(node.values())
                        elif isinstance(node, list):
                            pending.extend(node)
                    try:
                        jsonschema.Draft202012Validator.check_schema(schema)
                    except jsonschema.SchemaError:
                        raise _error("invalid_schema", "MCP 参数 schema 无效") from None
                    tools[name] = {"name": name, "inputSchema": schema}
            cursor = result.get("nextCursor")
            if cursor is None:
                self._tools = tools
                return tools
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen:
                raise _error("pagination_limit", "MCP 工具分页游标无效")
            seen.add(cursor)
        raise _error("pagination_limit", "MCP 工具分页超过上限")

    def safe_discover(self) -> list[dict]:
        """Read remote tool schemas. Discovery grants no tool execution permission."""
        with self._lock:
            return self._read_with_reinitialize(lambda: list(self._discover().values()))

    def list_tools(self) -> list[dict]:
        return self.safe_discover()

    def _read_with_reinitialize(self, operation):
        for attempt in range(2):
            try:
                return operation()
            except FireplayMCPError as exc:
                if exc.code != "invalid_session" or attempt:
                    raise
                self._reset()
        raise AssertionError("unreachable")

    def _call(self, name: str, action: str, payload: dict) -> dict:
        campaign_read = self.config.service == "campaigns" and action in ALLOWED_ACTIONS.get(name, ())
        requirement_read = self.config.service == "ai_designer" and name == "requirement_list" and action == "read"
        library_read = self.config.service == "ai_designer" and name in {"material_list", "material_detail"} and action == "read"
        if not (campaign_read or requirement_read or library_read):
            raise _error("operation_not_allowed", "仅支持已审核的 Fireplay 只读查询")
        self._require_enabled()
        if not self.config._token():
            raise _error("credentials_missing", "尚未配置 Fireplay Bearer 凭据环境变量")
        arguments = payload if (requirement_read or library_read) else {"action": action, "payload": payload}
        try:
            if len(json.dumps(arguments).encode()) > 16384:
                raise ValueError()
        except (TypeError, ValueError, RecursionError):
            raise _error("invalid_request", "Fireplay 查询参数超出限制") from None

        def execute():
            tool = self._discover().get(name)
            if tool is None:
                raise _error("required_tool_missing", "Fireplay 服务未提供所需只读工具")
            try:
                jsonschema.Draft202012Validator(tool["inputSchema"]).validate(arguments)
            except (jsonschema.ValidationError, Unresolvable, RecursionError):
                raise _error("schema_mismatch", "Fireplay 当前工具参数与客户端契约不一致") from None
            result = self._post("tools/call", {"name": name, "arguments": arguments})
            return self._business_result(result)

        with self._lock:
            return self._read_with_reinitialize(execute)

    @staticmethod
    def _business_result(result: dict) -> dict:
        value = result.get("structuredContent")
        if value is None:
            blocks = result.get("content", [])
            if not isinstance(blocks, list):
                raise _error("invalid_response", "Fireplay 返回无效业务结果")
            texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            if len(texts) != 1 or not isinstance(texts[0], str):
                raise _error("tool_error" if result.get("isError") else "invalid_response", "Fireplay 未返回结构化业务结果")
            try:
                value = json.loads(texts[0])
            except ValueError:
                raise _error("tool_error" if result.get("isError") else "invalid_response", "Fireplay 未返回结构化业务结果") from None
        if not isinstance(value, dict):
            raise _error("invalid_response", "Fireplay 返回无效业务结果")
        if result.get("isError") or value.get("status") == "error" or value.get("success") is False:
            text = str(value.get("message", "")).lower()
            if any(x in text for x in ("身份", "authorization", "bearer", "unauthorized", "forbidden", "权限")):
                raise _error("unauthorized", "Fireplay 身份无效或无查询权限")
            raise _error("business_error", "Fireplay 拒绝了该只读查询")
        return value

    def library_original_lookup(self, youtube_id: str) -> dict:
        """Fixed read-only catalog route: look up existing originals, never ingest video."""
        if self.config.service != "ai_designer" or not isinstance(youtube_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_id):
            raise _error("invalid_request", "原视频查询需要规范的 YouTube 视频 ID")
        self._require_enabled()
        if not self.config._token():
            raise _error("credentials_missing", "尚未配置 Fireplay 登录身份")
        name = "material_youtube_original_lookup"
        arguments = {"youtube_urls": ["https://www.youtube.com/watch?v=" + youtube_id]}

        def execute():
            tools = self._discover()

            def invoke(tool_name, payload):
                tool = tools.get(tool_name)
                if tool is None:
                    raise _error("required_tool_missing", "Fireplay 未提供素材库原视频查询接口")
                try:
                    jsonschema.Draft202012Validator(tool["inputSchema"]).validate(payload)
                except (jsonschema.ValidationError, Unresolvable, RecursionError):
                    raise _error("schema_mismatch", "素材库查询接口合同已变化") from None
                return self._business_result(self._post("tools/call", {"name": tool_name, "arguments": payload}))

            definition = invoke("tool_catalog", {"tool": name})
            if definition.get("name") != name or definition.get("operation_class") != "read" or definition.get("invoke_via") != "tool_read":
                raise _error("operation_not_allowed", "原视频工具不符合已审核的只读合同")
            version, schema = definition.get("schema_version"), definition.get("inputSchema")
            if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version) or not isinstance(schema, dict):
                raise _error("invalid_schema", "原视频查询未返回有效版本或参数合同")
            try:
                pending = [schema]
                while pending:
                    node = pending.pop()
                    if isinstance(node, dict):
                        if any(k in node and (not isinstance(node[k], str) or not node[k].startswith("#"))
                               for k in ("$ref", "$dynamicRef")):
                            raise _error("invalid_schema", "MCP schema 不允许外部引用")
                        pending.extend(node.values())
                    elif isinstance(node, list):
                        pending.extend(node)
                jsonschema.Draft202012Validator.check_schema(schema)
                jsonschema.Draft202012Validator(schema).validate(arguments)
            except (jsonschema.SchemaError, jsonschema.ValidationError, Unresolvable, RecursionError):
                raise _error("schema_mismatch", "原视频查询参数不符合当前合同") from None
            return invoke("tool_read", {"tool": name, "schema_version": version, "arguments": arguments})

        with self._lock:
            return self._read_with_reinitialize(execute)

    def library_read(self, name: str, arguments: dict) -> dict:
        """Read only the two reviewed material-library tools with live schema validation."""
        if name not in {"material_list", "material_detail"} or not isinstance(arguments, dict):
            raise _error("operation_not_allowed", "仅支持素材库只读检索和详情")
        return self._call(name, "read", arguments)

    def requirement_delivery_options(self) -> dict:
        """Read two fixed option tools using the authenticated primary department.

        Catalog discovery is not execution permission: every definition must still
        identify the expected read-only route. No caller can choose another tool or
        department, and no default options are initialized by this operation.
        """
        if self.config.service != "ai_designer":
            raise _error("operation_not_allowed", "交付选项需要AI Designer服务")
        self._require_enabled()
        if not self.config._token():
            raise _error("credentials_missing", "尚未配置Fireplay登录身份")

        def execute():
            tools = self._discover()

            def invoke(tool_name, arguments):
                tool = tools.get(tool_name)
                if tool is None:
                    raise _error("required_tool_missing", "Fireplay未提供交付选项只读接口")
                try:
                    jsonschema.Draft202012Validator(tool["inputSchema"]).validate(arguments)
                except (jsonschema.ValidationError, Unresolvable, RecursionError):
                    raise _error("schema_mismatch", "Fireplay交付选项接口合同已变化") from None
                return self._business_result(self._post("tools/call", {
                    "name": tool_name, "arguments": arguments}))

            def read_option_tool(name, arguments):
                definition = invoke("tool_catalog", {"tool": name})
                if (definition.get("name") != name or definition.get("operation_class") != "read"
                        or definition.get("invoke_via") != "tool_read"):
                    raise _error("operation_not_allowed", "交付选项工具不符合已审核只读合同")
                version, schema = definition.get("schema_version"), definition.get("inputSchema")
                if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
                    raise _error("invalid_schema", "交付选项未返回有效合同版本")
                if not isinstance(schema, dict):
                    raise _error("invalid_schema", "交付选项未返回有效参数合同")
                pending = [schema]
                while pending:
                    node = pending.pop()
                    if isinstance(node, dict):
                        if any(key in node and (not isinstance(node[key], str)
                               or not node[key].startswith("#")) for key in ("$ref", "$dynamicRef")):
                            raise _error("invalid_schema", "MCP schema不允许外部引用")
                        pending.extend(node.values())
                    elif isinstance(node, list):
                        pending.extend(node)
                try:
                    jsonschema.Draft202012Validator.check_schema(schema)
                    jsonschema.Draft202012Validator(schema).validate(arguments)
                except (jsonschema.SchemaError, jsonschema.ValidationError, Unresolvable, RecursionError):
                    raise _error("schema_mismatch", "交付选项参数不符合当前合同") from None
                return invoke("tool_read", {"tool": name, "schema_version": version, "arguments": arguments})

            fields = read_option_tool("requirement_field_options_query", {"include_members": False})
            department = fields.get("deptId")
            if not isinstance(department, str) or not department.strip() or len(department) > 200:
                raise _error("invalid_response", "Fireplay未返回当前身份的需求部门")
            return read_option_tool("requirement_create_options", {"dept_id": department})

        with self._lock:
            return self._read_with_reinitialize(execute)

    def requirement_list(self, arguments: dict) -> dict:
        """Use the live discovered requirement_list schema; never invent a payload envelope.

        The caller supplies reviewed arguments matching that schema. No write tools,
        automatic paging, or requirement response normalization occurs here.
        """
        if not isinstance(arguments, dict):
            raise _error("invalid_request", "需求查询参数必须为对象")
        try:
            if len(json.dumps(arguments).encode()) > 16384:
                raise ValueError()
        except (TypeError, ValueError, RecursionError):
            raise _error("invalid_request", "需求查询参数超出限制") from None
        pending = [arguments]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"page", "page_size", "limit"}:
                        _bounded_int(value, key)
                    if key == "offset":
                        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
                            raise _error("invalid_request", "需求分页 offset 超过上限")
                    if key == "view" and value not in {"summary", "blind_brief", "delivery"}:
                        raise _error("invalid_request", "需求查询 view 不在已审核范围")
                pending.extend(node.values())
            elif isinstance(node, list):
                if len(node) > 100:
                    raise _error("invalid_request", "需求查询列表参数超过上限")
                pending.extend(node)
        return self._call("requirement_list", "read", arguments)

    def list_tasks(self, scope: str = "personal", page: int = 1, page_size: int = 20) -> dict:
        scopes = {"personal": 1, "team": 3}
        if scope not in scopes:
            raise _error("invalid_scope", "仅支持 personal 或 team 查询范围")
        return self._call(TASK_TOOL, "list", {"search_type": scopes[scope],
                          "page": _bounded_int(page, "page"), "page_size": _bounded_int(page_size, "page_size")})

    def get_task(self, task_uuid: str) -> dict:
        return self._call(TASK_TOOL, "get", {"task_uuid": self._task_id(task_uuid)})

    def get_task_details(self, task_uuid: str, page: int = 1, page_size: int = 20) -> dict:
        return self._call(TASK_TOOL, "details", {"task_uuid": self._task_id(task_uuid),
                          "page": _bounded_int(page, "page"), "offset": _bounded_int(page_size, "page_size")})

    def user_info(self) -> dict:
        return self._call(REFERENCE_TOOL, "user_info", {})

    @staticmethod
    def _task_id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise _error("invalid_request", "任务标识格式无效")
        return value
