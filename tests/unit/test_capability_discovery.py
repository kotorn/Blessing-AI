"""
Unit Tests for Binance Capability Discovery Engine
Verifies LOT_SIZE, PRICE_FILTER, and MIN_NOTIONAL extraction without live network calls.
"""

import unittest
from decimal import Decimal

from venues.binance.capabilities import BinanceCapabilityDiscovery


class TestBinanceCapabilityDiscovery(unittest.TestCase):
    def test_mock_exchange_info_filter_parsing(self):
        discovery = BinanceCapabilityDiscovery()
        mock_data = {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "pricePrecision": 1,
            "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
            ],
        }

        instrument = discovery.parse_mock_exchange_info("BTCUSDT", mock_data)
        self.assertEqual(instrument.symbol, "BTCUSDT")
        self.assertEqual(instrument.tick_size, Decimal("0.10"))
        self.assertEqual(instrument.step_size, Decimal("0.001"))
        self.assertEqual(instrument.min_notional, Decimal("5.0"))
        self.assertTrue(instrument.is_trading_enabled)


if __name__ == "__main__":
    unittest.main()
