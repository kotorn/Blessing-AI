import os
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Set
from datetime import datetime
from pydantic import BaseModel, Field

from domain.models import utc_now

class ExchangeAccountSnapshot(BaseModel):
    wallet_balance: Decimal
    margin_balance: Decimal
    available_balance: Decimal

    unrealized_pnl: Decimal

    total_initial_margin: Decimal
    total_maint_margin: Decimal
    position_initial_margin: Decimal

    total_position_notional: Decimal

    effective_leverage: Decimal
    margin_utilization_pct: Decimal

    # None means not applicable for a flat account or unavailable for an active
    # position. It is deliberately not represented as an invented 100 percent.
    min_liquidation_distance_pct: Decimal | None = None
    liquidation_safety: str = "UNKNOWN"
    exchange_environment: str = "UNKNOWN"
    valid: bool = False
    invalid_reason: str | None = None

    timestamp: datetime = Field(default_factory=utc_now)

class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    STREAM_STARTING = "STREAM_STARTING"
    SYNCING = "SYNCING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    RECONCILING = "RECONCILING"

class TestnetSafetyLimits(BaseModel):
    allowed_symbols: Set[str] = Field(default_factory=lambda: {"BTCUSDT"})
    max_single_order_notional: Decimal = Decimal("100.0")
    max_total_open_notional: Decimal = Decimal("100.0")
    max_open_orders: int = 1
    max_active_exposure_chains: int = 1

    @classmethod
    def from_environment(cls) -> "TestnetSafetyLimits":
        """Load bounded overrides without allowing malformed values to disable caps.

        First-launch limits are a hard safety default.  Expanding them requires
        an explicit operator acknowledgement; a typo or an inherited large
        deployment value must never silently widen Testnet exposure.
        """

        defaults = cls()

        raw_symbols = os.getenv("TESTNET_ALLOWED_SYMBOLS")
        overrides_approved = os.getenv(
            "TESTNET_LIMITS_OVERRIDE_APPROVED", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if raw_symbols is None:
            symbols = defaults.allowed_symbols
        else:
            parsed_symbols = {
                symbol.strip().upper()
                for symbol in raw_symbols.split(",")
                if symbol.strip()
            }
            symbols = parsed_symbols or defaults.allowed_symbols
            if not overrides_approved:
                symbols = symbols & defaults.allowed_symbols or defaults.allowed_symbols

        def positive_decimal(name: str, fallback: Decimal) -> Decimal:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return fallback
            try:
                value = Decimal(raw)
            except (InvalidOperation, ValueError):
                return fallback
            return value if value.is_finite() and value > 0 else fallback

        def positive_int(name: str, fallback: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return fallback
            try:
                value = int(raw)
            except ValueError:
                return fallback
            return value if value > 0 else fallback

        single_order = positive_decimal(
            "TESTNET_MAX_SINGLE_ORDER_NOTIONAL", defaults.max_single_order_notional
        )
        total_open = positive_decimal(
            "TESTNET_MAX_TOTAL_OPEN_NOTIONAL", defaults.max_total_open_notional
        )
        max_open_orders = positive_int(
            "TESTNET_MAX_OPEN_ORDERS", defaults.max_open_orders
        )
        max_chains = positive_int(
            "TESTNET_MAX_ACTIVE_EXPOSURE_CHAINS", defaults.max_active_exposure_chains
        )
        if not overrides_approved:
            single_order = min(single_order, defaults.max_single_order_notional)
            total_open = min(total_open, defaults.max_total_open_notional)
            max_open_orders = min(max_open_orders, defaults.max_open_orders)
            max_chains = min(max_chains, defaults.max_active_exposure_chains)

        return cls(
            allowed_symbols=symbols,
            max_single_order_notional=single_order,
            max_total_open_notional=total_open,
            max_open_orders=max_open_orders,
            max_active_exposure_chains=max_chains,
        )

class BinanceExecutionError(Exception):
    """Base exception for Binance execution errors."""
    pass

class BinanceDefinitiveRejection(BinanceExecutionError):
    """Order definitively rejected by Binance matching engine (e.g. -2010, -1013, -2011)."""
    def __init__(self, code: int, msg: str):
        super().__init__(f"Binance definitive rejection ({code}): {msg}")
        self.code = code
        self.msg = msg

class BinanceTransportAmbiguity(BinanceExecutionError):
    """Transport ambiguity / timeout: status of order unknown on exchange."""
    pass

class BinanceAuthenticationError(BinanceExecutionError):
    """Authentication or signature failure (-2014, -2015)."""
    def __init__(self, code: int, msg: str):
        super().__init__(f"Binance auth error ({code}): {msg}")
        self.code = code
        self.msg = msg

class BinanceRateLimitError(BinanceExecutionError):
    """Rate limit breach / IP ban (-1003, -1015, HTTP 429)."""
    def __init__(self, code: int, msg: str, headers: dict[str, str] | None = None):
        super().__init__(f"Binance rate limit ({code}): {msg}")
        self.code = code
        self.msg = msg
        self.headers = headers or {}

class BinanceTimestampError(BinanceExecutionError):
    """Timestamp outside recvWindow (-1021)."""
    def __init__(self, code: int, msg: str):
        super().__init__(f"Binance timestamp error ({code}): {msg}")
        self.code = code
        self.msg = msg

