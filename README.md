# Blessing AI v0.1

**Adaptive Basket Grid Trading System, Market Regime Engine, AI Grid Safety Score, and Portfolio Risk Governor for Binance Spot & USDⓈ-M Futures**

Blessing AI transforms the legacy MT4 Blessing EA basket-recovery philosophy into an institutionally robust, event-driven quantitative trading architecture. It replaces aggressive Martingale doubling with controlled anti-martingale progression, dynamic ATR volatility spacing, probabilistic regime classification, machine learning safety scoring, and a deterministic fail-closed Risk Governor.

---

## Key Pillars

1. **Controlled Position Progression**: Anti-martingale volume scaling (`L1: 1.0, L2: 1.0, L3: 1.1, L4: 1.2, L5: 1.3`), max 5 levels.
2. **Adaptive Grid Spacing**: `Grid Distance = ATR × Regime Multiplier × Level Multiplier × Stress Multiplier`.
3. **Market Regime Engine**: 7-state probabilistic classifier (R0 Mean Reversion to R6 Crisis) that disables grids during breakouts and extreme shocks.
4. **Research-only AI Grid Safety Score**: Offline/research scaffold for evaluating expected basket profitability, MAE, drawdown, and recovery duration; it has no live execution authority.
5. **Funding & Basis Intelligence**: Evaluates funding rate drag and spot-perp basis divergence as first-class risk metrics.
6. **Portfolio Risk Governor**: Independent, non-overridable capital preservation engine monitoring margin utilization, leverage (≤ 2.0x), drawdown escalation, and cross-instrument crypto beta correlation.
7. **Strict Fail-Closed Architecture**: Any market data staleness (>3s), WebSocket disconnection, or database failure halts all new entries immediately.

Paper simulations and the current UI replay fixtures are not profitability evidence. A
Testnet or Small Live decision requires separately verified data-backed backtests,
walk-forward/OOS results, and execution evidence after fees, funding, slippage, and
execution costs.

---

## Quickstart (Docker Compose)

```bash
# 1. Clone repository and setup environment
cp .env.example .env

# 2. Start NATS, PostgreSQL, Redis, and Traders
docker compose up -d

# 3. Access Interactive Control Cockpit
# Open http://localhost:3000 in your browser
```

---

## Development & Testing

```bash
# Install dependencies
pip install -e ".[dev]"

# Run unit and failure simulation tests
pytest tests/ -v --cov=core
```
