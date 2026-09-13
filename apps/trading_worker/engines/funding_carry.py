import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from domain.models import (
    MarketEvent,
    MarketState,
    MarketType,
    PositionSide,
    StrategyIntent,
)
from .identifiers import event_time_token

logger = logging.getLogger("blessing.engines.funding_carry")


@dataclass(frozen=True)
class FundingCarryCostInputs:
    """Explicit execution and holding-cost inputs for carry research.

    Rates are decimal fractions (``0.0005`` is 5 bps). Spread and slippage
    values are one-sided/full-quote bps in the same convention as the
    research cost model.  There are intentionally no implicit fee or funding
    assumptions: without this object the carry engine emits no intent.
    """

    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    entry_spread_bps: Decimal
    exit_spread_bps: Decimal
    entry_slippage_bps: Decimal
    exit_slippage_bps: Decimal
    annual_financing_rate: Decimal
    funding_intervals_per_day: int
    holding_horizon_sec: int

    def __post_init__(self) -> None:
        decimal_fields = (
            "maker_fee_rate",
            "taker_fee_rate",
            "entry_spread_bps",
            "exit_spread_bps",
            "entry_slippage_bps",
            "exit_slippage_bps",
            "annual_financing_rate",
        )
        for field_name in decimal_fields:
            try:
                value = Decimal(str(getattr(self, field_name)))
            except (TypeError, ValueError, ArithmeticError):
                raise ValueError(f"{field_name} must be Decimal-compatible") from None
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")
            if field_name.endswith("fee_rate") and value > 1:
                raise ValueError(f"{field_name} must not exceed 1")
            object.__setattr__(self, field_name, value)

        if (
            isinstance(self.funding_intervals_per_day, bool)
            or not isinstance(self.funding_intervals_per_day, int)
            or self.funding_intervals_per_day <= 0
        ):
            raise ValueError("funding_intervals_per_day must be a positive integer")
        if (
            isinstance(self.holding_horizon_sec, bool)
            or not isinstance(self.holding_horizon_sec, int)
            or self.holding_horizon_sec <= 0
        ):
            raise ValueError("holding_horizon_sec must be a positive integer")

    @classmethod
    def from_environment(cls) -> Optional["FundingCarryCostInputs"]:
        """Load a complete explicit carry model, or disable carry safely.

        A partial configuration is invalid and is treated as unavailable.  A
        deployment can therefore opt into carry only after providing every
        economic input needed to compute net expected return.
        """

        names = {
            "maker_fee_rate": "CARRY_MAKER_FEE_RATE",
            "taker_fee_rate": "CARRY_TAKER_FEE_RATE",
            "entry_spread_bps": "CARRY_ENTRY_SPREAD_BPS",
            "exit_spread_bps": "CARRY_EXIT_SPREAD_BPS",
            "entry_slippage_bps": "CARRY_ENTRY_SLIPPAGE_BPS",
            "exit_slippage_bps": "CARRY_EXIT_SLIPPAGE_BPS",
            "annual_financing_rate": "CARRY_ANNUAL_FINANCING_RATE",
            "funding_intervals_per_day": "CARRY_FUNDING_INTERVALS_PER_DAY",
            "holding_horizon_sec": "CARRY_HOLDING_HORIZON_SEC",
        }
        values = {field: os.getenv(env_name, "").strip() for field, env_name in names.items()}
        if not any(values.values()):
            return None
        if any(not value for value in values.values()):
            logger.error("Carry disabled: all CARRY_* economic inputs must be configured")
            return None
        try:
            return cls(
                maker_fee_rate=Decimal(values["maker_fee_rate"]),
                taker_fee_rate=Decimal(values["taker_fee_rate"]),
                entry_spread_bps=Decimal(values["entry_spread_bps"]),
                exit_spread_bps=Decimal(values["exit_spread_bps"]),
                entry_slippage_bps=Decimal(values["entry_slippage_bps"]),
                exit_slippage_bps=Decimal(values["exit_slippage_bps"]),
                annual_financing_rate=Decimal(values["annual_financing_rate"]),
                funding_intervals_per_day=int(values["funding_intervals_per_day"]),
                holding_horizon_sec=int(values["holding_horizon_sec"]),
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            logger.error("Carry disabled: invalid CARRY_* economic inputs: %s", exc)
            return None


class FundingCarryEngine:
    def __init__(
        self,
        strategy_id: str = "funding_carry",
        min_annualized_yield: Decimal = Decimal("10.0"),
        cost_inputs: Optional[FundingCarryCostInputs] = None,
    ):
        self.strategy_id = strategy_id
        self.min_annualized_yield = Decimal(str(min_annualized_yield))
        if not self.min_annualized_yield.is_finite() or self.min_annualized_yield <= 0:
            raise ValueError("min_annualized_yield must be finite and positive")
        self.cost_inputs = cost_inputs
        
    def evaluate(self, event: MarketEvent, market_state: MarketState) -> Optional[StrategyIntent]:
        # Funding is an economic input, not a value that can be safely inferred.
        # Without a current exchange funding event there is no carry edge to
        # evaluate, so fail closed and emit no intent.
        funding_rate = event.funding_rate
        if funding_rate is None or not funding_rate.is_finite():
            logger.info("Skipping carry intent for %s: funding rate is unavailable.", event.symbol)
            return None

        cost_inputs = self.cost_inputs
        if cost_inputs is None:
            logger.info(
                "Skipping carry intent for %s: explicit carry cost inputs are unavailable.",
                event.symbol,
            )
            return None

        horizon_days = Decimal(cost_inputs.holding_horizon_sec) / Decimal("86400")
        gross_funding_pct = (
            abs(funding_rate)
            * Decimal(cost_inputs.funding_intervals_per_day)
            * horizon_days
            * Decimal("100")
        )
        fee_cost_pct = (cost_inputs.maker_fee_rate + cost_inputs.taker_fee_rate) * Decimal("100")
        spread_cost_pct = (
            cost_inputs.entry_spread_bps + cost_inputs.exit_spread_bps
        ) / Decimal("200")
        slippage_cost_pct = (
            cost_inputs.entry_slippage_bps + cost_inputs.exit_slippage_bps
        ) / Decimal("100")
        financing_cost_pct = (
            cost_inputs.annual_financing_rate * horizon_days / Decimal("365") * Decimal("100")
        )
        net_horizon_pct = (
            gross_funding_pct
            - fee_cost_pct
            - spread_cost_pct
            - slippage_cost_pct
            - financing_cost_pct
        )
        net_annualized_pct = net_horizon_pct / horizon_days * Decimal("365")
        
        if net_annualized_pct < self.min_annualized_yield:
            return None  # Insufficient net yield to justify carry after costs
            
        direction = PositionSide.SHORT if funding_rate > 0 else PositionSide.LONG
        opportunity_score = min(
            Decimal("1"),
            max(Decimal("0"), net_annualized_pct / (self.min_annualized_yield * Decimal("2"))),
        )
        
        return StrategyIntent(
            intent_id=f"CARRY-INTENT-{event.symbol}-{event_time_token(event.event_time)}",
            strategy_id=self.strategy_id,
            symbol=event.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=Decimal("-0.1") if direction == PositionSide.SHORT else Decimal("0.1"),
            opportunity_score=opportunity_score,
            confidence=Decimal("0.85"),
            expected_holding_horizon_sec=cost_inputs.holding_horizon_sec,
            evidence={
                "gross_funding_pct": str(gross_funding_pct),
                "fee_cost_pct": str(fee_cost_pct),
                "spread_cost_pct": str(spread_cost_pct),
                "slippage_cost_pct": str(slippage_cost_pct),
                "financing_cost_pct": str(financing_cost_pct),
                "net_horizon_pct": str(net_horizon_pct),
                "net_annualized_pct": str(net_annualized_pct),
                "raw_funding": str(funding_rate)
            },
            timestamp=event.event_time,
        )
