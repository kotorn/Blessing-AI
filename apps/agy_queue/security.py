"""Redaction and prompt safety checks for queue-owned data."""

from __future__ import annotations

import re
from typing import Any


class CredentialLikeContentError(ValueError):
    """Raised when a prompt appears to contain a credential or private key."""


_LABELED_SECRET = re.compile(
    r"(?ix)(?:api[_-]?(?:key|secret)|password|token|authorization|private[_-]?key|"
    r"client[_-]?secret|service[_-]?account)\s*[:=]\s*(['\"]?)([^\s,'\"}]+)\1"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")
_BINANCE_KEY_SHAPE = re.compile(r"\b[A-Za-z0-9]{32,128}\b")
_PLACEHOLDERS = {"", "<redacted>", "<secret>", "***", "changeme", "example"}


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDERS


def contains_credential_like_content(value: str) -> bool:
    if _PRIVATE_KEY.search(value) or _BEARER.search(value):
        return True
    for match in _LABELED_SECRET.finditer(value):
        if not _is_placeholder(match.group(2)):
            return True
    return False


def validate_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("AGY prompt must be a non-empty string")
    if len(prompt) > 200_000:
        raise ValueError("AGY prompt exceeds the 200 KB limit")
    if contains_credential_like_content(prompt):
        raise CredentialLikeContentError(
            "Prompt contains credential-like content; use a Secret Manager reference instead"
        )
    return prompt


def redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = _PRIVATE_KEY.sub("<REDACTED_PRIVATE_KEY>", value)
    redacted = _BEARER.sub("Bearer <REDACTED_TOKEN>", redacted)

    def replace_secret(match: re.Match[str]) -> str:
        label = match.group(0).split(match.group(2), 1)[0]
        return f"{label}<REDACTED>"

    redacted = _LABELED_SECRET.sub(replace_secret, redacted)
    return redacted


def redact_json(value: Any) -> Any:
    """Redact strings recursively without persisting environment dumps."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, tuple):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(
                # token(?!s) excludes usage-count fields like output_tokens/
                # total_tokens (plural, not credential-shaped) while still
                # catching access_token/auth_token/bare "token" keys.
                r"(?i)(api[_-]?(?:key|secret)|password|token(?!s)|authorization|private[_-]?key|"
                r"client[_-]?secret)",
                key_text,
            ):
                result[key_text] = "<REDACTED>"
            else:
                result[key_text] = redact_json(item)
        return result
    return value
