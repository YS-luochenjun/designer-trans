"""Allowlisted provider diagnostics; never retain error bodies or arbitrary strings."""

import re


ERROR_CODES = {
    "invalid_api_key", "authentication_error", "permission_denied", "rate_limit_exceeded",
    "insufficient_quota", "context_length_exceeded", "unsupported_parameter",
    "invalid_request_error", "invalid_json_schema", "model_not_found", "request_too_large",
    "server_error", "invalid_image", "invalid_image_format", "image_too_large",
}
ERROR_TYPES = ERROR_CODES | {"api_error", "requests", "tokens"}
FAILURE_REASONS = {
    "context_length", "image_count", "image_format", "schema_contract", "payload_size",
    "unsupported_parameter",
}
EVENT_INVALID_REASONS = {
    "unknown", "malformed_event", "duplicate_thread", "invalid_turn_start",
    "item_outside_turn", "invalid_item", "unsupported_item_event", "invalid_turn_end",
    "unexpected_failure", "unsupported_event", "oversized_event",
    "event_reader_error", "event_drain_timeout",
}
EVENT_TYPES = {
    "thread.started", "turn.started", "turn.completed", "turn.failed", "error",
    "item.started", "item.updated", "item.completed", "other",
}


def safe_failure_evidence(value):
    result = {}
    for key, allowed in (("provider_error_code", ERROR_CODES), ("provider_error_type", ERROR_TYPES)):
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    status = value.get("provider_http_status")
    if type(status) is int and 400 <= status <= 599:
        result["provider_http_status"] = status
    if (isinstance(value.get("provider_failure_reason"), str)
            and value["provider_failure_reason"] in FAILURE_REASONS):
        result["provider_failure_reason"] = value["provider_failure_reason"]
    for key, allowed in (("event_invalid_reason", EVENT_INVALID_REASONS),
                         ("event_invalid_type", EVENT_TYPES)):
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    index = value.get("event_invalid_index")
    if type(index) is int and 0 <= index <= 1_000_000:
        result["event_invalid_index"] = index
    return result


def extract_failure_evidence(text):
    result = {}
    for field, key, allowed in (("code", "provider_error_code", ERROR_CODES),
                                ("type", "provider_error_type", ERROR_TYPES)):
        match = next((v for v in re.findall(
            rf"""["']{field}["']\s*:\s*["']([a-z_]+)["']""", text) if v in allowed), None)
        if match:
            result[key] = match
    status = re.search(r"(?:status(?:\s+code)?[:\s]+|HTTP[/\d.]*\s+)([45]\d\d)\b", text, re.I)
    if status:
        result["provider_http_status"] = int(status[1])
    lower = text.lower()
    # Fixed diagnostic categories only; neither provider prose nor URLs leave here.
    for reason, patterns in (
        ("context_length", ("context_length_exceeded", "maximum context length", "context window")),
        ("image_count", ("too many images", "maximum number of images", "image count limit")),
        ("image_format", ("invalid image", "unsupported image format", "invalid_image")),
        ("schema_contract", ("invalid_json_schema", "invalid schema", "unsupported schema")),
        ("payload_size", ("request too large", "payload too large", "request_too_large")),
        ("unsupported_parameter", ("unsupported_parameter", "unsupported parameter")),
    ):
        if any(pattern in lower for pattern in patterns):
            result["provider_failure_reason"] = reason
            break
    return result
