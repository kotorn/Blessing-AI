from __future__ import annotations

from contextlib import asynccontextmanager
import os

import pytest

from apps.trading_worker.persistence.manager import (
    PersistenceConfig,
    PersistenceManager,
    PersistenceMode,
)
from apps.trading_worker.persistence.postgres.client import (
    PostgresConfigurationError,
    PostgresSettings,
    get_postgres_client,
)


LOCAL_ENV = {
    "LOCAL_ONLY": "true",
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "5433",
    "POSTGRES_DB": "blessing_trading",
    "POSTGRES_USER": "blessing_worker",
    "POSTGRES_PASSWORD": "unit-test-only",
}


def test_local_settings_report_safe_postgres_identity() -> None:
    settings = PostgresSettings.from_environment(LOCAL_ENV)

    assert settings.configuration_error is None
    assert settings.runtime_target == "LOCAL"
    assert settings.database_provider == "POSTGRES_LOCAL"
    assert settings.safe_host == "127.0.0.1"
    assert settings.safe_port == 5433
    assert settings.dsn is not None
    assert "unit-test-only" in settings.dsn


def test_local_readiness_reports_identity_without_credentials() -> None:
    client = get_postgres_client(LOCAL_ENV)
    manager = PersistenceManager(
        db=client,
        config=PersistenceConfig(mode=PersistenceMode.REQUIRED),
    )

    readiness = manager.readiness()

    assert readiness["runtime_target"] == "LOCAL"
    assert readiness["database_provider"] == "POSTGRES_LOCAL"
    assert readiness["database_host"] == "127.0.0.1"
    assert readiness["database_port"] == 5433
    assert readiness["database_identity_verified"] is False
    assert readiness["schema_verified"] is False
    assert readiness["ready"] is False
    assert "unit-test-only" not in repr(readiness)


@pytest.mark.parametrize(
    ("host", "container_runtime", "expected_ready"),
    [
        ("127.0.0.1", False, True),
        ("host.docker.internal", True, True),
        ("host.docker.internal", False, False),
        ("192.168.1.100", True, False),
    ],
)
def test_local_readiness_accepts_only_verified_loopback_or_container_endpoint(
    host, container_runtime, expected_ready
) -> None:
    class ReadyDatabase:
        config_error = None

        def readiness_identity(self):
            return {
                "runtime_target": "LOCAL",
                "database_provider": "POSTGRES_LOCAL",
                "database_host": host,
                "database_port": 5433,
                "database_identity_verified": True,
                "database_container_runtime": container_runtime,
            }

    manager = PersistenceManager(
        db=ReadyDatabase(),
        config=PersistenceConfig(mode=PersistenceMode.REQUIRED),
    )
    manager.is_connected = True
    manager._state = "READY"
    manager._schema_verified = True

    readiness = manager.readiness()

    assert readiness["ready"] is expected_ready
    assert readiness["durable"] is expected_ready
    assert readiness["database_identity_verified"] is expected_ready


def test_local_rejects_database_url_even_when_it_points_to_loopback() -> None:
    settings = PostgresSettings.from_environment(
        {**LOCAL_ENV, "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5433/db"}
    )

    assert settings.dsn is None
    assert settings.configuration_error is not None
    assert "DATABASE_URL" in settings.configuration_error


def test_local_rejects_non_loopback_host_or_non_local_port() -> None:
    for override in (
        {"POSTGRES_HOST": "localhost"},
        {"POSTGRES_HOST": "192.168.1.100"},
        {"POSTGRES_PORT": "5432"},
        {"POSTGRES_PORT": "15433"},
    ):
        settings = PostgresSettings.from_environment({**LOCAL_ENV, **override})
        assert settings.dsn is None
        assert settings.configuration_error == (
            "LOCAL_ONLY requires PostgreSQL at exactly 127.0.0.1:5433"
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX secret modes are verified in the Linux Docker runtime")
def test_container_settings_read_database_password_from_private_file(monkeypatch, tmp_path):
    import apps.trading_worker.local_runtime as runtime_module
    import apps.trading_worker.persistence.postgres.client as client_module

    monkeypatch.setattr(runtime_module, "local_container_runtime", lambda values: True)
    monkeypatch.setattr(client_module, "local_container_runtime", lambda values: True)
    monkeypatch.setattr(client_module, "local_postgres_host", lambda values: "host.docker.internal")
    password_file = tmp_path / "postgres_password"
    password_file.write_text("container-db-password", encoding="utf-8")
    password_file.chmod(0o600)
    container_env = {
        **LOCAL_ENV,
        "LOCAL_WORKER_CONTAINER": "true",
        "POSTGRES_HOST": "host.docker.internal",
        "POSTGRES_PASSWORD": "",
        "POSTGRES_PASSWORD_FILE": str(password_file),
    }

    settings = PostgresSettings.from_environment(container_env)
    assert settings.configuration_error is None
    assert settings.local_container is True
    assert settings.safe_host == "host.docker.internal"
    assert settings.dsn is not None and "container-db-password" in settings.dsn


def test_container_local_settings_require_container_marker_and_exact_host(monkeypatch) -> None:
    import apps.trading_worker.persistence.postgres.client as client_module

    monkeypatch.setattr(client_module, "local_container_runtime", lambda values: True)
    monkeypatch.setattr(client_module, "local_postgres_host", lambda values: "host.docker.internal")
    container_env = {
        **LOCAL_ENV,
        "LOCAL_WORKER_CONTAINER": "true",
        "POSTGRES_HOST": "host.docker.internal",
    }
    settings = PostgresSettings.from_environment(container_env)

    assert settings.configuration_error is None
    assert settings.local_container is True
    assert settings.safe_host == "host.docker.internal"
    assert settings.safe_port == 5433

    wrong_host = PostgresSettings.from_environment(
        {**container_env, "POSTGRES_HOST": "192.168.1.100"}
    )
    assert wrong_host.dsn is None
    assert wrong_host.configuration_error == (
        "LOCAL_ONLY requires PostgreSQL at exactly host.docker.internal:5433"
    )


def test_cloud_socket_configuration_remains_supported() -> None:
    settings = PostgresSettings.from_environment(
        {
            "POSTGRES_HOST": "/cloudsql/project:region:instance",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "blessing_trading",
            "POSTGRES_USER": "worker",
            "POSTGRES_PASSWORD": "unit-test-only",
        }
    )

    assert settings.configuration_error is None
    assert settings.runtime_target == "CLOUD"
    assert settings.database_provider == "CLOUD_SQL"
    assert settings.dsn is not None
    assert "host=%2Fcloudsql%2Fproject%3Aregion%3Ainstance" in settings.dsn


def test_local_invalid_identity_is_not_ready_even_if_persistence_is_optional() -> None:
    client = get_postgres_client({**LOCAL_ENV, "POSTGRES_PORT": "5432"})
    manager = PersistenceManager(
        db=client,
        config=PersistenceConfig(mode=PersistenceMode.OPTIONAL),
    )

    readiness = manager.readiness()

    assert readiness["database_identity_verified"] is False
    assert readiness["ready"] is False
    assert readiness["durable"] is False


class IdentityConnection:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, query):
        return self.row


class IdentityPool:
    def __init__(self, row):
        self.connection = IdentityConnection(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_local_database_identity_requires_actual_server_readback():
    client = get_postgres_client(LOCAL_ENV)
    client.pool = IdentityPool(
        {
            "database_name": "blessing_trading",
            "database_user": "blessing_worker",
            "server_port": 5432,
            "client_address": "172.18.0.1",
        }
    )

    assert client.readiness_identity()["database_identity_verified"] is False
    await client.verify_local_database_identity()
    assert client.readiness_identity()["database_identity_verified"] is True


@pytest.mark.asyncio
async def test_local_database_identity_mismatch_fails_closed():
    client = get_postgres_client(LOCAL_ENV)
    client.pool = IdentityPool(
        {
            "database_name": "unexpected_database",
            "database_user": "blessing_worker",
            "server_port": 5432,
            "client_address": "172.18.0.1",
        }
    )

    with pytest.raises(PostgresConfigurationError, match="server identity"):
        await client.verify_local_database_identity()
    assert client.readiness_identity()["database_identity_verified"] is False


@pytest.mark.asyncio
async def test_local_persistence_refuses_cloud_database_configuration(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")

    class CloudDatabase:
        runtime_target = "CLOUD"

        def __init__(self):
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1

        async def disconnect(self):
            return None

    db = CloudDatabase()
    manager = PersistenceManager(
        db=db,
        config=PersistenceConfig(mode=PersistenceMode.REQUIRED),
    )

    with pytest.raises(RuntimeError, match="not configured as POSTGRES_LOCAL"):
        await manager.start()
    assert db.connect_calls == 0
    assert manager.readiness()["ready"] is False


@pytest.mark.asyncio
async def test_postgres_schema_verification_is_required_for_cloud_and_local(monkeypatch):
    import scripts.apply_local_postgres_migrations as migrations

    verified = []

    async def verify_schema(db):
        verified.append(db)

    monkeypatch.setattr(migrations, "verify_schema", verify_schema)

    class SchemaDatabase:
        runtime_target = "CLOUD"

        async def fetchval(self, query):
            return True

        async def fetch(self, query):
            from scripts.apply_local_postgres_migrations import (
                discover_migrations,
                migration_checksum,
            )

            return [
                {"version": item.name, "checksum": migration_checksum(item)}
                for item in discover_migrations()
            ]

    db = SchemaDatabase()
    cloud_manager = PersistenceManager(
        db=db,  # type: ignore[arg-type]
        config=PersistenceConfig(mode=PersistenceMode.REQUIRED),
    )
    await cloud_manager._verify_postgres_schema(require_local_ledger=False)
    assert cloud_manager._schema_verified is True
    assert verified == [db]

    local_manager = PersistenceManager(
        db=db,  # type: ignore[arg-type]
        config=PersistenceConfig(mode=PersistenceMode.REQUIRED),
    )
    await local_manager._verify_postgres_schema(require_local_ledger=True)
    assert local_manager._schema_verified is True
    assert verified == [db, db]
