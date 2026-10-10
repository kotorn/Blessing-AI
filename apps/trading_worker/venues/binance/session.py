"""Pure, fail-closed timing rules for a bounded Local Live Pilot session."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

DEFAULT_ENTRY_CUTOFF_SECONDS = 5_400
DEFAULT_CLOSE_AFTER_SECONDS = 6_600
DEFAULT_SESSION_END_SECONDS = 7_200
MAX_CLOCK_DRIFT_SECONDS = 5.0


class ClockDriftError(RuntimeError):
    """Raised when wall and monotonic clocks disagree beyond the safe bound."""


class SessionPhase(str, Enum):
    ENTRY_ALLOWED = "ENTRY_ALLOWED"
    NO_ENTRY = "NO_ENTRY"
    CLOSE_ONLY = "CLOSE_ONLY"
    ENDED = "ENDED"


@dataclass(frozen=True)
class SessionLimits:
    entry_cutoff_seconds: int = DEFAULT_ENTRY_CUTOFF_SECONDS
    close_after_seconds: int = DEFAULT_CLOSE_AFTER_SECONDS
    end_seconds: int = DEFAULT_SESSION_END_SECONDS

    @classmethod
    def from_policy(cls, policy: dict[str, object]) -> SessionLimits:
        return cls(
            entry_cutoff_seconds=policy.get(
                "session_entry_cutoff_seconds", DEFAULT_ENTRY_CUTOFF_SECONDS
            ),
            close_after_seconds=policy.get(
                "session_close_after_seconds", DEFAULT_CLOSE_AFTER_SECONDS
            ),
            end_seconds=policy.get("session_end_seconds", DEFAULT_SESSION_END_SECONDS),
        )

    @classmethod
    def from_deadlines(
        cls,
        *,
        armed_at: datetime,
        entry_cutoff_at: datetime,
        close_after_at: datetime,
        end_at: datetime,
    ) -> SessionLimits:
        armed = utc_datetime(armed_at)
        durations = tuple(
            int((utc_datetime(deadline) - armed).total_seconds())
            for deadline in (entry_cutoff_at, close_after_at, end_at)
        )
        if any(
            (utc_datetime(deadline) - armed).total_seconds() != duration
            for deadline, duration in zip(
                (entry_cutoff_at, close_after_at, end_at), durations
            )
        ):
            raise ValueError("pilot session deadlines must use whole seconds")
        return cls(*durations)

    def __post_init__(self) -> None:
        values = (
            self.entry_cutoff_seconds,
            self.close_after_seconds,
            self.end_seconds,
        )
        if any(type(value) is not int for value in values):
            raise ValueError("pilot session limits must be integer seconds")
        if not (
            0 < self.entry_cutoff_seconds <= DEFAULT_ENTRY_CUTOFF_SECONDS
            and self.entry_cutoff_seconds <= self.close_after_seconds
            <= DEFAULT_CLOSE_AFTER_SECONDS
            and self.close_after_seconds <= self.end_seconds <= DEFAULT_SESSION_END_SECONDS
        ):
            raise ValueError("pilot session limits exceed the pinned safe window")


DEFAULT_SESSION_LIMITS = SessionLimits()


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("pilot session time must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class SessionTimer:
    """Deadline evaluator anchored to durable ARM T0 and this process's monotonic clock.

    On process restart this object is reconstructed from the immutable ARM event.
    The remaining deadline therefore decreases with wall time instead of starting
    a fresh 90/110/120 minute interval.
    """

    armed_at: datetime
    wall_anchor: datetime
    monotonic_anchor: float
    elapsed_anchor_seconds: float
    limits: SessionLimits = SessionLimits()

    @classmethod
    def start(
        cls,
        armed_at: datetime,
        *,
        wall_now: datetime,
        monotonic_now: float,
        limits: SessionLimits = DEFAULT_SESSION_LIMITS,
    ) -> SessionTimer:
        armed = utc_datetime(armed_at)
        wall = utc_datetime(wall_now)
        elapsed = (wall - armed).total_seconds()
        if elapsed < -MAX_CLOCK_DRIFT_SECONDS:
            raise ClockDriftError("worker wall clock predates durable ARM T0")
        if elapsed < 0:
            elapsed = 0.0
        return cls(armed, wall, float(monotonic_now), elapsed, limits)

    def elapsed_seconds(self, *, wall_now: datetime, monotonic_now: float) -> float:
        wall = utc_datetime(wall_now)
        wall_elapsed = (wall - self.armed_at).total_seconds()
        mono_elapsed = self.elapsed_anchor_seconds + float(monotonic_now) - self.monotonic_anchor
        if mono_elapsed < -MAX_CLOCK_DRIFT_SECONDS or abs(wall_elapsed - mono_elapsed) > MAX_CLOCK_DRIFT_SECONDS:
            raise ClockDriftError("wall and monotonic pilot session clocks disagree")
        if wall_elapsed < 0:
            raise ClockDriftError("worker wall clock predates durable ARM T0")
        return max(wall_elapsed, mono_elapsed)

    def phase(
        self,
        *,
        wall_now: datetime,
        monotonic_now: float,
        entry_count: int,
        exchange_flat: bool,
        open_algo_count: int,
        ledger_in_sync: bool,
    ) -> SessionPhase:
        if entry_count < 0 or open_algo_count < 0:
            raise ValueError("pilot session counters cannot be negative")
        elapsed = self.elapsed_seconds(wall_now=wall_now, monotonic_now=monotonic_now)
        if elapsed >= self.limits.end_seconds:
            if exchange_flat and open_algo_count == 0 and ledger_in_sync:
                return SessionPhase.ENDED
            return SessionPhase.CLOSE_ONLY
        if elapsed >= self.limits.close_after_seconds:
            return SessionPhase.CLOSE_ONLY
        if entry_count == 0 and elapsed >= self.limits.entry_cutoff_seconds:
            return SessionPhase.NO_ENTRY
        return SessionPhase.ENTRY_ALLOWED

    def readback(self, phase: SessionPhase) -> dict[str, str]:
        return {
            "armed_at": self.armed_at.isoformat().replace("+00:00", "Z"),
            "entry_cutoff_at": (self.armed_at + timedelta(seconds=self.limits.entry_cutoff_seconds)).isoformat().replace("+00:00", "Z"),
            "close_after_at": (self.armed_at + timedelta(seconds=self.limits.close_after_seconds)).isoformat().replace("+00:00", "Z"),
            "end_at": (self.armed_at + timedelta(seconds=self.limits.end_seconds)).isoformat().replace("+00:00", "Z"),
            "stage": phase.value,
        }
