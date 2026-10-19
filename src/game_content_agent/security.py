"""Minimize secrets in persisted records and user-visible errors."""

import re
from typing import Any

SECRET_FIELDS = re.compile(
    r"^(authorization|api[_-]?key|openai_api_key|password|secret|access_token)$",
    re.IGNORECASE,
)
KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE)


def sanitize(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]"
            if SECRET_FIELDS.match(str(k))
            else sanitize(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, str):
        text = value
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return BEARER_PATTERN.sub(
            "Bearer [REDACTED]", KEY_PATTERN.sub("[REDACTED]", text)
        )
    return value
