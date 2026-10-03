"""One explicitly approved ETHUSDC protected lifecycle trial on Futures Testnet."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from apps.trading_worker.main import ArmRequest, TradingWorkerApp
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.manual_testnet import _current_sha
from apps.trading_worker.venues.binance.protected_ethusdc_trial import (
    plan_protected_ethusdc_trial,
)
from domain.enums import (
    EconomicRiskClass, MarketType, OrderSide, OrderType, PositionSide, TimeInForce,
)
from domain.models import ExecutionDecision, OrderIntent, utc_now


def _approved_testnet_runtime() -> None:
    if os.getenv("TESTNET_PROTECTED_ETHUSDC_TRIAL_APPROVED", "").strip().lower() != "true":
        raise RuntimeError("Protected ETHUSDC Testnet mutation is not approved")
    if os.getenv("BINANCE_TESTNET", "").strip().lower() != "true":
        raise RuntimeError("Binance Testnet is not selected")
    if not os.getenv("BINANCE_TESTNET_API_KEY") or not os.getenv("BINANCE_TESTNET_API_SECRET"):
        raise RuntimeError("Binance Testnet credentials are unavailable")
    if os.getenv("BINANCE_MAINNET_API_KEY") or os.getenv("BINANCE_MAINNET_API_SECRET"):
        raise RuntimeError("Mainnet credentials must not be present in the Testnet trial")
    if os.getenv("PERSISTENCE_MODE", "").upper() != "REQUIRED":
        raise RuntimeError("Protected Testnet trial requires durable persistence")
    if (os.getenv("POSTGRES_HOST") != "127.0.0.1"
            or os.getenv("POSTGRES_PORT") != "5433"
            or not re.fullmatch(r"blessing_testnet_trial_[a-z0-9_]+", os.getenv("POSTGRES_DB", ""))
            or os.getenv("DATABASE_URL")):
        raise RuntimeError("Protected Testnet trial requires a dedicated local database")


async def _flat_testnet_baseline(adapter) -> dict[str, object]:
    if getattr(adapter, "env", None) != BinanceEnvironment.TESTNET:
        raise RuntimeError("Protected trial adapter is not Binance Testnet")
    positions = await adapter.rest_client.request(
        "GET", adapter._position_risk_path, signed=True
    )
    orders = await adapter.rest_client.request(
        "GET", "/fapi/v1/openOrders", signed=True
    )
    algos = await adapter.rest_client.request(
        "GET", adapter._open_algo_orders_path, signed=True,
        params={"algoType": "CONDITIONAL"},
    )
    if not isinstance(positions, list) or not isinstance(orders, list) or not isinstance(algos, list):
        raise RuntimeError("Testnet baseline snapshots are incomplete")
    try:
        if any(Decimal(str(row["positionAmt"])) != 0 for row in positions) or orders or algos:
            raise RuntimeError("Testnet account has pre-existing exposure or open orders")
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("Testnet position baseline is invalid") from exc
    eth_rows = [row for row in positions if row.get("symbol") == "ETHUSDC"]
    if len(eth_rows) != 1 or eth_rows[0].get("positionSide", "BOTH") != "BOTH":
        raise RuntimeError("Testnet trial requires one-way ETHUSDC position mode")
    try:
        leverage = int(eth_rows[0]["leverage"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Testnet ETHUSDC leverage is unavailable") from exc
    if not 1 <= leverage <= 10:
        raise RuntimeError("Testnet ETHUSDC leverage exceeds the trial limit")
    return {
        "position_mode": "ONE_WAY",
        "leverage": leverage,
        "nonzero_positions": 0,
        "open_orders": len(orders),
        "open_algo_orders": len(algos),
    }


async def run_protected_ethusdc_testnet_trial() -> dict[str, object]:
    """Submit exactly one bounded protected entry, then close and read back."""
    _approved_testnet_runtime()
    build_sha = _current_sha()
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    artifact: dict[str, object] = {
        "trial_type": "PROTECTED_ETHUSDC_V1", "build_sha": build_sha,
        "environment": "BINANCE_TESTNET", "symbol": "ETHUSDC", "status": "FAIL",
        "protection_status": "UNKNOWN", "close_status": "UNKNOWN",
        "reconciliation_status": "UNKNOWN", "diff_count": -1, "entry_fill_count": 0,
        "position_after": "UNKNOWN", "open_orders_after": "UNKNOWN",
        "open_algo_after": "UNKNOWN",
        "account_baseline": {
            "position_mode": "UNKNOWN", "leverage": 0, "nonzero_positions": -1,
            "open_orders": -1, "open_algo_orders": -1,
        },
        "entry_client_order_id": "", "close_client_order_id": "",
        "stop_client_algo_id": "", "target_client_algo_id": "",
    }
    try:
        armed, _reason = await worker.arm(ArmRequest(
            executionMode="TESTNET", instruments=["ETHUSDC"],
            strategies={"grid": True}, riskProfile="CONSERVATIVE",
        ))
        if not armed or worker.execution_adapter is None:
            raise RuntimeError("Protected ETHUSDC Testnet Worker did not ARM")
        adapter = worker.execution_adapter
        baseline = await _flat_testnet_baseline(adapter)
        artifact["account_baseline"] = baseline
        leverage = int(baseline["leverage"])
        repository = getattr(worker.persistence, "repository", None)
        protections = getattr(repository, "algo_protections", None)
        if protections is None or await protections.list_active_protections(
            venue="binance_testnet"
        ):
            raise RuntimeError("Unresolved durable Testnet protection owner exists")
        quote = await adapter.get_best_bid_ask("ETHUSDC")
        observed_at = adapter.last_market_event_at.get("ETHUSDC")
        rules = adapter.symbol_rules.get("ETHUSDC")
        if quote is None or observed_at is None or rules is None:
            raise RuntimeError("Fresh ETHUSDC Testnet quote or rules are unavailable")
        commission = await adapter.rest_client.request(
            "GET", "/fapi/v1/commissionRate", signed=True,
            params={"symbol": "ETHUSDC"},
        )
        if not isinstance(commission, dict):
            raise RuntimeError("Signed Testnet commission evidence is unavailable")
        try:
            taker_rate = Decimal(str(commission["takerCommissionRate"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeError("Signed Testnet commission rate is invalid") from exc
        if not taker_rate.is_finite() or taker_rate <= 0:
            raise RuntimeError("Signed Testnet commission rate is invalid")
        conservative_cost = Decimal("50") * (taker_rate * 2 + Decimal("0.002"))
        plan = plan_protected_ethusdc_trial(
            rules=rules, bid=Decimal(str(quote[0])), ask=Decimal(str(quote[1])),
            quote_observed_at=observed_at,
            estimated_roundtrip_cost_usdc=conservative_cost,
            actual_leverage=leverage,
        )
        if plan.conservative_notional_usdc > adapter.safety_limits.max_single_order_notional:
            raise RuntimeError("Testnet adapter order cap is below the protected trial size")
        entry_id = f"BAI-ETH-T1-{uuid4().hex[:18]}"
        artifact["entry_client_order_id"] = entry_id
        intent = OrderIntent(
            client_order_id=entry_id, symbol="ETHUSDC",
            basket_id=f"testnet-trial-{uuid4().hex[:16]}",
            market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
            position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC, quantity=plan.quantity,
            stop_loss_price=plan.stop_trigger,
            take_profit_price=plan.target_trigger,
            strategy_id="manual_testnet_trial",
        )
        decision = ExecutionDecision(
            decision_id=f"ETH-T1-{uuid4().hex[:18]}", symbol="ETHUSDC",
            action="SUBMIT_ORDER", risk_class=EconomicRiskClass.NEW_RISK,
            orders=[intent],
        )
        executed = await worker.execute_protected_testnet_decision(decision)
        protection = adapter.last_testnet_protection
        if len(executed) != 1 or protection.get("status") != "PROTECTED":
            raise RuntimeError("Protected ETHUSDC Testnet entry was not verified")
        fills = [fill for fill in await adapter.ledger.get_fills()
                 if fill.client_order_id == entry_id and fill.symbol == "ETHUSDC"]
        if not fills:
            raise RuntimeError("Protected ETHUSDC Testnet fill evidence is unavailable")
        artifact["entry_fill_count"] = len(fills)
        artifact["protection_status"] = "PROTECTED_VERIFIED"
        artifact["stop_client_algo_id"] = str(protection.get("stop_client_algo_id") or "")
        artifact["target_client_algo_id"] = str(protection.get("take_profit_client_algo_id") or "")
        closure = await worker.close_protected_ethusdc_testnet_trial(entry_id)
        artifact.update(closure)
        artifact["status"] = "PASS"
        return artifact
    finally:
        try:
            await worker.disarm()
        except Exception:
            artifact["status"] = "FAIL"
        folder = Path("artifacts")
        folder.mkdir(exist_ok=True)
        name = f"testnet-trial-{int(time.time())}-{uuid4().hex[:8]}.json"
        (folder / name).write_text(json.dumps(artifact, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(run_protected_ethusdc_testnet_trial())
