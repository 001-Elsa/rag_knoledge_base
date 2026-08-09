"""Sensitive-value redaction for logs and audit metadata."""

import re

_PATTERNS = (
    (re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"), lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:]),
    (re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3}[0-9Xx])(?!\d)"), lambda m: m.group(1) + "********" + m.group(3)),
    (re.compile(r"\b([A-Z0-9._%+-]{1,64})@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.I), lambda m: m.group(1)[:2] + "***@" + m.group(2)),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*[^\s,;]+"), lambda m: m.group(1) + "=***"),
)


def redact_text(value: str) -> str:
    result = value
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: "***" if any(term in str(key).casefold() for term in ("password", "secret", "token", "api_key")) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value
