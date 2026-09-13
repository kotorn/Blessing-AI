"""Supervised, bounded autonomous Testnet soak runner."""

import asyncio
import json
import logging
import os
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from domain.models import utc_now
from apps.trading_worker.evidence import BuildEvidence, TestnetSoakArtifact
from apps.trading_worker.main import ArmRequest, TradingWorkerApp
from apps.trading_worker.venues.binance.models import ConnectionState

logger = logging.getLogger("soak_runner")


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _current_sha() -> str:
    try:
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        working_tree = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        if working_tree:
            raise RuntimeError("ABORT: soak runner requires a clean working tree")
        return head_sha
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "ABORT: cannot verify current Git SHA for the soak runner"
        ) from exc


def _require_prerequisite_evidence(build_sha: str) -> BuildEvidence:
    """Require current-SHA read-only AND manual trial evidence before running soak."""
    for evidence_path in (
        Path("build_metadata.json"),
        Path("artifacts") / "build-evidence.json",
    ):
        if not evidence_path.exists():
            continue
        try:
            evidence = BuildEvidence.model_validate(
                json.loads(evidence_path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            logger.warning("Ignoring invalid build evidence at %s: %s", evidence_path, exc)
            continue
        if (
            evidence.build_sha == build_sha
            and evidence.local_non_secret_tests_verified
            and evidence.readonly_contract_verified
            and evidence.manual_trial_verified
            and evidence.manual_trial_sha == build_sha
        ):
            return evidence
    raise RuntimeError(
        "ABORT: current-SHA read-only AND manual trial Testnet evidence are required "
        "before the autonomous soak run"
    )


async def run_supervised_soak(
    duration_sec: Optional[float] = None,
    max_decisions: Optional[int] = None,
    max_fills: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute a supervised bounded autonomous soak run."""
    if not _enabled("AUTONOMOUS_TESTNET_SOAK_APPROVED"):
        raise RuntimeError("ABORT: AUTONOMOUS_TESTNET_SOAK_APPROVED must be true")
    if not _enabled("TESTNET_LAUNCH_APPROVED"):
        raise RuntimeError("ABORT: TESTNET_LAUNCH_APPROVED must be true")
    if not _enabled("BINANCE_TESTNET"):
        raise RuntimeError("ABORT: BINANCE_TESTNET must be true")
    if not os.getenv("BINANCE_TESTNET_API_KEY", "").strip() or not os.getenv(
        "BINANCE_TESTNET_API_SECRET", ""
    ).strip():
        raise RuntimeError("ABORT: Testnet credentials are not configured")

    build_sha = _current_sha()
    _require_prerequisite_evidence(build_sha)

    duration = duration_sec or float(os.getenv("SOAK_DURATION_SEC", "300"))
    decision_limit = max_decisions or int(os.getenv("SOAK_MAX_DECISIONS", "20"))
    fill_limit = max_fills or int(os.getenv("SOAK_MAX_FILLS", "2"))
    symbol = "BTCUSDT"
    strategy = os.getenv("TESTNET_SOAK_STRATEGY", "grid")

    artifact: Dict[str, Any] = {
        "build_sha": build_sha,
        "environment": "BINANCE_TESTNET",
        "strategy": strategy,
        "symbol": symbol,
        "runtime_seconds": 0.0,
        "intent_count": 0,
        "risk_approved_count": 0,
        "risk_rejected_count": 0,
        "orders_submitted_count": 0,
        "fills_count": 0,
        "total_fees_usdt": 0.0,
        "slippage_bps": 0.0,
        "max_exposure_notional": 0.0,
        "stream_disconnect_count": 0,
        "stream_reconnect_count": 0,
        "reconciliation_runs_count": 0,
        "reconciliation_diffs_count": 0,
        "ambiguous_requests_count": 0,
        "duplicate_fills_count": 0,
        "final_positions": [],
        "hard_stop_triggered": False,
        "hard_stop_reason": None,
        "status": "FAIL",
    }

    worker = TradingWorkerApp(symbols=[symbol])
    start_time = time.monotonic()

    try:
        armed, reason = await worker.arm(
            ArmRequest(
                executionMode="TESTNET",
                instruments=[symbol],
                strategies={strategy: True},
                riskProfile="CONSERVATIVE",
            )
        )
        if not armed:
            raise RuntimeError(f"Worker ARM failed for soak: {reason}")

        adapter = worker.execution_adapter
        if adapter is None:
            raise RuntimeError("Worker armed without an execution adapter")

        readiness = worker.get_launch_readiness()
        if not readiness.get("testnet_autonomous_soak_ready"):
            raise RuntimeError(
                f"Worker reported testnet_autonomous_soak_ready == False: {readiness}"
            )

        logger.info(
            "Soak started: symbol=%s strategy=%s duration=%.1fs max_decisions=%d max_fills=%d",
            symbol,
            strategy,
            duration,
            decision_limit,
            fill_limit,
        )

        poll_interval = 2.0
        reconcile_interval = 10.0
        last_reconcile = time.monotonic()

        while time.monotonic() - start_time < duration:
            elapsed = time.monotonic() - start_time
            artifact["runtime_seconds"] = round(elapsed, 2)

            # Tripwire 1: Adapter connection state
            if adapter.connection_state != ConnectionState.READY:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = f"Adapter connection degraded: {adapter.connection_state}"
                break

            # Tripwire 2: Private user stream health
            if not adapter.private_stream_healthy:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Private stream reported unhealthy"
                break

            # Tripwire 3: Authentication truth
            if not adapter.authenticated or not adapter.capabilities.trade_authorized:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Authentication or trade authorization lost"
                break

            # Tripwire 4: Check fill limits
            fill_count = len(adapter.ledger.fills)
            artifact["fills_count"] = fill_count
            if fill_count > fill_limit:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = f"Fill count exceeded soak bound: {fill_count} > {fill_limit}"
                break

            # Tripwire 5: Check duplicate fills
            seen_trades = set()
            for fill in adapter.ledger.fills:
                key = (fill.symbol, fill.exchange_trade_id)
                if key in seen_trades:
                    artifact["duplicate_fills_count"] += 1
                    artifact["hard_stop_triggered"] = True
                    artifact["hard_stop_reason"] = f"Duplicate fill detected: {key}"
                    break
                seen_trades.add(key)
            if artifact["hard_stop_triggered"]:
                break

            # Periodic reconciliation
            if time.monotonic() - last_reconcile >= reconcile_interval:
                reconcile_status = await adapter.reconciliation.reconcile()
                artifact["reconciliation_runs_count"] += 1
                artifact["reconciliation_diffs_count"] += len(adapter.reconciliation.last_diffs)
                if reconcile_status != "IN_SYNC":
                    artifact["hard_stop_triggered"] = True
                    artifact["hard_stop_reason"] = f"Reconciliation diverged: {reconcile_status}"
                    break
                last_reconcile = time.monotonic()

            # Refresh market data
            await adapter.refresh_market_data([symbol])
            worker.last_market_event_at.update(adapter.last_market_event_at)

            await asyncio.sleep(poll_interval)

        # Final checks
        artifact["runtime_seconds"] = round(time.monotonic() - start_time, 2)
        final_reconcile = await adapter.reconciliation.reconcile()
        artifact["reconciliation_runs_count"] += 1
        artifact["reconciliation_diffs_count"] += len(adapter.reconciliation.last_diffs)
        
        active_positions = [
            p.model_dump()
            for p in await adapter.ledger.get_positions()
            if p.quantity != Decimal("0")
        ]
        artifact["final_positions"] = active_positions

        if artifact["hard_stop_triggered"]:
            logger.error("Soak hard-stop triggered: %s", artifact["hard_stop_reason"])
            await worker.set_pause_new_risk(True)
            await worker.disarm()
            await adapter.reconciliation.reconcile()
            artifact["status"] = "FAIL"
        elif final_reconcile != "IN_SYNC":
            artifact["hard_stop_triggered"] = True
            artifact["hard_stop_reason"] = f"Final reconciliation not IN_SYNC: {final_reconcile}"
            artifact["status"] = "FAIL"
        else:
            artifact["status"] = "PASS"
            logger.info("Soak completed successfully with zero hard-stop triggers.")

        return artifact
    finally:
        if worker.execution_adapter is not None:
            try:
                await worker.disarm()
            except Exception as exc:
                logger.error("Error disarming worker after soak: %s", exc)

        timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        artifact_path = Path("artifacts") / f"testnet-soak-{build_sha[:7]}-{timestamp}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(
                TestnetSoakArtifact.model_validate(artifact).model_dump(), indent=2
            ),
            encoding="utf-8",
        )
        logger.info("Wrote sanitized Testnet soak artifact: %s", artifact_path)

        if artifact["status"] == "PASS":
            evidence_file = Path("artifacts") / "build-evidence.json"
            if evidence_file.exists():
                try:
                    ev_data = json.loads(evidence_file.read_text(encoding="utf-8"))
                    if ev_data.get("build_sha") == build_sha:
                        ev_data["testnet_soak_verified"] = True
                        evidence_file.write_text(json.dumps(ev_data, indent=2) + "\n", encoding="utf-8")
                        logger.info("Promoted testnet_soak_verified=True in build evidence")
                except Exception as exc:
                    logger.error("Failed to promote soak verified in evidence: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervised_soak())
