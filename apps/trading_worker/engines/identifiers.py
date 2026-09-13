"""Stable identifiers for event-driven strategy lineage."""

from datetime import UTC, datetime


def event_time_token(timestamp: datetime) -> str:
    """Return a filesystem/transport-safe token for an event timestamp.

    Strategy intent IDs are part of the audit lineage.  They must therefore
    use the observed event time instead of process wall-clock time, otherwise
    the same historical input produces different lineage on every run.
    """

    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise ValueError("event timestamp must be timezone-aware")
    return timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
