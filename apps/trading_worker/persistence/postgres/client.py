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


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """Connection settings derived from environment without credential defaults."""

    dsn: Optional[str] = None
    missing: tuple[str, ...] = ()

    @classmethod
    def from_environment(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "PostgresSettings":
        values = environ if environ is not None else os.environ
        database_url = str(values.get("DATABASE_URL", "")).strip()
        if database_url:
            return cls(dsn=database_url)

        required = (
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
        )
        missing = tuple(name for name in required if not str(values.get(name, "")).strip())
        if missing:
            return cls(missing=missing)

        user = quote(str(values["POSTGRES_USER"]), safe="")
        password = quote(str(values["POSTGRES_PASSWORD"]), safe="")
        host = str(values["POSTGRES_HOST"]).strip()
        port = str(values["POSTGRES_PORT"]).strip()
        database = str(values["POSTGRES_DB"]).strip()
        if host.startswith("/"):
            # Cloud Run exposes an attached Cloud SQL instance as a Unix
            # socket. asyncpg requires the socket path in the query string;
            # treating it as a TCP hostname would silently fail readiness.
            socket_host = quote(host, safe="")
            return cls(dsn=f"postgresql://{user}:{password}@/{database}?host={socket_host}")
        return cls(dsn=f"postgresql://{user}:{password}@{host}:{port}/{database}")


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
    ) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.config_error = config_error
        self.pool: Optional[asyncpg.Pool] = None

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
            )
        except Exception as exc:
            self.pool = None
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
    if settings.missing:
        config_error = (
            "PostgreSQL configuration is incomplete; missing: "
            + ", ".join(settings.missing)
        )
    return PostgresClient(dsn=settings.dsn, config_error=config_error)
