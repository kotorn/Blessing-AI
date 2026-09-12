"""Explicitly approved one-order Testnet lifecycle contract test."""

import os
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
    artifact = await manual_testnet_workflow()

    assert artifact is not None
    assert artifact["environment"] == "BINANCE_TESTNET"
    assert artifact["status"] == "PASS"
    assert artifact["reconciliation_status"] == "IN_SYNC"
    assert artifact["diff_count"] == 0
    assert artifact["position_after"] == []
    assert artifact["modify_result"] == "VERIFIED"
    assert artifact["cancel_result"] == "VERIFIED"
    assert list(Path("artifacts").glob("testnet-trial-*.json"))
