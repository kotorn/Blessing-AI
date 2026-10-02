from __future__ import annotations

import io
import os
from pathlib import Path
import stat

import pytest

from apps.trading_worker import local_container_bootstrap, local_runtime


@pytest.mark.parametrize("existing", ["binance_mainnet_api_key", "binance_mainnet_api_secret", "worker_identity_token", "postgres_password"])
def test_failed_staging_preserves_preexisting_secret_files(tmp_path, existing):
    path = tmp_path / existing
    path.write_text("belongs-to-another-run", encoding="utf-8")
    with pytest.raises(FileExistsError):
        local_container_bootstrap.stage_secrets(
            b'{"apiKey":"fixture-key","apiSecret":"fixture-secret","workerIdentityToken":"fixture-token","postgresPassword":"fixture-password"}', tmp_path,
        )
    assert path.read_text(encoding="utf-8") == "belongs-to-another-run"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("written", [0, -1])
def test_partial_secret_write_failure_removes_only_created_file(monkeypatch, tmp_path, written):
    monkeypatch.setattr(local_container_bootstrap.os, "write", lambda *args: written)
    with pytest.raises(OSError, match="no progress"):
        local_container_bootstrap._write_secret(tmp_path / "partial", "fixture")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX secret modes are verified in the Linux Docker runtime")
def test_bootstrap_stages_only_expected_secrets_with_private_modes(tmp_path):
    paths = local_container_bootstrap.stage_secrets(
        b'{"apiKey":"test-key","apiSecret":"test-secret","workerIdentityToken":"worker-token","postgresPassword":"db-password"}', tmp_path
    )

    key_path = Path(paths["BINANCE_MAINNET_API_KEY_FILE"])
    secret_path = Path(paths["BINANCE_MAINNET_API_SECRET_FILE"])
    assert key_path.read_text(encoding="utf-8") == "test-key"
    assert secret_path.read_text(encoding="utf-8") == "test-secret"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert (tmp_path / "worker_identity_token").read_text(encoding="utf-8") == "worker-token"
    assert (tmp_path / "postgres_password").read_text(encoding="utf-8") == "db-password"


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"apiKey":"key","apiSecret":"secret","workerIdentityToken":"worker-token","postgresPassword":"db-password","extra":"value"}',
        b'{"apiKey":"","apiSecret":"secret","workerIdentityToken":"worker-token","postgresPassword":"db-password"}',
        b'{"apiKey":"key\\n","apiSecret":"secret","workerIdentityToken":"worker-token","postgresPassword":"db-password"}',
        b"x" * (local_container_bootstrap.MAX_PAYLOAD_BYTES + 1),
    ],
)
def test_bootstrap_rejects_invalid_secret_envelopes_without_files(payload, tmp_path):
    with pytest.raises(ValueError):
        local_container_bootstrap.stage_secrets(payload, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_bootstrap_refuses_symlink_secret_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="tmpfs directory"):
        local_container_bootstrap.stage_secrets(
            b'{"apiKey":"key","apiSecret":"secret","workerIdentityToken":"worker-token","postgresPassword":"db-password"}', link
        )
    assert list(target.iterdir()) == []


def test_bootstrap_execs_worker_with_only_tmpfs_secret_paths(monkeypatch, tmp_path):
    payload = b'{"apiKey":"test-key","apiSecret":"test-secret","workerIdentityToken":"worker-token","postgresPassword":"db-password"}\n'
    monkeypatch.setattr(local_container_bootstrap, "SECRET_DIRECTORY", tmp_path)
    monkeypatch.setattr(
        local_container_bootstrap.sys,
        "stdin",
        type("Input", (), {"buffer": io.BytesIO(payload)})(),
    )
    captured = {}

    class ExecReached(Exception):
        pass

    def capture_exec(executable, arguments):
        captured["executable"] = executable
        captured["arguments"] = arguments
        captured["environment"] = dict(os.environ)
        raise ExecReached

    monkeypatch.setattr(local_container_bootstrap.os, "execv", capture_exec)
    monkeypatch.setenv("BINANCE_MAINNET_API_KEY", "")
    monkeypatch.setenv("BINANCE_MAINNET_API_SECRET", "")
    monkeypatch.setenv("BINANCE_MAINNET_API_KEY_FILE", "")
    monkeypatch.setenv("BINANCE_MAINNET_API_SECRET_FILE", "")
    monkeypatch.setenv("WORKER_IDENTITY_TOKEN_FILE", "")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", "")
    with pytest.raises(ExecReached):
        local_container_bootstrap.main()

    assert captured["arguments"][1:] == ["-m", "apps.trading_worker.main"]
    assert captured["environment"]["BINANCE_MAINNET_API_KEY"] == ""
    assert captured["environment"]["BINANCE_MAINNET_API_SECRET"] == ""
    assert captured["environment"]["BINANCE_MAINNET_API_KEY_FILE"] == str(
        tmp_path / "binance_mainnet_api_key"
    )
    assert captured["environment"]["BINANCE_MAINNET_API_SECRET_FILE"] == str(
        tmp_path / "binance_mainnet_api_secret"
    )
    assert captured["environment"]["WORKER_IDENTITY_TOKEN_FILE"] == str(
        tmp_path / "worker_identity_token"
    )
    assert captured["environment"]["POSTGRES_PASSWORD_FILE"] == str(
        tmp_path / "postgres_password"
    )
    assert "test-key" not in repr(captured["environment"])
    assert "test-secret" not in repr(captured["environment"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX file ownership and modes are verified in the Linux Docker runtime")
def test_file_secret_reader_requires_exact_container_paths_and_private_file(monkeypatch, tmp_path):
    monkeypatch.setattr(local_runtime, "local_container_runtime", lambda values: True)
    original_reader = local_runtime.local_container_secret_file
    # Production reads /run/secrets; the helpers under test do not take a directory, so point them at tmp_path.
    monkeypatch.setattr(
        local_runtime,
        "local_container_secret_file",
        lambda variable, filename, environ=None, secret_directory=tmp_path: original_reader(
            variable, filename, environ, tmp_path
        ),
    )
    paths = local_container_bootstrap.stage_secrets(
        b'{"apiKey":"test-key","apiSecret":"test-secret","workerIdentityToken":"worker-token","postgresPassword":"db-password"}', tmp_path
    )
    environment = {
        "LOCAL_WORKER_CONTAINER": "true",
        "BINANCE_MAINNET_API_KEY_FILE": paths["BINANCE_MAINNET_API_KEY_FILE"],
        "BINANCE_MAINNET_API_SECRET_FILE": paths["BINANCE_MAINNET_API_SECRET_FILE"],
    }

    assert local_runtime.mainnet_secret_value(
        "BINANCE_MAINNET_API_KEY", environment, tmp_path
    ) == "test-key"
    assert local_runtime.mainnet_secret_value(
        "BINANCE_MAINNET_API_SECRET", environment, tmp_path
    ) == "test-secret"
    assert local_runtime.worker_identity_token_value({
        **environment,
        "WORKER_IDENTITY_TOKEN_FILE": str(tmp_path / "worker_identity_token"),
    }) == "worker-token"
    assert local_runtime.postgres_password_value({
        **environment,
        "POSTGRES_PASSWORD_FILE": str(tmp_path / "postgres_password"),
    }) == "db-password"
    assert local_runtime.mainnet_secret_value(
        "BINANCE_MAINNET_API_KEY",
        {**environment, "BINANCE_MAINNET_API_KEY_FILE": str(tmp_path / "other")},
        tmp_path,
    ) == ""

    os.chmod(tmp_path / "binance_mainnet_api_key", 0o644)
    assert local_runtime.mainnet_secret_value(
        "BINANCE_MAINNET_API_KEY", environment, tmp_path
    ) == ""


def test_file_secret_reader_keeps_direct_worker_environment_compatible():
    assert local_runtime.mainnet_secret_value(
        "BINANCE_MAINNET_API_KEY", {"BINANCE_MAINNET_API_KEY": "direct-key"}
    ) == "direct-key"
