# Blessing AI v0.1

[![Blessing AI CI](https://github.com/kotorn/Blessing-AI/actions/workflows/ci.yml/badge.svg)](https://github.com/kotorn/Blessing-AI/actions/workflows/ci.yml)

**Adaptive Basket Grid Trading System, Market Regime Engine, AI Grid Safety Score, and Portfolio Risk Governor for Binance Spot & USDⓈ-M Futures**

Blessing AI transforms the legacy MT4 Blessing EA basket-recovery philosophy into an institutionally robust, event-driven quantitative trading architecture. It replaces aggressive Martingale doubling with bounded deterministic grid guardrails, dynamic ATR volatility spacing, probabilistic regime classification, research-only safety scoring, and a deterministic fail-closed Risk Governor. Research or AI diagnostics never have live order authority.

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

# 2. Start NATS, PostgreSQL, Redis, the market-data collector, and the trading worker
docker compose up -d

# 3. Access the services
# Worker control API:  http://localhost:8000  (health/state endpoints)
# Control Plane cockpit: http://localhost:3000 (requires Firebase credentials
# and ADC for full functionality; for active development prefer `npm run dev`)
```

---

## Development & Testing

```bash
# Install dependencies
pip install -e ".[dev]"

# Run only the non-secret unit/failure-simulation suite.
# Credentialed Testnet and mutating tests are deliberately excluded.
pytest tests/python/ -m "not contract_readonly and not contract_mutating" -v
```
