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
    # Mainnet risk-increasing decisions require a signed 24h realized-PnL
    # observation.  ``None``/False means the observation is unavailable, not
    # zero.
    daily_realized_pnl: Decimal | None = None
    daily_loss_known: bool = False
    # Every balance and PnL used by a risk gate carries the asset that gave it
    # meaning.  A value with an UNKNOWN asset is never safe for Mainnet.
    collateral_asset: str = "UNKNOWN"
    risk_currency: str = "UNKNOWN"
    daily_loss_asset: str = "UNKNOWN"
    daily_pnl_includes_fees: bool = False
    daily_pnl_includes_funding: bool = False
    daily_loss_window_start: datetime | None = None
    daily_loss_window_end: datetime | None = None
    # This is the exchange-reported symbol leverage configuration, not the
    # effective exposure/equity ratio calculated below.  Mainnet requires both
    # values to be independently known and within their separate limits.
    configured_leverage: Decimal | None = None
    configured_leverage_known: bool = False
    margin_mode: str = "UNKNOWN"
    margin_mode_known: bool = False
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
    """Bounded limits shared by Testnet and explicitly-approved Mainnet.

    The legacy class name is retained for import compatibility.  The selected
    environment is now explicit, and Mainnet has a separate hard ceiling that
    cannot be widened by environment variables.
    """

    environment: str = "TESTNET"
    allowed_symbols: Set[str] = Field(default_factory=lambda: {"BTCUSDT"})
    max_single_order_notional: Decimal = Decimal("100.0")
    max_total_open_notional: Decimal = Decimal("100.0")
    max_open_orders: int = 1
    max_active_exposure_chains: int = 1
    max_collateral: Decimal = Decimal("100.0")
    max_daily_loss: Decimal = Decimal("5.0")
    max_leverage: Decimal = Decimal("2.0")

    @classmethod
    def from_environment(cls, environment: object = "TESTNET") -> "TestnetSafetyLimits":
        """Load a route-specific configuration without allowing cap widening.

        Testnet keeps its historical bounded defaults and explicit override
        acknowledgement. Mainnet is intentionally fixed to ETHUSDC and the
        pilot caps from the launch plan: collateral 100, gross 1000, order 50,
        daily loss 5, leverage 10x, and one active chain.
        """

        normalized = getattr(environment, "value", environment)
        normalized = str(normalized).strip().upper()
        if normalized not in {"TESTNET", "MAINNET"}:
            raise ValueError("Binance environment must be TESTNET or MAINNET")

        if normalized == "MAINNET":
            defaults = cls(
                environment="MAINNET",
                allowed_symbols={"ETHUSDC"},
                max_single_order_notional=Decimal("50"),
                max_total_open_notional=Decimal("1000"),
                max_open_orders=1,
                max_active_exposure_chains=1,
                max_collateral=Decimal("250"),
                max_daily_loss=Decimal("5"),
                max_leverage=Decimal("10"),
            )
            prefix = "MAINNET"
            overrides_approved = False
        else:
            defaults = cls(environment="TESTNET")
            prefix = "TESTNET"
            overrides_approved = os.getenv(
                "TESTNET_LIMITS_OVERRIDE_APPROVED", ""
            ).strip().lower() in {"1", "true", "yes", "on"}

        raw_symbols = os.getenv(f"{prefix}_ALLOWED_SYMBOLS")
        if raw_symbols is None:
            symbols = set(defaults.allowed_symbols)
        else:
            parsed_symbols = {
                symbol.strip().upper()
                for symbol in raw_symbols.split(",")
                if symbol.strip()
            }
            symbols = parsed_symbols or set(defaults.allowed_symbols)
            if not overrides_approved:
                symbols = symbols & defaults.allowed_symbols or set(defaults.allowed_symbols)

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
            f"{prefix}_MAX_SINGLE_ORDER_NOTIONAL", defaults.max_single_order_notional
        )
        total_open = positive_decimal(
            f"{prefix}_MAX_TOTAL_OPEN_NOTIONAL", defaults.max_total_open_notional
        )
        max_open_orders = positive_int(
            f"{prefix}_MAX_OPEN_ORDERS", defaults.max_open_orders
        )
        max_chains = positive_int(
            f"{prefix}_MAX_ACTIVE_EXPOSURE_CHAINS", defaults.max_active_exposure_chains
        )
        max_collateral = positive_decimal(
            f"{prefix}_MAX_COLLATERAL", defaults.max_collateral
        )
        max_daily_loss = positive_decimal(
            f"{prefix}_MAX_DAILY_LOSS", defaults.max_daily_loss
        )
        max_leverage = positive_decimal(
            f"{prefix}_MAX_LEVERAGE", defaults.max_leverage
        )
        if not overrides_approved:
            single_order = min(single_order, defaults.max_single_order_notional)
            total_open = min(total_open, defaults.max_total_open_notional)
            max_open_orders = min(max_open_orders, defaults.max_open_orders)
            max_chains = min(max_chains, defaults.max_active_exposure_chains)
            max_collateral = min(max_collateral, defaults.max_collateral)
            max_daily_loss = min(max_daily_loss, defaults.max_daily_loss)
            max_leverage = min(max_leverage, defaults.max_leverage)

        return cls(
            environment=normalized,
            allowed_symbols=symbols,
            max_single_order_notional=single_order,
            max_total_open_notional=max(total_open, single_order),
            max_open_orders=max_open_orders,
            max_active_exposure_chains=max_chains,
            max_collateral=max_collateral,
            max_daily_loss=max_daily_loss,
            max_leverage=max_leverage,
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

