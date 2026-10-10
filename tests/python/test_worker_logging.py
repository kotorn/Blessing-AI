"""Tests for structured rotating file logging and secret redaction."""

import logging
from pathlib import Path
import pytest

from apps.trading_worker.logging_config import (
    SECRET_PATTERNS,
    SecretRedactingFormatter,
    configure_rotating_file_logger,
    redact_secrets,
)


def test_redact_secrets_redacts_credential_patterns():
    raw_text = (
        "Connected with api_key=ab12cd34ef56gh78 and api_secret=secret123456789. "
        "Signature: signature=1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef. "
        "Header: X-MBX-APIKEY=my_mbx_key_12345678. "
        "Auth: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9. "
        "DB: postgresql://postgres:supersecretpassword@127.0.0.1:5432/blessing_trading and "
        "password=dbpassword123."
    )
    redacted = redact_secrets(raw_text)

    assert "ab12cd34ef56gh78" not in redacted
    assert "secret123456789" not in redacted
    assert "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef" not in redacted
    assert "my_mbx_key_12345678" not in redacted
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
    assert "supersecretpassword" not in redacted
    assert "dbpassword123" not in redacted
    assert "<redacted>" in redacted


def test_redact_secrets_scrubs_environment_secrets():
    environ = {
        "BINANCE_API_KEY": "super_secret_binance_key_999",
        "BINANCE_API_SECRET": "another_top_secret_binance_secret",
        "POSTGRES_PASSWORD": "custom_pg_password_888",
    }
    raw = (
        "Worker starting with custom_pg_password_888 on host and key "
        "super_secret_binance_key_999 and another_top_secret_binance_secret."
    )
    redacted = redact_secrets(raw, environ=environ)
    assert "super_secret_binance_key_999" not in redacted
    assert "another_top_secret_binance_secret" not in redacted
    assert "custom_pg_password_888" not in redacted
    assert "<redacted>" in redacted


def test_secret_redacting_formatter():
    formatter = SecretRedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Authentication failed for api_key=leaked_key_value_12345",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert "leaked_key_value_12345" not in formatted
    assert "api_key=<redacted>" in formatted


def test_rotating_file_handler_rotation_and_backup(tmp_path: Path):
    log_file = tmp_path / "test_worker.log"
    handler = configure_rotating_file_logger(
        log_file_path=log_file,
        max_bytes=300,
        backup_count=2,
        logger_name="test_rotation",
    )
    assert handler is not None
    test_logger = logging.getLogger("test_rotation")
    test_logger.setLevel(logging.INFO)

    try:
        # Write enough lines to exceed max_bytes and trigger rotation
        for i in range(25):
            test_logger.info(f"Log message number {i:03d} to trigger file rotation")

        handler.flush()
        handler.close()

        # Verify log file and rotated backups exist
        assert log_file.exists()
        rotated_1 = tmp_path / "test_worker.log.1"
        assert rotated_1.exists()

        content = log_file.read_text(encoding="utf-8")
        assert len(content) > 0
    finally:
        test_logger.removeHandler(handler)


def test_configure_rotating_file_logger_is_idempotent(tmp_path: Path):
    log_file = tmp_path / "idempotent.log"
    logger_name = "test_idempotent"
    test_logger = logging.getLogger(logger_name)

    handler1 = configure_rotating_file_logger(
        log_file_path=log_file, logger_name=logger_name
    )
    handler2 = configure_rotating_file_logger(
        log_file_path=log_file, logger_name=logger_name
    )
    try:
        assert handler1 is handler2
        matching = [
            h for h in test_logger.handlers
            if getattr(h, "baseFilename", None) == str(log_file.resolve())
        ]
        assert len(matching) == 1
    finally:
        if handler1:
            test_logger.removeHandler(handler1)
            handler1.close()
