"""Small, explicitly configured PostgreSQL client used by the worker outbox."""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Mapping, Optional
from urllib.parse import quote, urlsplit

import asyncpg
from apps.trading_worker.local_runtime import (
    local_container_runtime,
    local_postgres_host,
    postgres_password_value,
)

logger = logging.getLogger("blessing.persistence.postgres")


def redact_error(value: object) -> str:
    """Keep database diagnostics useful without exposing credentials."""

    text = str(value)
    text = re.sub(
        r"(?i)(postgres(?:ql)?://)[^\s/@:]+(?::[^\s/@]*)?@",
        r"\1<redacted>@",
        text,
    )
    text = re.sub(
        r"(?i)(password\s*[=:]\s*)[^\s,;]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)(POSTGRES_PASSWORD\s*[=:]\s*)[^\s,;]+",
        r"\1<redacted>",
        text,
    )
    return text[:500]


class PostgresConfigurationError(RuntimeError):
    """Raised when persistence was requested without complete DB configuration."""


def _local_only_enabled(values: Mapping[str, str]) -> bool:
    return str(values.get("LOCAL_ONLY", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """Connection settings derived from environment without credential defaults."""

    dsn: Optional[str] = None
    missing: tuple[str, ...] = ()
    configuration_error: Optional[str] = None
    runtime_target: str = "CLOUD"
    database_provider: str = "POSTGRES"
    safe_host: Optional[str] = None
    safe_port: Optional[int] = None
    expected_database: Optional[str] = None
    expected_user: Optional[str] = None
    local_container: bool = False

    @classmethod
    def from_environment(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "PostgresSettings":
        values = environ if environ is not None else os.environ
        local_only = _local_only_enabled(values)
        container_runtime = local_container_runtime(values)
        expected_local_host = local_postgres_host(values)
        database_url = str(values.get("DATABASE_URL", "")).strip()
        if database_url:
            if local_only:
                return cls(
                    configuration_error=(
                        "LOCAL_ONLY forbids DATABASE_URL; configure PostgreSQL "
                        f"with POSTGRES_HOST={expected_local_host} and POSTGRES_PORT=5433"
                    ),
                    runtime_target="LOCAL",
                    database_provider="POSTGRES_LOCAL",
                    local_container=container_runtime,
                )
            return cls(dsn=database_url)

        required = (
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DB",
            "POSTGRES_USER",
        )
        missing_values = [name for name in required if not str(values.get(name, "")).strip()]
        password = postgres_password_value(values)
        if not password:
            missing_values.append("POSTGRES_PASSWORD")
        missing = tuple(missing_values)
        if missing:
            return cls(
                missing=missing,
                runtime_target="LOCAL" if local_only else "CLOUD",
                database_provider="POSTGRES_LOCAL" if local_only else "POSTGRES",
                expected_database=str(values.get("POSTGRES_DB", "")).strip() or None,
                expected_user=str(values.get("POSTGRES_USER", "")).strip() or None,
                local_container=container_runtime,
            )

        user = quote(str(values["POSTGRES_USER"]), safe="")
        password = quote(password, safe="")
        host = str(values["POSTGRES_HOST"]).strip()
        port = str(values["POSTGRES_PORT"]).strip()
        database = str(values["POSTGRES_DB"]).strip()
        if local_only and (host != expected_local_host or port != "5433"):
            return cls(
                configuration_error=(
                    f"LOCAL_ONLY requires PostgreSQL at exactly {expected_local_host}:5433"
                ),
                runtime_target="LOCAL",
                database_provider="POSTGRES_LOCAL",
                safe_host=host,
                safe_port=int(port) if port.isdecimal() else None,
                local_container=container_runtime,
            )
        if host.startswith("/"):
            # Cloud Run exposes an attached Cloud SQL instance as a Unix
            # socket. asyncpg requires the socket path in the query string;
            # treating it as a TCP hostname would silently fail readiness.
            socket_host = quote(host, safe="")
            return cls(
                dsn=f"postgresql://{user}:{password}@/{database}?host={socket_host}",
                database_provider="CLOUD_SQL",
                expected_database=database,
                expected_user=str(values["POSTGRES_USER"]).strip(),
            )
        return cls(
            dsn=f"postgresql://{user}:{password}@{host}:{port}/{database}",
            runtime_target="LOCAL" if local_only else "CLOUD",
            database_provider="POSTGRES_LOCAL" if local_only else "POSTGRES",
            safe_host=host if local_only else None,
            safe_port=int(port) if local_only else None,
            expected_database=database,
            expected_user=str(values["POSTGRES_USER"]).strip(),
            local_container=container_runtime,
        )


def _safe_connection_target(dsn: str) -> str:
    """Return host/port only; never expose userinfo or query parameters."""

    try:
        parsed = urlsplit(dsn)
        host = parsed.hostname or "unknown"
        return f"{host}:{parsed.port}" if parsed.port else host
    except ValueError:
        return "configured-database"


class PostgresClient:
    """Async PostgreSQL facade with explicit transactions for outbox replay."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        min_size: int = 1,
        max_size: int = 10,
        *,
        config_error: Optional[str] = None,
        runtime_target: str = "CLOUD",
        database_provider: str = "POSTGRES",
        safe_host: Optional[str] = None,
        safe_port: Optional[int] = None,
        expected_database: Optional[str] = None,
        expected_user: Optional[str] = None,
        local_container: bool = False,
    ) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.config_error = config_error
        self.runtime_target = runtime_target
        self.database_provider = database_provider
        self.safe_host = safe_host
        self.safe_port = safe_port
        self.expected_database = expected_database
        self.expected_user = expected_user
        self.local_container = local_container
        self.pool: Optional[asyncpg.Pool] = None
        self._local_identity_verified = False

    def readiness_identity(self) -> dict[str, object]:
        """Return non-secret database identity suitable for readiness output."""

        return {
            "runtime_target": self.runtime_target,
            "database_provider": self.database_provider,
            "database_host": self.safe_host,
            "database_port": self.safe_port,
            "database_identity_verified": self._local_identity_verified,
            "database_container_runtime": self.local_container,
        }

    async def verify_local_database_identity(self) -> None:
        """Read back the actual PostgreSQL endpoint identity for Local runtime."""
        if (
            self.runtime_target != "LOCAL"
            or self.database_provider != "POSTGRES_LOCAL"
            or self.safe_host != ("host.docker.internal" if self.local_container else "127.0.0.1")
            or self.safe_port != 5433
            or not self.expected_database
            or not self.expected_user
        ):
            raise PostgresConfigurationError("Local PostgreSQL identity configuration is invalid")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT current_database() AS database_name,
                       current_user AS database_user,
                       inet_server_port() AS server_port,
                       inet_client_addr() AS client_address
                """
            )
        if (
            row is None
            or str(row["database_name"]) != self.expected_database
            or str(row["database_user"]) != self.expected_user
            or int(row["server_port"] or 0) != 5432
            or row["client_address"] is None
        ):
            self._local_identity_verified = False
            raise PostgresConfigurationError("Connected PostgreSQL server identity does not match Local configuration")
        self._local_identity_verified = True

    async def connect(self) -> None:
        if self.pool is not None:
            return
        if self.config_error:
            raise PostgresConfigurationError(self.config_error)
        if not self.dsn:
            raise PostgresConfigurationError(
                "PostgreSQL configuration is unavailable; set DATABASE_URL or all POSTGRES_* values"
            )

        target = _safe_connection_target(self.dsn)
        logger.info("Connecting to PostgreSQL at %s", target)
        try:
            self.pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=self.min_size,
                max_size=self.max_size,
                server_settings={
                    "application_name": "blessing-trading-worker",
                    "search_path": "public",
                },
            )
            if self.runtime_target == "LOCAL":
                await self.verify_local_database_identity()
        except Exception as exc:
            pool = self.pool
            self.pool = None
            self._local_identity_verified = False
            if pool is not None:
                try:
                    await pool.close()
                except Exception:
                    pass
            logger.error(
                "PostgreSQL connection failed at %s (%s)",
                target,
                type(exc).__name__,
            )
            raise
        logger.info("PostgreSQL connection pool established at %s", target)

    async def disconnect(self) -> None:
        pool = self.pool
        self.pool = None
        self._local_identity_verified = False
        if pool is not None:
            await pool.close()
            logger.info("PostgreSQL connection pool closed")

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Database pool is not initialized")
        return self.pool

    async def execute(self, query: str, *args: object) -> str:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            return await connection.execute(query, *args)

    async def fetch(self, query: str, *args: object) -> list[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def fetchrow(
        self, query: str, *args: object
    ) -> Optional[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: object) -> object:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            return await connection.fetchval(query, *args)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire one connection and keep all operations in one transaction."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                yield connection


def get_postgres_client(
    environ: Optional[Mapping[str, str]] = None,
) -> PostgresClient:
    """Build a client from explicit environment configuration.

    Missing configuration is retained as a sanitized error and reported by the
    persistence manager at startup. This keeps importing the worker harmless
    while still making REQUIRED persistence fail closed.
    """

    settings = PostgresSettings.from_environment(environ)
    config_error = None
    if settings.configuration_error:
        config_error = settings.configuration_error
    elif settings.missing:
        config_error = (
            "PostgreSQL configuration is incomplete; missing: "
            + ", ".join(settings.missing)
        )
    return PostgresClient(
        dsn=settings.dsn,
        config_error=config_error,
        runtime_target=settings.runtime_target,
        database_provider=settings.database_provider,
        safe_host=settings.safe_host,
        safe_port=settings.safe_port,
        expected_database=settings.expected_database,
        expected_user=settings.expected_user,
        local_container=settings.local_container,
    )
