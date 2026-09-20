"""Explicitly approved supervised autonomous Testnet soak contract test."""

import json
import os
import time
from pathlib import Path

import pytest

from apps.trading_worker.venues.binance.soak_runner import run_supervised_soak

pytestmark = [pytest.mark.asyncio, pytest.mark.contract_soak]


@pytest.fixture
def soak_approval():
    if os.getenv("AUTONOMOUS_TESTNET_SOAK_APPROVED", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        pytest.skip("Autonomous Testnet soak is not explicitly approved")
    if os.getenv("TESTNET_LAUNCH_APPROVED", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        pytest.skip("TESTNET_LAUNCH_APPROVED is not enabled")
    if os.getenv("BINANCE_TESTNET", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        pytest.skip("BINANCE_TESTNET is not enabled")
    if not os.getenv("BINANCE_TESTNET_API_KEY", "").strip() or not os.getenv(
        "BINANCE_TESTNET_API_SECRET", ""
    ).strip():
        pytest.skip("Testnet credentials not found")


async def test_supervised_autonomous_testnet_soak(soak_approval):
    trial_started_ns = time.time_ns()
    artifact = await run_supervised_soak(duration_sec=15.0)

    assert artifact is not None
    assert artifact["environment"] == "BINANCE_TESTNET"
    assert artifact["status"] == "PASS"
    assert artifact["hard_stop_triggered"] is False
    assert artifact["final_positions"] == []

    current_artifacts = [
        path
        for path in Path("artifacts").glob("testnet-soak-*.json")
        if path.stat().st_mtime_ns >= trial_started_ns
    ]
    assert len(current_artifacts) == 1
    written = json.loads(current_artifacts[0].read_text(encoding="utf-8"))
    assert written["build_sha"] == artifact["build_sha"]
    assert written["status"] == "PASS"
