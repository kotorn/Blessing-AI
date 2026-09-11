"""
Binance USDⓈ-M Futures Client & Order Translation Guard
Enforces Hedge Mode requirements, strict step_size rounding, and client_order_id formatting.
"""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Dict, Any, Optional
import hmac
import hashlib
import time

from domain.models import OrderIntent, OrderSide, PositionSide, OrderType, TimeInForce


class BinanceFuturesClient:
    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret

    def sign_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params["timestamp"] = int(time.time() * 1000)
        query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    @staticmethod
    def format_order_payload(
        intent: OrderIntent,
        tick_size: Decimal,
        step_size: Decimal,
    ) -> Dict[str, Any]:
        """
        Translates Domain OrderIntent into Binance Futures REST payload.
        Enforces Hedge Mode positionSide ("LONG" | "SHORT") and decimal quantize rules.
        """
        # Step size truncation (strictly round down to avoid LOT_SIZE breach)
        qty_str = f"{intent.quantity.quantize(step_size, rounding=ROUND_DOWN):f}"

        # PositionSide verification for Hedge Mode
        pos_side = intent.position_side.value
        if pos_side not in ("LONG", "SHORT"):
            raise ValueError(
                f"Invalid positionSide '{pos_side}' for Binance Hedge Mode. Must be 'LONG' or 'SHORT'."
            )

        payload: Dict[str, Any] = {
            "symbol": intent.symbol,
            "side": intent.side.value,
            "positionSide": pos_side,
            "type": intent.order_type.value,
            "quantity": qty_str,
            "newClientOrderId": intent.client_order_id,
        }

        if intent.reduce_only:
            payload["reduceOnly"] = "true"

        if intent.order_type == OrderType.LIMIT:
            if intent.price is None:
                raise ValueError("Price is required for LIMIT order")
            price_str = f"{intent.price.quantize(tick_size, rounding=ROUND_HALF_UP):f}"
            payload["price"] = price_str
            # Post-only mapping
            if intent.time_in_force == TimeInForce.POST_ONLY:
                payload["timeInForce"] = "GTX"
            else:
                payload["timeInForce"] = intent.time_in_force.value

        return payload
