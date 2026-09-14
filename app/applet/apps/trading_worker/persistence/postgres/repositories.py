import json
import logging
from decimal import Decimal
from typing import List, Optional
from datetime import datetime
from apps.trading_worker.persistence.postgres.client import PostgresClient
from domain.models import (
    ExecutionOrder, ExchangeFill, ExchangePosition, RiskSnapshot, 
    RiskState, PositionSide, OrderSide, MarketType, TimeInForce, EconomicRiskClass
)

logger = logging.getLogger(__name__)

class OrderRepository:
    def __init__(self, db: PostgresClient):
        self.db = db

    async def _ensure_instrument(self, symbol: str):
        query = """
            INSERT INTO instruments (
                symbol, market_type, base_asset, quote_asset,
                price_precision, quantity_precision, tick_size, step_size, min_notional
            ) VALUES (
                $1, 'USDM_PERP', $2, 'USDT', 2, 3, 0.01, 0.001, 5.0
            ) ON CONFLICT (symbol) DO NOTHING
        """
        # Very basic fallback insertion
        base_asset = symbol.replace("USDT", "")
        await self.db.execute(query, symbol, base_asset)

    async def save_order(self, order: ExecutionOrder):
        await self._ensure_instrument(order.symbol)
        query = """
            INSERT INTO orders (
                client_order_id, exchange_order_id, symbol, side, order_type,
                order_role, price, quantity, status, time_in_force,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11
            ) ON CONFLICT (client_order_id) DO UPDATE SET
                exchange_order_id = EXCLUDED.exchange_order_id,
                status = EXCLUDED.status,
                price = EXCLUDED.price,
                quantity = EXCLUDED.quantity,
                updated_at = EXCLUDED.updated_at
        """
        await self.db.execute(
            query,
            order.client_order_id,
            order.exchange_order_id,
            order.symbol,
            str(order.side.value if hasattr(order.side, "value") else order.side),
            order.order_type,
            "SYSTEM_ORDER", # default role
            order.price,
            order.quantity,
            order.status,
            str(order.time_in_force.value if hasattr(order.time_in_force, "value") else order.time_in_force),
            order.timestamp
        )

class FillRepository:
    def __init__(self, db: PostgresClient):
        self.db = db

    async def _ensure_instrument(self, symbol: str):
        query = """
            INSERT INTO instruments (
                symbol, market_type, base_asset, quote_asset,
                price_precision, quantity_precision, tick_size, step_size, min_notional
            ) VALUES (
                $1, 'USDM_PERP', $2, 'USDT', 2, 3, 0.01, 0.001, 5.0
            ) ON CONFLICT (symbol) DO NOTHING
        """
        base_asset = symbol.replace("USDT", "")
        await self.db.execute(query, symbol, base_asset)

    async def save_fill(self, fill: ExchangeFill):
        await self._ensure_instrument(fill.symbol)
        query = """
            INSERT INTO fills (
                fill_id, client_order_id, exchange_trade_id, symbol,
                side, price, quantity, fee, fee_asset, is_maker, executed_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
            ) ON CONFLICT (fill_id) DO NOTHING
        """
        # We use exchange_trade_id as the primary key fill_id for now
        await self.db.execute(
            query,
            fill.exchange_trade_id,
            fill.client_order_id,
            fill.exchange_trade_id,
            fill.symbol,
            str(fill.side.value if hasattr(fill.side, "value") else fill.side),
            fill.price,
            fill.quantity,
            fill.commission,
            fill.commission_asset,
            fill.maker,
            fill.event_time or fill.transaction_time or datetime.utcnow()
        )

class PositionRepository:
    def __init__(self, db: PostgresClient):
        self.db = db

    async def _ensure_instrument(self, symbol: str):
        query = """
            INSERT INTO instruments (
                symbol, market_type, base_asset, quote_asset,
                price_precision, quantity_precision, tick_size, step_size, min_notional
            ) VALUES (
                $1, 'USDM_PERP', $2, 'USDT', 2, 3, 0.01, 0.001, 5.0
            ) ON CONFLICT (symbol) DO NOTHING
        """
        base_asset = symbol.replace("USDT", "")
        await self.db.execute(query, symbol, base_asset)

    async def save_position(self, pos: ExchangePosition):
        await self._ensure_instrument(pos.symbol)
        query = """
            INSERT INTO positions (
                venue, symbol, direction, quantity, entry_price, mark_price,
                liquidation_price, unrealized_pnl, leverage, margin_type,
                initial_margin, maintenance_margin, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0.0, 0.0, $11
            ) ON CONFLICT (venue, symbol) DO UPDATE SET
                direction = EXCLUDED.direction,
                quantity = EXCLUDED.quantity,
                entry_price = EXCLUDED.entry_price,
                mark_price = EXCLUDED.mark_price,
                liquidation_price = EXCLUDED.liquidation_price,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                leverage = EXCLUDED.leverage,
                margin_type = EXCLUDED.margin_type,
                updated_at = EXCLUDED.updated_at
        """
        direction = str(pos.position_side.value if hasattr(pos.position_side, "value") else pos.position_side)
        if direction == "BOTH" or direction == "FLAT":
            direction_str = "FLAT" if pos.quantity == Decimal("0.0") else "LONG" # simplifying assumptions
        else:
            direction_str = direction

        await self.db.execute(
            query,
            "binance_global",
            pos.symbol,
            direction_str,
            pos.quantity,
            pos.entry_price,
            pos.mark_price or Decimal("0.0"),
            pos.liquidation_price,
            pos.unrealized_pnl,
            pos.leverage,
            pos.margin_type,
            pos.event_time or datetime.utcnow()
        )

class RiskSnapshotRepository:
    def __init__(self, db: PostgresClient):
        self.db = db

    async def save_snapshot(self, snapshot: RiskSnapshot):
        query = """
            INSERT INTO portfolio_snapshots (
                timestamp, total_equity, total_balance, free_margin, used_margin,
                margin_utilization_pct, effective_leverage, current_drawdown_pct,
                risk_state, aggregate_long_exposure, aggregate_short_exposure,
                crypto_beta_exposure_pct, active_baskets_count, kill_switch_active,
                reasons
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
            )
        """
        reasons_json = json.dumps({
            "hard_violations": snapshot.hard_violations,
            "soft_violations": snapshot.soft_violations
        })
        
        await self.db.execute(
            query,
            snapshot.timestamp,
            snapshot.portfolio_equity,
            snapshot.portfolio_equity, # simplify balance mapping
            snapshot.portfolio_equity * (Decimal("1.0") - (snapshot.margin_utilization_pct / 100)), # simplify
            snapshot.portfolio_equity * (snapshot.margin_utilization_pct / 100),
            snapshot.margin_utilization_pct,
            snapshot.effective_leverage,
            snapshot.current_drawdown_pct,
            str(snapshot.risk_state.value if hasattr(snapshot.risk_state, "value") else snapshot.risk_state),
            Decimal("0.0"), # Not directly on model
            Decimal("0.0"),
            Decimal("0.0"),
            0,
            "KILL_SWITCH" in [v.upper() for v in snapshot.hard_violations], # approximate
            reasons_json
        )
