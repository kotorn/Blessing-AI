# Blessing AI v0.2

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

## Local Chrome Remote Desktop Runtime

On Windows, run `npm run dev:local` to start the Control Plane and Trading Worker as local processes and the isolated PostgreSQL 17 database through Docker Desktop. Open `http://127.0.0.1:3001` in the browser on this computer (including during a Chrome Remote Desktop session). The Worker listens on `127.0.0.1:8000`; local Postgres is published on `127.0.0.1:5433` and maps to container port `5432`, so it can coexist with another local database using `5432`.

The local launcher refuses occupied required ports and does not stop or move existing processes. A server-side supervisor starts the Worker in PAPER/DISARMED mode without Mainnet secrets. Firebase and Secret Manager are used only when their authenticated Local approval endpoints are invoked; a restart always returns to PAPER/DISARMED.

The authenticated Local endpoints are `GET /api/local/runtime`,
`POST /api/local/mainnet/candidate` (empty request body), and
`POST /api/local/mainnet/approve` (`candidateId` only). Candidate creation
fails closed until the hash-bound OOS/Shadow bundle passes the 50-basket and
per-cohort thresholds. Approval loads only the pinned Secret Manager versions,
starts LIVE/DISARMED, and runs read-only preflight; it does not ARM the Worker.
The ETHUSDC QUICK Live Research Pilot (50 USDC exposure, 2 USDC planned stop
risk, 5 USDC campaign drawdown, leverage <= 10x, 7 days) is gated by
`trading_admin` campaign approval, clean-SHA evidence and three independent
reviews. Local readiness stays `BLOCKED` until that evidence is verified, and
no order is sent before a separate ARM. See `docs/MAINNET-RELEASE-RUNBOOK.md`.
The Cloud Run release APIs and Cloud SQL release path remain a separate target.

## Development & Testing

```bash
# Install dependencies
pip install -e ".[dev]"

# Run only the non-secret unit/failure-simulation suite.
# Credentialed Testnet and mutating tests are deliberately excluded.
pytest tests/python/ -m "not contract_readonly and not contract_mutating" -v
```
