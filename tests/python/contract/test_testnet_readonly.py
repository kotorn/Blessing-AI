"""Credentialed, read-only Binance USDⓈ-M Testnet contract checks."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.rest_client import BinanceRestClient
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from apps.trading_worker.venues.binance.protected_ethusdc_trial import plan_protected_ethusdc_trial

pytestmark = [pytest.mark.asyncio, pytest.mark.contract_readonly]


@pytest.fixture
def api_credentials():
    if os.getenv("BINANCE_TESTNET", "").strip().lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("BINANCE_TESTNET is not explicitly enabled")
    api_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
    if not api_key or not api_secret:
        pytest.skip("Testnet credentials not found. Skipping contract test.")
    # Never return credentials from a fixture. Pytest includes fixture values
    # in failure reports, so returning the tuple would disclose secrets when
    # a read-only contract assertion fails.
    return True


async def test_binance_testnet_algo_baseline_is_strictly_read_only(api_credentials):
    """Inspect existing Testnet state with GET-only signed calls; never adopt or cancel."""
    api_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
    client = BinanceRestClient(
        api_key=api_key,
        api_secret=api_secret,
        env=BinanceEnvironment.TESTNET,
        read_only=True,
    )
    observed_methods = []

    async def get(path, *, signed=False, params=None):
        observed_methods.append(("GET", path))
        return await client.request("GET", path, signed=signed, params=params or {})

    async def scan_window(path, *, start_ms, end_ms, depth=0):
        if end_ms <= start_ms:
            raise AssertionError("history audit interval did not advance")
        rows = await get(
            path,
            signed=True,
            params={
                "symbol": "ETHUSDC",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(rows, list) or len(rows) > 1000:
            raise AssertionError(f"{path} returned an invalid history page")
        if len(rows) < 1000:
            return rows, False
        midpoint = start_ms + (end_ms - start_ms) // 2
        if depth >= 20 or midpoint <= start_ms or midpoint >= end_ms:
            return rows, True
        left, left_saturated = await scan_window(
            path, start_ms=start_ms, end_ms=midpoint, depth=depth + 1
        )
        right, right_saturated = await scan_window(
            path, start_ms=max(start_ms, midpoint - 1), end_ms=end_ms, depth=depth + 1
        )
        return [*left, *right], left_saturated or right_saturated

    try:
        time_result = await get("/fapi/v1/time")
        exchange_info = await get("/fapi/v1/exchangeInfo")
        account = await get("/fapi/v2/account", signed=True)
        position_mode = await get("/fapi/v1/positionSide/dual", signed=True)
        positions = await get("/fapi/v2/positionRisk", signed=True)
        open_orders = await get(
            "/fapi/v1/openOrders", signed=True, params={"symbol": "ETHUSDC"}
        )
        open_algos = await get(
            "/fapi/v1/openAlgoOrders", signed=True, params={"symbol": "ETHUSDC"}
        )
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000)
        algo_history, algo_saturated = await scan_window(
            "/fapi/v1/allAlgoOrders", start_ms=start_ms, end_ms=end_ms
        )
        order_history, orders_saturated = await scan_window(
            "/fapi/v1/allOrders", start_ms=start_ms, end_ms=end_ms
        )
        trade_history, trades_saturated = await scan_window(
            "/fapi/v1/userTrades", start_ms=start_ms, end_ms=end_ms
        )

        symbol_rows = {
            row.get("symbol"): row
            for row in exchange_info.get("symbols", [])
            if isinstance(row, dict) and row.get("symbol") == "ETHUSDC"
        }
        eth_positions = [
            row for row in positions
            if isinstance(row, dict) and row.get("symbol") == "ETHUSDC"
        ]
        unknown_position = any(
            Decimal(str(row.get("positionAmt", "NaN"))) != 0
            for row in eth_positions
        )
        historical_by_id = {}
        for row in algo_history:
            if not isinstance(row, dict):
                raise AssertionError("Algo history contains a non-object row")
            algo_id = str(row.get("algoId") or "")
            if not algo_id or algo_id in historical_by_id:
                raise AssertionError("Algo history contains a missing or duplicate Algo ID")
            historical_by_id[algo_id] = row

        order_by_id = {}
        for row in order_history:
            if not isinstance(row, dict):
                raise AssertionError("Order history contains a non-object row")
            order_id = str(row.get("orderId") or "")
            if not order_id or order_id in order_by_id:
                raise AssertionError("Order history contains a missing or duplicate order ID")
            order_by_id[order_id] = row
        trade_quantity_by_order = {}
        for row in trade_history:
            if not isinstance(row, dict):
                raise AssertionError("Trade history contains a non-object row")
            order_id = str(row.get("orderId") or "")
            trade_quantity_by_order[order_id] = trade_quantity_by_order.get(
                order_id, Decimal("0")
            ) + Decimal(str(row.get("qty", "NaN")))
        history_lineage_mismatches = []
        for order_id, row in order_by_id.items():
            try:
                executed = Decimal(str(row.get("executedQty", "NaN")))
            except Exception:
                history_lineage_mismatches.append(order_id)
                continue
            if not executed.is_finite() or trade_quantity_by_order.get(
                order_id, Decimal("0")
            ) != executed:
                history_lineage_mismatches.append(order_id)
        for trade_order_id, traded_quantity in trade_quantity_by_order.items():
            if (
                not traded_quantity.is_finite()
                or trade_order_id not in order_by_id
            ):
                history_lineage_mismatches.append(trade_order_id or "MISSING_ORDER_ID")
        algo_child_order_mismatches = []
        for algo_id, row in historical_by_id.items():
            child_order_id = str(
                row.get("actualOrderId") or row.get("triggeredOrderId") or ""
            ).strip()
            if child_order_id and child_order_id not in order_by_id:
                algo_child_order_mismatches.append(algo_id)
        order_ids_by_client_hash = {}
        for order_id, row in order_by_id.items():
            client_hash = sha256(
                str(row.get("clientOrderId") or "").encode("utf-8")
            ).hexdigest()
            order_ids_by_client_hash.setdefault(client_hash, []).append(order_id)
        duplicate_client_order_ids = [
            {"client_id_sha256": client_hash, "order_ids": sorted(order_ids)}
            for client_hash, order_ids in sorted(order_ids_by_client_hash.items())
            if len(order_ids) > 1
        ]

        algo_summary = [
            {
                "algo_id": str(row.get("algoId") or ""),
                "client_id_sha256": sha256(
                    str(row.get("clientAlgoId") or "").encode("utf-8")
                ).hexdigest(),
                "status": str(row.get("algoStatus") or "UNKNOWN").upper(),
                "order_type": str(row.get("orderType") or row.get("type") or "UNKNOWN").upper(),
                "create_time": row.get("createTime"),
                "update_time": row.get("updateTime"),
            }
            for row in sorted(historical_by_id.values(), key=lambda item: str(item.get("algoId")))
        ]
        open_algo_ids = sorted(
            str(row.get("algoId") or "")
            for row in open_algos
            if isinstance(row, dict)
        )
        audit = {
            "environment": "BINANCE_TESTNET",
            "observed_at_utc": datetime.fromtimestamp(
                end_ms / 1000, tz=timezone.utc
            ).isoformat(),
            "strict_get_only": all(method == "GET" for method, _ in observed_methods),
            "signed_account_read": isinstance(account, dict),
            "can_trade": account.get("canTrade") if isinstance(account, dict) else None,
            "position_mode_known": isinstance(position_mode, dict)
            and isinstance(position_mode.get("dualSidePosition"), bool),
            "symbol_status": symbol_rows.get("ETHUSDC", {}).get("status"),
            "position_rows": len(eth_positions),
            "nonzero_ethusdc_position": unknown_position,
            "open_order_count": len(open_orders) if isinstance(open_orders, list) else None,
            "open_algo_ids": open_algo_ids,
            "recent_algo_history": algo_summary,
            "history_window_days": 3,
            "history_saturated": {
                "algo": algo_saturated,
                "orders": orders_saturated,
                "trades": trades_saturated,
            },
            "recent_all_order_count": len(order_history),
            "recent_user_trade_count": len(trade_history),
            "recent_orders": [
                {
                    "order_id": order_id,
                    "client_id_sha256": sha256(
                        str(row.get("clientOrderId") or "").encode("utf-8")
                    ).hexdigest(),
                    "side": str(row.get("side") or "UNKNOWN").upper(),
                    "status": str(row.get("status") or "UNKNOWN").upper(),
                    "executed_quantity": str(row.get("executedQty") or "0"),
                    "trade_quantity": str(trade_quantity_by_order.get(order_id, Decimal("0"))),
                }
                for order_id, row in sorted(order_by_id.items())
            ],
            "history_lineage_mismatches": sorted(set(history_lineage_mismatches)),
            "algo_child_order_mismatches": sorted(set(algo_child_order_mismatches)),
            "duplicate_client_order_ids": duplicate_client_order_ids,
            "durable_owner_match": "NOT_CHECKED_NO_TESTNET_OWNER_DATABASE",
            "readiness": "BLOCKED_UNTIL_OWNER_AND_HISTORY_PROVEN",
            "server_time_read": isinstance(time_result.get("serverTime"), int),
            "order_endpoint_attempts": client.order_endpoint_attempts,
        }
        # A sizing-only check uses the same planner as the protected trial,
        # but never grants authority or claims slippage has been measured.
        commission = await get("/fapi/v1/commissionRate", signed=True, params={"symbol": "ETHUSDC"})
        quote = await get("/fapi/v1/ticker/bookTicker", params={"symbol": "ETHUSDC"})
        quote_retrieved_at = datetime.now(timezone.utc)
        trial_plan = {"status": "FAIL", "reason": "TRIAL_SIZING_NOT_VERIFIED"}
        try:
            rules = SymbolTradingRules("ETHUSDC")
            rules.parse_exchange_info(symbol_rows.get("ETHUSDC", {}))
            taker_rate = Decimal(str(commission["takerCommissionRate"]))
            if not taker_rate.is_finite() or taker_rate <= 0 or len(eth_positions) != 1:
                raise ValueError("cost or position identity unavailable")
            actual_leverage = int(eth_positions[0]["leverage"])
            plan = plan_protected_ethusdc_trial(
                rules=rules, bid=Decimal(str(quote["bidPrice"])), ask=Decimal(str(quote["askPrice"])),
                quote_observed_at=quote_retrieved_at,
                estimated_roundtrip_cost_usdc=Decimal("50") * (2 * taker_rate + Decimal("0.002")),
                actual_leverage=actual_leverage,
            )
            trial_plan = {
                "status": "PASS", "scope": "SIZING_ONLY",
                "quantity": str(plan.quantity), "conservative_notional_usdc": str(plan.conservative_notional_usdc),
                "planned_loss_usdc": str(plan.planned_loss_usdc), "actual_leverage": actual_leverage,
                "min_market_notional": str(rules.min_notional_for("MARKET")),
                "taker_rate": str(taker_rate),
                "cost_model": "exchange_taker_roundtrip_plus_assumed_20bps_buffer",
                "quote_time_semantics": "REST_RETRIEVAL_NOT_SOURCE_TIMESTAMP",
            }
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            # These errors originate only from public rules/quote, commission
            # fields and the sizing planner, never credential or HTTP payloads.
            trial_plan["failure_type"] = type(exc).__name__
            trial_plan["failure_detail"] = str(exc)[:256]
        audit["protected_trial_sizing"] = trial_plan
        audit["strict_get_only"] = all(method == "GET" for method, _ in observed_methods)
        audit["order_endpoint_attempts"] = client.order_endpoint_attempts
        print("TESTNET_READ_ONLY_ALGO_AUDIT=" + json.dumps(audit, sort_keys=True))
        assert audit["strict_get_only"] is True
        assert audit["order_endpoint_attempts"] == 0
        assert isinstance(positions, list)
        assert isinstance(open_orders, list)
        assert isinstance(open_algos, list)
        assert audit["symbol_status"] == "TRADING"
        assert audit["can_trade"] is True
        assert position_mode.get("dualSidePosition") is False
        assert audit["open_order_count"] == 0
        assert len(audit["open_algo_ids"]) == 0
        assert audit["protected_trial_sizing"]["status"] == "PASS"
    finally:
        await client.close()
