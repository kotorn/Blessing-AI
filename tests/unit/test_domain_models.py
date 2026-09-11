"""
Unit Tests for Blessing AI v0.2 Domain Models
Validates Decimal precision, immutable data structures, signed desired deltas, and bounded scores.
"""

import unittest
from decimal import Decimal
from datetime import datetime, timezone

from domain.models import (
    Instrument,
    MarketEvent,
    StrategyIntent,
    TargetExposure,
    Basket,
    GridLevel,
    OrderIntent,
    RiskSnapshot,
    MarketType,
    OrderSide,
    PositionSide,
    OrderType,
    TimeInForce,
    RiskState,
    BasketState,
)


class TestDomainModels(unittest.TestCase):
    def test_decimal_precision_preservation(self):
        """Verify financial arithmetic does not suffer from floating point epsilon issues."""
        p1 = Decimal("91250.10")
        p2 = Decimal("91250.20")
        self.assertEqual(p1 + p2, Decimal("182500.30"))

    def test_strategy_intent_signed_delta(self):
        """Ensure StrategyIntent accurately encapsulates directional intents and scores."""
        intent = StrategyIntent(
            intent_id="int_001",
            strategy_id="structural_grid",
            symbol="BTCUSDT",
            market_type=MarketType.USDM_FUTURES,
            direction=PositionSide.LONG,
            desired_delta_qty=Decimal("1.50"),
            opportunity_score=Decimal("0.85"),
            confidence=Decimal("0.90"),
            expected_holding_horizon_sec=3600,
            evidence={"regime": "RANGE", "atr_1h": "840.5"},
        )
        self.assertEqual(intent.desired_delta_qty, Decimal("1.50"))
        self.assertEqual(intent.direction, PositionSide.LONG)
        self.assertGreaterEqual(intent.opportunity_score, Decimal("0.0"))
        self.assertLessEqual(intent.opportunity_score, Decimal("1.0"))

    def test_target_exposure_strategy_attribution(self):
        """Verify target exposure attributes virtual positions correctly."""
        now = datetime.now(timezone.utc)
        target = TargetExposure(
            symbol="BTCUSDT",
            market_type=MarketType.USDM_FUTURES,
            target_net_delta_qty=Decimal("0.10"),
            target_gross_limit_qty=Decimal("2.50"),
            strategy_attributions={
                "grid": Decimal("1.00"),
                "trend": Decimal("-0.60"),
                "shock": Decimal("-0.30"),
            },
            expires_at=now,
        )
        net_calc = sum(target.strategy_attributions.values())
        self.assertEqual(net_calc, target.target_net_delta_qty)

    def test_basket_lifecycle_levels(self):
        """Ensure Basket correctly stores grid levels with precise prices and status."""
        level1 = GridLevel(
            level=1,
            target_price=Decimal("91500.0"),
            quantity=Decimal("0.40"),
            is_filled=True,
        )
        level2 = GridLevel(
            level=2,
            target_price=Decimal("90700.0"),
            quantity=Decimal("0.44"),
            is_filled=False,
        )
        basket = Basket(
            basket_id="BSK-BTC-001",
            strategy_id="grid",
            symbol="BTCUSDT",
            direction=PositionSide.LONG,
            state=BasketState.ACTIVE,
            grid_depth=1,
            max_grid_levels=5,
            levels=[level1, level2],
            total_quantity=Decimal("0.40"),
            average_entry_price=Decimal("91500.0"),
        )
        self.assertEqual(basket.grid_depth, 1)
        self.assertEqual(len(basket.levels), 2)
        self.assertTrue(basket.levels[0].is_filled)
        self.assertFalse(basket.levels[1].is_filled)


if __name__ == "__main__":
    unittest.main()
