"""Hermetic harness: ONE real Local Live Research Pilot entry attempt, real adapter.

Nothing here talks to a network.  The only fake at the transport edge is an
``aiohttp.ClientSession`` stand-in injected into the REAL ``BinanceRestClient``
(``rest_client.session``), so the production route allowlist, signing, and the
``before_mutation`` hook (which runs the final risk fence) are all exercised.

Production code under test (all real):
  TradingWorkerApp._clamp_order_notional_if_needed
  -> TradingWorkerApp._evaluate_execution_gate (DecisionExecutionGate)
  -> TradingWorkerApp.execute_manual_decision
  -> BinanceExecutionAdapter.execute_decision
       (OrderExecutionGate #1, durable owner/outbox, _final_risk_increase_fence
        == OrderExecutionGate #2, order POST, _protect_local_mainnet_entry)

Faked (success-path behaviour only):
  * the HTTP session (Binance-shaped payloads, per-call latency, call log)
  * persistence / protection store / launch session (in memory)
  * the execution lease, user stream and ``reconciliation`` object
    (``reconcile``/``_recover_order_fills`` only issue representative REST reads)

Time is REAL (``asyncio.sleep``); the code under test reads five different
clocks, so virtualising them would create failures that are not production's.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from apps.trading_worker import main as worker_main
from apps.trading_worker.main import (
    TradingWorkerApp,
    WorkerEngineState,
    WorkerExecutionMode,
)
from apps.trading_worker.venues.binance import execution as execution_module
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.mainnet_risk import LOCAL_LIVE_PILOT_POLICY_SHA256
from apps.trading_worker.venues.binance.models import (
    ConnectionState,
    ExchangeAccountSnapshot,
)
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
)
from domain.models import (
    ExchangeFill,
    ExecutionDecision,
    MarketEvent,
    OrderIntent,
    utc_now,
)

SYMBOL = "ETHUSDC"
FINGERPRINT = "a" * 64
LAUNCH_ID = "launch-realpath-0001"
CAMPAIGN_ID = "pilot-campaign-001"
APPROVAL_ID = "local-approval-12345678-1234-1234-1234-123456789abc"

# Real ETHUSDC exchangeInfo entry (artifacts/gemini-progress/ethusdc-public.json).
# NOTE: the real MIN_NOTIONAL is 20, not 5.
ETHUSDC_EXCHANGE_INFO = {
    "symbol": "ETHUSDC", "pair": "ETHUSDC", "contractType": "PERPETUAL",
    "deliveryDate": 4133404800000, "onboardDate": 1704285300000, "status": "TRADING",
    "maintMarginPercent": "2.5000", "requiredMarginPercent": "5.0000",
    "baseAsset": "ETH", "quoteAsset": "USDC", "marginAsset": "USDC",
    "pricePrecision": 2, "quantityPrecision": 3, "baseAssetPrecision": 8,
    "quotePrecision": 8, "underlyingType": "COIN", "underlyingSubType": ["USDC", "Crypto"],
    "triggerProtect": "0.0500", "liquidationFee": "0.012500", "marketTakeBound": "0.05",
    "maxMoveOrderLimit": 10000,
    "filters": [
        {"maxPrice": "100000", "filterType": "PRICE_FILTER", "minPrice": "0.01", "tickSize": "0.01"},
        {"stepSize": "0.001", "filterType": "LOT_SIZE", "maxQty": "8000", "minQty": "0.001"},
        {"minQty": "0.001", "maxQty": "700", "stepSize": "0.001", "filterType": "MARKET_LOT_SIZE"},
        {"filterType": "MAX_NUM_ORDERS", "limit": 200},
        {"filterType": "MIN_NOTIONAL", "notional": "20"},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.0500", "multiplierDown": "0.9500",
         "multiplierDecimal": "4"},
        {"filterType": "POSITION_RISK_CONTROL", "positionControlSide": "NONE"},
    ],
    "orderTypes": ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT",
                   "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"],
    "timeInForce": ["GTC", "IOC", "FOK", "GTX", "GTD"],
}


def make_rules() -> SymbolTradingRules:
    rules = SymbolTradingRules(SYMBOL)
    rules.parse_exchange_info(ETHUSDC_EXCHANGE_INFO)
    return rules


# --------------------------------------------------------------------------
# Fake exchange (HTTP session level)
# --------------------------------------------------------------------------
@dataclass
class CallRecord:
    seq: int
    phase: str
    method: str
    path: str
    signed: bool
    params: Dict[str, Any]
    t_start: float  # seconds since attempt start
    t_end: float = math.nan


class Book:
    """Top of book.  ``step(ticks)`` shifts bid and ask by ticks (+1 is adverse for a BUY)."""

    def __init__(self, bid: str = "2610.58", ask: str = "2610.59", bid_qty: str = "21.101",
                 ask_qty: str = "5.964") -> None:
        self.bid0, self.ask0 = Decimal(bid), Decimal(ask)
        self.bid_qty, self.ask_qty = Decimal(bid_qty), Decimal(ask_qty)
        self.shift = Decimal("0")

    def step(self, ticks: int) -> None:
        self.shift += Decimal("0.01") * ticks

    def quote(self) -> tuple[Decimal, Decimal]:
        return self.bid0 + self.shift, self.ask0 + self.shift


class FakeExchange:
    """Answers every route the chain calls; records and delays each call."""

    def __init__(self, *, portfolio_margin: bool, latency: float = 0.0,
                 post_latency: Optional[float] = None, book: Optional[Book] = None,
                 clock_skew_ms: int = 0, stamp_fraction: float = 0.5,
                 funding_floor: str = "-0.00300") -> None:
        self.pm = portfolio_margin
        # Real ETHUSDC fundingInfo reports a NEGATIVE adjustedFundingRateFloor.
        self.funding_floor = funding_floor
        self.latency = latency
        self.post_latency = latency if post_latency is None else post_latency
        self.book = book or Book()
        self.clock_skew_ms = clock_skew_ms
        self.stamp_fraction = stamp_fraction
        self.phase = "arm"
        self.calls: List[CallRecord] = []
        self.unhandled: List[str] = []
        self.t0 = time.monotonic()
        self.position_qty = Decimal("0")
        self.algos: Dict[int, Dict[str, Any]] = {}
        self._algo_seq = 1_000_000_000
        self._order_seq = 8_000_000_000
        self.orders_posted: List[Dict[str, Any]] = []
        self.fill_time_ms: Optional[int] = None
        self.fill_price = Decimal("0")
        self.fill_qty = Decimal("0")
        self.fill_client_id = ""
        p = "/papi/v1/um" if portfolio_margin else "/fapi/v1"
        self.order_path = f"{p}/order"
        self.open_orders_path = f"{p}/openOrders"
        self.position_path = "/papi/v1/um/positionRisk" if portfolio_margin else "/fapi/v2/positionRisk"
        self.user_trades_path = f"{p}/userTrades"
        self.algo_post_path = "/papi/v1/um/algo/order" if portfolio_margin else "/fapi/v1/algoOrder"
        self.algo_query_path = "/papi/v1/um/algo/algoOrder" if portfolio_margin else "/fapi/v1/algoOrder"
        self.open_algo_path = ("/papi/v1/um/algo/openAlgoOrders" if portfolio_margin
                               else "/fapi/v1/openAlgoOrders")

    # -- time helpers -----------------------------------------------------
    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self.clock_skew_ms

    def rel(self) -> float:
        return time.monotonic() - self.t0

    # -- dispatch ----------------------------------------------------------
    async def handle(self, method: str, url: str, params: Dict[str, Any]) -> Any:
        path = urlparse(url).path
        clean = {k: v for k, v in params.items() if k not in {"timestamp", "recvWindow", "signature"}}
        rec = CallRecord(len(self.calls), self.phase, method, path, "signature" in params,
                         clean, self.rel())
        self.calls.append(rec)
        latency = self.post_latency if self.phase in {"post_fence", "post"} else self.latency
        await asyncio.sleep(latency * self.stamp_fraction)
        body = self._build(method, path, clean)  # server stamps its data here
        await asyncio.sleep(latency * (1.0 - self.stamp_fraction))
        rec.t_end = self.rel()
        if method == "POST" and path == self.order_path:
            self.phase = "post"
        return body

    def _build(self, method: str, path: str, p: Dict[str, Any]) -> Any:
        now = self._now_ms()
        bid, ask = self.book.quote()
        if (method, path) == ("GET", "/fapi/v1/ticker/bookTicker"):
            return {"lastUpdateId": 11753110753006, "symbol": SYMBOL, "bidPrice": str(bid),
                    "bidQty": str(self.book.bid_qty), "askPrice": str(ask),
                    "askQty": str(self.book.ask_qty), "time": now}
        if (method, path) == ("GET", "/fapi/v1/premiumIndex"):
            return {"symbol": SYMBOL, "markPrice": "2610.66465506", "indexPrice": "2612.13009450",
                    "estimatedSettlePrice": "2610.36981543", "lastFundingRate": "0.00000268",
                    "interestRate": "0.00010000", "nextFundingTime": now + 3_600_000, "time": now}
        if (method, path) == ("GET", "/fapi/v1/depth"):
            n = int(p.get("limit", 100))
            asks = [[str(ask + Decimal("0.01") * i * (1 + i // 10)), str(Decimal("1.5") + Decimal(i % 7) * Decimal("0.4"))]
                    for i in range(n)]
            bids = [[str(bid - Decimal("0.01") * i * (1 + i // 10)), str(Decimal("1.5") + Decimal(i % 5) * Decimal("0.6"))]
                    for i in range(n)]
            return {"lastUpdateId": 11753110753006, "E": now, "T": now, "bids": bids, "asks": asks}
        if (method, path) == ("GET", "/fapi/v1/fundingRate"):
            return [{"symbol": SYMBOL, "fundingRate": "0.00000268", "fundingTime": now - 3600_000 * h,
                     "markPrice": "2610.66"} for h in (24, 16, 8)]
        if (method, path) == ("GET", "/fapi/v1/fundingInfo"):
            return [{"symbol": "BTCUSDC", "adjustedFundingRateCap": "0.02", "adjustedFundingRateFloor": "-0.02",
                     "fundingIntervalHours": 8, "disclaimer": True},
                    {"symbol": SYMBOL, "adjustedFundingRateCap": "0.00300",
                     "adjustedFundingRateFloor": self.funding_floor, "fundingIntervalHours": 8,
                     "disclaimer": True, "updateTime": None}]
        if method == "GET" and path in {"/fapi/v1/commissionRate", "/papi/v1/um/commissionRate"}:
            return {"symbol": SYMBOL, "makerCommissionRate": "0.000200", "takerCommissionRate": "0.000400"}
        if method == "GET" and path in {"/fapi/v1/leverageBracket", "/papi/v1/um/leverageBracket"}:
            return [{"symbol": SYMBOL, "notionalCoef": 1.0, "brackets": [
                {"bracket": 1, "initialLeverage": 100, "notionalCap": 50000, "notionalFloor": 0,
                 "maintMarginRatio": 0.004, "cum": 0.0},
                {"bracket": 2, "initialLeverage": 50, "notionalCap": 500000, "notionalFloor": 50000,
                 "maintMarginRatio": 0.005, "cum": 50.0}]}]
        if method == "GET" and path == self.open_orders_path:
            return []
        if method == "GET" and path == self.position_path:
            return [{"symbol": SYMBOL, "positionAmt": str(self.position_qty), "positionSide": "BOTH",
                     "entryPrice": str(self.fill_price), "markPrice": "2610.66", "leverage": "5",
                     "marginType": "cross", "liquidationPrice": "0", "unRealizedProfit": "0"}]
        if method == "GET" and path in {"/fapi/v2/account", "/papi/v1/account", "/papi/v1/um/account"}:
            return {"accountStatus": "NORMAL", "canTrade": True, "totalWalletBalance": "200",
                    "totalMarginBalance": "200", "availableBalance": "200", "assets": [], "positions": []}
        if method == "GET" and path == "/papi/v1/balance":
            return [{"asset": "USDC", "totalWalletBalance": "200", "crossMarginFree": "200"}]
        if method == "GET" and path == self.user_trades_path:
            if not self.fill_client_id:
                return []
            return [{"symbol": SYMBOL, "id": 770001, "orderId": self.fill_order_id, "side": "BUY" if self.fill_side_buy else "SELL",
                     "price": str(self.fill_price), "qty": str(self.fill_qty), "commission": "0.0198",
                     "commissionAsset": "USDC", "realizedPnl": "0", "maker": False,
                     "buyer": self.fill_side_buy, "positionSide": "BOTH", "time": self.fill_time_ms}]
        if method == "POST" and path == self.order_path:
            return self._post_order(p, now, ask, bid)
        if method == "GET" and path == self.order_path:
            return self._order_status(p)
        if method == "POST" and path == self.algo_post_path:
            self._algo_seq += 1
            row = {"algoId": self._algo_seq, "clientAlgoId": p["clientAlgoId"], "algoType": "CONDITIONAL",
                   "orderType": p["type"], "symbol": p["symbol"], "side": p["side"],
                   "positionSide": p["positionSide"], "quantity": p["quantity"],
                   "triggerPrice": p["triggerPrice"], "workingType": p["workingType"],
                   "reduceOnly": p["reduceOnly"], "closePosition": p["closePosition"],
                   "algoStatus": "NEW", "createTime": now, "updateTime": now}
            self.algos[self._algo_seq] = row
            return dict(row)
        if method == "GET" and path == self.algo_query_path:
            return dict(self.algos[int(p["algoId"])])
        if method == "GET" and path == self.open_algo_path:
            return [dict(r) for r in self.algos.values()]
        self.unhandled.append(f"{method} {path}")
        raise AssertionError(f"unscripted route {method} {path}")

    fill_order_id: int = 0
    fill_side_buy: bool = True

    def _post_order(self, p: Dict[str, Any], now: int, ask: Decimal, bid: Decimal) -> Dict[str, Any]:
        self._order_seq += 1
        qty = Decimal(p["quantity"])
        buy = p["side"] == "BUY"
        price = ask if buy else bid
        self.orders_posted.append(dict(p))
        self.position_qty = qty if buy else -qty
        self.fill_time_ms, self.fill_price, self.fill_qty = now, price, qty
        self.fill_client_id, self.fill_order_id, self.fill_side_buy = p["newClientOrderId"], self._order_seq, buy
        return {"orderId": self._order_seq, "symbol": SYMBOL, "status": "FILLED",
                "clientOrderId": p["newClientOrderId"], "price": "0.00", "avgPrice": str(price),
                "origQty": p["quantity"], "executedQty": p["quantity"], "cumQty": p["quantity"],
                "cumQuote": str(price * qty), "timeInForce": "GTC", "type": "MARKET", "reduceOnly": False,
                "closePosition": False, "side": p["side"], "positionSide": "BOTH", "stopPrice": "0.00",
                "workingType": "CONTRACT_PRICE", "priceProtect": False, "origType": "MARKET",
                "updateTime": now}

    def _order_status(self, p: Dict[str, Any]) -> Dict[str, Any]:
        return {"orderId": self.fill_order_id, "symbol": SYMBOL, "status": "FILLED",
                "clientOrderId": self.fill_client_id, "price": "0.00", "avgPrice": str(self.fill_price),
                "origQty": str(self.fill_qty), "executedQty": str(self.fill_qty), "type": "MARKET",
                "reduceOnly": False, "side": "BUY" if self.fill_side_buy else "SELL",
                "positionSide": "BOTH", "time": self.fill_time_ms, "updateTime": self.fill_time_ms}


class _FakeResponse:
    def __init__(self, body: Any) -> None:
        self.status = 200
        self.headers: Dict[str, str] = {}
        self._body = body

    async def json(self) -> Any:
        return self._body


class _FakeRequestCtx:
    def __init__(self, exchange: FakeExchange, method: str, url: str, kwargs: Dict[str, Any]) -> None:
        self._args = (exchange, method, url, dict(kwargs.get("params") or {}))

    async def __aenter__(self) -> _FakeResponse:
        exchange, method, url, params = self._args
        return _FakeResponse(await exchange.handle(method, url, params))

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeSession:
    """Stand-in for aiohttp.ClientSession injected into the real REST client."""

    def __init__(self, exchange: FakeExchange) -> None:
        self.exchange = exchange

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeRequestCtx:
        return _FakeRequestCtx(self.exchange, method.upper(), url, kwargs)

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# Fake persistence, lease, stream, reconciliation (success paths only)
# --------------------------------------------------------------------------
class DurableMemoryLedger(InMemoryLedger):
    durable_snapshot = True
    symbol = SYMBOL
    venue = "BINANCE_MAINNET"


class FakeProtectionStore:
    def __init__(self) -> None:
        self.records: Dict[tuple[str, str], Dict[str, Any]] = {}

    async def upsert_protection(self, record: Dict[str, Any]) -> Dict[str, Any]:
        stored = dict(record)
        self.records[(str(record["symbol"]).upper(), str(record["entry_client_order_id"]))] = stored
        return dict(stored)

    async def list_active_protections(self, venue: str, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.records.values()
                if r.get("venue") == venue and r.get("state") != "CLOSED"
                and (symbol is None or r.get("symbol") == symbol)]

    async def get_protection(self, venue: str, symbol: str, entry_id: str) -> Optional[Dict[str, Any]]:
        row = self.records.get((symbol.upper(), entry_id))
        return dict(row) if row else None

    async def close_mainnet_unfilled_protection_with_proof(self, symbol: str, entry_id: str,
                                                           proof: Dict[str, Any]) -> Dict[str, Any]:
        row = self.records[(symbol.upper(), entry_id)]
        row.update(state="CLOSED", closure_evidence={"kind": "UNFILLED_ENTRY_TERMINAL",
                                                      "client_order_id": entry_id})
        return dict(row)

    async def close_mainnet_protection_with_proof(self, *a: Any, **k: Any) -> Dict[str, Any]:
        raise AssertionError("close-only flow is outside the entry attempt")


class _Repo:
    def __init__(self) -> None:
        self.algo_protections = FakeProtectionStore()


class FakePersistence:
    def __init__(self, session: Dict[str, Any], ledger: DurableMemoryLedger, db_latency: float) -> None:
        self.mode = type("Mode", (), {"value": "REQUIRED"})()
        self.is_connected = True
        self.session = session
        self.ledger = ledger
        self.db_latency = db_latency
        self.repository = _Repo()
        self.events: List[str] = []

    async def _db(self, name: str) -> None:
        self.events.append(name)
        if self.db_latency:
            await asyncio.sleep(self.db_latency)

    def readiness(self) -> Dict[str, Any]:
        return {"durable": True, "mode": "REQUIRED", "runtime_target": "LOCAL",
                "database_provider": "POSTGRES_LOCAL", "database_host": "127.0.0.1",
                "database_port": 5433, "database_identity_verified": True, "schema_verified": True,
                "pending_outbox": 0, "queue_size": 0, "failed_writes": 0, "dropped_writes": 0,
                "unflushed_writes": 0}

    async def get_mainnet_launch_session(self, launch_id: Optional[str] = None) -> Dict[str, Any]:
        await self._db("get_mainnet_launch_session")
        return dict(self.session)

    async def create_execution_ledger(self, symbol: str, venue: str) -> DurableMemoryLedger:
        await self._db("create_execution_ledger")
        return self.ledger

    async def ensure_order_durable(self, order: Any) -> bool:
        await self._db("ensure_order_durable")
        return True

    async def reserve_mainnet_risk_order(self, launch_id: str, client_id: str, basket_id: str) -> bool:
        await self._db("reserve_mainnet_risk_order")
        s = self.session
        if s["reserved_orders"] != s["submitted_orders"] or s.get("pending_order_client_order_id"):
            return False
        s.update(reserved_orders=s["reserved_orders"] + 1, pending_order_client_order_id=client_id,
                 basket_id=basket_id or s.get("basket_id"))
        return True

    async def bind_mainnet_launch_basket(self, launch_id: str, basket_id: str) -> bool:
        await self._db("bind_mainnet_launch_basket")
        current = self.session.get("basket_id")
        if current in (None, ""):
            self.session["basket_id"] = basket_id
            return True
        return current == basket_id

    def enqueue_order(self, *a: Any, **k: Any) -> None: ...
    def enqueue_fill(self, *a: Any, **k: Any) -> None: ...
    def enqueue_position(self, *a: Any, **k: Any) -> None: ...


class FakeLease:
    fencing_token = 7

    def __init__(self, latency: float) -> None:
        self.latency = latency
        self.checks = 0

    async def assert_valid(self) -> None:
        self.checks += 1
        if self.latency:
            await asyncio.sleep(self.latency)


class FakeUserStream:
    is_connected = True

    def is_healthy(self) -> bool:
        return True

    async def close(self) -> None: ...


class FakeReconciliation:
    """Issues representative REST reads (real routes, real client) and reports IN_SYNC."""

    authentication_failed = False
    algo_protection_repository = None
    last_diffs: list = []

    def __init__(self, adapter: BinanceExecutionAdapter, exchange: FakeExchange) -> None:
        self.adapter, self.exchange = adapter, exchange
        self.last_status = "IN_SYNC"
        self.reconcile_calls = 0

    async def _get(self, path: str, **params: Any) -> Any:
        return await self.adapter.rest_client.request("GET", path, signed=True, params=params)

    async def reconcile(self) -> str:
        self.reconcile_calls += 1
        ex = self.exchange
        account = "/papi/v1/um/account" if ex.pm else "/fapi/v2/account"
        await asyncio.gather(self._get(account), self._get(ex.position_path),
                             self._get(ex.open_orders_path, symbol=SYMBOL))
        await self._get(ex.open_algo_path)
        self.last_status = "IN_SYNC"
        return "IN_SYNC"

    async def _recover_order_fills(self, order: Any, status: Dict[str, Any]) -> int:
        trades = await self._get(self.exchange.user_trades_path, symbol=SYMBOL,
                                 orderId=status.get("orderId"))
        for t in trades:
            await self.adapter.ledger.append_fill(ExchangeFill(
                exchange_trade_id=str(t["id"]), exchange_order_id=str(t["orderId"]),
                client_order_id=order.client_order_id, symbol=SYMBOL,
                side=OrderSide(t["side"]), position_side=PositionSide.BOTH,
                quantity=Decimal(t["qty"]), price=Decimal(t["price"]),
                commission=Decimal(t["commission"]), commission_asset=t["commissionAsset"],
                realized_pnl=Decimal(t["realizedPnl"]), maker=bool(t["maker"]),
                event_time=int(t["time"]), transaction_time=int(t["time"]), source="REST"))
        return len(trades)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
@dataclass
class AttemptResult:
    outcome: str = "UNKNOWN"          # PROTECTED / ORDER_POSTED / PREPLAN_FAILED / EXECUTION_BLOCKED / ...
    reason: str = ""
    order_posted: bool = False
    protection_started: bool = False  # an algo POST was sent
    protected: bool = False
    elapsed: float = 0.0
    marks: Dict[str, float] = field(default_factory=dict)
    calls: List[CallRecord] = field(default_factory=list)
    log: List[str] = field(default_factory=list)
    provider_none: List[str] = field(default_factory=list)
    unhandled: List[str] = field(default_factory=list)
    fail_closed: List[str] = field(default_factory=list)
    last_order_block: Optional[Dict[str, str]] = None
    adapter_state: str = ""

    def route_counts(self, *phases: str) -> Counter:
        return Counter(f"{c.method} {c.path}" for c in self.calls if c.phase in phases)

    def phase_calls(self, phase: str) -> int:
        return sum(1 for c in self.calls if c.phase == phase)

    def hops(self, *phases: str) -> int:
        """Sequential latency hops: overlapping (gathered) calls count once."""
        spans = sorted((c.t_start, c.t_end) for c in self.calls if c.phase in phases)
        hops, end = 0, -1.0
        for start, stop in spans:
            if start >= end - 1e-9:
                hops += 1
                end = stop
            else:
                end = max(end, stop)
        return hops

    def has_log(self, text: str) -> bool:
        return any(text in line for line in self.log)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.name}:{record.levelname}:{record.getMessage()}")


@dataclass
class Config:
    portfolio_margin: bool = False
    latency: float = 0.0
    post_latency: Optional[float] = None
    ws: str = "live"                  # "live" | "none" (REST-only market data)
    ws_interval: float = 0.1
    ask_step_after: Optional[str] = None  # "clamp" | "gate1": one WS book frame lands right after
    ask_step_ticks: int = 1             # +1 adverse for a BUY, -1 favourable
    db_latency: float = 0.003
    snapshot_age: float = 0.0
    clock_skew_ms: int = 0
    shim_keys: bool = False           # bypass validated_* key loss (see SHIM_DOC)
    shim_hashes: bool = False         # bypass pilot hash binding mismatch (see SHIM_DOC)
    positive_funding_floor: bool = False  # bypass the negative-floor rejection (see SHIM_DOC)


SHIM_DOC = (
    "shim_keys / shim_hashes: see install_shim. positive_funding_floor: fake fundingInfo returns adjustedFundingRateFloor as +0.003 instead "
    "of the real -0.003, working around execution.get_local_mainnet_cost_evidence calling "
    "_decimal_value(..., nonnegative=True) on it. "
    "shim_risk_context: echoes validated_quantity/validated_entry_price from the caller's "
    "context and lifts pilot source/dependency/migration hashes to top-level keys, working "
    "around execution.get_local_mainnet_risk_context (never sets them) vs "
    "main._clamp_order_notional_if_needed / gates._local_mainnet_risk_gate (read them)."
)


def _snapshot(age: float) -> ExchangeAccountSnapshot:
    now = utc_now()
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return ExchangeAccountSnapshot(
        wallet_balance=Decimal("200"), margin_balance=Decimal("200"), available_balance=Decimal("200"),
        unrealized_pnl=Decimal("0"), total_initial_margin=Decimal("0"), total_maint_margin=Decimal("0"),
        position_initial_margin=Decimal("0"), total_position_notional=Decimal("0"),
        effective_leverage=Decimal("0"), margin_utilization_pct=Decimal("0"),
        min_liquidation_distance_pct=None, liquidation_safety="KNOWN",
        exchange_environment="BINANCE_MAINNET", daily_realized_pnl=Decimal("0"), daily_loss_known=True,
        collateral_asset="USDC", risk_currency="USDC", daily_loss_asset="USDC",
        daily_pnl_includes_fees=True, daily_pnl_includes_funding=True,
        daily_loss_window_start=start, daily_loss_window_end=start + timedelta(days=1),
        configured_leverage=Decimal("5"), configured_leverage_known=True, margin_mode="CROSS",
        margin_mode_known=True, valid=True, timestamp=now - timedelta(seconds=age))


def _launch_session() -> Dict[str, Any]:
    h = lambda tag: hashlib.sha256(tag.encode()).hexdigest()  # noqa: E731
    return {
        "launch_id": LAUNCH_ID, "symbol": SYMBOL, "runtime_target": "LOCAL",
        "runtime_fingerprint": FINGERPRINT, "image_digest": None, "approval_id": APPROVAL_ID,
        "policy": "LIVE_RESEARCH_PILOT", "state": "ACTIVE", "pilot_status": "ACTIVE",
        "pilot_drawdown_triggered": False, "pilot_campaign_id": CAMPAIGN_ID,
        "pilot_campaign_expires_at": utc_now() + timedelta(hours=24),
        "reserved_orders": 0, "submitted_orders": 0, "pending_order_client_order_id": None,
        "basket_id": None, "first_order_client_order_id": None,
        "pilot_risk_policy_hash": LOCAL_LIVE_PILOT_POLICY_SHA256,
        "pilot_strategy_hash": h("strategy"), "pilot_source_hash": h("source"),
        "pilot_dependency_hash": h("deps"), "pilot_migration_hash": h("migrations"),
        "pilot_management_mode": "QUICK", "pilot_net_pnl_usdc": Decimal("0"),
        "pilot_peak_pnl_usdc": Decimal("0"), "pilot_accounting_resume_eligible": False,
    }


def make_decision(*, qty: str = "0.01", decision_id: str = "DEC-REALPATH-1") -> ExecutionDecision:
    now = utc_now()
    intent = OrderIntent(
        client_order_id="CID-REALPATH-BUY-1", symbol=SYMBOL, market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY, position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, quantity=Decimal(qty), created_at=now,
        strategy_id="grid", metadata={"strategy_id": "grid"})
    return ExecutionDecision(
        decision_id=decision_id, symbol=SYMBOL, action="SUBMIT_ORDER", strategy_id="grid",
        risk_class=EconomicRiskClass.NEW_RISK, orders=[intent],
        net_exposure_delta=Decimal(qty), timestamp=now)


def apply_env(monkeypatch: Any, portfolio_margin: bool) -> None:
    for name, value in {
        "LOCAL_RUNTIME_TARGET": "LOCAL", "LOCAL_ONLY": "true", "MAINNET_LIVE_APPROVED": "true",
        "LOCAL_LIVE_PILOT_CAMPAIGN_ID": CAMPAIGN_ID, "LOCAL_LIVE_PILOT_STRATEGY_ID": "grid",
        "LOCAL_SOURCE_FINGERPRINT": FINGERPRINT, "MAINNET_RELEASE_APPROVAL_ID": APPROVAL_ID,
        "BINANCE_PORTFOLIO_MARGIN": "true" if portfolio_margin else "false",
        "BINANCE_MAINNET_API_KEY": "unit-test-key", "BINANCE_MAINNET_API_SECRET": "unit-test-secret",
        # The Local supervisor passes the verified runtime hashes to the worker.
        "LOCAL_LIVE_PILOT_SOURCE_HASH": hashlib.sha256(b"source").hexdigest(),
        "LOCAL_LIVE_PILOT_DEPENDENCY_HASH": hashlib.sha256(b"deps").hexdigest(),
        "LOCAL_LIVE_PILOT_MIGRATION_HASH": hashlib.sha256(b"migrations").hexdigest(),
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("MAX_MARKET_DATA_AGE_SEC", "ACCOUNT_SNAPSHOT_MAX_AGE_SEC", "BINANCE_REQUEST_TIMEOUT_SEC",
                 "MAINNET_MAX_COLLATERAL", "MAINNET_MAX_SINGLE_ORDER_NOTIONAL"):
        monkeypatch.delenv(name, raising=False)


def _tripwire_network(monkeypatch: Any) -> None:
    import aiohttp

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("real aiohttp.ClientSession must never be created by this harness")

    monkeypatch.setattr(aiohttp, "ClientSession", boom)


def install_shim(adapter: BinanceExecutionAdapter, *, keys: bool, hashes: bool) -> None:
    original = adapter.get_local_mainnet_risk_context

    async def shim_risk_context(intent: Any, execution_context: Dict[str, Any]) -> Any:
        result = await original(intent, execution_context)
        if isinstance(result, dict):
            result = dict(result)
            if keys:
                result.setdefault("validated_quantity", execution_context.get("validated_quantity"))
                result.setdefault("validated_entry_price", execution_context.get("validated_entry_price"))
            if hashes:
                pilot = result.get("live_research_pilot") or {}
                for key in ("source_hash", "dependency_hash", "migration_hash"):
                    result.setdefault(key, pilot.get(key))
        return result

    adapter.get_local_mainnet_risk_context = shim_risk_context  # type: ignore[method-assign]


class Harness:
    def __init__(self, monkeypatch: Any, cfg: Config) -> None:
        self.mp, self.cfg = monkeypatch, cfg
        self.worker: TradingWorkerApp
        self.adapter: BinanceExecutionAdapter
        self.exchange: FakeExchange
        self.persistence: FakePersistence
        self.result = AttemptResult()
        self._gate_calls = 0
        self._feeder: Optional[asyncio.Task] = None
        self._t0 = 0.0

    # -- construction -----------------------------------------------------
    async def build(self) -> None:
        mp, cfg = self.mp, self.cfg
        apply_env(mp, cfg.portfolio_margin)
        _tripwire_network(mp)
        ready = lambda: {"can_start": True, "blockers": [], "status": "READY"}  # noqa: E731
        mp.setattr(execution_module, "local_live_pilot_readiness", ready)
        mp.setattr(worker_main, "local_live_pilot_readiness", ready)

        self.exchange = FakeExchange(
            portfolio_margin=cfg.portfolio_margin, latency=0.0, post_latency=0.0,
            book=Book(), clock_skew_ms=cfg.clock_skew_ms,
            funding_floor="-0.00300")  # real Binance floors are negative

        ledger = DurableMemoryLedger()
        await ledger.mark_initialized()
        await ledger.set_account_snapshot(_snapshot(cfg.snapshot_age))
        adapter = BinanceExecutionAdapter(
            api_key="unit-test-key", api_secret="unit-test-secret", env=BinanceEnvironment.MAINNET,
            ledger=ledger, portfolio_margin=cfg.portfolio_margin)
        assert adapter.portfolio_margin is cfg.portfolio_margin
        adapter.rest_client.session = FakeSession(self.exchange)  # type: ignore[assignment]
        adapter.state = ConnectionState.READY
        adapter.capabilities.account_request_succeeded = True
        adapter.capabilities.authenticated = True
        adapter.capabilities.trade_authorized = True
        adapter.capabilities.position_mode_known = True
        adapter.capabilities.hedge_mode = False
        adapter.capabilities.symbol_rules[SYMBOL] = make_rules()
        adapter.user_stream = FakeUserStream()  # type: ignore[assignment]
        adapter.reconciliation = FakeReconciliation(adapter, self.exchange)  # type: ignore[assignment]
        adapter.set_execution_lease(FakeLease(cfg.db_latency), required=True)  # type: ignore[arg-type]
        self.adapter = adapter

        worker = TradingWorkerApp(symbols=[SYMBOL])
        worker.execution_mode = WorkerExecutionMode.LIVE
        worker.engine_state = WorkerEngineState.ARMED
        worker.execution_adapter = adapter
        worker.symbols = [SYMBOL]
        worker.active_configuration = {
            "executionMode": "LIVE", "instruments": [SYMBOL],
            "strategies": {"grid": True, "trend": False, "shock": False, "carry": False},
            "riskProfile": "CONSERVATIVE"}
        worker.pause_new_risk = False
        worker.recovery_only = False
        worker.kill_switch_active = False
        worker.private_stream_healthy = True
        worker.authenticated = True
        worker.connection_state = "READY"
        worker.reconciliation_status = "IN_SYNC"
        session = _launch_session()
        self.persistence = FakePersistence(session, ledger, cfg.db_latency)
        worker.persistence = self.persistence  # type: ignore[assignment]
        worker._set_mainnet_launch_session(dict(session))
        mp.setattr(worker, "_mainnet_configured", lambda: True)
        now = utc_now()
        worker._pilot_lifecycle_monitor_started_at = now
        worker._pilot_lifecycle_monitor_completed_at = now
        worker._pilot_lifecycle_monitor_last_success_at = now
        worker._pilot_lifecycle_monitor_last_error = None
        worker.record_local_supervisor_heartbeat()

        async def record_fail_closed(error: Exception) -> dict:
            self.result.fail_closed.append(f"{type(error).__name__}: {error}")
            return {}

        mp.setattr(worker, "_fail_closed_after_autonomous_execution_error", record_fail_closed)
        adapter.bind_worker_authority(worker)
        adapter.before_order_submission = worker._before_order_submission

        async def on_submission_result(order: Any, outcome: str) -> None:
            self.persistence.events.append(f"submission_result:{outcome}")

        adapter.on_order_submission_result = on_submission_result
        adapter.on_local_mainnet_protection_update = worker._persist_local_mainnet_protection_update
        adapter.on_local_mainnet_close_verified = worker._persist_local_mainnet_close_verified
        self.worker = worker

        self._install_tracking()
        if cfg.shim_keys or cfg.shim_hashes:
            install_shim(adapter, keys=cfg.shim_keys, hashes=cfg.shim_hashes)

        # What ARM does: fetch one fresh book (REST), seed the worker clock.
        assert await adapter.refresh_market_data([SYMBOL])
        worker.last_market_event_at.update(adapter.last_market_event_at)
        worker.market_data_healthy = True
        self.exchange.calls.clear()  # ARM calls are not part of the attempt

    def _install_tracking(self) -> None:
        adapter, ex, result = self.adapter, self.exchange, self.result
        original_check = adapter.order_gate.check

        async def tracked_check(*args: Any, **kwargs: Any) -> Any:
            self._gate_calls += 1
            first = self._gate_calls == 1
            ex.phase = "gate1" if first else "fence"
            result.marks[f"{ex.phase}_start"] = self._rel()
            try:
                return await original_check(*args, **kwargs)
            finally:
                result.marks[f"{ex.phase}_end"] = self._rel()
                ex.phase = "pre_post" if first else "post_fence"
                if first and self.cfg.ask_step_after == "gate1":
                    self._apply_book_step()

        adapter.order_gate.check = tracked_check  # type: ignore[method-assign]

        for name in ("get_local_mainnet_risk_context", "get_local_mainnet_cost_evidence"):
            original = getattr(adapter, name)

            async def wrapped(*args: Any, orig_fn: Callable = original, fn_name: str = name, **kwargs: Any) -> Any:
                value = await orig_fn(*args, **kwargs)
                if value is None:
                    result.provider_none.append(f"{ex.phase}:{fn_name}:{self.runtime_suspects()}")
                return value

            setattr(adapter, name, wrapped)

        evidence = adapter._local_mainnet_runtime_evidence

        async def wrapped_evidence(*args: Any, **kwargs: Any) -> Any:
            value = await evidence(*args, **kwargs)
            if value is None:
                result.provider_none.append(f"{ex.phase}:_local_mainnet_runtime_evidence:{self.runtime_suspects()}")
            return value

        adapter._local_mainnet_runtime_evidence = wrapped_evidence  # type: ignore[method-assign]

    def _apply_book_step(self) -> None:
        """One adverse/favourable tick lands as a WS book frame, right now."""
        self.exchange.book.step(self.cfg.ask_step_ticks)
        self._push_ws_frame()

    def _push_ws_frame(self) -> None:
        bid, ask = self.exchange.book.quote()
        now = utc_now()
        event = MarketEvent(
            event_id=f"ws-{time.monotonic_ns()}", event_time=now, venue="BINANCE_MAINNET",
            symbol=SYMBOL, market_type=MarketType.USDM_FUTURES, last_price=ask,
            best_bid=bid, best_ask=ask)
        if self.adapter.record_market_event(event):
            self.worker.last_market_event_at[SYMBOL] = now

    def runtime_suspects(self) -> Dict[str, Any]:
        a, w = self.adapter, self.worker
        snap_age = (utc_now() - a.account_snapshot.timestamp).total_seconds() if a.account_snapshot else None
        sample = a.last_market_event_at.get(SYMBOL)
        return {
            "snapshot_age_s": None if snap_age is None else round(snap_age, 3),
            "adapter_sample_age_s": None if sample is None else round((utc_now() - sample).total_seconds(), 3),
            "worker_market_fresh": w.is_market_data_fresh([SYMBOL]),
            "hb_fresh": w.local_supervisor_heartbeat_is_fresh(),
        }

    def _rel(self) -> float:
        return time.monotonic() - self._t0

    # -- WS feeder ----------------------------------------------------------
    async def _ws_feeder(self) -> None:
        while True:
            self._push_ws_frame()
            await asyncio.sleep(self.cfg.ws_interval)

    # -- the attempt --------------------------------------------------------
    async def attempt(self, decision: Optional[ExecutionDecision] = None) -> AttemptResult:
        cfg, ex, res = self.cfg, self.exchange, self.result
        capture = _Capture()
        root = logging.getLogger()
        root.addHandler(capture)
        decision = decision or make_decision()
        if cfg.ws == "live":
            self._feeder = asyncio.create_task(self._ws_feeder())
            await asyncio.sleep(0)  # first WS frame lands before the strategy decision
        ex.latency, ex.post_latency = cfg.latency, (cfg.latency if cfg.post_latency is None else cfg.post_latency)
        self._t0 = ex.t0 = time.monotonic()
        ex.phase = "clamp"
        try:
            res.marks["clamp_start"] = 0.0
            try:
                decision = await self.worker._clamp_order_notional_if_needed(decision, Decimal("2610.59"))
            except Exception as exc:  # handle_market_event: PREPLAN_FAILED
                res.outcome, res.reason = "PREPLAN_FAILED", f"{type(exc).__name__}: {exc}"
                return res
            res.marks["clamp_end"] = self._rel()
            if cfg.ask_step_after == "clamp":
                self._apply_book_step()
            ex.phase = "worker_gate"
            allowed, reason = self.worker._evaluate_execution_gate(decision)
            if not allowed:
                res.outcome, res.reason = "EXECUTION_BLOCKED", reason
                return res
            try:
                await self.worker.execute_manual_decision(decision)
            except Exception as exc:
                res.outcome, res.reason = "EXECUTION_ERROR", f"{type(exc).__name__}: {exc}"
                return res
            res.outcome = "RETURNED"
            return res
        finally:
            res.elapsed = self._rel()
            if self._feeder:
                self._feeder.cancel()
                await asyncio.gather(self._feeder, return_exceptions=True)
            root.removeHandler(capture)
            self._finish(capture)

    def _finish(self, capture: _Capture) -> None:
        res, ex, a = self.result, self.exchange, self.adapter
        res.calls = list(ex.calls)
        res.log = capture.lines
        res.unhandled = list(ex.unhandled)
        res.last_order_block = a.last_order_block
        res.adapter_state = str(getattr(a.state, "value", a.state))
        res.order_posted = bool(ex.orders_posted)
        res.protection_started = any(c.method == "POST" and c.path == ex.algo_post_path for c in ex.calls)
        res.protected = a.last_local_mainnet_protection.get("status") == "PROTECTED"
        if res.outcome == "RETURNED":
            if res.protected:
                res.outcome = "PROTECTED"
            elif res.order_posted:
                res.outcome = "ORDER_POSTED"
            else:
                res.outcome = "NO_ORDER"
        if not res.reason:
            failed = [line for line in res.log if "post-fill protection failed" in line]
            if failed:
                res.reason = failed[0]
            elif a.last_order_block:
                res.reason = f"{a.last_order_block['stage']}: {a.last_order_block['reason']}"
            else:
                interesting = [line for line in res.log if "gate" in line.lower() or "fence" in line.lower()
                               or "stale" in line.lower() or "Blocked" in line]
                res.reason = interesting[-1] if interesting else (res.log[-1] if res.log else "")


async def run_scenario(monkeypatch: Any, cfg: Config) -> tuple[AttemptResult, Harness]:
    harness = Harness(monkeypatch, cfg)
    await harness.build()
    result = await harness.attempt()
    return result, harness


def run(monkeypatch: Any, **cfg: Any) -> tuple[AttemptResult, Harness]:
    """``shim=True`` enables every documented defect bypass (SHIM_DOC)."""
    cfg.pop("shim", None)  # defect bypasses were removed with the fixes
    return asyncio.run(run_scenario(monkeypatch, Config(**cfg)))
