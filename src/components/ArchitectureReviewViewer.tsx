import React, { useState } from 'react';
import { FileText, CheckCircle2, Code, Shield, GitBranch, Layers, Cpu, Server } from 'lucide-react';

export const ArchitectureReviewViewer: React.FC = () => {
  const [selectedStep, setSelectedStep] = useState<number>(1);

  const steps = [
    { num: 1, title: 'Step 1: Architecture Review', desc: 'Strengths, weaknesses, risks & assumptions' },
    { num: 2, title: 'Step 2: MVP Architecture & Diagram', desc: 'Mermaid dataflow and decoupled modules' },
    { num: 3, title: 'Step 3: Component I/O Contracts', desc: 'Strict interface contracts for all 10 services' },
    { num: 4, title: 'Step 4: Domain Models', desc: 'Basket, GridLevel, Order, Fill, Position' },
    { num: 5, title: 'Step 5: VenueAdapter Interface', desc: 'Decoupled BinanceGlobalAdapter' },
    { num: 6, title: 'Step 6: Event Schema (NATS)', desc: 'JetStream subjects & Pydantic envelopes' },
    { num: 7, title: 'Step 7: Database Schema (DDL)', desc: 'PostgreSQL 17 schema with audit indexes' },
    { num: 8, title: 'Step 8: Basket State Machine', desc: 'Mermaid lifecycle & fail-closed invariants' },
    { num: 9, title: 'Step 9: Risk Governor Rules', desc: 'Hard leverage bounds & drawdown tiers' },
    { num: 10, title: 'Step 10: Event-Driven Backtester', desc: 'Tick-level orderbook queue & funding drag' },
    { num: 11, title: 'Step 11: ML Research Pipeline', desc: 'Offline feature store & training orchestration' },
    { num: 12, title: 'Step 12: Repository Structure', desc: 'Production directory hierarchy' },
    { num: 13, title: 'Step 13: Infra & Docker Compose', desc: 'Microservices configuration & pyproject' },
    { num: 14, title: 'Step 14: GitHub Issues Roadmap', desc: 'Epics, Features, and Task breakdowns' },
    { num: 15, title: 'Step 15: Phase 0 Verification', desc: 'Public market data & foundation completed' },
  ];

  return (
    <div className="space-y-6">
      <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 space-y-4">
        <div>
          <h2 className="text-lg font-bold text-zinc-100 flex items-center space-x-2">
            <Layers className="w-5 h-5 text-indigo-400" />
            <span>Blessing AI v0.1 — 15-Step Quant Implementation Architecture</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Senior Algorithmic Trading Architect & Quant Engineer Specifications (Steps 1 through 15)
          </p>
        </div>

        {/* Step Selector Pills */}
        <div className="flex flex-wrap gap-2 pt-1">
          {steps.map((s) => (
            <button
              key={s.num}
              onClick={() => setSelectedStep(s.num)}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${
                selectedStep === s.num
                  ? 'bg-indigo-600 text-white border-indigo-500 shadow-md shadow-indigo-600/20'
                  : 'bg-zinc-950/60 text-zinc-400 border-zinc-800 hover:border-zinc-700 hover:text-zinc-200'
              }`}
            >
              Step {s.num}
            </button>
          ))}
        </div>
      </div>

      {/* Step Content Card */}
      <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-6 space-y-4">
        {selectedStep === 1 && (
          <div className="space-y-4 text-xs leading-relaxed text-zinc-300">
            <h3 className="text-base font-bold text-zinc-100 border-b border-zinc-800 pb-2">
              Step 1: In-Depth Architecture Review of Blessing AI v0.1
            </h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="bg-zinc-950/50 p-4 rounded-lg border border-zinc-800 space-y-2">
                <span className="font-bold text-emerald-400 text-sm">System Strengths</span>
                <ul className="list-disc list-inside space-y-1 text-zinc-300">
                  <li><strong>Controlled Progression:</strong> Completely abolishes 2.0x Martingale doubling in favor of capped 1.0-1.3x steps.</li>
                  <li><strong>Strategy-Risk Decoupling:</strong> Portfolio Risk Governor is structurally incapable of being bypassed by AI or Alpha logic.</li>
                  <li><strong>Funding & Basis Awareness:</strong> Direct incorporation of funding rate drag and spot-perp basis velocity as native risk parameters.</li>
                  <li><strong>Fail-Closed Design:</strong> Rejects all orders if market data, private WebSocket streams, or database transactions drop.</li>
                </ul>
              </div>

              <div className="bg-zinc-950/50 p-4 rounded-lg border border-zinc-800 space-y-2">
                <span className="font-bold text-amber-400 text-sm">Identified Weaknesses & Mitigation</span>
                <ul className="list-disc list-inside space-y-1 text-zinc-300">
                  <li><strong>Regime Transition Lag:</strong> A sudden breakout can trigger grid entries before 1h ADX updates. <em>Mitigation: Added real-time 1m realized vol Z-score + basis velocity circuit breaker.</em></li>
                  <li><strong>Adverse Selection on Grid Limit Fills:</strong> When price crashes, limit buy orders get filled immediately with negative momentum. <em>Mitigation: Adaptive distance expands up to 2.0x ATR in higher levels.</em></li>
                  <li><strong>Perpetual Funding Bleed:</strong> Sustained negative basis can erode basket profit. <em>Mitigation: Net PnL formula explicitly deducts accrued funding.</em></li>
                </ul>
              </div>
            </div>

            <div className="bg-zinc-950/50 p-4 rounded-lg border border-zinc-800 space-y-2">
              <span className="font-bold text-indigo-400 text-sm">Architectural Invariants & Explicit Assumptions</span>
              <p>
                1. <strong>Deterministic Live Path:</strong> AI models only produce advisory probabilities; they have zero direct order execution rights.<br />
                2. <strong>Decoupled Venue:</strong> The Strategy layer interacts exclusively with <code>VenueAdapter</code>; no Binance SDK imports outside of <code>/venues</code>.<br />
                3. <strong>Strict Capital Cap:</strong> Maximum effective leverage cannot exceed 2.0x under any circumstances.
              </p>
            </div>
          </div>
        )}

        {selectedStep === 2 && (
          <div className="space-y-4 text-xs leading-relaxed text-zinc-300">
            <h3 className="text-base font-bold text-zinc-100 border-b border-zinc-800 pb-2">
              Step 2: MVP Architecture & Mermaid Dataflow
            </h3>
            <div className="bg-zinc-950/80 p-4 rounded-lg border border-zinc-800 font-mono text-[11px] text-zinc-300 overflow-x-auto">
              <pre>{`flowchart TD
    subgraph MarketData [Market Data Layer]
        BinanceWS[Binance WebSocket Feed] --> MarketService[Market Data Ingestion Service]
        BinanceREST[Binance REST API] --> MarketService
        MarketService -->|Trades, Depth, Funding, MarkPrice| NATS[NATS JetStream Bus]
    end

    subgraph FeatureAndAI [Feature & AI Layer]
        NATS --> FeatureEngine[Real-time Feature Engine]
        FeatureEngine --> RegimeEngine[Market Regime Engine (R0-R6)]
        FeatureEngine --> GridSafetyAI[AI Grid Safety Score Model]
        RegimeEngine -->|Regime State| NATS
        GridSafetyAI -->|Safety Score 0-100| NATS
    end

    subgraph StrategyAndRisk [Core Strategy & Execution]
        NATS --> StrategyEngine[Blessing Grid Strategy Engine]
        StrategyEngine --> BasketSM[Basket State Machine]
        BasketSM -->|Proposed Order| RiskGovernor[Portfolio Risk Governor]
        RiskGovernor -->|Approved / Blocked| ExecutionEngine[Execution Engine]
        ExecutionEngine --> VenueAdapter[Binance USDM VenueAdapter]
        VenueAdapter --> BinanceExchange((Binance Exchange API))
    end

    subgraph StorageAndAudit [Persistence & State]
        ExecutionEngine --> DB[(PostgreSQL 17 / TimescaleDB)]
        BasketSM --> DB
        RiskGovernor --> Redis[(Redis State Cache)]
    end`}</pre>
            </div>
          </div>
        )}

        {selectedStep === 7 && (
          <div className="space-y-4 text-xs leading-relaxed text-zinc-300">
            <h3 className="text-base font-bold text-zinc-100 border-b border-zinc-800 pb-2">
              Step 7: PostgreSQL 17 / TimescaleDB DDL Schema
            </h3>
            <p className="text-zinc-400">
              Generated in <code>/infra/postgres/init_schema.sql</code>. All numerical values use exact Decimal(28, 10) precision.
            </p>
            <div className="bg-zinc-950/80 p-4 rounded-lg border border-zinc-800 font-mono text-[11px] text-zinc-300 overflow-x-auto max-h-96">
              <pre>{`-- 3. Baskets (First-Class Entity for Adaptive Basket Recovery)
CREATE TABLE IF NOT EXISTS baskets (
    basket_id VARCHAR(64) PRIMARY KEY,
    strategy_id VARCHAR(64) REFERENCES strategies(strategy_id),
    venue VARCHAR(32) NOT NULL,
    instrument VARCHAR(32) REFERENCES instruments(symbol),
    direction VARCHAR(8) NOT NULL,           -- LONG, SHORT
    state VARCHAR(32) NOT NULL,              -- NEW, ACTIVE, GRID_EXPANDING, PROFITABLE, RECOVERY, ...
    grid_depth INT DEFAULT 0,
    max_grid_levels INT NOT NULL,
    total_size NUMERIC(28, 10) DEFAULT 0.0,
    average_entry NUMERIC(28, 10) DEFAULT 0.0,
    current_mark_price NUMERIC(28, 10) DEFAULT 0.0,
    target_tp_price NUMERIC(28, 10),
    stop_loss_price NUMERIC(28, 10),
    realized_pnl NUMERIC(28, 10) DEFAULT 0.0,
    unrealized_pnl NUMERIC(28, 10) DEFAULT 0.0,
    trading_fees NUMERIC(28, 10) DEFAULT 0.0,
    funding_accrued NUMERIC(28, 10) DEFAULT 0.0,
    slippage_cost NUMERIC(28, 10) DEFAULT 0.0,
    net_pnl NUMERIC(28, 10) DEFAULT 0.0,
    grid_safety_score_entry NUMERIC(6, 2),
    market_regime_entry VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL,
    last_updated TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ
);`}</pre>
            </div>
          </div>
        )}

        {selectedStep === 8 && (
          <div className="space-y-4 text-xs leading-relaxed text-zinc-300">
            <h3 className="text-base font-bold text-zinc-100 border-b border-zinc-800 pb-2">
              Step 8: Basket State Machine Transitions & Invariants
            </h3>
            <div className="bg-zinc-950/80 p-4 rounded-lg border border-zinc-800 font-mono text-[11px] text-zinc-300 overflow-x-auto">
              <pre>{`stateDiagram-v2
    [*] --> NEW
    NEW --> ACTIVE : Signal Confirmed & Safety Score >= 65
    NEW --> EMERGENCY_EXIT : Pre-trade Risk Check Failed

    ACTIVE --> GRID_EXPANDING : Next Grid Step Triggered & Safety Score >= 50
    GRID_EXPANDING --> ACTIVE : Fill Confirmed & Avg Entry Updated
    GRID_EXPANDING --> NO_NEW_GRID : Max Depth (L5) Reached

    ACTIVE --> PROFITABLE : Mark Price reaches Net Basket TP
    PROFITABLE --> CLOSING : Trailing Stop Triggered / Limit Hit
    CLOSING --> CLOSED : All Fills Reconciled

    ACTIVE --> RECOVERY : Floating Drawdown > Recovery Threshold
    RECOVERY --> PROFITABLE : Average Entry Reached on Pullback
    RECOVERY --> DELEVERAGING : Adverse Trend Continues
    DELEVERAGING --> RECOVERY : Partial Size Liquidated
    DELEVERAGING --> CLOSING : Basket Reduced to Flat

    ACTIVE --> NO_NEW_GRID : Risk Governor State = NO_NEW_GRID
    NO_NEW_GRID --> RECOVERY : Price Retraces
    NO_NEW_GRID --> EMERGENCY_EXIT : Portfolio Drawdown >= 8%

    ANY_STATE --> EMERGENCY_EXIT : Circuit Breaker / Kill Switch
    EMERGENCY_EXIT --> CLOSED : Orders Cancelled & Positions Flattened
    CLOSED --> [*]`}</pre>
            </div>
          </div>
        )}

        {selectedStep === 14 && (
          <div className="space-y-4 text-xs leading-relaxed text-zinc-300">
            <h3 className="text-base font-bold text-zinc-100 border-b border-zinc-800 pb-2">
              Step 14: GitHub Issues Breakdown (Epics, Features & Tasks)
            </h3>
            <div className="space-y-3">
              <div className="bg-zinc-950/60 p-3.5 rounded-lg border border-zinc-800">
                <span className="font-bold text-indigo-400">Epic 1: Event-Driven Infrastructure & Market Ingestion</span>
                <p className="text-zinc-400 mt-1">• Task 1.1: Deploy NATS JetStream cluster and stream retention policies.<br />• Task 1.2: Implement native Binance WebSocket multiplexer with heartbeat monitoring.<br />• Task 1.3: Setup TimescaleDB hypertables for tick trades and 1m mark prices.</p>
              </div>

              <div className="bg-zinc-950/60 p-3.5 rounded-lg border border-zinc-800">
                <span className="font-bold text-emerald-400">Epic 2: Core Basket Engine & Adaptive Grid Strategy</span>
                <p className="text-zinc-400 mt-1">• Task 2.1: Implement Basket state machine with strict transition guards.<br />• Task 2.2: Implement adaptive ATR grid calculator with anti-martingale volume progression.<br />• Task 2.3: Build real-time Net PnL reconciliation factoring funding accrual.</p>
              </div>

              <div className="bg-zinc-950/60 p-3.5 rounded-lg border border-zinc-800">
                <span className="font-bold text-amber-400">Epic 3: Market Regime Engine & AI Grid Safety Scoring</span>
                <p className="text-zinc-400 mt-1">• Task 3.1: Train CatBoost 7-state regime classifier on historical tick data.<br />• Task 3.2: Implement multi-target Grid Safety inference model for MAE and drawdown expectation.<br />• Task 3.3: Embed live inference caching with fallback heuristic rules.</p>
              </div>

              <div className="bg-zinc-950/60 p-3.5 rounded-lg border border-zinc-800">
                <span className="font-bold text-rose-400">Epic 4: Portfolio Risk Governor & Execution Protection</span>
                <p className="text-zinc-400 mt-1">• Task 4.1: Code non-overridable hard leverage limit (2.0x) and margin check.<br />• Task 4.2: Build multi-tier drawdown escalation logic (Caution 2% to Emergency 8%).<br />• Task 4.3: Implement emergency circuit breaker and automated position flattening.</p>
              </div>
            </div>
          </div>
        )}

        {selectedStep === 15 && (
          <div className="space-y-4 text-xs leading-relaxed text-zinc-300">
            <h3 className="text-base font-bold text-zinc-100 border-b border-zinc-800 pb-2">
              Step 15: Phase 0 Verification & Deliverables Summary
            </h3>
            <div className="bg-zinc-950/60 p-4 rounded-lg border border-zinc-800 space-y-2">
              <span className="font-bold text-emerald-400 text-sm">Completed Phase 0 Milestones:</span>
              <ul className="list-disc list-inside space-y-1 text-zinc-300">
                <li><code>pyproject.toml</code>: Configured Python 3.13 dependencies (FastAPI, NATS, DuckDB, Polars, CatBoost).</li>
                <li><code>docker-compose.yml</code>: Configured NATS JetStream, PostgreSQL 17, Redis, MLflow, Prometheus, and Grafana.</li>
                <li><code>infra/postgres/init_schema.sql</code>: Executable PostgreSQL DDL with 11 relational tables and audit indexes.</li>
                <li><code>venues/base/adapter.py</code> & <code>venues/binance_global/usdm.py</code>: Strict VenueAdapter interface eliminating CCXT from the execution hot path.</li>
                <li><code>core/basket/models.py</code> & <code>core/basket/state_machine.py</code>: Domain entities and fail-closed state machine.</li>
                <li><code>core/grid/adaptive_grid.py</code>: ATR volatility distance and controlled anti-martingale progression.</li>
                <li><code>core/risk/governor.py</code>: Non-overridable hard rules, margin bounds, and drawdown tiers.</li>
                <li><code>data/collectors/binance_collector.py</code>: Async market collector for public feeds.</li>
                <li><code>Interactive Cockpit</code>: Real-time UI dashboard with account metrics, grid ladders, backtest replay, and Gemini AI research assistant.</li>
              </ul>
            </div>
          </div>
        )}

        {![1, 2, 7, 8, 14, 15].includes(selectedStep) && (
          <div className="space-y-3 text-xs leading-relaxed text-zinc-300">
            <h3 className="text-base font-bold text-zinc-100 border-b border-zinc-800 pb-2">
              {steps.find(s => s.num === selectedStep)?.title}
            </h3>
            <p className="text-zinc-400">{steps.find(s => s.num === selectedStep)?.desc}</p>
            <div className="bg-zinc-950/60 p-4 rounded-lg border border-zinc-800">
              <span className="font-mono text-emerald-400">Status: Fully Designed & Documented in Project Repository.</span>
              <p className="mt-2 text-zinc-300">
                Refer to <code>/core/</code>, <code>/venues/</code>, <code>/ai/</code>, and <code>/config/</code> in the project workspace for full Python code and configuration files.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
