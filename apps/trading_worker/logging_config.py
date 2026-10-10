"""Structured rotating file logging with secret redaction for the trading worker."""

from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional, Pattern

SECRET_PATTERNS: tuple[Pattern[str], ...] = (
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;&\"']+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?secret\s*[=:]\s*)[^\s,;&\"']+", re.IGNORECASE),
    re.compile(r"(?i)(signature\s*[=:]\s*)[a-f0-9]{64}", re.IGNORECASE),
    re.compile(r"(?i)(X-MBX-APIKEY\s*[=:]\s*)[^\s,;&\"']+", re.IGNORECASE),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9_.-]+", re.IGNORECASE),
    re.compile(r"(?i)(password\s*[=:]\s*)[^\s,;&\"']+", re.IGNORECASE),
    re.compile(r"(?i)(POSTGRES_PASSWORD\s*[=:]\s*)[^\s,;&\"']+", re.IGNORECASE),
    re.compile(r"(?i)(postgres(?:ql)?://)[^\s/@:]+(?::[^\s/@]*)?@", re.IGNORECASE),
)


def redact_secrets(text: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """Redact known secrets, passwords, signatures, and credential patterns from text."""
    redacted = str(text)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1<redacted>", redacted)

    env = environ if environ is not None else os.environ
    for key, value in env.items():
        if not value or len(value) < 8:
            continue
        key_upper = key.upper()
        if any(
            token in key_upper
            for token in ("KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL")
        ):
            if value in redacted:
                redacted = redacted.replace(value, "<redacted>")
    return redacted


class SecretRedactingFormatter(logging.Formatter):
    """Logging formatter that strips credentials and secrets from formatted messages."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact_secrets(formatted)


def configure_rotating_file_logger(
    log_file_path: Optional[str | Path] = None,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    logger_name: Optional[str] = None,
    level: int = logging.INFO,
) -> Optional[RotatingFileHandler]:
    """Attach a rotating file handler with secret redaction to the specified logger.

    Defaults to 'logs/trading_worker.log' (gitignored).
    """
    if log_file_path is None:
        log_file_path = os.getenv("TRADING_WORKER_LOG_FILE", "logs/trading_worker.log")

    path = Path(log_file_path).resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    handler = RotatingFileHandler(
        str(path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    formatter = SecretRedactingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    handler.setFormatter(formatter)

    target_logger = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    for existing in target_logger.handlers:
        if isinstance(existing, RotatingFileHandler) and getattr(existing, "baseFilename", None) == str(path):
            return existing
    target_logger.addHandler(handler)
    return handler
