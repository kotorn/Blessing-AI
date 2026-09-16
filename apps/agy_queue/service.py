"""Queue service: validation, submission, retrieval, and bounded retry."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import asdict
from typing import Any

from .agy import model_effort_suffix
from .config import QueueConfig
from .models import (
    JOB_EFFECT_CLASS,
    GateState,
    JobKind,
    JobRecord,
    JobRequest,
    JobStatus,
    new_job_id,
    utc_iso,
)
from .repository import snapshot_repository
from .security import CredentialLikeContentError, contains_credential_like_content, validate_prompt
from .storage import JsonlQueueStore, QueueStore, ResultArtifactStore, SQLiteQueueStore


class QueuePolicyError(ValueError):
    """Raised when a job violates the queue's safety boundary."""


def create_store(config: QueueConfig) -> QueueStore:
    config.paths.ensure()
    if config.backend == "sqlite":
        return SQLiteQueueStore(config.database_path, paths=config.paths)
    if config.backend == "jsonl":
        return JsonlQueueStore(config.root)
    raise QueuePolicyError(f"Unsupported queue backend: {config.backend}")


def job_to_dict(job: JobRecord) -> dict[str, Any]:
    payload = asdict(job)
    for key in ("status", "job_kind", "external_effect_class", "gate_state"):
        value = payload[key]
        payload[key] = value.value if hasattr(value, "value") else value
    return payload


class QueueService:
    def __init__(
        self,
        config: QueueConfig,
        *,
        store: QueueStore | None = None,
        artifacts: ResultArtifactStore | None = None,
    ):
        self.config = config
        self.store = store or create_store(config)
        self.artifacts = artifacts or ResultArtifactStore(config.paths)

    def close(self) -> None:
        self.store.close()

    @staticmethod
    def _validate_reference(value: str | None, label: str) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 512 or "\n" in normalized or "\r" in normalized:
            raise QueuePolicyError(f"{label} must be a short single-line reference")
        if contains_credential_like_content(normalized):
            raise CredentialLikeContentError(f"{label} contains credential-like content")
        return normalized

    def submit(self, request: JobRequest) -> JobRecord:
        prompt = validate_prompt(request.prompt)
        kind = request.normalized_kind()
        if kind in {JobKind.TRADING_ORDER, JobKind.EMERGENCY_KILL_SWITCH}:
            raise QueuePolicyError(
                f"{kind.value} is outside AGY queue authority; use the deterministic control path"
            )
        model = request.model.strip()
        if not model or len(model) > 160:
            raise QueuePolicyError("model must be a short non-empty value")

        # A model name ending in -low/-medium/-high already bakes in its
        # effort tier and AGY rejects a --effort flag that disagrees with it
        # (verified against the installed release). Derive silently when the
        # caller didn't ask for a specific tier; reject only a real conflict.
        suffix = model_effort_suffix(model)
        if request.effort is None:
            effort = suffix or self.config.default_effort
        elif suffix is not None and request.effort != suffix:
            raise QueuePolicyError(
                f"model {model!r} encodes effort {suffix!r} but "
                f"effort={request.effort!r} was explicitly requested"
            )
        else:
            effort = request.effort
        if not effort.strip() or len(effort) > 40:
            raise QueuePolicyError("effort must be a short non-empty value")

        if request.max_attempts < 1 or request.max_attempts > min(3, self.config.max_attempts):
            raise QueuePolicyError("max_attempts must be between 1 and the configured limit of 3")
        if request.timeout_sec < 1 or request.timeout_sec > 3600:
            raise QueuePolicyError("timeout_sec must be between 1 and 3600")
        if request.expected_output_schema is not None and not isinstance(
            request.expected_output_schema, dict
        ):
            raise QueuePolicyError("expected_output_schema must be a JSON object")
        if kind == JobKind.REPO_CHANGE:
            required = (
                request.expected_output_schema.get("required")
                if request.expected_output_schema
                else None
            )
            if not isinstance(required, list) or "tests_passed" not in required:
                raise QueuePolicyError(
                    "REPO_CHANGE jobs must supply expected_output_schema requiring "
                    "'tests_passed', since the queue cannot verify repo-change "
                    "success without it"
                )

        snapshot = snapshot_repository(request.repo)
        if request.branch and request.branch != snapshot.branch:
            raise QueuePolicyError(
                f"branch does not match current repository: {request.branch} != {snapshot.branch}"
            )
        if request.base_sha and request.base_sha != snapshot.sha:
            raise QueuePolicyError("base_sha does not match the current repository HEAD")
        if kind == JobKind.REPO_CHANGE and snapshot.branch in {"main", "master"}:
            raise QueuePolicyError("REPO_CHANGE cannot target main/master")

        authorization_ref = self._validate_reference(request.authorization_ref, "authorization_ref")
        verification_job_id = self._validate_reference(
            request.verification_job_id, "verification_job_id"
        )
        session_key = self._validate_reference(request.session_key, "session_key")
        scope_key = self._validate_reference(request.scope_key, "scope_key") or snapshot.root

        gate_state = GateState.READY
        if kind in {
            JobKind.REPO_CHANGE,
            JobKind.CLOUD_WRITE,
            JobKind.MAINNET_PREFLIGHT,
        } and not authorization_ref:
            gate_state = GateState.WAITING_AUTHORIZATION
        elif kind == JobKind.CLOUD_WRITE and not verification_job_id:
            gate_state = GateState.WAITING_VERIFICATION

        job_id = new_job_id()
        now = utc_iso()
        job = JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            prompt=prompt,
            repo=snapshot.root,
            base_sha=snapshot.sha,
            model=model,
            effort=effort.strip(),
            timeout_sec=request.timeout_sec,
            status=JobStatus.PENDING,
            attempt=0,
            max_attempts=request.max_attempts,
            lease_until=None,
            worker_id=None,
            conversation_id=None,
            result_path=str(self.artifacts.result_path(job_id)),
            raw_events_path=str(self.artifacts.events_path(job_id)),
            error_code=None,
            stderr=None,
            started_at=None,
            finished_at=None,
            branch=snapshot.branch,
            head_sha=None,
            next_attempt_at=None,
            session_key=session_key,
            scope_key=scope_key,
            job_kind=kind,
            external_effect_class=JOB_EFFECT_CLASS[kind],
            gate_state=gate_state,
            authorization_ref=authorization_ref,
            verification_job_id=verification_job_id,
            expected_output_schema=request.expected_output_schema,
            result_json=None,
        )
        # This is the durability boundary: create_job completes before a
        # worker is allowed to deliver the prompt to AGY.
        return self.store.create_job(job)

    def get(self, job_id: str) -> JobRecord:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(f"Unknown AGY job: {job_id}")
        return job

    def list(
        self,
        *,
        status: JobStatus | None = None,
        job_kind: JobKind | None = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        return self.store.list(status=status, job_kind=job_kind, limit=limit)

    def retry(self, job_id: str) -> JobRecord:
        if not self.store.retry_failed(job_id, now=utc_iso()):
            job = self.get(job_id)
            raise QueuePolicyError(
                f"Job {job_id} is not eligible for bounded retry "
                f"(status={job.status.value}, attempt={job.attempt}/{job.max_attempts})"
            )
        return self.get(job_id)

    def watch(
        self,
        job_id: str,
        *,
        poll_interval_sec: float | None = None,
        timeout_sec: float | None = None,
    ) -> Iterator[JobRecord]:
        interval = poll_interval_sec or self.config.poll_interval_sec
        started = time.monotonic()
        previous: tuple[Any, ...] | None = None
        while True:
            job = self.get(job_id)
            signature = (
                job.updated_at,
                job.status,
                job.gate_state,
                job.attempt,
                job.error_code,
                job.result_path,
            )
            if signature != previous:
                previous = signature
                yield job
            if job.is_terminal:
                return
            if timeout_sec is not None and time.monotonic() - started >= timeout_sec:
                return
            time.sleep(max(0.05, interval))
