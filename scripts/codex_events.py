"""Bounded, private event evidence for one Codex exec, never a replay policy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from harness_bridge import BridgeError, MAX_OUTPUT, atomic_json, load_json, validate_answer
from video_translation.integrations.agents.failure_evidence import extract_failure_evidence

MAX_EVENT = MAX_OUTPUT * 2 + 65536
FAILURE_KINDS = {
    "authentication", "rate_limit", "provider_unavailable", "context_limit",
    "request_rejected", "transport", "cli_local", "unknown",
}
TURN_STATES = {"not_started", "started", "completed", "failed", "invalid"}
ITEM_KINDS = {
    "agent_message", "reasoning", "command_execution", "file_change", "mcp_tool_call",
    "web_search", "todo_list", "plan_update", "image_generation",
}
FAILURE_CODES = {
    "invalid_api_key": "authentication",
    "authentication_error": "authentication",
    "permission_denied": "authentication",
    "rate_limit_exceeded": "rate_limit",
    "insufficient_quota": "rate_limit",
    "context_length_exceeded": "context_limit",
    "unsupported_parameter": "request_rejected",
    "invalid_request_error": "request_rejected",
    "invalid_json_schema": "request_rejected",
    "model_not_found": "request_rejected",
    "request_too_large": "request_rejected",
    "server_error": "provider_unavailable",
}


def failure_kind(text: str) -> str:
    """Return fixed categories only; never retain provider text, URLs or credentials."""
    # A specific code can explain a generic HTTP 400 or invalid_request_error type.
    for field in ("code", "type"):
        for code in re.findall(rf"""["']{field}["']\s*:\s*["']([a-z_]+)["']""", text):
            if code in FAILURE_CODES:
                return FAILURE_CODES[code]
    status = re.search(r"(?:status(?:\s+code)?[:\s]+|HTTP[/\d.]*\s+)([45]\d\d)\b", text, re.I)
    if status:
        code = int(status[1])
        if code in {401, 403}:
            return "authentication"
        if code == 429:
            return "rate_limit"
        if code >= 500:
            return "provider_unavailable"
        if code in {400, 404, 413, 422}:
            return "request_rejected"
    lower = text.lower()
    if any(x in lower for x in ("context_length_exceeded", "context window", "maximum context")):
        return "context_limit"
    if any(x in lower for x in (
        "stream disconnected", "connection reset", "connection closed", "connection refused",
        "error sending request", "failed to connect", "timed out", "tls", "dns",
    )):
        return "transport"
    if any(x in lower for x in ("error loading config.toml:", "no space left", "os error", "permission denied")):
        return "cli_local"
    return "unknown"


class EventRecorder:
    """Only the last agent item of one fully completed turn can become a result."""

    def __init__(self, root: Path, schema: dict, request_sha256: str):
        self.root, self.schema, self.request_sha256 = root, schema, request_sha256
        self.state = "not_started"
        self.thread_seen = False
        self.turn_seen = False
        self.count = 0
        self.last_item = None
        self.pending = set()
        self.answer = None
        self.cause = "unknown"
        self.malformed = False
        self.thread_id = None
        self.failure_fingerprint = None
        self.provider_details = {}

    def summary(self):
        return {
            "event_turn_state": self.state, "event_count": self.count,
            "failure_kind": self.cause, "event_stream_invalid": self.malformed,
            **({"cli_thread_id": self.thread_id} if self.thread_id else {}),
            **({"failure_message_sha256": self.failure_fingerprint} if self.failure_fingerprint else {}),
            **self.provider_details,
        }

    def observe_failure(self, message, *, fallback=False):
        cause = failure_kind(message)
        if cause != "unknown" and (not fallback or self.cause == "unknown"):
            self.cause = cause
        details = extract_failure_evidence(message)
        if fallback:
            for key, value in details.items():
                self.provider_details.setdefault(key, value)
        else:
            self.provider_details.update(details)
            self.failure_fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()

    def save(self):
        atomic_json(self.root / "event-summary.json", {
            "request_sha256": self.request_sha256, **self.summary(),
        })

    def invalidate(self):
        self.state, self.malformed, self.answer = "invalid", True, None
        (self.root / "event-result.json").unlink(missing_ok=True)

    def consume(self, raw: bytes):
        previous = self.summary()
        self.count = min(self.count + 1, 1_000_000)
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            self.invalidate()
            self.save()
            return
        kind = event["type"]
        if kind == "thread.started":
            if self.thread_seen or self.state != "not_started":
                self.invalidate()
            self.thread_seen = True
            identifier = event.get("thread_id")
            if isinstance(identifier, str) and re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", identifier):
                self.thread_id = identifier
        elif kind == "turn.started":
            if not self.thread_seen or self.turn_seen:
                self.invalidate()
            elif self.state != "invalid":
                self.state = "started"
            self.turn_seen = True
        elif kind.startswith("item."):
            item = event.get("item")
            # 0.166.0 emits setup diagnostics (e.g. unknown model metadata) before turn.started.
            if (kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error"
                    and self.thread_seen and self.state in {"not_started", "started"}):
                self.answer, self.last_item = None, "error"
                if isinstance(item.get("message"), str):
                    self.observe_failure(item["message"])
            elif self.state != "started" or not isinstance(item, dict):
                self.invalidate()
            else:
                self.last_item = item.get("type") if item.get("type") in ITEM_KINDS else "other"
                identifier = item.get("id")
                if not isinstance(identifier, str) or len(identifier) > 128 or len(self.pending) >= 256:
                    self.invalidate()
                elif kind in {"item.started", "item.updated"}:
                    self.pending.add(identifier)
                    self.answer = None
                elif kind == "item.completed":
                    self.pending.discard(identifier)
                    text = item.get("text")
                    self.answer = (text if self.last_item == "agent_message"
                                   and item.get("phase") in {None, "final_answer"}
                                   and isinstance(text, str)
                                   and len(text.encode("utf-8")) <= MAX_OUTPUT else None)
                else:
                    self.invalidate()
        elif kind == "turn.completed":
            if self.state != "started" or self.pending:
                self.invalidate()
            else:
                self.state = "completed"
                if self.answer is not None and self.last_item == "agent_message":
                    try:
                        # Strict schema here: formatting repair still uses its existing separate path.
                        value = validate_answer(self.answer, self.schema)
                        atomic_json(self.root / "event-result.json", {
                            "request_sha256": self.request_sha256, "answer": value,
                            "answer_sha256": hashlib.sha256(
                                json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                            "event_turn_state": "completed", "result_source": "completed_event",
                        })
                    except (BridgeError, ValueError):
                        # An invalid model answer is evidence, never a recovered success.
                        pass
        elif kind in {"turn.failed", "error"}:
            message = event.get("message")
            if kind == "turn.failed":
                error = event.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                if self.state == "started":
                    self.state = "failed"
                else:
                    self.invalidate()
                self.answer = None
                (self.root / "event-result.json").unlink(missing_ok=True)
            if isinstance(message, str):
                self.observe_failure(message)
        else:
            # A changed event contract cannot certify an answer.
            self.invalidate()
        if previous != self.summary():
            self.save()


def read_events(pipe, recorder: EventRecorder):
    while raw := pipe.readline(MAX_EVENT + 1):
        if len(raw) > MAX_EVENT:
            while raw and not raw.endswith(b"\n"):
                raw = pipe.readline(65536)
            recorder.invalidate()
            recorder.save()
        else:
            recorder.consume(raw)


def recovered_event(root: Path, request: dict):
    """Recheck identity and schema even for a previously saved completed event."""
    path, summary_path = root / "event-result.json", root / "event-summary.json"
    if (path.is_symlink() or not path.is_file() or summary_path.is_symlink()
            or not summary_path.is_file()):
        return None
    try:
        summary = load_json(summary_path, 4096)
        value = load_json(path, MAX_OUTPUT + 4096)
        if (summary.get("request_sha256") != request["sha256"]
                or summary.get("event_turn_state") != "completed"
                or summary.get("event_stream_invalid") is not False
                or value.get("request_sha256") != request["sha256"]
                or value.get("event_turn_state") != "completed"
                or value.get("result_source") != "completed_event"):
            return None
        encoded = json.dumps(value["answer"], sort_keys=True, ensure_ascii=False)
        if hashlib.sha256(encoded.encode()).hexdigest() != value.get("answer_sha256"):
            return None
        return validate_answer(encoded, request["schema"])
    except (BridgeError, ValueError, OSError, KeyError, TypeError):
        return None
