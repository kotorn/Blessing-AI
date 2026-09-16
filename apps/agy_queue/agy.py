"""Version-aware AGY stream-json subprocess driver."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import JobRecord
from .security import redact_text


class AgyError(RuntimeError):
    """Base class for a failed AGY turn."""

    code = "agy_error"
    retryable = False
    uncertain = False


class AgyStartupError(AgyError):
    code = "agy_startup_error"
    retryable = True


class AgyCapacityError(AgyError):
    code = "agy_capacity"
    retryable = True


class AgyTimeoutError(AgyError):
    code = "agy_timeout_uncertain"
    retryable = True
    uncertain = True


class AgyProcessExitError(AgyError):
    code = "agy_process_exit_before_result"
    retryable = True
    uncertain = True


class AgyProtocolError(AgyError):
    code = "agy_protocol_error"


class AgyPermissionError(AgyError):
    code = "agy_permission_policy"


class AgyResultError(AgyError):
    code = "agy_result_invalid"


@dataclass(frozen=True, slots=True)
class AgyCliInspection:
    version: str
    help_text: str
    supports_sandbox: bool
    supports_mode: bool
    supports_print_timeout: bool


@dataclass(frozen=True, slots=True)
class AgyTerminalResult:
    raw_result: dict[str, Any]
    conversation_id: str | None
    status: str
    response: str | None
    structured_output: Any
    usage: dict[str, Any]
    stderr: str | None


def _command_output(completed: subprocess.CompletedProcess[str]) -> str:
    return redact_text((completed.stdout or "") + "\n" + (completed.stderr or "")) or ""


def inspect_cli(
    executable: str | Path,
    *,
    cwd: str | Path,
    expected_version: str | None = None,
    timeout_sec: int = 30,
) -> AgyCliInspection:
    """Inspect the installed executable before relying on versioned flags."""

    exe = str(Path(executable).resolve())
    try:
        version_result = subprocess.run(
            [exe, "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
        help_result = subprocess.run(
            [exe, "--help"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgyStartupError("Unable to inspect the configured AGY executable") from exc

    version_output = _command_output(version_result)
    help_output = _command_output(help_result)
    if version_result.returncode != 0 or help_result.returncode != 0:
        raise AgyStartupError("AGY --version/--help did not complete successfully")
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", version_output)
    version = match.group(1) if match else "unknown"
    if expected_version and version != expected_version:
        raise AgyStartupError(
            f"AGY version mismatch: expected {expected_version}, got {version}"
        )
    return AgyCliInspection(
        version=version,
        help_text=help_output,
        supports_sandbox="--sandbox" in help_output,
        supports_mode="--mode" in help_output,
        supports_print_timeout="--print-timeout" in help_output,
    )


_SECRET_ENV_NAME = re.compile(
    r"(?i)(?:api[_-]?(?:key|secret)|password|token|authorization|private[_-]?key|"
    r"client[_-]?secret|google_application_credentials|firebase_token|database_url|"
    r"binance_|postgres_)")


def safe_child_environment() -> dict[str, str]:
    """Keep runtime basics while stripping common credential variables."""

    safe: dict[str, str] = {}
    for key, value in os.environ.items():
        if _SECRET_ENV_NAME.search(key):
            continue
        safe[key] = value
    return safe


_EFFORT_SUFFIX = re.compile(r"-(low|medium|high)$")


def model_effort_suffix(model: str) -> str | None:
    """Return the effort tier a model name bakes in, if any (e.g. "high")."""

    match = _EFFORT_SUFFIX.search(model)
    return match.group(1) if match else None


def model_accepts_effort_flag(model: str) -> bool:
    """Whether AGY accepts a separate --effort flag for this model.

    Verified empirically against the installed AGY 1.2.4 release: every
    claude-* model rejects --effort outright, and every Gemini/gpt-oss model
    in the current roster already bakes its tier into the model name
    (-low/-medium/-high) and rejects a --effort value that disagrees with it.
    Passing --effort only for a model that has neither trait keeps this
    forward-compatible with a future model that takes free-form effort.
    """

    return not model.startswith("claude-") and model_effort_suffix(model) is None


def build_argv(
    *,
    executable: str | Path,
    job: JobRecord,
    inspection: AgyCliInspection,
    print_timeout: str,
) -> list[str]:
    """Build the only supported stream-json launch shape."""

    arguments = [
        str(Path(executable).resolve()),
        "--model",
        job.model,
    ]
    if model_accepts_effort_flag(job.model):
        arguments.extend(["--effort", job.effort])
    arguments.extend(
        [
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ]
    )
    if inspection.supports_print_timeout:
        arguments.extend(["--print-timeout", print_timeout])
    if job.expected_output_schema:
        arguments.extend(["--json-schema", json.dumps(job.expected_output_schema)])

    if job.job_kind.value == "REPO_CHANGE":
        if not inspection.supports_mode:
            raise AgyPermissionError("AGY release does not expose an accept-edits mode")
        arguments.extend(["--mode", "accept-edits"])
    elif inspection.supports_sandbox:
        arguments.append("--sandbox")
    elif inspection.supports_mode:
        arguments.extend(["--mode", "request-review"])
    else:
        raise AgyPermissionError("AGY release exposes no safe permission mode")

    arguments.extend(["--add-dir", job.repo])
    if any(argument in {"-p", "--prompt", "--print", "--dangerously-skip-permissions"} for argument in arguments):
        raise AgyPermissionError("Unsafe or one-shot AGY flag detected")
    return arguments


_ERROR_MARKERS = (
    "print timeout",
    "timed out",
    "authentication required",
    "permission denied",
    "fatal error",
)


class AgySession:
    """One persistent AGY process for a deliberately related job batch."""

    def __init__(
        self,
        *,
        executable: str | Path,
        inspection: AgyCliInspection,
        print_timeout: str,
        startup_timeout_sec: int,
        on_event: Callable[[JobRecord, dict[str, Any]], None],
    ):
        self.executable = Path(executable).resolve()
        self.inspection = inspection
        self.print_timeout = print_timeout
        self.startup_timeout_sec = startup_timeout_sec
        self._on_event = on_event
        self._process: subprocess.Popen[str] | None = None
        self._output: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._reader_threads: list[threading.Thread] = []
        self._started_at = 0.0
        self._last_activity = 0.0
        self._jobs_used = 0
        self._job: JobRecord | None = None
        self.conversation_id: str | None = None

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def last_activity(self) -> float:
        return self._last_activity

    @property
    def jobs_used(self) -> int:
        return self._jobs_used

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def compatible(self, job: JobRecord, now: float, *, idle_timeout_sec: int, max_age_sec: int, max_jobs: int) -> bool:
        if not self.alive or self._job is None:
            return False
        if not job.session_key or job.session_key != self._job.session_key:
            return False
        if job.repo != self._job.repo or job.branch != self._job.branch or job.base_sha != self._job.base_sha:
            return False
        if job.scope_key != self._job.scope_key or job.model != self._job.model or job.effort != self._job.effort:
            return False
        if (
            job.job_kind != self._job.job_kind
            or job.expected_output_schema != self._job.expected_output_schema
        ):
            return False
        if now - self._last_activity > idle_timeout_sec:
            return False
        return now - self._started_at <= max_age_sec and self._jobs_used < max_jobs

    def _reader(self, stream: Any, stream_name: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._output.put((stream_name, line.rstrip("\r\n")))
        finally:
            self._output.put((f"{stream_name}_eof", None))

    def _start_process(self, job: JobRecord) -> None:
        argv = build_argv(
            executable=self.executable,
            job=job,
            inspection=self.inspection,
            print_timeout=self.print_timeout,
        )
        try:
            process = subprocess.Popen(
                argv,
                cwd=job.repo,
                env=safe_child_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise AgyStartupError("Unable to start AGY process") from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise AgyStartupError("AGY process did not expose standard streams")
        self._process = process
        self._job = job
        self._started_at = time.monotonic()
        self._last_activity = self._started_at
        self._stderr_lines = []
        self._reader_threads = [
            threading.Thread(target=self._reader, args=(process.stdout, "stdout"), daemon=True),
            threading.Thread(target=self._reader, args=(process.stderr, "stderr"), daemon=True),
        ]
        for thread in self._reader_threads:
            thread.start()

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any]:
        if not line.strip():
            raise AgyProtocolError("AGY emitted an empty NDJSON line")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AgyProtocolError("AGY emitted malformed NDJSON") from exc
        if not isinstance(value, dict):
            raise AgyProtocolError("AGY NDJSON event must be an object")
        return value

    def _read_until(
        self,
        deadline: float,
        *,
        on_tick: Callable[[], None] | None = None,
        tick_interval_sec: float = 30,
    ) -> tuple[str, dict[str, Any]]:
        next_tick = time.monotonic() + max(0.05, tick_interval_sec)
        while True:
            now = time.monotonic()
            if on_tick and now >= next_tick:
                on_tick()
                next_tick = time.monotonic() + max(0.05, tick_interval_sec)
            remaining = deadline - now
            if remaining <= 0:
                raise AgyTimeoutError("AGY startup or result timeout")
            wait_time = min(remaining, 0.25)
            if on_tick:
                wait_time = min(wait_time, max(0.01, next_tick - time.monotonic()))
            try:
                stream, line = self._output.get(timeout=wait_time)
            except queue.Empty:
                continue
            if stream == "stderr":
                if line:
                    self._stderr_lines.append(redact_text(line) or "")
                continue
            if stream == "stdout_eof":
                code = self._process.poll() if self._process else None
                raise AgyProcessExitError(f"AGY exited before result (code={code})")
            if stream != "stdout" or line is None:
                continue
            event = self._parse_line(line)
            return stream, event

    def _record_event(self, job: JobRecord, event: dict[str, Any]) -> None:
        self._last_activity = time.monotonic()
        self._on_event(job, event)

    def _validate_init(self, job: JobRecord, event: dict[str, Any]) -> None:
        init = event.get("init")
        if not isinstance(init, dict):
            raise AgyProtocolError("AGY init event is missing its init object")
        observed_cwd = init.get("cwd")
        if not isinstance(observed_cwd, str) or Path(observed_cwd).resolve() != Path(job.repo).resolve():
            raise AgyPermissionError("AGY active cwd does not match the intended repository")
        observed_model = init.get("model") or event.get("model")
        if not isinstance(observed_model, str) or not observed_model.strip():
            raise AgyPermissionError("AGY init did not report the configured model")
        if str(observed_model) != job.model:
            raise AgyPermissionError("AGY init model does not match the queued job")
        observed_effort = init.get("effort") or event.get("effort")
        if observed_effort is not None and str(observed_effort) != job.effort:
            raise AgyPermissionError("AGY init effort does not match the queued job")
        permission_mode = str(init.get("permission_mode") or "").strip().lower()
        # AGY 1.2.4 reports permission_mode="always-proceed" in the init event
        # for every launch we drive (--sandbox, --mode accept-edits, --mode
        # plan alike), confirmed empirically against the installed release --
        # it does not reflect the effective restriction in this version. The
        # real enforcement boundary is build_argv(), which never emits
        # --dangerously-skip-permissions and always emits a limiting flag, so
        # "always-proceed" is treated as unverifiable rather than unsafe. We
        # still hard-fail if the CLI ever names the unsafe flag explicitly.
        if permission_mode == "dangerously-skip-permissions":
            raise AgyPermissionError("AGY effective permission mode is unrestricted")
        allowed = {
            "request-review",
            "accept-edits",
            "sandbox",
            "read-only",
            "plan",
            "always-proceed",
        }
        if permission_mode not in allowed:
            raise AgyPermissionError(f"AGY permission mode is not allowlisted: {permission_mode}")
        if job.job_kind.value == "REPO_CHANGE" and permission_mode not in {
            "accept-edits",
            "request-review",
            "always-proceed",
        }:
            raise AgyPermissionError("Repository change job did not use an acceptable edit mode")
        self.conversation_id = str(init.get("conversation_id") or event.get("conversation_id") or "") or None

    def start(self, job: JobRecord) -> None:
        self._start_process(job)
        _, event = self._read_until(time.monotonic() + self.startup_timeout_sec)
        self._record_event(job, event)
        if event.get("event") != "init":
            raise AgyProtocolError("AGY stream did not begin with init")
        self._validate_init(job, event)

    def send(
        self,
        job: JobRecord,
        *,
        on_heartbeat: Callable[[], None] | None = None,
        heartbeat_interval_sec: int = 30,
    ) -> AgyTerminalResult:
        if not self.alive:
            self.start(job)
        else:
            self._job = job
        if self._process is None or self._process.stdin is None:
            raise AgyStartupError("AGY stdin is unavailable")
        # Stderr belongs to one turn.  Keeping an old warning/error marker in a
        # persistent session would make a later, otherwise valid related job
        # fail because of an unrelated earlier turn.
        self._stderr_lines = []
        next_heartbeat_at = time.monotonic() + max(0.05, float(heartbeat_interval_sec))

        def heartbeat_if_due() -> None:
            nonlocal next_heartbeat_at
            if on_heartbeat is None:
                return
            now = time.monotonic()
            if now < next_heartbeat_at:
                return
            on_heartbeat()
            next_heartbeat_at = time.monotonic() + max(0.05, float(heartbeat_interval_sec))

        message = {"event": "user", "message": {"content": job.prompt}}
        try:
            self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AgyProcessExitError("AGY stdin closed before prompt delivery") from exc
        deadline = time.monotonic() + job_timeout(job)
        while True:
            _, event = self._read_until(
                deadline,
                on_tick=heartbeat_if_due,
                tick_interval_sec=heartbeat_interval_sec,
            )
            self._record_event(job, event)
            # An event-rich AGY turn can return from _read_until before its
            # local timer fires. Check the shared deadline after every event
            # so a continuous stream cannot starve the lease heartbeat.
            heartbeat_if_due()
            if event.get("event") != "result":
                continue
            result = event.get("result")
            if not isinstance(result, dict):
                raise AgyProtocolError("AGY result event is missing its result object")
            conversation_id = str(result.get("conversation_id") or self.conversation_id or "") or None
            self.conversation_id = conversation_id
            status = str(result.get("status") or "").upper()
            error_text = str(result.get("error") or "")
            if error_text.strip():
                if _looks_like_capacity(error_text):
                    raise AgyCapacityError("AGY reported provider capacity was unavailable")
                raise AgyResultError("AGY result included an error marker")
            if status not in {"SUCCESS", "SUCCEEDED", "OK"}:
                if _looks_like_capacity(error_text + " " + " ".join(self._stderr_lines)):
                    raise AgyCapacityError("AGY provider capacity was unavailable")
                raise AgyResultError("AGY returned a non-success result")
            response = result.get("response")
            response_text = str(response) if response is not None else None
            structured = result.get("structured_output")
            if (
                (not response_text or not response_text.strip())
                and (structured is None or structured == {} or structured == [])
            ):
                raise AgyResultError("AGY returned an empty response")
            stderr = "\n".join(self._stderr_lines).strip() or None
            if stderr and any(marker in stderr.lower() for marker in _ERROR_MARKERS):
                raise AgyResultError("AGY emitted an error or timeout marker on stderr")
            self._jobs_used += 1
            usage_value = result.get("usage")
            usage: dict[str, Any] = (
                dict(usage_value) if isinstance(usage_value, dict) else {}
            )
            return AgyTerminalResult(
                raw_result=result,
                conversation_id=conversation_id,
                status=status,
                response=response_text,
                structured_output=structured,
                usage=usage,
                stderr=stderr,
            )

    def terminate(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self._process = None

    def close(self) -> None:
        self.terminate()


def job_timeout(job: JobRecord) -> int:
    # The value is persisted by submitter policy; this conservative default
    # keeps old records compatible with the queue schema.
    return max(1, int(getattr(job, "timeout_sec", 300)))


def _looks_like_capacity(value: str) -> bool:
    lowered = value.lower()
    return "503" in lowered or "capacity" in lowered or "no capacity" in lowered
