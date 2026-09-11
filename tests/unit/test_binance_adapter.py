"""
Unit Tests for Binance USDⓈ-M Futures Order Translation Guard
Verifies strict Hedge Mode positionSide formatting, step_size truncation, and Post-Only GTX mapping.
"""

import unittest
from decimal import Decimal

from domain.models import (
    OrderIntent,
    MarketType,
    OrderSide,
    PositionSide,
    OrderType,
    TimeInForce,
)
from venues.binance.client import BinanceFuturesClient


class TestBinanceOrderTranslation(unittest.TestCase):
    def test_hedge_mode_long_order_formatting(self):
        """Verify LIMIT LONG order generates correct Binance Hedge Mode payload."""
        intent = OrderIntent(
            client_order_id="BLS_BTC_L1_001",
            symbol="BTCUSDT",
            market_type=MarketType.USDM_FUTURES,
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.POST_ONLY,
            quantity=Decimal("0.45678"),  # Needs stepSize truncation
            price=Decimal("91234.567"),    # Needs tickSize rounding
        )

        payload = BinanceFuturesClient.format_order_payload(
            intent=intent,
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
        )

        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertEqual(payload["side"], "BUY")
        self.assertEqual(payload["positionSide"], "LONG")
        self.assertEqual(payload["type"], "LIMIT")
        self.assertEqual(payload["quantity"], "0.456")   # Round down
        self.assertEqual(payload["price"], "91234.6")    # Round half up
        self.assertEqual(payload["timeInForce"], "GTX")  # POST_ONLY -> GTX
        self.assertEqual(payload["newClientOrderId"], "BLS_BTC_L1_001")

    def test_invalid_position_side_raises_error(self):
        """Hedge Mode strictly prohibits positionSide='BOTH'."""
        intent = OrderIntent(
            client_order_id="BLS_BTC_ERR",
            symbol="BTCUSDT",
            market_type=MarketType.USDM_FUTURES,
            side=OrderSide.BUY,
            position_side=PositionSide.BOTH,  # Invalid in Hedge Mode
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
            quantity=Decimal("1.0"),
        )

        with self.assertRaises(ValueError):
            BinanceFuturesClient.format_order_payload(
                intent=intent,
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
            )


if __name__ == "__main__":
    unittest.main()
