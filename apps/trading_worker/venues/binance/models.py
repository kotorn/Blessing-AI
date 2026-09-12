from enum import Enum
from decimal import Decimal
from typing import Set
from datetime import datetime
from pydantic import BaseModel, Field

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

    timestamp: datetime = Field(default_factory=datetime.utcnow)

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
    allowed_symbols: Set[str] = Field(default_factory=lambda: {"BTCUSDT", "ETHUSDT"})
    max_single_order_notional: Decimal = Decimal("500.0")
    max_total_open_notional: Decimal = Decimal("2000.0")
    max_open_orders: int = 5

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
    def __init__(self, code: int, msg: str):
        super().__init__(f"Binance rate limit ({code}): {msg}")
        self.code = code
        self.msg = msg

class BinanceTimestampError(BinanceExecutionError):
    """Timestamp outside recvWindow (-1021)."""
    def __init__(self, code: int, msg: str):
        super().__init__(f"Binance timestamp error ({code}): {msg}")
        self.code = code
        self.msg = msg

