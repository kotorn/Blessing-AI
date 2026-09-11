"""
NATS JetStream Event Schemas and Subject Hierarchies for Blessing AI v0.2
"""

from typing import Dict, Any, Optional
from datetime import datetime, timezone
import json
from decimal import Decimal

from domain.models import utc_now


class EventSubjectBuilder:
    """Standardized subject topic builder for NATS JetStream."""

    @staticmethod
    def market_trades(venue: str, market: str, symbol: str) -> str:
        return f"market.{venue}.{market}.{symbol}.trade"

    @staticmethod
    def market_book(venue: str, market: str, symbol: str) -> str:
        return f"market.{venue}.{market}.{symbol}.book"

    @staticmethod
    def market_mark(venue: str, market: str, symbol: str) -> str:
        return f"market.{venue}.{market}.{symbol}.mark"

    @staticmethod
    def market_funding(venue: str, market: str, symbol: str) -> str:
        return f"market.{venue}.{market}.{symbol}.funding"

    @staticmethod
    def market_state(symbol: str) -> str:
        return f"state.{symbol}.market"

    @staticmethod
    def strategy_intent(symbol: str, strategy: str) -> str:
        return f"strategy.{symbol}.{strategy}.intent"

    @staticmethod
    def portfolio_target(symbol: str) -> str:
        return f"portfolio.target.{symbol}"

    @staticmethod
    def risk_portfolio() -> str:
        return "risk.portfolio.snapshot"

    @staticmethod
    def recovery_intent(basket_id: str) -> str:
        return f"recovery.intent.{basket_id}"

    @staticmethod
    def execution_order(symbol: str) -> str:
        return f"execution.orders.{symbol}"

    @staticmethod
    def account_position(symbol: str) -> str:
        return f"account.position.{symbol}"


class DomainEvent:
    """Base envelope for all NATS messages across services."""

    def __init__(
        self,
        event_type: str,
        payload: Dict[str, Any],
        source: str,
        symbol: Optional[str] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        event_time: Optional[datetime] = None,
    ):
        self.event_id = f"evt_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        self.event_type = event_type
        self.schema_version = "0.2.0"
        self.event_time = event_time or utc_now()
        self.receive_time = utc_now()
        self.source = source
        self.symbol = symbol
        self.correlation_id = correlation_id or self.event_id
        self.causation_id = causation_id
        self.payload = payload

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "event_time": self.event_time.isoformat(),
            "receive_time": self.receive_time.isoformat(),
            "source": self.source,
            "symbol": self.symbol,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload": self.payload,
        }

    def serialize(self) -> bytes:
        def default_encoder(obj):
            if isinstance(obj, Decimal):
                return str(obj)
            if isinstance(obj, datetime):
                return obj.isoformat()
            if hasattr(obj, "dict"):
                return obj.dict()
            if hasattr(obj, "model_dump"):
                return obj.model_dump()
            return str(obj)

        return json.dumps(self.to_dict(), default=default_encoder).encode("utf-8")
