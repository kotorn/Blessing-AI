"""Durable queue storage backends.

SQLite is the authoritative local backend.  JSONL is intentionally explicit
and single-writer; it is a fallback for machines where SQLite WAL cannot be
used, not an implicit recovery path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from builtins import list as builtin_list
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .models import (
    ExternalEffectClass,
    GateState,
    JobKind,
    JobRecord,
    JobStatus,
    utc_iso,
)
from .security import redact_json


class QueueStorageError(RuntimeError):
    """Raised when durable queue storage cannot be trusted."""


class QueueConflictError(QueueStorageError):
    """Raised when an idempotent operation conflicts with prior state."""


def _lease_expiry(now: str, lease_ttl_sec: int) -> str:
    """Calculate a lease from the caller's logical UTC time.

    Queue operations accept an explicit timestamp so replay, recovery, and
    tests can use one consistent clock.  Do not mix it with wall-clock time
    when calculating expiry.
    """

    parsed = datetime.fromisoformat(now)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return utc_iso(parsed.astimezone(UTC) + timedelta(seconds=lease_ttl_sec))


@dataclass(frozen=True, slots=True)
class QueuePaths:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "queue.db"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def events(self) -> Path:
        return self.root / "events"

    @property
    def inbox(self) -> Path:
        return self.root / "inbox.jsonl"

    @property
    def event_log(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def lock(self) -> Path:
        return self.root / ".queue.lock"

    def ensure(self) -> None:
        if self.root.is_absolute() is False:
            raise QueueStorageError("Queue root must be an absolute path")
        if os.name == "nt" and str(self.root).startswith("\\\\"):
            raise QueueStorageError("SQLite WAL queue cannot use a network filesystem")
        self.root.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        self.events.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            # Windows ACL inheritance is managed by the user profile.  The
            # queue never stores credentials and still fails closed on paths
            # that are explicitly network-backed.
            pass


def default_queue_root() -> Path:
    """Return the non-repository default runtime directory on Windows."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "agy-queue" / "blessing-ai"
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        return Path(user_profile) / "AppData" / "Local" / "agy-queue" / "blessing-ai"
    return Path("C:/Users/Public/AppData/Local/agy-queue/blessing-ai")


class ResultArtifactStore:
    """Write redacted result and NDJSON artifacts atomically and durably."""

    def __init__(self, paths: QueuePaths):
        self.paths = paths
        self.paths.ensure()
        self._lock = threading.Lock()

    def result_path(self, job_id: str) -> Path:
        return self.paths.results / f"{job_id}.json"

    def events_path(self, job_id: str) -> Path:
        return self.paths.events / f"{job_id}.jsonl"

    def append_event(self, job_id: str, event: Any) -> str:
        path = self.events_path(job_id)
        payload = redact_json(event)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock, path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return str(path)

    def write_result(self, job_id: str, envelope: dict[str, Any]) -> str:
        path = self.result_path(job_id)
        temporary = path.with_suffix(".json.tmp")
        payload = redact_json(envelope)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        with self._lock:
            if path.exists():
                existing = path.read_text(encoding="utf-8")
                if existing != serialized + "\n":
                    raise QueueStorageError(f"Result artifact for {job_id} already differs")
                return str(path)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        return str(path)


class QueueStore(Protocol):
    """Stable storage interface shared by SQLite and JSONL backends."""

    paths: QueuePaths

    def close(self) -> None: ...

    def create_job(self, job: JobRecord) -> JobRecord: ...

    def get(self, job_id: str) -> JobRecord | None: ...

    def list(
        self,
        *,
        status: JobStatus | None = None,
        job_kind: JobKind | None = None,
        limit: int = 100,
    ) -> builtin_list[JobRecord]: ...

    def claim_next(
        self,
        worker_id: str,
        *,
        now: str,
        lease_ttl_sec: int,
        eligible_kinds: Collection[JobKind] | None = None,
    ) -> JobRecord | None: ...

    def heartbeat(self, job_id: str, worker_id: str, lease_until: str) -> bool: ...

    def recover_expired(self, *, now: str) -> builtin_list[str]: ...

    def append_event(self, job_id: str, event: Any, event_type: str | None = None) -> None: ...

    def update_conversation_id(self, job_id: str, conversation_id: str) -> None: ...

    def mark_success(
        self,
        job_id: str,
        *,
        result: dict[str, Any],
        result_path: str,
        conversation_id: str | None,
        head_sha: str | None,
        gate_state: GateState = GateState.VERIFIED,
    ) -> None: ...

    def mark_failure(
        self,
        job_id: str,
        *,
        error_code: str,
        stderr: str | None,
        result: dict[str, Any] | None = None,
        head_sha: str | None = None,
        gate_state: GateState = GateState.BLOCKED,
    ) -> None: ...

    def schedule_retry(
        self,
        job_id: str,
        *,
        next_attempt_at: str,
        error_code: str,
        stderr: str | None,
    ) -> bool: ...

    def retry_failed(self, job_id: str, *, now: str) -> bool: ...

    def cancel(self, job_id: str) -> bool: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  prompt TEXT NOT NULL,
  repo TEXT NOT NULL,
  base_sha TEXT,
  model TEXT NOT NULL,
  effort TEXT NOT NULL,
  timeout_sec INTEGER NOT NULL DEFAULT 300,
  status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  lease_until TEXT,
  worker_id TEXT,
  conversation_id TEXT,
  result_path TEXT,
  raw_events_path TEXT,
  result_json TEXT,
  stderr TEXT,
  error_code TEXT,
  started_at TEXT,
  finished_at TEXT,
  branch TEXT,
  head_sha TEXT,
  next_attempt_at TEXT,
  session_key TEXT,
  scope_key TEXT,
  job_kind TEXT NOT NULL,
  external_effect_class TEXT NOT NULL,
  gate_state TEXT NOT NULL,
  authorization_ref TEXT,
  verification_job_id TEXT,
  expected_output_schema TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at, job_id);
CREATE INDEX IF NOT EXISTS jobs_eligible ON jobs(status, gate_state, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS job_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(job_id, sequence)
);
CREATE INDEX IF NOT EXISTS job_events_job_sequence ON job_events(job_id, sequence);
CREATE TABLE IF NOT EXISTS job_results (
  job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(redact_json(value), ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": "<INVALID_JSON>"}


def _job_payload(job: JobRecord) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "prompt": job.prompt,
        "repo": job.repo,
        "base_sha": job.base_sha,
        "model": job.model,
        "effort": job.effort,
        "timeout_sec": job.timeout_sec,
        "status": job.status.value,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "lease_until": job.lease_until,
        "worker_id": job.worker_id,
        "conversation_id": job.conversation_id,
        "result_path": job.result_path,
        "raw_events_path": job.raw_events_path,
        "result_json": job.result_json,
        "stderr": job.stderr,
        "error_code": job.error_code,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "branch": job.branch,
        "head_sha": job.head_sha,
        "next_attempt_at": job.next_attempt_at,
        "session_key": job.session_key,
        "scope_key": job.scope_key,
        "job_kind": job.job_kind.value,
        "external_effect_class": job.external_effect_class.value,
        "gate_state": job.gate_state.value,
        "authorization_ref": job.authorization_ref,
        "verification_job_id": job.verification_job_id,
        "expected_output_schema": job.expected_output_schema,
    }


def _payload_to_job(payload: dict[str, Any]) -> JobRecord:
    return JobRecord(
        job_id=str(payload["job_id"]),
        created_at=str(payload["created_at"]),
        updated_at=str(payload["updated_at"]),
        prompt=str(payload["prompt"]),
        repo=str(payload["repo"]),
        base_sha=payload.get("base_sha"),
        model=str(payload["model"]),
        effort=str(payload["effort"]),
        timeout_sec=int(payload.get("timeout_sec", 300)),
        status=JobStatus(str(payload["status"])),
        attempt=int(payload.get("attempt", 0)),
        max_attempts=int(payload.get("max_attempts", 3)),
        lease_until=payload.get("lease_until"),
        worker_id=payload.get("worker_id"),
        conversation_id=payload.get("conversation_id"),
        result_path=payload.get("result_path"),
        raw_events_path=payload.get("raw_events_path"),
        error_code=payload.get("error_code"),
        stderr=payload.get("stderr"),
        started_at=payload.get("started_at"),
        finished_at=payload.get("finished_at"),
        branch=payload.get("branch"),
        head_sha=payload.get("head_sha"),
        next_attempt_at=payload.get("next_attempt_at"),
        session_key=payload.get("session_key"),
        scope_key=payload.get("scope_key"),
        job_kind=JobKind(str(payload.get("job_kind", JobKind.READ_ONLY.value))),
        external_effect_class=ExternalEffectClass(
            str(payload.get("external_effect_class", ExternalEffectClass.NONE.value))
        ),
        gate_state=GateState(str(payload.get("gate_state", GateState.READY.value))),
        authorization_ref=payload.get("authorization_ref"),
        verification_job_id=payload.get("verification_job_id"),
        expected_output_schema=payload.get("expected_output_schema"),
        result_json=payload.get("result_json"),
    )


class SQLiteQueueStore:
    """SQLite WAL-backed queue with atomic claims and idempotent results."""

    def __init__(self, database_path: str | Path, *, paths: QueuePaths | None = None):
        self.database_path = Path(database_path).resolve()
        self.paths = paths or QueuePaths(self.database_path.parent)
        self.paths.ensure()
        if self.database_path != self.paths.database:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            connection.close()
            raise QueueStorageError("SQLite WAL could not be enabled")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            migrations: dict[str, str] = {
                "timeout_sec": "ALTER TABLE jobs ADD COLUMN timeout_sec INTEGER NOT NULL DEFAULT 300",
                "result_path": "ALTER TABLE jobs ADD COLUMN result_path TEXT",
                "raw_events_path": "ALTER TABLE jobs ADD COLUMN raw_events_path TEXT",
                "branch": "ALTER TABLE jobs ADD COLUMN branch TEXT",
                "head_sha": "ALTER TABLE jobs ADD COLUMN head_sha TEXT",
                "next_attempt_at": "ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT",
                "session_key": "ALTER TABLE jobs ADD COLUMN session_key TEXT",
                "scope_key": "ALTER TABLE jobs ADD COLUMN scope_key TEXT",
                "job_kind": "ALTER TABLE jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'READ_ONLY'",
                "external_effect_class": "ALTER TABLE jobs ADD COLUMN external_effect_class TEXT NOT NULL DEFAULT 'NONE'",
                "gate_state": "ALTER TABLE jobs ADD COLUMN gate_state TEXT NOT NULL DEFAULT 'READY'",
                "authorization_ref": "ALTER TABLE jobs ADD COLUMN authorization_ref TEXT",
                "verification_job_id": "ALTER TABLE jobs ADD COLUMN verification_job_id TEXT",
                "expected_output_schema": "ALTER TABLE jobs ADD COLUMN expected_output_schema TEXT",
            }
            for name, statement in migrations.items():
                if name not in columns:
                    connection.execute(statement)
        finally:
            connection.close()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        payload = dict(row)
        stored_result = payload.pop("stored_result_json", None)
        payload["result_json"] = _json_loads(stored_result or payload.get("result_json"))
        payload["expected_output_schema"] = _json_loads(payload.get("expected_output_schema"))
        return _payload_to_job(payload)

    def _select_job(self, connection: sqlite3.Connection, job_id: str) -> JobRecord | None:
        row = connection.execute(
            """
            SELECT jobs.*, job_results.result_json AS stored_result_json
            FROM jobs LEFT JOIN job_results ON job_results.job_id = jobs.job_id
            WHERE jobs.job_id = ?
            """,
            (job_id,),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def close(self) -> None:
        return None

    def create_job(self, job: JobRecord) -> JobRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO jobs (
                  job_id, created_at, updated_at, prompt, repo, base_sha, model,
                  effort, timeout_sec, status, attempt, max_attempts, lease_until, worker_id,
                  conversation_id, result_path, raw_events_path, result_json,
                  stderr, error_code, started_at, finished_at, branch, head_sha,
                  next_attempt_at, session_key, scope_key, job_kind,
                  external_effect_class, gate_state, authorization_ref,
                  verification_job_id, expected_output_schema
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.created_at,
                    job.updated_at,
                    job.prompt,
                    job.repo,
                    job.base_sha,
                    job.model,
                    job.effort,
                    job.timeout_sec,
                    job.status.value,
                    job.attempt,
                    job.max_attempts,
                    job.lease_until,
                    job.worker_id,
                    job.conversation_id,
                    job.result_path,
                    job.raw_events_path,
                    None,
                    job.stderr,
                    job.error_code,
                    job.started_at,
                    job.finished_at,
                    job.branch,
                    job.head_sha,
                    job.next_attempt_at,
                    job.session_key,
                    job.scope_key,
                    job.job_kind.value,
                    job.external_effect_class.value,
                    job.gate_state.value,
                    job.authorization_ref,
                    job.verification_job_id,
                    _json_dumps(job.expected_output_schema),
                ),
            )
            connection.commit()
            return job
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise QueueConflictError(f"Job {job.job_id} already exists") from exc
        finally:
            connection.close()

    def get(self, job_id: str) -> JobRecord | None:
        connection = self._connect()
        try:
            return self._select_job(connection, job_id)
        finally:
            connection.close()

    def list(
        self,
        *,
        status: JobStatus | None = None,
        job_kind: JobKind | None = None,
        limit: int = 100,
    ) -> builtin_list[JobRecord]:
        safe_limit = max(1, min(int(limit), 1000))
        conditions: list[str] = []
        values: list[Any] = []
        if status is not None:
            conditions.append("jobs.status = ?")
            values.append(status.value)
        if job_kind is not None:
            conditions.append("jobs.job_kind = ?")
            values.append(job_kind.value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT jobs.*, job_results.result_json AS stored_result_json
                FROM jobs LEFT JOIN job_results ON job_results.job_id = jobs.job_id
                {where}
                ORDER BY jobs.created_at DESC, jobs.job_id DESC
                LIMIT ?
                """,
                (*values, safe_limit),
            ).fetchall()
            return [self._row_to_job(row) for row in rows]
        finally:
            connection.close()

    def claim_next(
        self,
        worker_id: str,
        *,
        now: str,
        lease_ttl_sec: int,
        eligible_kinds: Collection[JobKind] | None = None,
    ) -> JobRecord | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            conditions = [
                "jobs.status = 'pending'",
                "jobs.gate_state = 'READY'",
                "(jobs.next_attempt_at IS NULL OR jobs.next_attempt_at <= ?)",
                (
                    "NOT EXISTS ("
                    "SELECT 1 FROM jobs AS active "
                    "WHERE active.status = 'running' "
                    "AND active.scope_key IS NOT NULL "
                    "AND active.scope_key = jobs.scope_key"
                    ")"
                ),
            ]
            values: list[Any] = [now]
            if eligible_kinds is not None:
                kinds = list(eligible_kinds)
                if not kinds:
                    connection.rollback()
                    return None
                placeholders = ",".join("?" for _ in kinds)
                conditions.append(f"jobs.job_kind IN ({placeholders})")
                values.extend(kind.value for kind in kinds)
            row = connection.execute(
                f"SELECT * FROM jobs WHERE {' AND '.join(conditions)} "
                "ORDER BY created_at ASC, job_id ASC LIMIT 1",
                values,
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            lease_until = _lease_expiry(now, lease_ttl_sec)
            connection.execute(
                """
                UPDATE jobs
                SET status='running', attempt=attempt+1, lease_until=?, worker_id=?,
                    started_at=COALESCE(started_at, ?), updated_at=?, error_code=NULL,
                    stderr=NULL
                WHERE job_id=? AND status='pending'
                """,
                (lease_until, worker_id, now, now, row["job_id"]),
            )
            connection.commit()
            return self._select_job(connection, str(row["job_id"]))
        finally:
            connection.close()

    def heartbeat(self, job_id: str, worker_id: str, lease_until: str) -> bool:
        connection = self._connect()
        try:
            updated = connection.execute(
                """
                UPDATE jobs SET lease_until=?, updated_at=?
                WHERE job_id=? AND status='running' AND worker_id=?
                """,
                (lease_until, utc_iso(), job_id, worker_id),
            ).rowcount
            connection.commit()
            return updated == 1
        finally:
            connection.close()

    def recover_expired(self, *, now: str) -> builtin_list[str]:
        connection = self._connect()
        recovered: list[str] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job_id, external_effect_class FROM jobs
                WHERE status='running' AND lease_until IS NOT NULL AND lease_until < ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                effect = str(row["external_effect_class"])
                unsafe = effect in {
                    ExternalEffectClass.CLOUD_WRITE.value,
                    ExternalEffectClass.EXCHANGE_ORDER.value,
                    ExternalEffectClass.EMERGENCY_CONTROL.value,
                }
                if unsafe:
                    connection.execute(
                        """
                        UPDATE jobs SET status='failed', gate_state='BLOCKED',
                          error_code='external_effect_uncertain', finished_at=?,
                          updated_at=?, lease_until=NULL, worker_id=NULL
                        WHERE job_id=? AND status='running'
                        """,
                        (now, now, job_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE jobs SET status='pending', gate_state='READY',
                          error_code='lease_expired', next_attempt_at=?,
                          updated_at=?, lease_until=NULL, worker_id=NULL
                        WHERE job_id=? AND status='running'
                        """,
                        (now, now, job_id),
                    )
                recovered.append(job_id)
            connection.commit()
            return recovered
        finally:
            connection.close()

    def append_event(self, job_id: str, event: Any, event_type: str | None = None) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM job_events WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            event_payload = redact_json(event)
            event_name = event_type or (
                str(event_payload.get("event"))
                if isinstance(event_payload, dict) and event_payload.get("event")
                else "unknown"
            )
            connection.execute(
                """
                INSERT INTO job_events(job_id, sequence, event_type, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    int(current) + 1,
                    event_name,
                    json.dumps(event_payload, ensure_ascii=False, sort_keys=True, default=str),
                    utc_iso(),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def update_conversation_id(self, job_id: str, conversation_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE jobs SET conversation_id=?, updated_at=? WHERE job_id=?",
                (conversation_id, utc_iso(), job_id),
            )
            connection.commit()
        finally:
            connection.close()

    def mark_success(
        self,
        job_id: str,
        *,
        result: dict[str, Any],
        result_path: str,
        conversation_id: str | None,
        head_sha: str | None,
        gate_state: GateState = GateState.VERIFIED,
    ) -> None:
        connection = self._connect()
        serialized = _json_dumps(result) or "{}"
        now = utc_iso()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT result_json FROM job_results WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if existing and existing[0] != serialized:
                raise QueueConflictError(f"Result for {job_id} already differs")
            connection.execute(
                """
                INSERT INTO job_results(job_id, result_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET result_json=excluded.result_json
                """,
                (job_id, serialized, now),
            )
            connection.execute(
                """
                UPDATE jobs SET status='succeeded', gate_state=?, result_path=?,
                  result_json=?, conversation_id=COALESCE(?, conversation_id),
                  head_sha=COALESCE(?, head_sha), finished_at=?, updated_at=?,
                  lease_until=NULL, worker_id=NULL, error_code=NULL, stderr=NULL
                WHERE job_id=? AND status IN ('running','succeeded')
                """,
                (
                    gate_state.value,
                    result_path,
                    serialized,
                    conversation_id,
                    head_sha,
                    now,
                    now,
                    job_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_failure(
        self,
        job_id: str,
        *,
        error_code: str,
        stderr: str | None,
        result: dict[str, Any] | None = None,
        head_sha: str | None = None,
        gate_state: GateState = GateState.BLOCKED,
    ) -> None:
        connection = self._connect()
        now = utc_iso()
        serialized = _json_dumps(result)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if serialized is not None:
                connection.execute(
                    """
                    INSERT INTO job_results(job_id, result_json, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET result_json=excluded.result_json
                    """,
                    (job_id, serialized, now),
                )
            connection.execute(
                """
                UPDATE jobs SET status='failed', gate_state=?, error_code=?, stderr=?,
                  result_json=COALESCE(?, result_json), head_sha=COALESCE(?, head_sha),
                  finished_at=?, updated_at=?, lease_until=NULL, worker_id=NULL
                WHERE job_id=? AND status IN ('running','failed')
                """,
                (gate_state.value, error_code, stderr, serialized, head_sha, now, now, job_id),
            )
            connection.commit()
        finally:
            connection.close()

    def schedule_retry(
        self,
        job_id: str,
        *,
        next_attempt_at: str,
        error_code: str,
        stderr: str | None,
    ) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT attempt, max_attempts, external_effect_class, status
                FROM jobs WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            unsafe = str(row["external_effect_class"]) in {
                ExternalEffectClass.CLOUD_WRITE.value,
                ExternalEffectClass.EXCHANGE_ORDER.value,
                ExternalEffectClass.EMERGENCY_CONTROL.value,
            }
            if (
                str(row["status"]) != JobStatus.RUNNING.value
                or unsafe
                or int(row["attempt"]) >= int(row["max_attempts"])
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE jobs SET status='pending', gate_state='READY',
                  next_attempt_at=?, error_code=?, stderr=?, updated_at=?,
                  lease_until=NULL, worker_id=NULL
                WHERE job_id=? AND status='running'
                """,
                (next_attempt_at, error_code, stderr, utc_iso(), job_id),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    def retry_failed(self, job_id: str, *, now: str) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT attempt, max_attempts, external_effect_class, status
                FROM jobs WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            unsafe = str(row["external_effect_class"]) in {
                ExternalEffectClass.CLOUD_WRITE.value,
                ExternalEffectClass.EXCHANGE_ORDER.value,
                ExternalEffectClass.EMERGENCY_CONTROL.value,
            }
            if (
                str(row["status"]) != JobStatus.FAILED.value
                or unsafe
                or int(row["attempt"]) >= int(row["max_attempts"])
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE jobs SET status='pending', gate_state='READY',
                  next_attempt_at=?, error_code='manual_retry', stderr=NULL,
                  finished_at=NULL, updated_at=?
                WHERE job_id=? AND status='failed'
                """,
                (now, now, job_id),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    def cancel(self, job_id: str) -> bool:
        connection = self._connect()
        try:
            updated = connection.execute(
                """
                UPDATE jobs SET status='cancelled', gate_state='BLOCKED',
                  finished_at=?, updated_at=?, lease_until=NULL, worker_id=NULL
                WHERE job_id=? AND status IN ('pending','running')
                """,
                (utc_iso(), utc_iso(), job_id),
            ).rowcount
            connection.commit()
            return updated == 1
        finally:
            connection.close()


class JsonlQueueStore:
    """Append-only JSONL fallback with an explicit single-writer lock."""

    def __init__(self, root: str | Path):
        self.paths = QueuePaths(Path(root).resolve())
        self.paths.ensure()
        self._thread_lock = threading.RLock()

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        deadline = time.monotonic() + 5.0
        lock_handle: int | None = None
        while lock_handle is None:
            try:
                lock_handle = os.open(
                    self.paths.lock,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                os.write(lock_handle, str(os.getpid()).encode("ascii"))
            except FileExistsError:
                try:
                    if time.time() - self.paths.lock.stat().st_mtime > 600:
                        self.paths.lock.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise QueueStorageError("JSONL queue writer lock is busy")
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(lock_handle)
            try:
                self.paths.lock.unlink()
            except FileNotFoundError:
                pass

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        line = json.dumps(redact_json(payload), ensure_ascii=False, sort_keys=True, default=str)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_jobs(self) -> dict[str, JobRecord]:
        jobs: dict[str, JobRecord] = {}
        sources = [self.paths.inbox, self.paths.event_log]
        for source in sources:
            if not source.exists():
                continue
            with source.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        raise QueueStorageError(f"Malformed JSONL record in {source}")
                    if record.get("record_type") in {"job", "job_update"}:
                        job = _payload_to_job(record["job"])
                        jobs[job.job_id] = job
        return jobs

    def _append_update(self, job: JobRecord) -> None:
        self._append(self.paths.event_log, {"record_type": "job_update", "job": _job_payload(job)})

    def _mutate(self, job_id: str, operation: Any) -> JobRecord | None:
        with self._thread_lock, self._file_lock():
            jobs = self._read_jobs()
            job = jobs.get(job_id)
            if job is None:
                return None
            operation(job)
            job.updated_at = utc_iso()
            self._append_update(job)
            return job

    def close(self) -> None:
        return None

    def create_job(self, job: JobRecord) -> JobRecord:
        with self._thread_lock, self._file_lock():
            if job.job_id in self._read_jobs():
                raise QueueConflictError(f"Job {job.job_id} already exists")
            self._append(self.paths.inbox, {"record_type": "job", "job": _job_payload(job)})
            return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._thread_lock:
            return self._read_jobs().get(job_id)

    def list(
        self,
        *,
        status: JobStatus | None = None,
        job_kind: JobKind | None = None,
        limit: int = 100,
    ) -> builtin_list[JobRecord]:
        jobs = list(self._read_jobs().values())
        if status is not None:
            jobs = [job for job in jobs if job.status == status]
        if job_kind is not None:
            jobs = [job for job in jobs if job.job_kind == job_kind]
        jobs.sort(key=lambda job: (job.created_at, job.job_id), reverse=True)
        return jobs[: max(1, min(int(limit), 1000))]

    def claim_next(
        self,
        worker_id: str,
        *,
        now: str,
        lease_ttl_sec: int,
        eligible_kinds: Collection[JobKind] | None = None,
    ) -> JobRecord | None:
        with self._thread_lock, self._file_lock():
            jobs = self._read_jobs()
            eligible = set(eligible_kinds) if eligible_kinds is not None else None
            running_scopes = {
                job.scope_key
                for job in jobs.values()
                if job.status == JobStatus.RUNNING and job.scope_key is not None
            }
            candidates = [
                job
                for job in jobs.values()
                if job.status == JobStatus.PENDING
                and job.gate_state == GateState.READY
                and (job.next_attempt_at is None or job.next_attempt_at <= now)
                and (eligible is None or job.job_kind in eligible)
                and (job.scope_key is None or job.scope_key not in running_scopes)
            ]
            if not candidates:
                return None
            job = min(candidates, key=lambda item: (item.created_at, item.job_id))
            job.status = JobStatus.RUNNING
            job.attempt += 1
            job.lease_until = _lease_expiry(now, lease_ttl_sec)
            job.worker_id = worker_id
            job.started_at = job.started_at or now
            job.updated_at = now
            job.error_code = None
            job.stderr = None
            self._append_update(job)
            return job

    def heartbeat(self, job_id: str, worker_id: str, lease_until: str) -> bool:
        result = self._mutate(
            job_id,
            lambda job: setattr(job, "lease_until", lease_until)
            if job.status == JobStatus.RUNNING and job.worker_id == worker_id
            else None,
        )
        return bool(result and result.status == JobStatus.RUNNING and result.worker_id == worker_id)

    def recover_expired(self, *, now: str) -> builtin_list[str]:
        recovered: list[str] = []
        with self._thread_lock, self._file_lock():
            jobs = self._read_jobs()
            for job in jobs.values():
                if job.status != JobStatus.RUNNING or not job.lease_until or job.lease_until >= now:
                    continue
                recovered.append(job.job_id)
                unsafe = job.external_effect_class in {
                    ExternalEffectClass.CLOUD_WRITE,
                    ExternalEffectClass.EXCHANGE_ORDER,
                    ExternalEffectClass.EMERGENCY_CONTROL,
                }
                if unsafe:
                    job.status = JobStatus.FAILED
                    job.gate_state = GateState.BLOCKED
                    job.error_code = "external_effect_uncertain"
                    job.finished_at = now
                else:
                    job.status = JobStatus.PENDING
                    job.gate_state = GateState.READY
                    job.error_code = "lease_expired"
                    job.next_attempt_at = now
                job.lease_until = None
                job.worker_id = None
                job.updated_at = now
                self._append_update(job)
        return recovered

    def append_event(self, job_id: str, event: Any, event_type: str | None = None) -> None:
        event_payload = redact_json(event)
        with self._thread_lock, self._file_lock():
            self._append(
                self.paths.event_log,
                {
                    "record_type": "event",
                    "job_id": job_id,
                    "event_type": event_type or "unknown",
                    "event": event_payload,
                    "created_at": utc_iso(),
                },
            )

    def update_conversation_id(self, job_id: str, conversation_id: str) -> None:
        self._mutate(job_id, lambda job: setattr(job, "conversation_id", conversation_id))

    def mark_success(
        self,
        job_id: str,
        *,
        result: dict[str, Any],
        result_path: str,
        conversation_id: str | None,
        head_sha: str | None,
        gate_state: GateState = GateState.VERIFIED,
    ) -> None:
        def update(job: JobRecord) -> None:
            if job.result_json is not None and _json_dumps(job.result_json) != _json_dumps(result):
                raise QueueConflictError(f"Result for {job_id} already differs")
            job.status = JobStatus.SUCCEEDED
            job.gate_state = gate_state
            job.result_json = redact_json(result)
            job.result_path = result_path
            job.conversation_id = conversation_id or job.conversation_id
            job.head_sha = head_sha or job.head_sha
            job.finished_at = utc_iso()
            job.lease_until = None
            job.worker_id = None
            job.error_code = None
            job.stderr = None

        self._mutate(job_id, update)

    def mark_failure(
        self,
        job_id: str,
        *,
        error_code: str,
        stderr: str | None,
        result: dict[str, Any] | None = None,
        head_sha: str | None = None,
        gate_state: GateState = GateState.BLOCKED,
    ) -> None:
        def update(job: JobRecord) -> None:
            job.status = JobStatus.FAILED
            job.gate_state = gate_state
            job.error_code = error_code
            job.stderr = stderr
            job.result_json = redact_json(result) if result is not None else job.result_json
            job.head_sha = head_sha or job.head_sha
            job.finished_at = utc_iso()
            job.lease_until = None
            job.worker_id = None

        self._mutate(job_id, update)

    def schedule_retry(
        self,
        job_id: str,
        *,
        next_attempt_at: str,
        error_code: str,
        stderr: str | None,
    ) -> bool:
        changed = False

        def update(job: JobRecord) -> None:
            nonlocal changed
            if (
                job.status != JobStatus.RUNNING
                or job.attempt >= job.max_attempts
                or job.external_effect_class
                in {
                    ExternalEffectClass.CLOUD_WRITE,
                    ExternalEffectClass.EXCHANGE_ORDER,
                    ExternalEffectClass.EMERGENCY_CONTROL,
                }
            ):
                return
            job.status = JobStatus.PENDING
            job.gate_state = GateState.READY
            job.next_attempt_at = next_attempt_at
            job.error_code = error_code
            job.stderr = stderr
            job.lease_until = None
            job.worker_id = None
            changed = True

        self._mutate(job_id, update)
        return changed

    def retry_failed(self, job_id: str, *, now: str) -> bool:
        changed = False

        def update(job: JobRecord) -> None:
            nonlocal changed
            if (
                job.status != JobStatus.FAILED
                or job.attempt >= job.max_attempts
                or job.external_effect_class
                in {
                    ExternalEffectClass.CLOUD_WRITE,
                    ExternalEffectClass.EXCHANGE_ORDER,
                    ExternalEffectClass.EMERGENCY_CONTROL,
                }
            ):
                return
            job.status = JobStatus.PENDING
            job.gate_state = GateState.READY
            job.next_attempt_at = now
            job.error_code = "manual_retry"
            job.stderr = None
            job.finished_at = None
            changed = True

        self._mutate(job_id, update)
        return changed

    def cancel(self, job_id: str) -> bool:
        changed = False

        def update(job: JobRecord) -> None:
            nonlocal changed
            if job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
                job.status = JobStatus.CANCELLED
                job.gate_state = GateState.BLOCKED
                job.finished_at = utc_iso()
                job.lease_until = None
                job.worker_id = None
                changed = True

        self._mutate(job_id, update)
        return changed
