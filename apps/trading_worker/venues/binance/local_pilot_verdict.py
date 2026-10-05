"""Control-Plane to Worker signed pilot readiness verdict protocol (Decision D3).

Inside Docker, the Worker cannot access host git, gh, or attestation files.
The Control Plane validates the 5 Track C attestations on the host and issues
a short-lived, HMAC-signed verdict bound to the campaignId, gitSha, and 5-field
Track C binding. The Worker validates this verdict fail-closed before ARM and
before every order submission.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
from typing import Any

from scripts.local_pilot_track_c import CLASSES

_ACTIVE_VERDICT: dict[str, Any] | None = None
REQUIRED_VERDICT_FIELDS = (
    "verdict", "campaignId", "gitSha", "binding",
    "verifiedClasses", "issuedAt", "expiresAt", "signature",
)
REQUIRED_BINDING_FIELDS = (
    "gitSha", "sourceSha256", "dependencySha256", "migrationSha256", "pilotPolicySha256",
)
HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
GIT_SHA_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


def canonical_verdict_bytes(payload: dict[str, Any]) -> bytes:
    """Produce deterministic byte representation for HMAC verification."""
    clean = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_verdict(payload: dict[str, Any], token: str) -> str:
    """Compute HMAC-SHA256 signature over the canonical payload."""
    raw = canonical_verdict_bytes(payload)
    return hmac.new(token.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_pilot_readiness_verdict(
    verdict: dict[str, Any],
    token: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate signed readiness verdict against Worker environment and current time."""
    if not isinstance(verdict, dict):
        return False, "VERDICT_PAYLOAD_INVALID", {}
    if not all(k in verdict for k in REQUIRED_VERDICT_FIELDS):
        return False, "VERDICT_FIELDS_INCOMPLETE", {}
    if verdict.get("verdict") != "READY":
        return False, "VERDICT_STATUS_NOT_READY", {}

    sig = verdict.get("signature")
    if not isinstance(sig, str) or not HEX_SHA256.fullmatch(sig):
        return False, "VERDICT_SIGNATURE_INVALID", {}
    expected_sig = sign_verdict(verdict, token)
    if not hmac.compare_digest(sig, expected_sig):
        return False, "VERDICT_SIGNATURE_INVALID", {}

    # Campaign ID check
    expected_campaign = str(os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "")).strip()
    if not expected_campaign or verdict.get("campaignId") != expected_campaign:
        return False, "VERDICT_CAMPAIGN_MISMATCH", {}

    # Git SHA check
    expected_git_sha = str(os.getenv("LOCAL_LIVE_PILOT_GIT_SHA", "")).strip()
    if expected_git_sha and verdict.get("gitSha") != expected_git_sha:
        return False, "VERDICT_GIT_SHA_MISMATCH", {}

    # Binding check
    binding = verdict.get("binding")
    if not isinstance(binding, dict) or not all(k in binding for k in REQUIRED_BINDING_FIELDS):
        return False, "VERDICT_BINDING_INVALID", {}
    if binding.get("gitSha") != verdict.get("gitSha"):
        return False, "VERDICT_BINDING_MISMATCH", {}

    expected_source_hash = str(os.getenv("LOCAL_LIVE_PILOT_SOURCE_HASH", "")).strip()
    if expected_source_hash and binding.get("sourceSha256") != expected_source_hash:
        return False, "VERDICT_BINDING_MISMATCH", {}

    expected_dep_hash = str(os.getenv("LOCAL_LIVE_PILOT_DEPENDENCY_HASH", "")).strip()
    if expected_dep_hash and binding.get("dependencySha256") != expected_dep_hash:
        return False, "VERDICT_BINDING_MISMATCH", {}

    expected_mig_hash = str(os.getenv("LOCAL_LIVE_PILOT_MIGRATION_HASH", "")).strip()
    if expected_mig_hash and binding.get("migrationSha256") != expected_mig_hash:
        return False, "VERDICT_BINDING_MISMATCH", {}

    expected_policy_hash = str(os.getenv("LOCAL_LIVE_PILOT_RISK_POLICY_HASH", "")).strip()
    if expected_policy_hash and binding.get("pilotPolicySha256") != expected_policy_hash:
        return False, "VERDICT_BINDING_MISMATCH", {}

    # Verified classes check
    verified_classes = verdict.get("verifiedClasses")
    if not isinstance(verified_classes, list) or not set(CLASSES).issubset(set(verified_classes)):
        return False, "VERDICT_CLASSES_INCOMPLETE", {}

    # Timestamps & TTL check
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        issued_at = datetime.fromisoformat(str(verdict["issuedAt"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        expires_at = datetime.fromisoformat(str(verdict["expiresAt"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return False, "VERDICT_TIMESTAMPS_INVALID", {}

    # Clock skew tolerance: 10 seconds in the future
    if (issued_at - current_time).total_seconds() > 10:
        return False, "VERDICT_NOT_YET_VALID", {}

    # Expired check
    if current_time > expires_at:
        return False, "VERDICT_EXPIRED", {}

    return True, "TRACK_C_VERDICT_VERIFIED", {
        "campaignId": verdict["campaignId"],
        "gitSha": verdict["gitSha"],
        "issuedAt": verdict["issuedAt"],
        "expiresAt": verdict["expiresAt"],
    }


def set_active_pilot_verdict(verdict: dict[str, Any] | None) -> None:
    """Store the most recent signed verdict in Worker memory."""
    global _ACTIVE_VERDICT
    _ACTIVE_VERDICT = verdict


def get_active_pilot_verdict() -> dict[str, Any] | None:
    """Retrieve the current in-memory signed verdict."""
    return _ACTIVE_VERDICT
