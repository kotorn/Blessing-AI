"""Explicitly gated, worker-owned Binance USDⓈ-M Testnet lifecycle trial."""

import asyncio
import json
import logging
import os
import subprocess
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any, Dict, List, Optional

from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
)
from domain.models import ExecutionDecision, OrderIntent, utc_now

from apps.trading_worker.evidence import BuildEvidence, TestnetTrialArtifact
from apps.trading_worker.main import ArmRequest, TradingWorkerApp


logger = logging.getLogger("manual_testnet")


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
            raise RuntimeError("ABORT: manual Testnet trial requires a clean working tree")
        return head_sha
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "ABORT: cannot verify the current Git SHA for the manual Testnet trial"
        ) from exc


def _sanitized_positions(positions: List[dict]) -> List[dict]:
    result = []
    for position in positions:
        amount = str(position.get("positionAmt", "0"))
        try:
            if Decimal(amount) == 0:
                continue
        except (InvalidOperation, ValueError):
            # Preserve malformed active-looking entries so the caller fails
            # closed instead of treating an unusable exchange value as flat.
            pass
        result.append(
            {
                "symbol": position.get("symbol"),
                "positionSide": position.get("positionSide", "BOTH"),
                "positionAmt": amount,
                "entryPrice": str(position.get("entryPrice", "0")),
                "markPrice": str(position.get("markPrice", "0")),
            }
        )
    return result


def _require_current_readonly_evidence(build_sha: str) -> BuildEvidence:
    """Require local and read-only evidence for this exact source revision."""

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
        ):
            return evidence
    raise RuntimeError(
        "ABORT: current-SHA local and read-only Testnet evidence is required "
        "before the manual mutation trial"
    )


async def _exchange_positions(adapter) -> List[dict]:
    payload = await adapter.rest_client.request(
        "GET", "/fapi/v2/positionRisk", signed=True
    )
    if not isinstance(payload, list):
        raise RuntimeError("positionRisk response is invalid")
    return payload


async def _exchange_open_orders(adapter, symbol: str) -> List[dict]:
    payload = await adapter.rest_client.request(
        "GET", "/fapi/v1/openOrders", signed=True, params={"symbol": symbol}
    )
    if not isinstance(payload, list):
        raise RuntimeError("openOrders response is invalid")
    return payload


async def _cleanup_trial_open_orders(
    worker: TradingWorkerApp,
    trial_client_order_ids: set[str],
) -> bool:
    """Cancel and verify only the manual trial's still-open orders.

    The preflight requires BTCUSDT to be clear, so an order observed after the
    trial starts must belong to this bounded workflow.  An order without a
    verifiable client ID is left untouched and causes cleanup to fail closed;
    it must never be guessed or silently adopted.
    """

    adapter = worker.execution_adapter
    if adapter is None:
        return False
    known_ids = {str(order_id) for order_id in trial_client_order_ids if order_id}
    try:
        open_orders = await _exchange_open_orders(adapter, "BTCUSDT")
        cancellation_failed = False
        for raw_order in open_orders:
            if not isinstance(raw_order, dict):
                cancellation_failed = True
                continue
            client_order_id = str(raw_order.get("clientOrderId") or "")
            # Binance amendments normally retain the original client ID. Keep
            # the prefix check as a bounded fallback for a Testnet-generated
            # replacement ID, while still rejecting unknown IDs.
            belongs_to_trial = client_order_id in known_ids or client_order_id.startswith(
                "BAI-MANUAL-"
            )
            if not belongs_to_trial or not client_order_id:
                cancellation_failed = True
                continue
            known_ids.add(client_order_id)
            if not await worker.cancel_testnet_order("BTCUSDT", client_order_id):
                cancellation_failed = True

        remaining = await _exchange_open_orders(adapter, "BTCUSDT")
        if remaining:
            cancellation_failed = True
        return not cancellation_failed
    except Exception as exc:
        logger.error("Manual Testnet trial open-order cleanup is unknown: %s", exc)
        return False


async def _flatten_unexpected_testnet_exposure(
    worker: TradingWorkerApp,
    artifact: Dict[str, Any],
) -> None:
    """Pause new risk and use only the worker-owned Testnet emergency path."""

    await worker.set_pause_new_risk(True)
    await worker.emergency_flatten("BTCUSDT")
    if "EMERGENCY_FLATTEN_SUBMITTED" not in artifact["order_lifecycle"]:
        artifact["order_lifecycle"].append("EMERGENCY_FLATTEN_SUBMITTED")
    if worker.execution_adapter is not None:
        artifact["fill_count"] = len(worker.execution_adapter.ledger.fills)


async def _wait_for_stream_order(
    worker: TradingWorkerApp,
    client_order_id: str,
    status: str,
    previous_order_event=None,
    acceptable_statuses=None,
):
    adapter = worker.execution_adapter
    if adapter is None:
        return False, None
    expected_statuses = set(acceptable_statuses or {status, "PARTIALLY_FILLED", "FILLED"})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        order = await adapter.ledger.get_order_by_client_id(client_order_id)
        current_event = adapter.last_order_event_at.get(client_order_id)
        if (
            order is not None
            and order.status in expected_statuses
            and current_event is not None
            and (previous_order_event is None or current_event > previous_order_event)
        ):
            return True, order
        await asyncio.sleep(0.25)
    return False, await adapter.ledger.get_order_by_client_id(client_order_id)


def _passive_order(adapter, symbol: str, bid: Decimal, ask: Decimal):
    rules = adapter.symbol_rules.get(symbol)
    if rules is None or not rules.is_ready_for("LIMIT"):
        raise RuntimeError("BTCUSDT LIMIT rules are unavailable")
    passive_price = rules.normalize_price(bid - rules.tick_size)
    if passive_price <= 0 or passive_price >= ask:
        raise RuntimeError("Unable to derive a passive Testnet bid from the live quote")

    required_steps = (rules.min_notional / passive_price / rules.step_size).to_integral_value(
        rounding=ROUND_CEILING
    )
    quantity = max(rules.min_qty, required_steps * rules.step_size)
    quantity = rules.normalize_quantity(quantity)
    notional = quantity * passive_price
    if quantity <= 0 or notional < rules.min_notional:
        raise RuntimeError("Exchange minimum order requirements cannot be normalized")
    if quantity > rules.max_qty:
        raise RuntimeError("Exchange minimum notional requires more than the symbol max quantity")
    if notional > adapter.safety_limits.max_single_order_notional:
        raise RuntimeError(
            "ABORT: Binance minimum notional exceeds the configured 25 USDT Testnet cap"
        )
    return passive_price, quantity


async def manual_testnet_workflow() -> Optional[Dict[str, Any]]:
    """Run one trial only when explicit local mutation approval is present."""

    if not _enabled("TESTNET_MANUAL_TRIAL_APPROVED"):
        logger.info(
            "Manual Testnet mutation not run; TESTNET_MANUAL_TRIAL_APPROVED is not enabled"
        )
        return None
    if not _enabled("BINANCE_TESTNET"):
        raise RuntimeError("ABORT: BINANCE_TESTNET must be true")
    if not os.getenv("BINANCE_TESTNET_API_KEY", "").strip() or not os.getenv(
        "BINANCE_TESTNET_API_SECRET", ""
    ).strip():
        raise RuntimeError("ABORT: Testnet credentials are not configured")

    build_sha = _current_sha()
    _require_current_readonly_evidence(build_sha)
    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    artifact: Dict[str, Any] = {
        "build_sha": build_sha,
        "environment": "BINANCE_TESTNET",
        "rest_host": "https://testnet.binancefuture.com",
        "ws_host": "wss://stream.binancefuture.com/ws",
        "symbol": "BTCUSDT",
        "client_order_id": "NOT_CREATED",
        "exchange_order_id": None,
        "submit_latency_ms": None,
        "ws_latency_ms": None,
        "order_lifecycle": [],
        "modify_result": "NOT_RUN",
        "cancel_result": "NOT_RUN",
        "fill_count": 0,
        "position_before": [],
        "position_after": [],
        "reconciliation_status": "UNKNOWN",
        "diff_count": 0,
        "status": "FAIL",
    }
    trial_client_order_ids: set[str] = set()
    unexpected_fill_observed = False

    try:
        armed, reason = await worker.arm(
            ArmRequest(
                executionMode="TESTNET",
                instruments=["BTCUSDT"],
                strategies={"grid": True},
                riskProfile="CONSERVATIVE",
            )
        )
        if not armed:
            raise RuntimeError(f"Worker Testnet ARM failed: {reason}")
        adapter = worker.execution_adapter
        if adapter is None:
            raise RuntimeError("Worker armed without a Testnet adapter")

        before = await _exchange_positions(adapter)
        artifact["position_before"] = _sanitized_positions(before)
        if artifact["position_before"]:
            raise RuntimeError(
                "ABORT: Existing BTCUSDT Testnet exposure must be flat before the manual trial"
            )
        existing_orders = await _exchange_open_orders(adapter, "BTCUSDT")
        if existing_orders:
            raise RuntimeError(
                "ABORT: Existing BTCUSDT Testnet open orders must be cleared before the manual trial"
            )
        quote = await adapter.get_best_bid_ask("BTCUSDT")
        if quote is None:
            raise RuntimeError("Fresh BTCUSDT bid/ask unavailable")
        passive_price, quantity = _passive_order(adapter, "BTCUSDT", *quote)
        position_side = PositionSide.LONG if adapter.capabilities.hedge_mode else PositionSide.BOTH
        client_order_id = f"BAI-MANUAL-{int(time.time() * 1000)}"
        trial_client_order_ids.add(client_order_id)
        artifact["client_order_id"] = client_order_id
        intent = OrderIntent(
            client_order_id=client_order_id,
            symbol="BTCUSDT",
            market_type=MarketType.USDM_FUTURES,
            side=OrderSide.BUY,
            position_side=position_side,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=quantity,
            price=passive_price,
            strategy_id="manual_testnet_trial",
        )
        decision = ExecutionDecision(
            decision_id=f"MANUAL-TRIAL-{int(time.time() * 1000)}",
            symbol="BTCUSDT",
            action="SUBMIT_ORDER",
            risk_class=EconomicRiskClass.NEW_RISK,
            orders=[intent],
        )

        submit_started = time.perf_counter()
        stream_event_before_submit = adapter.last_order_event_at.get(client_order_id)
        submitted = await worker.execute_manual_decision(decision)
        artifact["submit_latency_ms"] = round((time.perf_counter() - submit_started) * 1000, 2)
        if not submitted:
            raise RuntimeError("Final worker/order gate rejected the manual order")
        order = submitted[0]
        artifact["exchange_order_id"] = order.exchange_order_id
        artifact["order_lifecycle"].append("REST_ACK")

        ws_started = time.perf_counter()
        ws_seen, streamed_order = await _wait_for_stream_order(
            worker,
            client_order_id,
            "NEW",
            previous_order_event=stream_event_before_submit,
        )
        artifact["ws_latency_ms"] = round((time.perf_counter() - ws_started) * 1000, 2)
        if not ws_seen:
            raise RuntimeError("WS NEW was not observed for the submitted order")
        artifact["order_lifecycle"].append("WS_NEW")

        # ORDER_TRADE_UPDATE invalidates the previous account snapshot and
        # reconciliation result.  Re-verify the new open order before any
        # amendment is allowed through the Worker decision gate.
        if await adapter.reconciliation.reconcile() != "IN_SYNC":
            raise RuntimeError("Post-WS_NEW reconciliation was not verified")

        order_status = (streamed_order or order).status
        if order_status in {"FILLED", "PARTIALLY_FILLED"}:
            # An unexpected fill is handled only on Testnet and only through
            # the worker-owned emergency path.
            unexpected_fill_observed = True
            artifact["order_lifecycle"].append("UNEXPECTED_FILL")
            await _flatten_unexpected_testnet_exposure(worker, artifact)
        else:
            amended_price = adapter.symbol_rules["BTCUSDT"].normalize_price(
                passive_price - adapter.symbol_rules["BTCUSDT"].tick_size
            )
            amended = await worker.amend_testnet_order(
                "BTCUSDT", client_order_id, amended_price, quantity, "BUY"
            )
            if amended is None:
                raise RuntimeError("Testnet order amendment was not verified")
            artifact["modify_result"] = "VERIFIED"
            artifact["order_lifecycle"].append("MODIFY_ACK")
            active_client_id = amended.client_order_id or client_order_id
            trial_client_order_ids.add(active_client_id)
            queried = await adapter.query_order("BTCUSDT", active_client_id)
            if not queried or queried.get("status") not in {"NEW", "PARTIALLY_FILLED"}:
                raise RuntimeError("Amended Testnet order was not verified as open")
            cancel_event_before = adapter.last_order_event_at.get(active_client_id)
            cancelled = await worker.cancel_testnet_order("BTCUSDT", active_client_id)
            if not cancelled:
                latest_order = await adapter.ledger.get_order_by_client_id(active_client_id)
                if latest_order is not None and latest_order.status in {
                    "FILLED",
                    "PARTIALLY_FILLED",
                }:
                    unexpected_fill_observed = True
                    artifact["order_lifecycle"].append("UNEXPECTED_FILL")
                    await _flatten_unexpected_testnet_exposure(worker, artifact)
                else:
                    raise RuntimeError("Testnet order cancellation was not verified")
            else:
                artifact["cancel_result"] = "VERIFIED"
                artifact["order_lifecycle"].append("CANCEL_ACK")
                ws_cancel_seen, _ = await _wait_for_stream_order(
                    worker,
                    active_client_id,
                    "CANCELED",
                    previous_order_event=cancel_event_before,
                    acceptable_statuses={"CANCELED", "CANCELLED"},
                )
                if not ws_cancel_seen:
                    raise RuntimeError("WS cancellation was not observed for the canceled order")
                artifact["order_lifecycle"].append("WS_CANCELED")

        reconciliation_status = await adapter.reconciliation.reconcile()
        artifact["reconciliation_status"] = reconciliation_status
        artifact["diff_count"] = len(adapter.reconciliation.last_diffs)
        after = await _exchange_positions(adapter)
        artifact["position_after"] = _sanitized_positions(after)
        artifact["fill_count"] = len(adapter.ledger.fills)
        if artifact["position_after"]:
            unexpected_fill_observed = True
            if "UNEXPECTED_FILL" not in artifact["order_lifecycle"]:
                artifact["order_lifecycle"].append("UNEXPECTED_FILL")
            await _flatten_unexpected_testnet_exposure(worker, artifact)
            reconciliation_status = await adapter.reconciliation.reconcile()
            artifact["reconciliation_status"] = reconciliation_status
            artifact["diff_count"] = len(adapter.reconciliation.last_diffs)
            after = await _exchange_positions(adapter)
            artifact["position_after"] = _sanitized_positions(after)
            artifact["fill_count"] = len(adapter.ledger.fills)
        if reconciliation_status != "IN_SYNC" or artifact["position_after"]:
            raise RuntimeError("Final Testnet reconciliation did not prove zero exposure")
        if unexpected_fill_observed:
            raise RuntimeError(
                "Manual Testnet lifecycle failed closed because the passive order filled unexpectedly"
            )
        if artifact["modify_result"] != "VERIFIED" or artifact["cancel_result"] != "VERIFIED":
            raise RuntimeError("Manual Testnet lifecycle did not verify modify and cancel")
        artifact["status"] = "PASS"
        return artifact
    except Exception:
        # If a failure happens before the normal final verification, make one
        # best-effort Testnet-only exposure check/flatten before disarming. The
        # preflight above rejects pre-existing BTCUSDT exposure, so this cannot
        # intentionally target unrelated starting inventory.
        if (
            worker.execution_adapter is not None
            and not artifact["position_before"]
            and artifact["client_order_id"] != "NOT_CREATED"
        ):
            try:
                cleanup_verified = await _cleanup_trial_open_orders(
                    worker, trial_client_order_ids
                )
                artifact["order_lifecycle"].append(
                    "FAILURE_CLEANUP_OPEN_ORDERS_VERIFIED"
                    if cleanup_verified
                    else "FAILURE_CLEANUP_OPEN_ORDERS_UNKNOWN"
                )
                after_failure = await _exchange_positions(worker.execution_adapter)
                if _sanitized_positions(after_failure):
                    if "UNEXPECTED_FILL" not in artifact["order_lifecycle"]:
                        artifact["order_lifecycle"].append("UNEXPECTED_FILL")
                    await _flatten_unexpected_testnet_exposure(worker, artifact)
                    artifact["reconciliation_status"] = await worker.execution_adapter.reconciliation.reconcile()
                    artifact["diff_count"] = len(worker.execution_adapter.reconciliation.last_diffs)
                    artifact["position_after"] = _sanitized_positions(
                        await _exchange_positions(worker.execution_adapter)
                    )
                    artifact["fill_count"] = len(worker.execution_adapter.ledger.fills)
            except Exception as cleanup_exc:
                logger.error("Unexpected-fill cleanup could not be verified: %s", cleanup_exc)
        raise
    finally:
        if worker.execution_adapter is not None:
            try:
                await worker.disarm()
            except Exception as exc:
                logger.error("Failed to disarm after manual Testnet trial: %s", exc)
        timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        artifact_path = Path("artifacts") / f"testnet-trial-{timestamp}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(
                TestnetTrialArtifact.model_validate(artifact).model_dump(), indent=2
            ),
            encoding="utf-8",
        )
        logger.info("Wrote sanitized Testnet trial artifact: %s", artifact_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(manual_testnet_workflow())
