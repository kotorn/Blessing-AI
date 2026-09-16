"""Queue worker that drives AGY while keeping external side effects gated."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .agy import (
    AgyCliInspection,
    AgyError,
    AgySession,
    AgyTerminalResult,
    inspect_cli,
)
from .models import AGY_JOB_KINDS, GateState, JobKind, JobRecord, JobStatus, utc_iso
from .repository import RepositoryError, diff_evidence, verify_snapshot
from .security import redact_text
from .service import QueueService


class WorkerError(RuntimeError):
    """Base worker error."""

    code = "worker_error"
    retryable = False
    uncertain = False


class LeaseLostError(WorkerError):
    code = "lease_lost"
    uncertain = True


class VerificationError(WorkerError):
    code = "verification_failed"


RepoVerifier = Callable[[JobRecord, AgyTerminalResult], dict[str, Any]]


def default_repo_verifier(job: JobRecord, result: AgyTerminalResult) -> dict[str, Any]:
    evidence = diff_evidence(job.repo)
    structured = result.structured_output
    tests_passed = False
    if isinstance(structured, dict):
        tests_passed = structured.get("tests_passed") is True
        verification = structured.get("verification")
        if isinstance(verification, dict):
            tests_passed = tests_passed or verification.get("tests_passed") is True
    evidence["tests_passed"] = tests_passed
    if not evidence["diff_check_passed"]:
        raise VerificationError("git diff --check failed")
    if not str(evidence.get("status_porcelain") or "").strip():
        raise VerificationError("repository-change result contains no repository diff")
    if not tests_passed:
        raise VerificationError("repository-change result lacks independent tests_passed evidence")
    return evidence


def _validate_structured_output(job: JobRecord, result: AgyTerminalResult) -> None:
    schema = job.expected_output_schema
    if not schema:
        return
    value = result.structured_output
    if value is None:
        raise VerificationError("expected structured output was not returned")
    required = schema.get("required", [])
    if isinstance(required, list) and isinstance(value, dict):
        missing = [str(name) for name in required if str(name) not in value]
        if missing:
            raise VerificationError(f"structured output is missing required fields: {','.join(missing)}")


class QueueWorker:
    """Single-writer local worker with bounded AGY sessions."""

    def __init__(
        self,
        service: QueueService,
        *,
        worker_id: str | None = None,
        inspection: AgyCliInspection | None = None,
        session_factory: Callable[[JobRecord, AgyCliInspection, Callable[[JobRecord, dict[str, Any]], None]], AgySession]
        | None = None,
        repo_verifier: RepoVerifier = default_repo_verifier,
        random_source: random.Random | None = None,
    ):
        self.service = service
        self.config = service.config
        self.worker_id = worker_id or f"agy-worker-{uuid4().hex[:12]}"
        self._inspection = inspection
        self._session_factory = session_factory or self._default_session_factory
        self._repo_verifier = repo_verifier
        self._random: random.Random = random_source or random.Random()
        self._session: AgySession | None = None
        self._session_job: JobRecord | None = None
        self._stop = threading.Event()

    def _default_session_factory(
        self,
        job: JobRecord,
        inspection: AgyCliInspection,
        on_event: Callable[[JobRecord, dict[str, Any]], None],
    ) -> AgySession:
        return AgySession(
            executable=self.config.agy_exe,
            inspection=inspection,
            print_timeout=self.config.print_timeout,
            startup_timeout_sec=self.config.startup_timeout_sec,
            on_event=on_event,
        )

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
            self._session_job = None

    def _inspection_for(self, job: JobRecord) -> AgyCliInspection:
        if self._inspection is None:
            self._inspection = inspect_cli(
                self.config.agy_exe,
                cwd=job.repo,
                expected_version=self.config.expected_agy_version,
                timeout_sec=self.config.startup_timeout_sec,
            )
        return self._inspection

    def _record_event(self, job: JobRecord, event: dict[str, Any]) -> None:
        event_type = str(event.get("event") or "unknown")
        self.service.store.append_event(job.job_id, event, event_type)
        self.service.artifacts.append_event(job.job_id, event)
        if event_type == "init":
            init = event.get("init")
            if isinstance(init, dict):
                conversation_id = init.get("conversation_id") or event.get("conversation_id")
                if conversation_id:
                    self.service.store.update_conversation_id(job.job_id, str(conversation_id))

    def _get_session(self, job: JobRecord) -> AgySession:
        now = time.monotonic()
        if self._session is not None and self._session.compatible(
            job,
            now,
            idle_timeout_sec=self.config.session_idle_timeout_sec,
            max_age_sec=self.config.session_max_age_sec,
            max_jobs=self.config.session_max_jobs,
        ):
            return self._session
        self.close()
        inspection = self._inspection_for(job)
        self._session = self._session_factory(job, inspection, self._record_event)
        self._session_job = job
        return self._session

    def _heartbeat(self, job: JobRecord) -> None:
        lease_until = utc_iso(
            datetime.now(UTC) + timedelta(seconds=self.config.lease_ttl_sec)
        )
        if not self.service.store.heartbeat(job.job_id, self.worker_id, lease_until):
            raise LeaseLostError(f"Lease lost for job {job.job_id}")

    def _retry_delay(self, attempt: int) -> float:
        base: int = min(
            self.config.retry_cap_sec,
            self.config.retry_base_sec * (2 ** max(0, attempt - 1)),
        )
        jitter: float = float(self._random.uniform(0, 5))
        return float(min(float(self.config.retry_cap_sec), base + jitter))

    @staticmethod
    def _result_envelope(job: JobRecord, result: AgyTerminalResult, evidence: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "status": "succeeded",
            "attempt": job.attempt,
            "response": result.response,
            "structured_output": result.structured_output,
            "model": job.model,
            "effort": job.effort,
            "repo": job.repo,
            "base_sha": job.base_sha,
            "finished_at": utc_iso(),
            "conversation_id": result.conversation_id,
            "usage": result.usage,
            "stderr": result.stderr,
            "verification": evidence,
            "error": None,
        }

    def _failure_envelope(self, job: JobRecord, error: BaseException) -> dict[str, Any]:
        code = getattr(error, "code", "worker_error")
        return {
            "job_id": job.job_id,
            "status": "failed",
            "attempt": job.attempt,
            "response": None,
            "model": job.model,
            "effort": job.effort,
            "repo": job.repo,
            "base_sha": job.base_sha,
            "finished_at": utc_iso(),
            "usage": {},
            "error": {"code": code, "message": redact_text(str(error))},
        }

    def _handle_error(self, job: JobRecord, error: BaseException) -> None:
        retryable = bool(getattr(error, "retryable", False))
        uncertain = bool(getattr(error, "uncertain", False))
        if job.job_kind == JobKind.REPO_CHANGE and uncertain:
            retryable = False
        if retryable and job.attempt < job.max_attempts:
            delay = self._retry_delay(job.attempt)
            next_time = datetime.now(UTC) + timedelta(seconds=delay)
            error_code = str(getattr(error, "code", "worker_retryable_error"))
            self.service.store.append_event(
                job.job_id,
                {"event": "queue_retry", "error_code": error_code, "delay_sec": delay},
                "queue_retry",
            )
            self.service.artifacts.append_event(
                job.job_id,
                {"event": "queue_retry", "error_code": error_code, "delay_sec": delay},
            )
            if self.service.store.schedule_retry(
                job.job_id,
                next_attempt_at=utc_iso(next_time),
                error_code=error_code,
                stderr=redact_text(str(error)),
            ):
                # A retry always starts a fresh process after capacity or
                # timeout uncertainty; never reuse a possibly half-finished
                # stream for the next attempt.
                self.close()
                return
        try:
            current = self.service.get(job.job_id)
            head_sha = None
            try:
                head_sha = verify_snapshot(
                    job.repo,
                    expected_branch=job.branch,
                    expected_sha=job.base_sha,
                    require_writable_branch=False,
                ).sha
            except RepositoryError:
                pass
            envelope = self._failure_envelope(job, error)
            result_path = self.service.artifacts.write_result(job.job_id, envelope)
            self.service.store.mark_failure(
                job.job_id,
                error_code=str(getattr(error, "code", "worker_error")),
                stderr=redact_text(str(error)),
                result=envelope,
                head_sha=head_sha,
                gate_state=GateState.BLOCKED,
            )
            if current.status == JobStatus.CANCELLED:
                return
            _ = result_path
        finally:
            self.close()

    def run_once(self) -> JobRecord | None:
        self.service.store.recover_expired(now=utc_iso())
        job = self.service.store.claim_next(
            self.worker_id,
            now=utc_iso(),
            lease_ttl_sec=self.config.lease_ttl_sec,
            eligible_kinds=AGY_JOB_KINDS,
        )
        if job is None:
            return None
        try:
            verify_snapshot(
                job.repo,
                expected_branch=job.branch,
                expected_sha=job.base_sha,
                require_writable_branch=job.job_kind == JobKind.REPO_CHANGE,
            )
            session = self._get_session(job)
            terminal = session.send(
                job,
                on_heartbeat=lambda: self._heartbeat(job),
                heartbeat_interval_sec=self.config.heartbeat_interval_sec,
            )
            _validate_structured_output(job, terminal)
            evidence = None
            if job.job_kind == JobKind.REPO_CHANGE:
                evidence = self._repo_verifier(job, terminal)
            envelope = self._result_envelope(job, terminal, evidence)
            result_path = self.service.artifacts.write_result(job.job_id, envelope)
            self.service.store.mark_success(
                job.job_id,
                result=envelope,
                result_path=result_path,
                conversation_id=terminal.conversation_id,
                head_sha=evidence.get("head_sha") if evidence else None,
                gate_state=GateState.VERIFIED,
            )
            return self.service.get(job.job_id)
        except (AgyError, WorkerError, RepositoryError, VerificationError) as error:
            self._handle_error(job, error)
            return self.service.get(job.job_id)
        except Exception as error:  # noqa: BLE001 - final fail-closed guard
            self._handle_error(job, WorkerError(str(error)))
            return self.service.get(job.job_id)

    def run(self, *, wait: bool = False) -> None:
        try:
            while not self._stop.is_set():
                outcome = self.run_once()
                if outcome is None:
                    if not wait:
                        return
                    self._stop.wait(self.config.poll_interval_sec)
        finally:
            self.close()
