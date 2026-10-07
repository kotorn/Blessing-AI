"""Tests for Control-Plane to Worker signed pilot readiness verdict (Decision D3)."""
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from apps.trading_worker.venues.binance.local_pilot_verdict import (
    canonical_verdict_bytes,
    sign_verdict,
    verify_pilot_readiness_verdict,
    set_active_pilot_verdict,
    get_active_pilot_verdict,
    get_pilot_verdict_status,
)
from apps.trading_worker.venues.binance.local_pilot_readiness import local_live_pilot_readiness
from apps.trading_worker.main import app, get_default_state


TOKEN = "test-worker-identity-secret-token-12345"
CAMPAIGN_ID = "pilot-test-campaign-12345678"
GIT_SHA = "a" * 40
SOURCE_HASH = "b" * 64
DEP_HASH = "c" * 64
MIG_HASH = "d" * 64
POLICY_HASH = "e" * 64
CLASSES = ["CHECKS", "REVIEW_AUTH_RELEASE", "REVIEW_ORDER_RISK", "REVIEW_PERSISTENCE", "TESTNET_ETHUSDC"]


def sample_verdict(expires_in_seconds: int = 3600, delta_issued: int | None = None) -> dict:
    now = datetime.now(timezone.utc)
    if delta_issued is None:
        delta_issued = (expires_in_seconds - 300) if expires_in_seconds < 0 else 0
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
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    set_active_pilot_verdict(None)
    yield
    set_active_pilot_verdict(None)


def test_golden_fixture_canonicalization_and_signature():
    golden_path = Path("tests/fixtures/pilot_verdict_golden.json")
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    raw_payload = golden["rawPayload"]
    expected_canonical = golden["canonicalJson"]
    expected_sig = golden["expectedSignature"]
    token = golden["token"]

    canonical_bytes = canonical_verdict_bytes(raw_payload)
    assert canonical_bytes.decode("utf-8") == expected_canonical

    computed_sig = sign_verdict(raw_payload, token)
    assert computed_sig == expected_sig


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


def test_verdict_mandatory_binding_env_vars_missing_rejected(monkeypatch):
    verdict = sample_verdict()
    monkeypatch.delenv("LOCAL_LIVE_PILOT_SOURCE_HASH", raising=False)
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_BINDING_MISMATCH"

    monkeypatch.setenv("LOCAL_LIVE_PILOT_SOURCE_HASH", SOURCE_HASH)
    monkeypatch.delenv("LOCAL_LIVE_PILOT_GIT_SHA", raising=False)
    ok2, reason2, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok2 is False
    assert reason2 == "VERDICT_GIT_SHA_MISMATCH"


def test_verdict_max_ttl_exceeded_rejected():
    # TTL > 3600 seconds
    verdict = sample_verdict(expires_in_seconds=3601, delta_issued=0)
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_TTL_EXCEEDED"


def test_verdict_expires_before_issued_rejected():
    # expiresAt <= issuedAt
    now = datetime.now(timezone.utc)
    verdict = sample_verdict()
    verdict["issuedAt"] = now.isoformat()
    verdict["expiresAt"] = (now - timedelta(seconds=10)).isoformat()
    verdict["signature"] = sign_verdict({k: v for k, v in verdict.items() if k != "signature"}, TOKEN)
    ok, reason, _ = verify_pilot_readiness_verdict(verdict, TOKEN)
    assert ok is False
    assert reason == "VERDICT_TIMESTAMPS_INVALID"


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


def test_set_active_pilot_verdict_retains_newest_valid():
    v1 = sample_verdict(expires_in_seconds=1800, delta_issued=-100)
    set_active_pilot_verdict(v1)
    assert get_active_pilot_verdict() == v1

    # Incoming invalid verdict does not clobber v1
    v_bad = sample_verdict()
    v_bad["signature"] = "0" * 64
    set_active_pilot_verdict(v_bad)
    assert get_active_pilot_verdict() == v1

    # Incoming older verdict does not replace v1
    v_older = sample_verdict(expires_in_seconds=1800, delta_issued=-200)
    set_active_pilot_verdict(v_older)
    assert get_active_pilot_verdict() == v1

    # Incoming newer valid verdict replaces v1
    v2 = sample_verdict(expires_in_seconds=3600, delta_issued=0)
    set_active_pilot_verdict(v2)
    assert get_active_pilot_verdict() == v2

    # Setting None explicitly clears
    set_active_pilot_verdict(None)
    assert get_active_pilot_verdict() is None


def test_get_pilot_verdict_status():
    set_active_pilot_verdict(None)
    assert get_pilot_verdict_status() == "NOT_AVAILABLE"

    valid = sample_verdict(expires_in_seconds=1800)
    set_active_pilot_verdict(valid)
    assert get_pilot_verdict_status() == "VALID"

    expired = sample_verdict(expires_in_seconds=-10)
    set_active_pilot_verdict(None)
    set_active_pilot_verdict(expired)
    assert get_pilot_verdict_status() == "EXPIRED"

    bad = sample_verdict()
    bad["signature"] = "0" * 64
    set_active_pilot_verdict(None)
    set_active_pilot_verdict(bad)
    assert get_pilot_verdict_status() == "INVALID"


def test_local_live_pilot_readiness_uses_active_verdict_delegated():
    verdict = sample_verdict()
    set_active_pilot_verdict(verdict)
    readiness = local_live_pilot_readiness()
    assert readiness["can_start"] is True
    assert readiness["status"] == "READY"
    assert readiness["blockers"] == []
    assert readiness["ci_attestation"]["reason"] == "DELEGATED"
    assert readiness["provenance"]["local_checks"] == "DELEGATED"
    assert readiness["provenance"]["reviews"] == "DELEGATED"
    assert readiness["provenance"]["testnet"] == "DELEGATED"


def test_local_live_pilot_readiness_blocks_on_invalid_verdict():
    verdict = sample_verdict(expires_in_seconds=-10)
    set_active_pilot_verdict(verdict)
    readiness = local_live_pilot_readiness()
    assert readiness["can_start"] is False
    assert readiness["status"] == "BLOCKED"
    assert any("VERDICT" in b for b in readiness["blockers"])


def test_endpoints_verdict_heartbeat_and_expiry_flip(monkeypatch):
    import unittest.mock as mock
    mock_engine = mock.MagicMock()
    monkeypatch.setattr("apps.trading_worker.main.WORKER_ENGINE", mock_engine)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    # 1. /local-pilot/verdict with valid and invalid verdict
    valid_verdict = sample_verdict(expires_in_seconds=300)
    resp = client.post("/local-pilot/verdict", json={"verdict": valid_verdict}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert get_pilot_verdict_status() == "VALID"

    invalid_verdict = sample_verdict()
    invalid_verdict["signature"] = "0" * 64
    resp_bad = client.post("/local-pilot/verdict", json={"verdict": invalid_verdict}, headers=headers)
    assert resp_bad.status_code == 400
    assert "LOCAL_PILOT_VERDICT_REJECTED" in resp_bad.json()["detail"]

    # 2. Check /state reports pilot_verdict_status
    mock_engine.get_state.side_effect = get_default_state
    state_resp = client.get("/state", headers=headers)
    assert state_resp.status_code == 200
    assert state_resp.json()["pilot_verdict_status"] == "VALID"

    # 3. /supervisor/heartbeat updates verdict and accepts null clearing
    new_verdict = sample_verdict(expires_in_seconds=600)
    hb_resp = client.post("/supervisor/heartbeat", json={"pilotReadinessVerdict": new_verdict}, headers=headers)
    assert hb_resp.status_code == 200
    assert get_active_pilot_verdict() == new_verdict

    hb_clear_resp = client.post("/supervisor/heartbeat", json={"pilotReadinessVerdict": None}, headers=headers)
    assert hb_clear_resp.status_code == 200
    assert get_active_pilot_verdict() is None
    assert get_pilot_verdict_status() == "NOT_AVAILABLE"

    # 4. Expiry flip in /state
    expired_verdict = sample_verdict(expires_in_seconds=-10)
    set_active_pilot_verdict(expired_verdict)
    assert client.get("/state", headers=headers).json()["pilot_verdict_status"] == "EXPIRED"


def test_arm_sets_active_verdict(monkeypatch):
    import unittest.mock as mock
    mock_engine = mock.MagicMock()
    mock_engine.arm = mock.AsyncMock(return_value=(True, "ok"))
    mock_engine.get_state.side_effect = get_default_state
    monkeypatch.setattr("apps.trading_worker.main.WORKER_ENGINE", mock_engine)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    verdict = sample_verdict(expires_in_seconds=500)
    set_active_pilot_verdict(None)
    assert get_active_pilot_verdict() is None

    resp = client.post(
        "/arm",
        json={
            "executionMode": "LIVE",
            "instruments": ["ETHUSDC"],
            "launchPolicy": "LIVE_RESEARCH_PILOT",
            "pilotCampaignId": CAMPAIGN_ID,
            "pilotReadinessVerdict": verdict,
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert get_active_pilot_verdict() == verdict
    assert get_pilot_verdict_status() == "VALID"

