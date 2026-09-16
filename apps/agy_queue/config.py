"""Explicit runtime configuration for the local AGY queue."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .storage import QueuePaths, default_queue_root

DEFAULT_MODEL = "gemini-3.8-flash-medium"
DEFAULT_EFFORT = "medium"
DEFAULT_AGY_VERSION = "1.2.4"
DEFAULT_PRINT_TIMEOUT = "5m"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_JOB_TIMEOUT_SEC = 300
DEFAULT_LEASE_TTL_SEC = 90
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30
DEFAULT_RETRY_BASE_SEC = 15
DEFAULT_RETRY_CAP_SEC = 120
DEFAULT_SESSION_IDLE_TIMEOUT_SEC = 300
DEFAULT_SESSION_MAX_AGE_SEC = 1800
DEFAULT_SESSION_MAX_JOBS = 10
DEFAULT_POLL_INTERVAL_SEC = 2
DEFAULT_STARTUP_TIMEOUT_SEC = 30


def _positive_int(name: str, default: int, *, maximum: int | None = None) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0 or (maximum is not None and value > maximum):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be > 0{suffix}")
    return value


def _non_empty(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


@dataclass(frozen=True, slots=True)
class QueueConfig:
    root: Path
    backend: str = "sqlite"
    agy_exe: Path = Path(r"C:\Users\Kan\AppData\Local\agy\bin\agy.exe")
    control_plane_url: str = ""
    expected_agy_version: str | None = DEFAULT_AGY_VERSION
    default_model: str = DEFAULT_MODEL
    default_effort: str = DEFAULT_EFFORT
    print_timeout: str = DEFAULT_PRINT_TIMEOUT
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    job_timeout_sec: int = DEFAULT_JOB_TIMEOUT_SEC
    lease_ttl_sec: int = DEFAULT_LEASE_TTL_SEC
    heartbeat_interval_sec: int = DEFAULT_HEARTBEAT_INTERVAL_SEC
    retry_base_sec: int = DEFAULT_RETRY_BASE_SEC
    retry_cap_sec: int = DEFAULT_RETRY_CAP_SEC
    session_idle_timeout_sec: int = DEFAULT_SESSION_IDLE_TIMEOUT_SEC
    session_max_age_sec: int = DEFAULT_SESSION_MAX_AGE_SEC
    session_max_jobs: int = DEFAULT_SESSION_MAX_JOBS
    poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC
    startup_timeout_sec: int = DEFAULT_STARTUP_TIMEOUT_SEC

    @property
    def paths(self) -> QueuePaths:
        return QueuePaths(self.root)

    @property
    def database_path(self) -> Path:
        return self.paths.database

    @classmethod
    def from_env(cls) -> QueueConfig:
        root = Path(os.environ.get("AGY_QUEUE_ROOT", str(default_queue_root()))).resolve()
        backend = os.environ.get("AGY_QUEUE_BACKEND", "sqlite").strip().lower()
        if backend not in {"sqlite", "jsonl"}:
            raise ValueError("AGY_QUEUE_BACKEND must be sqlite or jsonl")
        default_exe = shutil.which("agy") or str(
            Path(os.environ.get("LOCALAPPDATA", "C:/Users/Public/AppData/Local")) / "agy" / "bin" / "agy.exe"
        )
        agy_exe = Path(os.environ.get("AGY_EXE", default_exe)).resolve()
        expected = _non_empty("AGY_EXPECTED_VERSION", DEFAULT_AGY_VERSION)
        return cls(
            root=root,
            backend=backend,
            agy_exe=agy_exe,
            control_plane_url=os.environ.get("AGY_CONTROL_PLANE_URL", "").strip().rstrip("/"),
            expected_agy_version=expected,
            default_model=_non_empty("AGY_DEFAULT_MODEL", DEFAULT_MODEL),
            default_effort=_non_empty("AGY_DEFAULT_EFFORT", DEFAULT_EFFORT),
            print_timeout=_non_empty("AGY_PRINT_TIMEOUT", DEFAULT_PRINT_TIMEOUT),
            max_attempts=_positive_int("AGY_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, maximum=3),
            job_timeout_sec=_positive_int("AGY_JOB_TIMEOUT_SEC", DEFAULT_JOB_TIMEOUT_SEC, maximum=3600),
            lease_ttl_sec=_positive_int("AGY_LEASE_TTL_SEC", DEFAULT_LEASE_TTL_SEC, maximum=3600),
            heartbeat_interval_sec=_positive_int(
                "AGY_HEARTBEAT_INTERVAL_SEC", DEFAULT_HEARTBEAT_INTERVAL_SEC, maximum=600
            ),
            retry_base_sec=_positive_int("AGY_RETRY_BASE_SEC", DEFAULT_RETRY_BASE_SEC, maximum=600),
            retry_cap_sec=_positive_int("AGY_RETRY_CAP_SEC", DEFAULT_RETRY_CAP_SEC, maximum=3600),
            session_idle_timeout_sec=_positive_int(
                "AGY_SESSION_IDLE_TIMEOUT_SEC", DEFAULT_SESSION_IDLE_TIMEOUT_SEC, maximum=3600
            ),
            session_max_age_sec=_positive_int(
                "AGY_SESSION_MAX_AGE_SEC", DEFAULT_SESSION_MAX_AGE_SEC, maximum=86_400
            ),
            session_max_jobs=_positive_int("AGY_SESSION_MAX_JOBS", DEFAULT_SESSION_MAX_JOBS, maximum=100),
            poll_interval_sec=_positive_int("AGY_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC, maximum=300),
            startup_timeout_sec=_positive_int(
                "AGY_STARTUP_TIMEOUT_SEC", DEFAULT_STARTUP_TIMEOUT_SEC, maximum=300
            ),
        )
