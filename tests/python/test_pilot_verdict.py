"""Tests for Control-Plane to Worker signed pilot readiness verdict (Decision D3)."""
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import os
import pytest

from apps.trading_worker.venues.binance.local_pilot_verdict import (
    canonical_verdict_bytes,
    sign_verdict,
    verify_pilot_readiness_verdict,
    set_active_pilot_verdict,
    get_active_pilot_verdict,
)
from apps.trading_worker.venues.binance.local_pilot_readiness import local_live_pilot_readiness


TOKEN = "test-worker-identity-secret-token-12345"
CAMPAIGN_ID = "pilot-test-campaign-12345678"
GIT_SHA = "a" * 40
SOURCE_HASH = "b" * 64
DEP_HASH = "c" * 64
MIG_HASH = "d" * 64
POLICY_HASH = "e" * 64
CLASSES = ["CHECKS", "REVIEW_AUTH_RELEASE", "REVIEW_ORDER_RISK", "REVIEW_PERSISTENCE", "TESTNET_ETHUSDC"]


def sample_verdict(expires_in_seconds: int = 3600, delta_issued: int = 0) -> dict:
    now = datetime.now(timezone.utc)
    issued_at = (now + timedelta(seconds=delta_issued)).isoformat()
    expires_at = (now + timedelta(seconds=expires_in_seconds)).isoformat()
    binding = {
        "gitSha": GIT_SHA,
        "sourceSha256": SOURCE_HASH,
        "dependencySha256": DEP_HASH,
        "migrationSha256": MIG_HASH,
        "pilotPolicySha256": POLICY_HASH,
    }
    payload = {
        "verdict": "READY",
        "campaignId": CAMPAIGN_ID,
        "gitSha": GIT_SHA,
        "binding": binding,
        "verifiedClasses": CLASSES,
        "issuedAt": issued_at,
        "expiresAt": expires_at,
    }
    sig = sign_verdict(payload, TOKEN)
    return {**payload, "signature": sig}


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    monkeypatch.setenv("WORKER_IDENTITY_TOKEN", TOKEN)
    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", CAMPAIGN_ID)
    monkeypatch.setenv("LOCAL_LIVE_PILOT_GIT_SHA", GIT_SHA)
    monkeypatch.setenv("LOCAL_LIVE_PILOT_SOURCE_HASH", SOURCE_HASH)
    monkeypatch.setenv("LOCAL_LIVE_PILOT_DEPENDENCY_HASH", DEP_HASH)
    monkeypatch.setenv("LOCAL_LIVE_PILOT_MIGRATION_HASH", MIG_HASH)
    monkeypatch.setenv("LOCAL_LIVE_PILOT_RISK_POLICY_HASH", POLICY_HASH)
    set_active_pilot_verdict(None)
    yield
    set_active_pilot_verdict(None)


def test_valid_verdict_verification():
    verdict = sample_verdict()
    ok, reason, details = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is True
    assert reason == "TRACK_C_VERDICT_VERIFIED"
    assert details["campaignId"] == CAMPAIGN_ID


def test_verdict_tampered_signature_rejected():
    verdict = sample_verdict()
    verdict["signature"] = "0" * 64
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_SIGNATURE_INVALID"


def test_verdict_tampered_campaign_rejected():
    verdict = sample_verdict()
    verdict["campaignId"] = "pilot-other-campaign-99999999"
    # Even if signature matches the tampered payload:
    verdict["signature"] = sign_verdict({k: v for k, v in verdict.items() if k != "signature"}, TOKEN)
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_CAMPAIGN_MISMATCH"


def test_verdict_tampered_git_sha_rejected():
    verdict = sample_verdict()
    verdict["gitSha"] = "f" * 40
    verdict["signature"] = sign_verdict({k: v for k, v in verdict.items() if k != "signature"}, TOKEN)
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_GIT_SHA_MISMATCH"


def test_verdict_tampered_binding_rejected():
    verdict = sample_verdict()
    verdict["binding"]["sourceSha256"] = "f" * 64
    verdict["signature"] = sign_verdict({k: v for k, v in verdict.items() if k != "signature"}, TOKEN)
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_BINDING_MISMATCH"


def test_verdict_expired_rejected():
    verdict = sample_verdict(expires_in_seconds=-10)  # expired 10 seconds ago
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_EXPIRED"


def test_verdict_future_issued_rejected():
    verdict = sample_verdict(expires_in_seconds=3600, delta_issued=60)  # issued 60 seconds in the future
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_NOT_YET_VALID"


def test_verdict_missing_classes_rejected():
    verdict = sample_verdict()
    verdict["verifiedClasses"] = CLASSES[:4]  # missing TESTNET_ETHUSDC
    verdict["signature"] = sign_verdict({k: v for k, v in verdict.items() if k != "signature"}, TOKEN)
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_CLASSES_INCOMPLETE"


def test_local_live_pilot_readiness_uses_active_verdict():
    verdict = sample_verdict()
    set_active_pilot_verdict(verdict)
    readiness = local_live_pilot_readiness()
    assert readiness["can_start"] is True
    assert readiness["status"] == "READY"
    assert readiness["blockers"] == []


def test_local_live_pilot_readiness_blocks_on_invalid_verdict():
    verdict = sample_verdict(expires_in_seconds=-10)
    set_active_pilot_verdict(verdict)
    readiness = local_live_pilot_readiness()
    assert readiness["can_start"] is False
    assert readiness["status"] == "BLOCKED"
    assert any("VERDICT" in b for b in readiness["blockers"])
