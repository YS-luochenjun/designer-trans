"""Allowlisted provider diagnostics; never retain error bodies or arbitrary strings."""

import re


ERROR_CODES = {
    "invalid_api_key", "authentication_error", "permission_denied", "rate_limit_exceeded",
    "insufficient_quota", "context_length_exceeded", "unsupported_parameter",
    "invalid_request_error", "invalid_json_schema", "model_not_found", "request_too_large",
    "server_error", "invalid_image", "invalid_image_format", "image_too_large",
}
ERROR_TYPES = ERROR_CODES | {"api_error", "requests", "tokens"}


def safe_failure_evidence(value):
    result = {}
    for key, allowed in (("provider_error_code", ERROR_CODES), ("provider_error_type", ERROR_TYPES)):
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    status = value.get("provider_http_status")
    if type(status) is int and 400 <= status <= 599:
        result["provider_http_status"] = status
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
    return result
