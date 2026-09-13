"""
Binance Global USDⓈ-M Futures Adapter
Production-ready native async implementation of DerivativesVenueAdapter.
Uses Binance Native REST API and WebSocket streams.
Strictly excludes CCXT from the live execution hot path.
"""

import asyncio
import hmac
import hashlib
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Coroutine, Dict, List, Optional
from datetime import datetime, timezone
import aiohttp
import structlog

from venues.base.adapter import (
    DerivativesVenueAdapter,
    SymbolInfo,
    BalanceInfo,
    PositionInfo,
    OrderRequest,
    OrderResponse,
    FundingInfo,
)

logger = structlog.get_logger()


def _required_positive_decimal(value: Any, field_name: str) -> Decimal:
    """Parse a required positive exchange value without a venue fallback."""

    if value in (None, ""):
        raise ValueError(f"Binance response is missing {field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Binance response has invalid {field_name}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"Binance response has unusable {field_name}")
    return parsed


def _required_decimal(value: Any, field_name: str) -> Decimal:
    """Parse a required finite exchange value, including a valid zero."""

    if value in (None, ""):
        raise ValueError(f"Binance response is missing {field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Binance response has invalid {field_name}") from exc
    if not parsed.is_finite():
        raise ValueError(f"Binance response has non-finite {field_name}")
    return parsed


def _required_nonnegative_decimal(value: Any, field_name: str) -> Decimal:
    parsed = _required_decimal(value, field_name)
    if parsed < 0:
        raise ValueError(f"Binance response has negative {field_name}")
    return parsed


def _required_nonnegative_int(value: Any, field_name: str) -> int:
    if value in (None, ""):
        raise ValueError(f"Binance response is missing {field_name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Binance response has invalid {field_name}") from exc
    if parsed < 0:
        raise ValueError(f"Binance response has negative {field_name}")
    return parsed


def _symbol_filters(symbol_data: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    filters = symbol_data.get("filters")
    if not isinstance(filters, list):
        raise ValueError("Binance exchangeInfo symbol is missing filters")
    by_type = {
        str(item.get("filterType", "")).upper(): item
        for item in filters
        if isinstance(item, dict)
    }
    price_filter = by_type.get("PRICE_FILTER")
    lot_filter = by_type.get("LOT_SIZE")
    notional_filter = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL")
    if not price_filter or not lot_filter or not notional_filter:
        raise ValueError(
            "Binance exchangeInfo symbol must include PRICE_FILTER, LOT_SIZE, and MIN_NOTIONAL/NOTIONAL"
        )
    return price_filter, lot_filter, notional_filter


class BinanceGlobalUSDMAdapter(DerivativesVenueAdapter):
    """
    Read-only compatibility adapter for legacy data consumers.

    Mutable exchange authority belongs exclusively to
    ``apps.trading_worker.venues.binance.BinanceExecutionAdapter``.  Keeping
    this legacy interface read-only prevents a caller from bypassing the
    Worker decision and order gates.

    Endpoints:
      REST: https://testnet.binancefuture.com
      WSS:  wss://stream.binancefuture.com/ws
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        recv_window: int = 5000,
    ):
        if not testnet:
            raise ValueError("LIVE/Mainnet mutable execution is permanently blocked.")
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.recv_window = recv_window
        self.rest_base = (
            "https://testnet.binancefuture.com"
            if testnet
            else "https://fapi.binance.com"
        )
        self.ws_base = (
            "wss://stream.binancefuture.com/ws"
            if testnet
            else "wss://fstream.binance.com/ws"
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._listen_key: Optional[str] = None
        self._ws_tasks: List[asyncio.Task] = []
        self._is_connected = False

    def _sign(self, params: Dict[str, Any]) -> str:
        query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query_string}&signature={signature}"

    @staticmethod
    def _mutable_execution_is_worker_only() -> None:
        raise RuntimeError(
            "Legacy BinanceGlobalUSDMAdapter is read-only; route mutations "
            "through the Python Trading Worker execution adapter."
        )

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            headers = {"X-MBX-APIKEY": self.api_key}
            self._session = aiohttp.ClientSession(headers=headers)
        self._is_connected = True
        logger.info("binance_adapter_connected", testnet=self.testnet)

    async def disconnect(self) -> None:
        self._is_connected = False
        for task in self._ws_tasks:
            task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("binance_adapter_disconnected")

    async def subscribe_trades(self, symbol: str, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        raise RuntimeError(
            "Legacy BinanceGlobalUSDMAdapter does not implement streams; use the managed Worker adapter"
        )

    async def subscribe_orderbook(self, symbol: str, depth: int, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        raise RuntimeError(
            "Legacy BinanceGlobalUSDMAdapter does not implement streams; use the managed Worker adapter"
        )

    async def subscribe_mark_price(self, symbol: str, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        raise RuntimeError(
            "Legacy BinanceGlobalUSDMAdapter does not implement streams; use the managed Worker adapter"
        )

    async def subscribe_account(self, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        raise RuntimeError(
            "Legacy BinanceGlobalUSDMAdapter does not implement streams; use the managed Worker adapter"
        )

    async def subscribe_orders(self, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        raise RuntimeError(
            "Legacy BinanceGlobalUSDMAdapter does not implement streams; use the managed Worker adapter"
        )

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        assert self._session is not None
        url = f"{self.rest_base}/fapi/v1/exchangeInfo"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch futures exchangeInfo: HTTP {resp.status}")
            data = await resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
                raise ValueError("Binance exchangeInfo response is invalid")
            for s in data["symbols"]:
                if not isinstance(s, dict):
                    continue
                if s.get("symbol") == symbol:
                    if s.get("status") != "TRADING":
                        raise ValueError(f"Symbol {symbol} is not TRADING")
                    if not s.get("baseAsset") or not s.get("quoteAsset"):
                        raise ValueError(f"ExchangeInfo symbol {symbol} is missing asset metadata")
                    price_filter, lot_filter, notional_filter = _symbol_filters(s)
                    tick_size = _required_positive_decimal(
                        price_filter.get("tickSize"), "PRICE_FILTER.tickSize"
                    )
                    step_size = _required_positive_decimal(
                        lot_filter.get("stepSize"), "LOT_SIZE.stepSize"
                    )
                    min_qty = _required_positive_decimal(
                        lot_filter.get("minQty"), "LOT_SIZE.minQty"
                    )
                    max_qty = _required_positive_decimal(
                        lot_filter.get("maxQty"), "LOT_SIZE.maxQty"
                    )
                    if max_qty < min_qty:
                        raise ValueError("ExchangeInfo LOT_SIZE maxQty is below minQty")
                    min_notional = _required_positive_decimal(
                        notional_filter.get(
                            "notional", notional_filter.get("minNotional")
                        ),
                        "MIN_NOTIONAL.notional",
                    )

                    return SymbolInfo(
                        symbol=s["symbol"],
                        venue="binance_global",
                        market_type="USDM_PERP",
                        base_asset=s["baseAsset"],
                        quote_asset=s["quoteAsset"],
                        contract_size=Decimal("1.0"),
                        price_precision=_required_nonnegative_int(
                            s.get("pricePrecision"), "pricePrecision"
                        ),
                        quantity_precision=_required_nonnegative_int(
                            s.get("quantityPrecision"), "quantityPrecision"
                        ),
                        tick_size=tick_size,
                        step_size=step_size,
                        min_notional=min_notional,
                        max_leverage=None,
                    )
            raise ValueError(f"Symbol {symbol} not found on Binance USDⓈ-M exchange info")

    async def get_balance(self, asset: str = "USDT") -> BalanceInfo:
        assert self._session is not None
        params = {"timestamp": int(time.time() * 1000), "recvWindow": self.recv_window}
        signed_qs = self._sign(params)
        url = f"{self.rest_base}/fapi/v2/account?{signed_qs}"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch futures account: HTTP {resp.status}")
            data = await resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
                raise ValueError("Binance account response is invalid")
            for b in data["assets"]:
                if not isinstance(b, dict):
                    continue
                if b.get("asset") == asset:
                    return BalanceInfo(
                        asset=b["asset"],
                        wallet_balance=_required_decimal(b.get("walletBalance"), "walletBalance"),
                        available_balance=_required_decimal(b.get("availableBalance"), "availableBalance"),
                        unrealized_pnl=_required_decimal(b.get("unrealizedProfit"), "unrealizedProfit"),
                        margin_balance=_required_decimal(b.get("marginBalance"), "marginBalance"),
                    )
            raise ValueError(f"Binance account response does not contain asset {asset}")

    async def get_position(self, symbol: str) -> PositionInfo:
        positions = await self.get_positions()
        for p in positions:
            if p.symbol == symbol:
                return p
        return PositionInfo(
            symbol=symbol,
            direction="FLAT",
            quantity=Decimal("0.0"),
            entry_price=Decimal("0.0"),
            mark_price=Decimal("0.0"),
            liquidation_price=None,
            unrealized_pnl=Decimal("0.0"),
            leverage=Decimal("1.0"),
            margin_type="CROSS",
            isolated_margin=Decimal("0.0"),
            updated_at=datetime.now(timezone.utc),
        )

    async def get_positions(self) -> List[PositionInfo]:
        assert self._session is not None
        params = {"timestamp": int(time.time() * 1000), "recvWindow": self.recv_window}
        signed_qs = self._sign(params)
        url = f"{self.rest_base}/fapi/v2/positionRisk?{signed_qs}"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch position risk: HTTP {resp.status}")
            data = await resp.json()
            if not isinstance(data, list):
                raise ValueError("Binance position risk response is invalid")
            result = []
            for p in data:
                if not isinstance(p, dict) or not p.get("symbol"):
                    raise ValueError("Binance position risk entry is invalid")
                amt = _required_decimal(p.get("positionAmt"), "positionAmt")
                entry_price = _required_decimal(p.get("entryPrice"), "entryPrice")
                mark_price = _required_positive_decimal(p.get("markPrice"), "markPrice")
                unrealized_pnl = _required_decimal(
                    p.get("unRealizedProfit"), "unRealizedProfit"
                )
                leverage = _required_positive_decimal(p.get("leverage"), "leverage")
                margin_type = p.get("marginType")
                if not isinstance(margin_type, str) or not margin_type.strip():
                    raise ValueError("Binance position risk entry is missing marginType")
                direction = "FLAT"
                if amt > 0:
                    direction = "LONG"
                elif amt < 0:
                    direction = "SHORT"

                raw_liq_price = p.get("liquidationPrice")
                liq_price = None
                if raw_liq_price not in (None, ""):
                    parsed_liq_price = _required_decimal(raw_liq_price, "liquidationPrice")
                    if parsed_liq_price > 0:
                        liq_price = parsed_liq_price
                result.append(
                    PositionInfo(
                        symbol=p["symbol"],
                        direction=direction,
                        quantity=abs(amt),
                        entry_price=entry_price,
                        mark_price=mark_price,
                        liquidation_price=liq_price,
                        unrealized_pnl=unrealized_pnl,
                        leverage=leverage,
                        margin_type=margin_type.upper(),
                        isolated_margin=_required_nonnegative_decimal(
                            p.get("isolatedMargin"), "isolatedMargin"
                        ),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            return result

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderResponse]:
        assert self._session is not None
        params: Dict[str, Any] = {"timestamp": int(time.time() * 1000), "recvWindow": self.recv_window}
        if symbol:
            params["symbol"] = symbol
        signed_qs = self._sign(params)
        url = f"{self.rest_base}/fapi/v1/openOrders?{signed_qs}"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch open orders: HTTP {resp.status}")
            data = await resp.json()
            if not isinstance(data, list):
                raise ValueError("Binance open orders response is invalid")
            res = []
            for o in data:
                if not isinstance(o, dict):
                    raise ValueError("Binance open order entry is invalid")
                client_order_id = o.get("clientOrderId")
                exchange_order_id = o.get("orderId")
                order_symbol = o.get("symbol")
                order_status = o.get("status")
                order_type = o.get("type")
                if any(value in (None, "") for value in (
                    client_order_id,
                    exchange_order_id,
                    order_symbol,
                    order_status,
                    order_type,
                    o.get("origQty"),
                    o.get("executedQty"),
                    o.get("time"),
                )):
                    raise ValueError("Binance open order entry is missing required fields")
                raw_price = o.get("price")
                price = (
                    None
                    if raw_price in (None, "", "0", "0.0", 0)
                    else _required_positive_decimal(raw_price, "price")
                )
                if str(order_type).upper() in {"LIMIT", "LIMIT_MAKER"} and price is None:
                    raise ValueError("Binance limit order is missing price")
                res.append(
                    OrderResponse(
                        client_order_id=str(client_order_id),
                        exchange_order_id=str(exchange_order_id),
                        symbol=str(order_symbol).upper(),
                        status=str(order_status).upper(),
                        price=price,
                        quantity=_required_positive_decimal(o.get("origQty"), "origQty"),
                        filled_quantity=_required_nonnegative_decimal(
                            o.get("executedQty"), "executedQty"
                        ),
                        avg_fill_price=(
                            _required_positive_decimal(o.get("avgPrice"), "avgPrice")
                            if o.get("avgPrice") not in (None, "", "0", 0)
                            else None
                        ),
                        transact_time=datetime.fromtimestamp(
                            _required_nonnegative_int(o.get("time"), "time") / 1000,
                            tz=timezone.utc,
                        ),
                    )
                )
            return res

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        self._mutable_execution_is_worker_only()
        raise AssertionError("unreachable")

    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        self._mutable_execution_is_worker_only()
        raise AssertionError("unreachable")

    async def amend_order(self, symbol: str, client_order_id: str, new_price: Decimal, new_quantity: Decimal) -> OrderResponse:
        self._mutable_execution_is_worker_only()
        raise AssertionError("unreachable")

    async def emergency_flatten(self, symbol: Optional[str] = None) -> List[OrderResponse]:
        self._mutable_execution_is_worker_only()
        raise AssertionError("unreachable")

    async def get_funding_rate(self, symbol: str) -> FundingInfo:
        assert self._session is not None
        url = f"{self.rest_base}/fapi/v1/premiumIndex?symbol={symbol}"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch premium index: HTTP {resp.status}")
            data = await resp.json()
            if not isinstance(data, dict):
                raise ValueError("Binance premium index response is invalid")
            response_symbol = data.get("symbol")
            if response_symbol != symbol:
                raise ValueError("Binance premium index response symbol does not match request")
            return FundingInfo(
                symbol=str(response_symbol).upper(),
                funding_rate=_required_decimal(data.get("lastFundingRate"), "lastFundingRate"),
                funding_time=datetime.fromtimestamp(
                    int(data["nextFundingTime"]) / 1000, tz=timezone.utc
                ),
                mark_price=_required_positive_decimal(data.get("markPrice"), "markPrice"),
                index_price=_required_positive_decimal(data.get("indexPrice"), "indexPrice"),
                estimated_settle_rate=(
                    _required_decimal(data.get("interestRate"), "interestRate")
                    if data.get("interestRate") not in (None, "")
                    else None
                ),
            )

    async def get_mark_price(self, symbol: str) -> Decimal:
        info = await self.get_funding_rate(symbol)
        return info.mark_price

    async def get_index_price(self, symbol: str) -> Decimal:
        info = await self.get_funding_rate(symbol)
        return info.index_price

    async def get_open_interest(self, symbol: str) -> Dict[str, Any]:
        assert self._session is not None
        url = f"{self.rest_base}/fapi/v1/openInterest?symbol={symbol}"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch open interest: HTTP {resp.status}")
            data = await resp.json()
            if not isinstance(data, dict):
                raise ValueError("Binance open interest response is invalid")
            response_symbol = data.get("symbol")
            if response_symbol != symbol:
                raise ValueError("Binance open interest response symbol does not match request")
            return {
                "symbol": response_symbol,
                "open_interest_contracts": _required_nonnegative_decimal(
                    data.get("openInterest"), "openInterest"
                ),
                "timestamp": datetime.fromtimestamp(
                    _required_nonnegative_int(data.get("time"), "time") / 1000,
                    tz=timezone.utc,
                ),
            }

    async def get_liquidation_information(self, symbol: str) -> Dict[str, Any]:
        pos = await self.get_position(symbol)
        if pos.liquidation_price and pos.mark_price > 0:
            if pos.direction == "LONG":
                distance = (pos.mark_price - pos.liquidation_price) / pos.mark_price
            elif pos.direction == "SHORT":
                distance = (pos.liquidation_price - pos.mark_price) / pos.mark_price
            else:
                distance = None
            if distance is None or not distance.is_finite() or distance < 0:
                return {
                    "symbol": symbol,
                    "liquidation_price": pos.liquidation_price,
                    "mark_price": pos.mark_price,
                    "distance_pct": None,
                    "liquidation_safety": "UNKNOWN",
                    "is_critical": False,
                }
            dist_pct = min(Decimal("100"), distance * Decimal("100"))
            return {
                "symbol": symbol,
                "liquidation_price": pos.liquidation_price,
                "mark_price": pos.mark_price,
                "distance_pct": dist_pct,
                "liquidation_safety": "KNOWN",
                "is_critical": dist_pct < Decimal("15.0"),
            }
        return {
            "symbol": symbol,
            "liquidation_price": None,
            "mark_price": pos.mark_price,
            "distance_pct": None,
            "liquidation_safety": "UNKNOWN" if pos.direction != "FLAT" else "NOT_APPLICABLE",
            "is_critical": False,
        }

    async def get_margin_information(self) -> Dict[str, Any]:
        assert self._session is not None
        params = {"timestamp": int(time.time() * 1000), "recvWindow": self.recv_window}
        signed_qs = self._sign(params)
        url = f"{self.rest_base}/fapi/v2/account?{signed_qs}"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch futures margin: HTTP {resp.status}")
            data = await resp.json()
            if not isinstance(data, dict):
                raise ValueError("Binance futures account response is invalid")
            required = (
                "totalInitialMargin",
                "totalMaintMargin",
                "totalWalletBalance",
                "totalUnrealizedProfit",
                "totalMarginBalance",
                "availableBalance",
            )
            missing = [field for field in required if data.get(field) in (None, "")]
            if missing:
                raise ValueError(
                    "Binance futures account response is missing: " + ", ".join(missing)
                )
            return {
                "total_initial_margin": _required_decimal(
                    data.get("totalInitialMargin"), "totalInitialMargin"
                ),
                "total_maint_margin": _required_decimal(
                    data.get("totalMaintMargin"), "totalMaintMargin"
                ),
                "total_wallet_balance": _required_decimal(
                    data.get("totalWalletBalance"), "totalWalletBalance"
                ),
                "total_unrealized_profit": _required_decimal(
                    data.get("totalUnrealizedProfit"), "totalUnrealizedProfit"
                ),
                "total_margin_balance": _required_decimal(
                    data.get("totalMarginBalance"), "totalMarginBalance"
                ),
                "available_balance": _required_decimal(
                    data.get("availableBalance"), "availableBalance"
                ),
            }
