"""Explicitly approved one-order Testnet lifecycle contract test."""

import json
import os
import time
from pathlib import Path

import pytest

from apps.trading_worker.venues.binance.manual_testnet import manual_testnet_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.contract_mutating]


@pytest.fixture
def mutation_approval():
    enabled = os.getenv("TESTNET_MANUAL_TRIAL_APPROVED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        pytest.skip("Manual Testnet mutation is not explicitly approved")
    if os.getenv("BINANCE_TESTNET", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        pytest.skip("BINANCE_TESTNET is not explicitly enabled")
    if not os.getenv("BINANCE_TESTNET_API_KEY", "").strip() or not os.getenv(
        "BINANCE_TESTNET_API_SECRET", ""
    ).strip():
        pytest.skip("Testnet credentials not found")


async def test_worker_owned_bounded_testnet_lifecycle(mutation_approval):
    trial_started_ns = time.time_ns()
    artifact = await manual_testnet_workflow()

    assert artifact is not None
    assert artifact["environment"] == "BINANCE_TESTNET"
    assert artifact["status"] == "PASS"
    assert artifact["reconciliation_status"] == "IN_SYNC"
    assert artifact["diff_count"] == 0
    assert artifact["position_after"] == []
    assert artifact["modify_result"] == "VERIFIED"
    assert artifact["cancel_result"] == "VERIFIED"

    # A previous successful trial must not satisfy this contract.  Require the
    # sanitized artifact written by this invocation and bind it to the same
    # source SHA returned by the worker-owned workflow.
    current_artifacts = [
        path
        for path in Path("artifacts").glob("testnet-trial-*.json")
        if path.stat().st_mtime_ns >= trial_started_ns
    ]
    assert len(current_artifacts) == 1
    written_artifact = json.loads(current_artifacts[0].read_text(encoding="utf-8"))
    assert written_artifact["build_sha"] == artifact["build_sha"]
    assert written_artifact["environment"] == "BINANCE_TESTNET"
    assert written_artifact["status"] == "PASS"
