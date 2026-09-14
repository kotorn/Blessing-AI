"""Canonical production strategy-intent allocator."""

from __future__ import annotations

import logging
from datetime import UTC, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, List

from domain.models import MarketType, PositionSide, StrategyIntent, TargetExposure, utc_now

logger = logging.getLogger("blessing.engines.meta_allocator")


class MetaAllocator:
    """Convert one symbol's strategy intents into one TargetExposure."""

    def __init__(
        self,
        max_portfolio_gross: Decimal = Decimal("5.0"),
        *,
        max_gross_exposure_btc: Decimal | None = None,
    ) -> None:
        configured_limit = (
            max_portfolio_gross
            if max_gross_exposure_btc is None
            else max_gross_exposure_btc
        )
        try:
            parsed_limit = Decimal(str(configured_limit))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("max portfolio gross exposure must be Decimal-compatible") from exc
        if not parsed_limit.is_finite() or parsed_limit <= 0:
            raise ValueError("max portfolio gross exposure must be positive and finite")
        # Keep the pre-recovery constructor attribute available while making
        # the canonical allocator contract explicit.
        self.max_portfolio_gross = parsed_limit
        self.max_gross_exposure_btc = parsed_limit

    def allocate(self, intents: List[StrategyIntent], symbol: str) -> TargetExposure:
        """Aggregate and validate intents for exactly one symbol."""

        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise ValueError("allocation symbol must not be empty")
        if not intents:
            return TargetExposure(
                symbol=normalized_symbol,
                market_type=MarketType.USDM_FUTURES,
                target_net_delta_qty=Decimal("0.0"),
                target_gross_limit_qty=Decimal("0.0"),
                strategy_attributions={},
                expires_at=utc_now() + timedelta(seconds=60),
            )

        net_delta = Decimal("0.0")
        gross_exposure = Decimal("0.0")
        attributions: Dict[str, Decimal] = {}
        source_intent_ids: List[str] = []
        seen_intent_ids: set[str] = set()
        market_type = intents[0].market_type

        for intent in intents:
            intent_symbol = str(intent.symbol).strip().upper()
            if intent_symbol != normalized_symbol:
                raise ValueError(
                    f"strategy intent symbol {intent_symbol!r} does not match allocation symbol "
                    f"{normalized_symbol!r}"
                )
            if intent.market_type != market_type:
                raise ValueError("strategy intents must use one market type")
            if not intent.intent_id or intent.intent_id in seen_intent_ids:
                raise ValueError("strategy intent IDs must be present and unique")
            seen_intent_ids.add(intent.intent_id)

            try:
                desired_delta = Decimal(str(intent.desired_delta_qty))
                weight = Decimal(str(intent.confidence)) * Decimal(
                    str(intent.opportunity_score)
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("strategy intent quantities and scores must be numeric") from exc
            if not desired_delta.is_finite() or not weight.is_finite():
                raise ValueError("strategy intent quantities and scores must be finite")
            direction = (
                intent.direction
                if isinstance(intent.direction, PositionSide)
                else PositionSide(str(intent.direction).upper())
            )
            if (
                direction == PositionSide.LONG
                and desired_delta < 0
            ) or (
                direction == PositionSide.SHORT
                and desired_delta > 0
            ) or (
                direction == PositionSide.BOTH
                and desired_delta != 0
            ):
                raise ValueError("strategy intent direction does not match its signed delta")

            effective_delta = desired_delta * weight
            if not effective_delta.is_finite():
                raise ValueError("weighted strategy intent delta must be finite")

            source_intent_ids.append(intent.intent_id)
            attributions[intent.strategy_id] = (
                attributions.get(intent.strategy_id, Decimal("0.0")) + effective_delta
            )
            net_delta += effective_delta
            gross_exposure += abs(effective_delta)

            logger.debug(
                "[%s] %s intent: %s * %s = %s",
                normalized_symbol,
                intent.strategy_id,
                desired_delta,
                weight,
                effective_delta,
            )

        if gross_exposure > self.max_gross_exposure_btc:
            scale_factor = self.max_gross_exposure_btc / gross_exposure
            net_delta *= scale_factor
            gross_exposure = self.max_gross_exposure_btc
            for strategy_id in attributions:
                attributions[strategy_id] *= scale_factor

        intent_timestamps = [intent.timestamp for intent in intents]
        if any(timestamp.tzinfo is None for timestamp in intent_timestamps):
            raise ValueError("strategy intent timestamps must be timezone-aware")
        as_of = max(timestamp.astimezone(UTC) for timestamp in intent_timestamps)

        return TargetExposure(
            symbol=normalized_symbol,
            market_type=market_type,
            target_net_delta_qty=net_delta,
            target_gross_limit_qty=gross_exposure,
            strategy_attributions=attributions,
            created_at=as_of,
            expires_at=as_of + timedelta(seconds=60),
            source_intent_ids=source_intent_ids,
        )
