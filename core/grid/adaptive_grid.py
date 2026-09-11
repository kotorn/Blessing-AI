"""
Adaptive Grid Calculator for Blessing AI v0.1
Calculates dynamic grid spacing and controlled volume progression.
Replaces legacy static fixed-pip grids with volatility & regime-aware mathematical models.
"""

from decimal import Decimal
from typing import Dict, List, Optional
from core.basket.models import MarketRegime, Direction


class AdaptiveGridCalculator:
    """
    Core Spacing Formula:
    Grid Distance = ATR * Regime Multiplier * Level Multiplier * Stress Multiplier

    Controlled Progression (Section 2):
    L1: 1.00, L2: 1.00, L3: 1.10, L4: 1.20, L5: 1.30
    """

    # Section 11 Regime Spacing Multipliers
    REGIME_MULTIPLIERS: Dict[MarketRegime, Optional[Decimal]] = {
        MarketRegime.R0_STRONG_MEAN_REVERSION: Decimal("0.70"),
        MarketRegime.R1_RANGE: Decimal("0.90"),
        MarketRegime.R2_WEAK_TREND: Decimal("1.20"),
        MarketRegime.R3_STRONG_TREND: Decimal("1.60"),
        MarketRegime.R4_BREAKOUT: None,  # Grid Disabled
        MarketRegime.R5_VOLATILITY_SHOCK: Decimal("2.00"),
        MarketRegime.R6_CRISIS: None,   # Grid Disabled
    }

    # Geometric level spacing expansion (widens distance deeper into the grid)
    LEVEL_SPACING_MULTIPLIERS: List[Decimal] = [
        Decimal("1.00"),  # L1
        Decimal("1.10"),  # L2
        Decimal("1.30"),  # L3
        Decimal("1.60"),  # L4
        Decimal("2.00"),  # L5
    ]

    # Controlled anti-martingale volume progression (Section 2)
    VOLUME_PROGRESSION: List[Decimal] = [
        Decimal("1.00"),  # L1
        Decimal("1.00"),  # L2
        Decimal("1.10"),  # L3
        Decimal("1.20"),  # L4
        Decimal("1.30"),  # L5
    ]

    @classmethod
    def calculate_grid_distance(
        cls,
        atr: Decimal,
        regime: MarketRegime,
        level: int,  # 1 to 5
        stress_multiplier: Decimal = Decimal("1.0"),
    ) -> Optional[Decimal]:
        """
        Calculates price delta distance for a given level.
        Returns None if grid is disabled for this regime.
        """
        regime_mult = cls.REGIME_MULTIPLIERS.get(regime)
        if regime_mult is None:
            return None  # Regime disabled (e.g. Breakout or Crisis)

        idx = min(max(level - 1, 0), len(cls.LEVEL_SPACING_MULTIPLIERS) - 1)
        level_mult = cls.LEVEL_SPACING_MULTIPLIERS[idx]

        distance = atr * regime_mult * level_mult * stress_multiplier
        return distance

    @classmethod
    def calculate_level_price(
        cls,
        current_or_entry_price: Decimal,
        direction: Direction,
        distance: Decimal,
    ) -> Decimal:
        """
        Determines target limit price for the next grid step.
        For LONG: Entry - Distance
        For SHORT: Entry + Distance
        """
        if direction == Direction.LONG:
            return current_or_entry_price - distance
        else:
            return current_or_entry_price + distance

    @classmethod
    def calculate_level_quantity(
        cls,
        base_unit_size: Decimal,
        level: int,
    ) -> Decimal:
        """
        Computes controlled order size:
        base_unit_size * progression[level]
        """
        idx = min(max(level - 1, 0), len(cls.VOLUME_PROGRESSION) - 1)
        progression_mult = cls.VOLUME_PROGRESSION[idx]
        return base_unit_size * progression_mult
