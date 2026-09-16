"""Public queue records and policy enums."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GateState(StrEnum):
    READY = "READY"
    WAITING_AUTHORIZATION = "WAITING_AUTHORIZATION"
    WAITING_VERIFICATION = "WAITING_VERIFICATION"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"


class JobKind(StrEnum):
    READ_ONLY = "READ_ONLY"
    REPO_CHANGE = "REPO_CHANGE"
    RELEASE_GATE_EVALUATE = "RELEASE_GATE_EVALUATE"
    MAINNET_PREFLIGHT = "MAINNET_PREFLIGHT"
    VERIFY_EXTERNAL = "VERIFY_EXTERNAL"
    CLOUD_WRITE = "CLOUD_WRITE"
    TRADING_ORDER = "TRADING_ORDER"
    EMERGENCY_KILL_SWITCH = "EMERGENCY_KILL_SWITCH"


class ExternalEffectClass(StrEnum):
    NONE = "NONE"
    REPO_CHANGE = "REPO_CHANGE"
    CLOUD_WRITE = "CLOUD_WRITE"
    EXCHANGE_ORDER = "EXCHANGE_ORDER"
    EMERGENCY_CONTROL = "EMERGENCY_CONTROL"


JOB_EFFECT_CLASS: dict[JobKind, ExternalEffectClass] = {
    JobKind.READ_ONLY: ExternalEffectClass.NONE,
    JobKind.RELEASE_GATE_EVALUATE: ExternalEffectClass.NONE,
    JobKind.REPO_CHANGE: ExternalEffectClass.REPO_CHANGE,
    JobKind.MAINNET_PREFLIGHT: ExternalEffectClass.NONE,
    JobKind.VERIFY_EXTERNAL: ExternalEffectClass.NONE,
    JobKind.CLOUD_WRITE: ExternalEffectClass.CLOUD_WRITE,
    JobKind.TRADING_ORDER: ExternalEffectClass.EXCHANGE_ORDER,
    JobKind.EMERGENCY_KILL_SWITCH: ExternalEffectClass.EMERGENCY_CONTROL,
}

AGY_JOB_KINDS = frozenset(
    {
        JobKind.READ_ONLY,
        JobKind.REPO_CHANGE,
        JobKind.RELEASE_GATE_EVALUATE,
    }
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    """Serialize an aware UTC timestamp in a stable form."""

    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def new_job_id() -> str:
    return f"agy-{utc_now():%Y%m%d-%H%M%S}-{uuid4().hex[:12]}"


def normalize_job_kind(value: JobKind | str) -> JobKind:
    if isinstance(value, JobKind):
        return value
    normalized = str(value).strip().replace("-", "_").upper()
    try:
        return JobKind(normalized)
    except ValueError as exc:
        raise ValueError(f"Unsupported AGY queue job kind: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class JobRequest:
    prompt: str
    repo: str
    kind: JobKind | str = JobKind.READ_ONLY
    model: str = "gemini-3.8-flash-medium"
    effort: str | None = None
    session_key: str | None = None
    max_attempts: int = 3
    timeout_sec: int = 300
    expected_output_schema: Mapping[str, Any] | None = None
    authorization_ref: str | None = None
    verification_job_id: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    scope_key: str | None = None

    def normalized_kind(self) -> JobKind:
        return normalize_job_kind(self.kind)


@dataclass(slots=True)
class JobRecord:
    job_id: str
    created_at: str
    updated_at: str
    prompt: str
    repo: str
    base_sha: str | None
    model: str
    effort: str
    timeout_sec: int
    status: JobStatus
    attempt: int
    max_attempts: int
    lease_until: str | None
    worker_id: str | None
    conversation_id: str | None
    result_path: str | None
    raw_events_path: str | None
    error_code: str | None
    stderr: str | None
    started_at: str | None
    finished_at: str | None
    branch: str | None = None
    head_sha: str | None = None
    next_attempt_at: str | None = None
    session_key: str | None = None
    scope_key: str | None = None
    job_kind: JobKind = JobKind.READ_ONLY
    external_effect_class: ExternalEffectClass = ExternalEffectClass.NONE
    gate_state: GateState = GateState.READY
    authorization_ref: str | None = None
    verification_job_id: str | None = None
    expected_output_schema: Mapping[str, Any] | None = None
    result_json: Mapping[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }

    @property
    def can_retry(self) -> bool:
        return (
            self.status == JobStatus.FAILED
            and self.attempt < self.max_attempts
            and self.external_effect_class
            not in {
                ExternalEffectClass.CLOUD_WRITE,
                ExternalEffectClass.EXCHANGE_ORDER,
                ExternalEffectClass.EMERGENCY_CONTROL,
            }
        )
