"""Fenced execution leases for mutually-exclusive risk-increasing submissions."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Protocol


class LeaseLostError(RuntimeError):
    """Raised when an execution lease is absent, expired, or fenced out."""


class ExecutionLease(Protocol):
    scope_key: str
    owner_id: str
    fencing_token: int | None

    async def acquire(self) -> bool: ...
    async def renew(self) -> bool: ...
    async def assert_valid(self) -> None: ...
    async def release(self) -> None: ...


def execution_lease_required(environ: Optional[dict[str, str]] = None) -> bool:
    """Require a distributed lease in Cloud Run/Mainnet or when explicitly set."""

    values = environ if environ is not None else os.environ
    raw = values.get("EXECUTION_LEASE_REQUIRED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(values.get("K_SERVICE", "").strip())


@dataclass
class _InMemoryLeaseRecord:
    owner_id: str
    fencing_token: int
    expires_at: float


class InMemoryExecutionLeaseStore:
    """Small shared store used by tests and explicitly local-only runs."""

    _default: "InMemoryExecutionLeaseStore | None" = None
    _default_guard = threading.Lock()

    def __init__(self) -> None:
        self._records: dict[str, _InMemoryLeaseRecord] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def default(cls) -> "InMemoryExecutionLeaseStore":
        with cls._default_guard:
            if cls._default is None:
                cls._default = cls()
            return cls._default

    async def acquire(self, scope_key: str, owner_id: str, ttl_seconds: float) -> int | None:
        now = time.monotonic()
        async with self._lock:
            current = self._records.get(scope_key)
            if current is not None and current.expires_at > now:
                return None
            next_token = (current.fencing_token + 1) if current is not None else 1
            self._records[scope_key] = _InMemoryLeaseRecord(
                owner_id=owner_id,
                fencing_token=next_token,
                expires_at=now + ttl_seconds,
            )
            return next_token

    async def renew(
        self, scope_key: str, owner_id: str, fencing_token: int, ttl_seconds: float
    ) -> bool:
        async with self._lock:
            current = self._records.get(scope_key)
            if (
                current is None
                or current.owner_id != owner_id
                or current.fencing_token != fencing_token
                or current.expires_at <= time.monotonic()
            ):
                return False
            current.expires_at = time.monotonic() + ttl_seconds
            return True

    async def assert_valid(self, scope_key: str, owner_id: str, fencing_token: int) -> None:
        async with self._lock:
            current = self._records.get(scope_key)
            if (
                current is None
                or current.owner_id != owner_id
                or current.fencing_token != fencing_token
                or current.expires_at <= time.monotonic()
            ):
                raise LeaseLostError(f"Execution lease is not valid for scope {scope_key}")

    async def release(self, scope_key: str, owner_id: str, fencing_token: int) -> None:
        async with self._lock:
            current = self._records.get(scope_key)
            if (
                current is not None
                and current.owner_id == owner_id
                and current.fencing_token == fencing_token
            ):
                # Retain the fencing high-water mark. Removing the row would
                # let a cleanly released scope restart at token 1 and weaken
                # stale-writer protection across worker restarts.
                current.expires_at = 0

    async def lose(self, scope_key: str) -> None:
        """Test hook that simulates a competing instance fencing this scope."""

        async with self._lock:
            current = self._records.get(scope_key)
            if current is not None:
                current.expires_at = 0


class InMemoryExecutionLease:
    """Lease handle backed by a shared in-process store."""

    def __init__(
        self,
        scope_key: str,
        owner_id: Optional[str] = None,
        *,
        ttl_seconds: float = 10.0,
        store: Optional[InMemoryExecutionLeaseStore] = None,
    ) -> None:
        if not scope_key.strip():
            raise ValueError("Execution lease scope_key is required")
        if ttl_seconds <= 0:
            raise ValueError("Execution lease ttl_seconds must be positive")
        self.scope_key = scope_key
        self.owner_id = owner_id or uuid.uuid4().hex
        self.ttl_seconds = ttl_seconds
        self.store = store or InMemoryExecutionLeaseStore.default()
        self.fencing_token: int | None = None

    async def acquire(self) -> bool:
        token = await self.store.acquire(self.scope_key, self.owner_id, self.ttl_seconds)
        if token is None:
            return False
        self.fencing_token = token
        return True

    async def renew(self) -> bool:
        if self.fencing_token is None:
            return False
        return await self.store.renew(
            self.scope_key, self.owner_id, self.fencing_token, self.ttl_seconds
        )

    async def assert_valid(self) -> None:
        if self.fencing_token is None:
            raise LeaseLostError(f"Execution lease was never acquired for {self.scope_key}")
        await self.store.assert_valid(self.scope_key, self.owner_id, self.fencing_token)

    async def release(self) -> None:
        if self.fencing_token is not None:
            await self.store.release(self.scope_key, self.owner_id, self.fencing_token)
            self.fencing_token = None


class PostgresExecutionLease:
    """PostgreSQL lease using an atomic owner check and monotonically fenced token."""

    def __init__(
        self,
        db: Any,
        scope_key: str,
        owner_id: Optional[str] = None,
        *,
        ttl_seconds: float = 10.0,
    ) -> None:
        if not scope_key.strip():
            raise ValueError("Execution lease scope_key is required")
        if ttl_seconds <= 0:
            raise ValueError("Execution lease ttl_seconds must be positive")
        self.db = db
        self.scope_key = scope_key
        self.owner_id = owner_id or uuid.uuid4().hex
        self.ttl_seconds = ttl_seconds
        self.fencing_token: int | None = None

    async def acquire(self) -> bool:
        row = await self.db.fetchrow(
            """
            INSERT INTO execution_leases (scope_key, owner_id, fencing_token, lease_until)
            VALUES ($1, $2, 1, CURRENT_TIMESTAMP + ($3 * INTERVAL '1 second'))
            ON CONFLICT (scope_key) DO UPDATE SET
                owner_id = EXCLUDED.owner_id,
                fencing_token = execution_leases.fencing_token + 1,
                lease_until = CURRENT_TIMESTAMP + ($3 * INTERVAL '1 second'),
                updated_at = CURRENT_TIMESTAMP
            WHERE execution_leases.lease_until <= CURRENT_TIMESTAMP
            RETURNING fencing_token
            """,
            self.scope_key,
            self.owner_id,
            self.ttl_seconds,
        )
        if row is None:
            return False
        self.fencing_token = int(row["fencing_token"])
        return True

    async def renew(self) -> bool:
        if self.fencing_token is None:
            return False
        row = await self.db.fetchrow(
            """
            UPDATE execution_leases
            SET lease_until = CURRENT_TIMESTAMP + ($4 * INTERVAL '1 second'),
                updated_at = CURRENT_TIMESTAMP
            WHERE scope_key = $1
              AND owner_id = $2
              AND fencing_token = $3
              AND lease_until > CURRENT_TIMESTAMP
            RETURNING fencing_token
            """,
            self.scope_key,
            self.owner_id,
            self.fencing_token,
            self.ttl_seconds,
        )
        return row is not None

    async def assert_valid(self) -> None:
        if self.fencing_token is None:
            raise LeaseLostError(f"Execution lease was never acquired for {self.scope_key}")
        row = await self.db.fetchrow(
            """
            SELECT fencing_token
            FROM execution_leases
            WHERE scope_key = $1
              AND owner_id = $2
              AND fencing_token = $3
              AND lease_until > CURRENT_TIMESTAMP
            """,
            self.scope_key,
            self.owner_id,
            self.fencing_token,
        )
        if row is None:
            raise LeaseLostError(f"Execution lease is not valid for scope {self.scope_key}")

    async def release(self) -> None:
        if self.fencing_token is None:
            return
        await self.db.execute(
            """
            UPDATE execution_leases
            SET lease_until = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE scope_key = $1 AND owner_id = $2 AND fencing_token = $3
            """,
            self.scope_key,
            self.owner_id,
            self.fencing_token,
        )
        self.fencing_token = None
