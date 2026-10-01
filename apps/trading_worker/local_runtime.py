"""Local runtime identity helpers shared by Worker subsystems."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path


def local_container_runtime(environ: Mapping[str, str] | None = None) -> bool:
    """Recognize only an explicitly opted-in Linux container, never host env alone."""

    values = environ if environ is not None else os.environ
    enabled = str(values.get("LOCAL_WORKER_CONTAINER", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return enabled and Path("/.dockerenv").is_file()


def local_postgres_host(environ: Mapping[str, str] | None = None) -> str:
    """Use Docker Desktop's host route only from the explicitly marked container."""

    return "host.docker.internal" if local_container_runtime(environ) else "127.0.0.1"


def mainnet_secret_value(
    name: str,
    environ: Mapping[str, str] | None = None,
    secret_directory: Path = Path("/run/secrets"),
) -> str:
    """Read a direct Worker env secret or an approved tmpfs file in Docker."""

    if name not in {"BINANCE_MAINNET_API_KEY", "BINANCE_MAINNET_API_SECRET"}:
        raise ValueError("unsupported Mainnet secret name")
    values = environ if environ is not None else os.environ
    direct = str(values.get(name, ""))
    if direct.strip():
        return direct
    suffix = "api_key" if name.endswith("_KEY") else "api_secret"
    return local_container_secret_file(f"{name}_FILE", f"binance_mainnet_{suffix}", values, secret_directory)


def local_container_secret_file(
    variable: str,
    filename: str,
    environ: Mapping[str, str] | None = None,
    secret_directory: Path = Path("/run/secrets"),
) -> str:
    """Read one allowlisted file from the container's private secret tmpfs."""

    allowed = {
        "BINANCE_MAINNET_API_KEY_FILE": "binance_mainnet_api_key",
        "BINANCE_MAINNET_API_SECRET_FILE": "binance_mainnet_api_secret",
        "WORKER_IDENTITY_TOKEN_FILE": "worker_identity_token",
        "POSTGRES_PASSWORD_FILE": "postgres_password",
    }
    if allowed.get(variable) != filename or not local_container_runtime(environ):
        return ""
    values = environ if environ is not None else os.environ
    expected = secret_directory / filename
    if str(values.get(variable, "")) != str(expected):
        return ""
    try:
        info = expected.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            return ""
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return ""
        raw = expected.read_bytes()
        if not raw or len(raw) > 4_096 or b"\x00" in raw or b"\r" in raw or b"\n" in raw:
            return ""
        value = raw.decode("utf-8")
        return value if value.strip() else ""
    except (OSError, UnicodeDecodeError):
        return ""


def worker_identity_token_value(environ: Mapping[str, str] | None = None) -> str:
    values = environ if environ is not None else os.environ
    return str(values.get("WORKER_IDENTITY_TOKEN", "")).strip() or local_container_secret_file(
        "WORKER_IDENTITY_TOKEN_FILE", "worker_identity_token", values
    )


def postgres_password_value(environ: Mapping[str, str] | None = None) -> str:
    values = environ if environ is not None else os.environ
    return str(values.get("POSTGRES_PASSWORD", "")) or local_container_secret_file(
        "POSTGRES_PASSWORD_FILE", "postgres_password", values
    )


def clear_local_container_secrets(
    environ: Mapping[str, str] | None = None,
    secret_directory: Path = Path("/run/secrets"),
) -> None:
    """Remove all exact runtime secret files during Worker shutdown."""

    values = environ if environ is not None else os.environ
    if not local_container_runtime(values):
        return
    clear_local_mainnet_secrets(values, secret_directory)
    for filename in ("worker_identity_token", "postgres_password"):
        target = secret_directory / filename
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


def clear_local_mainnet_secrets(
    environ: Mapping[str, str] | None = None,
    secret_directory: Path = Path("/run/secrets"),
) -> None:
    """Remove exchange credentials after disarm but retain DB/control identity."""

    values = environ if environ is not None else os.environ
    if not local_container_runtime(values):
        return
    for filename in (
        "binance_mainnet_api_key",
        "binance_mainnet_api_secret",
    ):
        target = secret_directory / filename
        try:
            target.unlink(missing_ok=True)
        except OSError:
            # Never replace shutdown handling with a secret-cleanup exception.
            pass
