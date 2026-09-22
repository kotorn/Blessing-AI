# Blessing AI — Mainnet Wealth Growth & Closed-Loop Learning Engine

## 1. Executive Summary & North Star

The **Blessing AI Wealth Growth Engine** operationalizes autonomous trading on Binance USDⓈ-M Futures with a singular North Star:
> **Sustainable Risk-Adjusted Wealth Growth** under predefined risk bounds, with capital survival as the absolute first priority.

The system decouples **System Control (Safety Governors & Exchange Gates)** from **Strategy Intelligence (AI, Scorer, Learning Engine)**:
* **The Risk Envelope is Inviolable**: AI optimizes *inside* the Risk Envelope; AI does not redefine the Risk Envelope by itself.
* **Rule #0 Invariant**: `Unknown Risk = No New Risk`. If risk metrics, market state, or venue connectivity cannot be verified, the system strictly halts new exposure and enters Recovery or Defensive mode.
* **Risk-Adjusted Return Over Raw Profit**: Performance is measured by Sharpe, Sortino, Calmar, Profit Factor, Expectancy, Win/Loss Payoff, and Value at Risk (VaR/CVaR), never by unconstrained nominal P&L.

---

## 2. The Autonomous Loop

```
       ┌────────────────────────────────────────────────────────┐
       │                                                        │
       ▼                                                        │
┌──────────────┐     ┌──────────────┐     ┌──────────────┐      │
│   Observe    │ ──> │   Analyze    │ ──> │    Decide    │      │
│ Market State │     │ Alpha Scores │     │ Meta Alloc   │      │
└──────────────┘     └──────────────┘     └──────────────┘      │
                                                 │              │
                                                 ▼              │
┌──────────────┐     ┌──────────────┐     ┌──────────────┐      │
│    Verify    │ <── │   Execute    │ <── │  Risk Check  │      │
│ Fills & Pos  │     │ Adapter Gate │     │ RiskGovernor │      │
└──────────────┘     └──────────────┘     └──────────────┘      │
       │                                                        │
       ▼                                                        │
┌──────────────┐     ┌──────────────┐                           │
│    Learn     │ ──> │   Improve    │ ──────────────────────────┘
│   Lineage    │     │ PDCA / 8D    │
└──────────────┘     └──────────────┘
```

1. **Observe**: Ingest real-time order books, mark prices, funding rates, and 24h volume across 13 supported USDⓈ-M pairs (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `BNBUSDT`, `XRPUSDT`, `DOGEUSDT`, `ADAUSDT`, `AVAXUSDT`, `SUIUSDT`, `NEARUSDT`, `LINKUSDT`, `ETHUSDC`, `BTCUSDC`).
2. **Analyze**: Classify market regimes ($R_0$ through $R_6$), compute multi-horizon volatility z-scores, ATR, liquidity sweeps, and price displacement.
3. **Decide**: Generate strategy intents across Trend Breakout, Range Fade, Shock Momentum, and Structural Grid; calibrate opportunity scores ($0.0 - 1.0$).
4. **Risk Check (Authority Gate)**: `RiskGovernor` evaluates hard constraints:
   - Leverage cap ($\le 2.0\times$)
   - Drawdown limit ($\le 6.0\%$)
   - Margin utilization ($\le 70.0\%$)
   - Rule #0 check: Reject if risk state is unknown or anomalous.
5. **Execute**: `DecisionExecutionGate` enforces idempotent client order IDs, checks pre-trade L2 book depth, and submits signed orders.
6. **Verify**: Reconcile orders, positions, wallet balance, and income against authoritative Binance WebSocket user data stream.
7. **Learn**: Assemble closed-loop `TradeLineage` capturing complete provenance and grading post-trade outcome (`ALPHA`, `ACCEPTABLE_PROFIT`, `ACCEPTABLE_LOSS`, `EXCESS_SLIPPAGE`, `REGIME_MISMATCH`, `UNEXPECTED_LOSS`).
8. **Improve**: Continuous PDCA evaluation, automated 5-Why root cause diagnosis, and 8D problem solving.

---

## 3. Progressive Mainnet Deployment Pipeline

Mainnet progression requires rigorous, evidence-based qualification. Skipping stages is forbidden by automated promotion gates.

| Stage | Name | Key Verification Requirement | Promotion Gate Criteria |
|---|---|---|---|
| **Stage 1** | `OBSERVE_ONLY` | Read-only preflight passing 16/16 checks, signed account snapshot verified | Base telemetry confirmed |
| **Stage 2** | `SHADOW_TRADING` | Simulated orders against real orderbook without exchange dispatch | $\ge 10$ trades, MaxDD $\le 5\%$, Profit Factor $\ge 1.0$, 0 Rule #0 violations |
| **Stage 3** | `STAGED_FIRST_ORDER` | 1 real micro-lot order on `ETHUSDC`, manual release approval ID | $\ge 25$ trades, MaxDD $\le 4\%$, Sharpe $\ge 0.8$, 0 Rule #0 violations |
| **Stage 4** | `SMALL_LIVE` | Live trading capped at 0.5x leverage, 1 symbol | $\ge 50$ trades, MaxDD $\le 3.5\%$, Sharpe $\ge 1.0$, WinRate $\ge 50\%$ |
| **Stage 5** | `CONSTRAINED_AUTONOMOUS` | Autonomous trading across 3 symbols with strict exposure caps | $\ge 100$ trades, MaxDD $\le 3.0\%$, Sharpe $\ge 1.3$, Growth Score $\ge 70$ |
| **Stage 6** | `PORTFOLIO_AUTONOMOUS` | Full multi-symbol portfolio rebalancing | $\ge 250$ trades, MaxDD $\le 2.5\%$, Sharpe $\ge 1.6$, Sortino $\ge 2.0$ |

---

## 4. Closed-Loop Learning Subsystem

### A. End-to-End Trade Lineage (`domain/trade_lineage.py`)
Every single trade executed by Blessing AI is stamped with an immutable trace:
```
Market State (Regime, ATR, Vol Z-score)
  ↓
Strategy Intent (Direction, Delta Qty, Target Horizon)
  ↓
Opportunity Score (Expected Edge, Tail Risk Factor)
  ↓
Target Exposure & Capital Multiplier
  ↓
Risk Governor Decision (Authority Approval, Risk Class)
  ↓
Execution Orders & Exchange Fills
  ↓
Realized PnL, Fees, Funding, Slippage
  ↓
Outcome Evaluation (Edge Decay = Expected Edge - Actual Edge)
  ↓
Requires 8D Trigger Check
```

### B. 8D Problem Solving Framework (`domain/eight_d.py` & `apps/learning_engine/eight_d_manager.py`)
When a trade exhibits anomalous behavior (slippage $\ge 25$ bps, loss exceeding $2\times$ expected ATR, regime mismatch, or Rule #0 anomaly), the system auto-spawns an **Eight Disciplines (8D)** case:
* **D1 — Establish Team**: Auto-assigns responsible components (`RiskGovernor`, `BinanceExecutionGate`, `WhyWhyAnalyzer`, `ChiefRiskOfficerAgent`).
* **D2 — Describe the Problem**: Quantifies the financial and statistical gap (e.g. Expected Edge $+25$ bps vs Actual Edge $-60$ bps; financial impact in USDT).
* **D3 — Interim Containment Actions**: Immediately cools down the affected symbol, clamps leverage to $1.0\times$, or pauses new risk globally if critical.
* **D4 — Root Cause Analysis (5-Why Tree)**: Recursively queries from symptom to systemic mechanism:
  1. *Why did fill price deviate?*
  2. *Why was the order book depth thin?*
  3. *Why did volatility shock occur without pre-emptive cancellation?*
  4. *Why did the strategy predict edge during regime transition?*
  5. *What is the systemic root cause?*
* **D5 — Permanent Corrective Actions (PCA)**: Stages concrete fixes (e.g. dynamic L2 depth verification gate, 1-minute displacement velocity filters).
* **D6 — Implement & Validate PCA**: Runs shadow tests to verify the flaw is eliminated.
* **D7 — Prevent Recurrence**: Applies preventive safeguards globally across all 13 supported pairs.
* **D8 — Closure & Knowledge Cataloging**: Chief Risk Officer signoff, lessons recorded into persistent learning catalog.

### C. PDCA Continuous Improvement Loop (`apps/learning_engine/pdca_evaluator.py`)
* **Plan**: Target benchmarks per strategy (Win Rate $\ge 55\%$, Edge $\ge 20$ bps, Max Slippage $\le 5$ bps).
* **Do**: Execute and capture trade lineage in rolling windows of 20-50 trades.
* **Check**: Detect edge decay, win rate lag, and slippage excess.
* **Act**: Auto-recommend and enforce capital multiplier dampening or entry threshold tightening.

### D. Dynamic Capital Scaling (`apps/learning_engine/capital_scaling.py`)
Capital multiplier formula:
$$\text{Multiplier} = \text{Base} \times \text{PerformanceFactor} \times \text{DrawdownPenalty} \times \text{ConsistencyFactor} \times \text{ReliabilityFactor}$$
* **Rule #0 Invariant**: If `unknown_risk_violations > 0` or active critical 8D incidents exist, Multiplier is clamped to **$0.0\times$**.
* Multiplier is strictly bounded between $[0.0, 1.5]$ and can never bypass `RiskGovernor` limits.

---

## 5. Control Plane & Operator UI Integration

* **REST APIs**:
  - `GET /api/wealth/metrics`: Full risk-adjusted stats, Sharpe/Sortino/Calmar, Drawdown, Promotion Gate eligibility.
  - `GET /api/incidents/8d`: Active and historical 8D incident tracker with 5-Why trees and containment states.
  - `POST /api/incidents/8d/:incidentId/close`: Closes verified 8D incidents with audit logging.
  - `GET /api/learning/lineages`: End-to-end provenance for recent executions.
  - `GET /api/learning/pdca`: Strategy edge decay and drift diagnostics.
* **Frontend Component**:
  - `WealthGrowthDeck.tsx` integrated into `AnalyticsPage.tsx` under the **WEALTH & 8D** tab.
  - Real-time Rule #0 indicator badge, deployment pipeline status, 8D investigation cards, and PDCA tables.

---

## 6. Verification & Test Suite

* **Python Unit Tests**:
  - `tests/python/test_wealth_metrics.py`: Mathematical correctness of Sharpe, Sortino, Calmar, VaR, CVaR, Rule #0 blocks.
  - `tests/python/test_trade_lineage.py`: Provenance lifecycle, outcome grading, 8D trigger detection.
  - `tests/python/test_eight_d_learning.py`: 5-Why tree generation, 8D incident progression (D1-D8), PDCA drift detection, and dynamic capital scaling.
  - `tests/python/test_worker_wealth_api.py`: FastAPI endpoints integration test.
* **TypeScript & Frontend Tests**:
  - `vitest run`: All 15 test files passing (92/92 tests).
  - `tsc --noEmit` & `eslint .`: 0 errors, 0 warnings.
  - `npm run build`: Production client bundle and Node CJS server built successfully.
