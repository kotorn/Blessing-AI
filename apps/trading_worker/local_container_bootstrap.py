"""Receive Mainnet keys over container stdin and keep them only in tmpfs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

SECRET_DIRECTORY = Path("/run/secrets")
MAX_PAYLOAD_BYTES = 8_192


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    """Never delete a pre-existing file or a replacement owned by another run."""
    try:
        observed = path.lstat()
        if (observed.st_dev, observed.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def _write_secret(path: Path, value: str) -> tuple[int, int]:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    observed = os.fstat(descriptor)
    identity = (observed.st_dev, observed.st_ino)
    try:
        raw = value.encode("utf-8")
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("secret write made no progress")
            offset += written
    except Exception:
        os.close(descriptor)
        descriptor = -1
        _unlink_owned(path, identity)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return identity


def stage_secrets(payload: bytes, directory: Path = SECRET_DIRECTORY) -> dict[str, str]:
    """Validate one bounded secret envelope and create mode-0600 files."""

    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("secret payload is too large")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("secret payload is invalid") from exc
    required_fields = {"apiKey", "apiSecret", "workerIdentityToken", "postgresPassword"}
    if not isinstance(value, dict) or set(value) != required_fields:
        raise ValueError("secret payload schema is invalid")
    key = value["apiKey"]
    secret = value["apiSecret"]
    worker_token = value["workerIdentityToken"]
    postgres_password = value["postgresPassword"]
    for item, required in (
        (key, False),
        (secret, False),
        (worker_token, True),
        (postgres_password, True),
    ):
        if (
            not isinstance(item, str)
            or (required and not item.strip())
            or len(item.encode("utf-8")) > 4_096
            or any(character in item for character in "\x00\r\n")
        ):
            raise ValueError("secret payload value is invalid")
    if bool(key.strip()) != bool(secret.strip()):
        raise ValueError("Mainnet credential pair is incomplete")
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("secret tmpfs directory is unavailable")

    key_path = directory / "binance_mainnet_api_key"
    secret_path = directory / "binance_mainnet_api_secret"
    token_path = directory / "worker_identity_token"
    password_path = directory / "postgres_password"
    created: list[tuple[Path, tuple[int, int]]] = []
    try:
        if key:
            created.append((key_path, _write_secret(key_path, key)))
            created.append((secret_path, _write_secret(secret_path, secret)))
        created.append((token_path, _write_secret(token_path, worker_token)))
        created.append((password_path, _write_secret(password_path, postgres_password)))
    except Exception:
        for path, identity in reversed(created):
            _unlink_owned(path, identity)
        raise
    paths = {
        "BINANCE_MAINNET_API_KEY_FILE": str(key_path),
        "BINANCE_MAINNET_API_SECRET_FILE": str(secret_path),
        "WORKER_IDENTITY_TOKEN_FILE": str(token_path),
        "POSTGRES_PASSWORD_FILE": str(password_path),
    }
    return paths


def main() -> None:
    payload = sys.stdin.buffer.readline(MAX_PAYLOAD_BYTES + 1)
    if not payload.endswith(b"\n") or len(payload) > MAX_PAYLOAD_BYTES:
        raise SystemExit("local worker secret envelope is unavailable")
    paths = stage_secrets(payload[:-1], SECRET_DIRECTORY)
    os.environ["BINANCE_MAINNET_API_KEY"] = ""
    os.environ["BINANCE_MAINNET_API_SECRET"] = ""
    os.environ.update(paths)
    os.execv(sys.executable, [sys.executable, "-m", "apps.trading_worker.main"])


if __name__ == "__main__":
    main()
