"""Supervised, bounded autonomous Binance USDⓈ-M Testnet soak runner.

This path is deliberately separate from the normal worker process.  It is
explicitly opt-in, requires current-build evidence and a successful manual
trial, starts the real worker market-event path, and performs an authoritative
Testnet cleanup before the worker is disarmed.  The repository defaults keep
the runner disabled.
"""

import asyncio
import json
import logging
import math
import os
import subprocess
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional

from apps.trading_worker.evidence import BuildEvidence, TestnetSoakArtifact
from apps.trading_worker.main import ArmRequest, TradingWorkerApp
from apps.trading_worker.venues.binance.config import BinanceEnvironment, get_ws_url
from apps.trading_worker.venues.binance.manual_testnet import (
    _cleanup_trial_open_orders,
)
from apps.trading_worker.venues.binance.models import ConnectionState
from domain.models import utc_now
from apps.trading_worker.venues.binance.public_ws import BinancePublicWebSocket


logger = logging.getLogger("soak_runner")

_MAX_SOAK_DURATION_SEC = 900.0
_MAX_SOAK_DECISIONS = 100
_MAX_SOAK_FILLS = 10


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
        if len(head_sha) < 7:
            raise RuntimeError("ABORT: current Git SHA is invalid")
        return head_sha
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("ABORT: cannot verify current Git SHA for the soak runner") from exc


def _bounded_float(
    value: Optional[float],
    env_name: str,
    default: float,
    maximum: float,
) -> float:
    raw: Any = value
    if raw is None:
        raw = os.getenv(env_name, str(default))
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ABORT: {env_name} must be a finite positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0 or parsed > maximum:
        raise RuntimeError(
            f"ABORT: {env_name} must be > 0 and <= {maximum:g} for a bounded soak"
        )
    return parsed


def _bounded_int(
    value: Optional[int],
    env_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw: Any = value
    if raw is None:
        raw = os.getenv(env_name, str(default))
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ABORT: {env_name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(
            f"ABORT: {env_name} must be between {minimum} and {maximum} for a bounded soak"
        )
    return parsed


def _require_prerequisite_evidence(build_sha: str) -> BuildEvidence:
    """Require current-SHA local, CI, read-only, and manual evidence."""
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
            and evidence.github_ci_verified
            and evidence.readonly_contract_verified
            and evidence.manual_trial_verified
            and evidence.manual_trial_sha == build_sha
        ):
            return evidence
    raise RuntimeError(
        "ABORT: current-SHA local, CI, read-only, and manual Testnet evidence "
        "are required before the autonomous soak run"
    )


async def _exchange_positions(adapter) -> list[dict]:
    payload = await adapter.rest_client.request(
        "GET", "/fapi/v2/positionRisk", signed=True
    )
    if not isinstance(payload, list):
        raise RuntimeError("positionRisk response is invalid")
    return payload


async def _exchange_open_orders(adapter, symbol: str) -> list[dict]:
    payload = await adapter.rest_client.request(
        "GET",
        "/fapi/v1/openOrders",
        signed=True,
        params={"symbol": symbol},
    )
    if not isinstance(payload, list):
        raise RuntimeError("openOrders response is invalid")
    return payload


def _active_position_summaries(positions: list[dict], symbol: str) -> list[dict]:
    """Validate position amounts and return sanitized active positions."""
    active: list[dict] = []
    normalized_symbol = symbol.upper()
    for position in positions:
        if not isinstance(position, dict):
            raise RuntimeError("positionRisk contains a non-object entry")
        current_symbol = str(position.get("symbol", "")).upper()
        if current_symbol != normalized_symbol:
            continue
        raw_amount = position.get("positionAmt")
        if raw_amount in (None, ""):
            raise RuntimeError("positionRisk entry is missing positionAmt")
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeError("positionRisk positionAmt is invalid") from exc
        if not amount.is_finite():
            raise RuntimeError("positionRisk positionAmt is not finite")
        if amount == 0:
            continue
        active.append(
            {
                "symbol": current_symbol,
                "positionSide": str(position.get("positionSide", "BOTH")),
                "positionAmt": str(amount),
                "entryPrice": str(position.get("entryPrice", "0")),
                "markPrice": str(position.get("markPrice", "0")),
            }
        )
    return active


def _fill_key(fill: Any) -> tuple[str, str]:
    return (str(fill.symbol).upper(), str(fill.exchange_trade_id))


def _update_fill_metrics(
    adapter,
    baseline_fill_keys: set[tuple[str, str]],
    artifact: Dict[str, Any],
) -> None:
    fills = list(adapter.ledger.fills)
    keys = [_fill_key(fill) for fill in fills]
    unique_keys = set(keys)
    new_fills = [fill for fill in fills if _fill_key(fill) not in baseline_fill_keys]
    artifact["fills_count"] = len(unique_keys - baseline_fill_keys)
    artifact["duplicate_fills_count"] = len(keys) - len(unique_keys)
    artifact["total_fees_usdt"] = float(
        sum(
            (
                fill.commission
                for fill in new_fills
                if str(fill.commission_asset).upper() == "USDT"
            ),
            Decimal("0"),
        )
    )


async def _record_exposure(adapter, artifact: Dict[str, Any]) -> None:
    snapshot = await adapter.ledger.get_account_snapshot()
    if snapshot is None:
        return
    try:
        notional = Decimal(str(snapshot.total_position_notional))
    except (InvalidOperation, TypeError, ValueError):
        return
    if not notional.is_finite() or notional < 0:
        return
    artifact["max_exposure_notional"] = max(
        artifact["max_exposure_notional"], float(notional)
    )


async def run_supervised_soak(
    duration_sec: Optional[float] = None,
    max_decisions: Optional[int] = None,
    max_fills: Optional[int] = None,
) -> Dict[str, Any]:
    """Run a bounded autonomous Testnet session only after explicit approval."""
    if not _enabled("AUTONOMOUS_TESTNET_SOAK_APPROVED"):
        raise RuntimeError("ABORT: AUTONOMOUS_TESTNET_SOAK_APPROVED must be true")
    if not _enabled("AUTONOMOUS_TESTNET_EXECUTION"):
        raise RuntimeError("ABORT: AUTONOMOUS_TESTNET_EXECUTION must be true")
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

    duration = _bounded_float(
        duration_sec,
        "SOAK_DURATION_SEC",
        300.0,
        _MAX_SOAK_DURATION_SEC,
    )
    decision_limit = _bounded_int(
        max_decisions,
        "SOAK_MAX_DECISIONS",
        20,
        1,
        _MAX_SOAK_DECISIONS,
    )
    fill_limit = _bounded_int(
        max_fills,
        "SOAK_MAX_FILLS",
        2,
        0,
        _MAX_SOAK_FILLS,
    )
    symbol = "BTCUSDT"
    strategy = os.getenv("TESTNET_SOAK_STRATEGY", "grid").strip().lower()
    if strategy not in {"grid", "trend", "shock", "carry"}:
        raise RuntimeError("ABORT: TESTNET_SOAK_STRATEGY is unsupported")

    artifact: Dict[str, Any] = {
        "build_sha": build_sha,
        "environment": "BINANCE_TESTNET",
        "strategy": strategy,
        "symbol": symbol,
        "runtime_seconds": 0.0,
        "market_events_count": 0,
        "decision_count": 0,
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
        "reconciliation_status": "UNKNOWN",
        "open_orders_remaining": 0,
        "ambiguous_requests_count": 0,
        "duplicate_fills_count": 0,
        "final_positions": [],
        "hard_stop_triggered": False,
        "hard_stop_reason": None,
        "status": "FAIL",
    }

    worker = TradingWorkerApp(symbols=[symbol])
    adapter = None
    public_stream: Optional[BinancePublicWebSocket] = None
    ownership_proven = False
    finalization_attempted = False
    stop_requested = False
    trial_client_order_ids: set[str] = set()
    submitted_client_order_ids: set[str] = set()
    baseline_fill_keys: set[tuple[str, str]] = set()
    start_time = time.monotonic()

    async def on_market_event(event) -> None:
        nonlocal stop_requested
        if artifact["hard_stop_triggered"] or stop_requested:
            return
        if artifact["decision_count"] >= decision_limit:
            stop_requested = True
            return
        artifact["market_events_count"] += 1
        try:
            decision = await worker.handle_market_event(event)
        except Exception as exc:
            artifact["hard_stop_triggered"] = True
            artifact["hard_stop_reason"] = f"Unhandled worker event exception: {exc}"
            await worker.set_pause_new_risk(True)
            return
        if decision is None or decision.action == "NOOP":
            return

        artifact["decision_count"] += 1
        artifact["intent_count"] += len(decision.orders)
        decision_submitted = False
        for intent in decision.orders:
            trial_client_order_ids.add(intent.client_order_id)
            order = await adapter.ledger.get_order_by_client_id(
                intent.client_order_id
            )
            if order is not None:
                submitted_client_order_ids.add(intent.client_order_id)
                decision_submitted = True
        artifact["orders_submitted_count"] = len(submitted_client_order_ids)
        if decision.orders and decision_submitted:
            artifact["risk_approved_count"] += 1
        elif decision.orders:
            artifact["risk_rejected_count"] += 1
        if artifact["decision_count"] >= decision_limit:
            stop_requested = True

    async def finalize_exchange_state() -> bool:
        nonlocal finalization_attempted
        finalization_attempted = True
        if adapter is None or not ownership_proven:
            return False
        try:
            # Keep the private stream alive while cancellation, emergency
            # reduction, and reconciliation are verified.
            await worker.set_pause_new_risk(True)
            cleanup_verified = await _cleanup_trial_open_orders(
                worker,
                trial_client_order_ids,
                allowed_prefixes=("B-SYS-", "BAI-SOAK-"),
            )
            positions = await _exchange_positions(adapter)
            active_positions = _active_position_summaries(positions, symbol)
            if active_positions:
                await worker.emergency_flatten(symbol)
                positions = await _exchange_positions(adapter)
                active_positions = _active_position_summaries(positions, symbol)

            open_orders = await _exchange_open_orders(adapter, symbol)
            artifact["open_orders_remaining"] = len(open_orders)
            artifact["final_positions"] = active_positions
            reconciliation_status = await adapter.reconciliation.reconcile()
            artifact["reconciliation_runs_count"] += 1
            artifact["reconciliation_diffs_count"] += len(
                adapter.reconciliation.last_diffs
            )
            artifact["reconciliation_status"] = reconciliation_status
            _update_fill_metrics(adapter, baseline_fill_keys, artifact)

            return bool(
                cleanup_verified
                and not open_orders
                and not active_positions
                and reconciliation_status == "IN_SYNC"
                and artifact["duplicate_fills_count"] == 0
                and artifact["fills_count"] <= fill_limit
            )
        except Exception as exc:
            logger.error("Soak exchange cleanup/reconciliation failed: %s", exc)
            artifact["hard_stop_triggered"] = True
            artifact["hard_stop_reason"] = f"Final cleanup/reconciliation failed: {exc}"
            artifact["reconciliation_status"] = "UNKNOWN"
            return False

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

        # Prove ownership before any future cleanup action. Existing exchange
        # inventory or open orders aborts without touching those resources.
        initial_positions = await _exchange_positions(adapter)
        if _active_position_summaries(initial_positions, symbol):
            raise RuntimeError("ABORT: existing BTCUSDT Testnet exposure is not flat")
        initial_open_orders = await _exchange_open_orders(adapter, symbol)
        if initial_open_orders:
            raise RuntimeError("ABORT: existing BTCUSDT Testnet open orders are present")
        ownership_proven = True
        baseline_fill_keys = {
            _fill_key(fill) for fill in adapter.ledger.fills
        }

        readiness = worker.get_launch_readiness()
        if not readiness.get("testnet_autonomous_soak_ready"):
            raise RuntimeError(
                "Worker reported testnet_autonomous_soak_ready == False; "
                "all current-build evidence and runtime gates are required"
            )

        public_stream = BinancePublicWebSocket(
            symbols=[symbol],
            base_ws_url=get_ws_url(BinanceEnvironment.TESTNET),
            event_callback=on_market_event,
        )
        worker.ws_client = public_stream
        if not await public_stream.start():
            raise RuntimeError("Public Testnet market stream could not be started")

        connect_deadline = time.monotonic() + 10.0
        while not public_stream.is_connected and time.monotonic() < connect_deadline:
            await asyncio.sleep(0.25)
        if not public_stream.is_connected:
            raise RuntimeError("Public Testnet market stream did not become connected")

        start_time = time.monotonic()
        poll_interval = 2.0
        reconcile_interval = 10.0
        last_reconcile = time.monotonic()
        logger.info(
            "Soak started: symbol=%s strategy=%s duration=%.1fs max_decisions=%d max_fills=%d",
            symbol,
            strategy,
            duration,
            decision_limit,
            fill_limit,
        )

        while (
            time.monotonic() - start_time < duration
            and not artifact["hard_stop_triggered"]
            and not stop_requested
        ):
            artifact["runtime_seconds"] = round(time.monotonic() - start_time, 2)
            if not public_stream.is_connected:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Public market stream disconnected"
                break
            if adapter.connection_state != ConnectionState.READY:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = (
                    f"Adapter connection degraded: {adapter.connection_state}"
                )
                break
            if not adapter.private_stream_healthy:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Private stream reported unhealthy"
                break
            if not adapter.authenticated or not adapter.capabilities.trade_authorized:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = (
                    "Authentication or trade authorization lost"
                )
                break

            _update_fill_metrics(adapter, baseline_fill_keys, artifact)
            await _record_exposure(adapter, artifact)
            if artifact["fills_count"] > fill_limit:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = (
                    f"Fill count exceeded soak bound: {artifact['fills_count']} > {fill_limit}"
                )
                break
            if artifact["duplicate_fills_count"] > 0:
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Duplicate fill detected"
                break

            if time.monotonic() - last_reconcile >= reconcile_interval:
                reconcile_status = await adapter.reconciliation.reconcile()
                artifact["reconciliation_runs_count"] += 1
                artifact["reconciliation_diffs_count"] += len(
                    adapter.reconciliation.last_diffs
                )
                artifact["reconciliation_status"] = reconcile_status
                if reconcile_status != "IN_SYNC":
                    artifact["hard_stop_triggered"] = True
                    artifact["hard_stop_reason"] = (
                        f"Reconciliation diverged: {reconcile_status}"
                    )
                    break
                last_reconcile = time.monotonic()

            if not await adapter.refresh_market_data([symbol]):
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Fresh Testnet market data unavailable"
                break
            worker.last_market_event_at.update(adapter.last_market_event_at)
            if not worker.is_market_data_fresh([symbol]):
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Testnet market data is stale"
                break
            await asyncio.sleep(min(poll_interval, max(0.05, duration - (time.monotonic() - start_time))))

        if artifact["market_events_count"] == 0 and not artifact["hard_stop_triggered"]:
            artifact["hard_stop_triggered"] = True
            artifact["hard_stop_reason"] = "No public Testnet market events were observed"

        artifact["runtime_seconds"] = round(time.monotonic() - start_time, 2)
        cleanup_ok = await finalize_exchange_state()
        if not cleanup_ok:
            artifact["hard_stop_triggered"] = True
            if not artifact["hard_stop_reason"]:
                artifact["hard_stop_reason"] = "Final Testnet cleanup was not verified"
            artifact["status"] = "FAIL"
        else:
            artifact["status"] = "FAIL" if artifact["hard_stop_triggered"] else "PASS"
        return artifact
    finally:
        if ownership_proven and not finalization_attempted:
            await finalize_exchange_state()
        if public_stream is not None:
            try:
                await public_stream.stop()
            except Exception as exc:
                logger.error("Error stopping public Testnet stream: %s", exc)
                artifact["status"] = "FAIL"
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Public Testnet stream stop was not verified"
        if worker.execution_adapter is not None:
            try:
                await worker.disarm()
            except Exception as exc:
                logger.error("Error disarming worker after soak: %s", exc)
                artifact["status"] = "FAIL"
                artifact["hard_stop_triggered"] = True
                artifact["hard_stop_reason"] = "Worker disarm was not verified"

        artifact["runtime_seconds"] = round(time.monotonic() - start_time, 2)
        timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        artifact_path = Path("artifacts") / f"testnet-soak-{build_sha[:7]}-{timestamp}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(TestnetSoakArtifact.model_validate(artifact).model_dump(), indent=2)
            + "\n",
            encoding="utf-8",
        )
        logger.info("Wrote sanitized Testnet soak artifact: %s", artifact_path)

        if artifact["status"] == "PASS":
            evidence_file = Path("artifacts") / "build-evidence.json"
            if evidence_file.exists():
                try:
                    evidence = BuildEvidence.model_validate(
                        json.loads(evidence_file.read_text(encoding="utf-8"))
                    )
                    if evidence.build_sha == build_sha:
                        data = evidence.model_dump()
                        data["testnet_soak_verified"] = True
                        evidence_file.write_text(
                            json.dumps(data, indent=2) + "\n", encoding="utf-8"
                        )
                        logger.info("Promoted testnet_soak_verified=True in build evidence")
                except Exception as exc:
                    logger.error("Failed to promote soak verified in evidence: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervised_soak())
