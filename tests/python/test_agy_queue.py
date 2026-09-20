from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from apps.agy_queue.agy import (
    AgyCapacityError,
    AgyCliInspection,
    AgyPermissionError,
    AgyProcessExitError,
    AgyProtocolError,
    AgyResultError,
    AgySession,
    AgyTerminalResult,
    build_argv,
    model_accepts_effort_flag,
    model_effort_suffix,
)
from apps.agy_queue.cli import main as queue_cli_main
from apps.agy_queue.config import QueueConfig
from apps.agy_queue.models import (
    GateState,
    JobKind,
    JobRequest,
    JobStatus,
    utc_iso,
)
from apps.agy_queue.security import CredentialLikeContentError
from apps.agy_queue.service import QueuePolicyError, QueueService
from apps.agy_queue.storage import (
    QueuePaths,
    QueueStorageError,
    ResultArtifactStore,
    SQLiteQueueStore,
)
from apps.agy_queue.worker import QueueWorker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def make_worker_repo(tmp_path: Path, *, branch: str = "feature/queue-change") -> Path:
    """Create a throwaway git repo on a writable branch for REPO_CHANGE tests.

    REPO_CHANGE jobs are rejected while the target checkout sits on
    main/master (a fail-closed guard in QueueService), so these tests must
    not point at the real checkout: its branch name differs between local
    runs and CI (PR merge refs vs ``main``), which made the suite red only
    on pushes to ``main``.
    """
    repo_dir = tmp_path / "worker-repo"
    repo_dir.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            check=True,
            capture_output=True,
        )

    git("init", "-b", branch)
    git("config", "user.email", "agy-queue-test@example.invalid")
    git("config", "user.name", "AGY Queue Tests")
    (repo_dir / "README.md").write_text(
        "throwaway repo for queue tests\n", encoding="utf-8"
    )
    git("add", ".")
    git("commit", "-m", "initial commit")
    return repo_dir


def make_service(tmp_path: Path, *, backend: str = "sqlite") -> QueueService:
    config = QueueConfig(
        root=tmp_path / "queue",
        backend=backend,
        agy_exe=Path(sys.executable),
        max_attempts=3,
        job_timeout_sec=30,
        lease_ttl_sec=30,
        heartbeat_interval_sec=5,
        poll_interval_sec=1,
        startup_timeout_sec=5,
    )
    return QueueService(config)


def submit_read_only(service: QueueService, *, session_key: str | None = None):
    return service.submit(
        JobRequest(
            prompt="Inspect the repository and return a short result.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.READ_ONLY,
            model="test-model",
            effort="medium",
            session_key=session_key,
        )
    )


def test_cli_submit_reads_prompt_file_and_returns_durable_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Inspect only; do not modify files.", encoding="utf-8")
    monkeypatch.setenv("AGY_QUEUE_ROOT", str(tmp_path / "queue"))
    exit_code = queue_cli_main(
        [
            "submit",
            "--prompt-file",
            str(prompt_file),
            "--kind",
            "read_only",
            "--repo",
            str(REPOSITORY_ROOT),
        ]
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert exit_code == 0
    assert output["job_id"].startswith("agy-")
    assert output["status"] == "pending"
    assert "Inspect only" not in captured.out


def test_submit_is_immediate_and_survives_worker_restart(tmp_path: Path) -> None:
    first = make_service(tmp_path)
    job = submit_read_only(first)
    assert job.status == JobStatus.PENDING
    first.close()

    second = make_service(tmp_path)
    recovered = second.get(job.job_id)
    assert recovered.status == JobStatus.PENDING
    assert recovered.prompt.startswith("Inspect the repository")
    assert recovered.base_sha
    assert recovered.result_path
    second.close()

    connection = sqlite3.connect(tmp_path / "queue" / "queue.db")
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert str(journal_mode).lower() == "wal"


def test_jsonl_backend_is_explicit_and_durable(tmp_path: Path) -> None:
    service = make_service(tmp_path, backend="jsonl")
    job = submit_read_only(service)
    service.close()

    reopened = make_service(tmp_path, backend="jsonl")
    assert reopened.get(job.job_id).job_id == job.job_id
    assert (tmp_path / "queue" / "inbox.jsonl").exists()
    reopened.close()


def test_jsonl_corruption_fails_closed(tmp_path: Path) -> None:
    service = make_service(tmp_path, backend="jsonl")
    job = submit_read_only(service)
    service.close()

    inbox = tmp_path / "queue" / "inbox.jsonl"
    with inbox.open("a", encoding="utf-8") as f:
        f.write("{this is not valid json\n")

    reopened = make_service(tmp_path, backend="jsonl")
    with pytest.raises(QueueStorageError):
        reopened.get(job.job_id)
    reopened.close()



def test_external_order_and_kill_switch_are_rejected_before_persistence(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    for kind in (JobKind.TRADING_ORDER, JobKind.EMERGENCY_KILL_SWITCH):
        with pytest.raises(QueuePolicyError):
            service.submit(
                JobRequest(prompt="send it", repo=str(REPOSITORY_ROOT), kind=kind)
            )
    assert service.list() == []
    service.close()


def test_cloud_write_waits_for_authorization_and_verification(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    waiting_auth = service.submit(
        JobRequest(prompt="deploy", repo=str(REPOSITORY_ROOT), kind=JobKind.CLOUD_WRITE)
    )
    assert waiting_auth.gate_state == GateState.WAITING_AUTHORIZATION

    waiting_verification = service.submit(
        JobRequest(
            prompt="deploy",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.CLOUD_WRITE,
            authorization_ref="release-manifest://job-1",
        )
    )
    assert waiting_verification.gate_state == GateState.WAITING_VERIFICATION
    assert service.store.claim_next(
        "worker", now=utc_iso(), lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    ) is None
    preflight = service.submit(
        JobRequest(
            prompt="Run the fixed read-only Mainnet preflight.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.MAINNET_PREFLIGHT,
        )
    )
    assert preflight.gate_state == GateState.WAITING_AUTHORIZATION
    service.close()


def test_mainnet_preflight_queue_uses_fixed_control_plane_without_agy(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    job = service.submit(
        JobRequest(
            prompt="Run the fixed read-only Mainnet preflight.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.MAINNET_PREFLIGHT,
            authorization_ref="release-preflight://candidate-1",
        )
    )
    calls: list[str] = []

    def runner(received: Any) -> dict[str, Any]:
        calls.append(received.job_id)
        return {
            "executionMode": "LIVE",
            "preflightOnly": True,
            "preflightPassed": True,
            "orderSubmissionAttempts": 0,
            "orderEndpointAttempts": 0,
            "observedAt": utc_iso(),
            "checks": [
                {
                    "id": "PREFLIGHT",
                    "name": "fixed",
                    "required": True,
                    "status": "PASS",
                    "message": "OK",
                }
            ],
        }

    worker = QueueWorker(service, worker_id="preflight-worker", preflight_runner=runner)
    result = worker.run_once()
    assert result is not None and result.status == JobStatus.SUCCEEDED
    assert calls == [job.job_id]
    assert result.result_json is not None
    assert result.result_json["verification"]["execution_authority"] == "PYTHON_TRADING_WORKER"
    worker.close()
    service.close()


def test_mainnet_preflight_queue_rejects_non_verified_runner_result(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.submit(
        JobRequest(
            prompt="Run the fixed read-only Mainnet preflight.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.MAINNET_PREFLIGHT,
            authorization_ref="release-preflight://candidate-2",
        )
    )
    worker = QueueWorker(
        service,
        worker_id="preflight-worker",
        preflight_runner=lambda _job: {
            "preflightOnly": True,
            "preflightPassed": False,
            "orderSubmissionAttempts": 0,
            "orderEndpointAttempts": 0,
        },
    )
    result = worker.run_once()
    assert result is not None and result.status == JobStatus.FAILED
    assert result.error_code == "mainnet_preflight_failed"
    worker.close()
    service.close()


def test_mainnet_preflight_client_rejects_empty_sanitized_checks() -> None:
    from apps.agy_queue.preflight import _sanitize_response

    evidence = _sanitize_response(
        {
            "preflightOnly": True,
            "preflightPassed": True,
            "orderSubmissionAttempts": 0,
            "orderEndpointAttempts": 0,
            "checks": [{"id": "", "name": "", "status": "PASS", "message": "ok"}],
        }
    )
    assert evidence["checks"] == []


def _fake_inspection() -> AgyCliInspection:
    return AgyCliInspection(
        version="test",
        help_text="--sandbox --mode --print-timeout",
        supports_sandbox=True,
        supports_mode=True,
        supports_print_timeout=True,
    )


def test_model_effort_suffix_and_flag_support() -> None:
    assert model_effort_suffix("gemini-3.8-flash-high") == "high"
    assert model_effort_suffix("claude-sonnet-4-6") is None
    assert model_effort_suffix("gpt-oss-120b-medium") == "medium"
    # Verified against the installed AGY 1.2.4 release: claude-* rejects
    # --effort outright, and suffixed models reject a disagreeing value.
    assert model_accepts_effort_flag("claude-sonnet-4-6") is False
    assert model_accepts_effort_flag("gemini-3.8-flash-high") is False
    assert model_accepts_effort_flag("some-generic-model") is True


def test_build_argv_omits_effort_for_fixed_effort_models(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    job = service.submit(
        JobRequest(
            prompt="Inspect the repository.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.READ_ONLY,
            model="claude-sonnet-4-6",
        )
    )
    argv = build_argv(
        executable=Path(sys.executable),
        job=job,
        inspection=_fake_inspection(),
        print_timeout="5m",
    )
    assert "--effort" not in argv
    assert "--model" in argv and "claude-sonnet-4-6" in argv
    service.close()


def test_build_argv_omits_effort_for_suffixed_models(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    job = service.submit(
        JobRequest(
            prompt="Inspect the repository.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.READ_ONLY,
            model="gemini-3.8-flash-high",
        )
    )
    assert job.effort == "high"  # derived silently from the model suffix
    argv = build_argv(
        executable=Path(sys.executable),
        job=job,
        inspection=_fake_inspection(),
        print_timeout="5m",
    )
    assert "--effort" not in argv
    service.close()


def test_build_argv_includes_effort_for_generic_models(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    job = service.submit(
        JobRequest(
            prompt="Inspect the repository.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.READ_ONLY,
            model="some-generic-model",
            effort="high",
        )
    )
    argv = build_argv(
        executable=Path(sys.executable),
        job=job,
        inspection=_fake_inspection(),
        print_timeout="5m",
    )
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"
    service.close()


def test_submit_rejects_explicit_effort_conflicting_with_model_suffix(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(QueuePolicyError):
        service.submit(
            JobRequest(
                prompt="Inspect the repository.",
                repo=str(REPOSITORY_ROOT),
                kind=JobKind.READ_ONLY,
                model="gemini-3.8-flash-high",
                effort="medium",
            )
        )
    service.close()


def test_submit_repo_change_requires_tests_passed_schema(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    repo_dir = make_worker_repo(tmp_path)
    with pytest.raises(QueuePolicyError):
        service.submit(
            JobRequest(
                prompt="Make a trivial edit.",
                repo=str(repo_dir),
                kind=JobKind.REPO_CHANGE,
                authorization_ref="user-approved:test",
            )
        )
    job = service.submit(
        JobRequest(
            prompt="Make a trivial edit.",
            repo=str(repo_dir),
            kind=JobKind.REPO_CHANGE,
            authorization_ref="user-approved:test",
            expected_output_schema={
                "type": "object",
                "required": ["tests_passed"],
                "properties": {"tests_passed": {"type": "boolean"}},
            },
        )
    )
    assert job.job_kind == JobKind.REPO_CHANGE
    service.close()


def test_atomic_claim_prevents_duplicate_workers(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    job = submit_read_only(service)
    store_a = SQLiteQueueStore(tmp_path / "queue" / "queue.db", paths=service.config.paths)
    store_b = SQLiteQueueStore(tmp_path / "queue" / "queue.db", paths=service.config.paths)
    claimed = store_a.claim_next(
        "worker-a", now=utc_iso(), lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    duplicate = store_b.claim_next(
        "worker-b", now=utc_iso(), lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    assert claimed is not None and claimed.job_id == job.job_id
    assert duplicate is None
    store_a.close()
    store_b.close()
    service.close()


def test_claim_serializes_overlapping_scope(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = submit_read_only(service)
    second = submit_read_only(service)
    claimed = service.store.claim_next(
        "worker-a", now=utc_iso(), lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    blocked = service.store.claim_next(
        "worker-b", now=utc_iso(), lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    assert claimed is not None and claimed.job_id == first.job_id
    assert blocked is None
    assert service.get(second.job_id).status == JobStatus.PENDING
    service.close()


def test_lease_expiry_requeues_local_job_but_blocks_external_effect(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    local_job = submit_read_only(service)
    local_claim = service.store.claim_next(
        "worker", now="2026-09-16T00:00:00.000Z", lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    assert local_claim is not None
    recovered = service.store.recover_expired(now="2026-09-16T00:01:00.000Z")
    assert local_job.job_id in recovered
    assert service.get(local_job.job_id).status == JobStatus.PENDING

    external = service.submit(
        JobRequest(
            prompt="deploy",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.CLOUD_WRITE,
            authorization_ref="release-manifest://job-2",
            verification_job_id="verify-2",
        )
    )
    external_claim = service.store.claim_next(
        "external-worker",
        now="2026-09-16T00:00:00.000Z",
        lease_ttl_sec=30,
        eligible_kinds={JobKind.CLOUD_WRITE},
    )
    assert external_claim is not None
    service.store.recover_expired(now="2026-09-16T00:01:00.000Z")
    blocked = service.get(external.job_id)
    assert blocked.status == JobStatus.FAILED
    assert blocked.gate_state == GateState.BLOCKED
    assert blocked.error_code == "external_effect_uncertain"
    service.close()


@pytest.mark.parametrize("backend", ["sqlite", "jsonl"])
def test_stale_worker_cannot_complete_a_job_reclaimed_by_another_worker(
    backend: str, tmp_path: Path
) -> None:
    """A worker whose lease expired and was reclaimed must not silently
    overwrite the new owner's in-flight job on late completion (mark_success/
    mark_failure previously had no worker_id ownership check, unlike
    heartbeat)."""
    service = make_service(tmp_path, backend=backend)
    job = submit_read_only(service)
    stale_claim = service.store.claim_next(
        "worker-a", now="2026-09-16T00:00:00.000Z", lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    assert stale_claim is not None
    recovered = service.store.recover_expired(now="2026-09-16T00:01:00.000Z")
    assert job.job_id in recovered

    fresh_claim = service.store.claim_next(
        "worker-b", now="2026-09-16T00:01:00.000Z", lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    assert fresh_claim is not None
    assert fresh_claim.worker_id == "worker-b"

    # worker-a's stale call (from before its lease expired) must not succeed.
    stale_result = {"job_id": job.job_id, "status": "succeeded", "error": None}
    owned = service.store.mark_success(
        job.job_id,
        worker_id="worker-a",
        result=stale_result,
        result_path="/tmp/stale.json",
        conversation_id=None,
        head_sha=None,
    )
    assert owned is False
    still_running = service.get(job.job_id)
    assert still_running.status == JobStatus.RUNNING
    assert still_running.worker_id == "worker-b"
    assert still_running.result_json is None

    # worker-b (the rightful current owner) can still complete it normally.
    owned_by_rightful_worker = service.store.mark_success(
        job.job_id,
        worker_id="worker-b",
        result={"job_id": job.job_id, "status": "succeeded", "error": None},
        result_path="/tmp/real.json",
        conversation_id=None,
        head_sha=None,
    )
    assert owned_by_rightful_worker is True
    assert service.get(job.job_id).status == JobStatus.SUCCEEDED
    service.close()


@pytest.mark.parametrize("backend", ["sqlite", "jsonl"])
def test_stale_worker_mark_failure_cannot_overwrite_reclaimed_job(
    backend: str, tmp_path: Path
) -> None:
    service = make_service(tmp_path, backend=backend)
    job = submit_read_only(service)
    stale_claim = service.store.claim_next(
        "worker-a", now="2026-09-16T00:00:00.000Z", lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    assert stale_claim is not None
    service.store.recover_expired(now="2026-09-16T00:01:00.000Z")
    fresh_claim = service.store.claim_next(
        "worker-b", now="2026-09-16T00:01:00.000Z", lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    assert fresh_claim is not None

    owned = service.store.mark_failure(
        job.job_id,
        worker_id="worker-a",
        error_code="stale_timeout",
        stderr="stale worker's late failure report",
        result=None,
    )
    assert owned is False
    still_running = service.get(job.job_id)
    assert still_running.status == JobStatus.RUNNING
    assert still_running.worker_id == "worker-b"
    assert still_running.error_code is None
    service.close()


@pytest.mark.parametrize("backend", ["sqlite", "jsonl"])
def test_expired_job_at_attempt_limit_becomes_terminal(backend: str, tmp_path: Path) -> None:
    service = make_service(tmp_path, backend=backend)
    job = service.submit(
        JobRequest(
            prompt="Inspect the repository and return a short result.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.READ_ONLY,
            model="test-model",
            effort="medium",
            max_attempts=1,
        )
    )
    assert service.store.claim_next(
        "worker", now="2026-09-16T00:00:00.000Z", lease_ttl_sec=30,
        eligible_kinds={JobKind.READ_ONLY},
    ) is not None
    assert service.store.recover_expired(now="2026-09-16T00:01:00.000Z") == [job.job_id]
    recovered = service.get(job.job_id)
    assert recovered.status == JobStatus.FAILED
    assert recovered.gate_state == GateState.BLOCKED
    assert recovered.error_code == "lease_expired_attempts_exhausted"
    assert service.store.claim_next(
        "second-worker", now="2026-09-16T00:02:00.000Z", lease_ttl_sec=30,
        eligible_kinds={JobKind.READ_ONLY},
    ) is None
    service.close()


class FakeSession:
    def __init__(
        self,
        on_event: Callable[[Any, dict[str, Any]], None],
        behavior: Callable[[Any], AgyTerminalResult] | BaseException,
    ):
        self.on_event = on_event
        self.behavior = behavior
        self.current_job: Any = None
        self.closed = False

    def compatible(self, job: Any, now: float, **_: Any) -> bool:
        return bool(
            self.current_job
            and self.current_job.session_key
            and self.current_job.session_key == job.session_key
            and self.current_job.repo == job.repo
            and self.current_job.base_sha == job.base_sha
            and self.current_job.branch == job.branch
            and self.current_job.scope_key == job.scope_key
            and self.current_job.model == job.model
            and self.current_job.effort == job.effort
            and self.current_job.job_kind == job.job_kind
            and self.current_job.expected_output_schema == job.expected_output_schema
        )

    def send(self, job: Any, **_: Any) -> AgyTerminalResult:
        self.current_job = job
        self.on_event(
            job,
            {
                "event": "init",
                "init": {
                    "cwd": job.repo,
                    "model": job.model,
                    "permission_mode": "request-review",
                },
            },
        )
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        self.on_event(
            job,
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": self.behavior.response,
                    "conversation_id": "conversation-test",
                },
            },
        )
        return self.behavior

    def close(self) -> None:
        self.closed = True


def success_result(response: str = "done") -> AgyTerminalResult:
    return AgyTerminalResult(
        raw_result={"status": "SUCCESS", "response": response},
        conversation_id="conversation-test",
        status="SUCCESS",
        response=response,
        structured_output=None,
        usage={},
        stderr=None,
    )


def fake_worker(
    service: QueueService,
    behavior: Callable[[Any], AgyTerminalResult] | BaseException,
    *,
    factory_calls: list[int] | None = None,
) -> QueueWorker:
    inspection = AgyCliInspection(
        version="test",
        help_text="--sandbox --mode --print-timeout",
        supports_sandbox=True,
        supports_mode=True,
        supports_print_timeout=True,
    )

    def factory(job: Any, _inspection: AgyCliInspection, on_event: Callable[..., None]) -> FakeSession:
        if factory_calls is not None:
            factory_calls.append(1)
        return FakeSession(on_event, behavior)

    return QueueWorker(
        service,
        worker_id="test-worker",
        inspection=inspection,
        session_factory=factory,
        random_source=__import__("random").Random(1),
    )


def test_worker_persists_result_and_get_can_read_after_process_exit(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    job = submit_read_only(service)
    worker = fake_worker(service, success_result())
    outcome = worker.run_once()
    assert outcome is not None
    assert outcome.status == JobStatus.SUCCEEDED
    assert service.get(job.job_id).result_json is not None
    assert Path(service.get(job.job_id).result_path or "").exists()
    assert Path(service.get(job.job_id).raw_events_path or "").exists()
    result_payload = dict(service.get(job.job_id).result_json or {})
    assert service.artifacts.write_result(job.job_id, result_payload) == service.get(job.job_id).result_path
    result_payload["response"] = "different-result"
    with pytest.raises(QueueStorageError):
        service.artifacts.write_result(job.job_id, result_payload)
    worker.close()
    service.close()


def test_worker_retries_capacity_with_bounded_backoff(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    submit_read_only(service)
    worker = fake_worker(service, AgyCapacityError("503 No capacity"))
    outcome = worker.run_once()
    assert outcome is not None
    assert outcome.status == JobStatus.PENDING
    assert outcome.attempt == 1
    assert outcome.error_code == "agy_capacity"
    assert outcome.next_attempt_at is not None
    worker.close()
    service.close()


def test_related_jobs_reuse_session_but_unrelated_jobs_start_fresh(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    submit_read_only(service, session_key="related")
    submit_read_only(service, session_key="related")
    calls: list[int] = []
    worker = fake_worker(service, success_result(), factory_calls=calls)
    assert worker.run_once() is not None
    assert worker.run_once() is not None
    assert len(calls) == 1
    worker.close()

    submit_read_only(service)
    submit_read_only(service)
    worker = fake_worker(service, success_result(), factory_calls=calls)
    assert worker.run_once() is not None
    assert worker.run_once() is not None
    assert len(calls) == 3
    worker.close()
    service.close()


def test_session_reuse_does_not_cross_permission_class(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    submit_read_only(service, session_key="same-session")
    service.submit(
        JobRequest(
            prompt="Summarize the release evidence.",
            repo=str(REPOSITORY_ROOT),
            kind=JobKind.RELEASE_GATE_EVALUATE,
            model="test-model",
            effort="medium",
            session_key="same-session",
        )
    )
    calls: list[int] = []
    worker = fake_worker(service, success_result(), factory_calls=calls)
    assert worker.run_once() is not None
    assert worker.run_once() is not None
    assert len(calls) == 2
    worker.close()
    service.close()


def test_prompt_and_artifacts_never_store_credentials(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(CredentialLikeContentError):
        service.submit(
            JobRequest(
                prompt="api_key=super-secret-value-123456",
                repo=str(REPOSITORY_ROOT),
            )
        )
    artifacts = ResultArtifactStore(QueuePaths(tmp_path / "artifacts"))
    path = artifacts.write_result(
        "agy-redaction",
        {"status": "failed", "error": "password=secret-value-123456"},
    )
    stored = Path(path).read_text(encoding="utf-8")
    assert "secret-value-123456" not in stored
    assert "<REDACTED>" in stored
    service.close()


def test_usage_token_counts_survive_redaction_but_credential_tokens_do_not(
    tmp_path: Path,
) -> None:
    from apps.agy_queue.security import redact_json

    payload = redact_json(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "thinking_tokens": 5,
                "cache_read_tokens": 0,
                "total_tokens": 125,
            },
            "access_token": "abc123secretvalue",
            "api_key": "xyz",
        }
    )
    assert payload["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "thinking_tokens": 5,
        "cache_read_tokens": 0,
        "total_tokens": 125,
    }
    assert payload["access_token"] == "<REDACTED>"
    assert payload["api_key"] == "<REDACTED>"


def test_watch_yields_pending_then_terminal(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    job = submit_read_only(service)
    claimed = service.store.claim_next(
        "watch-worker", now=utc_iso(), lease_ttl_sec=30, eligible_kinds={JobKind.READ_ONLY}
    )
    assert claimed is not None
    service.store.mark_failure(
        job.job_id, worker_id="watch-worker", error_code="test", stderr="no", result=None
    )
    states = [item.status for item in service.watch(job.job_id, poll_interval_sec=0.01)]
    assert states == [JobStatus.FAILED]
    service.close()


def test_stream_session_waits_for_terminal_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "fake_agy.py"
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'event':'init','init':{'cwd':os.getcwd(),'model':'test-model','permission_mode':'request-review'}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    message=json.loads(line)\n"
        "    print(json.dumps({'event':'step_update','step_update':{'state':'DONE'}}), flush=True)\n"
        "    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'stream-result','conversation_id':'c1'}}), flush=True)\n",
        encoding="utf-8",
    )
    job_service = make_service(tmp_path)
    job = submit_read_only(job_service)
    inspection = AgyCliInspection(
        version="test",
        help_text="",
        supports_sandbox=True,
        supports_mode=False,
        supports_print_timeout=False,
    )
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    events: list[dict[str, Any]] = []
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda _job, event: events.append(event),
    )
    result = session.send(job, heartbeat_interval_sec=30)
    session.close()
    assert result.response == "stream-result"
    assert [event["event"] for event in events] == ["init", "step_update", "result"]
    job_service.close()


def test_stream_session_uses_committed_fake_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = REPOSITORY_ROOT / "tests" / "fixtures" / "agy" / "fake_stream.py"
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection(
        version="fixture",
        help_text="--sandbox --mode --print-timeout",
        supports_sandbox=True,
        supports_mode=True,
        supports_print_timeout=True,
    )
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [
            sys.executable,
            str(fixture),
            "--model",
            job.model,
            "--effort",
            job.effort,
            "--sandbox",
        ],
    )
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    result = session.send(job, heartbeat_interval_sec=30)
    session.close()
    service.close()
    assert result.response == f"fixture:{len(job.prompt)}"
    assert result.structured_output == {"fixture": True}


def test_stream_session_heartbeats_while_waiting_for_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "fake_agy_slow.py"
    script.write_text(
        "import json, os, sys, time\n"
        "print(json.dumps({'event':'init','init':{'cwd':os.getcwd(),'model':'test-model','permission_mode':'request-review'}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    time.sleep(0.2)\n"
        "    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'slow-ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection("test", "", True, False, False)
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    beats: list[int] = []
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    result = session.send(
        job,
        on_heartbeat=lambda: beats.append(1),
        heartbeat_interval_sec=0.05,
    )
    session.close()
    service.close()
    assert result.response == "slow-ok"
    assert beats


def test_stream_session_heartbeats_during_continuous_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "fake_agy_event_rich.py"
    script.write_text(
        "import json, os, sys, time\n"
        "print(json.dumps({'event':'init','init':{'cwd':os.getcwd(),'model':'test-model','permission_mode':'request-review'}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    for index in range(8):\n"
        "        print(json.dumps({'event':'step_update','step_update':{'index':index}}), flush=True)\n"
        "        time.sleep(0.02)\n"
        "    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'event-rich-ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection("test", "", True, False, False)
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    heartbeats: list[int] = []
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    result = session.send(
        job,
        on_heartbeat=lambda: heartbeats.append(1),
        heartbeat_interval_sec=0.05,
    )
    session.close()
    assert result.response == "event-rich-ok"
    assert heartbeats
    service.close()


def test_stream_session_missing_result_is_uncertain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "fake_agy_exit.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'event':'init','init':{'cwd':os.getcwd(),'model':'test-model','permission_mode':'request-review'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection("test", "", True, False, False)
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    with pytest.raises(AgyProcessExitError):
        session.send(job, heartbeat_interval_sec=30)
    session.close()
    service.close()


def test_stream_session_malformed_ndjson_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "fake_agy_malformed.py"
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'event':'init','init':{'cwd':os.getcwd(),'model':'test-model','permission_mode':'request-review'}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('INVALID NOT JSON', flush=True)\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection("test", "", True, False, False)
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    with pytest.raises(AgyProtocolError):
        session.send(job, heartbeat_interval_sec=30)
    session.close()
    service.close()


def test_stream_session_empty_response_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "fake_agy_empty.py"
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'event':'init','init':{'cwd':os.getcwd(),'model':'test-model','permission_mode':'request-review'}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'','conversation_id':'c1'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection("test", "", True, False, False)
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    with pytest.raises(AgyResultError):
        session.send(job, heartbeat_interval_sec=30)
    session.close()
    service.close()


def test_stream_session_invalid_cwd_fails_permission(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    wrong_dir = tmp_path / "wrong_cwd"
    wrong_dir.mkdir()
    script = tmp_path / "fake_agy_wrong_cwd.py"
    escaped_wrong = str(wrong_dir).replace('\\', '\\\\')
    script.write_text(
        "import json\n"
        f"print(json.dumps({{'event':'init','init':{{'cwd':'{escaped_wrong}','model':'test-model','permission_mode':'request-review'}}}}), flush=True)\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection("test", "", True, False, False)
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    with pytest.raises(AgyPermissionError):
        session.send(job, heartbeat_interval_sec=30)
    session.close()
    service.close()


def test_stream_session_accepts_always_proceed_permission_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # AGY 1.2.4 reports permission_mode="always-proceed" for every real
    # launch we drive (--sandbox, --mode accept-edits, --mode plan alike);
    # the queue must not reject real jobs on this unverifiable self-report.
    script = tmp_path / "fake_agy_always_proceed.py"
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'event':'init','init':{'cwd':os.getcwd(),'model':'test-model','permission_mode':'always-proceed'}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'ok','conversation_id':'c1'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection("test", "", True, False, False)
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    result = session.send(job, heartbeat_interval_sec=30)
    assert result.response == "ok"
    session.close()
    service.close()


def test_stream_session_rejects_explicit_skip_permissions_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "fake_agy_skip_permissions.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'event':'init','init':{'cwd':os.getcwd(),'model':'test-model','permission_mode':'dangerously-skip-permissions'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    job = submit_read_only(service)
    inspection = AgyCliInspection("test", "", True, False, False)
    monkeypatch.setattr(
        "apps.agy_queue.agy.build_argv",
        lambda **_: [sys.executable, str(script)],
    )
    session = AgySession(
        executable=sys.executable,
        inspection=inspection,
        print_timeout="5m",
        startup_timeout_sec=5,
        on_event=lambda *_: None,
    )
    with pytest.raises(AgyPermissionError):
        session.send(job, heartbeat_interval_sec=30)
    session.close()
    service.close()
