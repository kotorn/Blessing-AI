"""Deprecated Testnet-only diagnostic entrypoint.

Use the isolated official ``binance-cli`` research wrapper for observations.
This compatibility script intentionally has no Mainnet branch, no direct REST
signing, and never prints credential material.
"""

from __future__ import annotations

import json
import os

from apps.trading_worker.research.binance_cli import (
    BinanceCliResearchRunner,
    BinanceCliPolicyError,
    ReadOnlyCheck,
)


def run_binance_connectivity() -> int:
    """Run safe, read-only Testnet checks and print sanitized observations."""

    if os.getenv("BINANCE_TESTNET", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    } or os.getenv("BINANCE_API_ENV", "") != "testnet":
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "Explicit BINANCE_TESTNET=true and BINANCE_API_ENV=testnet are required",
                    "mainnet_enabled": False,
                }
            )
        )
        return 2

    runner = BinanceCliResearchRunner()
    statuses: list[str] = []
    for check in (
        ReadOnlyCheck.SERVER_TIME,
        ReadOnlyCheck.EXCHANGE_INFO,
        ReadOnlyCheck.ACCOUNT,
        ReadOnlyCheck.POSITION_MODE,
    ):
        try:
            result = runner.run(check)
        except BinanceCliPolicyError as exc:
            print(json.dumps({"check": check.value, "status": "BLOCKED", "error": str(exc)}))
            return 2
        statuses.append(result.status)
        print(json.dumps(result.as_dict(), ensure_ascii=False))

    return 0 if all(status in {"PASS", "NOT_RUN"} for status in statuses) else 3


if __name__ == "__main__":
    raise SystemExit(run_binance_connectivity())
