"""Fixed Control Plane client for queued, read-only Mainnet preflight jobs."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .models import JobRecord

_CANONICAL_SERVICE_URL = re.compile(
    r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])*$"
)


class MainnetPreflightError(RuntimeError):
    """Base error for the fixed read-only preflight path."""

    code = "mainnet_preflight_failed"
    retryable = False
    uncertain = False


class MainnetPreflightUnavailable(MainnetPreflightError):
    """Transport/availability failure; a bounded retry is safe."""

    code = "mainnet_preflight_unavailable"
    retryable = True


class MainnetPreflightRejected(MainnetPreflightError):
    """The fixed Worker path returned a failed release preflight."""

    code = "mainnet_preflight_rejected"


def _safe_text(value: object, limit: int = 500) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")[:limit]


def _sanitize_response(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MainnetPreflightUnavailable("Control Plane returned a non-object preflight response")
    checks: list[dict[str, Any]] = []
    raw_checks = payload.get("checks")
    if isinstance(raw_checks, list):
        for item in raw_checks:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            if status not in {"PASS", "FAIL"}:
                continue
            check_id = _safe_text(item.get("id"), 80)
            check_name = _safe_text(item.get("name"), 160)
            if not check_id or not check_name:
                continue
            checks.append(
                {
                    "id": check_id,
                    "name": check_name,
                    "required": item.get("required") is not False,
                    "status": status,
                    "message": _safe_text(item.get("message")),
                }
            )
    attempts = payload.get("orderSubmissionAttempts", payload.get("order_submission_attempts"))
    endpoint_attempts = payload.get("orderEndpointAttempts", payload.get("order_endpoint_attempts"))
    try:
        attempts_int = int(attempts)
        endpoint_attempts_int = int(endpoint_attempts)
    except (TypeError, ValueError) as exc:
        raise MainnetPreflightUnavailable("Control Plane preflight counters are invalid") from exc
    return {
        "executionMode": "LIVE",
        "preflightOnly": payload.get("preflightOnly") is True,
        "preflightPassed": payload.get("preflightPassed") is True,
        "orderSubmissionAttempts": attempts_int,
        "orderEndpointAttempts": endpoint_attempts_int,
        "observedAt": _safe_text(payload.get("observedAt"), 64),
        "checks": checks,
    }


class MainnetPreflightClient:
    """Call only the fixed Control Plane internal preflight route.

    The client obtains an ID token from the attached runtime identity. It does
    not accept a token, Binance credential, arbitrary URL, or custom path from
    a queue job, and it never invokes an exchange order endpoint.
    """

    def __init__(
        self,
        control_plane_url: str,
        *,
        token_provider: Callable[[str], str] | None = None,
        timeout_sec: int = 90,
    ) -> None:
        normalized = control_plane_url.strip().rstrip("/")
        if (
            len(normalized) > 253
            or not _CANONICAL_SERVICE_URL.fullmatch(normalized)
            or "." not in normalized.removeprefix("https://")
        ):
            raise ValueError("AGY_CONTROL_PLANE_URL must be a canonical HTTPS service URL")
        self.control_plane_url = normalized
        self.token_provider = token_provider
        self.timeout_sec = max(1, min(int(timeout_sec), 300))

    def _token(self) -> str:
        if self.token_provider is not None:
            token = self.token_provider(self.control_plane_url)
        else:
            try:
                from google.auth.transport.requests import Request
                from google.oauth2 import id_token

                token = id_token.fetch_id_token(Request(), self.control_plane_url)
            except Exception as exc:  # noqa: BLE001 - provider details are not safe to expose
                raise MainnetPreflightUnavailable("attached Control Plane identity is unavailable") from exc
        if not isinstance(token, str) or not token.strip():
            raise MainnetPreflightUnavailable("attached Control Plane identity token is empty")
        return token.strip()

    def __call__(self, job: JobRecord) -> dict[str, Any]:
        if job.job_kind.value != "MAINNET_PREFLIGHT":
            raise MainnetPreflightRejected("fixed preflight client received an unexpected job kind")
        token = self._token()
        request = urllib.request.Request(
            f"{self.control_plane_url}/internal/release/preflight",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Release-Path": "queued-mainnet-preflight",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read(256_000)
        except urllib.error.HTTPError as exc:
            if exc.code in {408, 425, 429, 500, 502, 503, 504}:
                raise MainnetPreflightUnavailable(
                    f"Control Plane preflight unavailable (HTTP {exc.code})"
                ) from exc
            raise MainnetPreflightRejected(
                f"Control Plane preflight rejected (HTTP {exc.code})"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MainnetPreflightUnavailable("Control Plane preflight transport failed") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MainnetPreflightUnavailable("Control Plane preflight returned invalid JSON") from exc
        evidence = _sanitize_response(payload)
        if not evidence["checks"]:
            raise MainnetPreflightRejected("Mainnet preflight returned no sanitized checks")
        if not (
            evidence["preflightPassed"] is True
            and evidence["preflightOnly"] is True
            and evidence["orderSubmissionAttempts"] == 0
            and evidence["orderEndpointAttempts"] == 0
        ):
            raise MainnetPreflightRejected("Mainnet preflight did not pass its read-only zero-order contract")
        return evidence
